"""Source import and review races against PostgreSQL's organization lock."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from unittest import skipUnless

from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection, connections
from django.test import TransactionTestCase

from core.models import AuditEvent
from finance.models import Bill, Journal

from . import tests as fixtures
from .models import ImportBatch
from .review_services import review_exception
from .services import import_bundle


@skipUnless(connection.vendor == "postgresql", "Requires real PostgreSQL row locks")
class ConcurrentSourceReviews(TransactionTestCase):
    setUp = fixtures.SourceImportTests.setUp
    write_bundle = fixtures.SourceImportTests.write_bundle
    add_supplier = fixtures.SourceImportTests.add_supplier

    def race(self, *actions):
        ready = Barrier(len(actions))

        def run(action):
            close_old_connections()
            try:
                self.assertTrue(
                    connections["default"].settings_dict["NAME"].startswith("test_")
                )
                ready.wait(timeout=10)
                try:
                    return "committed", action().pk
                except ValidationError:
                    return "rejected", None
            finally:
                connections["default"].close()

        with ThreadPoolExecutor(max_workers=len(actions)) as pool:
            pending = [pool.submit(run, action) for action in actions]
            return [future.result(timeout=30) for future in pending]

    def test_two_public_imports_link_one_supplier_document_without_duplicate_bill(self):
        self.add_supplier()
        first = self.write_bundle()
        second = deepcopy(self.bundle)
        content = b"%PDF-1.7\nSynthetic second month\n%%EOF"
        (self.root / "second.pdf").write_bytes(content)
        second["sources"][0].update(
            relative_path="second.pdf", sha256=hashlib.sha256(content).hexdigest()
        )
        second["months"][0]["source_period"] = "2026-02"
        second_path = self.root / "second.json"
        second_path.write_text(json.dumps(second), encoding="utf-8")
        result = self.race(
            *[
                lambda path=path: import_bundle(
                    organization=self.org, actor=self.owner, bundle_path=path
                )
                for path in (first, second_path)
            ]
        )
        self.assertEqual([row[0] for row in result], ["committed", "committed"])
        self.assertEqual(Bill.objects.count(), 1)
        self.assertEqual(ImportBatch.objects.count(), 2)
        self.assertEqual(
            len(set(ImportBatch.objects.values_list("supplier_bill_id", flat=True))), 1
        )
        self.assertFalse(Journal.objects.exists())

    def test_competing_exception_decisions_have_one_current_version_and_one_stale_rejection(
        self,
    ):
        batch = import_bundle(
            organization=self.org, actor=self.owner, bundle_path=self.write_bundle()
        )
        exception = batch.exceptions.get()
        facts = {
            "organization": self.org,
            "actor": self.owner,
            "exception": exception,
            "responsible_user": self.owner,
            "reason": "Synthetic independent review",
            "evidence": "Original page 1",
            "correction": "Year interpreted as 2026",
            "source_document": batch.source_documents.get(),
            "expected_previous_id": None,
        }
        result = self.race(
            *[
                lambda decision=decision: review_exception(**facts, decision=decision)
                for decision in ("open", "resolved")
            ]
        )
        self.assertCountEqual([row[0] for row in result], ["committed", "rejected"])
        self.assertEqual(exception.reviews.count(), 1)
        self.assertEqual(
            AuditEvent.objects.filter(action__startswith="imports.exception.").count(),
            1,
        )
