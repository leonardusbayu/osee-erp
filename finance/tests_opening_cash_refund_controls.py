"""Public refund permissions, retries, cumulative limits and dated principal."""

from copy import deepcopy
from datetime import date
from decimal import Decimal
from uuid import uuid4

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase

from core.models import Membership, Organization

from . import opening_balances as opening
from . import services as ledger
from .models import (
    AccountingPeriod,
    BankPosting,
    BankTransaction,
    Journal,
    OpeningBalanceCancellation,
    OpeningBalanceCorrection,
    OpeningBalanceRefund,
)
from .tests_opening_balance_corrections import OpeningCorrectionFixtures


class OpeningRefundControlFixtures(OpeningCorrectionFixtures):
    def matched_receipt(self, *, amount="120"):
        item = self.item()
        bank = self.movement(amount, reference=f"SYN-REFUND-CONTROL-ORIGINAL-{uuid4()}")
        return item, bank, self.settle(item, bank, amount=amount)

    def refund_data(self, cash, bank, *, amount="40", **changes):
        data = dict(
            organization=self.org,
            actor=self.actor,
            settlement=cash,
            transaction=bank,
            amount=amount,
            reason="Synthetic independently confirmed actual return of original received cash.",
            evidence="Synthetic original receipt and distinct opposite bank evidence.",
            request_key=str(uuid4()),
            confirmed=True,
        )
        data.update(changes)
        data.setdefault(
            "expected_fingerprint",
            opening.opening_balance_action_context(
                organization=data["organization"],
                action="refund",
                settlement=data["settlement"],
                transaction=data["transaction"],
                amount=data["amount"],
            )["fingerprint"],
        )
        return data

    def refund_counts(self):
        return (
            *self.counts(),
            OpeningBalanceRefund.objects.count(),
            OpeningBalanceCancellation.objects.count(),
            OpeningBalanceCorrection.objects.count(),
        )


