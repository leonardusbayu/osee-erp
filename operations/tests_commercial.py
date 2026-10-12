from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from finance.models import BankAccount, BankTransaction
from finance.services import cancel_invoice, reconcile_receipt

from .analytics import commercial_snapshot
from .services import (
    add_order_line,
    create_order,
    invoice_line,
    invoice_quote,
    register_participant,
)
from .tests import OrderIntakeTests


class CommercialFactsTests(TestCase):
    setUp = OrderIntakeTests.setUp

    def test_price_preview_is_stale_safe_and_registration_cannot_reopen_cancelled_sale(
        self,
    ):
        order = create_order(
            organization=self.org,
            actor=self.actor,
            reference="QUOTE",
            buyer=self.buyer,
            date="2026-01-05",
        )
        line = add_order_line(
            organization=self.org,
            actor=self.actor,
            order=order,
            product=self.product,
            quantity=1,
        )
        quote = invoice_quote(
            organization=self.org, actor=self.actor, line=line, date="2026-01-05"
        )
        self.product.default_price = Decimal(530000)
        self.product.save()
        with self.assertRaises(ValidationError):
            invoice_line(
                organization=self.org,
                actor=self.actor,
                line=line,
                date="2026-01-05",
                expected_price_fingerprint=quote["fingerprint"],
            )
        inv = invoice_line(
            organization=self.org, actor=self.actor, line=line, date="2026-01-05"
        )
        cancel_invoice(
            organization=self.org,
            actor=self.actor,
            invoice=inv,
            date="2026-01-06",
            reason="Buyer cancelled",
        )
        with self.assertRaises(ValidationError):
            register_participant(
                organization=self.org,
                actor=self.actor,
                line=line,
                source="form",
                external_id="NO",
                name="Cannot reopen",
            )

    def test_commercial_paid_counts_respect_historical_cancellation_date(self):
        order = create_order(
            organization=self.org,
            actor=self.actor,
            reference="PAID",
            buyer=self.buyer,
            date="2026-01-05",
        )
        line = add_order_line(
            organization=self.org,
            actor=self.actor,
            order=order,
            product=self.product,
            quantity=1,
        )
        inv = invoice_line(
            organization=self.org, actor=self.actor, line=line, date="2026-01-05"
        )
        account = BankAccount.objects.create(
            organization=self.org, name="Synthetic", account_number="TEST"
        )
        bank = BankTransaction.objects.create(
            organization=self.org,
            account=account,
            date="2026-01-05",
            reference="PAY",
            amount="510000",
        )
        reconcile_receipt(
            organization=self.org,
            actor=self.actor,
            invoice=inv,
            transaction=bank,
            amount="510000",
        )
        cancel_invoice(
            organization=self.org,
            actor=self.actor,
            invoice=inv,
            date="2026-02-01",
            reason="Buyer cancelled",
        )
        before = commercial_snapshot(
            organization=self.org, start=date(2026, 1, 1), end=date(2026, 1, 31)
        )
        after = commercial_snapshot(
            organization=self.org, start=date(2026, 1, 1), end=date(2026, 2, 28)
        )
        self.assertEqual(before["cohort_fully_paid_orders"], 1)
        self.assertEqual(after["cohort_fully_paid_orders"], 0)
        self.assertIsNone(before["cohort_actual_margin"])

    def test_grouped_invoices_count_cash_once_and_actual_cost_gates_margin(self):
        from datetime import datetime

        from django.utils import timezone

        from finance.costing import allocate_cost
        from finance.fulfillment import deliver_units
        from finance.models import Party
        from finance.services import approve_bill, create_bill
        from marketing.analytics import marketing_snapshot
        from marketing.services import create_lead
        from operations.services import link_order_lead

        order = create_order(
            organization=self.org,
            actor=self.actor,
            reference="GROUP",
            buyer=self.buyer,
            date="2026-01-05",
        )
        lead = create_lead(
            organization=self.org,
            actor=self.actor,
            reference="LEAD",
            name="Group opportunity",
            channel="mitra",
            party=self.buyer,
            segment="reseller",
            product=self.product,
            received_at=timezone.make_aware(datetime(2026, 1, 4, 10)),
        )
        link_order_lead(
            organization=self.org,
            actor=self.actor,
            order=order,
            lead=lead,
            expected_version=1,
            reason="Confirmed source",
        )
        account = BankAccount.objects.create(
            organization=self.org, name="Synthetic", account_number="TEST"
        )
        invoices = []
        for n in range(2):
            line = add_order_line(
                organization=self.org,
                actor=self.actor,
                order=order,
                product=self.product,
                quantity=1,
                reference=str(n),
            )
            inv = invoice_line(
                organization=self.org, actor=self.actor, line=line, date="2026-01-05"
            )
            bank = BankTransaction.objects.create(
                organization=self.org,
                account=account,
                date="2026-01-05",
                reference=str(n),
                amount="510000",
            )
            reconcile_receipt(
                organization=self.org,
                actor=self.actor,
                invoice=inv,
                transaction=bank,
                amount="510000",
            )
            invoices.append(inv)
        for n, inv in enumerate(invoices):
            deliver_units(
                organization=self.org,
                actor=self.actor,
                invoice=inv,
                quantity=1,
                date="2026-01-05",
                reference=str(n),
                evidence="Delivery",
            )
        facts = commercial_snapshot(
            organization=self.org, start=date(2026, 1, 1), end=date(2026, 1, 31)
        )
        self.assertEqual(facts["cohort_fully_paid_orders"], 1)
        self.assertEqual(Decimal(facts["cohort_net_recognized_revenue"]), 1020000)
        self.assertFalse(facts["cost_complete"])
        report = marketing_snapshot(
            organization=self.org,
            start=date(2026, 1, 1),
            end=date(2026, 1, 31),
            group_by="channel",
        )
        self.assertEqual(Decimal(report["summary"]["cash_in"]), 1020000)
        supplier = Party.objects.create(
            organization=self.org, name="Synthetic provider", kind="supplier"
        )
        bill = approve_bill(
            organization=self.org,
            actor=self.actor,
            bill=create_bill(
                organization=self.org,
                actor=self.actor,
                supplier=supplier,
                amount="900000",
                date="2026-01-05",
                service_date="2026-01-05",
                category="provider",
            ),
        )
        for n, inv in enumerate(invoices):
            allocate_cost(
                organization=self.org,
                actor=self.actor,
                invoice=inv,
                bill=bill,
                quantity=1,
                amount="450000",
                date="2026-01-05",
                reference="COST" + str(n),
                reason="Provider unit cost",
                evidence="Confirmed invoice unit",
            )
        final = commercial_snapshot(
            organization=self.org, start=date(2026, 1, 1), end=date(2026, 1, 31)
        )
        self.assertTrue(final["cost_complete"])
        self.assertEqual(Decimal(final["cohort_actual_margin"]), 120000)

    def test_partner_delivery_requires_whole_order_payment_and_credited_lines_do_not_block_retained_order(
        self,
    ):
        from finance.fulfillment import credit_units, deliver_units

        order = create_order(
            organization=self.org,
            actor=self.actor,
            reference="WHOLE",
            buyer=self.buyer,
            date="2026-01-05",
        )
        one = add_order_line(
            organization=self.org,
            actor=self.actor,
            order=order,
            product=self.product,
            quantity=1,
            reference="A",
        )
        two = add_order_line(
            organization=self.org,
            actor=self.actor,
            order=order,
            product=self.product,
            quantity=1,
            reference="B",
        )
        first = invoice_line(
            organization=self.org, actor=self.actor, line=one, date="2026-01-05"
        )
        second = invoice_line(
            organization=self.org, actor=self.actor, line=two, date="2026-01-05"
        )
        account = BankAccount.objects.create(
            organization=self.org, name="Synthetic", account_number="TEST"
        )
        bank = BankTransaction.objects.create(
            organization=self.org,
            account=account,
            date="2026-01-05",
            reference="PAY",
            amount="510000",
        )
        reconcile_receipt(
            organization=self.org,
            actor=self.actor,
            invoice=first,
            transaction=bank,
            amount="510000",
        )
        delivery = dict(
            organization=self.org,
            actor=self.actor,
            invoice=first,
            quantity=1,
            date="2026-01-05",
            reference="DELIVER",
            evidence="Completed test",
        )
        with self.assertRaises(ValidationError):
            deliver_units(**delivery)
        credit_units(
            organization=self.org,
            actor=self.actor,
            invoice=second,
            quantity=1,
            kind="unfulfilled",
            date="2026-01-05",
            reference="CREDIT",
            evidence="Cancelled other component",
        )
        deliver_units(**delivery)
        facts = commercial_snapshot(
            organization=self.org, start=date(2026, 1, 1), end=date(2026, 1, 31)
        )
        self.assertEqual(facts["cohort_fully_paid_orders"], 1)
