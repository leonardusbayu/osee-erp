"""Regression boundaries for tax draft recovery and shared document validation."""

import io
import json
import zipfile
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse
from pypdf import PdfWriter

from core.models import AuditEvent, Membership, Organization
from evidence.services import inspect_upload
from taxes import tests_workflow as fixtures
from taxes import workflow as work
from taxes.models import TaxEvidence, TaxObligation, TaxObligationWithdrawal
from taxes.services import annual_readiness, monthly_tax_context


class RecoveryFixtures:
    setUp = fixtures.TaxReleaseWorkflowTests.setUp
    evidence = fixtures.TaxReleaseWorkflowTests.evidence
    external = fixtures.TaxReleaseWorkflowTests.external
    profile = fixtures.TaxReleaseWorkflowTests.profile
    identity = fixtures.TaxReleaseWorkflowTests.identity
    pph23_context = fixtures.TaxReleaseWorkflowTests.pph23_context

    def obligation(self):
        return work.prepare_obligation(
            organization=self.org,
            actor=self.owner,
            period="2025-09",
            rule_code="pph23_service_2",
            base="1000000",
            direction="payable",
            source_reference="SYN-SAME-SOURCE",
            rule_reference="Synthetic documented service",
            object_code="24-104-99",
            source_evidence=self.evidence(),
            **self.pph23_context(direction="payable", tax_point_date=date(2025, 9, 10)),
        )


