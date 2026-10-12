"""Real PostgreSQL refund competition; SQLite is not concurrency evidence."""

from copy import deepcopy
from datetime import date
from decimal import Decimal
from unittest import skipUnless
from uuid import uuid4

from django.db import connection
from django.test import TransactionTestCase

from . import opening_balances as opening
from . import services as ledger
from . import tests_tax_money_concurrency as race_fixtures
from .models import (
    BankPosting,
    BankTransaction,
    Journal,
    OpeningBalanceCancellation,
    OpeningBalanceRefund,
    OpeningBalanceSettlement,
)
from .tests_opening_cash_refund_controls import OpeningRefundControlFixtures


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL row locks")
class OpeningCashRefundConcurrencyTests(
    OpeningRefundControlFixtures, TransactionTestCase
):
    race = race_fixtures.TaxMoneyConcurrencyTests.race

    def test_two_distinct_refunds_cannot_claim_one_actual_opposite_bank_twice(self):
        first, first_bank, first_cash = self.matched_receipt()
        second, second_bank, second_cash = self.matched_receipt()
        returned = self.movement(
            "-80", day=date(2026, 9, 15), reference="SYN-REFUND-RACE-SHARED-BANK"
        )
        original_items = [
            deepcopy(
                opening.opening_balance_source_snapshot(
                    organization=self.org, item=item
                )
            )
            for item in (first, second)
        ]
        original_cash = [
            deepcopy(
                opening.opening_settlement_source_snapshot(
                    organization=self.org, settlement=cash
                )
            )
            for cash in (first_cash, second_cash)
        ]
        requests = [
            self.refund_data(cash, returned, amount="80")
            for cash in (first_cash, second_cash)
        ]
        for request in requests:
            preview = opening.opening_balance_action_context(
                organization=self.org,
                action="refund",
                settlement=request["settlement"],
                transaction=returned,
                amount="80",
            )
            self.assertTrue(preview["current"], preview)
            self.assertFalse(preview["blockers"], preview)
            self.assertEqual(
                (
                    preview["before"],
                    preview["after"],
                    preview["bank_remaining"],
                    preview["bank_after"],
                ),
                (Decimal(180), Decimal(260), Decimal(80), Decimal(0)),
            )
        self.assertNotEqual(requests[0]["request_key"], requests[1]["request_key"])
        self.assertCountEqual(
            self.race(
                *[
                    lambda request=request: opening.refund_opening_settlement(**request)
                    for request in requests
                ]
            ),
            ["committed", "rejected"],
        )
        event = OpeningBalanceRefund.objects.get()
        self.assertIn(event.settlement_id, (first_cash.pk, second_cash.pk))
        self.assertEqual(
            (event.amount, event.date, event.bank_posting.signed_amount),
            (Decimal(80), date(2026, 9, 15), Decimal(-80)),
        )
        self.assertEqual(
            (
                OpeningBalanceRefund.objects.count(),
                OpeningBalanceSettlement.objects.count(),
                BankPosting.objects.count(),
                BankTransaction.objects.count(),
                Journal.objects.count(),
            ),
            (1, 2, 3, 3, 5),
        )
        self.assertEqual(
            (
                first_bank.unallocated_amount,
                second_bank.unallocated_amount,
                returned.unallocated_amount,
            ),
            (Decimal(0), Decimal(0), Decimal(0)),
        )
        self.assertFalse(
            ledger.pending_bank_transactions(organization=self.org).exists()
        )
        residuals = tuple(
            self.position(item, as_of=date(2026, 9, 15))["remaining"]
            for item in (first, second)
        )
        self.assertIn(
            residuals, ((Decimal(260), Decimal(180)), (Decimal(180), Decimal(260)))
        )
        balances = ledger.report_balances(
            organization=self.org, as_of=date(2026, 9, 15)
        )
        self.assertEqual(
            tuple(
                balances[key]
                for key in ("BANK", "AR", "AP", "EQUITY", "REVENUE", "EXPENSE")
            ),
            (
                Decimal(160),
                Decimal(440),
                Decimal(0),
                Decimal(-600),
                Decimal(0),
                Decimal(0),
            ),
        )
        closing = opening.opening_balance_close_facts(
            organization=self.org, cutoff=date(2026, 9, 15)
        )
        self.assertTrue(closing["current"])
        self.assertEqual(
            (closing["receivable"], closing["payable"], closing["blockers"]),
            (Decimal(440), Decimal(0), []),
        )
        for item, proof in zip((first, second), original_items):
            self.assertEqual(
                opening.opening_balance_source_snapshot(
                    organization=self.org, item=item
                ),
                proof,
            )
        for cash, proof in zip((first_cash, second_cash), original_cash):
            self.assertEqual(
                opening.opening_settlement_source_snapshot(
                    organization=self.org, settlement=cash
                ),
                proof,
            )
        self.assertFalse(OpeningBalanceCancellation.objects.exists())

    def test_cancel_and_actual_refund_of_same_origin_cannot_both_commit(self):
        item, original_bank, cash = self.matched_receipt()
        returned = self.movement(
            "-40", day=date(2026, 9, 15), reference="SYN-REFUND-CANCEL-RACE-BANK"
        )
        original_item = deepcopy(
            opening.opening_balance_source_snapshot(organization=self.org, item=item)
        )
        original_cash = deepcopy(
            opening.opening_settlement_source_snapshot(
                organization=self.org, settlement=cash
            )
        )
        cancel_preview = opening.opening_balance_action_context(
            organization=self.org, action="cancel", settlement=cash
        )
        refund_preview = opening.opening_balance_action_context(
            organization=self.org,
            action="refund",
            settlement=cash,
            transaction=returned,
            amount="40",
        )
        for preview in (cancel_preview, refund_preview):
            self.assertTrue(preview["current"], preview)
            self.assertFalse(preview["blockers"], preview)
        cancel_data = dict(
            organization=self.org,
            actor=self.actor,
            settlement=cash,
            request_key=str(uuid4()),
            reason="Synthetic wrong assignment independently confirmed.",
            evidence="Synthetic original bank and wrong document proof.",
            expected_fingerprint=cancel_preview["fingerprint"],
            confirmed=True,
        )
        refund_data = self.refund_data(
            cash, returned, expected_fingerprint=refund_preview["fingerprint"]
        )
        self.assertCountEqual(
            self.race(
                lambda: opening.cancel_opening_settlement(**cancel_data),
                lambda: opening.refund_opening_settlement(**refund_data),
            ),
            ["committed", "rejected"],
        )
        cancellations, refunds = (
            OpeningBalanceCancellation.objects.count(),
            OpeningBalanceRefund.objects.count(),
        )
        self.assertIn((cancellations, refunds), ((1, 0), (0, 1)))
        self.assertEqual(
            (
                OpeningBalanceSettlement.objects.count(),
                BankTransaction.objects.count(),
                Journal.objects.count(),
            ),
            (1, 2, 3),
        )
        balances = ledger.report_balances(
            organization=self.org, as_of=date(2026, 9, 15)
        )
        closing = opening.opening_balance_close_facts(
            organization=self.org, cutoff=date(2026, 9, 15)
        )
        self.assertTrue(closing["current"])
        if cancellations:
            self.assertEqual(BankPosting.objects.count(), 1)
            self.assertEqual(
                (original_bank.unallocated_amount, returned.unallocated_amount),
                (Decimal(120), Decimal(-40)),
            )
            self.assertEqual(
                set(
                    ledger.pending_bank_transactions(organization=self.org).values_list(
                        "pk", flat=True
                    )
                ),
                {original_bank.pk, returned.pk},
            )
            self.assertEqual(
                tuple(balances[key] for key in ("BANK", "AR", "AP", "EQUITY")),
                (Decimal(0), Decimal(300), Decimal(0), Decimal(-300)),
            )
            self.assertEqual(closing["receivable"], Decimal(300))
        else:
            self.assertEqual(BankPosting.objects.count(), 2)
            self.assertEqual(
                (original_bank.unallocated_amount, returned.unallocated_amount),
                (Decimal(0), Decimal(0)),
            )
            self.assertFalse(
                ledger.pending_bank_transactions(organization=self.org).exists()
            )
            self.assertEqual(
                tuple(balances[key] for key in ("BANK", "AR", "AP", "EQUITY")),
                (Decimal(80), Decimal(220), Decimal(0), Decimal(-300)),
            )
            self.assertEqual(closing["receivable"], Decimal(220))
        self.assertEqual(
            (
                closing["payable"],
                closing["blockers"],
                balances["REVENUE"],
                balances["EXPENSE"],
            ),
            (Decimal(0), [], Decimal(0), Decimal(0)),
        )
        self.assertEqual(
            opening.opening_balance_source_snapshot(organization=self.org, item=item),
            original_item,
        )
        self.assertEqual(
            opening.opening_settlement_source_snapshot(
                organization=self.org, settlement=cash
            ),
            original_cash,
        )
