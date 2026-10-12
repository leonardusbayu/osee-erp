"""Independent public budget recovery/capacity probes, synthetic sources only."""

import json
from datetime import date, datetime

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase
from django.utils import timezone

from core.models import Membership, Organization
from finance import services as ledger
from finance import tests_budget_link as bill_fixtures
from finance import tests_tax_money as money_fixtures
from finance.models import BankAccount, BankTransaction, Journal

from . import services as planning
from . import spend_services as spending
from .budget_reporting import budget_summary
from .spend_models import BudgetPaymentMatch, BudgetSpendMovement, record_digest


class SpendIndependentTests(money_fixtures.TaxMoneyFixtures, TestCase):
    # Explicit fixture aliases do not inherit another suite's test_* methods.
    bill = bill_fixtures.BillBudgetLinkTests.bill
    no_withholding = bill_fixtures.BillBudgetLinkTests.no_withholding
    facts = bill_fixtures.BillBudgetLinkTests.facts
    spend = bill_fixtures.BillBudgetLinkTests.spend
    link = bill_fixtures.BillBudgetLinkTests.link
    position = bill_fixtures.BillBudgetLinkTests.position
    payment = bill_fixtures.BillBudgetLinkTests.payment

    def current(self, spend):
        spend.refresh_from_db()
        return spending.spend_position(
            organization=self.org, spend=spend, as_of=timezone.localdate()
        )

    def action(self, spend, reference, day=None):
        row = self.current(spend)
        return dict(
            organization=self.org,
            actor=self.actor,
            date=day or self.day,
            reference=reference,
            reason="Synthetic documented recovery or matching decision.",
            evidence="Synthetic original source reviewed.",
            expected_version=spend.version,
            expected_fingerprint=row["fingerprint"],
        )

    def linked_payment(self):
        bill = self.bill()
        self.no_withholding(bill)
        payment = self.payment(bill, "20000000", reference="INDEPENDENT-BANK-PAY")
        budget, entry, spend = self.spend()
        self.link(spend, entry, bill, reference="INDEPENDENT-LINK")
        return bill, payment, budget, entry, spend

    def manual_payment(
        self, spend, amount="20000000", *, reference="INDEPENDENT-MANUAL", day=None
    ):
        return spending.record_spend_payment(
            spend=spend, amount=amount, **self.action(spend, reference, day)
        )

    def match(
        self,
        spend,
        payment,
        manual,
        amount="20000000",
        *,
        reference="INDEPENDENT-MATCH",
        day=None,
        guard=None,
    ):
        return spending.match_spend_payment(
            spend=spend,
            manual_source_key=f"movement:{manual.pk}",
            finance_event_id=f"bill_payment:{payment.pk}",
            amount=amount,
            **(guard or self.action(spend, reference, day)),
        )

    def test_used_conversion_cannot_be_reversed_until_its_bill_link_is_withdrawn(self):
        budget, entry, spend = self.spend()
        conversion = spending.convert_spend(
            spend=spend,
            from_kind="reservation",
            to_kind="incurred",
            amount="20000000",
            **self.action(spend, "INDEPENDENT-CONVERT"),
        )
        bill = self.bill(amount="20000000")
        row = self.current(spend)
        link = spending.link_spend_bill(
            organization=self.org,
            actor=self.actor,
            spend=spend,
            bill=bill,
            coverage=[
                {"source_key": f"movement:{conversion.pk}", "amount": "20000000"}
            ],
            expected_bill_fingerprint=self.facts(bill)["fingerprint"],
            **{
                k: v
                for k, v in self.action(spend, "INDEPENDENT-CONVERT-LINK").items()
                if k not in {"organization", "actor"}
            },
        )
        self.assertTrue(row["current"])
        journal_rows = list(Journal.objects.order_by("pk").values())
        before = BudgetSpendMovement.objects.count()
        with self.assertRaises(ValidationError):
            spending.correct_spend_movement(
                movement=conversion,
                replacement=None,
                **self.action(spend, "INDEPENDENT-USED-REVERSE"),
            )
        self.assertEqual(BudgetSpendMovement.objects.count(), before)
        spending.void_spend_bill_link(
            link=link, **self.action(spend, "INDEPENDENT-LINK-VOID", date(2026, 9, 20))
        )
        spending.correct_spend_movement(
            movement=conversion,
            replacement=None,
            **self.action(spend, "INDEPENDENT-FREE-REVERSE", date(2026, 9, 20)),
        )
        result = self.position(budget)
        self.assertEqual(
            (result["incurred"], result["reservations"], result["consumed"]),
            ("0.00", "80000000.00", "80000000.00"),
        )
        self.assertEqual(list(Journal.objects.order_by("pk").values()), journal_rows)

    def test_matched_source_and_bill_link_are_protected_until_match_is_withdrawn(self):
        _, payment, budget, _, spend = self.linked_payment()
        manual = self.manual_payment(spend)
        match = self.match(spend, payment, manual)
        link = spend.bill_links.get(voided_at__isnull=True)
        before = BudgetSpendMovement.objects.count()
        with self.assertRaises(ValidationError):
            spending.correct_spend_movement(
                movement=manual,
                replacement=None,
                **self.action(spend, "INDEPENDENT-MATCHED-REVERSE"),
            )
        with self.assertRaises(ValidationError):
            spending.void_spend_bill_link(
                link=link, **self.action(spend, "INDEPENDENT-MATCHED-LINK-VOID")
            )
        self.assertEqual(BudgetSpendMovement.objects.count(), before)
        spending.void_spend_payment_match(
            match=match,
            **self.action(spend, "INDEPENDENT-MATCH-VOID", date(2026, 9, 20)),
        )
        spending.correct_spend_movement(
            movement=manual,
            replacement=None,
            **self.action(spend, "INDEPENDENT-MANUAL-VOID", date(2026, 9, 20)),
        )
        result = self.position(budget)
        self.assertEqual(
            (result["reported_payments"], result["verified_cash"], result["payments"]),
            ("0.00", "20000000.00", "20000000.00"),
        )

    def test_multiple_matches_share_exact_bank_capacity_and_no_third_match_fits(self):
        _, payment, _, _, spend = self.linked_payment()
        first = self.manual_payment(spend, reference="INDEPENDENT-MANUAL-A")
        second = self.manual_payment(spend, reference="INDEPENDENT-MANUAL-B")
        self.match(
            spend, payment, first, amount="10000000", reference="INDEPENDENT-MATCH-A"
        )
        self.match(
            spend, payment, second, amount="10000000", reference="INDEPENDENT-MATCH-B"
        )
        count = BudgetPaymentMatch.objects.count()
        with self.assertRaises(ValidationError):
            self.match(
                spend, payment, first, amount="1", reference="INDEPENDENT-MATCH-C"
            )
        self.assertEqual(BudgetPaymentMatch.objects.count(), count)
        row = self.current(spend)
        self.assertEqual(
            (
                row["reported_payments"],
                row["unmatched_reported_payments"],
                row["verified_cash"],
                row["payments"],
            ),
            ("40000000.00", "20000000.00", "20000000.00", "40000000.00"),
        )
        self.assertEqual(row["finance_payment_sources"][0]["unmatched"], "0.00")

    def test_same_amount_changed_source_identity_rejects_stale_match_intent(self):
        _, payment, budget, _, spend = self.linked_payment()
        manual = self.manual_payment(spend)
        viewed = self.action(spend, "INDEPENDENT-STALE-IDENTITY")
        self.account.account_number = "INDEPENDENT-NEW-ACCOUNT-IDENTITY"
        self.account.save()
        before = BudgetPaymentMatch.objects.count()
        with self.assertRaises(ValidationError):
            self.match(spend, payment, manual, guard=viewed)
        self.assertEqual(BudgetPaymentMatch.objects.count(), before)
        self.assertEqual(self.position(budget)["verified_cash"], "20000000.00")
        self.assertNotEqual(
            viewed["expected_fingerprint"], self.current(spend)["fingerprint"]
        )

    def test_match_withdrawal_changes_only_matching_from_its_effective_date(self):
        _, payment, budget, _, spend = self.linked_payment()
        manual = self.manual_payment(spend, day=date(2026, 9, 20))
        match = self.match(spend, payment, manual, day=date(2026, 9, 25))
        original_bank_journals = list(Journal.objects.order_by("pk").values())
        spending.void_spend_payment_match(
            match=match,
            **self.action(spend, "INDEPENDENT-DATED-VOID", date(2026, 10, 1)),
        )
        before = spending.budget_position(
            organization=self.org, budget=budget, as_of=date(2026, 9, 24)
        )
        matched = spending.budget_position(
            organization=self.org, budget=budget, as_of=date(2026, 9, 30)
        )
        after = spending.budget_position(
            organization=self.org, budget=budget, as_of=date(2026, 10, 1)
        )
        self.assertEqual(
            (
                before["unmatched_reported_payments"],
                matched["unmatched_reported_payments"],
                after["unmatched_reported_payments"],
            ),
            ("20000000.00", "0.00", "20000000.00"),
        )
        self.assertEqual(
            {r["verified_cash"] for r in (before, matched, after)}, {"20000000.00"}
        )
        self.assertEqual(
            {r["consumed"] for r in (before, matched, after)}, {"80000000.00"}
        )
        self.assertEqual(
            list(Journal.objects.order_by("pk").values()), original_bank_journals
        )

    def test_foreign_manual_match_pointer_fails_closed_even_with_recomputed_hash(self):
        _, payment, budget, _, spend = self.linked_payment()
        manual = self.manual_payment(spend)
        match = self.match(spend, payment, manual)
        local_org = self.org
        foreign = Organization.objects.create(
            name="Synthetic foreign independent spend"
        )
        Membership.objects.create(organization=foreign, user=self.actor, role="owner")
        self.org = foreign
        try:
            _, _, foreign_spend = self.spend(amount="20000000")
            foreign_manual = self.manual_payment(
                foreign_spend, reference="FOREIGN-INDEPENDENT-MANUAL"
            )
        finally:
            self.org = local_org
        match.manual_movement = foreign_manual
        digest = record_digest(match)
        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {connection.ops.quote_name(BudgetPaymentMatch._meta.db_table)} SET manual_movement_id=%s, content_hash=%s WHERE id=%s",
                [foreign_manual.pk, digest, match.pk],
            )
        result = self.position(budget)
        self.assertFalse(result["current"])
        self.assertIsNone(result["verified_cash"])
        summary = budget_summary(organization=self.org, year=2026)
        self.assertFalse(summary["current"])
        self.assertEqual(summary["status"], "review_required")
        self.assertIsNone(summary["consumed"])

    def test_annual_summary_never_reports_partial_total_when_one_budget_source_is_unknown(
        self,
    ):
        budget, entry, _ = self.spend()
        second, _, _ = self.spend(amount="10000000")
        good = budget_summary(organization=self.org, year=2026)
        self.assertEqual(
            (good["approved_count"], good["amount"], good["consumed"]),
            (2, "90000000.00", "90000000.00"),
        )
        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {connection.ops.quote_name(entry._meta.db_table)} SET description=%s WHERE id=%s",
                ["Changed original document identity with the same amount", entry.pk],
            )
        broken = budget_summary(organization=self.org, year=2026)
        self.assertFalse(broken["current"])
        self.assertIsNone(broken["consumed"])
        self.assertIsNone(broken["verified_cash"])
        self.assertEqual(self.position(second)["consumed"], "10000000.00")
        self.assertEqual(self.position(budget)["amount"], "80000000.00")

    def test_invalid_organization_and_exact_date_boundaries_do_not_become_empty_budget(
        self,
    ):
        _, _, spend = self.spend()
        for value in (datetime(2026, 9, 10), "2026-9-10", True):
            with self.assertRaises(ValidationError):
                spending.spend_position(organization=self.org, spend=spend, as_of=value)
        with self.assertRaises(ValidationError):
            budget_summary(organization=987654321, year=2026)

    def test_malformed_movement_payload_with_valid_digest_is_unknown_not_reader_crash(
        self,
    ):
        budget, _, spend = self.spend()
        movement = spending.convert_spend(
            spend=spend,
            from_kind="reservation",
            to_kind="incurred",
            amount="20000000",
            **self.action(spend, "INDEPENDENT-MALFORMED-CONVERT"),
        )
        movement.payload = ["Synthetic malformed stored importer record"]
        digest = record_digest(movement)
        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {connection.ops.quote_name(BudgetSpendMovement._meta.db_table)} SET payload=%s, content_hash=%s WHERE id=%s",
                [json.dumps(movement.payload), digest, movement.pk],
            )
        readers = {
            "spend": lambda: self.current(spend),
            "budget": lambda: self.position(budget),
            "annual": lambda: budget_summary(organization=self.org, year=2026),
        }
        for name, read in readers.items():
            with self.subTest(reader=name):
                result = read()
                self.assertFalse(result["current"])
                self.assertIsNone(result["consumed"])
                self.assertIsNone(result["verified_cash"])
                self.assertTrue(result["issues"])
                self.assertTrue(
                    any(issue.get("action_label") for issue in result["issues"])
                )

    def test_smaller_approved_linked_draft_does_not_silently_release_estimate_residual(
        self,
    ):
        bill = self.bill(approved=False)
        budget, entry, spend = self.spend(amount="60000000")
        link = self.link(spend, entry, bill, reference="INDEPENDENT-SHRINK-LINK")
        original_estimate = entry.amount
        # Draft amounts are editable through the normal scoped model contract;
        # Finance approval must preserve the real Rp40m source, not inflate it.
        bill.amount = "40000000"
        bill.save()
        ledger.approve_bill(organization=self.org, actor=self.actor, bill=bill)
        self.assertEqual(self.facts(bill)["incurred"], "40000000.00")
        result = self.position(budget)
        if result["current"]:
            self.assertEqual(
                (result["incurred"], result["reservations"], result["consumed"]),
                ("40000000.00", "20000000.00", "60000000.00"),
            )
        else:
            self.assertIsNone(result["consumed"])
            self.assertTrue(result["tasks"])
            self.assertTrue(any(task.get("action_label") for task in result["tasks"]))
        entry.refresh_from_db()
        link.refresh_from_db()
        self.assertEqual(entry.amount, original_estimate)
        self.assertEqual(
            link.coverage,
            [{"source_key": f"entry:{entry.pk}", "amount": "60000000.00"}],
        )

    def test_foreign_organization_on_legacy_payment_is_unknown_not_silently_dropped(
        self,
    ):
        budget = planning.create_budget(
            organization=self.org,
            actor=self.actor,
            title="Synthetic legacy payment integrity",
            year=2026,
            month=9,
            channel="meta_ads",
            amount="80000000",
        )
        budget = planning.transition_budget(
            organization=self.org,
            actor=self.actor,
            budget=budget,
            action="submit",
            expected_version=budget.version,
        )
        budget = planning.transition_budget(
            organization=self.org,
            actor=self.actor,
            budget=budget,
            action="approve",
            expected_version=budget.version,
            notes="Synthetic original authorization.",
            self_approval_reason="Synthetic single-owner documented approval exception.",
        )
        origin = planning.add_budget_entry(
            organization=self.org,
            actor=self.actor,
            budget=budget,
            kind="open_commitment",
            amount="80000000",
            date=date(2026, 9, 8),
            description="Synthetic original commitment",
            idempotency_key="independent-legacy-origin",
        )
        payment = planning.record_budget_payment(
            organization=self.org,
            actor=self.actor,
            entry=origin,
            amount="30000000",
            date=date(2026, 9, 9),
            description="Synthetic original reported payment",
            reference="INDEPENDENT-LEGACY-PAYMENT",
            idempotency_key="independent-legacy-payment",
        )
        before = spending.budget_position(
            organization=self.org, budget=budget, as_of=timezone.localdate()
        )
        spend = spending.adopt_budget_entry(
            organization=self.org,
            actor=self.actor,
            entry=origin,
            date=self.day,
            reference="INDEPENDENT-LEGACY-ADOPTION",
            reason="Preserve the original commitment and payment history.",
            evidence="Synthetic original manual documents reviewed.",
            expected_version=budget.version,
            expected_fingerprint=before["fingerprint"],
        )
        self.assertEqual(self.current(spend)["reported_payments"], "30000000.00")
        foreign = Organization.objects.create(
            name="PRIVATE-FOREIGN-LEGACY-DO-NOT-DISPLAY"
        )
        # Reproduce a damaged imported child FK; public writes prohibit it.
        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {connection.ops.quote_name(payment._meta.db_table)} SET organization_id=%s WHERE id=%s",
                [foreign.pk, payment.pk],
            )
        for name, read in {
            "spend": lambda: self.current(spend),
            "budget": lambda: self.position(budget),
            "annual": lambda: budget_summary(organization=self.org, year=2026),
        }.items():
            with self.subTest(reader=name):
                result = read()
                self.assertFalse(result["current"])
                self.assertIsNone(result["reported_payments"])
                self.assertIsNone(result["consumed"])
                self.assertTrue(
                    any(issue.get("action_label") for issue in result["issues"])
                )
                self.assertNotIn(foreign.name, json.dumps(result))

    def test_changed_unavailable_bank_identity_changes_repair_fingerprint_without_foreign_details(
        self,
    ):
        _, payment, _, _, spend = self.linked_payment()
        foreign = Organization.objects.create(
            name="PRIVATE-FOREIGN-BANK-DO-NOT-DISPLAY"
        )
        accounts = [
            BankAccount.objects.create(
                organization=foreign,
                name=f"PRIVATE-FOREIGN-ACCOUNT-{suffix}",
                account_number=f"PRIVATE-FOREIGN-NUMBER-{suffix}",
            )
            for suffix in ("A", "B")
        ]
        positions = []
        for account in accounts:
            # Both damaged pointers yield the same friendly scope error and
            # monetary amount; the private source identity nevertheless changes.
            with connection.cursor() as cursor:
                cursor.execute(
                    f"UPDATE {connection.ops.quote_name(BankTransaction._meta.db_table)} SET account_id=%s WHERE id=%s",
                    [account.pk, payment.transaction_id],
                )
            result = self.current(spend)
            self.assertFalse(result["current"])
            self.assertIsNone(result["verified_cash"])
            self.assertIsNone(result["consumed"])
            self.assertTrue(result["issues"])
            serialized = json.dumps(result)
            self.assertNotIn(foreign.name, serialized)
            for private in accounts:
                self.assertNotIn(private.name, serialized)
                self.assertNotIn(private.account_number, serialized)
            positions.append(result)
        self.assertEqual(positions[0]["issues"], positions[1]["issues"])
        self.assertNotEqual(positions[0]["fingerprint"], positions[1]["fingerprint"])
