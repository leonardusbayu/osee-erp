"""Real PostgreSQL recovery locks; SQLite is not concurrency evidence."""

from decimal import Decimal
from unittest import skipUnless

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
    OpeningBalanceSettlement,
)
from .tests_opening_cash_recovery import OpeningCancellationFixtures


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL row locks")
class OpeningCashRecoveryRaceTests(OpeningCancellationFixtures, TransactionTestCase):
    race = race_fixtures.TaxMoneyConcurrencyTests.race

    def test_same_cancel_submission_has_one_reversal_and_one_released_physical_claim(
        self,
    ):
        item, bank, cash = self.matched_receipt()
        data = self.cancellation_data(cash)
        self.assertEqual(
            self.race(
                lambda: opening.cancel_opening_settlement(**data),
                lambda: opening.cancel_opening_settlement(**data),
            ),
            ["committed", "committed"],
        )
        self.assertEqual(
            (
                OpeningBalanceCancellation.objects.count(),
                OpeningBalanceSettlement.objects.count(),
                BankPosting.objects.count(),
                BankTransaction.objects.count(),
                Journal.objects.count(),
            ),
            (1, 1, 1, 1, 3),
        )
        self.assertEqual(
            (self.position(item)["remaining"], bank.unallocated_amount),
            (Decimal(300), Decimal(120)),
        )
        balances = ledger.report_balances(organization=self.org)
        self.assertEqual(
            (balances["BANK"], balances["AR"], balances["EQUITY"]),
            (Decimal(0), Decimal(300), Decimal(-300)),
        )
        self.assertEqual(
            set(
                ledger.pending_bank_transactions(organization=self.org).values_list(
                    "pk", flat=True
                )
            ),
            {bank.pk},
        )

    def test_exact_cancel_retry_racing_rematch_keeps_one_release_and_one_physical_cash(
        self,
    ):
        item, bank, cash = self.matched_receipt()
        cancellation_data = self.cancellation_data(cash)
        cancellation = opening.cancel_opening_settlement(**cancellation_data)
        original = opening.opening_settlement_source_snapshot(
            organization=self.org, settlement=cash
        )
        rematch_data = self.settlement_data(item, bank)
        self.assertEqual(
            self.race(
                lambda: opening.cancel_opening_settlement(**cancellation_data),
                lambda: opening.settle_opening_balance(**rematch_data),
            ),
            ["committed", "committed"],
        )
        self.assertEqual(
            (
                OpeningBalanceCancellation.objects.count(),
                OpeningBalanceSettlement.objects.count(),
                BankPosting.objects.count(),
                BankTransaction.objects.count(),
                Journal.objects.count(),
            ),
            (1, 2, 2, 1, 4),
        )
        self.assertEqual(OpeningBalanceCancellation.objects.get().pk, cancellation.pk)
        self.assertEqual(
            (self.position(item)["remaining"], bank.unallocated_amount),
            (Decimal(180), Decimal(0)),
        )
        self.assertFalse(
            ledger.pending_bank_transactions(organization=self.org).exists()
        )
        self.assertEqual(
            opening.opening_settlement_source_snapshot(
                organization=self.org, settlement=cash
            ),
            original,
        )
        balances = ledger.report_balances(organization=self.org)
        self.assertEqual(
            (balances["BANK"], balances["AR"], balances["EQUITY"]),
            (Decimal(120), Decimal(180), Decimal(-300)),
        )

    def test_two_new_rematches_cannot_consume_one_released_bank_receipt_twice(self):
        item, bank, cash = self.matched_receipt()
        other = self.item(amount="200")
        cancellation = opening.cancel_opening_settlement(**self.cancellation_data(cash))
        original_item = opening.opening_balance_source_snapshot(
            organization=self.org, item=item
        )
        other_item = opening.opening_balance_source_snapshot(
            organization=self.org, item=other
        )
        original_cash = opening.opening_settlement_source_snapshot(
            organization=self.org, settlement=cash
        )
        cancellation_proof = opening.opening_cancellation_source_snapshot(
            organization=self.org, cancellation=cancellation
        )

        requests = []
        for target, principal in ((item, Decimal(300)), (other, Decimal(200))):
            preview = opening.opening_balance_action_context(
                organization=self.org,
                action="settle",
                item=target,
                transaction=bank,
                amount="120",
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
                (principal, principal - Decimal(120), Decimal(120), Decimal(0)),
            )
            requests.append(
                self.settlement_data(
                    target, bank, expected_fingerprint=preview["fingerprint"]
                )
            )
        self.assertNotEqual(requests[0]["request_key"], requests[1]["request_key"])
        self.assertNotEqual(requests[0]["item"].pk, requests[1]["item"].pk)
        self.assertCountEqual(
            self.race(
                *[
                    lambda data=data: opening.settle_opening_balance(**data)
                    for data in requests
                ]
            ),
            ["committed", "rejected"],
        )
        # A distinct rejected request must not create another claim or journal.
        accepted = OpeningBalanceSettlement.objects.exclude(pk=cash.pk)
        self.assertEqual(accepted.count(), 1)
        self.assertIn(accepted.get().item_id, (item.pk, other.pk))
        self.assertEqual(
            (accepted.get().amount, accepted.get().date), (Decimal(120), self.day)
        )
        self.assertEqual(
            (
                OpeningBalanceCancellation.objects.count(),
                OpeningBalanceSettlement.objects.count(),
                BankPosting.objects.count(),
                BankTransaction.objects.count(),
                Journal.objects.count(),
            ),
            (1, 2, 2, 1, 5),
        )
        self.assertEqual(
            (bank.amount, bank.unallocated_amount), (Decimal(120), Decimal(0))
        )
        self.assertFalse(
            ledger.pending_bank_transactions(organization=self.org).exists()
        )
        residuals = (
            self.position(item)["remaining"],
            self.position(other)["remaining"],
        )
        self.assertIn(
            residuals, ((Decimal(180), Decimal(200)), (Decimal(300), Decimal(80)))
        )
        balances = ledger.report_balances(organization=self.org)
        self.assertEqual(
            tuple(balances[key] for key in ("BANK", "AR", "AP", "EQUITY")),
            (Decimal(120), Decimal(380), Decimal(0), Decimal(-500)),
        )
        self.assertEqual(
            tuple(balances[key] for key in ("REVENUE", "EXPENSE", "DEFERRED_REVENUE")),
            (Decimal(0), Decimal(0), Decimal(0)),
        )
        self.assertEqual(sum(balances.values()), Decimal(0))
        closing = opening.opening_balance_close_facts(
            organization=self.org, cutoff=self.day
        )
        self.assertTrue(closing["current"], closing)
        self.assertEqual(
            (closing["receivable"], closing["payable"], closing["blockers"]),
            (Decimal(380), Decimal(0), []),
        )
        self.assertEqual(
            opening.opening_balance_source_snapshot(organization=self.org, item=item),
            original_item,
        )
        self.assertEqual(
            opening.opening_balance_source_snapshot(organization=self.org, item=other),
            other_item,
        )
        self.assertEqual(
            opening.opening_settlement_source_snapshot(
                organization=self.org, settlement=cash
            ),
            original_cash,
        )
        self.assertEqual(
            opening.opening_cancellation_source_snapshot(
                organization=self.org, cancellation=cancellation
            ),
            cancellation_proof,
        )
