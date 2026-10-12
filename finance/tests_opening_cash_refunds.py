"""Public genuine old-cash refunds use new bank evidence and reopen principal."""

from copy import deepcopy
from datetime import date
from decimal import Decimal
from uuid import uuid4

from django.test import TestCase

from . import opening_balances as opening
from . import services as ledger
from .models import (
    BankPosting,
    BankTransaction,
    Bill,
    Invoice,
    Journal,
    OpeningBalanceCancellation,
    OpeningBalanceCorrection,
    OpeningBalanceSettlement,
)
from .tests_opening_balances import OpeningBalanceFixtures


class OpeningCashRefundTests(OpeningBalanceFixtures, TestCase):
    def test_genuine_ar_and_ap_refunds_use_opposite_actual_cash_and_reopen_dated_principal(
        self,
    ):
        ledger.record_bank_opening(
            organization=self.org,
            actor=self.actor,
            account=self.account,
            date=self.cutoff,
            amount="1000",
            reference="SYN-REFUND-BANK-OPENING",
            evidence="Synthetic independently reviewed beginning bank statement.",
        )
        receivable = self.item()
        payable = self.item(kind="payable", party=self.supplier, amount="200")
        receipt_bank = self.movement("120", reference="SYN-REFUND-ORIGINAL-AR-CASH")
        payment_bank = self.movement("-50", reference="SYN-REFUND-ORIGINAL-AP-CASH")
        receipt = self.settle(receivable, receipt_bank)
        payment = self.settle(payable, payment_bank, amount="50")
        original_receivable = deepcopy(
            opening.opening_balance_source_snapshot(
                organization=self.org, item=receivable
            )
        )
        original_payable = deepcopy(
            opening.opening_balance_source_snapshot(organization=self.org, item=payable)
        )
        original_receipt = deepcopy(
            opening.opening_settlement_source_snapshot(
                organization=self.org, settlement=receipt
            )
        )
        original_payment = deepcopy(
            opening.opening_settlement_source_snapshot(
                organization=self.org, settlement=payment
            )
        )
        original_postings = {receipt.bank_posting_id, payment.bank_posting_id}
        original_balances = ledger.report_balances(
            organization=self.org, as_of=self.day
        )
        self.assertEqual(
            tuple(original_balances[key] for key in ("BANK", "AR", "AP", "EQUITY")),
            (Decimal(1070), Decimal(180), Decimal(-150), Decimal(-1100)),
        )
        self.assertEqual(
            (
                self.position(receivable)["remaining"],
                self.position(payable)["remaining"],
                receipt_bank.unallocated_amount,
                payment_bank.unallocated_amount,
            ),
            (Decimal(180), Decimal(150), Decimal(0), Decimal(0)),
        )
        self.assertFalse(
            ledger.pending_bank_transactions(organization=self.org).exists()
        )

        ar_day, ap_day = date(2026, 9, 15), date(2026, 9, 18)
        ar_bank = self.movement(
            "-40", day=ar_day, reference="SYN-REFUND-ACTUAL-AR-RETURN"
        )
        ap_bank = self.movement(
            "20", day=ap_day, reference="SYN-REFUND-ACTUAL-AP-RETURN"
        )
        finance = self.role("finance")
        bank_sources = {
            bank.pk: (
                bank.organization_id,
                bank.account_id,
                bank.date,
                bank.amount,
                bank.reference,
                bank.description,
            )
            for bank in (receipt_bank, payment_bank, ar_bank, ap_bank)
        }
        self.assertEqual(len(bank_sources), 4)
        self.assertEqual(
            set(
                ledger.pending_bank_transactions(organization=self.org).values_list(
                    "pk", flat=True
                )
            ),
            {ar_bank.pk, ap_bank.pk},
        )
        self.assertEqual(
            (ar_bank.unallocated_amount, ap_bank.unallocated_amount),
            (Decimal(-40), Decimal(20)),
        )

        # Resolve the new public command only after the original cash proof and
        # both distinct, opposite-direction actual-bank fixtures have succeeded.
        # Its absence is a genuine feature RED rather than a model-import fault.
        refund = opening.refund_opening_settlement
        ar_preview = opening.opening_balance_action_context(
            organization=self.org,
            action="refund",
            settlement=receipt,
            transaction=ar_bank,
            amount="40",
        )
        self.assertTrue(ar_preview["current"], ar_preview)
        self.assertFalse(ar_preview["blockers"], ar_preview)
        self.assertEqual(
            (
                ar_preview["date"],
                ar_preview["amount"],
                ar_preview["before"],
                ar_preview["after"],
                ar_preview["bank_remaining"],
                ar_preview["bank_after"],
            ),
            (ar_day, Decimal(40), Decimal(180), Decimal(220), Decimal(40), Decimal(0)),
        )
        ar_refund = refund(
            organization=self.org,
            actor=self.actor,
            settlement=receipt,
            transaction=ar_bank,
            amount="40",
            reason="Synthetic genuine partial return of received old cash.",
            evidence="Synthetic original receipt and distinct outgoing bank refund independently checked.",
            request_key=str(uuid4()),
            expected_fingerprint=ar_preview["fingerprint"],
            confirmed=True,
        )
        self.assertEqual(
            (
                ar_refund.settlement_id,
                ar_refund.date,
                ar_refund.amount,
                ar_refund.bank_posting.transaction_id,
                ar_refund.bank_posting.signed_amount,
            ),
            (receipt.pk, ar_day, Decimal(40), ar_bank.pk, Decimal(-40)),
        )
        self.assertEqual(
            (
                BankTransaction.objects.count(),
                BankPosting.objects.count(),
                Journal.objects.count(),
            ),
            (4, 3, 6),
        )
        self.assertEqual(
            (
                receipt_bank.unallocated_amount,
                payment_bank.unallocated_amount,
                ar_bank.unallocated_amount,
                ap_bank.unallocated_amount,
            ),
            (Decimal(0), Decimal(0), Decimal(0), Decimal(20)),
        )
        self.assertEqual(
            set(
                ledger.pending_bank_transactions(organization=self.org).values_list(
                    "pk", flat=True
                )
            ),
            {ap_bank.pk},
        )

        ap_preview = opening.opening_balance_action_context(
            organization=self.org,
            action="refund",
            settlement=payment,
            transaction=ap_bank,
            amount="20",
        )
        self.assertTrue(ap_preview["current"], ap_preview)
        self.assertFalse(ap_preview["blockers"], ap_preview)
        self.assertEqual(
            (
                ap_preview["date"],
                ap_preview["amount"],
                ap_preview["before"],
                ap_preview["after"],
                ap_preview["bank_remaining"],
                ap_preview["bank_after"],
            ),
            (ap_day, Decimal(20), Decimal(150), Decimal(170), Decimal(20), Decimal(0)),
        )
        ap_refund = refund(
            organization=self.org,
            actor=finance,
            settlement=payment,
            transaction=ap_bank,
            amount="20",
            reason="Synthetic supplier genuinely returned part of old payment.",
            evidence="Synthetic original payment and distinct incoming supplier refund independently checked.",
            request_key=str(uuid4()),
            expected_fingerprint=ap_preview["fingerprint"],
            confirmed=True,
        )
        self.assertEqual(
            (
                ap_refund.settlement_id,
                ap_refund.date,
                ap_refund.amount,
                ap_refund.bank_posting.transaction_id,
                ap_refund.bank_posting.signed_amount,
            ),
            (payment.pk, ap_day, Decimal(20), ap_bank.pk, Decimal(20)),
        )

        cases = (
            (date(2026, 8, 31), ("0", "0", "0", "0"), ("0", "0")),
            (self.cutoff, ("1000", "300", "-200", "-1100"), ("300", "200")),
            (date(2026, 9, 9), ("1000", "300", "-200", "-1100"), ("300", "200")),
            (self.day, ("1070", "180", "-150", "-1100"), ("180", "150")),
            (date(2026, 9, 14), ("1070", "180", "-150", "-1100"), ("180", "150")),
            (ar_day, ("1030", "220", "-150", "-1100"), ("220", "150")),
            (date(2026, 9, 17), ("1030", "220", "-150", "-1100"), ("220", "150")),
            (ap_day, ("1050", "220", "-170", "-1100"), ("220", "170")),
        )
        for cutoff, expected_ledger, expected_residuals in cases:
            with self.subTest(cutoff=cutoff):
                balances = ledger.report_balances(organization=self.org, as_of=cutoff)
                self.assertEqual(
                    tuple(balances[key] for key in ("BANK", "AR", "AP", "EQUITY")),
                    tuple(Decimal(value) for value in expected_ledger),
                )
                self.assertEqual(
                    tuple(
                        balances[key]
                        for key in ("REVENUE", "EXPENSE", "DEFERRED_REVENUE")
                    ),
                    (Decimal(0), Decimal(0), Decimal(0)),
                )
                self.assertEqual(sum(balances.values()), Decimal(0))
                ar_position = self.position(receivable, as_of=cutoff)
                ap_position = self.position(payable, as_of=cutoff)
                self.assertTrue(ar_position["current"], ar_position)
                self.assertTrue(ap_position["current"], ap_position)
                self.assertEqual(
                    (ar_position["remaining"], ap_position["remaining"]),
                    tuple(Decimal(value) for value in expected_residuals),
                )
                closing = opening.opening_balance_close_facts(
                    organization=self.org, cutoff=cutoff
                )
                self.assertTrue(closing["current"], closing)
                self.assertEqual(
                    (closing["receivable"], closing["payable"]),
                    tuple(Decimal(value) for value in expected_residuals),
                )
                self.assertFalse(closing["blockers"], closing)

        self.assertEqual(
            (
                self.position(receivable, as_of=ap_day)["settled"],
                self.position(payable, as_of=ap_day)["settled"],
            ),
            (Decimal(80), Decimal(30)),
        )
        self.assertEqual(
            (
                BankTransaction.objects.count(),
                BankPosting.objects.count(),
                OpeningBalanceSettlement.objects.count(),
                Journal.objects.count(),
            ),
            (4, 4, 2, 7),
        )
        self.assertEqual(
            BankPosting.objects.filter(pk__in=original_postings).count(), 2
        )
        self.assertFalse(OpeningBalanceCancellation.objects.exists())
        self.assertFalse(OpeningBalanceCorrection.objects.exists())
        self.assertEqual((Invoice.objects.count(), Bill.objects.count()), (0, 0))
        self.assertFalse(
            ledger.pending_bank_transactions(organization=self.org).exists()
        )
        for bank in (receipt_bank, payment_bank, ar_bank, ap_bank):
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
                bank_sources[bank.pk],
            )
            self.assertEqual(bank.unallocated_amount, Decimal(0))
        for bank in (receipt_bank, payment_bank):
            self.assertEqual(
                opening.opening_cash_cancellation_facts(
                    organization=self.org, transaction=bank
                )["released"],
                Decimal(0),
            )
        self.assertEqual(
            opening.opening_balance_source_snapshot(
                organization=self.org, item=receivable
            ),
            original_receivable,
        )
        self.assertEqual(
            opening.opening_balance_source_snapshot(
                organization=self.org, item=payable
            ),
            original_payable,
        )
        self.assertEqual(
            opening.opening_settlement_source_snapshot(
                organization=self.org, settlement=receipt
            ),
            original_receipt,
        )
        self.assertEqual(
            opening.opening_settlement_source_snapshot(
                organization=self.org, settlement=payment
            ),
            original_payment,
        )

    def test_semantically_tampered_refund_holds_opening_bank_and_shared_bill_budget_before_new_cash(
        self,
    ):
        from django.core.exceptions import ValidationError

        from taxes import workflow as tax

        from .budget_adapter import bill_budget_facts, bill_budget_source_identity
        from .models import JournalLine, OpeningBalanceRefund
        from .tests_tax_money import TaxMoneyFixtures

        ledger.record_bank_opening(
            organization=self.org,
            actor=self.actor,
            account=self.account,
            date=self.cutoff,
            amount="1000",
            reference="SYN-REFUND-INTEGRITY-BANK-OPENING",
            evidence="Synthetic independently reviewed beginning bank balance.",
        )
        item = self.item()
        original_bank = self.movement(
            "120", reference="SYN-REFUND-INTEGRITY-ORIGINAL-CASH"
        )
        cash = self.settle(item, original_bank)
        original_item = deepcopy(
            opening.opening_balance_source_snapshot(organization=self.org, item=item)
        )
        original_cash = deepcopy(
            opening.opening_settlement_source_snapshot(
                organization=self.org, settlement=cash
            )
        )
        refund_day = date(2026, 9, 15)
        shared_bank = self.movement(
            "-80", day=refund_day, reference="SYN-REFUND-INTEGRITY-SHARED-OUTFLOW"
        )
        preview = opening.opening_balance_action_context(
            organization=self.org,
            action="refund",
            settlement=cash,
            transaction=shared_bank,
            amount="40",
        )
        self.assertTrue(preview["current"], preview)
        self.assertFalse(preview["blockers"], preview)
        event = opening.refund_opening_settlement(
            organization=self.org,
            actor=self.actor,
            settlement=cash,
            transaction=shared_bank,
            amount="40",
            request_key=str(uuid4()),
            expected_fingerprint=preview["fingerprint"],
            confirmed=True,
            reason="Synthetic independently checked partial return of old received cash.",
            evidence="Synthetic distinct outgoing bank refund and original receipt.",
        )
        self.assertEqual(
            (event.bank_posting.signed_amount, shared_bank.unallocated_amount),
            (Decimal(-40), Decimal(-40)),
        )
        bill = ledger.create_bill(
            organization=self.org,
            actor=self.actor,
            supplier=self.supplier,
            amount="40",
            date=refund_day,
            service_date=refund_day,
            number="SYN-REFUND-INTEGRITY-BILL",
            supplier_reference="SYN-REFUND-INTEGRITY-BILL-SOURCE",
        )
        bill = ledger.approve_bill(organization=self.org, actor=self.actor, bill=bill)
        decision = dict(
            rule_code="no_withholding",
            base="0",
            period=refund_day.replace(day=1),
            reason="Synthetic received professional conclusion for this exact ordinary Bill.",
            object_code="",
        )
        review = tax.record_external_review(
            organization=self.org,
            actor=self.actor,
            subject=bill,
            decision=tax.bill_decision(**decision),
            evidence=TaxMoneyFixtures.evidence(self, actor=self.actor),
            professional_name="Synthetic professional",
            qualification_reference="Synthetic engagement",
            reviewed_on=refund_day,
            statement="Synthetic signed report explicitly confirms no withholding on this Bill.",
            report_confirmed=True,
        )
        tax.review_bill_tax(
            organization=self.org,
            actor=self.actor,
            bill=bill,
            external_review=review,
            **decision,
        )
        ledger.reconcile_bill_payment(
            organization=self.org,
            actor=self.actor,
            bill=bill,
            transaction=shared_bank,
            amount="40",
        )
        facts = bill_budget_facts(organization=self.org, bill=bill, as_of=refund_day)
        self.assertTrue(facts["current"])
        self.assertEqual(
            (
                facts["ap_settled"],
                facts["verified_cash_outflow"],
                facts["ap_outstanding"],
            ),
            ("40.00", "40.00", "0.00"),
        )
        balances = ledger.report_balances(organization=self.org, as_of=refund_day)
        self.assertEqual(
            tuple(balances[key] for key in ("BANK", "AR", "AP", "EQUITY", "EXPENSE")),
            (Decimal(1040), Decimal(220), Decimal(0), Decimal(-1300), Decimal(40)),
        )
        self.assertEqual(
            tuple(balances[key] for key in ("REVENUE", "DEFERRED_REVENUE")),
            (Decimal(0), Decimal(0)),
        )
        self.assertEqual(shared_bank.unallocated_amount, Decimal(0))
        self.assertFalse(
            ledger.pending_bank_transactions(organization=self.org).exists()
        )
        receipt = deepcopy(
            opening.opening_refund_source_snapshot(organization=self.org, refund=event)
        )
        good_graph = opening.opening_refund_source_graph(
            organization=self.org, refund=event
        )["fingerprint"]
        good_budget_graph = bill_budget_source_identity(
            organization=self.org, bill=bill, as_of=refund_day
        )
        next_bank = self.movement(
            "-10", day=date(2026, 9, 18), reference="SYN-REFUND-INTEGRITY-NEXT-CASH"
        )
        next_preview = opening.opening_balance_action_context(
            organization=self.org,
            action="refund",
            settlement=cash,
            transaction=next_bank,
            amount="10",
        )
        self.assertTrue(next_preview["current"], next_preview)
        self.assertFalse(next_preview["blockers"], next_preview)
        counts = (*self.counts(), OpeningBalanceRefund.objects.count())

        # This remains a balanced journal with the exact original bank amount.
        # Only the semantic non-bank account is forged: expense is not reopened AR.
        line = event.journal.lines.get(account="AR")
        self.corrupt(JournalLine, line.pk, "account", "EXPENSE")
        position = opening.opening_balance_position(
            organization=self.org, item=item, as_of=refund_day
        )
        close = opening.opening_balance_close_facts(
            organization=self.org, cutoff=refund_day
        )
        failed_preview = opening.opening_balance_action_context(
            organization=self.org,
            action="refund",
            settlement=cash,
            transaction=next_bank,
            amount="10",
        )
        for boundary, result in (
            ("position", position),
            ("close", close),
            ("next_refund", failed_preview),
        ):
            with self.subTest(boundary=boundary):
                self.assertFalse(result["current"])
                self.assertTrue(result["blockers"])
        self.assertEqual(
            (
                position["remaining"],
                close["receivable"],
                close["payable"],
                failed_preview["before"],
                failed_preview["after"],
            ),
            (None, None, None, None, None),
        )
        with self.assertRaises(ValidationError):
            bill_budget_facts(organization=self.org, bill=bill, as_of=refund_day)
        for bank in (original_bank, shared_bank, next_bank):
            with self.subTest(bank=bank.pk), self.assertRaises(ValidationError):
                _ = bank.unallocated_amount
        with self.assertRaises(ValidationError):
            ledger.pending_bank_transactions(organization=self.org)
        with self.assertRaises(ValidationError):
            opening.opening_refund_source_snapshot(organization=self.org, refund=event)
        self.assertNotEqual(
            good_graph,
            opening.opening_refund_source_graph(organization=self.org, refund=event)[
                "fingerprint"
            ],
        )
        self.assertNotEqual(
            good_budget_graph,
            bill_budget_source_identity(
                organization=self.org, bill=bill, as_of=refund_day
            ),
        )
        with self.assertRaises(ValidationError):
            opening.refund_opening_settlement(
                organization=self.org,
                actor=self.actor,
                settlement=cash,
                transaction=next_bank,
                amount="10",
                request_key=str(uuid4()),
                expected_fingerprint=next_preview["fingerprint"],
                confirmed=True,
                reason="Synthetic additional independently evidenced actual refund.",
                evidence="Synthetic next opposite bank cash and original receipt.",
            )
        self.assertEqual((*self.counts(), OpeningBalanceRefund.objects.count()), counts)

        self.corrupt(JournalLine, line.pk, "account", "AR")
        self.assertEqual(
            opening.opening_refund_source_snapshot(organization=self.org, refund=event),
            receipt,
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
        self.assertEqual(
            opening.opening_balance_position(
                organization=self.org, item=item, as_of=refund_day
            )["remaining"],
            Decimal(220),
        )
        restored = bill_budget_facts(organization=self.org, bill=bill, as_of=refund_day)
        self.assertEqual(
            (restored["ap_settled"], restored["verified_cash_outflow"]),
            ("40.00", "40.00"),
        )
        self.assertEqual(
            (
                original_bank.unallocated_amount,
                shared_bank.unallocated_amount,
                next_bank.unallocated_amount,
            ),
            (Decimal(0), Decimal(0), Decimal(-10)),
        )
        self.assertEqual(
            set(
                ledger.pending_bank_transactions(organization=self.org).values_list(
                    "pk", flat=True
                )
            ),
            {next_bank.pk},
        )
        self.assertEqual((*self.counts(), OpeningBalanceRefund.objects.count()), counts)
