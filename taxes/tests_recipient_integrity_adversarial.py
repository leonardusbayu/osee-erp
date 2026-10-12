"""Independent synthetic public-service probes for bound PPh 23 evidence.

Raw SQL only simulates damaged persisted records in the isolated test database;
all original identities, decisions and certificates use the public services.
"""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from threading import Barrier
from unittest import skipUnless

from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection, connections
from django.test import TestCase, TransactionTestCase

from finance import services as finance
from finance.models import Party
from taxes import tests_workflow as fixtures
from taxes import workflow
from taxes.models import AnnualTaxWorkpaper
from taxes.recipient_identity import (
    approve_recipient_identity,
    assert_pph23_obligation,
    identity_decision,
    identity_status,
    prepare_recipient_identity,
    resolve_recipient_identity,
)


class RecipientIntegrityAdversarialTests(TestCase):
    setUp = fixtures.TaxReleaseWorkflowTests.setUp
    evidence = fixtures.TaxReleaseWorkflowTests.evidence
    external = fixtures.TaxReleaseWorkflowTests.external
    identity = fixtures.TaxReleaseWorkflowTests.identity
    pph23_context = fixtures.TaxReleaseWorkflowTests.pph23_context
    prepared = fixtures.TaxReleaseWorkflowTests.prepared
    profile = fixtures.TaxReleaseWorkflowTests.profile
    certificate = fixtures.TaxReleaseWorkflowTests.certificate

    def damage(self, row, field, value):
        """Damage exactly one synthetic stored field without changing its digest."""
        quoted = connection.ops.quote_name
        if isinstance(value, dict):
            value = json.dumps(value)
        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {quoted(row._meta.db_table)} SET {quoted(field)} = %s WHERE {quoted('id')} = %s",
                [value, row.pk],
            )
        row.refresh_from_db()

    def approved_obligation(self, direction="payable"):
        row = self.prepared(direction)
        row = workflow.approve_tax_obligation(
            row,
            self.owner,
            external_review=self.external(row, {"action": "approve_obligation"}),
        )
        self.assertEqual(assert_pph23_obligation(row)["amount"], row.amount)
        return row

    def rename_successor(self, party=None):
        original = self.identity(party)
        original_name, original_fingerprint = original.legal_name, original.fingerprint
        target = party or self.org
        target.name = "Synthetic legally renamed recipient"
        target.save(update_fields=["name"])
        successor = prepare_recipient_identity(
            organization=self.org,
            actor=self.owner,
            recipient_party=party,
            legal_name=target.name,
            recipient_type="body",
            tax_residency="spdn",
            population_status="na",
            identity_type="npwp16",
            number=original.number,
            registration_status="registered",
            effective_from=date(2026, 10, 1),
            evidence=self.evidence(),
            reference="SYN-LEGAL-NAME-CHANGE",
            reason="Synthetic professional documents a dated legal-name change",
            request_key="synthetic-name-successor",
            previous=original,
        )
        report = self.external(successor, identity_decision(treatment="standard_npwp"))
        approve_recipient_identity(
            organization=self.org,
            actor=self.owner,
            identity=successor,
            treatment="standard_npwp",
            external_review=report,
            expected_fingerprint=successor.fingerprint,
            expected_version=successor.revision,
        )
        selected = resolve_recipient_identity(
            organization=self.org,
            recipient_party=party,
            tax_point_date=date(2026, 10, 1),
        )
        self.assertEqual(selected.pk, successor.pk)
        original.refresh_from_db()
        self.assertEqual(
            (original.legal_name, original.fingerprint),
            (original_name, original_fingerprint),
        )

    def test_dated_supplier_name_change_can_receive_a_new_identity_approval(self):
        party = Party.objects.create(
            organization=self.org, name="Synthetic old supplier", kind="supplier"
        )
        self.rename_successor(party)

    def test_dated_company_name_change_can_receive_a_new_identity_approval(self):
        self.rename_successor()

    def test_changed_identity_report_decision_with_original_digest_is_rejected(self):
        identity = self.identity()
        report = identity.approval.external_review
        self.damage(
            report,
            "decision",
            {"action": "approve_recipient_identity", "treatment": "increased_no_npwp"},
        )
        with self.assertRaises(ValidationError):
            resolve_recipient_identity(
                organization=self.org, tax_point_date=date(2026, 9, 10)
            )

    def test_changed_obligation_report_decision_with_original_digest_is_rejected(self):
        obligation = self.approved_obligation()
        self.damage(
            obligation.external_review, "decision", {"action": "reject_obligation"}
        )
        obligation.refresh_from_db()
        with self.assertRaises(ValidationError):
            assert_pph23_obligation(obligation)

    def test_approved_obligation_without_bound_professional_report_is_rejected(self):
        obligation = self.approved_obligation()
        self.damage(obligation, "external_review_id", None)
        with self.assertRaises(ValidationError):
            assert_pph23_obligation(obligation)

    def test_bound_professional_report_cannot_be_replaced_through_normal_save(self):
        obligation = self.approved_obligation()
        report = obligation.external_review
        original_hash = report.evidence_sha256
        report.statement = "Synthetic replacement conclusion after approval"
        with self.assertRaises(ValidationError):
            report.save()
        report.refresh_from_db()
        self.assertEqual(report.evidence_sha256, original_hash)
        self.assertEqual(
            assert_pph23_obligation(obligation)["amount"], obligation.amount
        )

    def test_changed_pdf_and_matching_evidence_hash_cannot_replace_the_original_report(
        self,
    ):
        obligation = self.approved_obligation()
        report = obligation.external_review
        original_hash = report.evidence_sha256
        changed_content = (
            bytes(report.evidence.content) + b"\n% synthetic replacement report"
        )
        changed_hash = hashlib.sha256(changed_content).hexdigest()
        self.damage(report.evidence, "content", changed_content)
        self.damage(report.evidence, "sha256", changed_hash)
        obligation.refresh_from_db()
        self.assertNotEqual(changed_hash, original_hash)
        with self.assertRaises(ValidationError):
            assert_pph23_obligation(obligation)

    def test_missing_recorded_original_report_hash_blocks_new_approved_use(self):
        obligation = self.approved_obligation()
        self.damage(obligation.external_review, "evidence_sha256", "")
        obligation.refresh_from_db()
        with self.assertRaises(ValidationError):
            assert_pph23_obligation(obligation)

    def test_corrected_parallel_preparation_preserves_wrong_unapproved_draft_as_history(
        self,
    ):
        party = Party.objects.create(
            organization=self.org,
            name="Synthetic draft correction supplier",
            kind="supplier",
        )
        values = {
            "organization": self.org,
            "actor": self.owner,
            "recipient_party": party,
            "legal_name": party.name,
            "recipient_type": "body",
            "tax_residency": "spdn",
            "population_status": "na",
            "effective_from": date(2025, 5, 22),
            "reference": "SYN-DRAFT-IDENTITY-CORRECTION",
            "reason": "Synthetic pending facts corrected before professional approval",
        }
        self.identity()  # Explicit public source publication for this synthetic organization.
        wrong = prepare_recipient_identity(
            **values,
            identity_type="unknown",
            registration_status="unknown",
            evidence=self.evidence(),
            request_key="synthetic-wrong-pending-identity",
        )
        original_fingerprint = wrong.fingerprint
        corrected = prepare_recipient_identity(
            **values,
            identity_type="npwp16",
            number="4234567890123456",
            registration_status="registered",
            evidence=self.evidence(),
            request_key="synthetic-corrected-pending-identity",
        )
        approve_recipient_identity(
            organization=self.org,
            actor=self.owner,
            identity=corrected,
            treatment="standard_npwp",
            external_review=self.external(
                corrected, identity_decision(treatment="standard_npwp")
            ),
            expected_fingerprint=corrected.fingerprint,
            expected_version=corrected.revision,
        )
        self.assertIsNone(corrected.previous_id)
        self.assertEqual(
            resolve_recipient_identity(
                organization=self.org,
                recipient_party=party,
                tax_point_date=date(2026, 9, 10),
            ).pk,
            corrected.pk,
        )
        wrong.refresh_from_db()
        self.assertEqual(wrong.fingerprint, original_fingerprint)
        self.assertFalse(
            identity_status(organization=self.org, identity=wrong)["can_approve"]
        )
        with self.assertRaises(ValidationError):
            approve_recipient_identity(
                organization=self.org,
                actor=self.owner,
                identity=wrong,
                treatment="unresolved",
                external_review=self.external(
                    wrong, identity_decision(treatment="unresolved")
                ),
                expected_fingerprint=wrong.fingerprint,
                expected_version=wrong.revision,
            )

    def test_committed_supplier_decision_reason_must_match_the_professional_report(
        self,
    ):
        supplier = Party.objects.create(
            organization=self.org, name="Synthetic reviewed supplier", kind="supplier"
        )
        bill = finance.create_bill(
            organization=self.org,
            actor=self.owner,
            supplier=supplier,
            amount="1000000",
            date=date(2026, 9, 10),
            service_date=date(2026, 9, 10),
            supplier_reference="SYN-BILL-IDENTITY",
        )
        bill = finance.approve_bill(organization=self.org, actor=self.owner, bill=bill)
        values = {
            "rule_code": "pph23_service_2",
            "base": "1000000",
            "period": date(2026, 9, 1),
            "reason": "Synthetic professional confirms the exact supplier transaction",
            "object_code": "24-104-99",
            **self.pph23_context(
                direction="payable", tax_point_date=date(2026, 9, 10), bill=bill
            ),
        }
        report = self.external(bill, workflow.bill_decision(**values))
        decision = workflow.review_bill_tax(
            organization=self.org,
            actor=self.owner,
            bill=bill,
            external_review=report,
            **values,
        )
        self.assertEqual(assert_pph23_obligation(decision.obligation)["amount"], 20000)
        self.damage(
            decision, "reason", "Synthetic different unsupported classification"
        )
        decision.obligation.refresh_from_db()
        with self.assertRaises(ValidationError):
            assert_pph23_obligation(decision.obligation)

    def verified_credit(self):
        obligation = self.approved_obligation("receivable")
        return workflow.verify_obligation_evidence(
            organization=self.org,
            actor=self.owner,
            obligation=obligation,
            kind="credit",
            evidence=self.evidence(),
            reference="SYN-ADVERSARIAL-CREDIT",
            taxpayer_reference=obligation.identity_snapshot["recipient"]["reference"],
            period=obligation.period,
            amount=obligation.amount,
            note="Synthetic certificate matched against both parties and tax point",
            matches_confirmed=True,
            **self.certificate(obligation),
        )

    def annual(self):
        return workflow.prepare_annual_workpaper(
            organization=self.org,
            actor=self.owner,
            year=2026,
            positive_adjustments="0",
            negative_adjustments="0",
            loss_compensation="0",
            turnover="1000025",
            regime_code="corporate_22",
            instalments="0",
            support_evidence=self.evidence(),
            reconciliation_note="Synthetic documented fiscal reconciliation with verified credit",
        )

    def test_corrupted_credit_pdf_cannot_reduce_a_new_annual_workpaper(self):
        self.profile()
        credit = self.verified_credit()
        self.damage(
            credit.evidence,
            "content",
            bytes(credit.evidence.content) + b"\n% synthetic changed bytes",
        )
        count = AnnualTaxWorkpaper.objects.filter(organization=self.org).count()
        with self.assertRaises(ValidationError):
            self.annual()
        self.assertEqual(
            AnnualTaxWorkpaper.objects.filter(organization=self.org).count(), count
        )

    def test_corrupted_credit_pdf_marks_an_existing_annual_workpaper_not_current(self):
        self.profile()
        credit = self.verified_credit()
        workpaper = self.annual()
        self.assertEqual(Decimal(workpaper.result["credits"]), Decimal(20001))
        self.assertTrue(
            workflow.annual_snapshot_status(self.org, workpaper)["snapshot_current"]
        )
        self.damage(
            credit.evidence,
            "content",
            bytes(credit.evidence.content) + b"\n% synthetic changed bytes",
        )
        self.assertFalse(
            workflow.annual_snapshot_status(self.org, workpaper)["snapshot_current"]
        )


