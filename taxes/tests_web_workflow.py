import io
from datetime import date
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, Client, override_settings
from django.urls import reverse
from pypdf import PdfWriter
from core.models import Membership, Organization
from taxes.models import TaxProfile, TaxObligation, ExternalTaxReview
from taxes.workflow import upload_evidence, prepare_obligation, prepare_annual_workpaper


@override_settings(OPENROUTER_API_KEY="")
class TaxWorkflowHTTPTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="HTTP synthetic tax")
        self.users = {}
        for role in ("owner", "finance", "marketing"):
            user = get_user_model().objects.create_user(username="tax-http-" + role)
            Membership.objects.create(organization=self.org, user=user, role=role)
            self.users[role] = user
        self.client.force_login(self.users["owner"])
        output = io.BytesIO()
        pdf = PdfWriter()
        pdf.add_blank_page(width=100, height=100)
        pdf.write(output)
        self.evidence = upload_evidence(organization=self.org, actor=self.users["owner"], file=SimpleUploadedFile("synthetic.pdf", output.getvalue()))

    def test_real_pages_render_without_creating_profile_or_approval_on_get(self):
        for name in ("index", "annual", "evidence_upload", "profile_review", "obligation_new", "annual_prepare"):
            response = self.client.get(reverse("taxes:" + name))
            self.assertEqual(response.status_code, 200, name)
        self.assertEqual(TaxProfile.objects.count(), 0)
        self.assertEqual(ExternalTaxReview.objects.count(), 0)

    def test_three_roles_separate_preparation_and_external_review_authorization(self):
        self.client.force_login(self.users["finance"])
        self.assertEqual(self.client.get(reverse("taxes:obligation_new")).status_code, 200)
        self.assertEqual(self.client.get(reverse("taxes:profile_review")).status_code, 403)
        self.client.force_login(self.users["marketing"])
        self.assertEqual(self.client.get(reverse("taxes:index")).status_code, 403)
        self.assertEqual(self.client.get(reverse("taxes:evidence_download", args=[self.evidence.pk])).status_code, 403)

    def test_preparation_submission_computes_amount_and_detail_is_reviewable(self):
        self.client.force_login(self.users["finance"])
        response = self.client.post(reverse("taxes:obligation_new"), {"period": "2026-09", "rule_code": "pph23_service_2", "base": "1000025",
            "direction": "receivable", "source_reference": "SYN-CUSTOMER", "rule_reference": "Reviewed synthetic service", "object_code": "24-104-99",
            "evidence": self.evidence.pk, "note": "Synthetic documented classification"})
        self.assertEqual(response.status_code, 302)
        row = TaxObligation.objects.get()
        self.assertEqual(row.amount, Decimal("20001"))
        self.assertEqual(row.status, "review")
        self.assertContains(self.client.get(response.url), "Perhitungan yang dapat ditelusuri")
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.users["owner"])
        self.assertEqual(csrf.post(reverse("taxes:obligation_new"), {}).status_code, 403)

    def test_annual_package_is_private_download_and_explicitly_not_official_schema(self):
        row = prepare_annual_workpaper(organization=self.org, actor=self.users["owner"], year=2026,
            positive_adjustments="0", negative_adjustments="0", loss_compensation="0", turnover="0",
            regime_code="corporate_22", instalments="0", support_evidence=self.evidence, reconciliation_note="Synthetic unfinished fiscal bridge")
        self.assertContains(self.client.get(reverse("taxes:annual_workpaper", args=[row.pk])), "Format resmi/XML DJP belum divalidasi")
        response = self.client.get(reverse("taxes:annual_export", args=[row.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/zip")
        self.assertIn("DRAF", response["Content-Disposition"])

    def test_profile_report_submission_records_scoped_professional_approval(self):
        profile = TaxProfile.objects.create(organization=self.org, registration_date=date(2024, 1, 1), registration_evidence="Synthetic SKT",
            vat_status_effective_from=date(2024, 1, 1), vat_status_evidence="Synthetic non-PKP evidence")
        response = self.client.post(reverse("taxes:profile_review"), {"evidence": self.evidence.pk,
            "professional_name": "Synthetic professional", "qualification_reference": "Synthetic engagement", "reviewed_on": "2026-09-09",
            "statement": "Synthetic profile report", "report_confirmed": "on", "taxpayer_reference": "1234567890123456",
            "regime": "normal", "effective_from": "2026-01-01"})
        self.assertEqual(response.status_code, 302)
        profile.refresh_from_db()
        self.assertEqual(profile.regime, "normal")
        self.assertEqual(profile.external_review.recorded_by, self.users["owner"])
        self.assertEqual(profile.reviewed_by, self.users["owner"])

    def test_model_validation_failure_is_a_form_error_and_rolls_back_report(self):
        row = prepare_obligation(organization=self.org, actor=self.users["owner"], period="2026-09", rule_code="pph23_service_2",
            base="1000000", direction="receivable", source_reference="Synthetic", rule_reference="Synthetic reviewed service",
            object_code="24-104-99", source_evidence=self.evidence)
        row.amount = Decimal("1")
        row.save()
        response = self.client.post(reverse("taxes:obligation_review", args=[row.pk]), {"evidence": self.evidence.pk,
            "professional_name": "Synthetic professional", "qualification_reference": "Synthetic engagement", "reviewed_on": "2026-09-09",
            "statement": "Synthetic review", "report_confirmed": "on"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Nominal/tarif tidak sama")
        self.assertFalse(ExternalTaxReview.objects.exists())
