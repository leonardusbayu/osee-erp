"""Public, synthetic supplier cash/application/refund conservation contracts."""

from datetime import date
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, connection
from django.test import TestCase

from core.models import AuditEvent, DomainEvent, Membership
from taxes import workflow as tax

from . import services as ledger
from . import supplier_advances as advances
from .models import (
    AccountingPeriod,
    BankPosting,
    BillPayment,
    Journal,
    Party,
    SupplierAdvance,
    SupplierAdvanceApplication,
    SupplierAdvanceApplicationReversal,
    SupplierAdvanceRefund,
)
from .tests_tax_money import TaxMoneyFixtures


class SupplierAdvanceFixtures(TaxMoneyFixtures):
    def setUp(self):
        super().setUp()
        self.day = date(2026, 1, 1)

    def bill(self, amount="600", day=date(2026, 1, 2), prepaid=False, supplier=None):
        row = ledger.create_bill(
            organization=self.org,
            actor=self.actor,
            supplier=supplier or self.supplier,
            amount=amount,
            date=day,
            service_date=date(2026, 2, 2) if prepaid else day,
            category="prepayment" if prepaid else "expense",
        )
        ledger.approve_bill(organization=self.org, actor=self.actor, bill=row)
        decision = dict(
            rule_code="no_withholding",
            base="0",
            period=day.replace(day=1),
            reason="Synthetic professional report for this exact supplier Bill.",
            object_code="",
        )
        tax.review_bill_tax(
            organization=self.org,
            actor=self.actor,
            bill=row,
            external_review=self.external(row, tax.bill_decision(**decision)),
            **decision,
        )
        row.refresh_from_db()
        return row

    def command(self, action, **values):
        data = dict(
            organization=self.org,
            actor=self.actor,
            source_reference=f"SYN-{uuid4()}",
            reason="Synthetic exact supplier decision reviewed.",
            evidence="Synthetic actual source evidence retained.",
            request_key=str(uuid4()),
            confirmed=True,
        )
        data.update(values)
        context_values = {
            k: data[k]
            for k in (
                "supplier",
                "transaction",
                "advance",
                "bill",
                "application",
                "date",
            )
            if k in data
        }
        data.setdefault(
            "expected_fingerprint",
            advances.supplier_advance_action_context(
                organization=self.org, action=action, **context_values
            )["fingerprint"],
        )
        function = {
            "record": advances.record_supplier_advance,
            "apply": advances.apply_supplier_advance,
            "refund": advances.refund_supplier_advance,
            "reverse": advances.reverse_supplier_advance_application,
        }[action]
        return function(**data)

    def advance(self, amount="1000", day=date(2026, 1, 1), reference=None):
        bank = self.movement(
            f"-{amount}", day=day, reference=reference or f"OUT-{uuid4()}"
        )
        return self.command(
            "record", supplier=self.supplier, transaction=bank, amount=amount
        )

    def position(self, row, day=date(2026, 1, 31)):
        return advances.supplier_advance_position(
            organization=self.org, advance=row, as_of=day
        )

    def counts(self):
        return tuple(
            model.objects.count()
            for model in (
                Journal,
                BankPosting,
                SupplierAdvance,
                SupplierAdvanceApplication,
                SupplierAdvanceRefund,
                SupplierAdvanceApplicationReversal,
                AuditEvent,
                DomainEvent,
            )
        )