class DraftRecoveryTests(RecoveryFixtures, TestCase):
    def withdraw(self, row, **kwargs):
        return work.withdraw_obligation(
            organization=self.org,
            actor=self.owner,
            obligation=row,
            reason=kwargs.pop("reason", "Sumber tidak sengaja dicatat dua kali"),
            expected_version=kwargs.pop("expected_version", row.updated_at.isoformat()),
            **kwargs,
        )

    def test_withdrawal_is_idempotent_immutable_and_cannot_be_approved_or_revived(self):
        row = self.obligation()
        evidence_id, version = row.source_evidence_id, row.updated_at.isoformat()
        review = self.external(row, {"action": "approve_obligation"})
        event = self.withdraw(row)
        retry = self.withdraw(row, expected_version=version)
        self.assertEqual(event.pk, retry.pk)
        self.assertEqual(TaxObligationWithdrawal.objects.count(), 1)
        self.assertEqual(
            AuditEvent.objects.filter(action="tax.obligation.withdrawn").count(), 1
        )
        row.refresh_from_db()
        self.assertEqual(row.source_evidence_id, evidence_id)
        self.assertEqual(event.snapshot["source_reference"], "SYN-SAME-SOURCE")
        for operation in (
            lambda: self.withdraw(row, reason="Berusaha mengganti alasan lama"),
            lambda: event.delete(),
            lambda: row.delete(),
            lambda: TaxObligation.objects.filter(pk=row.pk).update(status="review"),
            lambda: work.approve_tax_obligation(
                row, self.owner, external_review=review
            ),
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(ValidationError):
                    operation()
        event.reason = "Berusaha mengganti alasan lama"
        with self.assertRaises(ValidationError):
            event.save()
        row.status = "review"
        with self.assertRaises(ValidationError):
            row.save()

    def test_withdrawal_rejects_unexplained_stale_and_approved_records(self):
        row = self.obligation()
        for reason in ("", "  ", "x", "x" * 2001):
            with self.subTest(reason=reason[:10]), self.assertRaises(ValidationError):
                self.withdraw(row, reason=reason)
        old_version = row.updated_at.isoformat()
        row.note = "Catatan berubah sesudah halaman dibuka"
        row.save()
        with self.assertRaisesMessage(ValidationError, "berubah sejak halaman"):
            self.withdraw(row, expected_version=old_version)
        approved = work.approve_tax_obligation(
            row,
            self.owner,
            external_review=self.external(row, {"action": "approve_obligation"}),
        )
        with self.assertRaisesMessage(ValidationError, "koreksi formal"):
            self.withdraw(approved)
        self.assertFalse(TaxObligationWithdrawal.objects.exists())

    def test_bill_linked_drafts_cannot_detach_financial_review(self):
        from finance.models import Bill, Party

        supplier = Party.objects.create(
            organization=self.org, name="Synthetic supplier", kind="supplier"
        )
        bill = Bill.objects.create(
            organization=self.org,
            supplier=supplier,
            number="SYN-BILL",
            amount=Decimal(1000000),
            date=date(2025, 9, 1),
            service_date=date(2025, 9, 1),
        )
        row = self.obligation()
        row.source_bill = bill
        row.save()
        with self.assertRaisesMessage(ValidationError, "terkait tagihan"):
            self.withdraw(row)
        row.refresh_from_db()
        self.assertEqual(row.status, "review")
        self.assertEqual(row.source_bill_id, bill.pk)

    def test_current_scope_and_role_are_required_even_for_retries(self):
        row = self.obligation()
        other = Organization.objects.create(name="Synthetic other")
        Membership.objects.create(organization=other, user=self.owner, role="owner")
        with self.assertRaises(ValidationError):
            work.withdraw_obligation(
                organization=other,
                actor=self.owner,
                obligation=row,
                reason="Wrong organization request",
                expected_version=row.updated_at.isoformat(),
            )
        outsider = get_user_model().objects.create_user(username="synthetic-auditor")
        membership = Membership.objects.create(
            organization=self.org, user=outsider, role="auditor"
        )
        with self.assertRaises(PermissionDenied):
            work.withdraw_obligation(
                organization=self.org,
                actor=outsider,
                obligation=row,
                reason="Auditor must not withdraw",
                expected_version=row.updated_at.isoformat(),
            )
        membership.role = "finance"
        membership.save()
        event = work.withdraw_obligation(
            organization=self.org,
            actor=outsider,
            obligation=row,
            reason="Finance corrects duplicate draft",
            expected_version=row.updated_at.isoformat(),
        )
        membership.is_active = False
        membership.save()
        with self.assertRaises(PermissionDenied):
            work.withdraw_obligation(
                organization=self.org,
                actor=outsider,
                obligation=row,
                reason=event.reason,
                expected_version=event.source_version,
            )

    def test_withdrawal_recovers_duplicate_without_changing_books_or_bypassing_annual_review(
        self,
    ):
        from django.utils import timezone

        from finance.models import AccountingPeriod

        self.profile()
        for month in range(1, 13):
            AccountingPeriod.objects.create(
                organization=self.org,
                year=2025,
                month=month,
                closed=True,
                closed_at=timezone.now(),
            )
        correct = self.obligation()
        work.approve_tax_obligation(
            correct,
            self.owner,
            external_review=self.external(correct, {"action": "approve_obligation"}),
        )
        duplicate = self.obligation()

        def prepare_year():
            return work.prepare_annual_workpaper(
                organization=self.org,
                actor=self.owner,
                year=2025,
                positive_adjustments="0",
                negative_adjustments="0",
                loss_compensation="0",
                turnover="0",
                regime_code="corporate_22",
                instalments="0",
                support_evidence=self.evidence(),
                reconciliation_note="Synthetic inactive year",
            )

        stale = prepare_year()
        stale_review = self.external(stale, {"action": "approve_annual"})
        with self.assertRaisesMessage(
            ValidationError, "Selesaikan pemeriksaan seluruh"
        ):
            work.approve_annual_workpaper(
                organization=self.org,
                actor=self.owner,
                workpaper=stale,
                external_review=stale_review,
            )
        withdrawal = work.withdraw_obligation(
            organization=self.org,
            actor=self.owner,
            obligation=duplicate,
            reason="Sumber yang sama tidak sengaja dimasukkan dua kali",
            expected_version=duplicate.updated_at.isoformat(),
        )
        duplicate.refresh_from_db()
        self.assertEqual(duplicate.status, "withdrawn")
        self.assertEqual(withdrawal.actor, self.owner)
        self.assertEqual(withdrawal.snapshot["status"], "review")
        self.assertEqual(withdrawal.workflow_version, "tax-withdrawal-v1")
        monthly = monthly_tax_context(self.org, "2025-09")
        self.assertEqual([row.pk for row in monthly["obligations"]], [correct.pk])
        self.assertEqual(
            [row.pk for row in monthly["withdrawn_obligations"]], [duplicate.pk]
        )
        self.assertEqual(monthly["tax_totals"][0]["amount"], Decimal(20000))
        self.assertEqual(monthly["review_count"], 0)
        with self.assertRaisesMessage(
            ValidationError, "Bukti pajak atau keputusan profil berubah"
        ):
            work.approve_annual_workpaper(
                organization=self.org,
                actor=self.owner,
                workpaper=stale,
                external_review=stale_review,
            )
        fresh = prepare_year()
        self.assertNotEqual(stale.pk, fresh.pk)
        approval = work.approve_annual_workpaper(
            organization=self.org,
            actor=self.owner,
            workpaper=fresh,
            external_review=self.external(fresh, {"action": "approve_annual"}),
        )
        self.assertEqual(approval.workpaper, fresh)
        self.assertEqual(annual_readiness(self.org, 2025)["closed_month_count"], 12)


def pdf_bytes(*, javascript=False, encrypted=False, attachment=False, pages=1):
    output = io.BytesIO()
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=100, height=100)
    if javascript:
        writer.add_js("/* inert test marker; never executed */")
    if encrypted:
        writer.encrypt("synthetic-only")
    if attachment:
        writer.add_attachment("marker.txt", b"inert synthetic attachment")
    writer.write(output)
    return output.getvalue()


