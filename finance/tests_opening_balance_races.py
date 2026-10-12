"""PostgreSQL organization-lock evidence; never claimed from SQLite."""

from datetime import date
from unittest import skipUnless

from django.db import connection
from django.test import TransactionTestCase

from . import opening_balances as opening
from . import services as ledger
from . import tests_tax_money_concurrency as race_fixtures
from .models import BankPosting, Journal, OpeningBalanceItem, OpeningBalanceSettlement
from .tests_opening_balances import OpeningBalanceFixtures


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL row locks")
class OpeningBalanceRaceTests(OpeningBalanceFixtures, TransactionTestCase):
    race = race_fixtures.TaxMoneyConcurrencyTests.race

    def test_identical_opening_request_creates_one_journal_and_event(self):
        data = self.record_data()
        self.assertEqual(
            self.race(
                lambda: opening.record_opening_balance(**data),
                lambda: opening.record_opening_balance(**data),
            ),
            ["committed", "committed"],
        )
        self.assertEqual(
            (OpeningBalanceItem.objects.count(), Journal.objects.count()), (1, 1)
        )

    def test_common_cutoff_contenders_do_not_establish_two_dates(self):
        first = self.record_data(date=date(2026, 9, 1))
        second = self.record_data(date=date(2026, 9, 2))
        self.assertCountEqual(
            self.race(
                lambda: opening.record_opening_balance(**first),
                lambda: opening.record_opening_balance(**second),
            ),
            ["committed", "rejected"],
        )
        self.assertEqual(OpeningBalanceItem.objects.count(), 1)

    def test_normalized_original_document_identity_has_one_opening(self):
        first = self.record_data(source_reference="SYN-ORIGINAL")
        second = self.record_data(source_reference=" syn-original ")
        self.assertCountEqual(
            self.race(
                lambda: opening.record_opening_balance(**first),
                lambda: opening.record_opening_balance(**second),
            ),
            ["committed", "rejected"],
        )
        self.assertEqual(OpeningBalanceItem.objects.count(), 1)

    def test_two_actual_bank_rows_cannot_spend_one_remaining_principal(self):
        row = self.item(amount="100")
        banks = [
            self.movement("100", reference=f"RACE-PRINCIPAL-{n}") for n in range(2)
        ]
        data = [self.settlement_data(row, bank, amount="100") for bank in banks]
        self.assertCountEqual(
            self.race(
                *[
                    lambda data=data: opening.settle_opening_balance(**data)
                    for data in data
                ]
            ),
            ["committed", "rejected"],
        )
        self.assertEqual(
            (OpeningBalanceSettlement.objects.count(), self.position(row)["remaining"]),
            (1, 0),
        )

    def test_two_opening_items_cannot_allocate_same_bank_cash_twice(self):
        items = [self.item(amount="100") for _ in range(2)]
        bank = self.movement("100", reference="RACE-BANK-CAPACITY")
        data = [self.settlement_data(item, bank, amount="100") for item in items]
        self.assertCountEqual(
            self.race(
                *[
                    lambda data=data: opening.settle_opening_balance(**data)
                    for data in data
                ]
            ),
            ["committed", "rejected"],
        )
        self.assertEqual(
            (OpeningBalanceSettlement.objects.count(), bank.unallocated_amount), (1, 0)
        )

    def test_identical_cash_request_has_one_bank_claim_and_receipt(self):
        row = self.item(amount="100")
        data = self.settlement_data(
            row, self.movement("100", reference="RACE-SAME-CASH"), amount="100"
        )
        self.assertEqual(
            self.race(
                lambda: opening.settle_opening_balance(**data),
                lambda: opening.settle_opening_balance(**data),
            ),
            ["committed", "committed"],
        )
        self.assertEqual(
            (
                OpeningBalanceSettlement.objects.count(),
                BankPosting.objects.count(),
                Journal.objects.count(),
            ),
            (1, 1, 2),
        )

    def test_opening_and_ordinary_owner_claim_share_bank_capacity(self):
        row = self.item(amount="100")
        bank = self.movement("100", reference="RACE-SHARED-CASH")
        data = self.settlement_data(row, bank, amount="100")
        self.assertCountEqual(
            self.race(
                lambda: opening.settle_opening_balance(**data),
                lambda: ledger.reconcile_bank_adjustment(
                    organization=self.org,
                    actor=self.actor,
                    transaction=bank,
                    kind="owner_funding",
                    amount="100",
                    reference="SYN-RACE-FUNDING",
                    evidence="Synthetic actual owner funding.",
                ),
            ),
            ["committed", "rejected"],
        )
        self.assertEqual((BankPosting.objects.count(), bank.unallocated_amount), (1, 0))
