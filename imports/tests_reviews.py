"""Source review decisions preserve the imported record and financial boundary."""

import io

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from core.models import AuditEvent, Membership
from finance.models import BankTransaction, Journal

from . import review_services as reviews
from . import tests as fixtures
from .services import import_bundle, import_summary


@override_settings(MFA_REQUIRED=False)
class SourceExceptionReviewTests(TestCase):
    setUp = fixtures.SourceImportTests.setUp
    write_bundle = fixtures.SourceImportTests.write_bundle
    run_import = fixtures.SourceImportTests.run_import

    def test_resolve_reopen_and_correct_are_append_only_with_stale_conflict_protection(
        self,
    ):
        batch = self.run_import()
        exception = batch.exceptions.get()
        source = batch.source_documents.get()
        original = exception.payload.copy()
        facts = {
            "organization": self.org,
            "actor": self.owner,
            "exception": exception,
            "responsible_user": self.owner,
            "reason": "Confirmed year against source",
            "evidence": "Original PDF page 1 and registration record",
            "correction": "Use 2026-01-05 when interpreting row-1; original printed year is 2025",
            "source_document": source,
        }
        first = reviews.review_exception(
            **facts, decision="resolved", expected_previous_id=None
        )
        self.assertEqual(
            reviews.exception_state(organization=self.org, exception=exception)[
                "status"
            ],
            "resolved",
        )
        self.assertEqual(import_summary(organization=self.org)["unresolved_count"], 0)
        with self.assertRaises(ValidationError):
            reviews.review_exception(
                **facts, decision="resolved", expected_previous_id=None
            )
        second = reviews.review_exception(
            **facts, decision="reopened", expected_previous_id=first.pk
        )
        self.assertEqual(second.supersedes_id, first.pk)
        self.assertEqual(
            reviews.exception_state(organization=self.org, exception=exception)[
                "status"
            ],
            "open",
        )
        third = reviews.review_exception(
            **facts, decision="resolved", expected_previous_id=second.pk
        )
        self.assertEqual(third.supersedes_id, second.pk)
        self.assertEqual(exception.reviews.count(), 3)
        exception.refresh_from_db()
        batch.refresh_from_db()
        self.assertEqual(exception.status, "open")
        self.assertEqual(exception.payload, original)
        self.assertEqual(batch.payload, self.bundle)
        self.assertEqual(import_summary(organization=self.org)["reported_amount"], 100)
        self.assertFalse(Journal.objects.exists())
        self.assertFalse(BankTransaction.objects.exists())
        self.assertEqual(
            AuditEvent.objects.filter(action__startswith="imports.exception.").count(),
            3,
        )
        with self.assertRaises(ValidationError):
            first.delete()
        first.reason = "overwrite"
        with self.assertRaises(ValidationError):
            first.save()

    def proof(self):
        output = io.BytesIO()
        Image.new("RGB", (2, 2), "white").save(output, format="PNG")
        return SimpleUploadedFile(
            "synthetic-proof.png", output.getvalue(), content_type="image/png"
        )

    def test_public_inbox_resolution_evidence_download_and_revised_basis(self):
        batch = self.run_import()
        exception = batch.exceptions.get()
        self.client.force_login(self.owner)
        url = reverse("import_exception_review", args=[exception.pk])
        self.assertContains(
            self.client.get(reverse("import_exception_inbox")), exception.message
        )
        self.assertEqual(self.client.get(url).status_code, 200)
        data = {
            "responsible_user": self.owner.pk,
            "decision": "resolved",
            "reason": "Matched registration evidence",
            "evidence": "Registration proof line 1",
            "correction": "2026-01-05 is confirmed by registration",
        }
        self.assertContains(self.client.post(url, data), "dokumen sumber atau lampiran")
        response = self.client.post(url, {**data, "upload": self.proof()})
        self.assertEqual(response.status_code, 302)
        review = exception.reviews.get()
        self.assertEqual(review.original_name, "synthetic-proof.png")
        self.assertContains(self.client.get(url), "2026-01-05 is confirmed")
        download = self.client.get(
            reverse("import_exception_evidence", args=[review.pk])
        )
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download["Cache-Control"], "private, no-store")
        # Consuming the test client's stream closes it without closing TestCase's DB transaction.
        self.assertTrue(b"".join(download.streaming_content).startswith(b"\x89PNG"))
        self.assertTrue(download.closed)
        state = reviews.exception_state(organization=self.org, exception=exception)
        self.assertEqual(state["status"], "resolved")
        with open(review.file.path, "wb") as handle:
            handle.write(b"tampered proof")
        stale = reviews.exception_state(organization=self.org, exception=exception)
        self.assertEqual(stale["status"], "open")
        self.assertTrue(stale["requires_recheck"])
        self.assertNotEqual(state["fingerprint"], stale["fingerprint"])
        self.assertEqual(import_summary(organization=self.org)["unresolved_count"], 1)

    def test_scope_roles_inactive_pic_and_stale_attempt_cannot_change_or_leak_evidence(
        self,
    ):
        batch = self.run_import()
        exception = batch.exceptions.get()
        facts = {
            "organization": self.org,
            "actor": self.owner,
            "exception": exception,
            "responsible_user": self.owner,
            "reason": "Check source",
            "evidence": "PDF page 1",
            "correction": "Year 2026",
            "source_document": batch.source_documents.get(),
            "decision": "resolved",
            "expected_previous_id": None,
        }
        reviewer = get_user_model().objects.create_user("other-company-reviewer")
        Membership.objects.create(organization=self.other, user=reviewer, role="owner")
        with self.assertRaises(PermissionDenied):
            reviews.review_exception(**{**facts, "actor": reviewer})
        with self.assertRaises(ValidationError):
            reviews.review_exception(**{**facts, "responsible_user": reviewer})
        other_batch = import_bundle(
            organization=self.other, bundle_path=self.write_bundle()
        )
        with self.assertRaises(ValidationError):
            reviews.review_exception(
                **{**facts, "source_document": other_batch.source_documents.get()}
            )
        review = reviews.review_exception(**facts, upload=self.proof())
        before_files = list(self.storage.rglob("*.*"))
        with self.assertRaises(ValidationError):
            reviews.review_exception(**facts, upload=self.proof())
        self.assertEqual(list(self.storage.rglob("*.*")), before_files)
        self.assertEqual(exception.reviews.count(), 1)
        self.client.force_login(reviewer)
        self.assertEqual(
            self.client.get(
                reverse("import_exception_review", args=[exception.pk])
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                reverse("import_exception_evidence", args=[review.pk])
            ).status_code,
            404,
        )
        marketer = get_user_model().objects.create_user("source-marketer")
        Membership.objects.create(
            organization=self.org, user=marketer, role="marketing"
        )
        self.client.force_login(marketer)
        self.assertEqual(
            self.client.get(reverse("import_exception_inbox")).status_code, 403
        )
        with self.assertRaises(PermissionDenied):
            reviews.review_exception(**{**facts, "actor": marketer})
        self.owner.is_active = False
        self.owner.save()
        with self.assertRaises(PermissionDenied):
            reviews.review_exception(**facts)

    def test_source_change_and_missing_correction_block_resolution_while_assignment_is_recorded(
        self,
    ):
        batch = self.run_import()
        exception = batch.exceptions.get()
        source = batch.source_documents.get()
        facts = {
            "organization": self.org,
            "actor": self.owner,
            "exception": exception,
            "responsible_user": self.owner,
            "reason": "Need original confirmation",
            "evidence": "Original PDF page 1",
            "source_document": source,
            "expected_previous_id": None,
        }
        assignment = reviews.review_exception(**facts, decision="open")
        self.assertEqual(assignment.responsible_user, self.owner)
        self.assertEqual(
            reviews.exception_state(organization=self.org, exception=exception)[
                "status"
            ],
            "open",
        )
        facts["expected_previous_id"] = assignment.pk
        with self.assertRaises(ValidationError):
            reviews.review_exception(**facts, decision="resolved")
        with open(source.file.path, "wb") as handle:
            handle.write(b"changed original")
        with self.assertRaisesMessage(ValidationError, "sumber berubah"):
            reviews.review_exception(
                **facts, decision="resolved", correction="Year 2026"
            )
        self.assertEqual(exception.reviews.count(), 1)
