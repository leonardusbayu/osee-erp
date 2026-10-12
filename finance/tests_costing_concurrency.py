"""Cost caps and approved-quote consumption serialize under PostgreSQL locks."""

from unittest import skipUnless

from django.db import connection
from django.test import TransactionTestCase

from imports import tests_review_concurrency as races

from . import costing
from . import tests_costing as fixtures
from . import tests_fulfillment as fulfillment_fixtures
from .costing_models import CostAllocation, PriceOverrideUse
from .fulfillment import deliver_units
from .models import Invoice
from .services import create_invoice


@skipUnless(connection.vendor == "postgresql", "Requires real PostgreSQL row locks")
class ConcurrentCostingTests(TransactionTestCase):
    setUp = fulfillment_fixtures.PartialFulfillmentTests.setUp
    bill = fixtures.ActualCostTests.bill
    race = races.ConcurrentSourceReviews.race

    def test_competing_cost_allocations_cannot_exceed_bill_or_completed_unit_caps(self):
        bill = self.bill()
        deliver_units(
            organization=self.org,
            actor=self.owner,
            invoice=self.invoice,
            quantity=2,
            date=self.day,
            reference="RACE-COST-DELIVERY",
            evidence="Attendance",
        )
        result = self.race(
            *[
                lambda reference=reference: costing.allocate_cost(
                    organization=self.org,
                    actor=self.owner,
                    invoice=self.invoice,
                    bill=bill,
                    quantity=2,
                    amount="900000",
                    date=self.day,
                    reference=reference,
                    reason="Provider cost",
                    evidence="Supplier invoice",
                )
                for reference in ("A", "B")
            ]
        )
        self.assertCountEqual([row[0] for row in result], ["committed", "rejected"])
        self.assertEqual(CostAllocation.objects.count(), 1)
        self.assertTrue(
            costing.cost_facts(organization=self.org, invoice=self.invoice)["complete"]
        )

    def test_approved_quote_can_be_consumed_by_only_one_invoice(self):
        facts = {
            "organization": self.org,
            "actor": self.owner,
            "party": self.party,
            "product": self.product,
            "quantity": 1,
            "date": self.day,
            "unit_price": "505000",
        }
        request = costing.request_price_override(
            **facts, reason="Negotiated", evidence="Offer"
        )
        costing.decide_price_override(
            organization=self.org,
            actor=self.owner,
            request=request,
            expected_fingerprint=request.fingerprint,
            decision="approved",
            reason="Reviewed margin",
            evidence="Approval",
        )
        result = self.race(
            *[lambda: create_invoice(**facts, price_override=request) for _ in range(2)]
        )
        self.assertCountEqual([row[0] for row in result], ["committed", "rejected"])
        self.assertEqual(PriceOverrideUse.objects.count(), 1)
        self.assertEqual(
            Invoice.objects.count(), 2
        )  # The paid fixture invoice and the sole approved quote.
