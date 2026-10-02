"""Original supplier identity and request replay under real PostgreSQL locks."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Barrier
from unittest import skipUnless
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection, connections
from django.test import TransactionTestCase

from core.models import AuditEvent, DocumentCounter, Membership, Organization
from finance import services as f
from finance.models import Bill, Journal, Party


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL row locks")
class ConcurrentSupplierIdentityTests(TransactionTestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Synthetic concurrent bills")
        self.owner = get_user_model().objects.create_user("supplier-race-owner")
        Membership.objects.create(organization=self.org, user=self.owner, role="owner")
        self.supplier = Party.objects.create(organization=self.org, name="Synthetic supplier", kind="supplier")

    def race(self, keys):
        ready = Barrier(len(keys))

        def submit(key):
            close_old_connections()
            try:
                self.assertTrue(connections["default"].settings_dict["NAME"].startswith("test_"))
                ready.wait(timeout=10)
                try:
                    result = f.create_bill(organization=self.org.pk, actor=self.owner, supplier=self.supplier.pk,
                        amount="500000", date=date(2026, 1, 10), service_date=date(2026, 1, 10),
                        supplier_reference="SOURCE-ORIGINAL", submission_key=key)
                    return result.pk
                except ValidationError:
                    return "rejected"
            finally:
                connections["default"].close()

        with ThreadPoolExecutor(max_workers=len(keys)) as pool:
            results = [pool.submit(submit, key) for key in keys]
            return [result.result(timeout=30) for result in results]

    def assert_one_document(self):
        self.assertEqual(Bill.objects.count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action="finance.bill.created").count(), 1)
        self.assertEqual(DocumentCounter.objects.get(organization=self.org, kind="BILL").value, 1)
        self.assertFalse(Journal.objects.exists())

    def test_identical_concurrent_submissions_return_one_shared_bill(self):
        key = uuid4()
        results = self.race([key, key])
        self.assertEqual(results, [Bill.objects.get().pk] * 2)
        self.assert_one_document()

    def test_different_concurrent_requests_for_same_supplier_document_reject_one(self):
        results = self.race([uuid4(), uuid4()])
        self.assertCountEqual(results, [Bill.objects.get().pk, "rejected"])
        self.assert_one_document()
