"""Independent adversarial cases: new tax work cannot infer missing identity."""

import io
from datetime import date, datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase
from pypdf import PdfWriter

from core.models import Membership, Organization
from finance import services as finance
from finance.models import BankAccount, BankTransaction, Journal, Party, Product
from taxes import workflow as work
from taxes.knowledge import publish_catalog
from taxes.models import TaxProfile, TaxSource
from taxes.recipient_identity import (
    approve_recipient_identity,
    assert_pph23_credit_verification,
    identity_decision,
    pph23_object_decision,
    prepare_recipient_identity,
    resolve_recipient_identity,
)
from taxes.rules import calculate_pph23
from taxes.services import SOURCE_CATALOG


class RecipientIdentityIndependentTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(
            name="Synthetic independent recipient company", is_demo=True
        )
        self.owner = get_user_model().objects.create_user(
            username="recipient-independent-owner"
        )
        Membership.objects.create(organization=self.org, user=self.owner, role="owner")
        publish_catalog(
            SOURCE_CATALOG,
            date(2026, 9, 9),
            reviewed_by="Synthetic law reviewer",
            review_reference="Synthetic source publication, no company approval",
        )
        self.supplier = Party.objects.create(
            organization=self.org,
            name="Synthetic independent supplier",
            kind="supplier",
        )
        self.customer = Party.objects.create(
            organization=self.org,
            name="Synthetic independent customer",
            kind="customer",
        )
        self.product = Product.objects.create(
            organization=self.org,
            name="Synthetic ITP",
            code="SYN-ITP",
            kind="itp",
            default_price=Decimal(500000),
        )
        self.account = BankAccount.objects.create(
            organization=self.org,
            name="Synthetic isolated bank",
            account_number="SYN-RPI",
        )
        self.day = date(2026, 9, 10)

    def evidence(self, *, width=100):
        output = io.BytesIO()
        pdf = PdfWriter()
        pdf.add_blank_page(width=width, height=100)
        pdf.write(output)
        return work.upload_evidence(
            organization=self.org,
            actor=self.owner,
            file=SimpleUploadedFile("synthetic-recipient.pdf", output.getvalue()),
        )

    def external(self, subject, decision):
        return work.record_external_review(
            organization=self.org,
            actor=self.owner,
            subject=subject,
            decision=decision,
            evidence=self.evidence(),
            professional_name="Synthetic professional",
            qualification_reference="Synthetic engagement only",
            reviewed_on=date(2026, 10, 9),
            statement="Synthetic received report matched to exact subject",
            report_confirmed=True,
        )

    def prepare_identity(self, party=None, **changes):
        evidence = self.evidence()
        data = dict(
            organization=self.org,
            actor=self.owner,
            recipient_party=party,
            legal_name=party.name if party else self.org.name,
            recipient_type="body",
            tax_residency="spdn",
            population_status="na",
            identity_type="npwp16",
            registration_status="registered",
            number="2234567890123456" if party else "1234567890123456",
            effective_from=date(2025, 5, 22),
            evidence=evidence,
            reference="Synthetic identity document",
            reason="Synthetic documentary identity assessment",
            request_key=f"independent-identity-{evidence.pk}",
        )
        return prepare_recipient_identity(**(data | changes))

    def approve_identity(self, row, treatment="standard_npwp"):
        review = self.external(row, identity_decision(treatment=treatment))
        approve_recipient_identity(
            organization=self.org,
            actor=self.owner,
            identity=row,
            treatment=treatment,
            external_review=review,
            expected_fingerprint=row.fingerprint,
            expected_version=row.revision,
        )
        return row

    def own_identity(self):
        if not hasattr(self, "own"):
            self.own = self.approve_identity(self.prepare_identity())
        return self.own

    def draft(
        self,
        *,
        recipient=None,
        direction="receivable",
        point=None,
        rule="pph23_service_2",
        invoice=None,
        basis="due",
        object_decision=None,
    ):
        own = self.own_identity()
        point = point or self.day
        recipient = recipient or own
        holder = (
            {
                "withholder_name": self.customer.name,
                "withholder_reference": "3234567890123456",
                "withholder_party": self.customer,
            }
            if direction == "receivable"
            else {
                "recipient_party": self.supplier,
                "withholder_name": own.legal_name,
                "withholder_reference": own.number,
            }
        )
        return work.prepare_obligation(
            organization=self.org,
            actor=self.owner,
            period=point.replace(day=1),
            rule_code=rule,
            base="500000",
            direction=direction,
            source_reference="Synthetic independent source",
            rule_reference="Synthetic object review",
            object_code="24-104-99",
            source_evidence=self.evidence(),
            recipient_identity=recipient,
            tax_point_date=point,
            tax_point_basis=basis,
            object_decision=object_decision
            or pph23_object_decision(
                reason="Synthetic professional PPh23 classification",
                final_exempt_excluded=True,
            ),
            source_invoice=invoice,
            **holder,
        )

    def approve(self, row):
        return work.approve_tax_obligation(
            row,
            self.owner,
            external_review=self.external(row, {"action": "approve_obligation"}),
        )

    def invoice(self):
        row = finance.create_invoice(
            organization=self.org,
            actor=self.owner,
            party=self.customer,
            product=self.product,
            quantity=1,
            date=date(2026, 9, 9),
            service_date=self.day,
        )
        return finance.issue_invoice(
            organization=self.org, actor=self.owner, invoice=row
        )

    def bank(self, amount, *, day=None):
        return BankTransaction.objects.create(
            organization=self.org,
            account=self.account,
            date=day or self.day,
            reference=f"SYN-BANK-{BankTransaction.objects.count()}",
            amount=amount,
        )

    def credit(self, row, **changes):
        recipient, holder = (
            row.identity_snapshot["recipient"],
            row.identity_snapshot["withholder"],
        )
        data = dict(
            organization=self.org,
            actor=self.owner,
            obligation=row,
            kind="credit",
            evidence=self.evidence(),
            reference=f"SYN-INDEPENDENT-CERT-{row.pk}",
            taxpayer_reference=recipient["reference"],
            period=row.period,
            amount=row.amount,
            note="Synthetic original customer certificate matched",
            matches_confirmed=True,
            recipient_name=recipient["name"],
            recipient_reference=recipient["reference"],
            withholder_name=holder["name"],
            withholder_reference=holder["reference"],
            object_code=row.object_code,
            base=row.base,
            tax_point_date=row.tax_point_date,
            certificate_status="normal",
        )
        return work.verify_obligation_evidence(**(data | changes))

    def arithmetic(self, row, **changes):
        data = dict(
            rule_code=row.rule_code,
            base="500000",
            period=row.period,
            tax_point_date=row.tax_point_date,
            recipient_identity_decision=row.identity_snapshot["recipient"],
            object_decision=row.object_decision,
            legal_sources=row.legal_sources,
        )
        return calculate_pph23(**(data | changes))

    def test_new_pph23_cannot_use_legacy_identity_free_preparation(self):
        with self.assertRaises(ValidationError):
            work.prepare_obligation(
                organization=self.org,
                actor=self.owner,
                period=date(2026, 9, 1),
                rule_code="pph23_service_2",
                base="500000",
                direction="payable",
                source_reference="Synthetic supplier invoice",
                rule_reference="Synthetic object review",
                object_code="24-104-99",
                source_evidence=self.evidence(),
            )

    def test_blank_or_format_only_npwp_does_not_choose_standard_rate(self):
        row = self.prepare_identity(self.supplier, registration_status="unknown")
        with self.assertRaises(ValidationError):
            self.approve_identity(row)
        blank = self.prepare_identity(
            self.supplier,
            identity_type="unknown",
            number="",
            registration_status="unknown",
        )
        self.approve_identity(blank, treatment="unresolved")
        with self.assertRaises(ValidationError):
            self.draft(recipient=blank, direction="payable")

    def test_dummy_numbers_and_director_nik_cannot_be_company_identity(self):
        for number in ("0000000000000000", "1111111111111111"):
            with self.subTest(number=number), self.assertRaises(ValidationError):
                self.prepare_identity(number=number)
        with self.assertRaises(ValidationError):
            self.prepare_identity(
                identity_type="nik",
                recipient_type="person",
                population_status="resident",
                nik_validated=True,
                nik_integrated=True,
                nik_activated=True,
            )

    def test_documented_no_npwp_and_registered_identity_have_exact_oracles(self):
        for treatment, type_, registration, number, expected in (
            (
                "standard_npwp",
                "npwp16",
                "registered",
                "2234567890123456",
                ("10000", "75000"),
            ),
            ("increased_no_npwp", "none", "no_npwp", "", ("20000", "150000")),
        ):
            self.supplier = Party.objects.create(
                organization=self.org, name=f"Synthetic {treatment}", kind="supplier"
            )
            identity = self.approve_identity(
                self.prepare_identity(
                    self.supplier,
                    identity_type=type_,
                    registration_status=registration,
                    number=number,
                ),
                treatment=treatment,
            )
            for rule, amount in zip(("pph23_service_2", "pph23_other_15"), expected):
                with self.subTest(treatment=treatment, rule=rule):
                    row = self.approve(
                        self.draft(recipient=identity, direction="payable", rule=rule)
                    )
                    self.assertEqual(row.amount, Decimal(amount))
                    self.assertEqual(row.base, Decimal(500000))

    def test_exact_date_cutover_preserves_earlier_approved_rate(self):
        first = self.approve_identity(
            self.prepare_identity(
                self.supplier,
                identity_type="none",
                registration_status="no_npwp",
                number="",
            ),
            treatment="increased_no_npwp",
        )
        earlier = self.approve(
            self.draft(recipient=first, direction="payable", point=date(2026, 9, 14))
        )
        later = self.approve_identity(
            self.prepare_identity(
                self.supplier, previous=first, effective_from=date(2026, 9, 15)
            )
        )
        after = self.approve(
            self.draft(recipient=later, direction="payable", point=date(2026, 9, 15))
        )
        earlier.refresh_from_db()
        self.assertEqual(
            (earlier.amount, after.amount), (Decimal(20000), Decimal(10000))
        )
        self.assertEqual(
            resolve_recipient_identity(
                organization=self.org,
                recipient_party=self.supplier,
                tax_point_date=date(2026, 9, 14),
            ).pk,
            first.pk,
        )

    def test_wrong_recipient_target_cannot_enter_either_direction(self):
        supplier = self.approve_identity(self.prepare_identity(self.supplier))
        with self.assertRaises(ValidationError):
            self.draft(recipient=supplier, direction="receivable")
        with self.assertRaises(ValidationError):
            self.draft(recipient=self.own_identity(), direction="payable")

    def test_unsupported_object_route_is_not_a_pph23_escape(self):
        for route in ("pph21", "pph26", "final", "exempt", "unknown"):
            with self.subTest(route=route), self.assertRaises(ValidationError):
                self.draft(
                    object_decision=pph23_object_decision(
                        route=route,
                        reason="Synthetic different professional classification",
                        final_exempt_excluded=True,
                    )
                )

    def test_datetime_period_and_taxpoint_are_validation_errors(self):
        row = self.draft()
        for change in (
            {"period": datetime(2026, 9, 1)},
            {"tax_point_date": datetime(2026, 9, 10)},
        ):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                self.arithmetic(row, **change)

    def test_factor_and_legal_container_types_fail_without_server_error(self):
        row = self.draft()
        for factor in (1.0, Decimal(1), True):
            facts = row.identity_snapshot["recipient"] | {"factor": factor}
            with self.subTest(factor=repr(factor)), self.assertRaises(ValidationError):
                self.arithmetic(row, recipient_identity_decision=facts)
        for malformed in (None, {"id": []}, {"id": {}}):
            with (
                self.subTest(legal_source=malformed),
                self.assertRaises(ValidationError),
            ):
                self.arithmetic(row, legal_sources=row.legal_sources + [malformed])

    def test_whole_rupiah_boundary_uses_reviewed_two_stage_rounding(self):
        row = self.draft()
        below = self.arithmetic(row, base="500024.49")
        half = self.arithmetic(row, base="500024.50")
        self.assertEqual(
            (below["base"], below["amount"]), (Decimal(500024), Decimal(10000))
        )
        self.assertEqual(
            (half["base"], half["amount"]), (Decimal(500025), Decimal(10001))
        )

    def test_changed_pdf_prevents_new_approval_and_keeps_draft(self):
        row = self.draft()
        report = self.external(row, {"action": "approve_obligation"})
        identity = row.recipient_identity
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE taxes_taxevidence SET content=%s WHERE id=%s",
                [b"%PDF-corrupt", identity.evidence_id],
            )
        with self.assertRaises(ValidationError):
            work.approve_tax_obligation(row, self.owner, external_review=report)
        row.refresh_from_db()
        self.assertEqual((row.status, row.amount), ("review", Decimal(10000)))

    def test_report_original_hash_detects_changed_bytes_and_changed_metadata(self):
        row = self.draft()
        report = self.external(row, {"action": "approve_obligation"})
        replacement = self.evidence(width=121)
        self.assertNotEqual(replacement.sha256, report.evidence.sha256)
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE taxes_taxevidence SET content=%s, sha256=%s WHERE id=%s",
                [bytes(replacement.content), replacement.sha256, report.evidence_id],
            )
        with self.assertRaises(ValidationError):
            work.approve_tax_obligation(row, self.owner, external_review=report)
        row.refresh_from_db()
        self.assertEqual(row.status, "review")

    def test_unpublished_legal_head_edit_prevents_new_action(self):
        row = self.draft()
        TaxSource.objects.filter(slug="pph23-identity-law").update(
            summary="Unpublished changed primary-source summary"
        )
        with self.assertRaises(ValidationError):
            self.approve(row)

    def test_certificate_matches_bound_identity_after_current_profile_changes(self):
        row = self.approve(self.draft())
        TaxProfile.objects.create(
            organization=self.org, taxpayer_reference="9234567890123456"
        )
        for change in (
            {"recipient_reference": "9234567890123456"},
            {"withholder_name": "Wrong company"},
            {"base": "499999"},
            {"tax_point_date": date(2026, 9, 11)},
            {"certificate_status": "cancelled"},
        ):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                self.credit(row, **change)
        proof = self.credit(row, amount="10000", base="500000")
        proof.refresh_from_db()
        row.refresh_from_db()
        self.assertEqual(assert_pph23_credit_verification(proof, row).pk, proof.pk)
        self.assertEqual(proof.taxpayer_reference, "1234567890123456")

    def test_customer_certificate_byte_change_makes_no_cash_or_credit(self):
        invoice = self.invoice()
        row = self.approve(self.draft(invoice=invoice))
        proof = self.credit(row)
        bank = self.bank("490000")
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE taxes_taxevidence SET content=%s WHERE id=%s",
                [b"%PDF-corrupt", proof.evidence_id],
            )
        before = Journal.objects.count()
        with self.assertRaises(ValidationError):
            finance.reconcile_customer_tax_receipt(
                organization=self.org,
                actor=self.owner,
                obligation=row,
                transaction=bank,
                amount="490000",
                reference="SYN-CORRUPT-CERT",
                evidence="Synthetic cash confirmation",
            )
        self.assertEqual(Journal.objects.count(), before)
        self.assertEqual(bank.unallocated_amount, Decimal(490000))
        self.assertEqual(invoice.outstanding_amount, Decimal(500000))

    def test_late_certificate_uses_original_cash_date_for_paid_taxpoint(self):
        invoice = self.invoice()
        row = self.approve(self.draft(invoice=invoice, basis="paid"))
        bank = self.bank("490000", day=date(2026, 9, 11))
        allocation = finance.reconcile_receipt(
            organization=self.org,
            actor=self.owner,
            invoice=invoice,
            transaction=bank,
            amount="490000",
        )
        self.credit(row)
        before = Journal.objects.count()
        with self.assertRaises(ValidationError):
            finance.recognize_customer_tax_credit(
                organization=self.org,
                actor=self.owner,
                obligation=row,
                source_allocation=allocation,
                date=date(2026, 10, 1),
                reference="SYN-LATE-PAID",
                evidence="Synthetic late certificate with different original payment date",
            )
        self.assertEqual(Journal.objects.count(), before)
        self.assertEqual(invoice.outstanding_amount, Decimal(10000))
