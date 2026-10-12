"""Source-only draft: real refund dashboard chart totals and integrity holds.

Root may promote this NEW module after the frozen run finishes. This draft has
not been imported or executed. Two real HTTP tests check literal chart totals
and damaged-source holds. The healthy request counts executed SQL statements
and must meet the <=5,000 resource cap approved from measured profiling. No SQL
contents are recorded and no projection calls are mocked. Runtime performance
acceptance retains the original browser budget and a separate measured profile.
"""

from copy import deepcopy
from datetime import date
from decimal import Decimal
from uuid import uuid4

from django.db import connection
from django.db.models import Sum
from django.test import TestCase
from django.urls import reverse

from core.models import Organization
from finance import opening_balances as openings
from finance import services as ledger
from finance.models import Journal, JournalLine, OpeningBalanceRefund
from webapp import tests_opening_cash_refund_controls as refund_fixtures


class OpeningRefundDashboardChartHTTPTests(TestCase):
    # Reuse the existing synthetic fixture helper without inheriting its tests.
    old_cash = refund_fixtures.OpeningCashRefundControlsHTTPTests.old_cash
    counts = refund_fixtures.OpeningCashRefundControlsHTTPTests.counts

    def setUp(self):
        refund_fixtures.OpeningCashRefundControlsHTTPTests.setUp(self)
        self.originals = {
            original[0].pk: (
                original[0].amount,
                original[0].date,
                original[0].source_hash,
                deepcopy(original[0].source_snapshot),
                original[1].amount,
                original[1].date,
                original[1].source_hash,
                deepcopy(original[1].source_snapshot),
            )
            for original in (self.ar, self.ap)
        }
        self.refunds = []
        for original, amount, actual_day in (
            (self.ar, "50", date(2026, 9, 12)),
            (self.ap, "60", date(2026, 9, 15)),
        ):
            item, cash, original_bank, returned_bank = original
            preview = openings.opening_balance_action_context(
                organization=self.org,
                action="refund",
                settlement=cash,
                transaction=returned_bank,
                amount=amount,
            )
            self.assertTrue(preview["current"], preview)
            self.assertFalse(preview["blockers"], preview)
            refund = openings.refund_opening_settlement(
                organization=self.org,
                actor=self.actors["finance"],
                settlement=cash,
                transaction=returned_bank,
                amount=amount,
                reason="Synthetic genuine return independently checked",
                evidence="Synthetic original cash and distinct actual returned cash",
                request_key=str(uuid4()),
                expected_fingerprint=preview["fingerprint"],
                confirmed=True,
            )
            self.assertEqual(
                (refund.date, refund.amount), (actual_day, Decimal(amount))
            )
            self.assertEqual(
                (original_bank.unallocated_amount, returned_bank.unallocated_amount),
                (0, 0),
            )
            self.refunds.append(refund)
        self.assertEqual(
            OpeningBalanceRefund.objects.filter(organization=self.org).count(), 2
        )
        balances = ledger.report_balances(
            organization=self.org, as_of=date(2026, 9, 15)
        )
        self.assertEqual(
            tuple(balances[key] for key in ("BANK", "AR", "AP", "EQUITY")),
            (Decimal(1070), Decimal(50), Decimal(-60), Decimal(-1060)),
        )
        self.assertEqual(sum(balances.values()), 0)
        self.assertFalse(
            ledger.pending_bank_transactions(organization=self.org).exists()
        )

    def post_movements(
        self, reference, day, *, revenue="0", expense="0", organization=None
    ):
        """Explicit synthetic commercial entries, independent of chart helpers."""
        org = organization or self.org
        entries = []
        revenue, expense = Decimal(revenue), Decimal(expense)
        if revenue > 0:
            entries += [("EQUITY", revenue, 0), ("REVENUE", 0, revenue)]
        elif revenue < 0:
            entries += [("REVENUE", -revenue, 0), ("EQUITY", 0, -revenue)]
        if expense > 0:
            entries += [("EXPENSE", expense, 0), ("EQUITY", 0, expense)]
        elif expense < 0:
            entries += [("EQUITY", -expense, 0), ("EXPENSE", 0, -expense)]
        return ledger.post_journal(
            organization=org,
            actor=self.owner if org.pk == self.org.pk else None,
            date=day,
            source_key=reference,
            description="Synthetic dated chart entry",
            entries=entries,
        )

    def assert_original_sources_retained(self):
        for item, cash, _, _ in (self.ar, self.ap):
            item.refresh_from_db()
            cash.refresh_from_db()
            self.assertEqual(
                (
                    item.amount,
                    item.date,
                    item.source_hash,
                    item.source_snapshot,
                    cash.amount,
                    cash.date,
                    cash.source_hash,
                    cash.source_snapshot,
                ),
                self.originals[item.pk],
            )

    def test_dashboard_chart_uses_literal_six_month_signed_totals_and_both_scopes(self):
        # April and September exercise both inclusive month boundaries. June is
        # genuinely empty. May contains income reversal and an expense credit.
        for reference, day, revenue, expense in (
            ("SYN-CHART-APR-FIRST", date(2026, 4, 1), "100", "25"),
            ("SYN-CHART-APR-LAST", date(2026, 4, 30), "40", "10"),
            ("SYN-CHART-MAY", date(2026, 5, 10), "200", "80"),
            ("SYN-CHART-MAY-REVERSAL", date(2026, 5, 31), "-20", "-5"),
            ("SYN-CHART-JUL-FIRST", date(2026, 7, 1), "70", "0"),
            ("SYN-CHART-JUL-LAST", date(2026, 7, 31), "0", "20"),
            ("SYN-CHART-AUG", date(2026, 8, 20), "60", "15"),
            ("SYN-CHART-SEP-FIRST", date(2026, 9, 1), "50", "10"),
            ("SYN-CHART-SEP-LAST", date(2026, 9, 30), "30", "8"),
            ("SYN-CHART-BEFORE-WINDOW", date(2026, 3, 31), "700", "70"),
            ("SYN-CHART-AFTER-WINDOW", date(2026, 10, 1), "900", "90"),
        ):
            self.post_movements(reference, day, revenue=revenue, expense=expense)

        draft = Journal.objects.create(
            organization=self.org,
            date=date(2026, 6, 15),
            source_key="SYN-CHART-DRAFT",
            description="Synthetic unposted chart entry",
        )
        for account, debit, credit in (
            ("EQUITY", "999", "0"),
            ("REVENUE", "0", "999"),
            ("EXPENSE", "777", "0"),
            ("EQUITY", "0", "777"),
        ):
            JournalLine.objects.create(
                organization=self.org,
                journal=draft,
                account=account,
                debit=Decimal(debit),
                credit=Decimal(credit),
            )
        self.assertEqual(draft.status, "draft")

        foreign = Organization.objects.create(
            name="Synthetic private chart scope", is_demo=True
        )
        self.post_movements(
            "SYN-CHART-FOREIGN-NORMAL",
            date(2026, 7, 8),
            revenue="1234",
            expense="4321",
            organization=foreign,
        )
        own_parent = self.post_movements(
            "SYN-CHART-OWN-PARENT", date(2026, 7, 9), revenue="2222", expense="3333"
        )
        foreign_parent = self.post_movements(
            "SYN-CHART-FOREIGN-PARENT",
            date(2026, 7, 10),
            revenue="4444",
            expense="5555",
            organization=foreign,
        )
        # Intentional isolated persisted-scope faults exercise each side of the
        # double organization filter. They are not a normal editing operation.
        # All lines move together, so neither original journal becomes unbalanced.
        table = connection.ops.quote_name(JournalLine._meta.db_table)
        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {table} SET organization_id = %s WHERE journal_id = %s",
                [foreign.pk, own_parent.pk],
            )
            cursor.execute(
                f"UPDATE {table} SET organization_id = %s WHERE journal_id = %s",
                [self.org.pk, foreign_parent.pk],
            )
        self.assertEqual(
            set(own_parent.lines.values_list("organization_id", flat=True)),
            {foreign.pk},
        )
        self.assertEqual(
            set(foreign_parent.lines.values_list("organization_id", flat=True)),
            {self.org.pk},
        )
        valid = openings.opening_balance_close_facts(
            organization=self.org, cutoff=date(2026, 9, 30)
        )
        self.assertTrue(valid["current"], valid)

        before = self.counts()
        query_count = 0

        def count_request_sql(execute, sql, params, many, context):
            nonlocal query_count
            query_count += 1
            return execute(sql, params, many, context)

        # Count executed statements only within the real HTTP request, including
        # middleware and rendering; fixture and proof reads are outside the counter.
        # No SQL text, parameters, or mock projection results are recorded.
        with connection.execute_wrapper(count_request_sql):
            response = self.client.get(reverse("dashboard"), {"period": "2026-09"})
        self.assertEqual(response.status_code, 200)
        data = response.context["dashboard"]
        self.assertFalse(data.get("books_held", False))
        self.assertEqual(
            [(row["revenue"], row["costs"]) for row in data["monthly_series"]],
            [
                (Decimal(140), Decimal(35)),
                (Decimal(180), Decimal(75)),
                (Decimal(0), Decimal(0)),
                (Decimal(70), Decimal(20)),
                (Decimal(60), Decimal(15)),
                (Decimal(80), Decimal(18)),
            ],
        )
        self.assertEqual(
            (data["revenue"], data["costs"], data["profit"]),
            (Decimal(80), Decimal(18), Decimal(62)),
        )
        self.assertContains(response, 'class="bar-chart"')
        self.assertNotContains(response, "Ringkasan keuangan belum dapat diperiksa.")
        self.assertNotContains(response, foreign.name)
        self.assertEqual(
            self.counts(),
            before,
            "Reading the chart does not record financial or tax actions",
        )
        self.assert_original_sources_retained()
        # Root profiling of the authentic two-refund landing saturated query
        # logging at >=8,997 statements. 5,000 is the approved resource cap,
        # not an assertion of an unmeasured optimized native query count.
        # Evaluate it after all literal chart and source-retention assertions.
        self.assertLessEqual(
            query_count,
            5000,
            f"Healthy dashboard exceeded the approved SQL resource budget: {query_count} statements",
        )

    def test_dashboard_with_balanced_damaged_refund_keeps_all_numbers_and_chart_unknown(
        self,
    ):
        # Physical cash and the immutable source remain intact; changing the
        # typed debit still balances and must never become apparent profit.
        refund = self.refunds[0]
        debit = refund.journal.lines.get(account="AR", debit=Decimal(50))
        originals = {
            bank.pk: (
                bank.organization_id,
                bank.account_id,
                bank.date,
                bank.amount,
                bank.reference,
                bank.description,
            )
            for original in (self.ar, self.ap)
            for bank in original[2:]
        }
        table = connection.ops.quote_name(JournalLine._meta.db_table)
        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {table} SET account = %s WHERE id = %s", ["EXPENSE", debit.pk]
            )
        totals = refund.journal.lines.aggregate(
            debit=Sum("debit"), credit=Sum("credit")
        )
        self.assertEqual(
            (totals["debit"], totals["credit"]), (Decimal(50), Decimal(50))
        )
        position = openings.opening_balance_position(
            organization=self.org, item=self.ar[0]
        )
        self.assertFalse(position["current"], position)
        self.assertIsNone(position["remaining"])
        close = openings.opening_balance_close_facts(
            organization=self.org, cutoff=date(2026, 9, 30)
        )
        self.assertFalse(close["current"], close)
        self.assertIsNone(close["receivable"])
        self.assertIsNone(close["payable"])
        # report_balances is intentionally raw, not a source validator.
        self.assertEqual(
            ledger.report_balances(organization=self.org, as_of=date(2026, 9, 30))[
                "EXPENSE"
            ],
            Decimal(50),
        )

        before = self.counts()
        response = self.client.get(reverse("dashboard"), {"period": "2026-09"})
        self.assertEqual(response.status_code, 200)
        data = response.context["dashboard"]
        self.assertTrue(data["books_held"])
        self.assertEqual(data["monthly_series"], [])
        for field in (
            "bank_balance",
            "receivables",
            "payables",
            "revenue",
            "costs",
            "profit",
            "unmatched_count",
            "order_count",
            "review_bills",
        ):
            self.assertIsNone(data[field], field)
        self.assertEqual(data["recent_invoices"], [])
        self.assertContains(response, "Ringkasan keuangan belum dapat diperiksa.")
        self.assertContains(response, "Pendapatan dan biaya belum dapat diperiksa.")
        self.assertContains(response, "Belum dapat diperiksa")
        self.assertNotContains(response, 'class="bar-chart"')
        self.assertContains(response, reverse("opening_balances"))
        self.assertEqual(self.counts(), before)
        for original in (self.ar, self.ap):
            for bank in original[2:]:
                bank.refresh_from_db()
                self.assertEqual(
                    (
                        bank.organization_id,
                        bank.account_id,
                        bank.date,
                        bank.amount,
                        bank.reference,
                        bank.description,
                    ),
                    originals[bank.pk],
                )
        self.assert_original_sources_retained()