class OpeningCashRefundControlTests(OpeningRefundControlFixtures, TestCase):
    def test_active_role_precedes_lookup_and_retry_and_foreign_sources_write_nothing(
        self,
    ):
        item, bank, cash = self.matched_receipt()
        returned = self.movement(
            "-40", day=date(2026, 9, 15), reference="SYN-REFUND-ROLE-RETURN"
        )
        data = self.refund_data(cash, returned)
        for role in ("marketing", "reviewer", "auditor", "director"):
            actor = self.role(role)
            for selected in (cash, 999999999):
                counts = self.refund_counts()
                with (
                    self.subTest(role=role, source=getattr(selected, "pk", selected)),
                    self.assertRaises(PermissionDenied),
                ):
                    opening.refund_opening_settlement(
                        **{**data, "actor": actor, "settlement": selected}
                    )
                self.assertEqual(self.refund_counts(), counts)
        counts = self.refund_counts()
        with self.assertRaises(PermissionDenied):
            opening.refund_opening_settlement(**{**data, "actor": None})
        foreign = Organization.objects.create(name="Synthetic unrelated refund company")
        Membership.objects.create(organization=foreign, user=self.actor, role="owner")
        with self.assertRaises(ValidationError):
            opening.refund_opening_settlement(**{**data, "organization": foreign})
        self.assertEqual(self.refund_counts(), counts)
        finance = self.role("finance")
        data = self.refund_data(cash, returned, actor=finance)
        event = opening.refund_opening_settlement(**data)
        Membership.objects.filter(organization=self.org, user=finance).update(
            is_active=False
        )
        counts = self.refund_counts()
        for changes in ({}, {"settlement": 999999999}):
            with self.subTest(changes=changes), self.assertRaises(PermissionDenied):
                opening.refund_opening_settlement(**{**data, **changes})
            self.assertEqual(self.refund_counts(), counts)
        self.assertEqual(
            (
                event.amount,
                self.position(item, as_of=date(2026, 9, 15))["remaining"],
                bank.unallocated_amount,
                returned.unallocated_amount,
            ),
            (Decimal(40), Decimal(220), Decimal(0), Decimal(0)),
        )

    def test_confirmation_exact_closed_period_retry_and_changed_uuid_payload_preserve_cash(
        self,
    ):
        item, bank, cash = self.matched_receipt()
        returned = self.movement(
            "-40", day=date(2026, 9, 15), reference="SYN-REFUND-REPLAY-RETURN"
        )
        data = self.refund_data(cash, returned)
        counts = self.refund_counts()
        with self.assertRaises(ValidationError):
            opening.refund_opening_settlement(**{**data, "confirmed": False})
        for value in ("0", "-1", "40.001", "NaN"):
            with self.subTest(amount=value), self.assertRaises(ValidationError):
                opening.refund_opening_settlement(**{**data, "amount": value})
        with self.assertRaises(TypeError):
            opening.refund_opening_settlement(**{**data, "date": date(2026, 9, 20)})
        self.assertEqual(self.refund_counts(), counts)
        event = opening.refund_opening_settlement(**data)
        proof = deepcopy(
            opening.opening_refund_source_snapshot(organization=self.org, refund=event)
        )
        AccountingPeriod.objects.update_or_create(
            organization=self.org, year=2026, month=9, defaults={"closed": True}
        )
        counts = self.refund_counts()
        self.assertEqual(opening.refund_opening_settlement(**data).pk, event.pk)
        self.assertEqual(self.refund_counts(), counts)
        for changes in (
            {"reason": "Synthetic different instruction"},
            {"evidence": "Synthetic different evidence"},
            {"amount": "39"},
            {"expected_fingerprint": "0" * 64},
            {"request_key": str(uuid4())},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                opening.refund_opening_settlement(**{**data, **changes})
            self.assertEqual(self.refund_counts(), counts)
        self.assertEqual(
            opening.opening_refund_source_snapshot(organization=self.org, refund=event),
            proof,
        )
        self.assertEqual(
            (
                self.position(item, as_of=date(2026, 9, 30))["remaining"],
                bank.unallocated_amount,
                returned.unallocated_amount,
            ),
            (Decimal(220), Decimal(0), Decimal(0)),
        )
        self.assertEqual(
            (
                OpeningBalanceRefund.objects.count(),
                BankPosting.objects.count(),
                BankTransaction.objects.count(),
            ),
            (1, 2, 2),
        )

    def test_partial_ar_and_ap_returns_exhaust_each_original_without_releasing_original_bank(
        self,
    ):
        receivable = self.item()
        payable = self.item(kind="payable", party=self.supplier, amount="200")
        receipt_bank = self.movement("120", reference="SYN-REFUND-LIMIT-AR-ORIGINAL")
        payment_bank = self.movement("-50", reference="SYN-REFUND-LIMIT-AP-ORIGINAL")
        receipt = self.settle(receivable, receipt_bank)
        payment = self.settle(payable, payment_bank, amount="50")
        originals = [
            deepcopy(
                opening.opening_settlement_source_snapshot(
                    organization=self.org, settlement=cash
                )
            )
            for cash in (receipt, payment)
        ]
        cases = (
            (receipt, "-40", "40", date(2026, 9, 15)),
            (receipt, "-80", "80", date(2026, 9, 18)),
            (payment, "20", "20", date(2026, 9, 15)),
            (payment, "30", "30", date(2026, 9, 18)),
        )
        for cash, signed, amount, day in cases:
            returned = self.movement(
                signed, day=day, reference=f"SYN-REFUND-LIMIT-RETURN-{uuid4()}"
            )
            opening.refund_opening_settlement(
                **self.refund_data(cash, returned, amount=amount)
            )
        for cash, signed in ((receipt, "-1"), (payment, "1")):
            returned = self.movement(
                signed,
                day=date(2026, 9, 20),
                reference=f"SYN-REFUND-LIMIT-EXCESS-{uuid4()}",
            )
            counts = self.refund_counts()
            with self.subTest(original=cash.pk), self.assertRaises(ValidationError):
                opening.refund_opening_settlement(
                    **self.refund_data(cash, returned, amount="1")
                )
            self.assertEqual(self.refund_counts(), counts)
            self.assertEqual(returned.unallocated_amount, Decimal(signed))
        self.assertEqual(
            (
                self.position(receivable, as_of=date(2026, 9, 30))["remaining"],
                self.position(payable, as_of=date(2026, 9, 30))["remaining"],
                receipt_bank.unallocated_amount,
                payment_bank.unallocated_amount,
            ),
            (Decimal(300), Decimal(200), Decimal(0), Decimal(0)),
        )
        balances = ledger.report_balances(
            organization=self.org, as_of=date(2026, 9, 30)
        )
        self.assertEqual(
            tuple(
                balances[key]
                for key in ("BANK", "AR", "AP", "EQUITY", "REVENUE", "EXPENSE")
            ),
            (
                Decimal(0),
                Decimal(300),
                Decimal(-200),
                Decimal(-100),
                Decimal(0),
                Decimal(0),
            ),
        )
        self.assertEqual(
            (
                OpeningBalanceRefund.objects.count(),
                BankPosting.objects.count(),
                Journal.objects.count(),
            ),
            (4, 6, 8),
        )
        for cash, proof in zip((receipt, payment), originals):
            self.assertEqual(
                opening.opening_settlement_source_snapshot(
                    organization=self.org, settlement=cash
                ),
                proof,
            )
        self.assertFalse(OpeningBalanceCancellation.objects.exists())

    def test_later_refund_cannot_fund_earlier_receipt_and_correction_cannot_break_later_paid_prefix(
        self,
    ):
        item, bank, cash = self.matched_receipt()
        original = deepcopy(
            opening.opening_settlement_source_snapshot(
                organization=self.org, settlement=cash
            )
        )
        returned = self.movement(
            "-40", day=date(2026, 9, 15), reference="SYN-REFUND-DATES-RETURN"
        )
        opening.refund_opening_settlement(**self.refund_data(cash, returned))
        too_early = self.movement(
            "200", day=date(2026, 9, 14), reference="SYN-REFUND-DATES-EARLY-CASH"
        )
        counts = self.refund_counts()
        with self.assertRaises(ValidationError):
            self.settle(item, too_early, amount="200")
        self.assertEqual(self.refund_counts(), counts)
        self.assertEqual(
            (
                self.position(item, as_of=date(2026, 9, 14))["remaining"],
                self.position(item, as_of=date(2026, 9, 15))["remaining"],
                too_early.unallocated_amount,
            ),
            (Decimal(180), Decimal(220), Decimal(200)),
        )
        later = self.movement(
            "200", day=date(2026, 9, 18), reference="SYN-REFUND-DATES-LATER-CASH"
        )
        self.settle(item, later, amount="200")
        counts = self.refund_counts()
        with self.assertRaises(ValidationError):
            opening.correct_opening_balance(
                **self.correction_data(
                    item, date=date(2026, 9, 16), direction="decrease", amount="30"
                )
            )
        self.assertEqual(self.refund_counts(), counts)
        self.assertEqual(
            (
                self.position(item, as_of=date(2026, 9, 16))["remaining"],
                self.position(item, as_of=date(2026, 9, 18))["remaining"],
                later.unallocated_amount,
            ),
            (Decimal(220), Decimal(20), Decimal(0)),
        )
        balances = ledger.report_balances(
            organization=self.org, as_of=date(2026, 9, 18)
        )
        self.assertEqual(
            tuple(
                balances[key]
                for key in ("BANK", "AR", "AP", "EQUITY", "REVENUE", "EXPENSE")
            ),
            (
                Decimal(280),
                Decimal(20),
                Decimal(0),
                Decimal(-300),
                Decimal(0),
                Decimal(0),
            ),
        )
        self.assertEqual(
            opening.opening_settlement_source_snapshot(
                organization=self.org, settlement=cash
            ),
            original,
        )
        self.assertFalse(OpeningBalanceCorrection.objects.exists())
