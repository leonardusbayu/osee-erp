"""Actual opening-cash refunds need their own received fiscal authority.

The original monthly inclusion is evidence-bound; opposite bank cash does not
silently reverse that conclusion or authorize a withholding recovery.
"""

from copy import deepcopy
from datetime import date
from decimal import Decimal
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import TestCase

from finance import opening_balances as opening
from finance import services as ledger
from finance.models import AccountingPeriod, Bill, Invoice
from finance.test_statement_fixtures import reviewed_statement

from . import tests_monthly_packages as package_fixtures
from . import workflow
from .models import MonthlyFiscalApproval, TaxObligation
from .monthly_source_workflow import package_status, prepare_monthly_package
from .monthly_sources import build_monthly_sources
from .opening_balance_review import record_opening_settlement_review
from .tests_opening_balance_review import OpeningReviewFixtures


class OpeningRefundFiscalTests(OpeningReviewFixtures, TestCase):
    profile = package_fixtures.MonthlySourcePackageTests.profile
    decision = package_fixtures.MonthlySourcePackageTests.decision
    approve = package_fixtures.MonthlySourcePackageTests.approve

    def test_later_real_ar_refund_keeps_original_authority_and_needs_distinct_principal_only_review(
        self,
    ):
        self.cutoff, self.day = date(2026, 8, 1), date(2026, 8, 10)
        self.owner = self.finance = self.actor
        self.profile()
        item = self.item("300", kind="receivable")
        receipt = self.cash(item, amount="100", day=self.day)
        original_authority = record_opening_settlement_review(
            **self.review_data(receipt, reviewed_on=date(2026, 8, 12))
        )
        original_report = original_authority.external_review
        original_pdf = bytes(original_report.evidence.content)
        original_report_values = deepcopy(
            (
                original_report.subject_type,
                original_report.subject_id,
                original_report.subject_digest,
                original_report.decision,
                original_report.evidence_id,
                original_report.evidence_sha256,
                original_authority.source_snapshot,
                original_authority.snapshot_hash,
            )
        )
        item_proof = opening.opening_balance_source_snapshot(
            organization=self.org, item=item
        )
        cash_proof = opening.opening_settlement_source_snapshot(
            organization=self.org, settlement=receipt
        )
        original_source_fingerprint = self.context(receipt)["source_fingerprint"]
        original_bank = receipt.bank_posting.transaction

        reviewed_statement(
            self,
            organization=self.org,
            actor=self.actor,
            account=self.account,
            start=date(2026, 8, 1),
            end=date(2026, 8, 31),
            opening="0",
            closing="100",
            total_in="100",
            total_out="0",
            count=1,
        )
        august = prepare_monthly_package(
            organization=self.org, actor=self.actor, period="2026-08"
        )
        august_source = next(
            row
            for row in august.snapshot["rows"]
            if row["source_kind"] == "opening-receipt"
            and row["source_id"] == receipt.pk
        )
        august_approval = self.approve(
            august,
            self.decision(
                august,
                rows=[
                    {
                        "row_id": august_source["row_id"],
                        "decision": "include",
                        "reason": "Synthetic received monthly report establishes this original receipt belongs to August.",
                    }
                ],
            ),
        )
        self.assertEqual(august_approval.result["reviewed_bridge_amount"], "100.00")
        self.assertEqual(
            august_approval.result["allocations"],
            [
                {
                    "kind": "opening_cash",
                    "invoice_id": None,
                    "source_row_ids": [f"opening-receipt:{receipt.pk}"],
                    "amount": "100.00",
                }
            ],
        )
        self.assertEqual(
            package_status(organization=self.org, package=august)["status"], "approved"
        )
        august_preserved = deepcopy(
            (
                august.snapshot,
                august.profile_snapshot,
                august.legal_sources,
                august.package_hash,
                august_approval.decision,
                august_approval.result,
            )
        )
        august_sources = build_monthly_sources(self.org, date(2026, 8, 1))
        august_monthly_report = august_approval.external_review
        august_monthly_pdf = bytes(august_monthly_report.evidence.content)

        refund_bank = self.movement(
            "-40", day=date(2026, 9, 15), reference=f"SYN-ACTUAL-AR-REFUND-{uuid4()}"
        )
        preview = opening.opening_balance_action_context(
            organization=self.org,
            action="refund",
            settlement=receipt,
            transaction=refund_bank,
            amount="40",
        )
        self.assertTrue(preview["current"], preview)
        self.assertFalse(preview["blockers"], preview)
        refund = opening.refund_opening_settlement(
            organization=self.org,
            actor=self.actor,
            settlement=receipt,
            transaction=refund_bank,
            amount="40",
            reason="Synthetic genuine return of forty from the original one-hundred receipt.",
            evidence="Synthetic original receipt and distinct September outgoing bank refund.",
            request_key=str(uuid4()),
            expected_fingerprint=preview["fingerprint"],
            confirmed=True,
        )
        refund_proof = opening.opening_refund_source_snapshot(
            organization=self.org, refund=refund
        )
        balances = ledger.report_balances(
            organization=self.org, as_of=date(2026, 9, 30)
        )
        self.assertEqual(
            tuple(
                balances[key]
                for key in ("AR", "BANK", "REVENUE", "EXPENSE", "DEFERRED_REVENUE")
            ),
            (Decimal(240), Decimal(60), Decimal(0), Decimal(0), Decimal(0)),
        )
        self.assertEqual(
            (original_bank.unallocated_amount, refund_bank.unallocated_amount),
            (Decimal(0), Decimal(0)),
        )
        self.assertEqual(
            opening.opening_balance_source_snapshot(organization=self.org, item=item),
            item_proof,
        )
        self.assertEqual(
            opening.opening_settlement_source_snapshot(
                organization=self.org, settlement=receipt
            ),
            cash_proof,
        )
        self.assertEqual(
            (
                refund_proof["original_transaction_id"],
                refund_proof["transaction_id"],
                refund_proof["settlement_id"],
                refund_proof["amount"],
                refund_proof["date"],
            ),
            (original_bank.pk, refund_bank.pk, receipt.pk, "40.00", "2026-09-15"),
        )

        # Resolve the new public fiscal module only after genuine original cash,
        # its received report and monthly approval, and opposite-bank refund exist.
        from . import opening_refund_review as refund_review

        pending = refund_review.opening_refund_review_context(
            organization=self.org, refund=refund
        )
        self.assertEqual(
            (pending["status"], pending["version"], pending["history"]),
            ("pending", 0, []),
        )
        self.assertTrue(pending["blockers"])
        self.assertEqual(self.context(receipt)["status"], "reviewed")
        self.assertEqual(
            self.context(receipt)["source_fingerprint"], original_source_fingerprint
        )
        self.assertEqual(
            build_monthly_sources(self.org, date(2026, 8, 1)), august_sources
        )
        august_status = package_status(organization=self.org, package=august)
        self.assertEqual(august_status["status"], "approved")
        self.assertFalse(august_status["stale"])

        reviewed_statement(
            self,
            organization=self.org,
            actor=self.actor,
            account=self.account,
            start=date(2026, 9, 1),
            end=date(2026, 9, 30),
            opening="100",
            closing="60",
            total_in="0",
            total_out="40",
            count=1,
        )
        september = prepare_monthly_package(
            organization=self.org, actor=self.actor, period="2026-09"
        )
        refund_row = next(
            row
            for row in september.snapshot["rows"]
            if row["source_kind"] == "opening-refund" and row["source_id"] == refund.pk
        )
        self.assertEqual(
            (
                refund_row["invoice_id"],
                refund_row["amount"],
                refund_row["cash_amount"],
                refund_row["gross_amount"],
                refund_row["proposed_inclusion"],
            ),
            (None, "-40.00", "-40.00", None, False),
        )
        self.assertEqual(refund_row["source_proof"], refund_proof)
        self.assertEqual(len(september.snapshot["rows"]), 1)
        self.assertTrue(
            any(
                gate["code"] == "opening_refund_review"
                for gate in september.profile_snapshot["gates"]
            )
        )
        exclusion = self.decision(
            september,
            rows=[
                {
                    "row_id": refund_row["row_id"],
                    "decision": "exclude",
                    "reason": "Synthetic report identifies a principal-only refund; no September income correction is requested.",
                }
            ],
            prior_history_confirmed=True,
        )
        with self.assertRaises(ValidationError):
            self.approve(september, exclusion)
        self.assertFalse(
            MonthlyFiscalApproval.objects.filter(package=september).exists()
        )

        summary = ledger.monthly_summary(organization=self.org, year=2026, month=9)
        self.assertTrue(
            any("pengembalian" in message.lower() for message in summary["blockers"]),
            summary,
        )
        with self.assertRaises(ValidationError):
            ledger.close_month(
                organization=self.org, actor=self.actor, year=2026, month=9
            )
        self.assertFalse(
            AccountingPeriod.objects.filter(
                organization=self.org, year=2026, month=9, closed=True
            ).exists()
        )
        annual_pending = workflow.annual_tax_snapshot(
            self.org, date(2026, 1, 1), date(2026, 12, 31)
        )
        self.assertEqual(
            annual_pending["opening_refund_reviews"]["inventory"][0]["refund_id"],
            refund.pk,
        )
        self.assertEqual(
            annual_pending["opening_refund_reviews"]["inventory"][0]["status"],
            "pending",
        )

        refund_decision = {
            "schema": "opening-refund-tax-review",
            "version": 1,
            "scope": "refund_cash_event",
            "source_fingerprint": pending["source_fingerprint"],
            "resolution": "principal_only",
            "reviewed_facts": {
                "cash_amount": "40.00",
                "payment_date": "2026-09-15",
                "contract_reference": item.source_reference,
                "gross_amount": None,
                "withholding_amount": "0.00",
                "withholding_recovery_requested": False,
                "tax_credit_restitution_requested": False,
                "prior_period_income_correction_requested": False,
                "legal_basis_reference": "Synthetic received refund report paragraph 5",
                "professional_instruction": "Synthetic report confirms principal only; no withholding recovery, tax credit restitution or prior-period income amendment is requested.",
            },
        }
        refund_authority = refund_review.record_opening_refund_review(
            organization=self.org,
            actor=self.actor,
            refund=refund,
            decision=refund_decision,
            evidence=self.evidence(),
            professional_name="Synthetic refund professional",
            qualification_reference="Synthetic refund-specific engagement",
            reviewed_on=date(2026, 9, 16),
            statement="Synthetic received report confirms this distinct opposite-bank refund is principal only.",
            report_confirmed=True,
            expected_version=pending["version"],
            expected_fingerprint=pending["fingerprint"],
            request_key=str(uuid4()),
        )
        refund_report = refund_authority.external_review
        self.assertEqual(
            (refund_report.subject_type, refund_report.subject_id),
            ("openingbalancerefund", refund.pk),
        )
        self.assertNotEqual(refund_report.pk, original_report.pk)
        self.assertNotEqual(refund_report.evidence_id, original_report.evidence_id)
        accepted = refund_review.opening_refund_review_context(
            organization=self.org, refund=refund
        )
        self.assertEqual((accepted["status"], accepted["blockers"]), ("reviewed", []))

        reviewed_september = prepare_monthly_package(
            organization=self.org, actor=self.actor, period="2026-09"
        )
        self.assertNotEqual(reviewed_september.pk, september.pk)
        self.assertEqual(reviewed_september.source_hash, september.source_hash)
        self.assertNotEqual(reviewed_september.package_hash, september.package_hash)
        self.assertTrue(
            package_status(organization=self.org, package=september)["stale"]
        )
        approval = self.approve(
            reviewed_september,
            self.decision(
                reviewed_september,
                rows=[
                    {
                        "row_id": refund_row["row_id"],
                        "decision": "exclude",
                        "reason": "Synthetic received refund-specific and monthly reports explicitly exclude this principal return.",
                    }
                ],
                prior_history_confirmed=True,
            ),
        )
        self.assertEqual(
            (
                approval.result["reviewed_bridge_amount"],
                approval.result["allocations"],
                approval.result["taxable_turnover"],
                approval.result["final_tax"],
            ),
            ("0.00", [], None, None),
        )
        self.assertEqual(
            package_status(organization=self.org, package=reviewed_september)["status"],
            "approved",
        )
        self.assertTrue(
            ledger.close_month(
                organization=self.org, actor=self.actor, year=2026, month=9
            ).closed
        )
        annual = workflow.annual_tax_snapshot(
            self.org, date(2026, 1, 1), date(2026, 12, 31)
        )
        refund_dependency = annual["opening_refund_reviews"]["inventory"][0]
        self.assertEqual(
            (
                refund_dependency["refund_id"],
                refund_dependency["status"],
                refund_dependency["external_review_id"],
            ),
            (refund.pk, "reviewed", refund_report.pk),
        )
        self.assertNotEqual(
            annual["opening_refund_reviews"], annual_pending["opening_refund_reviews"]
        )
        retained_refund = next(
            row
            for row in annual["opening_principal_sources"]["inventory"]
            if row.get("refund_id") == refund.pk
        )
        self.assertEqual(retained_refund["source"], refund_proof)

        original_authority.refresh_from_db()
        original_report.refresh_from_db()
        original_report.evidence.refresh_from_db()
        august.refresh_from_db()
        august_approval.refresh_from_db()
        august_monthly_report.evidence.refresh_from_db()
        self.assertEqual(
            (
                original_report.subject_type,
                original_report.subject_id,
                original_report.subject_digest,
                original_report.decision,
                original_report.evidence_id,
                original_report.evidence_sha256,
                original_authority.source_snapshot,
                original_authority.snapshot_hash,
            ),
            original_report_values,
        )
        self.assertEqual(bytes(original_report.evidence.content), original_pdf)
        self.assertEqual(
            bytes(august_monthly_report.evidence.content), august_monthly_pdf
        )
        self.assertEqual(
            (
                august.snapshot,
                august.profile_snapshot,
                august.legal_sources,
                august.package_hash,
                august_approval.decision,
                august_approval.result,
            ),
            august_preserved,
        )
        self.assertEqual(
            package_status(organization=self.org, package=august)["status"], "approved"
        )
        self.assertEqual(
            opening.opening_settlement_source_snapshot(
                organization=self.org, settlement=receipt
            ),
            cash_proof,
        )
        self.assertEqual(
            opening.opening_refund_source_snapshot(
                organization=self.org, refund=refund
            ),
            refund_proof,
        )
        self.assertEqual(
            (
                Invoice.objects.count(),
                Bill.objects.count(),
                TaxObligation.objects.count(),
            ),
            (0, 0, 0),
        )
        self.assertEqual(ledger.report_balances(organization=self.org), balances)

    def test_superseded_intact_original_more_evidence_report_does_not_block_actual_refund(
        self,
    ):
        from core.models import AuditEvent

        from .opening_refund_review_dependencies import opening_refund_record_blockers

        item = self.item("300", kind="receivable")
        receipt = self.cash(item, amount="100", day=date(2026, 9, 10))
        first_data = self.review_data(
            receipt,
            reviewed_on=date(2026, 9, 11),
            statement="Synthetic received original report requires further supporting documents.",
        )
        first_data["decision"]["resolution"] = "more_evidence"
        first_data["decision"]["reviewed_facts"]["withholding_amount"] = None
        first_data["decision"]["reviewed_facts"]["professional_instruction"] = (
            "Synthetic first report requests additional original-cash evidence."
        )
        first = record_opening_settlement_review(**first_data)
        self.assertEqual(self.context(receipt)["status"], "held")
        second = record_opening_settlement_review(
            **self.review_data(
                receipt,
                reviewed_on=date(2026, 9, 12),
                statement="Synthetic later received report resolves the earlier evidence request and concludes no withholding.",
            )
        )
        current = self.context(receipt)
        self.assertEqual(
            (current["status"], current["version"], second.previous_id),
            ("reviewed", 2, first.pk),
        )
        self.assertEqual([row.pk for row in current["history"]], [first.pk, second.pk])
        original_proof = opening.opening_settlement_source_snapshot(
            organization=self.org, settlement=receipt
        )
        saved_records = deepcopy(
            [
                (
                    row.pk,
                    row.decision,
                    row.source_snapshot,
                    row.report_snapshot,
                    row.snapshot_hash,
                    row.external_review_id,
                )
                for row in (first, second)
            ]
        )
        saved_reports = deepcopy(
            [
                (
                    row.external_review.pk,
                    row.external_review.subject_digest,
                    row.external_review.decision,
                    bytes(row.external_review.evidence.content),
                )
                for row in (first, second)
            ]
        )
        saved_audits = list(
            AuditEvent.objects.filter(
                organization=self.org,
                action__in=(
                    "tax.external_review.recorded",
                    "tax.opening_balance_review.recorded",
                ),
            )
            .order_by("pk")
            .values()
        )

        bank = self.movement(
            "-40", day=date(2026, 9, 15), reference=f"SYN-SUPERSEDED-REFUND-{uuid4()}"
        )
        blockers = opening_refund_record_blockers(
            organization=self.org,
            settlement=receipt,
            transaction=bank,
            date=bank.date,
            amount="40",
        )
        self.assertEqual(blockers, [], blockers)
        preview = opening.opening_balance_action_context(
            organization=self.org,
            action="refund",
            settlement=receipt,
            transaction=bank,
            amount="40",
        )
        self.assertTrue(preview["current"], preview)
        self.assertEqual(preview["blockers"], [])
        refund = opening.refund_opening_settlement(
            organization=self.org,
            actor=self.actor,
            settlement=receipt,
            transaction=bank,
            amount="40",
            reason="Synthetic actual principal return after the original evidence request was professionally resolved.",
            evidence="Synthetic new opposite bank cash and intact original report history.",
            request_key=str(uuid4()),
            expected_fingerprint=preview["fingerprint"],
            confirmed=True,
        )
        self.assertEqual((refund.amount, refund.date), (Decimal(40), date(2026, 9, 15)))
        balances = ledger.report_balances(organization=self.org)
        self.assertEqual(
            tuple(
                balances[key]
                for key in ("AR", "BANK", "REVENUE", "EXPENSE", "TAX_PAYABLE")
            ),
            (Decimal(240), Decimal(60), Decimal(0), Decimal(0), Decimal(0)),
        )
        self.assertEqual(
            opening.opening_settlement_source_snapshot(
                organization=self.org, settlement=receipt
            ),
            original_proof,
        )
        self.assertEqual(self.context(receipt)["status"], "reviewed")
        for row in (first, second):
            row.refresh_from_db()
            row.external_review.refresh_from_db()
            row.external_review.evidence.refresh_from_db()
        self.assertEqual(
            [
                (
                    row.pk,
                    row.decision,
                    row.source_snapshot,
                    row.report_snapshot,
                    row.snapshot_hash,
                    row.external_review_id,
                )
                for row in (first, second)
            ],
            saved_records,
        )
        self.assertEqual(
            [
                (
                    row.external_review.pk,
                    row.external_review.subject_digest,
                    row.external_review.decision,
                    bytes(row.external_review.evidence.content),
                )
                for row in (first, second)
            ],
            saved_reports,
        )
        self.assertEqual(
            list(
                AuditEvent.objects.filter(
                    organization=self.org,
                    action__in=(
                        "tax.external_review.recorded",
                        "tax.opening_balance_review.recorded",
                    ),
                )
                .order_by("pk")
                .values()
            ),
            saved_audits,
        )
        self.assertFalse(TaxObligation.objects.exists())

    def test_deleted_typed_refund_review_retains_authority_and_rejects_new_root_after_monthly_approval(
        self,
    ):
        from django.db import connection

        from core.models import AuditEvent
        from finance.models import OpeningBalanceRefund

        from . import opening_refund_review as refund_review
        from .models import OpeningRefundTaxReview, TaxEvidence

        self.owner = self.finance = self.actor
        self.profile()
        item = self.item("300", kind="receivable")
        receipt = self.cash(item, amount="100", day=date(2026, 9, 10))
        record_opening_settlement_review(
            **self.review_data(receipt, reviewed_on=date(2026, 9, 11))
        )
        bank = self.movement(
            "-40",
            day=date(2026, 9, 15),
            reference=f"SYN-DELETED-AUTHORITY-REFUND-{uuid4()}",
        )
        preview = opening.opening_balance_action_context(
            organization=self.org,
            action="refund",
            settlement=receipt,
            transaction=bank,
            amount="40",
        )
        refund = opening.refund_opening_settlement(
            organization=self.org,
            actor=self.actor,
            settlement=receipt,
            transaction=bank,
            amount="40",
            reason="Synthetic actual refund needing an independent received report.",
            evidence="Synthetic actual opposite bank cash.",
            request_key=str(uuid4()),
            expected_fingerprint=preview["fingerprint"],
            confirmed=True,
        )
        pending = refund_review.opening_refund_review_context(
            organization=self.org, refund=refund
        )
        decision = {
            "schema": "opening-refund-tax-review",
            "version": 1,
            "scope": "refund_cash_event",
            "source_fingerprint": pending["source_fingerprint"],
            "resolution": "principal_only",
            "reviewed_facts": {
                "cash_amount": "40.00",
                "payment_date": "2026-09-15",
                "contract_reference": item.source_reference,
                "gross_amount": None,
                "withholding_amount": "0.00",
                "withholding_recovery_requested": False,
                "tax_credit_restitution_requested": False,
                "prior_period_income_correction_requested": False,
                "legal_basis_reference": "Synthetic independent principal-refund report paragraph 4",
                "professional_instruction": "Synthetic received report confirms principal only and no tax recovery, credit restitution or prior-period amendment.",
            },
        }
        authority = refund_review.record_opening_refund_review(
            organization=self.org,
            actor=self.actor,
            refund=refund,
            decision=decision,
            evidence=self.evidence(),
            professional_name="Synthetic refund professional",
            qualification_reference="Synthetic documented refund assignment",
            reviewed_on=date(2026, 9, 16),
            statement="Synthetic received report confirms this exact principal-only refund.",
            report_confirmed=True,
            expected_version=pending["version"],
            expected_fingerprint=pending["fingerprint"],
            request_key=str(uuid4()),
        )
        reviewed_statement(
            self,
            organization=self.org,
            actor=self.actor,
            account=self.account,
            start=date(2026, 9, 1),
            end=date(2026, 9, 30),
            opening="0",
            closing="60",
            total_in="100",
            total_out="40",
            count=2,
        )
        package = prepare_monthly_package(
            organization=self.org, actor=self.actor, period="2026-09"
        )
        approval = self.approve(
            package,
            self.decision(
                package,
                rows=[
                    {
                        "row_id": row["row_id"],
                        "decision": "exclude",
                        "reason": "Synthetic received monthly report explicitly excludes the original principal receipt and principal return.",
                    }
                    for row in package.snapshot["rows"]
                ],
                prior_history_confirmed=True,
            ),
        )
        self.assertEqual(
            (approval.result["reviewed_bridge_amount"], approval.result["allocations"]),
            ("0.00", []),
        )
        self.assertEqual(
            package_status(organization=self.org, package=package)["status"], "approved"
        )
        accepted = refund_review.opening_refund_review_context(
            organization=self.org, refund=refund
        )
        self.assertEqual((accepted["status"], accepted["version"]), ("reviewed", 1))
        report = authority.external_review
        report_values = deepcopy(type(report).objects.values().get(pk=report.pk))
        report_pdf = bytes(report.evidence.content)
        package_values = deepcopy(
            (
                package.snapshot,
                package.profile_snapshot,
                package.package_hash,
                approval.decision,
                approval.result,
            )
        )
        original_cash = opening.opening_settlement_source_snapshot(
            organization=self.org, settlement=receipt
        )
        refund_cash = opening.opening_refund_source_snapshot(
            organization=self.org, refund=refund
        )
        new_evidence = self.evidence()
        saved_audits = list(
            AuditEvent.objects.filter(organization=self.org).order_by("pk").values()
        )
        authority_audits = [
            row
            for row in saved_audits
            if row["action"] == "tax.opening_refund_review.recorded"
            and row["object_id"] == str(authority.pk)
        ]
        self.assertEqual(len(authority_audits), 1)
        self.assertEqual(authority_audits[0]["detail"]["refund_id"], refund.pk)

        # Isolated persisted damage removes only the typed decision. The actual
        # received report, PDF, original receipt audit and approved monthly
        # documents deliberately remain; a new version-one root is forbidden.
        quote = connection.ops.quote_name
        with connection.cursor() as cursor:
            cursor.execute(
                f"DELETE FROM {quote(OpeningRefundTaxReview._meta.db_table)} WHERE id = %s",
                [authority.pk],
            )
        self.assertFalse(
            OpeningRefundTaxReview.objects.filter(pk=authority.pk).exists()
        )
        damaged = refund_review.opening_refund_review_context(
            organization=self.org, refund=refund
        )
        before_counts = (
            self.counts(),
            OpeningRefundTaxReview.objects.count(),
            TaxEvidence.objects.count(),
            MonthlyFiscalApproval.objects.count(),
            OpeningBalanceRefund.objects.count(),
        )
        before_balances = ledger.report_balances(organization=self.org)
        with self.assertRaises(ValidationError):
            refund_review.record_opening_refund_review(
                organization=self.org,
                actor=self.actor,
                refund=refund,
                decision=deepcopy(decision),
                evidence=new_evidence,
                professional_name="Synthetic replacement professional",
                qualification_reference="Synthetic replacement assignment",
                reviewed_on=date(2026, 9, 17),
                statement="Synthetic new report must not recreate a deleted history root.",
                report_confirmed=True,
                expected_version=damaged["version"],
                expected_fingerprint=damaged["fingerprint"],
                request_key=str(uuid4()),
            )
        self.assertEqual(damaged["status"], "held")
        self.assertTrue(damaged["blockers"])
        self.assertNotEqual(damaged["fingerprint"], accepted["fingerprint"])
        self.assertEqual(
            (
                self.counts(),
                OpeningRefundTaxReview.objects.count(),
                TaxEvidence.objects.count(),
                MonthlyFiscalApproval.objects.count(),
                OpeningBalanceRefund.objects.count(),
            ),
            before_counts,
        )
        self.assertEqual(ledger.report_balances(organization=self.org), before_balances)
        self.assertEqual(
            list(
                AuditEvent.objects.filter(organization=self.org).order_by("pk").values()
            ),
            saved_audits,
        )
        report.refresh_from_db()
        report.evidence.refresh_from_db()
        self.assertEqual(type(report).objects.values().get(pk=report.pk), report_values)
        self.assertEqual(bytes(report.evidence.content), report_pdf)
        package.refresh_from_db()
        approval.refresh_from_db()
        self.assertEqual(
            (
                package.snapshot,
                package.profile_snapshot,
                package.package_hash,
                approval.decision,
                approval.result,
            ),
            package_values,
        )
        self.assertTrue(package_status(organization=self.org, package=package)["stale"])
        self.assertEqual(
            opening.opening_settlement_source_snapshot(
                organization=self.org, settlement=receipt
            ),
            original_cash,
        )
        self.assertEqual(
            opening.opening_refund_source_snapshot(
                organization=self.org, refund=refund
            ),
            refund_cash,
        )
        annual = workflow.annual_tax_snapshot(
            self.org, date(2026, 1, 1), date(2026, 12, 31)
        )
        self.assertEqual(
            annual["opening_refund_reviews"]["inventory"][0]["status"], "held"
        )
        self.assertFalse(TaxObligation.objects.exists())

    def test_ap_principal_only_requires_explicit_effect_statements_before_authority_and_monthly_exclusion(
        self,
    ):
        from core.models import AuditEvent

        from . import opening_refund_review as refund_review
        from .models import ExternalTaxReview, OpeningRefundTaxReview

        self.owner = self.finance = self.actor
        self.profile()
        item = self.item("300", kind="payable")
        receipt = self.cash(item, amount="100", day=date(2026, 9, 10))
        original = record_opening_settlement_review(
            **self.review_data(receipt, reviewed_on=date(2026, 9, 11))
        )
        original_values = deepcopy(
            (
                original.source_snapshot,
                original.decision,
                original.snapshot_hash,
                original.external_review.subject_digest,
                bytes(original.external_review.evidence.content),
            )
        )
        bank = self.movement(
            "20", day=date(2026, 9, 15), reference=f"SYN-AP-PRINCIPAL-REFUND-{uuid4()}"
        )
        preview = opening.opening_balance_action_context(
            organization=self.org,
            action="refund",
            settlement=receipt,
            transaction=bank,
            amount="20",
        )
        refund = opening.refund_opening_settlement(
            organization=self.org,
            actor=self.actor,
            settlement=receipt,
            transaction=bank,
            amount="20",
            reason="Synthetic actual supplier principal return.",
            evidence="Synthetic new incoming bank cash from the supplier.",
            request_key=str(uuid4()),
            expected_fingerprint=preview["fingerprint"],
            confirmed=True,
        )
        proof = opening.opening_refund_source_snapshot(
            organization=self.org, refund=refund
        )
        original_proof = opening.opening_settlement_source_snapshot(
            organization=self.org, settlement=receipt
        )
        pending = refund_review.opening_refund_review_context(
            organization=self.org, refund=refund
        )
        decision = {
            "schema": "opening-refund-tax-review",
            "version": 1,
            "scope": "refund_cash_event",
            "source_fingerprint": pending["source_fingerprint"],
            "resolution": "principal_only",
            "reviewed_facts": {
                "cash_amount": "20.00",
                "payment_date": "2026-09-15",
                "contract_reference": item.source_reference,
                "gross_amount": None,
                "withholding_amount": "0.00",
                "withholding_recovery_requested": False,
                "tax_credit_restitution_requested": False,
                "prior_period_income_correction_requested": False,
                "legal_basis_reference": "Synthetic AP refund professional report paragraph 3",
                "professional_instruction": "Synthetic report states principal only, zero new withholding and no recovery, restitution or prior-period amendment.",
            },
        }
        data = dict(
            organization=self.org,
            actor=self.actor,
            refund=refund,
            decision=decision,
            evidence=self.evidence(),
            professional_name="Synthetic AP professional",
            qualification_reference="Synthetic AP refund engagement",
            reviewed_on=date(2026, 9, 16),
            statement="Synthetic received AP refund report with explicit separate tax-effect statements.",
            report_confirmed=True,
            expected_version=pending["version"],
            expected_fingerprint=pending["fingerprint"],
        )
        before = (
            OpeningRefundTaxReview.objects.count(),
            ExternalTaxReview.objects.count(),
            AuditEvent.objects.count(),
            MonthlyFiscalApproval.objects.count(),
            TaxObligation.objects.count(),
        )
        balances = ledger.report_balances(organization=self.org)
        self.assertEqual(
            tuple(
                balances[key]
                for key in ("AP", "BANK", "REVENUE", "EXPENSE", "TAX_PAYABLE")
            ),
            (Decimal(-220), Decimal(-80), Decimal(0), Decimal(0), Decimal(0)),
        )
        for flag in (
            "withholding_recovery_requested",
            "tax_credit_restitution_requested",
            "prior_period_income_correction_requested",
        ):
            for treatment in ("missing", "unexplained", "requested"):
                with self.subTest(flag=flag, treatment=treatment):
                    unsupported = deepcopy(decision)
                    if treatment == "missing":
                        unsupported["reviewed_facts"].pop(flag)
                    else:
                        unsupported["reviewed_facts"][flag] = (
                            None if treatment == "unexplained" else True
                        )
                    with self.assertRaises(ValidationError):
                        refund_review.record_opening_refund_review(
                            **dict(data, decision=unsupported, request_key=str(uuid4()))
                        )
                    self.assertEqual(
                        (
                            OpeningRefundTaxReview.objects.count(),
                            ExternalTaxReview.objects.count(),
                            AuditEvent.objects.count(),
                            MonthlyFiscalApproval.objects.count(),
                            TaxObligation.objects.count(),
                        ),
                        before,
                    )
                    self.assertEqual(
                        refund_review.opening_refund_review_context(
                            organization=self.org, refund=refund
                        )["status"],
                        "pending",
                    )
                    self.assertEqual(
                        ledger.report_balances(organization=self.org), balances
                    )
        authority = refund_review.record_opening_refund_review(
            **dict(data, request_key=str(uuid4()))
        )
        self.assertEqual(
            refund_review.opening_refund_review_context(
                organization=self.org, refund=refund
            )["status"],
            "reviewed",
        )
        self.assertEqual(
            (
                authority.external_review.subject_type,
                authority.external_review.subject_id,
            ),
            ("openingbalancerefund", refund.pk),
        )
        self.assertNotEqual(authority.external_review_id, original.external_review_id)
        reviewed_statement(
            self,
            organization=self.org,
            actor=self.actor,
            account=self.account,
            start=date(2026, 9, 1),
            end=date(2026, 9, 30),
            opening="0",
            closing="-80",
            total_in="20",
            total_out="100",
            count=2,
        )
        package = prepare_monthly_package(
            organization=self.org, actor=self.actor, period="2026-09"
        )
        self.assertEqual(len(package.snapshot["rows"]), 1)
        row = package.snapshot["rows"][0]
        self.assertEqual(
            (
                row["source_kind"],
                row["source_id"],
                row["amount"],
                row["invoice_id"],
                row["gross_amount"],
            ),
            ("opening-refund", refund.pk, "20.00", None, None),
        )
        approval = self.approve(
            package,
            self.decision(
                package,
                rows=[
                    {
                        "row_id": row["row_id"],
                        "decision": "exclude",
                        "reason": "Synthetic received monthly report explicitly excludes the supplier principal return.",
                    }
                ],
                prior_history_confirmed=True,
            ),
        )
        self.assertEqual(
            (
                approval.result["reviewed_bridge_amount"],
                approval.result["allocations"],
                approval.result["final_tax"],
            ),
            ("0.00", [], None),
        )
        self.assertEqual(
            package_status(organization=self.org, package=package)["status"], "approved"
        )
        original.refresh_from_db()
        original.external_review.refresh_from_db()
        original.external_review.evidence.refresh_from_db()
        self.assertEqual(
            (
                original.source_snapshot,
                original.decision,
                original.snapshot_hash,
                original.external_review.subject_digest,
                bytes(original.external_review.evidence.content),
            ),
            original_values,
        )
        self.assertEqual(
            opening.opening_settlement_source_snapshot(
                organization=self.org, settlement=receipt
            ),
            original_proof,
        )
        self.assertEqual(
            opening.opening_refund_source_snapshot(
                organization=self.org, refund=refund
            ),
            proof,
        )
        self.assertEqual(ledger.report_balances(organization=self.org), balances)
        self.assertFalse(TaxObligation.objects.exists())

    def test_ap_refund_pdf_damage_and_moved_typed_aliases_hold_fiscal_history_without_changing_cash(
        self,
    ):
        import json

        from core.models import AuditEvent

        from . import opening_refund_review as refund_review
        from .models import OpeningRefundTaxReview, TaxEvidence

        self.owner = self.finance = self.actor
        self.profile()
        item = self.item("300", kind="payable")
        receipt = self.cash(item, amount="100", day=date(2026, 9, 10))
        record_opening_settlement_review(
            **self.review_data(receipt, reviewed_on=date(2026, 9, 11))
        )
        bank = self.movement(
            "20", day=date(2026, 9, 15), reference=f"SYN-AP-REPAIR-{uuid4()}"
        )
        preview = opening.opening_balance_action_context(
            organization=self.org,
            action="refund",
            settlement=receipt,
            transaction=bank,
            amount="20",
        )
        refund = opening.refund_opening_settlement(
            organization=self.org,
            actor=self.actor,
            settlement=receipt,
            transaction=bank,
            amount="20",
            reason="Synthetic supplier principal refund.",
            evidence="Synthetic distinct actual incoming bank cash.",
            request_key=str(uuid4()),
            expected_fingerprint=preview["fingerprint"],
            confirmed=True,
        )
        pending = refund_review.opening_refund_review_context(
            organization=self.org, refund=refund
        )
        decision = {
            "schema": "opening-refund-tax-review",
            "version": 1,
            "scope": "refund_cash_event",
            "source_fingerprint": pending["source_fingerprint"],
            "resolution": "principal_only",
            "reviewed_facts": {
                "cash_amount": "20.00",
                "payment_date": "2026-09-15",
                "contract_reference": item.source_reference,
                "gross_amount": None,
                "withholding_amount": "0.00",
                "withholding_recovery_requested": False,
                "tax_credit_restitution_requested": False,
                "prior_period_income_correction_requested": False,
                "legal_basis_reference": "Synthetic supplier refund report paragraph 4",
                "professional_instruction": "Synthetic principal only; no tax recovery, credit restitution or prior-period amendment.",
            },
        }
        authority = refund_review.record_opening_refund_review(
            organization=self.org,
            actor=self.actor,
            refund=refund,
            decision=decision,
            evidence=self.evidence(),
            professional_name="Synthetic AP professional",
            qualification_reference="Synthetic independent AP refund assignment",
            reviewed_on=date(2026, 9, 16),
            statement="Synthetic received report establishes this AP refund's separate principal-only conclusion.",
            report_confirmed=True,
            expected_version=pending["version"],
            expected_fingerprint=pending["fingerprint"],
            request_key=str(uuid4()),
        )
        report = authority.external_review
        pdf = bytes(report.evidence.content)
        reviewed_statement(
            self,
            organization=self.org,
            actor=self.actor,
            account=self.account,
            start=date(2026, 9, 1),
            end=date(2026, 9, 30),
            opening="0",
            closing="-80",
            total_in="20",
            total_out="100",
            count=2,
        )
        package = prepare_monthly_package(
            organization=self.org, actor=self.actor, period="2026-09"
        )
        self.approve(
            package,
            self.decision(
                package,
                rows=[
                    {
                        "row_id": row["row_id"],
                        "decision": "exclude",
                        "reason": "Synthetic monthly report explicitly excludes the AP principal return.",
                    }
                    for row in package.snapshot["rows"]
                ],
                prior_history_confirmed=True,
            ),
        )
        accepted = refund_review.opening_refund_review_context(
            organization=self.org, refund=refund
        )
        annual = workflow.annual_tax_snapshot(
            self.org, date(2026, 1, 1), date(2026, 12, 31)
        )
        commercial = build_monthly_sources(self.org, date(2026, 9, 1))
        cash = opening.opening_refund_source_snapshot(
            organization=self.org, refund=refund
        )
        original_cash = opening.opening_settlement_source_snapshot(
            organization=self.org, settlement=receipt
        )
        balances = ledger.report_balances(organization=self.org)
        self.assertEqual(
            (
                balances["AP"],
                balances["BANK"],
                balances["REVENUE"],
                balances["EXPENSE"],
            ),
            (-220, -80, 0, 0),
        )
        fingerprints = []
        for content in (
            b"synthetic damaged refund PDF one",
            b"synthetic damaged refund PDF two",
        ):
            self.tamper(TaxEvidence, report.evidence_id, "content", content)
            damaged = refund_review.opening_refund_review_context(
                organization=self.org, refund=refund
            )
            self.assertEqual(damaged["status"], "held")
            self.assertTrue(damaged["blockers"])
            self.assertEqual(
                damaged["source_fingerprint"], accepted["source_fingerprint"]
            )
            fingerprints.append(damaged["fingerprint"])
            self.assertNotEqual(damaged["fingerprint"], accepted["fingerprint"])
            self.assertTrue(
                package_status(organization=self.org, package=package)["stale"]
            )
            damaged_annual = workflow.annual_tax_snapshot(
                self.org, date(2026, 1, 1), date(2026, 12, 31)
            )
            self.assertNotEqual(
                damaged_annual["opening_refund_reviews"],
                annual["opening_refund_reviews"],
            )
            self.assertEqual(
                damaged_annual["opening_refund_reviews"]["inventory"][0]["status"],
                "held",
            )
            self.assertEqual(
                build_monthly_sources(self.org, date(2026, 9, 1)), commercial
            )
            self.assertEqual(
                opening.opening_refund_source_snapshot(
                    organization=self.org, refund=refund
                ),
                cash,
            )
            self.assertEqual(
                opening.opening_settlement_source_snapshot(
                    organization=self.org, settlement=receipt
                ),
                original_cash,
            )
            self.assertEqual(ledger.report_balances(organization=self.org), balances)
        self.assertNotEqual(*fingerprints)
        self.tamper(TaxEvidence, report.evidence_id, "content", pdf)
        self.assertEqual(
            refund_review.opening_refund_review_context(
                organization=self.org, refund=refund
            )["fingerprint"],
            accepted["fingerprint"],
        )
        self.assertEqual(
            package_status(organization=self.org, package=package)["status"], "approved"
        )

        # A later genuine refund supplies a different source for the damaged
        # aliases while retaining the original month's protected package.
        other_bank = self.movement(
            "20", day=date(2026, 10, 5), reference=f"SYN-LATER-AP-REFUND-{uuid4()}"
        )
        preview = opening.opening_balance_action_context(
            organization=self.org,
            action="refund",
            settlement=receipt,
            transaction=other_bank,
            amount="20",
        )
        other = opening.refund_opening_settlement(
            organization=self.org,
            actor=self.actor,
            settlement=receipt,
            transaction=other_bank,
            amount="20",
            reason="Synthetic separate later supplier principal refund.",
            evidence="Synthetic actual October incoming bank cash.",
            request_key=str(uuid4()),
            expected_fingerprint=preview["fingerprint"],
            confirmed=True,
        )
        balances = ledger.report_balances(organization=self.org)
        self.assertEqual(
            (
                balances["AP"],
                balances["BANK"],
                balances["REVENUE"],
                balances["EXPENSE"],
            ),
            (-240, -60, 0, 0),
        )
        self.assertEqual(
            package_status(organization=self.org, package=package)["status"], "approved"
        )
        original_snapshot, original_payload = (
            deepcopy(authority.source_snapshot),
            deepcopy(authority.request_payload),
        )
        moved_snapshot, moved_payload = (
            deepcopy(original_snapshot),
            deepcopy(original_payload),
        )
        moved_snapshot["id"], moved_payload["refund_id"] = other.pk, other.pk
        saved_audits = list(
            AuditEvent.objects.filter(organization=self.org).order_by("pk").values()
        )
        self.tamper(OpeningRefundTaxReview, authority.pk, "refund_id", other.pk)
        self.tamper(
            OpeningRefundTaxReview,
            authority.pk,
            "source_snapshot",
            json.dumps(moved_snapshot),
        )
        self.tamper(
            OpeningRefundTaxReview,
            authority.pk,
            "request_payload",
            json.dumps(moved_payload),
        )
        damaged = refund_review.opening_refund_review_context(
            organization=self.org, refund=refund
        )
        self.assertEqual(damaged["status"], "held")
        self.assertTrue(damaged["blockers"])
        self.assertNotEqual(damaged["fingerprint"], accepted["fingerprint"])
        self.assertTrue(package_status(organization=self.org, package=package)["stale"])
        moved_payload["statement"] = (
            "Synthetic second distinct repair state; original audit anchor still points to the first refund."
        )
        self.tamper(
            OpeningRefundTaxReview,
            authority.pk,
            "request_payload",
            json.dumps(moved_payload),
        )
        changed = refund_review.opening_refund_review_context(
            organization=self.org, refund=refund
        )
        self.assertEqual(changed["status"], "held")
        self.assertNotEqual(changed["fingerprint"], damaged["fingerprint"])
        self.assertEqual(
            list(
                AuditEvent.objects.filter(organization=self.org).order_by("pk").values()
            ),
            saved_audits,
        )
        self.assertEqual(
            opening.opening_refund_source_snapshot(
                organization=self.org, refund=refund
            ),
            cash,
        )
        self.assertEqual(
            opening.opening_settlement_source_snapshot(
                organization=self.org, settlement=receipt
            ),
            original_cash,
        )
        self.assertEqual(ledger.report_balances(organization=self.org), balances)
        self.tamper(OpeningRefundTaxReview, authority.pk, "refund_id", refund.pk)
        self.tamper(
            OpeningRefundTaxReview,
            authority.pk,
            "source_snapshot",
            json.dumps(original_snapshot),
        )
        self.tamper(
            OpeningRefundTaxReview,
            authority.pk,
            "request_payload",
            json.dumps(original_payload),
        )
        self.assertEqual(
            refund_review.opening_refund_review_context(
                organization=self.org, refund=refund
            )["fingerprint"],
            accepted["fingerprint"],
        )
        self.assertEqual(
            package_status(organization=self.org, package=package)["status"], "approved"
        )
        self.assertFalse(TaxObligation.objects.exists())
