"""Dated principal changes use public Finance and fiscal source interfaces."""

import calendar
from copy import deepcopy
from datetime import date
from decimal import Decimal
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import TestCase

from finance import opening_balances as opening
from finance import services as ledger
from finance.models import Journal, JournalLine
from finance.test_statement_fixtures import reviewed_statement
from finance.tests_tax_money import TaxMoneyFixtures

from . import tests_monthly_packages as package_fixtures
from . import workflow
from .monthly_source_workflow import (
    approve_monthly_package,
    generate_monthly_obligation,
    monthly_decision,
    package_status,
    prepare_monthly_package,
    revise_monthly_review,
)
from .monthly_sources import build_monthly_sources
from .opening_balance_review_dependencies import (
    opening_balance_financial_dependency,
    opening_principal_correction_blockers,
)
from .tests_opening_balance_review import OpeningReviewFixtures


class OpeningPrincipalCorrectionTests(OpeningReviewFixtures, TestCase):
    def correct(
        self, item, *, day=date(2026, 10, 2), direction="decrease", amount="80"
    ):
        values = {"item": item, "date": day, "direction": direction, "amount": amount}
        context = opening.opening_balance_action_context(
            organization=self.org, action="correct", **values
        )
        return opening.correct_opening_balance(
            organization=self.org,
            actor=self.actor,
            **values,
            reason="Synthetic received reconciliation corrects outstanding principal only.",
            evidence="Synthetic original source and Owner reconciliation attachment.",
            request_key=str(uuid4()),
            expected_fingerprint=context["fingerprint"],
            confirmed=True,
        )

    def test_later_correction_preserves_earlier_sources_and_is_no_income_or_cash(self):
        item = self.item("300", kind="receivable")
        original = opening.opening_balance_source_snapshot(
            organization=self.org, item=item
        )
        september = build_monthly_sources(self.org, date(2026, 9, 1))
        annual = workflow.annual_tax_snapshot(
            self.org, date(2026, 1, 1), date(2026, 12, 31)
        )

        correction = self.correct(item)

        self.assertEqual(
            opening.opening_balance_source_snapshot(organization=self.org, item=item),
            original,
        )
        self.assertEqual(build_monthly_sources(self.org, date(2026, 9, 1)), september)
        october = build_monthly_sources(self.org, date(2026, 10, 1))
        rows = october["inventory"]["opening_balances"]
        changed = next(
            row for row in rows if row["kind"] == "opening_principal_correction"
        )
        self.assertEqual(
            (
                changed["correction_id"],
                changed["item_id"],
                changed["date"],
                changed["direction"],
                changed["amount"],
                changed["income_effect"],
                changed["cash_effect"],
            ),
            (correction.pk, item.pk, "2026-10-02", "decrease", "80.00", "0.00", "0.00"),
        )
        self.assertEqual(
            changed["source"],
            opening.opening_correction_source_snapshot(
                organization=self.org, correction=correction
            ),
        )
        self.assertEqual(
            (october["rows"], october["book_revenue"], october["taxable_turnover"]),
            ([], "0.00", None),
        )
        self.assertNotEqual(
            workflow.annual_tax_snapshot(
                self.org, date(2026, 1, 1), date(2026, 12, 31)
            ),
            annual,
        )
        self.assertEqual(
            opening.opening_balance_position(
                organization=self.org, item=item, as_of=date(2026, 9, 30)
            )["remaining"],
            300,
        )
        self.assertEqual(
            opening.opening_balance_position(
                organization=self.org, item=item, as_of=date(2026, 10, 2)
            )["remaining"],
            220,
        )
        balances = ledger.report_balances(organization=self.org)
        self.assertEqual(
            (
                balances["AR"],
                balances["BANK"],
                balances["REVENUE"],
                balances["EXPENSE"],
            ),
            (220, 0, 0, 0),
        )
        final = workflow.annual_tax_snapshot(
            self.org, date(2026, 1, 1), date(2026, 12, 31)
        )
        self.assertEqual((final["obligations"], final["verifications"]), ([], []))
        self.assertNotIn("opening_cash_reviews", final)

    def test_approved_principal_only_monthly_requires_explicit_revision_before_correction(
        self,
    ):
        package_fixtures.MonthlySourcePackageTests.profile(self)
        item = self.item("300", kind="receivable")
        period, cutoff, correction_date = (
            "2026-09",
            date(2026, 9, 30),
            date(2026, 9, 20),
        )
        original_source = opening.opening_balance_source_snapshot(
            organization=self.org, item=item
        )
        package = prepare_monthly_package(
            organization=self.org, actor=self.actor, period=period
        )
        self.assertEqual(package.snapshot["rows"], [])
        self.assertTrue(
            any(
                row["kind"] == "opening_principal" and row["item_id"] == item.pk
                for row in package.snapshot["inventory"]["opening_balances"]
            )
        )
        decision = monthly_decision(
            package,
            rows=[],
            completeness_confirmed=True,
            inventory_note="Synthetic all products and channels reviewed; only documented old principal exists.",
            nil_confirmed=True,
            rule_reference="Synthetic received principal-only period review",
        )
        report = self.external(package, decision)
        approval = approve_monthly_package(
            organization=self.org,
            actor=self.actor,
            package=package,
            decision=decision,
            external_review=report,
            expected_fingerprint=package.package_hash,
            expected_version=package.revision,
        )
        preserved = deepcopy((package.snapshot, approval.decision, approval.result))
        original_pdf = bytes(report.evidence.content)
        before = ledger.report_balances(organization=self.org)
        dependency = opening_balance_financial_dependency(
            organization=self.org, cutoff=cutoff
        )

        context = opening.opening_balance_action_context(
            organization=self.org,
            action="correct",
            item=item,
            date=correction_date,
            direction="decrease",
            amount="80",
        )
        self.assertTrue(context["blockers"])
        with self.assertRaises(ValidationError):
            self.correct(item, day=correction_date)
        self.assertEqual(ledger.report_balances(organization=self.org), before)
        self.assertEqual(
            opening.opening_balance_source_snapshot(organization=self.org, item=item),
            original_source,
        )
        self.assertEqual(
            opening_balance_financial_dependency(organization=self.org, cutoff=cutoff),
            dependency,
        )
        status = package_status(organization=self.org, package=package)
        self.assertEqual(status["status"], "approved")
        self.assertTrue(status["can_review_revision"])

        revision = revise_monthly_review(
            organization=self.org,
            actor=self.actor,
            package=package,
            reason="Synthetic documented principal discrepancy requires an explicit fresh professional review.",
            expected_fingerprint=package.package_hash,
            expected_version=package.revision,
        )
        self.assertEqual(
            (
                revision.previous_id,
                revision.revision,
                revision.snapshot,
                revision.source_hash,
            ),
            (package.pk, package.revision + 1, package.snapshot, package.source_hash),
        )
        self.assertEqual(ledger.report_balances(organization=self.org), before)
        self.assertEqual(
            opening_balance_financial_dependency(organization=self.org, cutoff=cutoff),
            dependency,
        )
        self.assertEqual(
            opening.opening_balance_action_context(
                organization=self.org,
                action="correct",
                item=item,
                date=correction_date,
                direction="decrease",
                amount="80",
            )["blockers"],
            [],
        )

        correction = self.correct(item, day=correction_date)
        replacement = prepare_monthly_package(
            organization=self.org, actor=self.actor, period=period
        )
        self.assertEqual(
            (replacement.previous_id, replacement.revision),
            (revision.pk, revision.revision + 1),
        )
        self.assertNotEqual(replacement.source_hash, package.source_hash)
        changed = next(
            row
            for row in replacement.snapshot["inventory"]["opening_balances"]
            if row["kind"] == "opening_principal_correction"
        )
        self.assertEqual(
            changed["source"],
            opening.opening_correction_source_snapshot(
                organization=self.org, correction=correction
            ),
        )
        self.assertEqual(
            (
                replacement.snapshot["rows"],
                replacement.snapshot["book_revenue"],
                replacement.snapshot["taxable_turnover"],
            ),
            ([], "0.00", None),
        )
        self.assertNotEqual(
            opening_balance_financial_dependency(organization=self.org, cutoff=cutoff),
            dependency,
        )
        self.assertEqual(
            opening.opening_balance_source_snapshot(organization=self.org, item=item),
            original_source,
        )
        balances = ledger.report_balances(organization=self.org)
        self.assertEqual(
            (
                balances["AR"],
                balances["BANK"],
                balances["REVENUE"],
                balances["EXPENSE"],
            ),
            (220, 0, 0, 0),
        )
        old_status = package_status(organization=self.org, package=package)
        self.assertTrue(old_status["superseded"])
        self.assertEqual(old_status["approval"].pk, approval.pk)
        package.refresh_from_db()
        approval.refresh_from_db()
        report.refresh_from_db()
        report.evidence.refresh_from_db()
        self.assertEqual(
            (package.snapshot, approval.decision, approval.result), preserved
        )
        self.assertEqual(approval.external_review_id, report.pk)
        self.assertEqual(bytes(report.evidence.content), original_pdf)

    def test_approved_filed_annual_principal_guard_and_later_correction_keep_original_report_current(
        self,
    ):
        self.cutoff = date(2025, 9, 1)
        item = self.item("300", kind="receivable")
        original_source = opening.opening_balance_source_snapshot(
            organization=self.org, item=item
        )
        TaxMoneyFixtures.profile(self)
        for month in range(1, 13):
            start = date(2025, month, 1)
            end = date(2025, month, calendar.monthrange(2025, month)[1])
            reviewed_statement(
                self,
                organization=self.org,
                actor=self.actor,
                account=self.account,
                start=start,
                end=end,
                opening="0",
                closing="0",
                total_in="0",
                total_out="0",
                count=0,
            )
            ledger.close_month(
                organization=self.org, actor=self.actor, year=2025, month=month
            )
        workpaper = workflow.prepare_annual_workpaper(
            organization=self.org,
            actor=self.actor,
            year=2025,
            positive_adjustments="0",
            negative_adjustments="0",
            loss_compensation="0",
            turnover="0",
            regime_code="corporate_22",
            instalments="0",
            support_evidence=self.evidence(),
            reconciliation_note="Synthetic received reconciliation verifies only old principal, without income or cash.",
        )
        self.assertEqual(
            workpaper.inputs["tax_snapshot"]["opening_principal_sources"]["inventory"][
                0
            ]["item_id"],
            item.pk,
        )
        report = self.external(workpaper, {"action": "approve_annual"})
        approval = workflow.approve_annual_workpaper(
            organization=self.org,
            actor=self.actor,
            workpaper=workpaper,
            external_review=report,
        )
        protected_date = date(2025, 9, 20)
        before = ledger.report_balances(organization=self.org)
        for filed in (False, True):
            if filed:
                filing = workflow.verify_annual_filing(
                    organization=self.org,
                    actor=self.actor,
                    workpaper=workpaper,
                    evidence=self.evidence(),
                    reference="SYNTHETIC-PRINCIPAL-BPE-2025",
                    taxpayer_reference="1234567890123456",
                    year=2025,
                    return_version=0,
                    reported_tax=workpaper.result["income_tax"],
                    reported_balance_due=workpaper.result["balance_due"],
                    matches_confirmed=True,
                )
            blockers = opening_principal_correction_blockers(
                organization=self.org, item=item, date=protected_date
            )
            # Assert the fiscal dependency itself, independently of closed-date
            # enforcement in the financial command for the same posting date.
            self.assertTrue(
                any(
                    "SPT tahunan 2025" in message and "pokok ini" in message
                    for message in blockers
                )
            )
            with self.assertRaises(ValidationError):
                self.correct(item, day=protected_date)
            self.assertEqual(ledger.report_balances(organization=self.org), before)
            self.assertEqual(
                opening.opening_balance_source_snapshot(
                    organization=self.org, item=item
                ),
                original_source,
            )
            self.assertTrue(
                workflow.annual_snapshot_status(self.org, workpaper)["snapshot_current"]
            )

        saved = deepcopy((workpaper.book_snapshot, workpaper.inputs, workpaper.result))
        saved_report_pdf, saved_filing_pdf = (
            bytes(report.evidence.content),
            bytes(filing.evidence.content),
        )
        past = workflow.annual_tax_snapshot(
            self.org, date(2025, 1, 1), date(2025, 12, 31), include_identity=True
        )
        later_date = date(2026, 10, 2)
        self.assertEqual(
            opening_principal_correction_blockers(
                organization=self.org, item=item, date=later_date
            ),
            [],
        )
        correction = self.correct(item, day=later_date)
        self.assertEqual(
            opening.opening_balance_position(
                organization=self.org, item=item, as_of=date(2025, 12, 31)
            )["remaining"],
            300,
        )
        self.assertEqual(
            opening.opening_balance_position(
                organization=self.org, item=item, as_of=later_date
            )["remaining"],
            220,
        )
        self.assertEqual(
            workflow.annual_tax_snapshot(
                self.org, date(2025, 1, 1), date(2025, 12, 31), include_identity=True
            ),
            past,
        )
        self.assertTrue(
            workflow.annual_snapshot_status(self.org, workpaper)["snapshot_current"]
        )
        current = workflow.annual_tax_snapshot(
            self.org, date(2026, 1, 1), date(2026, 12, 31)
        )
        self.assertTrue(
            any(
                row.get("correction_id") == correction.pk
                for row in current["opening_principal_sources"]["inventory"]
            )
        )
        self.assertEqual((current["obligations"], current["verifications"]), ([], []))
        self.assertNotIn("opening_cash_reviews", current)
        balances = ledger.report_balances(organization=self.org)
        self.assertEqual(
            (
                balances["AR"],
                balances["BANK"],
                balances["REVENUE"],
                balances["EXPENSE"],
            ),
            (220, 0, 0, 0),
        )
        workpaper.refresh_from_db()
        approval.refresh_from_db()
        filing.refresh_from_db()
        report.evidence.refresh_from_db()
        filing.evidence.refresh_from_db()
        self.assertEqual(
            (workpaper.book_snapshot, workpaper.inputs, workpaper.result), saved
        )
        self.assertEqual(
            (approval.workpaper_id, approval.external_review_id, filing.workpaper_id),
            (workpaper.pk, report.pk, workpaper.pk),
        )
        self.assertEqual(
            (bytes(report.evidence.content), bytes(filing.evidence.content)),
            (saved_report_pdf, saved_filing_pdf),
        )

    def test_continuing_obligation_with_stored_monthly_history_blocks_principal_change_and_revision(
        self,
    ):
        package_fixtures.MonthlySourcePackageTests.profile(self, "final")
        self.cutoff = date(2026, 2, 1)
        item = self.item("300", kind="receivable")
        original_source = opening.opening_balance_source_snapshot(
            organization=self.org, item=item
        )
        package_fixtures.MonthlySourcePackageTests.sale(
            self,
            "500000",
            suffix="PRINCIPAL-FEB",
            issued=date(2026, 2, 5),
            delivered=date(2026, 2, 10),
        )
        prior = prepare_monthly_package(
            organization=self.org, actor=self.actor, period="2026-02"
        )
        decision = package_fixtures.MonthlySourcePackageTests.decision(self, prior)
        prior_approval = approve_monthly_package(
            organization=self.org,
            actor=self.actor,
            package=prior,
            decision=decision,
            external_review=self.external(prior, decision),
            expected_fingerprint=prior.package_hash,
            expected_version=prior.revision,
        )
        package_fixtures.MonthlySourcePackageTests.sale(
            self,
            "200000",
            suffix="PRINCIPAL-MAR",
            issued=date(2026, 3, 5),
            delivered=date(2026, 3, 10),
        )
        later = prepare_monthly_package(
            organization=self.org, actor=self.actor, period="2026-03"
        )
        self.assertTrue(
            any(
                row["package_id"] == prior.pk
                and row["approval_id"] == prior_approval.pk
                for row in later.fiscal_history
            )
        )
        decision = package_fixtures.MonthlySourcePackageTests.decision(
            self, later, prior_history_confirmed=True
        )
        later_approval = approve_monthly_package(
            organization=self.org,
            actor=self.actor,
            package=later,
            decision=decision,
            external_review=self.external(later, decision),
            expected_fingerprint=later.package_hash,
            expected_version=later.revision,
        )
        obligation = generate_monthly_obligation(
            organization=self.org,
            actor=self.actor,
            package=later,
            expected_fingerprint=later.package_hash,
            expected_version=later.revision,
        )
        self.assertEqual(
            (obligation.source_package_id, obligation.status), (later.pk, "review")
        )
        self.assertEqual(
            (obligation.base, obligation.amount),
            (
                Decimal(later_approval.result["taxable_turnover"]),
                Decimal(later_approval.result["final_tax"]),
            ),
        )
        protected_date = date(2026, 2, 20)
        before = ledger.report_balances(organization=self.org)
        source = opening_balance_financial_dependency(
            organization=self.org, cutoff=date(2026, 3, 31)
        )
        saved = deepcopy((prior.snapshot, later.snapshot, later.fiscal_history))
        for approved in (False, True):
            if approved:
                obligation = workflow.approve_tax_obligation(
                    obligation,
                    self.actor,
                    external_review=self.external(
                        obligation, {"action": "approve_obligation"}
                    ),
                )
            blockers = opening_principal_correction_blockers(
                organization=self.org, item=item, date=protected_date
            )
            self.assertTrue(
                any("Kewajiban pajak 03/2026" in message for message in blockers)
            )
            with self.assertRaises(ValidationError):
                self.correct(item, day=protected_date)
            with self.assertRaises(ValidationError):
                revise_monthly_review(
                    organization=self.org,
                    actor=self.actor,
                    package=prior,
                    reason="Synthetic requested principal reconciliation retains the active dependent liability.",
                    expected_fingerprint=prior.package_hash,
                    expected_version=prior.revision,
                )
            self.assertEqual(ledger.report_balances(organization=self.org), before)
            self.assertEqual(
                opening_balance_financial_dependency(
                    organization=self.org, cutoff=date(2026, 3, 31)
                ),
                source,
            )
            self.assertEqual(
                opening.opening_balance_source_snapshot(
                    organization=self.org, item=item
                ),
                original_source,
            )
            self.assertFalse(
                package_status(organization=self.org, package=prior)["stale"]
            )
            self.assertFalse(
                package_status(organization=self.org, package=later)["stale"]
            )
        with self.assertRaises(ValidationError):
            workflow.withdraw_obligation(
                organization=self.org,
                actor=self.actor,
                obligation=obligation,
                reason="Synthetic approved liability cannot be withdrawn to bypass its original fiscal dependency.",
                expected_version=obligation.updated_at.isoformat(),
            )
        obligation.refresh_from_db()
        prior.refresh_from_db()
        later.refresh_from_db()
        self.assertEqual(
            (obligation.status, obligation.source_package_id), ("approved", later.pk)
        )
        self.assertEqual((prior.snapshot, later.snapshot, later.fiscal_history), saved)
        self.assertEqual(
            (before["AR"], before["BANK"], before["REVENUE"], before["EXPENSE"]),
            (700300, 0, -700000, 0),
        )
        self.assertFalse(
            any(
                row["source_kind"] == "opening-receipt"
                for row in later.snapshot["rows"]
            )
        )

    def test_orphan_correction_journal_holds_empty_fiscal_inventory_and_binds_repairs(
        self,
    ):
        cutoff, period = date(2026, 9, 30), date(2026, 9, 1)
        empty_dependency = opening_balance_financial_dependency(
            organization=self.org, cutoff=cutoff
        )
        empty_monthly = build_monthly_sources(self.org, period)
        journal = ledger.post_journal(
            organization=self.org,
            actor=self.actor,
            date=date(2026, 9, 10),
            source_key=f"opening:opening-correct:{uuid4()}",
            description="Synthetic orphan correction journal without a principal decision.",
            entries=[("AR", "10", "0"), ("EQUITY", "0", "10")],
        )

        dependency = opening_balance_financial_dependency(
            organization=self.org, cutoff=cutoff
        )
        self.assertTrue(dependency["blockers"])
        self.assertNotEqual(dependency["fingerprint"], empty_dependency["fingerprint"])
        monthly = build_monthly_sources(self.org, period)
        self.assertTrue(
            any(
                row["code"] == "opening_balance_integrity" and row["hard_blocker"]
                for row in monthly["issues"]
            )
        )
        self.assertNotEqual(monthly["source_hash"], empty_monthly["source_hash"])
        self.assertEqual(
            (monthly["rows"], monthly["book_revenue"], monthly["taxable_turnover"]),
            ([], "0.00", None),
        )

        # Controlled corruption/repair injection; assertions use only public
        # fiscal extraction, never Finance's private inventory implementation.
        line = JournalLine.objects.get(journal=journal, account="AR")
        for model, pk, field, value in (
            (
                Journal,
                journal.pk,
                "description",
                "Synthetic corrected attachment description, still orphaned.",
            ),
            (JournalLine, line.pk, "account", "AP"),
        ):
            previous_dependency, previous_monthly = dependency, monthly
            self.tamper(model, pk, field, value)
            dependency = opening_balance_financial_dependency(
                organization=self.org, cutoff=cutoff
            )
            monthly = build_monthly_sources(self.org, period)
            self.assertTrue(dependency["blockers"])
            self.assertNotEqual(
                dependency["fingerprint"], previous_dependency["fingerprint"]
            )
            self.assertNotEqual(monthly["source_hash"], previous_monthly["source_hash"])
            self.assertTrue(
                any(
                    row["code"] == "opening_balance_integrity" and row["hard_blocker"]
                    for row in monthly["issues"]
                )
            )
            self.assertEqual(
                (monthly["rows"], monthly["book_revenue"], monthly["taxable_turnover"]),
                ([], "0.00", None),
            )
        final = workflow.annual_tax_snapshot(
            self.org, date(2026, 1, 1), date(2026, 12, 31)
        )
        self.assertTrue(final["opening_principal_sources"]["blockers"])
        self.assertEqual((final["obligations"], final["verifications"]), ([], []))
        self.assertNotIn("opening_cash_reviews", final)
