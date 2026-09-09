from datetime import date
from decimal import Decimal
import io
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from pypdf import PdfWriter

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings

from core.models import Membership, Organization
from taxes.models import TaxObligation, TaxProfile
from taxes.services import approve_tax_obligation, annual_readiness
from taxes import workflow as work
from taxes.rules import calculate_tax, calculate_annual


@override_settings(OPENROUTER_API_KEY="")
class TaxReleaseWorkflowTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Synthetic tax workflow", is_demo=True)
        self.owner = get_user_model().objects.create_user(username="tax-release-owner")
        self.reviewer = get_user_model().objects.create_user(username="tax-release-reviewer")
        Membership.objects.create(organization=self.org, user=self.owner, role="owner")
        Membership.objects.create(organization=self.org, user=self.reviewer, role="reviewer")

    def evidence(self, actor=None):
        output = io.BytesIO()
        pdf = PdfWriter()
        pdf.add_blank_page(width=100, height=100)
        pdf.write(output)
        return work.upload_evidence(organization=self.org, actor=actor or self.owner, file=SimpleUploadedFile("synthetic.pdf", output.getvalue()))

    def external(self, subject, decision):
        return work.record_external_review(organization=self.org, actor=self.owner, subject=subject, decision=decision,
            evidence=self.evidence(), professional_name="Synthetic professional", qualification_reference="Synthetic engagement",
            reviewed_on=date(2026, 9, 9), statement="Synthetic report confirms the displayed decision", report_confirmed=True)

    def profile(self):
        profile = TaxProfile.objects.create(organization=self.org, registration_date=date(2024, 1, 1), registration_evidence="Synthetic SKT",
            taxpayer_reference="1234567890123456", vat_status_evidence="Synthetic non-PKP", vat_status_effective_from=date(2024, 1, 1))
        decision = work.profile_decision(regime="normal", effective_from=date(2025, 1, 1))
        review = self.external(profile, decision)
        return work.approve_tax_profile(profile, self.owner, regime="normal", effective_from=date(2025, 1, 1), review_note="Synthetic normal decision", external_review=review)

    def prepared(self, direction="payable"):
        return work.prepare_obligation(organization=self.org, actor=self.owner, period="2026-09", rule_code="pph23_service_2",
            base=Decimal("1000025"), direction=direction, source_reference="SYN-INV-1", rule_reference="Synthetic reviewed service",
            object_code="24-104-99", source_evidence=self.evidence())

    def test_inconsistent_workpaper_cannot_receive_approval(self):
        row = TaxObligation.objects.create(organization=self.org, period=date(2026, 9, 1),
            tax_type="pph23", direction="payable", base=Decimal("1000000"), rate=Decimal("0.02"),
            amount=Decimal("1"), source_reference="Synthetic invoice", rule_reference="Synthetic rule")
        with self.assertRaises(ValidationError):
            approve_tax_obligation(row, self.reviewer)

    def test_inactive_reviewer_cannot_approve(self):
        self.reviewer.is_active = False
        self.reviewer.save(update_fields=["is_active"])
        row = TaxObligation.objects.create(organization=self.org, period=date(2026, 9, 1), tax_type="pph23", direction="payable")
        with self.assertRaises(PermissionDenied):
            approve_tax_obligation(row, self.reviewer)

    def test_owner_can_record_documented_professional_review_not_inherent_approval(self):
        row = self.prepared()
        self.assertEqual(row.amount, Decimal("20001"))
        with self.assertRaises(ValidationError):
            work.approve_tax_obligation(row, self.owner)
        review = self.external(row, {"action": "approve_obligation"})
        row = work.approve_tax_obligation(row, self.owner, external_review=review)
        self.assertEqual(row.status, "approved")
        row.amount = 1
        with self.assertRaises(ValidationError):
            row.save()

    def test_foreign_and_changed_subject_review_cannot_be_reused(self):
        row = self.prepared()
        review = self.external(row, {"action": "approve_obligation"})
        row.source_reference = "CHANGED"
        row.save()
        with self.assertRaises(ValidationError):
            work.approve_tax_obligation(row, self.owner, external_review=review)

    def test_review_cannot_approve_a_draft_with_an_inconsistent_tax_type(self):
        row = self.prepared()
        row.tax_type = "corporate_income"
        row.save()
        review = self.external(row, {"action": "approve_obligation"})
        with self.assertRaisesMessage(ValidationError, "Jenis, arah, dan metode pelaporan"):
            work.approve_tax_obligation(row, self.owner, external_review=review)

    def test_verified_evidence_requires_identity_period_amount_and_remains_immutable(self):
        self.profile()
        row = self.prepared("receivable")
        row = work.approve_tax_obligation(row, self.owner, external_review=self.external(row, {"action": "approve_obligation"}))
        kwargs = dict(organization=self.org, actor=self.owner, obligation=row, kind="credit", evidence=self.evidence(), reference="SYN-CERT-1",
            taxpayer_reference="1234567890123456", period="2026-09", amount=Decimal("20001"), note="All document fields matched", matches_confirmed=True)
        with self.assertRaises(ValidationError):
            work.verify_obligation_evidence(**{**kwargs, "amount": Decimal("1")})
        proof = work.verify_obligation_evidence(**kwargs)
        proof.reference = "CHANGED"
        with self.assertRaises(ValidationError):
            proof.save()
        self.assertIn("Tidak disetor", row.payment_status)

    def test_annual_math_uses_fiscal_bridge_and_does_not_infer_small_turnover_rate(self):
        result = calculate_annual(book_profit="500000950", positive_adjustments="0", negative_adjustments="0",
            loss_compensation="0", turnover="4500000000", regime_code="corporate_31e_small_11", credits="1000000", instalments="2000000")
        self.assertEqual(result["taxable_income"], "500000000")
        self.assertEqual(result["income_tax"], "55000000")
        self.assertEqual(result["balance_due"], "52000000")
        with self.assertRaises(ValidationError):
            calculate_annual(book_profit="1", positive_adjustments="0", negative_adjustments="0", loss_compensation="0",
                turnover="4800000001", regime_code="corporate_31e_small_11", credits="0", instalments="0")

    def test_whole_year_profile_coverage_preserves_months_without_decision(self):
        self.profile()
        context = annual_readiness(self.org, 2024)
        self.assertEqual(len({gate["period"] for gate in context["gates"]}), 12)
        self.assertEqual(context["checks"][0]["status"], "review")

    def test_new_ordinary_pt_cannot_activate_a_new_final_period(self):
        profile = TaxProfile.objects.create(organization=self.org, registration_date=date(2026, 5, 1), registration_evidence="Synthetic SKT",
            taxpayer_reference="1234567890123456", vat_status_evidence="Synthetic non-PKP", vat_status_effective_from=date(2026, 5, 1))
        kwargs = dict(regime="final", effective_from=date(2026, 5, 1), final_regime_start_date=date(2026, 1, 1), final_regime_end_date=date(2028, 12, 31), transition_evidence="Synthetic assertion")
        review = self.external(profile, work.profile_decision(**kwargs))
        with self.assertRaises(ValidationError):
            work.approve_tax_profile(profile, self.owner, review_note="Synthetic", external_review=review, **kwargs)

    def test_documented_legacy_final_period_uses_validated_payment_reporting(self):
        profile = TaxProfile.objects.create(organization=self.org, registration_date=date(2024, 1, 1), registration_evidence="Synthetic SKT",
            taxpayer_reference="1234567890123456", vat_status_evidence="Synthetic non-PKP", vat_status_effective_from=date(2024, 1, 1))
        kwargs = dict(regime="final", effective_from=date(2026, 1, 1), final_regime_start_date=date(2024, 1, 1),
            final_regime_end_date=date(2026, 12, 31), transition_evidence="Synthetic legacy eligibility reviewed",
            qualifying_turnover=Decimal("1000000000"), legacy_eligibility_confirmed=True, no_normal_election_confirmed=True)
        review = self.external(profile, work.profile_decision(**kwargs))
        work.approve_tax_profile(profile, self.owner, review_note="Synthetic yearly eligibility", external_review=review, **kwargs)
        row = work.prepare_obligation(organization=self.org, actor=self.owner, period="2026-09", rule_code="final_turnover_005",
            base="1000000", direction="own_tax", source_reference="Synthetic reconciled turnover", rule_reference="Synthetic final eligibility report",
            object_code="411128-420", source_evidence=self.evidence())
        row = work.approve_tax_obligation(row, self.owner, external_review=self.external(row, {"action": "approve_obligation"}))
        self.assertEqual(row.amount, Decimal("5000"))
        work.verify_obligation_evidence(organization=self.org, actor=self.owner, obligation=row, kind="payment", evidence=self.evidence(),
            reference="SYNTHETIC-NTPN", taxpayer_reference="1234567890123456", period="2026-09", amount="5000",
            note="Synthetic valid BPN identity and payment matched", matches_confirmed=True)
        self.assertIn("171(4)", row.filing_status)
        self.assertFalse(row.verifications.filter(kind="filing").exists())

    def test_annual_review_rejects_tax_evidence_added_after_preparation(self):
        self.profile()
        row = work.prepare_annual_workpaper(organization=self.org, actor=self.owner, year=2026, positive_adjustments="0", negative_adjustments="0",
            loss_compensation="0", turnover="0", regime_code="corporate_22", instalments="0", support_evidence=self.evidence(), reconciliation_note="Synthetic draft")
        review = self.external(row, {"action": "approve_annual"})
        self.prepared("receivable")
        with self.assertRaisesMessage(ValidationError, "Bukti pajak atau keputusan profil berubah"):
            work.approve_annual_workpaper(organization=self.org, actor=self.owner, workpaper=row, external_review=review)

    def test_completed_year_report_requires_exact_receipt_year_version_and_amounts(self):
        from finance.models import AccountingPeriod
        from django.utils import timezone
        self.profile()
        # Persisted synthetic close states isolate the tax-year contract; finance tests exercise closing.
        for month in range(1, 13):
            AccountingPeriod.objects.create(organization=self.org, year=2025, month=month, closed=True, closed_at=timezone.now())
        row = work.prepare_annual_workpaper(organization=self.org, actor=self.owner, year=2025, positive_adjustments="0", negative_adjustments="0",
            loss_compensation="0", turnover="0", regime_code="corporate_22", instalments="0", support_evidence=self.evidence(), reconciliation_note="Synthetic reviewed inactive year")
        work.approve_annual_workpaper(organization=self.org, actor=self.owner, workpaper=row,
            external_review=self.external(row, {"action": "approve_annual"}))
        kwargs = dict(organization=self.org, actor=self.owner, workpaper=row, evidence=self.evidence(), reference="SYNTHETIC-BPE",
            taxpayer_reference="1234567890123456", year=2025, return_version=0, reported_tax="0", reported_balance_due="0", matches_confirmed=True)
        for changes in ({"year": 2026}, {"return_version": 1}, {"reported_tax": "1"}, {"reported_balance_due": "1"}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                work.verify_annual_filing(**{**kwargs, **changes})
        proof = work.verify_annual_filing(**kwargs)
        self.assertEqual(proof.workpaper, row)
        with self.assertRaises(ValidationError):
            proof.save()

    def test_private_evidence_cross_org_and_role_writes_are_denied(self):
        evidence = self.evidence()
        outsider = get_user_model().objects.create_user(username="outside-tax")
        other = Organization.objects.create(name="Other synthetic")
        Membership.objects.create(organization=other, user=outsider, role="owner")
        self.client.force_login(outsider)
        self.assertEqual(self.client.get(reverse("taxes:evidence_download", args=[evidence.pk])).status_code, 404)
        with self.assertRaises(PermissionDenied):
            work.upload_evidence(organization=self.org, actor=outsider, file=SimpleUploadedFile("bad.pdf", b"bad"))