class SupplierAdvanceTests(SupplierAdvanceFixtures, TestCase):
    def test_real_cash_before_bill_is_asset_and_pending_review(self):
        row = self.advance()
        balances = ledger.report_balances(organization=self.org)
        self.assertEqual(
            (
                balances["SUPPLIER_ADVANCE"],
                balances["BANK"],
                balances["AP"],
                balances["EXPENSE"],
                balances["PREPAID"],
                balances["TAX_PAYABLE"],
            ),
            (Decimal(1000), Decimal(-1000), 0, 0, 0, 0),
        )
        self.assertEqual(self.position(row)["remaining"], Decimal(1000))
        self.assertEqual(self.position(row)["tax_review_status"], "pending")
        self.assertFalse(BillPayment.objects.exists())

    def test_partial_applications_then_actual_partial_refunds_conserve_every_cutoff(
        self,
    ):
        row, bill = self.advance(), self.bill()
        self.command(
            "apply", advance=row, bill=bill, amount="400", date=date(2026, 1, 3)
        )
        self.assertEqual(
            (
                self.position(row, date(2026, 1, 3))["remaining"],
                bill.outstanding_amount,
            ),
            (Decimal(600), Decimal(200)),
        )
        self.command(
            "apply", advance=row, bill=bill, amount="200", date=date(2026, 1, 4)
        )
        for day, value, residual in ((5, "150", "250"), (6, "250", "0")):
            bank = self.movement(
                value, day=date(2026, 1, day), reference=f"REFUND-{day}"
            )
            self.command("refund", advance=row, transaction=bank, amount=value)
            self.assertEqual(
                self.position(row, date(2026, 1, day))["remaining"], Decimal(residual)
            )
        balances = ledger.report_balances(organization=self.org)
        self.assertEqual(
            (
                balances["SUPPLIER_ADVANCE"],
                balances["AP"],
                balances["BANK"],
                balances["EXPENSE"],
            ),
            (0, 0, Decimal(-600), Decimal(600)),
        )
        self.assertEqual(
            (self.position(row)["tax_review_status"], BillPayment.objects.count()),
            ("pending", 0),
        )

    def test_application_does_not_release_future_service_cost(self):
        row, bill = self.advance("600"), self.bill(prepaid=True)
        self.command(
            "apply", advance=row, bill=bill, amount="600", date=date(2026, 1, 3)
        )
        balances = ledger.report_balances(organization=self.org)
        self.assertEqual(
            (
                balances["PREPAID"],
                balances["EXPENSE"],
                balances["AP"],
                balances["BANK"],
            ),
            (Decimal(600), 0, 0, Decimal(-600)),
        )

    def test_ordinary_cash_pays_only_bill_residual_after_application(self):
        row, bill = self.advance("400"), self.bill()
        self.command(
            "apply", advance=row, bill=bill, amount="400", date=date(2026, 1, 3)
        )
        bank = self.movement(
            "-200", day=date(2026, 1, 4), reference="ORDINARY-RESIDUAL"
        )
        ledger.reconcile_bill_payment(
            organization=self.org,
            actor=self.actor,
            bill=bill,
            transaction=bank,
            amount="200",
        )
        self.assertEqual(bill.outstanding_amount, 0)
        self.assertEqual(
            ledger.report_balances(organization=self.org)["BANK"], Decimal(-600)
        )

    def test_full_reversal_after_real_payment_and_refund_restores_principal_not_cash(
        self,
    ):
        row, bill = self.advance(), self.bill("1000")
        application = self.command(
            "apply", advance=row, bill=bill, amount="700", date=date(2026, 1, 3)
        )
        ledger.reconcile_bill_payment(
            organization=self.org,
            actor=self.actor,
            bill=bill,
            transaction=self.movement(
                "-300", day=date(2026, 1, 4), reference="REAL-300"
            ),
            amount="300",
        )
        self.command(
            "refund",
            advance=row,
            amount="300",
            transaction=self.movement(
                "300", day=date(2026, 1, 5), reference="REAL-REFUND-300"
            ),
        )
        self.command("reverse", application=application, date=date(2026, 1, 6))
        self.assertEqual(
            (self.position(row)["remaining"], bill.outstanding_amount),
            (Decimal(700), Decimal(700)),
        )
        self.assertEqual(self.position(row, date(2026, 1, 5))["remaining"], 0)
        balances = ledger.report_balances(organization=self.org)
        self.assertEqual(
            (
                balances["BANK"],
                balances["EXPENSE"],
                balances["AP"],
                balances["SUPPLIER_ADVANCE"],
            ),
            (Decimal(-1000), Decimal(1000), Decimal(-700), Decimal(700)),
        )

    def reversed_case(self):
        row, bill = self.advance("100"), self.bill("100")
        application = self.command(
            "apply", advance=row, bill=bill, amount="100", date=date(2026, 1, 10)
        )
        self.command("reverse", application=application, date=date(2026, 1, 20))
        return row, bill

    def test_backdated_refund_cannot_borrow_capacity_from_later_reversal(self):
        row, bill = self.reversed_case()
        bank = self.movement("100", day=date(2026, 1, 15), reference="EARLIER-REFUND")
        before = self.counts()
        with self.assertRaises(ValidationError):
            self.command("refund", advance=row, transaction=bank, amount="100")
        self.assertEqual(self.counts(), before)
        context = advances.supplier_advance_action_context(
            organization=self.org,
            action="refund",
            advance=row,
            transaction=bank,
            amount="100",
        )
        self.assertTrue(context["current"])
        self.assertTrue(context["blockers"])

    def test_backdated_application_cannot_borrow_capacity_from_later_reversal(self):
        row, bill = self.reversed_case()
        with self.assertRaises(ValidationError):
            self.command(
                "apply", advance=row, bill=bill, amount="100", date=date(2026, 1, 15)
            )

    def test_ordinary_bank_date_cannot_make_historical_ap_negative(self):
        row, bill = self.reversed_case()
        bank = self.movement("-100", day=date(2026, 1, 15), reference="EARLIER-PAYMENT")
        before = self.counts()
        with self.assertRaises(ValidationError):
            ledger.reconcile_bill_payment(
                organization=self.org,
                actor=self.actor,
                bill=bill,
                transaction=bank,
                amount="100",
            )
        self.assertEqual(self.counts(), before)
        self.assertEqual(bank.date, date(2026, 1, 15))

    def test_legitimate_late_import_with_nonnegative_prefixes_is_allowed(self):
        row, bill = self.advance("50"), self.bill("100")
        self.command(
            "apply", advance=row, bill=bill, amount="50", date=date(2026, 1, 10)
        )
        ledger.reconcile_bill_payment(
            organization=self.org,
            actor=self.actor,
            bill=bill,
            transaction=self.movement(
                "-50", day=date(2026, 1, 5), reference="VALID-LATE-IMPORT"
            ),
            amount="50",
        )
        self.assertEqual(bill.outstanding_amount, 0)

    def test_exact_retry_after_exhaustion_reversal_and_close_creates_nothing(self):
        row, bill = self.advance("100"), self.bill("100")
        context = advances.supplier_advance_action_context(
            organization=self.org, action="apply", advance=row, bill=bill
        )
        data = dict(
            organization=self.org,
            actor=self.actor,
            advance=row,
            bill=bill,
            amount="100",
            date=date(2026, 1, 3),
            source_reference="RETRY-APPLICATION",
            reason="Synthetic reviewed application.",
            evidence="Synthetic immutable proof.",
            request_key=str(uuid4()),
            expected_fingerprint=context["fingerprint"],
            confirmed=True,
        )
        application = advances.apply_supplier_advance(**data)
        self.command("reverse", application=application, date=date(2026, 2, 3))
        AccountingPeriod.objects.update_or_create(
            organization=self.org, year=2026, month=1, defaults={"closed": True}
        )
        before = self.counts()
        self.assertEqual(advances.apply_supplier_advance(**data).pk, application.pk)
        self.assertEqual(self.counts(), before)
        self.assertEqual(
            self.position(row, date(2026, 2, 3))["remaining"], Decimal(100)
        )
        with self.assertRaises(ValidationError):
            advances.apply_supplier_advance(**{**data, "amount": "99"})

    def test_stale_review_and_unconfirmed_input_have_no_effect(self):
        bank = self.movement("-100", reference="FRESHNESS-BANK")
        context = advances.supplier_advance_action_context(
            organization=self.org,
            action="record",
            supplier=self.supplier,
            transaction=bank,
        )
        data = dict(
            supplier=self.supplier,
            transaction=bank,
            amount="50",
            expected_fingerprint=context["fingerprint"],
        )
        with self.assertRaises(ValidationError):
            self.command("record", **data, confirmed=False)
        self.command("record", **data)
        before = self.counts()
        with self.assertRaises(ValidationError):
            self.command("record", **data)
        self.assertEqual(self.counts(), before)

    def test_record_and_refund_exact_retry_after_cash_exhausted_have_zero_writes(self):
        bank = self.movement("-100", reference="RECORD-RETRY-BANK")
        fp = advances.supplier_advance_action_context(
            organization=self.org,
            action="record",
            supplier=self.supplier,
            transaction=bank,
        )["fingerprint"]
        data = dict(
            organization=self.org,
            actor=self.actor,
            supplier=self.supplier,
            transaction=bank,
            amount="100",
            source_reference="RECORD-RETRY",
            reason="Synthetic reviewed cash source.",
            evidence="Synthetic exact bank evidence.",
            request_key=str(uuid4()),
            expected_fingerprint=fp,
            confirmed=True,
        )
        row = advances.record_supplier_advance(**data)
        returned = self.movement(
            "100", day=date(2026, 1, 3), reference="REFUND-RETRY-BANK"
        )
        refund_data = dict(
            organization=self.org,
            actor=self.actor,
            advance=row,
            transaction=returned,
            amount="100",
            reason="Synthetic exact supplier refund.",
            evidence="Synthetic real incoming bank source.",
            request_key=str(uuid4()),
            confirmed=True,
            expected_fingerprint=advances.supplier_advance_action_context(
                organization=self.org,
                action="refund",
                advance=row,
                transaction=returned,
            )["fingerprint"],
        )
        refund = advances.refund_supplier_advance(**refund_data)
        before = self.counts()
        self.assertEqual(advances.record_supplier_advance(**data).pk, row.pk)
        self.assertEqual(advances.refund_supplier_advance(**refund_data).pk, refund.pk)
        self.assertEqual(self.counts(), before)

    def test_database_write_failure_rolls_back_cash_posting_and_all_history(self):
        bank = self.movement("-100", reference="ROLLBACK-BANK")
        before = self.counts()
        # Fault at the database persistence boundary, not a substitute posting service.
        with (
            patch.object(
                DomainEvent,
                "save",
                side_effect=DatabaseError("Synthetic persistence failure"),
            ),
            self.assertRaises(DatabaseError),
        ):
            self.command(
                "record", supplier=self.supplier, transaction=bank, amount="100"
            )
        self.assertEqual(self.counts(), before)
        self.assertEqual(bank.unallocated_amount, Decimal(-100))

    def test_cash_shared_with_old_payment_types_has_one_capacity(self):
        bank = self.movement("-100", reference="SHARED-CASH")
        ledger.reconcile_bank_adjustment(
            organization=self.org,
            actor=self.actor,
            transaction=bank,
            kind="bank_fee",
            amount="30",
            reference="SHARED-FEE",
            evidence="Synthetic actual bank fee.",
        )
        row = self.command(
            "record", supplier=self.supplier, transaction=bank, amount="70"
        )
        self.assertEqual(
            (bank.unallocated_amount, self.position(row)["remaining"]), (0, Decimal(70))
        )
        with self.assertRaises(ValidationError):
            self.command("record", supplier=self.supplier, transaction=bank, amount="1")

    def test_read_fallback_exposes_damaged_original_proof_and_retry_rejects(self):
        bank = self.movement("-100", reference="DAMAGE-CASH")
        context = advances.supplier_advance_action_context(
            organization=self.org,
            action="record",
            supplier=self.supplier,
            transaction=bank,
        )
        data = dict(
            organization=self.org,
            actor=self.actor,
            supplier=self.supplier,
            transaction=bank,
            amount="100",
            source_reference="DAMAGE-PROOF",
            reason="Synthetic reviewed cash.",
            evidence="Synthetic original source.",
            request_key=str(uuid4()),
            expected_fingerprint=context["fingerprint"],
            confirmed=True,
        )
        row = advances.record_supplier_advance(**data)
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE finance_journalline SET account=%s WHERE journal_id=%s AND account=%s",
                ["EXPENSE", row.journal_id, "SUPPLIER_ADVANCE"],
            )
        position = self.position(row)
        self.assertFalse(position["current"])
        self.assertIsNone(position["remaining"])
        with self.assertRaises(ValidationError):
            advances.record_supplier_advance(**data)

    def test_supplier_rename_does_not_rewrite_original_financial_proof(self):
        row = self.advance()
        before = advances.supplier_advance_source_snapshot(
            organization=self.org, advance=row
        )
        self.supplier.name = "Synthetic legitimate legal-name correction"
        self.supplier.save()
        self.assertEqual(
            advances.supplier_advance_source_snapshot(
                organization=self.org, advance=row
            ),
            before,
        )

    def test_role_scope_sign_decimal_and_same_supplier_are_enforced(self):
        row, bill = self.advance(), self.bill()
        other = Party.objects.create(
            organization=self.org, name="Other synthetic supplier", kind="supplier"
        )
        other_bill = self.bill("100", supplier=other)
        with self.assertRaises(ValidationError):
            self.command(
                "apply",
                advance=row,
                bill=other_bill,
                amount="100",
                date=date(2026, 1, 3),
            )
        with self.assertRaises(ValidationError):
            self.command(
                "record",
                supplier=other,
                transaction=self.movement("100", reference="WRONG-SIGN"),
                amount="100",
            )
        for value in (True, 1.0, "0", "-1", "1.001"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self.command(
                    "apply", advance=row, bill=bill, amount=value, date=date(2026, 1, 3)
                )
        user = get_user_model().objects.create_user(
            username="supplier-advance-marketing"
        )
        Membership.objects.create(organization=self.org, user=user, role="marketing")
        with self.assertRaises(PermissionDenied):
            self.command(
                "apply",
                actor=user,
                advance=row,
                bill=bill,
                amount="100",
                date=date(2026, 1, 3),
            )

    def test_finance_cannot_reverse_and_an_application_cannot_be_reversed_twice(self):
        row, bill = self.advance("100"), self.bill("100")
        application = self.command(
            "apply", advance=row, bill=bill, amount="100", date=date(2026, 1, 3)
        )
        user = get_user_model().objects.create_user(username="supplier-advance-finance")
        Membership.objects.create(organization=self.org, user=user, role="finance")
        with self.assertRaises(PermissionDenied):
            self.command(
                "reverse", actor=user, application=application, date=date(2026, 1, 4)
            )
        self.command("reverse", application=application, date=date(2026, 1, 4))
        self.assertEqual(self.position(row)["effective_applied"], 0)
        with self.assertRaises(ValidationError):
            self.command("reverse", application=application, date=date(2026, 1, 5))

    def test_immutable_events_do_not_retain_service_privilege(self):
        row = self.advance()
        self.assertFalse(hasattr(row, "_service_transition"))
        row.amount = Decimal(999)
        with self.assertRaises(ValidationError):
            row.save()
        with self.assertRaises(ValidationError):
            SupplierAdvance.objects.filter(pk=row.pk).update(amount=Decimal(999))
        with self.assertRaises(ValidationError):
            row.delete()

    def test_close_holds_pending_review_even_when_cash_is_fully_refunded(self):
        row = self.advance("100")
        self.command(
            "refund",
            advance=row,
            amount="100",
            transaction=self.movement(
                "100", day=date(2026, 1, 3), reference="FULL-CASH-RETURN"
            ),
        )
        self.assertEqual(self.position(row)["remaining"], 0)
        with self.assertRaisesMessage(ValidationError, "pajak"):
            ledger.close_month(
                organization=self.org, actor=self.actor, year=2026, month=1
            )