class PDFPolicyTests(TestCase):
    setUp = fixtures.TaxReleaseWorkflowTests.setUp

    def test_tax_and_main_evidence_reject_the_same_active_pdf(self):
        content = pdf_bytes(javascript=True)
        with self.assertRaises(ValidationError):
            inspect_upload(SimpleUploadedFile("active.pdf", content))
        with self.assertRaises(ValidationError):
            work.upload_evidence(
                organization=self.org,
                actor=self.owner,
                file=SimpleUploadedFile("active.pdf", content),
            )

    def test_ordinary_pdf_is_preserved_and_other_unsafe_documents_are_rejected(self):
        good = pdf_bytes()
        self.assertEqual(
            inspect_upload(SimpleUploadedFile("ordinary.pdf", good))["data"], good
        )
        saved = work.upload_evidence(
            organization=self.org,
            actor=self.owner,
            file=SimpleUploadedFile("ordinary.pdf", good),
        )
        self.assertEqual(bytes(saved.content), good)
        for content in (
            pdf_bytes(encrypted=True),
            pdf_bytes(attachment=True),
            b"%PDF-1.7\nnot a PDF",
        ):
            with self.subTest(content=content[:20]):
                with self.assertRaises(ValidationError):
                    inspect_upload(SimpleUploadedFile("invalid.pdf", content))
                with self.assertRaises(ValidationError):
                    work.upload_evidence(
                        organization=self.org,
                        actor=self.owner,
                        file=SimpleUploadedFile("invalid.pdf", content),
                    )
        self.assertEqual(TaxEvidence.objects.count(), 1)

    def test_tax_specific_page_and_byte_limits_remain_stricter(self):
        medium = pdf_bytes(pages=201)
        inspect_upload(SimpleUploadedFile("ordinary-long.pdf", medium))
        with self.assertRaisesMessage(ValidationError, "200 halaman"):
            work.upload_evidence(
                organization=self.org,
                actor=self.owner,
                file=SimpleUploadedFile("ordinary-long.pdf", medium),
            )
        with self.assertRaises(ValidationError):
            inspect_upload(
                SimpleUploadedFile("too-many-pages.pdf", pdf_bytes(pages=2001))
            )
        # Metadata extends a valid PDF without trailing-garbage ambiguity.
        writer, out = PdfWriter(), io.BytesIO()
        writer.add_blank_page(width=100, height=100)
        writer.add_metadata({"/Title": "x" * (2 * 1024 * 1024)})
        writer.write(out)
        inspect_upload(SimpleUploadedFile("large.pdf", out.getvalue()))
        with self.assertRaisesMessage(ValidationError, "2 MB"):
            work.upload_evidence(
                organization=self.org,
                actor=self.owner,
                file=SimpleUploadedFile("large.pdf", out.getvalue()),
            )


