"""Public dated correction contracts; original principal receipts stay intact."""

from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.test import TestCase
from django.utils import timezone

from core.models import Membership, Organization

from . import opening_balances as opening
from . import services as ledger
from . import tests_opening_balances as fixtures
from .models import AccountingPeriod, Journal, JournalLine, OpeningBalanceCorrection


class OpeningCorrectionFixtures(fixtures.OpeningBalanceFixtures):
    def correction_data(self, item, **changes):
        data = dict(
            organization=self.org,
            actor=self.actor,
            item=item,
            date=date(2026, 9, 15),
            direction="increase",
            amount="100",
            reason="Synthetic original document proves additional outstanding principal.",
            evidence="Synthetic signed reconciliation of old principal; no new sale or cash.",
            request_key=str(uuid4()),
            confirmed=True,
        )
        data.update(changes)
        data.setdefault(
            "expected_fingerprint",
            opening.opening_balance_action_context(
                organization=data["organization"],
                action="correct",
                item=data["item"],
                date=data["date"],
                direction=data["direction"],
                amount=data["amount"],
            )["fingerprint"],
        )
        return data

    def correct(self, item, **changes):
        return opening.correct_opening_balance(**self.correction_data(item, **changes))


class OpeningCorrectionTests(OpeningCorrectionFixtures, TestCase):
    def test_owner_adds_dated_principal_without_rewriting_original_or_creating_sales_or_cash(
        self,
    ):
        item = self.item()
        original = opening.opening_balance_source_snapshot(
            organization=self.org, item=item
        )
        event = self.correct(item)
        before = opening.opening_balance_position(
            organization=self.org, item=item, as_of=date(2026, 9, 14)
        )
        after = opening.opening_balance_position(
            organization=self.org, item=item, as_of=date(2026, 9, 15)
        )
        self.assertEqual(
            (before["opening"], before["remaining"], before["correction_delta"]),
            (Decimal(300), Decimal(300), Decimal(0)),
        )
        self.assertEqual(
            (
                after["original_opening"],
                after["opening"],
                after["remaining"],
                after["correction_delta"],
            ),
            (Decimal(300), Decimal(400), Decimal(400), Decimal(100)),
        )
        self.assertEqual(
            [
                (entry.action, entry.direction, entry.amount)
                for entry in after["corrections"]
            ],
            [("correct", "increase", Decimal(100))],
        )
        self.assertEqual(
            opening.opening_balance_source_snapshot(organization=self.org, item=item),
            original,
        )
        receipt = opening.opening_correction_source_snapshot(
            organization=self.org, correction=event
        )
        self.assertEqual(
            (receipt["item_id"], receipt["direction"], receipt["date"]),
            (item.pk, "increase", "2026-09-15"),
        )
        balances = ledger.report_balances(
            organization=self.org, as_of=date(2026, 9, 15)
        )
        self.assertEqual(
            (
                balances["AR"],
                balances["EQUITY"],
                balances["BANK"],
                balances["REVENUE"],
                balances["EXPENSE"],
            ),
            (Decimal(400), Decimal(-400), 0, 0, 0),
        )

    def test_damaged_and_orphan_correction_hold_unknown_and_track_every_repair_without_rewriting_original_cash(
        self,
    ):
        item = self.item()
        cash = self.settle(
            item, self.movement("120", reference="CORRECTION-ORIGINAL-CASH")
        )
        original = opening.opening_balance_source_snapshot(
            organization=self.org, item=item
        )
        original_cash = opening.opening_settlement_source_snapshot(
            organization=self.org, settlement=cash
        )
        original_graph = opening.opening_balance_source_graph(
            organization=self.org, item=item
        )
        cash_graph = opening.opening_settlement_source_graph(
            organization=self.org, settlement=cash
        )
        correction = self.correct(item)
        self.assertEqual(
            opening.opening_balance_source_snapshot(organization=self.org, item=item),
            original,
        )
        self.assertEqual(
            opening.opening_settlement_source_snapshot(
                organization=self.org, settlement=cash
            ),
            original_cash,
        )
        self.assertEqual(
            opening.opening_balance_source_graph(organization=self.org, item=item),
            original_graph,
        )
        self.assertEqual(
            opening.opening_settlement_source_graph(
                organization=self.org, settlement=cash
            ),
            cash_graph,
        )
        self.assertEqual(
            opening.opening_balance_position(
                organization=self.org, item=item, as_of=date(2026, 9, 15)
            )["remaining"],
            Decimal(280),
        )

        def damaged_amount(value):
            # Explicit persisted-source fault fixture: keep the journal
            # balanced while making it disagree with the sealed correction.
            for line in correction.journal.lines.all():
                self.corrupt(
                    JournalLine, line.pk, "debit" if line.debit else "credit", value
                )

        def unknown_sources():
            position = opening.opening_balance_position(
                organization=self.org, item=item, as_of=date(2026, 9, 30)
            )
            context = opening.opening_balance_action_context(
                organization=self.org,
                action="correct",
                item=item,
                date=date(2026, 9, 20),
                direction="decrease",
                amount="10",
            )
            close = opening.opening_balance_close_facts(
                organization=self.org, cutoff=date(2026, 9, 30)
            )
            for name, facts in (
                ("position", position),
                ("action", context),
                ("close", close),
            ):
                with self.subTest(boundary=name):
                    self.assertFalse(facts["current"])
                    self.assertTrue(facts["blockers"])
            self.assertEqual(
                (
                    position["opening"],
                    position["remaining"],
                    context["before"],
                    context["after"],
                    close["receivable"],
                    close["payable"],
                ),
                (None, None, None, None, None, None),
            )
            for snapshot, keyword, event in (
                (opening.opening_balance_source_snapshot, "item", item),
                (opening.opening_settlement_source_snapshot, "settlement", cash),
                (opening.opening_correction_source_snapshot, "correction", correction),
            ):
                with self.subTest(receipt=keyword), self.assertRaises(ValidationError):
                    snapshot(organization=self.org, **{keyword: event})
            return {
                "position": position["fingerprint"],
                "action": context["fingerprint"],
                "close": close["fingerprint"],
                "item_graph": opening.opening_balance_source_graph(
                    organization=self.org, item=item
                )["fingerprint"],
                "cash_graph": opening.opening_settlement_source_graph(
                    organization=self.org, settlement=cash
                )["fingerprint"],
                "correction_graph": opening.opening_correction_source_graph(
                    organization=self.org, correction=correction
                )["fingerprint"],
            }, (position["blockers"], context["blockers"], close["blockers"])

        damaged_amount("110")
        damaged, first_messages = unknown_sources()
        damaged_amount("120")
        repaired, next_messages = unknown_sources()
        self.assertEqual(first_messages, next_messages)
        for boundary in damaged:
            with self.subTest(damaged_repair=boundary):
                self.assertNotEqual(damaged[boundary], repaired[boundary])

        # External deletion leaves an orphan typed journal. This is an
        # adversarial fixture only; routine financial events cannot be deleted.
        table = connection.ops.quote_name(OpeningBalanceCorrection._meta.db_table)
        with connection.cursor() as cursor:
            cursor.execute(f"DELETE FROM {table} WHERE id = %s", [correction.pk])
        orphaned, first_messages = unknown_sources()
        damaged_amount("130")
        orphan_repair, next_messages = unknown_sources()
        self.assertEqual(first_messages, next_messages)
        for boundary in orphaned:
            with self.subTest(orphan_line_repair=boundary):
                self.assertNotEqual(orphaned[boundary], orphan_repair[boundary])
        self.corrupt(Journal, correction.journal_id, "date", date(2026, 9, 16))
        orphan_date_repair, date_messages = unknown_sources()
        self.assertEqual(next_messages, date_messages)
        for boundary in orphan_repair:
            with self.subTest(orphan_date_repair=boundary):
                self.assertNotEqual(
                    orphan_repair[boundary], orphan_date_repair[boundary]
                )

    def test_dated_corrections_preserve_later_cash_cannot_fund_earlier_cash_and_conserve_receivable_payable_zero(
        self,
    ):
        receivable = self.item(source_reference="DATED-CORRECTION-AR", amount="300")
        late_bank = self.movement(
            "250", day=date(2026, 9, 20), reference="DATED-LATER-AR-CASH"
        )
        self.settle(receivable, late_bank, amount="250")
        before = ledger.report_balances(organization=self.org)
        rejected = self.correction_data(
            receivable, date=date(2026, 9, 10), direction="decrease", amount="100"
        )
        preview = opening.opening_balance_action_context(
            organization=self.org,
            action="correct",
            item=receivable,
            date=date(2026, 9, 10),
            direction="decrease",
            amount="100",
        )
        self.assertTrue(preview["blockers"])
        with self.assertRaises(ValidationError):
            opening.correct_opening_balance(**rejected)
        self.assertEqual(ledger.report_balances(organization=self.org), before)
        self.assertEqual(
            opening.opening_balance_position(
                organization=self.org, item=receivable, as_of=date(2026, 9, 20)
            )["remaining"],
            Decimal(50),
        )

        future_principal = self.item(
            source_reference="DATED-FUTURE-PRINCIPAL", amount="100"
        )
        self.correct(future_principal, date=date(2026, 9, 15), amount="100")
        early_bank = self.movement(
            "150", day=date(2026, 9, 10), reference="DATED-EARLIER-AR-CASH"
        )
        before = ledger.report_balances(organization=self.org)
        with self.assertRaises(ValidationError):
            self.settle(future_principal, early_bank, amount="150")
        self.assertEqual(ledger.report_balances(organization=self.org), before)
        self.assertEqual(early_bank.unallocated_amount, Decimal(150))
        self.settle(future_principal, early_bank, amount="100")
        self.assertEqual(early_bank.unallocated_amount, Decimal(50))
        self.correct(
            future_principal, date=date(2026, 9, 20), direction="decrease", amount="100"
        )

        payable = self.item(
            kind="payable",
            party=self.supplier,
            source_reference="DATED-CORRECTION-AP",
            amount="200",
        )
        self.settle(
            payable,
            self.movement("-50", day=date(2026, 9, 10), reference="DATED-AP-CASH"),
            amount="50",
        )
        self.correct(payable, date=date(2026, 9, 15), amount="100")
        self.correct(
            payable, date=date(2026, 9, 20), direction="decrease", amount="250"
        )
        zero_principal = self.item(
            source_reference="DATED-PRINCIPAL-TO-ZERO", amount="40"
        )
        self.correct(
            zero_principal, date=date(2026, 9, 12), direction="decrease", amount="40"
        )

        # Independently worked dated positions: principal, actual cash, residual.
        positions = (
            (
                date(2026, 9, 9),
                (
                    ("300", "0", "300"),
                    ("100", "0", "100"),
                    ("200", "0", "200"),
                    ("40", "0", "40"),
                ),
            ),
            (
                date(2026, 9, 10),
                (
                    ("300", "0", "300"),
                    ("100", "100", "0"),
                    ("200", "50", "150"),
                    ("40", "0", "40"),
                ),
            ),
            (
                date(2026, 9, 14),
                (
                    ("300", "0", "300"),
                    ("100", "100", "0"),
                    ("200", "50", "150"),
                    ("0", "0", "0"),
                ),
            ),
            (
                date(2026, 9, 15),
                (
                    ("300", "0", "300"),
                    ("200", "100", "100"),
                    ("300", "50", "250"),
                    ("0", "0", "0"),
                ),
            ),
            (
                date(2026, 9, 20),
                (
                    ("300", "250", "50"),
                    ("100", "100", "0"),
                    ("50", "50", "0"),
                    ("0", "0", "0"),
                ),
            ),
        )
        books = {
            date(2026, 9, 9): ("0", "440", "-200", "-240"),
            date(2026, 9, 10): ("50", "340", "-150", "-240"),
            date(2026, 9, 14): ("50", "300", "-150", "-200"),
            date(2026, 9, 15): ("50", "400", "-250", "-200"),
            date(2026, 9, 20): ("300", "50", "0", "-350"),
        }
        for cutoff, expected_positions in positions:
            for item, expected in zip(
                (receivable, future_principal, payable, zero_principal),
                expected_positions,
            ):
                with self.subTest(cutoff=cutoff, item=item.source_reference):
                    facts = opening.opening_balance_position(
                        organization=self.org, item=item, as_of=cutoff
                    )
                    self.assertTrue(facts["current"])
                    self.assertEqual(
                        tuple(
                            facts[key] for key in ("opening", "settled", "remaining")
                        ),
                        tuple(map(Decimal, expected)),
                    )
            with self.subTest(books_at=cutoff):
                balances = ledger.report_balances(organization=self.org, as_of=cutoff)
                self.assertEqual(
                    tuple(balances[key] for key in ("BANK", "AR", "AP", "EQUITY")),
                    tuple(map(Decimal, books[cutoff])),
                )
                self.assertEqual(
                    tuple(
                        balances[key]
                        for key in (
                            "REVENUE",
                            "EXPENSE",
                            "DEFERRED_REVENUE",
                            "TAX_PAYABLE",
                        )
                    ),
                    (0, 0, 0, 0),
                )
                close = opening.opening_balance_close_facts(
                    organization=self.org, cutoff=cutoff
                )
                self.assertFalse(close["blockers"])
                self.assertEqual(
                    (close["receivable"], -close["payable"]),
                    (balances["AR"], balances["AP"]),
                )

    def test_correction_authorizes_before_exact_retry_and_rejects_changed_locked_or_invalid_requests_without_writes(
        self,
    ):
        item = self.item()
        data = self.correction_data(item)
        correction = opening.correct_opening_balance(**data)
        original = opening.opening_correction_source_snapshot(
            organization=self.org, correction=correction
        )

        def state():
            # Audit/domain counts are observable persistence guarantees of an
            # immutable command retry, alongside the public financial result.
            return (
                self.counts(),
                OpeningBalanceCorrection.objects.count(),
                ledger.report_balances(organization=self.org),
            )

        finance = self.role("finance")
        foreign_org = Organization.objects.create(
            name="Synthetic correction scope boundary"
        )
        foreign_owner = get_user_model().objects.create_user(
            username=f"foreign-correction-owner-{uuid4()}"
        )
        Membership.objects.create(
            organization=foreign_org, user=foreign_owner, role="owner"
        )
        before = state()
        for actor in (None, finance, foreign_owner):
            with (
                self.subTest(unauthorized_actor=getattr(actor, "pk", None)),
                self.assertRaises(PermissionDenied),
            ):
                opening.correct_opening_balance(**{**data, "actor": actor})
            self.assertEqual(state(), before)
        Membership.all_objects.filter(organization=self.org, user=self.actor).update(
            is_active=False
        )
        with self.assertRaises(PermissionDenied):
            opening.correct_opening_balance(**data)
        self.assertEqual(state(), before)
        Membership.all_objects.filter(organization=self.org, user=self.actor).update(
            is_active=True
        )
        with self.assertRaises(ValidationError):
            opening.correct_opening_balance(
                **{**data, "organization": foreign_org, "actor": foreign_owner}
            )
        self.assertEqual(state(), before)

        for change in (
            {"amount": "101"},
            {"direction": "decrease"},
            {"date": date(2026, 9, 16)},
            {"reason": "Different correction decision"},
            {"evidence": "Different underlying proof"},
        ):
            with (
                self.subTest(changed_same_key=change),
                self.assertRaises(ValidationError),
            ):
                opening.correct_opening_balance(**{**data, **change})
            self.assertEqual(state(), before)

        invalid_requests = (
            {"date": timezone.localdate() + timedelta(days=1)},
            {"date": date(2026, 8, 31)},
            {"confirmed": False},
            {"direction": "replace"},
            {"amount": "0"},
            {"amount": "-1"},
            {"amount": "0.001"},
            {"amount": "NaN"},
            {"amount": True},
            {"amount": 1.5},
            {"evidence": "   "},
            {"reason": ""},
            {"expected_fingerprint": data["expected_fingerprint"]},
        )
        for change in invalid_requests:
            fresh = self.correction_data(
                item, **{"date": date(2026, 9, 16), "amount": "10", **change}
            )
            with (
                self.subTest(invalid_request=change),
                self.assertRaises(ValidationError),
            ):
                opening.correct_opening_balance(**fresh)
            self.assertEqual(state(), before)

        AccountingPeriod.objects.update_or_create(
            organization=self.org, year=2026, month=9, defaults={"closed": True}
        )
        before = state()
        retry = opening.correct_opening_balance(**data)
        self.assertEqual(
            (retry.pk, retry.journal_id), (correction.pk, correction.journal_id)
        )
        self.assertEqual(state(), before)
        self.assertEqual(
            opening.opening_correction_source_snapshot(
                organization=self.org, correction=retry
            ),
            original,
        )
        for request in (
            self.correction_data(item, date=date(2026, 9, 17), amount="10"),
            {**data, "evidence": "Changed after closing"},
        ):
            with (
                self.subTest(locked_or_changed_key=request["request_key"]),
                self.assertRaises(ValidationError),
            ):
                opening.correct_opening_balance(**request)
            self.assertEqual(state(), before)
        self.assertEqual(
            opening.opening_balance_position(
                organization=self.org, item=item, as_of=date(2026, 9, 30)
            )["remaining"],
            Decimal(400),
        )
