"""Public synthetic Finance→Director journeys; no company/provider fixtures."""

from datetime import date, datetime
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.test import TestCase
from django.utils import timezone

from core.models import AuditEvent, Organization
from director import services as planning
from director import spend_services as spending
from director.spend_models import BudgetBillLink, BudgetSpendMovement
from taxes import workflow as work

from . import services as ledger
from . import tests_tax_money as money_fixtures
from .budget_adapter import bill_budget_facts, sync_bill_budget
from .models import (
    BankPosting,
    BillPayment,
    Journal,
    JournalLine,
    PrepaymentRelease,
    TaxRemittance,
)


class BillBudgetLinkTests(money_fixtures.TaxMoneyFixtures, TestCase):
    def bill(
        self, *, amount="60000000", prepaid=False, approved=True, vat="0", category=None
    ):
        row = ledger.create_bill(
            organization=self.org,
            actor=self.actor,
            supplier=self.supplier,
            amount=amount,
            supplier_vat=vat,
            date=self.day,
            service_date=date(2026, 9, 15) if prepaid else self.day,
            category=category or ("prepayment" if prepaid else "expense"),
        )
        return (
            ledger.approve_bill(organization=self.org, actor=self.actor, bill=row)
            if approved
            else row
        )

    def no_withholding(self, bill):
        decision = dict(
            rule_code="no_withholding",
            base="0",
            period=self.day.replace(day=1),
            reason="Synthetic professional conclusion: no withholding on this exact bill.",
            object_code="",
        )
        return work.review_bill_tax(
            organization=self.org,
            actor=self.actor,
            bill=bill,
            external_review=self.external(bill, work.bill_decision(**decision)),
            **decision,
        )

    def positive_withholding(self, bill):
        decision = dict(
            rule_code="pph23_service_2",
            base="100000",
            period=self.day.replace(day=1),
            reason="Synthetic professional classification of the exact invoice amount.",
            object_code="24-104-99",
            **self.pph23_context(
                direction="payable", tax_point_date=self.day, bill=bill
            ),
        )
        return work.review_bill_tax(
            organization=self.org,
            actor=self.actor,
            bill=bill,
            external_review=self.external(bill, work.bill_decision(**decision)),
            **decision,
        )

    def facts(self, bill, as_of=None):
        return bill_budget_facts(
            organization=self.org, bill=bill, as_of=as_of or timezone.localdate()
        )

    def spend(self, *, amount="80000000"):
        budget = planning.create_budget(
            organization=self.org,
            actor=self.actor,
            title="Synthetic September campaign",
            year=2026,
            month=9,
            channel="meta_ads",
            amount=amount,
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
            notes="Synthetic authorized envelope, not a payment.",
            self_approval_reason="Synthetic single-owner approval exception documented for this fixture.",
        )
        entry = planning.add_budget_entry(
            organization=self.org,
            actor=self.actor,
            budget=budget,
            kind="reservation",
            amount=amount,
            date=date(2026, 9, 8),
            description="Synthetic campaign estimate",
            reference="SYN-RESERVE",
            idempotency_key=f"synthetic-budget-{budget.pk}",
        )
        position = spending.budget_position(
            organization=self.org, budget=budget, as_of=timezone.localdate()
        )
        spend = spending.adopt_budget_entry(
            organization=self.org,
            actor=self.actor,
            entry=entry,
            date=self.day,
            reference=f"SYN-ADOPT-{budget.pk}",
            reason="Preserve the existing estimate and its history.",
            evidence="Synthetic estimate reviewed; Finance proofs remain separate.",
            expected_version=budget.version,
            expected_fingerprint=position["fingerprint"],
        )
        return budget, entry, spend

    def link(
        self, spend, entry, bill, *, amount=None, reference="SYN-LINK", fingerprint=None
    ):
        spend.refresh_from_db()
        position = spending.spend_position(
            organization=self.org, spend=spend, as_of=timezone.localdate()
        )
        return spending.link_spend_bill(
            organization=self.org,
            actor=self.actor,
            spend=spend,
            bill=bill,
            coverage=[
                {
                    "source_key": f"entry:{entry.pk}",
                    "amount": str(amount or bill.amount),
                }
            ],
            date=self.day,
            reference=reference,
            reason="This exact supplier bill replaces the selected estimate portion.",
            evidence="Synthetic supplier document and estimate correspondence checked.",
            expected_version=spend.version,
            expected_fingerprint=position["fingerprint"],
            expected_bill_fingerprint=fingerprint or self.facts(bill)["fingerprint"],
        )

    def position(self, budget):
        return spending.budget_position(
            organization=self.org, budget=budget, as_of=timezone.localdate()
        )

    def payment(self, bill, amount, *, day=None, reference="SYN-BILL-PAY"):
        bank = self.movement(f"-{amount}", day=day or self.day, reference=reference)
        return ledger.reconcile_bill_payment(
            organization=self.org,
            actor=self.actor,
            bill=bill,
            transaction=bank,
            amount=amount,
        )

    def damage_line(self, line, *, account):
        # Deliberately reproduce damaged persisted proofs in the isolated test DB.
        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {connection.ops.quote_name(JournalLine._meta.db_table)} SET account=%s WHERE id=%s",
                [account, line.pk],
            )

    def test_prepaid_release_and_partial_cash_are_dated_distinct_facts(self):
        bill = self.bill(prepaid=True)
        self.no_withholding(bill)
        first = self.payment(bill, "20000000", reference="SYN-PAY-20")
        second = self.payment(
            bill, "15000000", day=date(2026, 9, 25), reference="SYN-PAY-15"
        )
        release = ledger.release_prepayment(
            organization=self.org,
            actor=self.actor,
            bill=bill,
            date=date(2026, 9, 20),
            amount="20000000",
            reference="SYN-RELEASE-20",
            evidence="Synthetic delivered campaign portion.",
        )
        early, final = (
            self.facts(bill, date(2026, 9, 19)),
            self.facts(bill, date(2026, 9, 30)),
        )
        self.assertEqual(
            (
                early["gross"],
                early["incurred"],
                early["commitment"],
                early["verified_cash_outflow"],
                early["ap_outstanding"],
            ),
            ("60000000.00", "0.00", "60000000.00", "20000000.00", "40000000.00"),
        )
        self.assertEqual(
            (
                final["incurred"],
                final["commitment"],
                final["verified_cash_outflow"],
                final["ap_outstanding"],
            ),
            ("20000000.00", "40000000.00", "35000000.00", "25000000.00"),
        )
        self.assertEqual(
            {p["event_id"] for p in final["payments"]},
            {f"bill_payment:{first.pk}", f"bill_payment:{second.pk}"},
        )
        event = next(
            e
            for e in final["events"]
            if e["event_id"] == f"prepayment_release:{release.pk}"
        )
        self.assertEqual(
            (event["date"], event["commitment"], event["cash_outflow"]),
            ("2026-09-20", "-20000000.00", "0.00"),
        )
        self.assertEqual(len({e["journal"]["id"] for e in final["events"]}), 4)

    def test_draft_and_not_yet_effective_approval_are_provisional(self):
        draft = self.bill(approved=False)
        before = self.facts(draft)
        self.assertTrue(before["provisional"])
        self.assertEqual(
            (
                before["bill"]["amount"],
                before["gross"],
                before["incurred"],
                before["events"],
            ),
            ("60000000.00", "0.00", "0.00", []),
        )
        ledger.approve_bill(organization=self.org, actor=self.actor, bill=draft)
        prior = self.facts(draft, date(2026, 9, 9))
        self.assertTrue(prior["provisional"])
        self.assertEqual(
            (prior["gross"], prior["incurred"], prior["verified_cash_outflow"]),
            ("0.00", "0.00", "0.00"),
        )
        self.assertFalse(self.facts(draft, self.day)["provisional"])

    def test_complete_original_journal_is_required_even_when_totals_match(self):
        bill = self.bill()
        journal = Journal.objects.get(
            source_key=f"bill:{bill.pk}:approval", organization=self.org
        )
        line = journal.lines.get(account="AP")
        self.damage_line(line, account="TAX_PAYABLE")
        with self.assertRaises(ValidationError):
            self.facts(bill)
        self.damage_line(line, account="AP")
        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {connection.ops.quote_name(Journal._meta.db_table)} SET status=%s WHERE id=%s",
                ["draft", journal.pk],
            )
        with self.assertRaises(ValidationError):
            self.facts(bill)

    def test_bank_identity_changes_fingerprint_without_inventing_cash(self):
        bill = self.bill()
        self.no_withholding(bill)
        self.payment(bill, "20000000")
        old = self.facts(bill, date(2026, 9, 20))
        self.assertEqual(
            old["fingerprint"], self.facts(bill, date(2026, 9, 30))["fingerprint"]
        )
        self.account.account_number = "SYN-RENAMED-ACCOUNT"
        self.account.save()
        changed = self.facts(bill)
        self.assertNotEqual(old["fingerprint"], changed["fingerprint"])
        self.assertEqual(changed["verified_cash_outflow"], "20000000.00")
        self.assertNotIn("SYN-RENAMED-ACCOUNT", str(changed["events"]))

    def test_cost_attribution_does_not_create_a_second_budget_expense(self):
        from .costing import allocate_cost
        from .fulfillment import deliver_units

        bill = self.bill(amount="450000", category="provider")
        invoice = ledger.issue_invoice(
            organization=self.org, actor=self.actor, invoice=self.invoice()
        )
        ledger.reconcile_receipt(
            organization=self.org,
            actor=self.actor,
            invoice=invoice,
            transaction=self.movement("500000", reference="SYN-ORDER-PAY"),
            amount="500000",
        )
        deliver_units(
            organization=self.org,
            actor=self.actor,
            invoice=invoice,
            quantity=1,
            date=self.day,
            reference="SYN-COST-DELIVERY",
            evidence="Synthetic completed test unit.",
        )
        before, journals = self.facts(bill), Journal.objects.count()
        allocate_cost(
            organization=self.org,
            actor=self.actor,
            invoice=invoice,
            bill=bill,
            quantity=1,
            amount="450000",
            date=self.day,
            reference="SYN-COST-ATTRIBUTION",
            reason="Match provider cost to the delivered test.",
            evidence="Synthetic provider invoice and delivery reviewed.",
        )
        self.assertEqual(before, self.facts(bill))
        self.assertEqual(Journal.objects.count(), journals)
        self.assertEqual(before["incurred"], "450000.00")

    def test_existing_paid_bill_link_is_backfill_without_finance_writes(self):
        bill = self.bill()
        self.no_withholding(bill)
        self.payment(bill, "20000000", reference="SYN-BACKFILL-20")
        self.payment(bill, "15000000", reference="SYN-BACKFILL-15")
        budget, entry, spend = self.spend()
        sources = list(Journal.objects.order_by("pk").values())
        payments = list(BillPayment.objects.order_by("pk").values())
        self.link(spend, entry, bill)
        result = self.position(budget)
        self.assertTrue(result["current"])
        self.assertEqual(
            (
                result["incurred"],
                result["reservations"],
                result["consumed"],
                result["verified_cash"],
                result["unpaid_bills"],
            ),
            ("60000000.00", "20000000.00", "80000000.00", "35000000.00", "25000000.00"),
        )
        self.assertEqual(list(Journal.objects.order_by("pk").values()), sources)
        self.assertEqual(list(BillPayment.objects.order_by("pk").values()), payments)
        spend.refresh_from_db()
        version, movements = spend.version, BudgetSpendMovement.objects.count()
        sync_bill_budget(organization=self.org, actor=self.actor, bill=bill)
        spend.refresh_from_db()
        self.assertEqual(
            (spend.version, BudgetSpendMovement.objects.count()), (version, movements)
        )

    def test_new_partial_release_syncs_once_and_exact_retry_preserves_history(self):
        bill = self.bill(prepaid=True)
        budget, entry, spend = self.spend()
        self.link(spend, entry, bill)
        spend.refresh_from_db()
        version = spend.version
        args = dict(
            organization=self.org,
            actor=self.actor,
            bill=bill,
            date=date(2026, 9, 20),
            amount="20000000",
            reference="SYN-LINKED-RELEASE",
            evidence="Synthetic campaign portion delivered.",
        )
        row = ledger.release_prepayment(**args)
        spend.refresh_from_db()
        self.assertEqual(spend.version, version + 1)
        result = self.position(budget)
        self.assertEqual(
            (
                result["incurred"],
                result["open_commitments"],
                result["reservations"],
                result["consumed"],
            ),
            ("20000000.00", "40000000.00", "20000000.00", "80000000.00"),
        )
        movements, journals = (
            BudgetSpendMovement.objects.count(),
            Journal.objects.count(),
        )
        self.assertEqual(ledger.release_prepayment(**args).pk, row.pk)
        sync_bill_budget(organization=self.org, actor=self.actor, bill=bill)
        spend.refresh_from_db()
        self.assertEqual(
            (
                spend.version,
                BudgetSpendMovement.objects.count(),
                Journal.objects.count(),
            ),
            (version + 1, movements, journals),
        )

    def test_linked_draft_approval_keeps_actual_overrun_visible(self):
        bill = self.bill(approved=False)
        budget, entry, spend = self.spend()
        self.link(spend, entry, bill, amount="60000000")
        bill.amount = Decimal(90000000)
        bill.save()
        ledger.approve_bill(organization=self.org, actor=self.actor, bill=bill)
        result = self.position(budget)
        self.assertTrue(result["current"])
        self.assertEqual(
            (
                result["incurred"],
                result["reservations"],
                result["consumed"],
                result["overrun"],
            ),
            ("90000000.00", "20000000.00", "110000000.00", "30000000.00"),
        )
        self.assertTrue(result["tasks"])
        self.assertEqual(self.facts(bill)["ap_outstanding"], "90000000.00")

    def test_corrupt_linked_source_rolls_back_new_release_in_same_transaction(self):
        bill = self.bill(prepaid=True)
        budget, entry, spend = self.spend()
        self.link(spend, entry, bill)
        source = Journal.objects.get(
            organization=self.org, source_key=f"bill:{bill.pk}:approval"
        )
        self.damage_line(source.lines.get(account="AP"), account="TAX_PAYABLE")
        before = (
            Journal.objects.count(),
            PrepaymentRelease.objects.count(),
            AuditEvent.objects.count(),
            BudgetSpendMovement.objects.count(),
        )
        spend.refresh_from_db()
        version = spend.version
        with self.assertRaises(ValidationError):
            ledger.release_prepayment(
                organization=self.org,
                actor=self.actor,
                bill=bill,
                date=date(2026, 9, 20),
                amount="20000000",
                reference="SYN-ROLLBACK-RELEASE",
                evidence="Synthetic completed campaign portion.",
            )
        self.assertEqual(
            (
                Journal.objects.count(),
                PrepaymentRelease.objects.count(),
                AuditEvent.objects.count(),
                BudgetSpendMovement.objects.count(),
            ),
            before,
        )
        spend.refresh_from_db()
        self.assertEqual(spend.version, version)
        self.assertFalse(
            Journal.objects.filter(
                source_key="prepayment:SYN-ROLLBACK-RELEASE"
            ).exists()
        )
        result = self.position(budget)
        self.assertFalse(result["current"])
        self.assertIsNone(result["incurred"])
        self.assertIsNone(result["verified_cash"])

    def test_linked_normal_payment_requires_actor_and_rolls_back_corrupt_source(self):
        bill = self.bill()
        self.no_withholding(bill)
        budget, entry, spend = self.spend()
        self.link(spend, entry, bill)
        bank = self.movement("-20000000", reference="SYN-ROLLBACK-PAY")
        counts = (
            BillPayment.objects.count(),
            Journal.objects.count(),
            BudgetSpendMovement.objects.count(),
        )
        with self.assertRaises((PermissionDenied, ValidationError)):
            ledger.reconcile_bill_payment(
                organization=self.org,
                actor=None,
                bill=bill,
                transaction=bank,
                amount="20000000",
            )
        self.assertEqual(
            (
                BillPayment.objects.count(),
                Journal.objects.count(),
                BudgetSpendMovement.objects.count(),
            ),
            counts,
        )
        original = Journal.objects.get(
            organization=self.org, source_key=f"bill:{bill.pk}:approval"
        )
        self.damage_line(original.lines.get(account="AP"), account="TAX_PAYABLE")
        with self.assertRaises(ValidationError):
            ledger.reconcile_bill_payment(
                organization=self.org,
                actor=self.actor,
                bill=bill,
                transaction=bank,
                amount="20000000",
            )
        self.assertEqual(
            (
                BillPayment.objects.count(),
                Journal.objects.count(),
                BudgetSpendMovement.objects.count(),
            ),
            counts,
        )
        self.assertEqual(bank.unallocated_amount, Decimal(-20000000))
        self.assertFalse(self.position(budget)["current"])

    def test_net_supplier_and_partial_tax_remit_count_vat_once_and_separate_liabilities(
        self,
    ):
        bill = self.bill(amount="111000", vat="11000")
        decision = self.positive_withholding(bill)
        budget, entry, spend = self.spend(amount="111000")
        self.link(spend, entry, bill)
        net = self.movement("-109000", reference="SYN-NET-SUPPLIER")
        settlement = ledger.reconcile_withheld_bill_payment(
            organization=self.org, actor=self.actor, bill=bill, transaction=net
        )
        before = self.position(budget)
        self.assertEqual(
            (
                before["incurred"],
                before["verified_cash"],
                before["unpaid_bills"],
                before["withholding_payable"],
            ),
            ("111000.00", "109000.00", "0.00", "2000.00"),
        )
        bank = self.movement("-2000", day=date(2026, 9, 20), reference="SYN-TAX-BANK")
        args = dict(
            organization=self.org,
            actor=self.actor,
            obligation=decision.obligation,
            transaction=bank,
            amount="1000",
            reference="SYN-TAX-PART",
            evidence="Synthetic observed tax transfer; official verification remains separate.",
        )
        remit = ledger.reconcile_tax_remittance(**args)
        result = self.position(budget)
        self.assertEqual(
            (
                result["incurred"],
                result["consumed"],
                result["verified_cash"],
                result["unpaid_bills"],
                result["withholding_payable"],
            ),
            ("111000.00", "111000.00", "110000.00", "0.00", "1000.00"),
        )
        facts = self.facts(bill)
        self.assertEqual(
            {p["event_id"]: p["amount"] for p in facts["payments"]},
            {
                f"withholding_supplier:{settlement.pk}": "109000.00",
                f"tax_remittance:{remit.pk}": "1000.00",
            },
        )
        count = BudgetSpendMovement.objects.count()
        self.assertEqual(ledger.reconcile_tax_remittance(**args).pk, remit.pk)
        self.assertEqual(
            ledger.reconcile_withheld_bill_payment(
                organization=self.org, actor=self.actor, bill=bill, transaction=net
            ).pk,
            settlement.pk,
        )
        self.assertEqual(BudgetSpendMovement.objects.count(), count)
        self.assertFalse(decision.obligation.payment_verified_at)

    def test_corrupt_withheld_journal_rolls_back_partial_remittance(self):
        bill = self.bill(amount="111000", vat="11000")
        decision = self.positive_withholding(bill)
        budget, entry, spend = self.spend(amount="111000")
        self.link(spend, entry, bill)
        settlement = ledger.reconcile_withheld_bill_payment(
            organization=self.org,
            actor=self.actor,
            bill=bill,
            transaction=self.movement("-109000", reference="SYN-CORRUPT-NET"),
        )
        self.damage_line(
            settlement.journal.lines.get(account="TAX_PAYABLE"), account="AP"
        )
        bank = self.movement(
            "-1000", day=date(2026, 9, 20), reference="SYN-CORRUPT-TAX"
        )
        counts = (
            BankPosting.objects.count(),
            TaxRemittance.objects.count(),
            Journal.objects.count(),
            BudgetSpendMovement.objects.count(),
        )
        with self.assertRaises(ValidationError):
            ledger.reconcile_tax_remittance(
                organization=self.org,
                actor=self.actor,
                obligation=decision.obligation,
                transaction=bank,
                amount="1000",
                reference="SYN-REMIT-ROLLBACK",
                evidence="Synthetic observed transfer.",
            )
        self.assertEqual(
            (
                BankPosting.objects.count(),
                TaxRemittance.objects.count(),
                Journal.objects.count(),
                BudgetSpendMovement.objects.count(),
            ),
            counts,
        )
        self.assertEqual(bank.unallocated_amount, Decimal(-1000))
        self.assertFalse(self.position(budget)["current"])

    def test_stale_selected_bill_amount_or_approval_does_not_create_link(self):
        bill = self.bill(approved=False)
        budget, entry, spend = self.spend()
        viewed = self.facts(bill)["fingerprint"]
        bill.amount = Decimal(61000000)
        bill.save()
        for action in ("amount", "approval"):
            if action == "approval":
                viewed = self.facts(bill)["fingerprint"]
                ledger.approve_bill(organization=self.org, actor=self.actor, bill=bill)
            before = BudgetSpendMovement.objects.count()
            with self.assertRaises(ValidationError):
                self.link(
                    spend,
                    entry,
                    bill,
                    amount="60000000",
                    fingerprint=viewed,
                    reference=f"SYN-STALE-{action}",
                )
            self.assertFalse(BudgetBillLink.objects.filter(spend=spend).exists())
            self.assertEqual(BudgetSpendMovement.objects.count(), before)
        self.assertEqual(self.position(budget)["reservations"], "80000000.00")

    def test_source_scope_and_exact_date_inputs_fail_closed(self):
        bill = self.bill()
        foreign = Organization.objects.create(name="Synthetic foreign budget source")
        with self.assertRaises(ValidationError):
            bill_budget_facts(organization=foreign, bill=bill, as_of=self.day)
        for value in (datetime(2026, 9, 10), "2026-9-10", True):
            with self.assertRaises(ValidationError):
                self.facts(bill, value)

    def test_corrupt_total_bank_assignments_cannot_be_verified_as_bill_cash(self):
        first, second = self.bill(amount="60000"), self.bill(amount="60000")
        self.no_withholding(first)
        self.no_withholding(second)
        bank = self.movement("-100000", reference="SYN-SPLIT-BANK")
        ledger.reconcile_bill_payment(
            organization=self.org,
            actor=self.actor,
            bill=first,
            transaction=bank,
            amount="60000",
        )
        other = ledger.reconcile_bill_payment(
            organization=self.org,
            actor=self.actor,
            bill=second,
            transaction=bank,
            amount="40000",
        )
        journal = Journal.objects.get(
            organization=self.org, source_key=f"bill-payment:{other.pk}"
        )
        self.assertEqual(bank.unallocated_amount, 0)
        self.assertEqual(self.facts(first)["verified_cash_outflow"], "60000.00")
        # Both individual source journals still balance and fit their Bills;
        # together they now claim Rp110k from one Rp100k actual bank outflow.
        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {connection.ops.quote_name(BillPayment._meta.db_table)} SET amount=%s WHERE id=%s",
                ["50000", other.pk],
            )
            cursor.execute(
                f"UPDATE {connection.ops.quote_name(JournalLine._meta.db_table)} SET debit=%s WHERE journal_id=%s AND account=%s",
                ["50000", journal.pk, "AP"],
            )
            cursor.execute(
                f"UPDATE {connection.ops.quote_name(JournalLine._meta.db_table)} SET credit=%s WHERE journal_id=%s AND account=%s",
                ["50000", journal.pk, "BANK"],
            )
        self.assertEqual(bank.unallocated_amount, Decimal(10000))
        with self.assertRaises(ValidationError):
            self.facts(first)

    def test_changed_supplier_role_blocks_bill_source_instead_of_accepting_cost(self):
        bill = self.bill()
        self.assertEqual(self.facts(bill)["incurred"], "60000000.00")
        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {connection.ops.quote_name(self.supplier._meta.db_table)} SET kind=%s WHERE id=%s",
                ["customer", self.supplier.pk],
            )
        with self.assertRaises(ValidationError):
            self.facts(bill)

    def test_changed_bank_date_cannot_hide_original_payment_journal_at_historical_cutoff(
        self,
    ):
        cutoff = date(2026, 9, 20)
        later = self.bill(amount="60000")
        self.no_withholding(later)
        self.payment(
            later, "60000", day=date(2026, 9, 25), reference="SYN-LEGIT-LATER-PAYMENT"
        )
        before_later_payment = self.facts(later, cutoff)
        self.assertEqual(
            (
                before_later_payment["ap_outstanding"],
                before_later_payment["verified_cash_outflow"],
            ),
            ("60000.00", "0.00"),
        )
        self.assertEqual(
            self.facts(later, date(2026, 9, 25))["verified_cash_outflow"], "60000.00"
        )

        bill = self.bill(amount="60000")
        self.no_withholding(bill)
        payment = self.payment(bill, "60000", reference="SYN-CHANGED-BANK-DATE")
        original = Journal.objects.get(
            organization=self.org, source_key=f"bill-payment:{payment.pk}"
        )
        original_lines = list(original.lines.order_by("pk").values())
        valid = self.facts(bill, cutoff)
        self.assertEqual(
            (valid["ap_outstanding"], valid["verified_cash_outflow"]),
            ("0.00", "60000.00"),
        )
        # The immutable source journal still proves a Sep10 payment. A later
        # damaged bank date must not make that original past proof disappear.
        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {connection.ops.quote_name(payment.transaction._meta.db_table)} SET date=%s WHERE id=%s",
                [date(2026, 9, 25), payment.transaction_id],
            )
        original.refresh_from_db()
        self.assertEqual(original.date, self.day)
        self.assertEqual(list(original.lines.order_by("pk").values()), original_lines)
        with self.assertRaises(ValidationError):
            self.facts(bill, cutoff)
