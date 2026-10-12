import io
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from pypdf import PdfWriter

from core.models import Membership, Organization
from taxes import workflow as work
from taxes.models import TaxObligation, TaxProfile
from taxes.rules import calculate_annual
from taxes.services import annual_readiness, approve_tax_obligation


@override_settings(OPENROUTER_API_KEY="")
class TaxReleaseWorkflowTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(
            name="Synthetic tax workflow", is_demo=True
        )
        self.owner = get_user_model().objects.create_user(username="tax-release-owner")
        self.reviewer = get_user_model().objects.create_user(
            username="tax-release-reviewer"
        )
        Membership.objects.create(organization=self.org, user=self.owner, role="owner")
        Membership.objects.create(
            organization=self.org, user=self.reviewer, role="reviewer"
        )

    def evidence(self, actor=None):
        output = io.BytesIO()
        pdf = PdfWriter()
        pdf.add_blank_page(width=100, height=100)
        pdf.write(output)
        return work.upload_evidence(
            organization=self.org,
            actor=actor or self.owner,
            file=SimpleUploadedFile("synthetic.pdf", output.getvalue()),
        )

    def external(self, subject, decision):
        return work.record_external_review(
            organization=self.org,
            actor=self.owner,
            subject=subject,
            decision=decision,
            evidence=self.evidence(),
            professional_name="Synthetic professional",
            qualification_reference="Synthetic engagement",
            reviewed_on=date(2026, 9, 9),
            statement="Synthetic report confirms the displayed decision",
            report_confirmed=True,
        )

    def profile(self):
        profile = TaxProfile.objects.create(
            organization=self.org,
            registration_date=date(2024, 1, 1),
            registration_evidence="Synthetic SKT",
            taxpayer_reference="1234567890123456",
            vat_status_evidence="Synthetic non-PKP",
            vat_status_effective_from=date(2024, 1, 1),
        )
        decision = work.profile_decision(
            regime="normal", effective_from=date(2025, 1, 1)
        )
        review = self.external(profile, decision)
        return work.approve_tax_profile(
            profile,
            self.owner,
            regime="normal",
            effective_from=date(2025, 1, 1),
            review_note="Synthetic normal decision",
            external_review=review,
        )

    def prepared(self, direction="payable"):
        return work.prepare_obligation(
            organization=self.org,
            actor=self.owner,
            period="2026-09",
            rule_code="pph23_service_2",
            base=Decimal(1000025),
            direction=direction,
            source_reference="SYN-INV-1",
            rule_reference="Synthetic reviewed service",
            object_code="24-104-99",
            source_evidence=self.evidence(),
            **self.pph23_context(direction=direction, tax_point_date=date(2026, 9, 10)),
        )

    def identity(self, party=None):
        """Synthetic documented ID; this fixture never seeds company records."""
        from taxes.knowledge import publish_catalog
        from taxes.models import RecipientTaxIdentity, TaxSource
        from taxes.recipient_identity import (
            approve_recipient_identity,
            identity_decision,
            prepare_recipient_identity,
        )
        from taxes.services import SOURCE_CATALOG

        if not TaxSource.objects.filter(
            slug="pph23-identity-law", approved=True
        ).exists():
            publish_catalog(
                SOURCE_CATALOG,
                date(2026, 9, 9),
                reviewed_by="Synthetic source reviewer",
                review_reference="Synthetic source publication only",
            )
        old = RecipientTaxIdentity.objects.filter(
            organization=self.org, recipient_party=party, approval__isnull=False
        ).first()
        if old:
            return old
        identity = prepare_recipient_identity(
            organization=self.org,
            actor=self.owner,
            recipient_party=party,
            legal_name=party.name if party else self.org.name,
            recipient_type="body",
            tax_residency="spdn",
            population_status="na",
            identity_type="npwp16",
            number="1234567890123456" if party is None else "2234567890123456",
            registration_status="registered",
            effective_from=date(2025, 5, 22),
            evidence=self.evidence(),
            reference="SYN-IDENTITY",
            reason="Synthetic documented recipient identity",
            request_key=f"synthetic-id-{party.pk if party else 'own'}",
        )
        review = self.external(identity, identity_decision(treatment="standard_npwp"))
        approve_recipient_identity(
            organization=self.org,
            actor=self.owner,
            identity=identity,
            treatment="standard_npwp",
            external_review=review,
            expected_fingerprint=identity.fingerprint,
            expected_version=identity.revision,
        )
        return identity

    def pph23_context(
        self, *, direction, tax_point_date, party=None, invoice=None, bill=None
    ):
        from finance.models import Party
        from taxes.recipient_identity import pph23_object_decision

        own = self.identity()
        if direction == "payable":
            party = (
                bill.supplier
                if bill
                else (
                    party
                    or Party.objects.get_or_create(
                        organization=self.org,
                        name="Synthetic tax supplier",
                        kind="supplier",
                    )[0]
                )
            )
            recipient = self.identity(party)
            holder_name, holder_reference = own.legal_name, own.number
            identities = {"recipient_party": party}
        else:
            party = (
                invoice.party
                if invoice
                else (
                    party
                    or Party.objects.get_or_create(
                        organization=self.org,
                        name="Synthetic tax customer",
                        kind="customer",
                    )[0]
                )
            )
            recipient = own
            holder_name, holder_reference = party.name, "3234567890123456"
            identities = {"withholder_party": party}
        return {
            "recipient_identity": recipient,
            "tax_point_date": tax_point_date,
            "tax_point_basis": "due",
            "withholder_name": holder_name,
            "withholder_reference": holder_reference,
            "object_decision": pph23_object_decision(
                reason="Synthetic professionally classified PPh 23 object",
                final_exempt_excluded=True,
                individual_21_26_excluded=True,
            ),
            **identities,
        }

    def certificate(self, obligation):
        recipient, holder = (
            obligation.identity_snapshot["recipient"],
            obligation.identity_snapshot["withholder"],
        )
        return {
            "recipient_name": recipient["name"],
            "recipient_reference": recipient["reference"],
            "withholder_name": holder["name"],
            "withholder_reference": holder["reference"],
            "object_code": obligation.object_code,
            "base": obligation.base,
            "tax_point_date": obligation.tax_point_date,
            "certificate_status": "normal",
        }

    def test_inconsistent_workpaper_cannot_receive_approval(self):
        row = TaxObligation.objects.create(
            organization=self.org,
            period=date(2026, 9, 1),
            tax_type="pph23",
            direction="payable",
            base=Decimal(1000000),
            rate=Decimal("0.02"),
            amount=Decimal(1),
            source_reference="Synthetic invoice",
            rule_reference="Synthetic rule",
        )
        with self.assertRaises(ValidationError):
            approve_tax_obligation(row, self.reviewer)

    def test_inactive_reviewer_cannot_approve(self):
        self.reviewer.is_active = False
        self.reviewer.save(update_fields=["is_active"])
        row = TaxObligation.objects.create(
            organization=self.org,
            period=date(2026, 9, 1),
            tax_type="pph23",
            direction="payable",
        )
        with self.assertRaises(PermissionDenied):
            approve_tax_obligation(row, self.reviewer)

    def test_owner_can_record_documented_professional_review_not_inherent_approval(
        self,
    ):
        row = self.prepared()
        self.assertEqual(row.amount, Decimal(20001))
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
        with self.assertRaisesMessage(
            ValidationError, "Jenis, arah, dan metode pelaporan"
        ):
            work.approve_tax_obligation(row, self.owner, external_review=review)

    def test_verified_evidence_requires_identity_period_amount_and_remains_immutable(
        self,
    ):
        self.profile()
        row = self.prepared("receivable")
        row = work.approve_tax_obligation(
            row,
            self.owner,
            external_review=self.external(row, {"action": "approve_obligation"}),
        )
        kwargs = dict(
            organization=self.org,
            actor=self.owner,
            obligation=row,
            kind="credit",
            evidence=self.evidence(),
            reference="SYN-CERT-1",
            taxpayer_reference="1234567890123456",
            period="2026-09",
            amount=Decimal(20001),
            note="All document fields matched",
            matches_confirmed=True,
            **self.certificate(row),
        )
        with self.assertRaises(ValidationError):
            work.verify_obligation_evidence(**{**kwargs, "amount": Decimal(1)})
        proof = work.verify_obligation_evidence(**kwargs)
        proof.reference = "CHANGED"
        with self.assertRaises(ValidationError):
            proof.save()
        self.assertIn("Tidak disetor", row.payment_status)

    def test_annual_math_uses_fiscal_bridge_and_does_not_infer_small_turnover_rate(
        self,
    ):
        result = calculate_annual(
            book_profit="500000950",
            positive_adjustments="0",
            negative_adjustments="0",
            loss_compensation="0",
            turnover="4500000000",
            regime_code="corporate_31e_small_11",
            credits="1000000",
            instalments="2000000",
        )
        self.assertEqual(result["taxable_income"], "500000000")
        self.assertEqual(result["income_tax"], "55000000")
        self.assertEqual(result["balance_due"], "52000000")
        with self.assertRaises(ValidationError):
            calculate_annual(
                book_profit="1",
                positive_adjustments="0",
                negative_adjustments="0",
                loss_compensation="0",
                turnover="4800000001",
                regime_code="corporate_31e_small_11",
                credits="0",
                instalments="0",
            )

    def test_whole_year_profile_coverage_preserves_months_without_decision(self):
        self.profile()
        context = annual_readiness(self.org, 2024)
        self.assertEqual(len({gate["period"] for gate in context["gates"]}), 12)
        self.assertEqual(context["checks"][0]["status"], "review")

    def test_new_ordinary_pt_cannot_activate_a_new_final_period(self):
        profile = TaxProfile.objects.create(
            organization=self.org,
            registration_date=date(2026, 5, 1),
            registration_evidence="Synthetic SKT",
            taxpayer_reference="1234567890123456",
            vat_status_evidence="Synthetic non-PKP",
            vat_status_effective_from=date(2026, 5, 1),
        )
        kwargs = dict(
            regime="final",
            effective_from=date(2026, 5, 1),
            final_regime_start_date=date(2026, 1, 1),
            final_regime_end_date=date(2028, 12, 31),
            transition_evidence="Synthetic assertion",
        )
        review = self.external(profile, work.profile_decision(**kwargs))
        with self.assertRaises(ValidationError):
            work.approve_tax_profile(
                profile,
                self.owner,
                review_note="Synthetic",
                external_review=review,
                **kwargs,
            )

    def test_documented_legacy_final_period_uses_validated_payment_reporting(self):
        profile = TaxProfile.objects.create(
            organization=self.org,
            registration_date=date(2024, 1, 1),
            registration_evidence="Synthetic SKT",
            taxpayer_reference="1234567890123456",
            vat_status_evidence="Synthetic non-PKP",
            vat_status_effective_from=date(2024, 1, 1),
        )
        kwargs = dict(
            regime="final",
            effective_from=date(2026, 1, 1),
            final_regime_start_date=date(2024, 1, 1),
            final_regime_end_date=date(2026, 12, 31),
            transition_evidence="Synthetic legacy eligibility reviewed",
            qualifying_turnover=Decimal(1000000000),
            legacy_eligibility_confirmed=True,
            no_normal_election_confirmed=True,
        )
        review = self.external(profile, work.profile_decision(**kwargs))
        work.approve_tax_profile(
            profile,
            self.owner,
            review_note="Synthetic yearly eligibility",
            external_review=review,
            **kwargs,
        )
        row = work.prepare_obligation(
            organization=self.org,
            actor=self.owner,
            period="2026-09",
            rule_code="final_turnover_005",
            base="1000000",
            direction="own_tax",
            source_reference="Synthetic reconciled turnover",
            rule_reference="Synthetic final eligibility report",
            object_code="411128-420",
            source_evidence=self.evidence(),
        )
        row = work.approve_tax_obligation(
            row,
            self.owner,
            external_review=self.external(row, {"action": "approve_obligation"}),
        )
        self.assertEqual(row.amount, Decimal(5000))
        from finance import services as finance
        from finance.models import BankAccount, BankTransaction

        recognized = finance.recognize_own_tax(
            organization=self.org,
            actor=self.owner,
            obligation=row,
            date=date(2026, 9, 10),
            reference="SYN-OWN-TAX",
            evidence="Synthetic reviewed tax expense",
        )
        account = BankAccount.objects.create(
            organization=self.org, name="Synthetic bank", account_number="TAX-SYN"
        )
        movement = BankTransaction.objects.create(
            organization=self.org,
            account=account,
            date=date(2026, 9, 10),
            reference="SYN-TAX-PAY",
            amount="-5000",
        )
        finance.reconcile_own_tax_payment(
            organization=self.org,
            actor=self.owner,
            recognition=recognized,
            transaction=movement,
            amount="5000",
            reference="SYN-TAX-PAY",
            evidence="Synthetic payment matched",
        )
        work.verify_obligation_evidence(
            organization=self.org,
            actor=self.owner,
            obligation=row,
            kind="payment",
            evidence=self.evidence(),
            reference="SYNTHETIC-NTPN",
            taxpayer_reference="1234567890123456",
            period="2026-09",
            amount="5000",
            note="Synthetic valid BPN identity and payment matched",
            matches_confirmed=True,
        )
        self.assertIn("171(4)", row.filing_status)
        self.assertFalse(row.verifications.filter(kind="filing").exists())

    def test_annual_review_rejects_tax_evidence_added_after_preparation(self):
        self.profile()
        row = work.prepare_annual_workpaper(
            organization=self.org,
            actor=self.owner,
            year=2026,
            positive_adjustments="0",
            negative_adjustments="0",
            loss_compensation="0",
            turnover="0",
            regime_code="corporate_22",
            instalments="0",
            support_evidence=self.evidence(),
            reconciliation_note="Synthetic draft",
        )
        review = self.external(row, {"action": "approve_annual"})
        self.prepared("receivable")
        with self.assertRaisesMessage(
            ValidationError, "Bukti pajak atau keputusan profil berubah"
        ):
            work.approve_annual_workpaper(
                organization=self.org,
                actor=self.owner,
                workpaper=row,
                external_review=review,
            )

    def test_completed_year_report_requires_exact_receipt_year_version_and_amounts(
        self,
    ):
        from django.utils import timezone

        from finance.models import AccountingPeriod

        self.profile()
        # Persisted synthetic close states isolate the tax-year contract; finance tests exercise closing.
        for month in range(1, 13):
            AccountingPeriod.objects.create(
                organization=self.org,
                year=2025,
                month=month,
                closed=True,
                closed_at=timezone.now(),
            )
        row = work.prepare_annual_workpaper(
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
            reconciliation_note="Synthetic reviewed inactive year",
        )
        work.approve_annual_workpaper(
            organization=self.org,
            actor=self.owner,
            workpaper=row,
            external_review=self.external(row, {"action": "approve_annual"}),
        )
        kwargs = dict(
            organization=self.org,
            actor=self.owner,
            workpaper=row,
            evidence=self.evidence(),
            reference="SYNTHETIC-BPE",
            taxpayer_reference="1234567890123456",
            year=2025,
            return_version=0,
            reported_tax="0",
            reported_balance_due="0",
            matches_confirmed=True,
        )
        for changes in (
            {"year": 2026},
            {"return_version": 1},
            {"reported_tax": "1"},
            {"reported_balance_due": "1"},
        ):
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
        self.assertEqual(
            self.client.get(
                reverse("taxes:evidence_download", args=[evidence.pk])
            ).status_code,
            404,
        )
        with self.assertRaises(PermissionDenied):
            work.upload_evidence(
                organization=self.org,
                actor=outsider,
                file=SimpleUploadedFile("bad.pdf", b"bad"),
            )