class WithdrawalHTTPTests(RecoveryFixtures, TestCase):
    def test_stale_annual_page_and_download_identify_changed_tax_snapshot(self):
        row = self.obligation()
        annual = work.prepare_annual_workpaper(
            organization=self.org,
            actor=self.owner,
            year=2025,
            positive_adjustments="0",
            negative_adjustments="0",
            loss_compensation="0",
            turnover="0",
            regime_code="corporate_22",
            instalments="0",
            support_evidence=self.evidence(),
            reconciliation_note="Synthetic reconciliation",
        )
        work.withdraw_obligation(
            organization=self.org,
            actor=self.owner,
            obligation=row,
            reason="Sumber yang sama dicatat dua kali",
            expected_version=row.updated_at.isoformat(),
        )
        self.client.force_login(self.owner)
        detail = self.client.get(reverse("taxes:annual_workpaper", args=[annual.pk]))
        self.assertContains(detail, "Paket lama — perlu versi baru")
        self.assertNotContains(
            detail, 'href="' + reverse("taxes:annual_review", args=[annual.pk]) + '"'
        )
        exported = self.client.get(reverse("taxes:annual_export", args=[annual.pk]))
        with zipfile.ZipFile(io.BytesIO(exported.content)) as package:
            manifest = json.loads(package.read("DRAF-manifest.json"))
            self.assertFalse(manifest["snapshot_current"])
            self.assertFalse(manifest["tax_snapshot_current"])
            self.assertIn("versi baru", package.read("BACA-DAHULU.txt").decode("utf-8"))

    def test_finance_can_withdraw_through_csrf_form_and_history_remains_visible(self):
        row = self.obligation()
        user = get_user_model().objects.create_user(username="synthetic-finance-http")
        Membership.objects.create(organization=self.org, user=user, role="finance")
        client = Client(enforce_csrf_checks=True)
        client.force_login(user)
        url = reverse("taxes:obligation_withdraw", args=[row.pk])
        before = AuditEvent.objects.count()
        response = client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(AuditEvent.objects.count(), before)
        self.assertFalse(TaxObligationWithdrawal.objects.exists())
        payload = {
            "reason": "Dokumen sumber tercatat dua kali",
            "expected_version": row.updated_at.isoformat(),
            "confirmed": "on",
        }
        self.assertEqual(client.post(url, payload).status_code, 403)
        payload["csrfmiddlewaretoken"] = client.cookies["csrftoken"].value
        response = client.post(url, payload)
        self.assertRedirects(response, reverse("taxes:obligation", args=[row.pk]))
        row.refresh_from_db()
        self.assertEqual(row.status, "withdrawn")
        detail = client.get(reverse("taxes:obligation", args=[row.pk]))
        self.assertContains(detail, payload["reason"])
        self.assertNotContains(detail, "Catat laporan &amp; persetujuan")
        self.assertContains(
            client.get(reverse("taxes:index"), {"period": "2025-09"}),
            "Riwayat draf ditarik",
        )
        exported = client.get(
            reverse("taxes:export"), {"period": "2025-09"}
        ).content.decode("utf-8")
        self.assertNotIn("obligation_draft,2025-09", exported)
        self.assertIn("withdrawn_history_excluded", exported)

    def test_wrong_org_read_role_and_revoked_membership_cannot_withdraw(self):
        row = self.obligation()
        url = reverse("taxes:obligation_withdraw", args=[row.pk])
        actor = get_user_model().objects.create_user(
            username="synthetic-restricted-http"
        )
        membership = Membership.objects.create(
            organization=self.org, user=actor, role="auditor"
        )
        self.client.force_login(actor)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(url, {}).status_code, 403)
        membership.role = "finance"
        membership.save()
        other = Organization.objects.create(name="Synthetic foreign tenant")
        session = self.client.session
        session["organization_id"] = other.pk
        session.save()
        Membership.objects.create(organization=other, user=actor, role="finance")
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(url, {}).status_code, 404)
        session = self.client.session
        session["organization_id"] = self.org.pk
        session.save()
        membership.is_active = False
        membership.save()
        self.assertEqual(self.client.post(url, {}).status_code, 403)
        row.refresh_from_db()
        self.assertEqual(row.status, "review")


class PDFUploadHTTPTests(RecoveryFixtures, TestCase):
    def test_tax_http_enforces_csrf_validation_and_private_download_scope(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        url = reverse("taxes:evidence_upload")
        self.assertEqual(client.get(url).status_code, 200)
        self.assertEqual(
            client.post(
                url,
                {"file": SimpleUploadedFile("active.pdf", pdf_bytes(javascript=True))},
            ).status_code,
            403,
        )
        token = client.cookies["csrftoken"].value
        bad = client.post(
            url,
            {
                "csrfmiddlewaretoken": token,
                "file": SimpleUploadedFile("active.pdf", pdf_bytes(javascript=True)),
            },
        )
        self.assertContains(bad, "tanpa sandi, skrip")
        self.assertEqual(TaxEvidence.objects.count(), 0)
        self.assertEqual(
            AuditEvent.objects.filter(action="tax.evidence.uploaded").count(), 0
        )
        content = pdf_bytes()
        self.assertEqual(
            client.post(
                url,
                {
                    "csrfmiddlewaretoken": token,
                    "file": SimpleUploadedFile("normal.pdf", content),
                },
            ).status_code,
            302,
        )
        saved = TaxEvidence.objects.get()
        download_url = reverse("taxes:evidence_download", args=[saved.pk])
        response = client.get(download_url)
        self.assertEqual(response.content, content)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertTrue(response["Content-Disposition"].startswith("attachment;"))
        outsider = get_user_model().objects.create_user(
            username="other-company-download"
        )
        other = Organization.objects.create(name="Synthetic private documents other")
        Membership.objects.create(organization=other, user=outsider, role="owner")
        client.force_login(outsider)
        self.assertEqual(client.get(download_url).status_code, 404)
        client.logout()
        self.assertEqual(client.get(download_url).status_code, 302)
