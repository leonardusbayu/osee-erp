from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase

from core.models import Membership, Organization
from finance.fulfillment import deliver_units, fulfillment_summary
from finance.models import BankAccount, BankTransaction, Party, Product
from finance.services import (
    create_invoice,
    issue_invoice,
    reconcile_receipt,
    report_balances,
)


class PartialFulfillmentTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Synthetic fulfillment")
        self.owner = get_user_model().objects.create_user(username="fulfillment-owner")
        Membership.objects.create(organization=self.org, user=self.owner, role="owner")
        self.party = Party.objects.create(
            organization=self.org, kind="reseller", name="Synthetic mitra"
        )
        self.product = Product.objects.create(
            organization=self.org,
            code="ITP",
            name="ITP",
            kind="itp",
            default_price="510000",
        )
        self.day = date(2026, 1, 5)
        self.invoice = issue_invoice(
            organization=self.org,
            actor=self.owner,
            invoice=create_invoice(
                organization=self.org,
                actor=self.owner,
                party=self.party,
                product=self.product,
                quantity=2,
                date=self.day,
            ),
        )
        self.account = BankAccount.objects.create(
            organization=self.org, name="Synthetic bank", account_number="SYNTHETIC"
        )
        bank = BankTransaction.objects.create(
            organization=self.org,
            account=self.account,
            date=self.day,
            reference="SYNTHETIC-PAID",
            amount="1020000",
        )
        reconcile_receipt(
            organization=self.org,
            actor=self.owner,
            invoice=self.invoice,
            transaction=bank,
            amount="1020000",
        )

    def test_one_participant_releases_only_one_unit_and_retry_is_idempotent(self):
        first = deliver_units(
            organization=self.org,
            actor=self.owner,
            invoice=self.invoice,
            quantity=1,
            date=self.day,
            reference="PARTICIPANT-1",
            evidence="Signed attendance one",
        )
        replay = deliver_units(
            organization=self.org,
            actor=self.owner,
            invoice=self.invoice,
            quantity=1,
            date=self.day,
            reference="PARTICIPANT-1",
            evidence="Signed attendance one",
        )
        self.assertEqual(first.pk, replay.pk)
        self.assertEqual(
            fulfillment_summary(organization=self.org, invoice=self.invoice)[
                "remaining_quantity"
            ],
            1,
        )
        balances = report_balances(organization=self.org)
        self.assertEqual(balances["REVENUE"], Decimal(-510000))
        self.assertEqual(balances["DEFERRED_REVENUE"], Decimal(-510000))
        self.assertEqual(sum(balances.values()), 0)

    def test_cancel_one_unfulfilled_unit_preserves_completed_revenue_and_refund_liability(
        self,
    ):
        from finance.fulfillment import credit_units, refund_credit

        deliver_units(
            organization=self.org,
            actor=self.owner,
            invoice=self.invoice,
            quantity=1,
            date=self.day,
            reference="COMPLETE-ONE",
            evidence="Attendance",
        )
        credit = credit_units(
            organization=self.org,
            actor=self.owner,
            invoice=self.invoice,
            quantity=1,
            kind="unfulfilled",
            date=self.day,
            reference="CREDIT-ONE",
            evidence="Owner approved cancellation",
        )
        facts = fulfillment_summary(organization=self.org, invoice=self.invoice)
        self.assertEqual(facts["net_total"], Decimal(510000))
        self.assertEqual(facts["remaining_quantity"], 0)
        balances = report_balances(organization=self.org)
        self.assertEqual(balances["REVENUE"], Decimal(-510000))
        self.assertEqual(balances["DEFERRED_REVENUE"], 0)
        self.assertEqual(balances["CUSTOMER_REFUND"], Decimal(-510000))
        outgoing = BankTransaction.objects.create(
            organization=self.org,
            account=self.account,
            date=self.day,
            reference="SYNTHETIC-REFUND",
            amount="-510000",
        )
        refund_credit(
            organization=self.org,
            actor=self.owner,
            credit=credit,
            transaction=outgoing,
            amount="510000",
            reference="RETURN-ONE",
            evidence="Verified outgoing statement",
        )
        self.assertEqual(report_balances(organization=self.org)["CUSTOMER_REFUND"], 0)
        with self.assertRaises(ValidationError):
            credit_units(
                organization=self.org,
                actor=self.owner,
                invoice=self.invoice,
                quantity=1,
                kind="unfulfilled",
                date=self.day,
                reference="OVER-CANCEL",
                evidence="Too many units",
            )

    def test_legacy_complete_after_partial_does_not_post_original_total_twice(self):
        from finance.services import cancel_invoice, record_delivery

        deliver_units(
            organization=self.org,
            actor=self.owner,
            invoice=self.invoice,
            quantity=1,
            date=self.day,
            reference="FIRST",
            evidence="First attendance",
        )
        with self.assertRaises(ValidationError):
            cancel_invoice(
                organization=self.org,
                actor=self.owner,
                invoice=self.invoice,
                date=self.day,
                reason="Invalid whole cancellation",
            )
        invoice = record_delivery(
            organization=self.org,
            actor=self.owner,
            invoice=self.invoice,
            date=self.day,
            evidence="Remaining attended",
        )
        self.assertEqual(invoice.status, "delivered")
        self.assertEqual(
            report_balances(organization=self.org)["REVENUE"], Decimal(-1020000)
        )
        self.assertEqual(report_balances(organization=self.org)["DEFERRED_REVENUE"], 0)

    def test_service_credit_has_separate_revenue_and_closed_period_protection(self):
        from finance.fulfillment import credit_units
        from finance.models import AccountingPeriod
        from finance.services import record_delivery

        record_delivery(
            organization=self.org,
            actor=self.owner,
            invoice=self.invoice,
            date=self.day,
            evidence="Legacy full roster",
        )
        credit_units(
            organization=self.org,
            actor=self.owner,
            invoice=self.invoice,
            quantity=1,
            kind="service",
            date=self.day,
            reference="SERVICE-CREDIT",
            evidence="Approved service correction",
        )
        self.assertEqual(
            report_balances(organization=self.org)["REVENUE"], Decimal(-510000)
        )
        self.assertEqual(report_balances(organization=self.org)["DEFERRED_REVENUE"], 0)
        period = AccountingPeriod.objects.get(organization=self.org, year=2026, month=1)
        period.closed = True
        period.save()
        with self.assertRaises(ValidationError):
            credit_units(
                organization=self.org,
                actor=self.owner,
                invoice=self.invoice,
                quantity=1,
                kind="service",
                date=self.day,
                reference="CLOSED-CREDIT",
                evidence="Must be rejected",
            )

    def test_finance_cannot_approve_credit_and_foreign_invoice_is_rejected(self):
        from finance.fulfillment import credit_units

        staff = get_user_model().objects.create_user(username="credit-finance")
        Membership.objects.create(organization=self.org, user=staff, role="finance")
        with self.assertRaises(PermissionDenied):
            credit_units(
                organization=self.org,
                actor=staff,
                invoice=self.invoice,
                quantity=1,
                kind="unfulfilled",
                date=self.day,
                reference="NO-APPROVAL",
                evidence="Not owner",
            )
        other = Organization.objects.create(name="Other")
        Membership.objects.create(organization=other, user=self.owner, role="owner")
        with self.assertRaises(ValidationError):
            deliver_units(
                organization=other,
                actor=self.owner,
                invoice=self.invoice,
                quantity=1,
                date=self.day,
                reference="FOREIGN",
                evidence="Wrong company",
            )
