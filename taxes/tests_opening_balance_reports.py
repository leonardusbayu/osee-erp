"""Actual opening cash remains separate from professional monthly turnover."""

import calendar
import csv
import io
import json
from copy import deepcopy
from datetime import date

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from finance import opening_balances as opening
from finance import services as ledger
from finance.models import (
    BankPosting,
    BankTransaction,
    Bill,
    Invoice,
    Journal,
    JournalLine,
    OpeningBalanceItem,
    OpeningBalanceSettlement,
)
from finance.test_statement_fixtures import reviewed_statement
from finance.tests_tax_money import TaxMoneyFixtures

from . import tests_monthly_packages as packages
from . import workflow
from .models import MonthlyTaxSourcePackage, TaxObligation
from .monthly_source_workflow import (
    package_status,
    prepare_monthly_package,
    revise_monthly_review,
)
from .monthly_sources import build_monthly_sources
from .opening_balance_review import record_opening_settlement_review
from .opening_balance_review_dependencies import (
    opening_settlement_review_correction_blockers,
)
from .tests_opening_balance_review import OpeningReviewFixtures


class OpeningBalanceReportTests(OpeningReviewFixtures, TestCase):
    profile = packages.MonthlySourcePackageTests.profile
    decision = packages.MonthlySourcePackageTests.decision
    approve = packages.MonthlySourcePackageTests.approve

    def setUp(self):
        super().setUp()
        self.owner = self.finance = self.actor

    def package(self, period=date(2026, 9, 1)):
        return prepare_monthly_package(
            organization=self.org, actor=self.actor, period=period
        )

    def test_opening_principal_is_no_income_and_only_actual_ar_cash_is_unclassified_candidate(
        self,
    ):
        ar = self.item("300", kind="receivable")
        ap = self.item("200", kind="payable")
        receipt = self.cash(ar, amount="120")
        payment = self.cash(ap, amount="50")
        source = build_monthly_sources(self.org, date(2026, 9, 1))
        self.assertEqual(source["book_revenue"], "0.00")
        self.assertIsNone(source["taxable_turnover"])
        self.assertEqual(len(source["rows"]), 1)
        row = source["rows"][0]
        self.assertEqual(
            (row["source_kind"], row["source_id"], row["source_item_id"], row["role"]),
            ("opening-receipt", receipt.pk, ar.pk, "candidate"),
        )
        self.assertEqual(
            (row["amount"], row["cash_amount"], row["gross_amount"], row["invoice_id"]),
            ("120.00", "120.00", None, None),
        )
        self.assertFalse(row["proposed_inclusion"])
        self.assertEqual(row["party_id"], self.party.pk)
        self.assertEqual(
            row["source_proof"],
            opening.opening_settlement_source_snapshot(
                organization=self.org, settlement=receipt
            ),
        )
        self.assertEqual(len(source["inventory"]["opening_balances"]), 4)
        self.assertFalse(
            any(
                row.get("source_id") == payment.pk
                and row["source_kind"] != "opening-receipt"
                for row in source["rows"]
            )
        )
        balances = ledger.report_balances(organization=self.org)
        self.assertEqual(
            (
                balances["AR"],
                balances["AP"],
                balances["BANK"],
                balances["REVENUE"],
                balances["EXPENSE"],
            ),
            (180, -150, 70, 0, 0),
        )
        self.assertFalse(Invoice.objects.exists())
        self.assertFalse(Bill.objects.exists())
        self.assertFalse(TaxObligation.objects.exists())

    def test_original_review_head_changes_package_dependency_not_commercial_source_hash(
        self,
    ):
        settlement = self.cash(kind="receivable")
        first = self.package()
        original_profile = deepcopy(first.profile_snapshot)
        self.zero_review(settlement)
        second = self.package()
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(first.source_hash, second.source_hash)
        self.assertNotEqual(first.package_hash, second.package_hash)
        self.assertTrue(package_status(organization=self.org, package=first)["stale"])
        first.refresh_from_db()
        self.assertEqual(first.profile_snapshot, original_profile)

    def test_received_monthly_report_cannot_waive_unreviewed_original_cash(self):
        self.profile()
        self.cash(kind="receivable")
        package = self.package()
        self.assertTrue(
            any(
                gate["code"] == "opening_balance_review"
                for gate in package.profile_snapshot["gates"]
            )
        )
        with self.assertRaises(ValidationError):
            self.approve(package)
        self.assertFalse(TaxObligation.objects.exists())

    def test_professional_monthly_receipt_inclusion_is_distinct_from_book_revenue(self):
        self.profile()
        settlement = self.cash(kind="receivable")
        self.zero_review(settlement)
        package = self.package()
        source = package.snapshot["rows"][0]
        decision = self.decision(
            package,
            rows=[
                {
                    "row_id": source["row_id"],
                    "decision": "include",
                    "reason": "Synthetic received report establishes this receipt is included this period.",
                }
            ],
        )
        approval = self.approve(package, decision)
        self.assertEqual(approval.result["reviewed_bridge_amount"], "100.00")
        self.assertEqual(
            approval.result["allocations"],
            [
                {
                    "kind": "opening_cash",
                    "invoice_id": None,
                    "source_row_ids": [f"opening-receipt:{settlement.pk}"],
                    "amount": "100.00",
                }
            ],
        )
        self.assertIsNone(approval.result["taxable_turnover"])
        self.assertIsNone(approval.result["final_tax"])
        self.assertEqual(ledger.report_balances(organization=self.org)["REVENUE"], 0)
        self.assertFalse(Invoice.objects.exists())
        self.assertFalse(TaxObligation.objects.exists())
        page = self.client.get(reverse("taxes:monthly_source", args=[package.pk]))
        self.assertContains(page, "Penerimaan piutang lama")
        self.assertContains(
            page, "Penerimaan piutang lama yang dimasukkan sesuai laporan"
        )
        self.assertContains(
            page, reverse("opening_balance_detail", args=[settlement.item_id])
        )

    def test_professional_monthly_exclusion_is_explicit_not_inferred_from_legacy_document_date(
        self,
    ):
        self.profile()
        settlement = self.cash(kind="receivable")
        self.zero_review(settlement)
        package = self.package()
        source = package.snapshot["rows"][0]
        self.assertEqual(source["source_date"], "2026-08-20")
        self.assertFalse(source["proposed_inclusion"])
        approval = self.approve(
            package,
            self.decision(
                package,
                rows=[
                    {
                        "row_id": source["row_id"],
                        "decision": "exclude",
                        "reason": "Synthetic report proves this income was already recognized in a prior tax period.",
                    }
                ],
            ),
        )
        self.assertEqual(approval.result["reviewed_bridge_amount"], "0.00")
        self.assertEqual(approval.result["allocations"], [])
        self.assertFalse(TaxObligation.objects.exists())

    def test_professional_timing_adjustment_retains_exact_original_receipt_reference(
        self,
    ):
        self.profile()
        settlement = self.cash(kind="receivable")
        self.zero_review(settlement)
        package = self.package()
        source = package.snapshot["rows"][0]
        decision = self.decision(
            package,
            adjustments=[
                {
                    "kind": "timing",
                    "amount": "100",
                    "reason": "Synthetic report establishes period timing for the actual original receipt.",
                    "evidence_id": self.evidence().pk,
                    "source_row_ids": [source["row_id"]],
                }
            ],
        )
        approval = self.approve(package, decision)
        self.assertEqual(approval.result["reviewed_bridge_amount"], "100.00")
        self.assertEqual(
            approval.result["allocations"][0]["source_row_ids"],
            [f"opening-receipt:{settlement.pk}"],
        )
        self.assertIsNone(approval.result["allocations"][0]["invoice_id"])
        self.assertFalse(Invoice.objects.exists())

    def test_paid_principal_and_old_cash_remain_tax_dependency_without_new_month_candidate(
        self,
    ):
        item = self.item("100", kind="receivable")
        settlement = self.cash(item, amount="100")
        self.assertEqual(
            opening.opening_balance_position(organization=self.org, item=item)[
                "remaining"
            ],
            0,
        )
        source = build_monthly_sources(self.org, date(2026, 10, 1))
        self.assertFalse(source["rows"])
        self.assertEqual(len(source["inventory"]["opening_balances"]), 2)
        package = self.package(date(2026, 10, 1))
        dependency = package.profile_snapshot["opening_cash_reviews"]
        self.assertEqual(dependency["inventory"][0]["settlement_id"], settlement.pk)
        self.assertEqual(dependency["inventory"][0]["status"], "pending")
        from .services import monthly_tax_context

        context = monthly_tax_context(self.org, date(2026, 10, 1))
        self.assertEqual(
            context["opening_balance_tax_candidates"][0]["settlement_id"], settlement.pk
        )

    def test_annual_source_dependency_tracks_original_principal_without_invented_turnover(
        self,
    ):
        start, end = date(2026, 1, 1), date(2026, 12, 31)
        empty = workflow.annual_tax_snapshot(self.org, start, end)
        self.assertNotIn("opening_cash_reviews", empty)
        self.assertNotIn("opening_principal_sources", empty)
        item = self.item()
        principal = workflow.annual_tax_snapshot(self.org, start, end)
        self.assertNotIn("opening_cash_reviews", principal)
        self.assertEqual(
            principal["opening_principal_sources"]["inventory"][0]["item_id"], item.pk
        )
        settlement = self.cash(item)
        pending = workflow.annual_tax_snapshot(self.org, start, end)
        self.zero_review(settlement)
        accepted = workflow.annual_tax_snapshot(self.org, start, end)
        self.assertNotEqual(
            accepted["opening_cash_reviews"], pending["opening_cash_reviews"]
        )
        self.assertEqual(
            accepted["opening_principal_sources"], pending["opening_principal_sources"]
        )
        self.assertEqual(
            accepted["opening_cash_reviews"]["inventory"][0]["status"], "reviewed"
        )

    def test_original_opening_item_damage_is_hard_issue_in_monthly_and_annual_snapshots(
        self,
    ):
        item = self.item()
        line = JournalLine.objects.get(journal_id=item.journal_id, account="AP")
        self.tamper(JournalLine, line.pk, "account", "EXPENSE")
        source = build_monthly_sources(self.org, date(2026, 9, 1))
        self.assertTrue(
            any(
                issue["code"] == "opening_balance_integrity" and issue["hard_blocker"]
                for issue in source["issues"]
            )
        )
        annual = workflow.annual_tax_snapshot(
            self.org, date(2026, 1, 1), date(2026, 12, 31)
        )
        self.assertFalse(annual["opening_principal_sources"]["inventory"][0]["intact"])
        self.assertTrue(annual["opening_principal_sources"]["blockers"])
        self.tamper(JournalLine, line.pk, "account", "PREPAID")
        changed = build_monthly_sources(self.org, date(2026, 9, 1))
        self.assertNotEqual(changed["source_hash"], source["source_hash"])

    def test_used_cash_review_requires_explicit_monthly_revision_and_old_package_stays_stored(
        self,
    ):
        self.profile()
        settlement = self.cash(kind="receivable")
        first = self.zero_review(settlement)
        package = self.package()
        self.approve(package)
        saved = deepcopy(package.profile_snapshot)
        self.assertTrue(
            opening_settlement_review_correction_blockers(
                organization=self.org, settlement=settlement
            )
        )
        with self.assertRaises(ValidationError):
            record_opening_settlement_review(**self.review_data(settlement))
        revised = revise_monthly_review(
            organization=self.org,
            actor=self.actor,
            package=package,
            reason="Synthetic explicit monthly review correction retains original package and cash.",
            expected_fingerprint=package.package_hash,
            expected_version=package.revision,
        )
        self.assertFalse(
            opening_settlement_review_correction_blockers(
                organization=self.org, settlement=settlement
            )
        )
        second = record_opening_settlement_review(**self.review_data(settlement))
        self.assertEqual((second.version, second.previous_id), (2, first.pk))
        self.assertTrue(package_status(organization=self.org, package=revised)["stale"])
        package.refresh_from_db()
        self.assertEqual(package.profile_snapshot, saved)

    def test_corrupted_approved_dependency_or_malformed_history_cannot_release_used_review(
        self,
    ):
        self.profile()
        settlement = self.cash()
        self.zero_review(settlement)
        package = self.package()
        self.approve(package)
        damaged = deepcopy(package.profile_snapshot)
        damaged["opening_cash_reviews"]["inventory"] = []
        self.tamper(
            MonthlyTaxSourcePackage, package.pk, "profile_snapshot", json.dumps(damaged)
        )
        with self.assertRaises(ValidationError):
            record_opening_settlement_review(**self.review_data(settlement))
        damaged["opening_cash_reviews"] = deepcopy(
            package.profile_snapshot["opening_cash_reviews"]
        )
        damaged["fiscal_history"] = [{"package_id": []}]
        self.tamper(
            MonthlyTaxSourcePackage, package.pk, "profile_snapshot", json.dumps(damaged)
        )
        self.tamper(
            MonthlyTaxSourcePackage,
            package.pk,
            "package_hash",
            workflow.digest(
                {
                    "snapshot": package.snapshot,
                    "profile": damaged,
                    "legal": package.legal_sources,
                }
            ),
        )
        with self.assertRaises(ValidationError):
            opening_settlement_review_correction_blockers(
                organization=self.org, settlement=settlement
            )

    def test_original_bank_date_anchor_keeps_tampered_future_cash_in_monthly_held_inventory(
        self,
    ):
        settlement = self.cash()
        self.tamper(OpeningBalanceSettlement, settlement.pk, "date", "2027-01-01")
        source = build_monthly_sources(self.org, date(2026, 9, 1))
        cash = next(
            row
            for row in source["inventory"]["opening_balances"]
            if row.get("settlement_id") == settlement.pk
        )
        self.assertFalse(cash["intact"])
        self.assertTrue(
            any(
                row["code"] == "opening_balance_integrity" and row["hard_blocker"]
                for row in source["issues"]
            )
        )
        from .services import monthly_tax_context

        context = monthly_tax_context(self.org, date(2026, 9, 1))
        self.assertEqual(
            context["opening_balance_tax_candidates"][0]["settlement_id"], settlement.pk
        )

    def test_all_live_no_cash_principal_dates_moved_future_cannot_erase_original_source(
        self,
    ):
        item = self.item()
        self.tamper(OpeningBalanceItem, item.pk, "date", "2027-01-01")
        self.tamper(Journal, item.journal_id, "date", "2027-01-01")
        source = build_monthly_sources(self.org, date(2026, 9, 1))
        original = next(
            row
            for row in source["inventory"]["opening_balances"]
            if row.get("item_id") == item.pk
        )
        self.assertEqual(original["kind"], "opening_principal")
        self.assertTrue(
            any(
                row["code"] == "opening_balance_integrity" and row["hard_blocker"]
                for row in source["issues"]
            )
        )
        annual = workflow.annual_tax_snapshot(
            self.org, date(2026, 1, 1), date(2026, 12, 31)
        )
        self.assertEqual(
            annual["opening_principal_sources"]["inventory"][0]["item_id"], item.pk
        )
        self.assertTrue(annual["opening_principal_sources"]["blockers"])
        facts = opening.opening_balance_close_facts(
            organization=self.org, cutoff=date(2026, 9, 30)
        )
        self.assertFalse(facts["integrity"])
        self.assertIsNone(facts["payable"])
        with self.assertRaises(ValidationError) as failure:
            ledger.close_month(
                organization=self.org, actor=self.actor, year=2026, month=9
            )
        self.assertIn(
            "Rincian saldo awal piutang/utang tidak dapat dinilai",
            str(failure.exception),
        )

    def test_all_live_cash_and_principal_dates_moved_future_keep_original_review_and_report_holds(
        self,
    ):
        settlement = self.cash(kind="receivable")
        item, posting = settlement.item, settlement.bank_posting
        for model, pk in (
            (OpeningBalanceItem, item.pk),
            (Journal, item.journal_id),
            (OpeningBalanceSettlement, settlement.pk),
            (Journal, settlement.journal_id),
            (BankPosting, posting.pk),
            (BankTransaction, posting.transaction_id),
        ):
            self.tamper(model, pk, "date", "2027-01-01")
        from .opening_balance_review import opening_balance_tax_gates

        gates = opening_balance_tax_gates(
            organization=self.org, cutoff=date(2026, 9, 30)
        )
        self.assertEqual(
            [row["settlement_id"] for row in gates["inventory"]], [settlement.pk]
        )
        self.assertEqual(gates["inventory"][0]["status"], "held")
        self.assertTrue(gates["blockers"])
        source = build_monthly_sources(self.org, date(2026, 9, 1))
        self.assertFalse(source["rows"])
        self.assertEqual(len(source["inventory"]["opening_balances"]), 2)
        self.assertTrue(
            any(
                row["code"] == "opening_balance_integrity" and row["hard_blocker"]
                for row in source["issues"]
            )
        )
        annual = workflow.annual_tax_snapshot(
            self.org, date(2026, 1, 1), date(2026, 12, 31)
        )
        self.assertEqual(
            annual["opening_cash_reviews"]["inventory"][0]["settlement_id"],
            settlement.pk,
        )
        self.assertTrue(annual["opening_principal_sources"]["blockers"])
        facts = opening.opening_balance_close_facts(
            organization=self.org, cutoff=date(2026, 9, 30)
        )
        self.assertFalse(facts["integrity"])
        self.assertIsNone(facts["receivable"])
        with self.assertRaises(ValidationError) as failure:
            ledger.close_month(
                organization=self.org, actor=self.actor, year=2026, month=9
            )
        self.assertIn(
            "Rincian saldo awal piutang/utang tidak dapat dinilai",
            str(failure.exception),
        )

    def test_genuine_sources_after_cutoff_are_excluded_without_assuming_empty_erp_is_nihil(
        self,
    ):
        empty = build_monthly_sources(self.org, date(2026, 9, 1))
        self.cutoff = date(2026, 10, 1)
        settlement = self.cash(kind="receivable", day=date(2026, 10, 2))
        past = build_monthly_sources(self.org, date(2026, 9, 1))
        self.assertEqual(past["source_hash"], empty["source_hash"])
        self.assertNotIn("opening_balances", past["inventory"])
        from .opening_balance_review import opening_balance_tax_gates

        self.assertEqual(
            opening_balance_tax_gates(organization=self.org, cutoff=date(2026, 9, 30))[
                "inventory"
            ],
            [],
        )
        annual = workflow.annual_tax_snapshot(
            self.org, date(2026, 1, 1), date(2026, 9, 30)
        )
        self.assertNotIn("opening_cash_reviews", annual)
        self.assertNotIn("opening_principal_sources", annual)
        self.assertEqual(self.context(settlement)["status"], "pending")
        self.assertIsNone(past["taxable_turnover"])

    def test_malformed_original_receipt_dates_remain_in_scope_as_holds(self):
        settlement = self.cash()
        item, posting = settlement.item, settlement.bank_posting
        for model, pk in (
            (OpeningBalanceItem, item.pk),
            (Journal, item.journal_id),
            (OpeningBalanceSettlement, settlement.pk),
            (Journal, settlement.journal_id),
            (BankPosting, posting.pk),
            (BankTransaction, posting.transaction_id),
        ):
            self.tamper(model, pk, "date", "2027-01-01")
        for row in (item, settlement):
            request = deepcopy(row.request_payload)
            request["date"] = "2027-01-01"
            self.tamper(type(row), row.pk, "request_payload", json.dumps(request))
            saved = deepcopy(row.source_snapshot)
            saved["date"] = "not-a-calendar-date"
            saved["request_payload"]["date"] = "2027-01-01"
            saved["journal"]["date"] = "2027-01-01"
            if isinstance(row, OpeningBalanceSettlement):
                saved["bank"]["date"] = "2027-01-01"
                saved["bank_posting"]["date"] = "2027-01-01"
            self.tamper(type(row), row.pk, "source_snapshot", json.dumps(saved))
        source = build_monthly_sources(self.org, date(2026, 9, 1))
        self.assertEqual(len(source["inventory"]["opening_balances"]), 2)
        self.assertTrue(
            any(
                row["code"] == "opening_balance_integrity" and row["hard_blocker"]
                for row in source["issues"]
            )
        )
        from .opening_balance_review import opening_balance_tax_gates

        gates = opening_balance_tax_gates(
            organization=self.org, cutoff=date(2026, 9, 30)
        )
        self.assertEqual(
            [row["settlement_id"] for row in gates["inventory"]], [settlement.pk]
        )
        self.assertTrue(gates["blockers"])

    def test_csv_unknown_gross_base_rate_tax_are_blank_and_tasks_link_exact_cash_event(
        self,
    ):
        settlement = self.cash()
        response = self.client.get(reverse("taxes:export"), {"period": "2026-09"})
        self.assertEqual(response.status_code, 200)
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
        candidate = next(
            row
            for row in rows
            if row and row[0] == "opening_balance_cash_review_candidate"
        )
        self.assertEqual(candidate[4:8], ["", "", "", ""])
        self.assertEqual(candidate[11], f"opening-settlement:{settlement.pk}")
        link = reverse("opening_balance_tax:review", args=[settlement.pk])
        self.assertContains(
            self.client.get(reverse("taxes:index"), {"period": "2026-09"}), link
        )
        self.assertContains(
            self.client.get(reverse("taxes:annual"), {"year": "2026"}), link
        )

    def test_approved_and_filed_annual_package_preserves_original_cash_review_dependency(
        self,
    ):
        self.cutoff, self.day = date(2025, 9, 1), date(2025, 9, 10)
        settlement = self.cash()
        self.zero_review(settlement)
        TaxMoneyFixtures.profile(self)
        for month in range(1, 13):
            start, end = (
                date(2025, month, 1),
                date(2025, month, calendar.monthrange(2025, month)[1]),
            )
            reviewed_statement(
                self,
                organization=self.org,
                actor=self.actor,
                account=self.account,
                start=start,
                end=end,
                opening="0" if month <= 9 else "-100",
                closing="0" if month < 9 else "-100",
                total_out="100" if month == 9 else "0",
                count=1 if month == 9 else 0,
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
            reconciliation_note="Synthetic received reconciliation of no book income and original principal cash.",
        )
        workflow.approve_annual_workpaper(
            organization=self.org,
            actor=self.actor,
            workpaper=workpaper,
            external_review=self.external(workpaper, {"action": "approve_annual"}),
        )
        blockers = opening_settlement_review_correction_blockers(
            organization=self.org, settlement=settlement
        )
        self.assertTrue(any("SPT tahunan 2025" in message for message in blockers))
        workflow.verify_annual_filing(
            organization=self.org,
            actor=self.actor,
            workpaper=workpaper,
            evidence=self.evidence(),
            reference="SYNTHETIC-BPE-2025",
            taxpayer_reference="1234567890123456",
            year=2025,
            return_version=0,
            reported_tax=workpaper.result["income_tax"],
            reported_balance_due=workpaper.result["balance_due"],
            matches_confirmed=True,
        )
        with self.assertRaises(ValidationError):
            record_opening_settlement_review(**self.review_data(settlement))
        workpaper.refresh_from_db()
        self.assertEqual(
            workpaper.inputs["tax_snapshot"]["opening_cash_reviews"]["inventory"][0][
                "settlement_id"
            ],
            settlement.pk,
        )
