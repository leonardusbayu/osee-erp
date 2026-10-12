"""Opening residuals must agree across existing reports and close consumers.

The ordinary invoice/Bill subledgers remain separate. Actual old-balance cash
must be counted once while missing fiscal review continues to hold closing.
"""

from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import Organization

from . import opening_balances as opening
from . import services as ledger
from . import tests_opening_balances as opening_fixtures
from .models import Journal, JournalLine, OpeningBalanceItem
from .test_statement_fixtures import reviewed_statement


@override_settings(MFA_REQUIRED=False)
class OpeningBalanceConsumerTests(opening_fixtures.OpeningBalanceFixtures, TestCase):
    AR_BLOCKER = "Saldo piutang tidak sama dengan subledger tagihan/alokasi."
    AP_BLOCKER = "Saldo utang tidak sama dengan subledger tagihan pembelian."

    def assert_subledgers_agree(self, cutoff):
        blockers = ledger._close_blockers(self.org, cutoff)
        self.assertNotIn(self.AR_BLOCKER, blockers)
        self.assertNotIn(self.AP_BLOCKER, blockers)
        return blockers

    def old_balances(self):
        return self.item(amount="300"), self.item(
            kind="payable", party=self.supplier, amount="200"
        )

    def actual_old_cash(self, receivable, payable):
        self.settle(
            receivable,
            self.movement("120", reference="SYN-CONSUMER-OLD-IN"),
            amount="120",
        )
        self.settle(
            payable, self.movement("-50", reference="SYN-CONSUMER-OLD-OUT"), amount="50"
        )

    def ordinary_documents(self):
        invoice = self.invoice(date=date(2026, 9, 5), service_date=date(2026, 10, 1))
        ledger.issue_invoice(organization=self.org, actor=self.actor, invoice=invoice)
        bill = ledger.create_bill(
            organization=self.org,
            actor=self.actor,
            supplier=self.supplier,
            amount="100",
            date=date(2026, 9, 5),
            service_date=date(2026, 9, 5),
            number="SYN-CONSUMER-CURRENT-BILL",
            supplier_reference="SYN-CONSUMER-CURRENT-SOURCE",
        )
        ledger.approve_bill(organization=self.org, actor=self.actor, bill=bill)
        return invoice, bill

    def report(self, period="2026-09"):
        self.client.force_login(self.actor)
        session = self.client.session
        session["organization_id"] = self.org.pk
        session.save()
        response = self.client.get(reverse("reports"), {"period": period})
        self.assertEqual(response.status_code, 200)
        return response, response.context["reports"]

    def test_no_cash_opening_residuals_do_not_create_ar_ap_close_discrepancy(self):
        self.old_balances()
        facts = opening.opening_balance_close_facts(
            organization=self.org, cutoff=date(2026, 9, 30)
        )
        self.assertEqual(
            (facts["receivable"], facts["payable"]), (Decimal(300), Decimal(200))
        )
        blockers = self.assert_subledgers_agree(date(2026, 9, 30))
        self.assertFalse(
            any(message.startswith("Kas saldo awal") for message in blockers)
        )
        balances = ledger.report_balances(
            organization=self.org, as_of=date(2026, 9, 30)
        )
        self.assertEqual(
            (
                balances["AR"],
                balances["AP"],
                balances["BANK"],
                balances["REVENUE"],
                balances["EXPENSE"],
            ),
            (Decimal(300), Decimal(-200), 0, 0, 0),
        )

    def test_close_combines_ordinary_subledgers_and_old_residual_once(self):
        receivable, payable = self.old_balances()
        invoice, bill = self.ordinary_documents()
        self.actual_old_cash(receivable, payable)
        for cutoff, ar, ap in (
            (date(2026, 9, 4), Decimal(300), Decimal(-200)),
            (
                date(2026, 9, 9),
                invoice.total + Decimal(300),
                -(bill.amount + Decimal(200)),
            ),
            (
                date(2026, 9, 30),
                invoice.total + Decimal(180),
                -(bill.amount + Decimal(150)),
            ),
        ):
            with self.subTest(cutoff=cutoff):
                self.assert_subledgers_agree(cutoff)
                balances = ledger.report_balances(organization=self.org, as_of=cutoff)
                self.assertEqual((balances["AR"], balances["AP"]), (ar, ap))
        self.assertEqual((invoice.paid_amount, bill.paid_amount), (0, 0))

    def test_actual_old_cash_changes_only_its_date_and_pending_tax_still_holds_close(
        self,
    ):
        ledger.record_bank_opening(
            organization=self.org,
            actor=self.actor,
            account=self.account,
            date=self.cutoff,
            amount="1000",
            reference="SYN-CONSUMER-BANK-OPEN",
            evidence="Synthetic independently confirmed starting bank balance.",
        )
        receivable, payable = self.old_balances()
        prior = ledger.report_balances(organization=self.org, as_of=date(2026, 9, 9))
        prior_facts = opening.opening_balance_close_facts(
            organization=self.org, cutoff=date(2026, 9, 9)
        )
        self.actual_old_cash(receivable, payable)
        self.assertEqual(
            ledger.report_balances(organization=self.org, as_of=date(2026, 9, 9)), prior
        )
        after_facts = opening.opening_balance_close_facts(
            organization=self.org, cutoff=date(2026, 9, 9)
        )
        self.assertEqual(
            (after_facts["receivable"], after_facts["payable"]),
            (prior_facts["receivable"], prior_facts["payable"]),
        )
        self.assertEqual(
            (prior["BANK"], prior["AR"], prior["AP"]),
            (Decimal(1000), Decimal(300), Decimal(-200)),
        )
        balances = ledger.report_balances(organization=self.org, as_of=self.day)
        self.assertEqual(
            (balances["BANK"], balances["AR"], balances["AP"]),
            (Decimal(1070), Decimal(180), Decimal(-150)),
        )
        reviewed_statement(
            self,
            organization=self.org,
            actor=self.actor,
            account=self.account,
            start=self.cutoff,
            end=date(2026, 9, 30),
            opening="1000",
            closing="1070",
            total_in="120",
            total_out="50",
            count=2,
        )
        blockers = self.assert_subledgers_agree(date(2026, 9, 30))
        self.assertTrue(
            any(message.startswith("Kas saldo awal") for message in blockers), blockers
        )
        self.assertFalse(
            any(
                "mutasi bank" in message.casefold()
                and "belum direkonsiliasi" in message
                for message in blockers
            )
        )
        with self.assertRaises(ValidationError):
            ledger.close_month(
                organization=self.org, actor=self.actor, year=2026, month=9
            )

    def test_corrupt_opening_residual_is_unknown_and_close_stays_held(self):
        receivable, _ = self.old_balances()
        self.corrupt(OpeningBalanceItem, receivable.pk, "amount", Decimal(299))
        position = self.position(receivable)
        facts = opening.opening_balance_close_facts(
            organization=self.org, cutoff=date(2026, 9, 30)
        )
        self.assertFalse(position["integrity"])
        self.assertIsNone(position["remaining"])
        self.assertEqual((facts["receivable"], facts["payable"]), (None, None))
        blockers = ledger._close_blockers(self.org, date(2026, 9, 30))
        self.assertTrue(
            any(
                "saldo awal piutang/utang tidak dapat dinilai" in message
                for message in blockers
            ),
            blockers,
        )
        with self.assertRaises(ValidationError):
            ledger.close_month(
                organization=self.org, actor=self.actor, year=2026, month=9
            )

    def relocate_principal_live_scope(self):
        row = self.item(
            amount="300", source_reference="SYN-CONSUMER-RELOCATED-PRINCIPAL"
        )
        original_hash, original_snapshot = row.source_hash, row.source_snapshot
        original_lines = list(
            JournalLine.objects.filter(journal=row.journal)
            .order_by("pk")
            .values_list("pk", "organization_id", "account", "debit", "credit")
        )
        foreign = Organization.objects.create(
            name="Synthetic relocated principal destination"
        )
        self.corrupt(OpeningBalanceItem, row.pk, "organization", foreign.pk)
        self.corrupt(Journal, row.journal_id, "organization", foreign.pk)
        row.refresh_from_db()
        self.assertEqual(
            (row.source_hash, row.source_snapshot), (original_hash, original_snapshot)
        )
        self.assertEqual(
            list(
                JournalLine.objects.filter(journal_id=row.journal_id)
                .order_by("pk")
                .values_list("pk", "organization_id", "account", "debit", "credit")
            ),
            original_lines,
        )
        return row

    def test_relocated_original_principal_is_unknown_and_actual_close_refuses_it(self):
        self.relocate_principal_live_scope()
        reviewed_statement(
            self,
            organization=self.org,
            actor=self.actor,
            account=self.account,
            start=self.cutoff,
            end=date(2026, 9, 30),
            opening="0",
            closing="0",
            total_in="0",
            total_out="0",
            count=0,
        )
        facts = opening.opening_balance_close_facts(
            organization=self.org, cutoff=date(2026, 9, 30)
        )
        self.assertEqual((facts["receivable"], facts["payable"]), (None, None))
        self.assertTrue(facts["blockers"])
        blockers = ledger._close_blockers(self.org, date(2026, 9, 30))
        self.assertTrue(
            any(
                "saldo awal piutang/utang tidak dapat dinilai" in message
                for message in blockers
            ),
            blockers,
        )
        with self.assertRaises(ValidationError):
            ledger.close_month(
                organization=self.org, actor=self.actor, year=2026, month=9
            )

    def test_reports_retain_relocated_original_principal_as_unknown_source(self):
        row = self.relocate_principal_live_scope()
        response, report = self.report()
        self.assertIn("opening_balances", report)
        self.assertEqual(
            (
                report["opening_balances"]["receivable"],
                report["opening_balances"]["payable"],
            ),
            (None, None),
        )
        self.assertContains(response, "Saldo lama yang masih belum dibayar")
        self.assertContains(response, "Saldo belum dapat dinilai.")
        self.assertNotContains(response, "Piutang lama yang belum diterima")
        self.assertNotContains(response, row.organization.name)
        self.assertTrue(
            any(
                "saldo awal piutang/utang tidak dapat dinilai" in message
                for message in report["blockers"]
            )
        )

    def test_reports_include_old_residual_in_existing_totals_with_explanatory_source_section(
        self,
    ):
        receivable, payable = self.old_balances()
        invoice, bill = self.ordinary_documents()
        self.actual_old_cash(receivable, payable)
        response, report = self.report()
        self.assertEqual(
            (report["receivables"], report["payables"]),
            (invoice.total + Decimal(180), bill.amount + Decimal(150)),
        )
        self.assertEqual(
            (
                report["opening_balances"]["receivable"],
                report["opening_balances"]["payable"],
            ),
            (Decimal(180), Decimal(150)),
        )
        trial = {
            row["account"]["code"]: row["debit"] - row["credit"]
            for row in report["trial_balance"]
        }
        self.assertEqual(
            (trial["AR"], -trial["AP"]), (report["receivables"], report["payables"])
        )
        self.assertEqual(report["debit_total"], report["credit_total"])
        self.assertContains(response, "Saldo lama yang masih belum dibayar")
        self.assertContains(
            response,
            "Sudah termasuk dalam total piutang dan utang; bukan penjualan atau biaya baru.",
        )
        self.assertContains(response, reverse("opening_balances") + "?as_of=2026-09-30")
        self.assertEqual((report["revenue"], report["costs"]), (0, Decimal(100)))

    def test_report_shows_unknown_source_instead_of_zero_when_opening_proof_is_corrupt(
        self,
    ):
        receivable, _ = self.old_balances()
        self.corrupt(OpeningBalanceItem, receivable.pk, "amount", Decimal(299))
        response, report = self.report()
        self.assertEqual(
            (
                report["opening_balances"]["receivable"],
                report["opening_balances"]["payable"],
            ),
            (None, None),
        )
        self.assertContains(response, "Saldo belum dapat dinilai.")
        self.assertNotContains(response, "Piutang lama yang belum diterima")
        self.assertEqual(
            report["receivables"],
            Decimal(300),
            "Original ledger remains visible while damaged detail is held.",
        )
        self.assertTrue(
            any(
                "saldo awal piutang/utang tidak dapat dinilai" in message
                for message in report["blockers"]
            )
        )

    def test_report_without_opening_items_keeps_existing_context_and_totals(self):
        invoice = self.invoice(service_date=date(2026, 10, 1))
        ledger.issue_invoice(organization=self.org, actor=self.actor, invoice=invoice)
        response, report = self.report()
        self.assertNotIn("opening_balances", report)
        self.assertNotContains(response, "Saldo lama yang masih belum dibayar")
        self.assertEqual(
            (
                report["receivables"],
                report["payables"],
                report["revenue"],
                report["costs"],
                report["profit"],
            ),
            (invoice.total, 0, 0, 0, 0),
        )
        balances = ledger.report_balances(
            organization=self.org, as_of=date(2026, 9, 30)
        )
        trial = {
            row["account"]["code"]: row["debit"] - row["credit"]
            for row in report["trial_balance"]
        }
        self.assertEqual(trial, balances)
        self.assertEqual(report["debit_total"], report["credit_total"])
