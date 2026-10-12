"""PostgreSQL organization-lock correction races; SQLite gives no lock evidence."""

from datetime import date
from decimal import Decimal
from unittest import skipIf

from django.db import connection
from django.test import TransactionTestCase

from . import opening_balances as opening
from . import services as ledger
from . import tests_tax_money_concurrency as race_fixtures
from .models import BankPosting, OpeningBalanceCorrection, OpeningBalanceSettlement
from .tests_opening_balance_corrections import OpeningCorrectionFixtures


@skipIf(
    connection.vendor == "sqlite",
    "Requires PostgreSQL row locks; SQLite is not concurrency evidence",
)
class OpeningCorrectionRaceTests(OpeningCorrectionFixtures, TransactionTestCase):
    race = race_fixtures.TaxMoneyConcurrencyTests.race

    def setUp(self):
        self.assertEqual(
            connection.vendor,
            "postgresql",
            "Correction race evidence requires PostgreSQL",
        )
        super().setUp()

    def test_two_principal_decreases_cannot_spend_the_same_remaining_principal(self):
        item = self.item(amount="100")
        contenders = [
            self.correction_data(item, direction="decrease", amount="80")
            for _ in range(2)
        ]
        self.assertCountEqual(
            self.race(
                *[
                    lambda data=data: opening.correct_opening_balance(**data)
                    for data in contenders
                ]
            ),
            ["committed", "rejected"],
        )
        position = opening.opening_balance_position(
            organization=self.org, item=item, as_of=date(2026, 9, 30)
        )
        self.assertTrue(position["current"])
        self.assertEqual(
            (position["opening"], position["remaining"], len(position["corrections"])),
            (Decimal(20), Decimal(20), 1),
        )
        self.assertEqual(
            (OpeningBalanceCorrection.objects.count(), BankPosting.objects.count()),
            (1, 0),
        )
        balances = ledger.report_balances(organization=self.org)
        self.assertEqual(
            (balances["BANK"], balances["AR"], balances["EQUITY"]),
            (0, Decimal(20), Decimal(-20)),
        )

    def test_backdated_decrease_and_actual_cash_cannot_both_consume_one_principal(self):
        item = self.item(amount="100")
        bank = self.movement(
            "80", day=date(2026, 9, 15), reference="CORRECTION-CASH-RACE"
        )
        correction = self.correction_data(
            item, date=date(2026, 9, 10), direction="decrease", amount="60"
        )
        cash = self.settlement_data(item, bank, amount="80")
        self.assertCountEqual(
            self.race(
                lambda: opening.correct_opening_balance(**correction),
                lambda: opening.settle_opening_balance(**cash),
            ),
            ["committed", "rejected"],
        )
        position = opening.opening_balance_position(
            organization=self.org, item=item, as_of=date(2026, 9, 30)
        )
        self.assertTrue(position["current"])
        self.assertEqual(
            OpeningBalanceCorrection.objects.count()
            + OpeningBalanceSettlement.objects.count(),
            1,
        )
        balances = ledger.report_balances(organization=self.org)
        if position["corrections"]:
            self.assertEqual(
                (
                    position["opening"],
                    position["settled"],
                    position["remaining"],
                    bank.unallocated_amount,
                ),
                (Decimal(40), 0, Decimal(40), Decimal(80)),
            )
            self.assertEqual(
                (balances["BANK"], balances["AR"], balances["EQUITY"]),
                (0, Decimal(40), Decimal(-40)),
            )
        else:
            self.assertEqual(
                (
                    position["opening"],
                    position["settled"],
                    position["remaining"],
                    bank.unallocated_amount,
                ),
                (Decimal(100), Decimal(80), Decimal(20), 0),
            )
            self.assertEqual(
                (balances["BANK"], balances["AR"], balances["EQUITY"]),
                (Decimal(80), Decimal(20), Decimal(-100)),
            )
        self.assertEqual((balances["REVENUE"], balances["EXPENSE"]), (0, 0))
