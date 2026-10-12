"""True PostgreSQL row-lock acceptance; SQLite is not race evidence."""

from datetime import date
from unittest import skipUnless

from django.db import connection
from django.test import TransactionTestCase

from finance import tests_tax_money_concurrency as race_fixtures
from finance.budget_adapter import bill_budget_facts

from . import spend_services as s
from .models import BudgetBillLink, BudgetPaymentMatch, BudgetSpendMovement
from .tests_spend import SpendFixtures, SpendPaymentFixtures


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL row locks")
class SpendConcurrencyTests(SpendFixtures, TransactionTestCase):
    race = race_fixtures.TaxMoneyConcurrencyTests.race

    def test_two_partial_conversions_cannot_use_the_same_commitment(self):
        spend = self.make_spend()
        controls = self.controls(spend)
        outcomes = self.race(
            *[
                lambda n=n: s.convert_spend(
                    spend=spend,
                    from_kind="open_commitment",
                    to_kind="incurred",
                    amount="70000000",
                    **(controls | {"reference": f"RACE-CONVERT-{n}"}),
                )
                for n in range(2)
            ]
        )
        self.assertCountEqual(outcomes, ["committed", "rejected"])
        self.assertEqual(
            (
                self.position(spend)["incurred"],
                self.position(spend)["open_commitments"],
            ),
            ("70000000.00", "10000000.00"),
        )
        self.assertEqual(BudgetSpendMovement.objects.filter(kind="convert").count(), 1)

    def test_identical_conversion_retry_commits_one_observation(self):
        spend = self.make_spend()
        controls = self.controls(spend)
        action = lambda: s.convert_spend(
            spend=spend,
            from_kind="open_commitment",
            to_kind="incurred",
            amount="20000000",
            **controls,
        )
        self.assertCountEqual(self.race(action, action), ["committed", "committed"])
        self.assertEqual(BudgetSpendMovement.objects.filter(kind="convert").count(), 1)
        self.assertEqual(self.position(spend)["consumed"], "80000000.00")

    def test_one_bill_cannot_be_linked_to_competing_spends(self):
        spends = [self.make_spend("reservation"), self.make_spend("reservation")]
        bill = self.bill()
        source_hash = bill_budget_facts(
            organization=self.org, bill=bill, as_of=date(2026, 10, 9)
        )["fingerprint"]
        actions = []
        for spend in spends:
            controls = self.controls(spend)
            actions.append(
                lambda spend=spend, controls=controls: s.link_spend_bill(
                    spend=spend,
                    bill=bill,
                    coverage=[
                        {
                            "source_key": f"entry:{spend.origin_entry_id}",
                            "amount": "60000000",
                        }
                    ],
                    expected_bill_fingerprint=source_hash,
                    **controls,
                )
            )
        self.assertCountEqual(self.race(*actions), ["committed", "rejected"])
        self.assertEqual(
            BudgetBillLink.objects.filter(voided_at__isnull=True).count(), 1
        )

    def test_owner_correction_and_new_conversion_have_one_current_version_winner(self):
        spend = self.make_spend()
        movement = s.convert_spend(
            spend=spend,
            from_kind="open_commitment",
            to_kind="incurred",
            amount="20000000",
            **self.controls(spend),
        )
        controls = self.controls(spend, date_value=date(2026, 10, 1))
        outcomes = self.race(
            lambda: s.correct_spend_movement(
                movement=movement,
                replacement=None,
                **(controls | {"actor": self.owner, "reference": "RACE-REVERSE"}),
            ),
            lambda: s.convert_spend(
                spend=spend,
                from_kind="open_commitment",
                to_kind="incurred",
                amount="10000000",
                **(controls | {"reference": "RACE-MORE"}),
            ),
        )
        self.assertCountEqual(outcomes, ["committed", "rejected"])
        self.assertEqual(self.position(spend)["consumed"], "80000000.00")


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL row locks")
class SpendPaymentConcurrencyTests(SpendPaymentFixtures, TransactionTestCase):
    race = race_fixtures.TaxMoneyConcurrencyTests.race

    def test_competing_matches_cannot_consume_the_same_source_capacity(self):
        spend, bill, reported, payment = self.payment_case()
        controls = self.controls(spend, date_value=date(2026, 10, 1))
        outcomes = self.race(
            *[
                lambda n=n: s.match_spend_payment(
                    spend=spend,
                    manual_source_key=f"movement:{reported.pk}",
                    finance_event_id=f"bill_payment:{payment.pk}",
                    amount="20000000",
                    **(controls | {"reference": f"RACE-MATCH-{n}"}),
                )
                for n in range(2)
            ]
        )
        self.assertCountEqual(outcomes, ["committed", "rejected"])
        self.assertEqual(
            BudgetPaymentMatch.objects.filter(voided_at__isnull=True).count(), 1
        )
        self.assertEqual(self.position(spend)["payments"], "40000000.00")
