"""Public recovery of a wrong opening-cash match without inventing bank cash."""

from datetime import date
from decimal import Decimal
from uuid import uuid4

from django.test import TestCase

from . import opening_balances as opening
from . import services as ledger
from .models import BankPosting, BankTransaction, Bill, Invoice
from .tests_opening_balances import OpeningBalanceFixtures


class OpeningCashRecoveryTests(OpeningBalanceFixtures, TestCase):
    def test_owner_cancels_wrong_match_at_original_bank_date_and_rematches_same_cash(
        self,
    ):
        ledger.record_bank_opening(
            organization=self.org,
            actor=self.actor,
            account=self.account,
            date=self.cutoff,
            amount="1000",
            reference="SYN-RECOVERY-BANK-OPENING",
            evidence="Synthetic reviewed opening bank statement.",
        )
        receivable = self.item()
        payable = self.item(kind="payable", party=self.supplier, amount="200")
        receipt_bank = self.movement("120", reference="SYN-RECOVERY-AR-RECEIPT")
        payment_bank = self.movement("-50", reference="SYN-RECOVERY-AP-PAYMENT")
        receipt = self.settle(receivable, receipt_bank)
        payment = self.settle(payable, payment_bank, amount="50")
        original_item = opening.opening_balance_source_snapshot(
            organization=self.org, item=receivable
        )
        original_cash = opening.opening_settlement_source_snapshot(
            organization=self.org, settlement=receipt
        )
        original_payment = opening.opening_settlement_source_snapshot(
            organization=self.org, settlement=payment
        )
        original_posting_ids = {receipt.bank_posting_id, payment.bank_posting_id}
        balances = ledger.report_balances(organization=self.org, as_of=self.day)
        self.assertEqual(
            tuple(balances[key] for key in ("BANK", "AR", "AP", "EQUITY")),
            (Decimal(1070), Decimal(180), Decimal(-150), Decimal(-1100)),
        )
        self.assertEqual(
            (
                self.position(receivable)["remaining"],
                self.position(payable)["remaining"],
            ),
            (Decimal(180), Decimal(150)),
        )
        self.assertFalse(
            ledger.pending_bank_transactions(organization=self.org).exists()
        )

        # Resolve the new public command only after the original opening and
        # actual-cash fixtures have succeeded. Its absence is a feature RED,
        # rather than an import or setup failure.
        cancel = opening.cancel_opening_settlement
        context = opening.opening_balance_action_context(
            organization=self.org, action="cancel", settlement=receipt
        )
        self.assertTrue(context["current"])
        cancellation = cancel(
            organization=self.org,
            actor=self.actor,
            settlement=receipt,
            reason="Synthetic confirmation that this receipt was matched to the wrong old document.",
            evidence="Synthetic bank source and signed correction request; the physical receipt remains.",
            request_key=str(uuid4()),
            expected_fingerprint=context["fingerprint"],
            confirmed=True,
        )
        self.assertEqual(
            (cancellation.date, cancellation.amount), (self.day, Decimal(120))
        )
        balances = ledger.report_balances(organization=self.org, as_of=self.day)
        self.assertEqual(
            tuple(balances[key] for key in ("BANK", "AR", "AP", "EQUITY")),
            (Decimal(950), Decimal(300), Decimal(-150), Decimal(-1100)),
        )
        self.assertEqual(
            (
                self.position(receivable)["remaining"],
                self.position(payable)["remaining"],
                receipt_bank.unallocated_amount,
                payment_bank.unallocated_amount,
            ),
            (Decimal(300), Decimal(150), Decimal(120), Decimal(0)),
        )
        self.assertEqual(
            set(
                ledger.pending_bank_transactions(organization=self.org).values_list(
                    "pk", flat=True
                )
            ),
            {receipt_bank.pk},
        )
        self.assertEqual(
            (BankTransaction.objects.count(), BankPosting.objects.count()), (2, 2)
        )
        self.assertEqual(
            set(BankPosting.objects.values_list("pk", flat=True)), original_posting_ids
        )
        self.assertEqual(
            opening.opening_balance_source_snapshot(
                organization=self.org, item=receivable
            ),
            original_item,
        )
        self.assertEqual(
            opening.opening_settlement_source_snapshot(
                organization=self.org, settlement=receipt
            ),
            original_cash,
        )
        self.assertEqual(
            opening.opening_settlement_source_snapshot(
                organization=self.org, settlement=payment
            ),
            original_payment,
        )

        rematched = self.settle(receivable, receipt_bank)
        self.assertNotEqual(rematched.pk, receipt.pk)
        balances = ledger.report_balances(organization=self.org, as_of=self.day)
        self.assertEqual(
            tuple(balances[key] for key in ("BANK", "AR", "AP", "EQUITY")),
            (Decimal(1070), Decimal(180), Decimal(-150), Decimal(-1100)),
        )
        self.assertEqual(
            (
                self.position(receivable)["remaining"],
                self.position(payable)["remaining"],
                receipt_bank.unallocated_amount,
            ),
            (Decimal(180), Decimal(150), Decimal(0)),
        )
        self.assertFalse(
            ledger.pending_bank_transactions(organization=self.org).exists()
        )
        self.assertEqual(BankTransaction.objects.count(), 2)
        self.assertEqual(
            BankPosting.objects.filter(pk__in=original_posting_ids).count(), 2
        )
        self.assertEqual(
            opening.opening_balance_source_snapshot(
                organization=self.org, item=receivable
            ),
            original_item,
        )
        self.assertEqual(
            opening.opening_settlement_source_snapshot(
                organization=self.org, settlement=receipt
            ),
            original_cash,
        )
        earlier = ledger.report_balances(organization=self.org, as_of=date(2026, 9, 9))
        self.assertEqual(
            tuple(earlier[key] for key in ("BANK", "AR", "AP", "EQUITY")),
            (Decimal(1000), Decimal(300), Decimal(-200), Decimal(-1100)),
        )
        self.assertEqual(
            tuple(
                balances[key]
                for key in ("REVENUE", "EXPENSE", "DEFERRED_REVENUE", "TAX_PAYABLE")
            ),
            (Decimal(0), Decimal(0), Decimal(0), Decimal(0)),
        )
        self.assertEqual((Invoice.objects.count(), Bill.objects.count()), (0, 0))

    def test_cancelled_opening_payment_can_pay_an_ordinary_bill_and_reach_budget_facts_once(
        self,
    ):
        from taxes import workflow as tax

        from .budget_adapter import bill_budget_facts, bill_budget_source_identity
        from .tests_tax_money import TaxMoneyFixtures

        ledger.record_bank_opening(
            organization=self.org,
            actor=self.actor,
            account=self.account,
            date=self.cutoff,
            amount="1000",
            reference="SYN-SHARED-RECOVERY-BANK-OPENING",
            evidence="Synthetic independently reviewed beginning bank balance.",
        )
        payable = self.item(kind="payable", party=self.supplier, amount="200")
        bank = self.movement("-50", reference="SYN-SHARED-RECOVERY-PHYSICAL-PAYMENT")
        cash = self.settle(payable, bank, amount="50")
        original_item = opening.opening_balance_source_snapshot(
            organization=self.org, item=payable
        )
        original_cash = opening.opening_settlement_source_snapshot(
            organization=self.org, settlement=cash
        )
        bill = ledger.create_bill(
            organization=self.org,
            actor=self.actor,
            supplier=self.supplier,
            amount="50",
            date=self.day,
            service_date=self.day,
            number="SYN-SHARED-RECOVERY-BILL",
            supplier_reference="SYN-SHARED-RECOVERY-CURRENT-SOURCE",
        )
        bill = ledger.approve_bill(organization=self.org, actor=self.actor, bill=bill)
        decision = dict(
            rule_code="no_withholding",
            base="0",
            period=self.day.replace(day=1),
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
            reviewed_on=self.day,
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
        context = opening.opening_balance_action_context(
            organization=self.org, action="cancel", settlement=cash
        )
        cancellation = opening.cancel_opening_settlement(
            organization=self.org,
            actor=self.actor,
            settlement=cash,
            reason="Synthetic bank payment belongs to the current Bill rather than this old payable.",
            evidence="Synthetic bank reference and both supplier documents checked independently.",
            request_key=str(uuid4()),
            expected_fingerprint=context["fingerprint"],
            confirmed=True,
        )
        self.assertEqual(
            (cancellation.date, cancellation.amount, bank.unallocated_amount),
            (self.day, Decimal(50), Decimal(-50)),
        )
        self.assertEqual(
            set(
                ledger.pending_bank_transactions(organization=self.org).values_list(
                    "pk", flat=True
                )
            ),
            {bank.pk},
        )
        balances = ledger.report_balances(organization=self.org, as_of=self.day)
        self.assertEqual(
            tuple(balances[key] for key in ("BANK", "AR", "AP", "EQUITY", "EXPENSE")),
            (Decimal(1000), Decimal(0), Decimal(-250), Decimal(-800), Decimal(50)),
        )

        payment = ledger.reconcile_bill_payment(
            organization=self.org,
            actor=self.actor,
            bill=bill,
            transaction=bank,
            amount="50",
        )
        facts = bill_budget_facts(organization=self.org, bill=bill, as_of=self.day)
        self.assertTrue(facts["current"])
        self.assertFalse(facts["issues"])
        self.assertEqual(
            (
                facts["ap_settled"],
                facts["verified_cash_outflow"],
                facts["ap_outstanding"],
            ),
            ("50.00", "50.00", "0.00"),
        )
        self.assertEqual(
            [
                (row["event_id"], row["cash_outflow"])
                for row in facts["events"]
                if row["kind"] == "bill_payment"
            ],
            [(f"bill_payment:{payment.pk}", "50.00")],
        )
        source_graph = bill_budget_source_identity(
            organization=self.org, bill=bill, as_of=self.day
        )
        self.assertEqual(
            source_graph,
            bill_budget_source_identity(
                organization=self.org, bill=bill, as_of=self.day
            ),
        )
        self.assertEqual(
            facts["fingerprint"],
            bill_budget_facts(organization=self.org, bill=bill, as_of=self.day)[
                "fingerprint"
            ],
        )
        self.assertEqual(
            (bank.unallocated_amount, self.position(payable)["remaining"]),
            (Decimal(0), Decimal(200)),
        )
        self.assertFalse(
            ledger.pending_bank_transactions(organization=self.org).exists()
        )
        self.assertEqual(
            (BankTransaction.objects.count(), BankPosting.objects.count()), (1, 1)
        )
        self.assertEqual(BankPosting.objects.get().pk, cash.bank_posting_id)
        self.assertEqual(
            opening.opening_balance_source_snapshot(
                organization=self.org, item=payable
            ),
            original_item,
        )
        self.assertEqual(
            opening.opening_settlement_source_snapshot(
                organization=self.org, settlement=cash
            ),
            original_cash,
        )
        close = opening.opening_balance_close_facts(
            organization=self.org, cutoff=date(2026, 9, 30)
        )
        self.assertTrue(close["current"])
        self.assertFalse(close["blockers"])
        self.assertEqual(
            (close["receivable"], close["payable"]), (Decimal(0), Decimal(200))
        )
        balances = ledger.report_balances(organization=self.org, as_of=self.day)
        self.assertEqual(
            tuple(balances[key] for key in ("BANK", "AR", "AP", "EQUITY", "EXPENSE")),
            (Decimal(950), Decimal(0), Decimal(-200), Decimal(-800), Decimal(50)),
        )
        self.assertEqual(
            tuple(
                balances[key] for key in ("REVENUE", "DEFERRED_REVENUE", "TAX_PAYABLE")
            ),
            (Decimal(0), Decimal(0), Decimal(0)),
        )


class OpeningCancellationFixtures(OpeningBalanceFixtures):
    def cancellation_data(self, settlement, **changes):
        data = dict(
            organization=self.org,
            actor=self.actor,
            settlement=settlement,
            reason="Synthetic confirmation that actual cash was assigned to the wrong old document.",
            evidence="Synthetic original bank evidence and signed assignment correction.",
            request_key=str(uuid4()),
            confirmed=True,
        )
        data.update(changes)
        data.setdefault(
            "expected_fingerprint",
            opening.opening_balance_action_context(
                organization=data["organization"],
                action="cancel",
                settlement=data["settlement"],
            )["fingerprint"],
        )
        return data

    def matched_receipt(self, *, amount="120", bank_amount=None):
        item = self.item()
        bank = self.movement(
            bank_amount or amount, reference=f"SYN-CANCEL-VALIDATION-{uuid4()}"
        )
        return item, bank, self.settle(item, bank, amount=amount)

    def recovery_counts(self):
        from .models import OpeningBalanceCancellation

        return (*self.counts(), OpeningBalanceCancellation.objects.count())


class OpeningCancellationValidationTests(OpeningCancellationFixtures, TestCase):
    def test_owner_authorization_precedes_foreign_lookup_and_exact_retry(self):
        from django.core.exceptions import PermissionDenied, ValidationError

        from core.models import Membership, Organization

        item, bank, cash = self.matched_receipt()
        data = self.cancellation_data(cash)
        for role in ("finance", "marketing", "reviewer", "auditor", "director"):
            actor = self.role(role)
            for selection in (cash, 999999999):
                before = self.recovery_counts()
                with (
                    self.subTest(
                        role=role, selection=getattr(selection, "pk", selection)
                    ),
                    self.assertRaises(PermissionDenied),
                ):
                    opening.cancel_opening_settlement(
                        **{**data, "actor": actor, "settlement": selection}
                    )
                self.assertEqual(self.recovery_counts(), before)
        before = self.recovery_counts()
        with self.assertRaises(PermissionDenied):
            opening.cancel_opening_settlement(**{**data, "actor": None})
        foreign = Organization.objects.create(
            name="Synthetic unrelated recovery company"
        )
        Membership.objects.create(organization=foreign, user=self.actor, role="owner")
        with self.assertRaises(ValidationError):
            opening.cancel_opening_settlement(**{**data, "organization": foreign})
        self.assertEqual(self.recovery_counts(), before)
        preview = opening.opening_balance_action_context(
            organization=foreign, action="cancel", settlement=cash
        )
        self.assertFalse(preview["current"])
        self.assertEqual(
            (preview["before"], preview["after"], preview["amount"]), (None, None, None)
        )
        event = opening.cancel_opening_settlement(**data)
        Membership.objects.filter(organization=self.org, user=self.actor).update(
            is_active=False
        )
        before = self.recovery_counts()
        with self.assertRaises(PermissionDenied):
            opening.cancel_opening_settlement(**data)
        self.assertEqual(self.recovery_counts(), before)
        self.assertEqual(
            (event.amount, self.position(item)["remaining"], bank.unallocated_amount),
            (Decimal(120), Decimal(300), Decimal(120)),
        )

    def test_exact_retry_after_close_is_read_only_and_changed_or_new_requests_do_not_cancel_twice(
        self,
    ):
        from django.core.exceptions import ValidationError

        from .models import AccountingPeriod

        item, bank, cash = self.matched_receipt()
        data = self.cancellation_data(cash)
        event = opening.cancel_opening_settlement(**data)
        receipt = opening.opening_cancellation_source_snapshot(
            organization=self.org, cancellation=event
        )
        AccountingPeriod.objects.update_or_create(
            organization=self.org, year=2026, month=10, defaults={"closed": True}
        )
        before = self.recovery_counts()
        self.assertEqual(opening.cancel_opening_settlement(**data).pk, event.pk)
        self.assertEqual(self.recovery_counts(), before)
        for changes in (
            {"reason": "Different correction instruction"},
            {"evidence": "Different evidence"},
            {"request_key": str(uuid4())},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                opening.cancel_opening_settlement(**{**data, **changes})
            self.assertEqual(self.recovery_counts(), before)
        self.assertEqual(
            opening.opening_cancellation_source_snapshot(
                organization=self.org, cancellation=event
            ),
            receipt,
        )
        self.assertEqual(
            (bank.unallocated_amount, self.position(item)["remaining"]),
            (Decimal(120), Decimal(300)),
        )

    def test_confirmation_and_stale_identity_or_competing_cash_claim_preserve_every_write(
        self,
    ):
        from django.core.exceptions import ValidationError

        item, bank, cash = self.matched_receipt(bank_amount="200")
        data = self.cancellation_data(cash)
        before = self.recovery_counts()
        with self.assertRaises(ValidationError):
            opening.cancel_opening_settlement(**{**data, "confirmed": False})
        self.assertEqual(self.recovery_counts(), before)
        self.account.name = "Synthetic bank identity changed after preview"
        self.account.save()
        before = self.recovery_counts()
        with self.assertRaises(ValidationError):
            opening.cancel_opening_settlement(**data)
        self.assertEqual(self.recovery_counts(), before)
        data = self.cancellation_data(cash)
        ledger.reconcile_bank_adjustment(
            organization=self.org,
            actor=self.actor,
            transaction=bank,
            kind="owner_funding",
            amount="10",
            reference="SYN-CANCEL-COMPETING-CLAIM",
            evidence="Synthetic independently verified owner funding in the same physical bank row.",
        )
        before = self.recovery_counts()
        with self.assertRaises(ValidationError):
            opening.cancel_opening_settlement(**data)
        self.assertEqual(self.recovery_counts(), before)
        self.assertEqual(
            (self.position(item)["remaining"], bank.unallocated_amount),
            (Decimal(180), Decimal(70)),
        )
        opening.cancel_opening_settlement(**self.cancellation_data(cash))
        self.assertEqual(
            (self.position(item)["remaining"], bank.unallocated_amount),
            (Decimal(300), Decimal(190)),
        )

    def test_original_closed_date_cannot_be_replaced_by_a_later_cancellation_date(self):
        from django.core.exceptions import ValidationError

        from .models import AccountingPeriod

        item, bank, cash = self.matched_receipt()
        data = self.cancellation_data(cash)
        AccountingPeriod.objects.update_or_create(
            organization=self.org, year=2026, month=9, defaults={"closed": True}
        )
        preview = opening.opening_balance_action_context(
            organization=self.org, action="cancel", settlement=cash
        )
        self.assertEqual(preview["date"], self.day)
        self.assertTrue(preview["blockers"])
        before = self.recovery_counts()
        with self.assertRaises(ValidationError):
            opening.cancel_opening_settlement(**data)
        with self.assertRaises(TypeError):
            opening.cancel_opening_settlement(**{**data, "date": date(2026, 10, 1)})
        with self.assertRaises(TypeError):
            opening.cancel_opening_settlement(**{**data, "amount": "1"})
        self.assertEqual(self.recovery_counts(), before)
        self.assertEqual(
            (self.position(item)["remaining"], bank.unallocated_amount),
            (Decimal(180), Decimal(0)),
        )

    def damaged_recovery_readbacks(self, item, bank, cash, cancellation):
        from django.core.exceptions import ValidationError

        position = self.position(item)
        close = opening.opening_balance_close_facts(
            organization=self.org, cutoff=date(2026, 9, 30)
        )
        preview = opening.opening_balance_action_context(
            organization=self.org, action="cancel", settlement=cash
        )
        for name, result in (
            ("position", position),
            ("close", close),
            ("preview", preview),
        ):
            with self.subTest(boundary=name):
                self.assertFalse(result["current"])
                self.assertTrue(result["blockers"])
        self.assertEqual(
            (
                position["remaining"],
                close["receivable"],
                close["payable"],
                preview["before"],
                preview["after"],
            ),
            (None, None, None, None, None),
        )
        for snapshot, keyword, source in (
            (opening.opening_balance_source_snapshot, "item", item),
            (opening.opening_settlement_source_snapshot, "settlement", cash),
            (
                opening.opening_cancellation_source_snapshot,
                "cancellation",
                cancellation,
            ),
        ):
            with self.subTest(receipt=keyword), self.assertRaises(ValidationError):
                snapshot(organization=self.org, **{keyword: source})
        with self.assertRaises(ValidationError):
            opening.opening_cash_cancellation_facts(
                organization=self.org, transaction=bank
            )
        with self.assertRaises(ValidationError):
            _ = bank.unallocated_amount
        with self.assertRaises(ValidationError):
            ledger.pending_bank_transactions(organization=self.org)
        return {
            "position": position["fingerprint"],
            "close": close["fingerprint"],
            "preview": preview["fingerprint"],
            "original": opening.opening_settlement_source_graph(
                organization=self.org, settlement=cash
            )["fingerprint"],
            "cancellation": opening.opening_cancellation_source_graph(
                organization=self.org, cancellation=cancellation
            )["fingerprint"],
        }, (position["blockers"], close["blockers"], preview["blockers"])

    def test_corrupt_recovery_journal_and_foreign_links_hold_unknown_and_advance_repair_identity(
        self,
    ):
        from core.models import Organization

        from .models import JournalLine, OpeningBalanceCancellation

        item, bank, cash = self.matched_receipt()
        cancellation = opening.cancel_opening_settlement(**self.cancellation_data(cash))
        lines = list(cancellation.journal.lines.all())
        for line in lines:
            self.corrupt(
                JournalLine, line.pk, "debit" if line.debit else "credit", Decimal(110)
            )
        first, messages = self.damaged_recovery_readbacks(
            item, bank, cash, cancellation
        )
        for line in lines:
            self.corrupt(
                JournalLine, line.pk, "debit" if line.debit else "credit", Decimal(130)
            )
        second, next_messages = self.damaged_recovery_readbacks(
            item, bank, cash, cancellation
        )
        self.assertEqual(messages, next_messages)
        for boundary in first:
            with self.subTest(repair_boundary=boundary):
                self.assertNotEqual(first[boundary], second[boundary])
        for line in lines:
            self.corrupt(
                JournalLine, line.pk, "debit" if line.debit else "credit", Decimal(120)
            )
        self.assertEqual(self.position(item)["remaining"], Decimal(300))
        foreign = Organization.objects.create(name="Synthetic foreign recovery fault")
        self.corrupt(
            OpeningBalanceCancellation, cancellation.pk, "organization", foreign.pk
        )
        first, _ = self.damaged_recovery_readbacks(item, bank, cash, cancellation)
        self.corrupt(
            BankTransaction,
            bank.pk,
            "description",
            "Synthetic original bank source changed during repair",
        )
        second, _ = self.damaged_recovery_readbacks(item, bank, cash, cancellation)
        for boundary in first:
            with self.subTest(foreign_repair_boundary=boundary):
                self.assertNotEqual(first[boundary], second[boundary])

    def test_deleted_cancellation_does_not_silently_release_cash_or_hide_its_typed_journal(
        self,
    ):
        from django.db import connection

        from .models import JournalLine, OpeningBalanceCancellation

        item, bank, cash = self.matched_receipt()
        cancellation = opening.cancel_opening_settlement(**self.cancellation_data(cash))
        table = connection.ops.quote_name(OpeningBalanceCancellation._meta.db_table)
        with connection.cursor() as cursor:
            cursor.execute(f"DELETE FROM {table} WHERE id = %s", [cancellation.pk])
        first, messages = self.damaged_recovery_readbacks(
            item, bank, cash, cancellation
        )
        line = cancellation.journal.lines.get(account="AR")
        self.corrupt(JournalLine, line.pk, "account", "EXPENSE")
        second, next_messages = self.damaged_recovery_readbacks(
            item, bank, cash, cancellation
        )
        self.assertEqual(messages, next_messages)
        for boundary in first:
            with self.subTest(orphan_repair_boundary=boundary):
                self.assertNotEqual(first[boundary], second[boundary])
        self.corrupt(
            BankTransaction,
            bank.pk,
            "description",
            "Synthetic original bank source changed after decision loss",
        )
        third, _ = self.damaged_recovery_readbacks(item, bank, cash, cancellation)
        for boundary in second:
            with self.subTest(orphan_original_bank_repair=boundary):
                self.assertNotEqual(second[boundary], third[boundary])

    def test_damaged_original_before_cancellation_retains_selection_and_changes_preview_repair_token(
        self,
    ):
        from django.core.exceptions import ValidationError

        from .models import JournalLine

        item, bank, cash = self.matched_receipt()
        data = self.cancellation_data(cash)
        line = cash.journal.lines.get(account="AR")
        self.corrupt(JournalLine, line.pk, "account", "EXPENSE")
        first = opening.opening_balance_action_context(
            organization=self.org, action="cancel", settlement=cash
        )
        first_graph = opening.opening_settlement_source_graph(
            organization=self.org, settlement=cash
        )
        self.corrupt(JournalLine, line.pk, "account", "PREPAID")
        second = opening.opening_balance_action_context(
            organization=self.org, action="cancel", settlement=cash
        )
        second_graph = opening.opening_settlement_source_graph(
            organization=self.org, settlement=cash
        )
        self.assertEqual(first["blockers"], second["blockers"])
        for preview in (first, second):
            self.assertFalse(preview["current"])
            self.assertEqual(
                (
                    preview["before"],
                    preview["after"],
                    preview["amount"],
                    preview["bank_after"],
                ),
                (None, None, None, None),
            )
        self.assertNotEqual(first_graph["fingerprint"], second_graph["fingerprint"])
        self.assertNotEqual(first["fingerprint"], second["fingerprint"])
        before = self.recovery_counts()
        with self.assertRaises(ValidationError):
            opening.cancel_opening_settlement(**data)
        self.assertEqual(self.recovery_counts(), before)
        self.assertEqual(
            opening.opening_cash_cancellation_facts(
                organization=self.org, transaction=bank
            )["released"],
            Decimal(0),
        )

    def test_audit_failure_rolls_back_reversal_and_saved_recovery_cannot_be_edited_or_deleted(
        self,
    ):
        from unittest.mock import patch

        from django.core.exceptions import ValidationError
        from django.db import DatabaseError

        from core.models import AuditEvent

        item, bank, cash = self.matched_receipt()
        data = self.cancellation_data(cash)
        before = self.recovery_counts()
        balances = ledger.report_balances(organization=self.org)
        with (
            patch.object(
                AuditEvent,
                "save",
                side_effect=DatabaseError("Synthetic audit persistence failure"),
            ),
            self.assertRaises(DatabaseError),
        ):
            opening.cancel_opening_settlement(**data)
        self.assertEqual(self.recovery_counts(), before)
        self.assertEqual(ledger.report_balances(organization=self.org), balances)
        self.assertEqual(
            (self.position(item)["remaining"], bank.unallocated_amount),
            (Decimal(180), Decimal(0)),
        )
        event = opening.cancel_opening_settlement(**data)
        self.assertFalse(hasattr(event, "_service_transition"))
        event.reason = "Changed historical correction reason"
        with self.assertRaises(ValidationError):
            event.save()
        with self.assertRaises(ValidationError):
            type(event).objects.filter(pk=event.pk).update(amount=Decimal(1))
        with self.assertRaises(ValidationError):
            event.delete()
        self.assertEqual(
            (self.position(item)["remaining"], bank.unallocated_amount),
            (Decimal(300), Decimal(120)),
        )
