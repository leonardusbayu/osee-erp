"""Synthetic public-service contracts for documented recipient identity."""

from datetime import date, datetime
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.test import TestCase

from taxes.tests_workflow import TaxReleaseWorkflowTests


class RecipientIdentityTests(TestCase):
    setUp = TaxReleaseWorkflowTests.setUp
    evidence = TaxReleaseWorkflowTests.evidence
    external = TaxReleaseWorkflowTests.external
    identity = TaxReleaseWorkflowTests.identity
    pph23_context = TaxReleaseWorkflowTests.pph23_context
    certificate = TaxReleaseWorkflowTests.certificate

    def proposed(self, **changes):
        from taxes.recipient_identity import prepare_recipient_identity

        self.identity()  # publish reviewed synthetic source versions once
        values = dict(
            organization=self.org,
            actor=self.owner,
            recipient_party=self.party(),
            legal_name="Synthetic supplier",
            recipient_type="body",
            tax_residency="spdn",
            population_status="na",
            identity_type="npwp16",
            number="4234567890123456",
            registration_status="registered",
            effective_from=date(2025, 5, 22),
            evidence=self.evidence(),
            reference="SYN-RECIPIENT",
            reason="Synthetic documented identity facts",
            request_key="proposed-one",
        )
        values.update(changes)
        return prepare_recipient_identity(**values)

    def party(self):
        from finance.models import Party

        return Party.objects.get_or_create(
            organization=self.org, name="Synthetic supplier", kind="supplier"
        )[0]

    def approval(self, identity, treatment="standard_npwp", **changes):
        from taxes.recipient_identity import (
            approve_recipient_identity,
            identity_decision,
        )

        values = dict(
            organization=self.org,
            actor=self.owner,
            identity=identity,
            treatment=treatment,
            external_review=self.external(
                identity, identity_decision(treatment=treatment)
            ),
            expected_fingerprint=identity.fingerprint,
            expected_version=identity.revision,
        )
        values.update(changes)
        return approve_recipient_identity(**values)

    def payable(self, identity, **changes):
        from taxes.workflow import prepare_obligation

        context = self.pph23_context(
            direction="payable",
            party=identity.recipient_party,
            tax_point_date=date(2026, 9, 10),
        )
        context.update(recipient_identity=identity)
        context.update(changes)
        return prepare_obligation(
            organization=self.org,
            actor=self.owner,
            period="2026-09",
            rule_code="pph23_service_2",
            base="500000",
            direction="payable",
            source_reference="SYN-OBJECT",
            rule_reference="Synthetic reviewed object",
            object_code="24-104-99",
            source_evidence=self.evidence(),
            **context,
        )

    def test_registered_supplier_uses_professionally_bound_identity(self):
        from finance.models import Party
        from taxes.knowledge import publish_catalog
        from taxes.recipient_identity import (
            approve_recipient_identity,
            identity_decision,
            identity_status,
            prepare_recipient_identity,
            resolve_recipient_identity,
        )
        from taxes.services import SOURCE_CATALOG

        publish_catalog(
            SOURCE_CATALOG,
            date(2026, 9, 9),
            reviewed_by="Synthetic reviewer",
            review_reference="Synthetic only source publication",
        )
        party = Party.objects.create(
            organization=self.org, name="Synthetic supplier", kind="supplier"
        )
        identity = prepare_recipient_identity(
            organization=self.org,
            actor=self.owner,
            recipient_party=party,
            legal_name=party.name,
            recipient_type="body",
            tax_residency="spdn",
            population_status="na",
            identity_type="npwp16",
            number="1234567890123456",
            registration_status="registered",
            effective_from=date(2025, 5, 22),
            evidence=self.evidence(),
            reference="SYN-IDENTITY-1",
            reason="Synthetic documented identity",
            request_key="identity-one",
        )
        state = identity_status(organization=self.org, identity=identity)
        review = self.external(identity, identity_decision(treatment="standard_npwp"))
        approve_recipient_identity(
            organization=self.org,
            actor=self.owner,
            identity=identity,
            treatment="standard_npwp",
            external_review=review,
            expected_fingerprint=state["fingerprint"],
            expected_version=state["version"],
        )
        selected = resolve_recipient_identity(
            organization=self.org,
            recipient_party=party,
            tax_point_date=date(2026, 9, 10),
        )
        self.assertEqual(selected.pk, identity.pk)
        self.assertEqual(
            identity_status(organization=self.org, identity=selected)["factor"], 1
        )

    def test_documented_no_npwp_uses_factor_two_for_both_supported_objects(self):
        from taxes.rules import calculate_pph23

        identity = self.proposed(
            identity_type="none", number="", registration_status="no_npwp"
        )
        self.approval(identity, "increased_no_npwp")
        row = self.payable(identity)
        self.assertEqual(row.rate, Decimal("0.04"))
        self.assertEqual(row.amount, Decimal(20000))
        result = calculate_pph23(
            rule_code="pph23_other_15",
            base="500000",
            period=row.period,
            tax_point_date=row.tax_point_date,
            recipient_identity_decision=row.identity_snapshot["recipient"],
            object_decision=row.object_decision,
            legal_sources=row.legal_sources,
        )
        self.assertEqual(result["amount"], Decimal(150000))

    def test_integrated_nik_without_activation_is_distinct_from_validated_only(self):
        identity = self.proposed(
            recipient_type="person",
            population_status="resident",
            identity_type="nik",
            registration_status="no_npwp",
            nik_validated=True,
            nik_integrated=True,
            nik_activated=False,
        )
        approval = self.approval(identity, "standard_integrated_nik")
        self.assertEqual(approval.factor, 1)
        self.assertEqual(self.payable(identity).amount, Decimal(10000))
        next_identity = self.proposed(
            recipient_type="person",
            population_status="resident",
            identity_type="nik",
            registration_status="no_npwp",
            nik_validated=True,
            nik_integrated=False,
            effective_from=date(2026, 9, 20),
            previous=identity,
            request_key="next-nik",
        )
        with self.assertRaises(ValidationError):
            self.approval(next_identity, "standard_integrated_nik")

    def test_unknown_blank_and_formatted_number_do_not_choose_a_rate(self):
        identity = self.proposed(
            identity_type="unknown", number="", registration_status="unknown"
        )
        with self.assertRaises(ValidationError):
            self.approval(identity)
        self.approval(identity, "unresolved")
        with self.assertRaises(ValidationError):
            self.payable(identity)

    def test_person_service_requires_explicit_article21_26_exclusion(self):
        from taxes.recipient_identity import pph23_object_decision

        identity = self.proposed(
            recipient_type="person",
            population_status="resident",
            identity_type="nik",
            registration_status="registered",
            nik_validated=True,
            nik_integrated=True,
            nik_activated=True,
        )
        self.approval(identity)
        with self.assertRaisesMessage(ValidationError, "PPh 21/26"):
            self.payable(
                identity,
                object_decision=pph23_object_decision(
                    reason="Synthetic service requires individual routing review",
                    final_exempt_excluded=True,
                ),
            )
        with self.assertRaises(ValidationError):
            self.payable(
                identity,
                object_decision=pph23_object_decision(
                    route="pph26",
                    reason="Synthetic foreign income routing",
                    final_exempt_excluded=True,
                ),
            )

    def test_replacement_cutover_preserves_old_identity_and_date_exactness(self):
        from taxes.recipient_identity import resolve_recipient_identity

        identity = self.proposed()
        self.approval(identity)
        next_identity = self.proposed(
            previous=identity,
            effective_from=date(2026, 9, 20),
            request_key="successor",
            number="5234567890123456",
        )
        self.approval(next_identity)
        self.assertEqual(
            resolve_recipient_identity(
                organization=self.org,
                recipient_party=identity.recipient_party,
                tax_point_date=date(2026, 9, 19),
            ).pk,
            identity.pk,
        )
        self.assertEqual(
            resolve_recipient_identity(
                organization=self.org,
                recipient_party=identity.recipient_party,
                tax_point_date=date(2026, 9, 20),
            ).pk,
            next_identity.pk,
        )
        identity.refresh_from_db()
        self.assertIsNone(identity.effective_until)
        with self.assertRaises(ValidationError):
            resolve_recipient_identity(
                organization=self.org,
                recipient_party=identity.recipient_party,
                tax_point_date=datetime(2026, 9, 20, 12),
            )

    def test_format_dummy_and_own_company_director_nik_are_rejected(self):
        for number in ("0000000000000000", "1111111111111111", "123"):
            with self.subTest(number=number), self.assertRaises(ValidationError):
                self.proposed(number=number)
        with self.assertRaises(ValidationError):
            self.proposed(
                recipient_party=None,
                legal_name=self.org.name,
                recipient_type="person",
                population_status="resident",
                identity_type="nik",
            )

    def test_overlap_stale_fingerprint_and_wrong_role_are_rejected(self):
        identity = self.proposed()
        with self.assertRaises(ValidationError):
            self.approval(identity, expected_fingerprint="0" * 64)
        with self.assertRaises(PermissionDenied):
            self.approval(identity, actor=self.reviewer)
        self.approval(identity)
        conflicting = self.proposed(request_key="overlap")
        with self.assertRaises(ValidationError):
            self.approval(conflicting)
        with self.assertRaises(ValidationError):
            self.proposed(effective_from=datetime(2026, 9, 10, 0))

    def test_actual_document_tamper_blocks_resolution_and_new_obligation(self):
        from taxes.recipient_identity import resolve_recipient_identity

        identity = self.proposed()
        self.approval(identity)
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE taxes_taxevidence SET content=%s WHERE id=%s",
                [b"tampered synthetic bytes", identity.evidence_id],
            )
        with self.assertRaisesMessage(ValidationError, "dokumen"):
            resolve_recipient_identity(
                organization=self.org,
                recipient_party=identity.recipient_party,
                tax_point_date=date(2026, 9, 10),
            )

    def test_name_change_requires_new_dated_identity_but_old_approval_stays_immutable(
        self,
    ):
        from taxes.recipient_identity import resolve_recipient_identity

        identity = self.proposed()
        old = self.approval(identity)
        party = identity.recipient_party
        party.name = "Synthetic renamed supplier"
        party.save()
        with self.assertRaises(ValidationError):
            resolve_recipient_identity(
                organization=self.org,
                recipient_party=party,
                tax_point_date=date(2026, 9, 10),
            )
        new = self.proposed(
            previous=identity,
            legal_name=party.name,
            recipient_party=party,
            request_key="renamed-supplier",
            effective_from=date(2026, 9, 20),
        )
        self.approval(new)
        self.assertEqual(
            resolve_recipient_identity(
                organization=self.org,
                recipient_party=party,
                tax_point_date=date(2026, 9, 20),
            ).pk,
            new.pk,
        )
        old.refresh_from_db()
        self.assertEqual(old.snapshot["identity"]["legal_name"], "Synthetic supplier")

    def test_customer_certificate_uses_bound_pt_identity_not_latest_profile(self):
        from taxes import workflow as work
        from taxes.recipient_identity import assert_pph23_credit_verification

        row = work.prepare_obligation(
            organization=self.org,
            actor=self.owner,
            period="2026-09",
            rule_code="pph23_service_2",
            base="500000",
            direction="receivable",
            source_reference="SYN-CUSTOMER",
            rule_reference="Synthetic reviewed service",
            object_code="24-104-99",
            source_evidence=self.evidence(),
            **self.pph23_context(
                direction="receivable", tax_point_date=date(2026, 9, 10)
            ),
        )
        row = work.approve_tax_obligation(
            row,
            self.owner,
            external_review=self.external(row, {"action": "approve_obligation"}),
        )
        self.assertEqual(row.filing_status, "Bukti potong pelanggan belum dicocokkan")
        kwargs = dict(
            organization=self.org,
            actor=self.owner,
            obligation=row,
            kind="credit",
            evidence=self.evidence(),
            reference="SYN-EXACT-CERT",
            taxpayer_reference="1234567890123456",
            period=row.period,
            amount="10000",
            note="Synthetic all certificate fields matched",
            matches_confirmed=True,
            **{**self.certificate(row), "base": "500000"},
        )
        with self.assertRaises(ValidationError):
            work.verify_obligation_evidence(
                **{**kwargs, "recipient_reference": kwargs["withholder_reference"]}
            )
        proof = work.verify_obligation_evidence(**kwargs)
        proof.refresh_from_db()
        row.refresh_from_db()
        self.assertEqual(proof.certificate_snapshot["amount"], "10000.00")
        self.assertEqual(proof.certificate_snapshot["base"], "500000.00")
        self.assertEqual(assert_pph23_credit_verification(proof, row).pk, proof.pk)
        self.assertEqual(
            row.filing_status, "Bukti potong pelanggan tercatat — SPT Tahunan terpisah"
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE taxes_taxevidence SET content=%s WHERE id=%s",
                [b"tampered certificate", proof.evidence_id],
            )
        with self.assertRaises(ValidationError):
            assert_pph23_credit_verification(proof, row)
        # This label records receipt of a document; the integrity guard still blocks use.
        self.assertEqual(
            row.filing_status, "Bukti potong pelanggan tercatat — SPT Tahunan terpisah"
        )

    def test_customer_filing_status_withdrawal_and_other_directions(self):
        from taxes import workflow as work
        from taxes.models import TaxObligation

        row = work.prepare_obligation(
            organization=self.org,
            actor=self.owner,
            period="2026-09",
            rule_code="pph23_service_2",
            base="500000",
            direction="receivable",
            source_reference="SYN-CUSTOMER-WITHDRAW",
            rule_reference="Synthetic reviewed service",
            object_code="24-104-99",
            source_evidence=self.evidence(),
            **self.pph23_context(
                direction="receivable", tax_point_date=date(2026, 9, 10)
            ),
        )
        self.assertEqual(row.filing_status, "Bukti potong pelanggan belum dicocokkan")
        work.withdraw_obligation(
            organization=self.org,
            actor=self.owner,
            obligation=row,
            reason="Synthetic duplicate customer tax workpaper",
            expected_version=row.updated_at.isoformat(),
        )
        row.refresh_from_db()
        self.assertEqual(row.filing_status, "Ditarik — tidak termasuk kewajiban aktif")
        for tax_type, direction in (
            ("pph23", "payable"),
            ("final_turnover", "own_tax"),
        ):
            with self.subTest(tax_type=tax_type, direction=direction):
                self.assertEqual(
                    TaxObligation(tax_type=tax_type, direction=direction).filing_status,
                    "Pelaporan belum diverifikasi",
                )

    def test_review_date_only_legal_renewal_preserves_binding_but_content_change_blocks(
        self,
    ):
        from taxes import workflow as work
        from taxes.knowledge import publish_catalog
        from taxes.models import TaxSource
        from taxes.recipient_identity import assert_pph23_obligation

        identity = self.proposed()
        self.approval(identity)
        row = self.payable(identity)
        row = work.approve_tax_obligation(
            row,
            self.owner,
            external_review=self.external(row, {"action": "approve_obligation"}),
        )
        original = row.legal_sources
        source = TaxSource.objects.get(slug="per11-2025")
        item = {
            key: getattr(source, key)
            for key in (
                "slug",
                "title",
                "url",
                "issuer",
                "article",
                "summary",
                "topics",
                "effective_from",
                "effective_until",
            )
        }
        publish_catalog(
            [item],
            date(2026, 10, 9),
            reviewed_by="Synthetic renewal reviewer",
            review_reference="Synthetic unchanged law renewal",
        )
        self.assertEqual(assert_pph23_obligation(row)["amount"], Decimal(10000))
        row.refresh_from_db()
        self.assertEqual(row.legal_sources, original)
        item["summary"] += " Synthetic materially changed normative content."
        publish_catalog(
            [item],
            date(2026, 10, 9),
            reviewed_by="Synthetic amendment reviewer",
            review_reference="Synthetic changed law review",
        )
        with self.assertRaises(ValidationError):
            assert_pph23_obligation(row)

    def test_same_normative_renewal_does_not_expire_a_dated_professional_report(self):
        from taxes import workflow as work
        from taxes.recipient_identity import (
            approve_recipient_identity,
            identity_decision,
        )

        identity = self.proposed()
        review = work.record_external_review(
            organization=self.org,
            actor=self.owner,
            subject=identity,
            decision=identity_decision(treatment="standard_npwp"),
            evidence=self.evidence(),
            professional_name="Synthetic professional",
            qualification_reference="Synthetic received engagement",
            reviewed_on=date(2025, 9, 9),
            statement="Synthetic documented dated identity facts",
            report_confirmed=True,
        )
        approved = approve_recipient_identity(
            organization=self.org,
            actor=self.owner,
            identity=identity,
            treatment="standard_npwp",
            external_review=review,
            expected_fingerprint=identity.fingerprint,
            expected_version=identity.revision,
        )
        self.assertEqual(approved.factor, 1)

    def test_unresolved_identity_can_be_resolved_at_actual_same_effective_date(self):
        from taxes.recipient_identity import resolve_recipient_identity

        original = self.proposed(
            identity_type="unknown", number="", registration_status="unknown"
        )
        approval = self.approval(original, "unresolved")
        successor = self.proposed(
            previous=original,
            request_key="resolved-same-actual-date",
            reference="SYN-PROFESSIONAL-IDENTITY-CONCLUSION",
            reason="Synthetic professional now confirms documented actual registration",
        )
        resolved = self.approval(successor)
        self.assertEqual(resolved.factor, 1)
        self.assertEqual(
            resolve_recipient_identity(
                organization=self.org,
                recipient_party=original.recipient_party,
                tax_point_date=original.effective_from,
            ).pk,
            successor.pk,
        )
        approval.refresh_from_db()
        self.assertIsNone(approval.factor)
        self.assertEqual(
            approval.snapshot["identity"]["registration_status"], "unknown"
        )
        with self.assertRaises(ValidationError):
            self.proposed(
                previous=successor, request_key="resolved-retroactive-correction"
            )

    def test_erroneous_unapproved_draft_can_be_replaced_with_new_request_same_date(
        self,
    ):
        from taxes.recipient_identity import identity_status

        wrong = self.proposed(number="4234567890123456")
        corrected = self.proposed(
            number="5234567890123456", request_key="corrected-parallel-request"
        )
        self.approval(corrected)
        state = identity_status(organization=self.org, identity=wrong)
        self.assertEqual(state["status"], "superseded")
        self.assertFalse(state["can_approve"])
        with self.assertRaises(ValidationError):
            self.approval(wrong)
        wrong.refresh_from_db()
        self.assertEqual(wrong.number, "4234567890123456")

    def test_direct_draft_foreign_identity_and_party_bindings_are_rejected(self):
        from core.models import Organization
        from finance.models import Party
        from taxes.models import TaxObligation

        foreign = Organization.objects.create(name="Synthetic foreign recipient scope")
        party = Party.objects.create(
            organization=foreign, name="Synthetic foreign party", kind="supplier"
        )
        for field in ("recipient_party", "withholder_party"):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                TaxObligation.objects.create(
                    organization=self.org,
                    period=date(2026, 9, 1),
                    tax_type="pph23",
                    direction="payable",
                    **{field: party},
                )
        from core.models import Membership

        Membership.objects.create(organization=foreign, user=self.owner, role="owner")
        original_org = self.org
        try:
            self.org = foreign
            foreign_identity = self.identity()
        finally:
            self.org = original_org
        for field in ("recipient_identity", "withholder_identity"):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                TaxObligation.objects.create(
                    organization=self.org,
                    period=date(2026, 9, 1),
                    tax_type="pph23",
                    direction="payable",
                    **{field: foreign_identity},
                )