@skipUnless(connection.vendor == "postgresql", "Real PostgreSQL row locks are required")
class RecipientIdentityConcurrencyTests(TransactionTestCase):
    """Actual competing public-service writes on separate DB connections."""

    setUp = fixtures.TaxReleaseWorkflowTests.setUp
    evidence = fixtures.TaxReleaseWorkflowTests.evidence
    external = fixtures.TaxReleaseWorkflowTests.external
    identity = fixtures.TaxReleaseWorkflowTests.identity

    def proposed_values(
        self, party, *, request_key, evidence, previous=None, number="5234567890123456"
    ):
        return {
            "organization": self.org,
            "actor": self.owner,
            "recipient_party": party,
            "legal_name": party.name,
            "recipient_type": "body",
            "tax_residency": "spdn",
            "population_status": "na",
            "identity_type": "npwp16",
            "number": number,
            "registration_status": "registered",
            "effective_from": date(2026, 9, 20),
            "evidence": evidence,
            "reference": "SYN-CONCURRENT-IDENTITY",
            "reason": "Synthetic professional documents this exact dated identity",
            "request_key": request_key,
            "previous": previous,
        }

    def test_simultaneous_identical_request_returns_one_prepared_identity(self):
        # Establish reviewed sources through a separate, legitimate identity.
        self.identity()
        party = Party.objects.create(
            organization=self.org,
            name="Synthetic request retry supplier",
            kind="supplier",
        )
        values = self.proposed_values(
            party, request_key="concurrent-identical-request", evidence=self.evidence()
        )
        start = Barrier(2)

        def prepare(_):
            close_old_connections()
            try:
                start.wait(timeout=10)
                return prepare_recipient_identity(**values).pk
            finally:
                # A finished test worker must also close healthy persistent connections.
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(prepare, (0, 1)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(prepare_recipient_identity(**values).pk, results[0])

    def test_competing_successor_approvals_preserve_one_dated_identity_chain(self):
        party = Party.objects.create(
            organization=self.org,
            name="Synthetic successor race supplier",
            kind="supplier",
        )
        original = self.identity(party)
        successors = []
        for key, number in (
            ("successor-a", "5234567890123456"),
            ("successor-b", "6234567890123456"),
        ):
            identity = prepare_recipient_identity(
                **self.proposed_values(
                    party,
                    request_key=key,
                    evidence=self.evidence(),
                    previous=original,
                    number=number,
                )
            )
            report = self.external(
                identity, identity_decision(treatment="standard_npwp")
            )
            successors.append((identity, report))
        start = Barrier(2)

        def approve(pair):
            close_old_connections()
            identity, report = pair
            try:
                start.wait(timeout=10)
                try:
                    approval = approve_recipient_identity(
                        organization=self.org,
                        actor=self.owner,
                        identity=identity,
                        treatment="standard_npwp",
                        external_review=report,
                        expected_fingerprint=identity.fingerprint,
                        expected_version=identity.revision,
                    )
                except ValidationError:
                    return "conflict", identity.pk
                return "approved", approval.identity_id
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(approve, successors))
        self.assertEqual(
            sorted(result[0] for result in results), ["approved", "conflict"]
        )
        winner = next(
            identity_id for state, identity_id in results if state == "approved"
        )
        self.assertEqual(
            resolve_recipient_identity(
                organization=self.org,
                recipient_party=party,
                tax_point_date=date(2026, 9, 20),
            ).pk,
            winner,
        )
        self.assertEqual(
            resolve_recipient_identity(
                organization=self.org,
                recipient_party=party,
                tax_point_date=date(2026, 9, 19),
            ).pk,
            original.pk,
        )
