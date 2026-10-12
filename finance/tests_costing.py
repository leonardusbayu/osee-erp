"""Actual unit cost and exceptional price approval through public commands."""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import Membership, Organization

from . import costing
from . import tests_fulfillment as fixtures
from .costing_models import CostAllocation, CostReversal, PriceOverrideDecision
from .fulfillment import deliver_units
from .models import AccountingPeriod, Invoice, Journal, Party
from .services import approve_bill, create_bill, create_invoice, issue_invoice


@override_settings(MFA_REQUIRED=False)
class ActualCostTests(TestCase):
    setUp = fixtures.PartialFulfillmentTests.setUp

    def bill(self, amount="900000"):
        supplier = Party.objects.create(
            organization=self.org, name="Synthetic provider", kind="supplier"
        )
        return approve_bill(
            organization=self.org,
            actor=self.owner,
            bill=create_bill(
                organization=self.org,
                actor=self.owner,
                supplier=supplier,
                amount=amount,
                date=self.day,
                service_date=self.day,
                category="provider",
            ),
        )

    def test_actual_cost_requires_completed_unit_coverage_and_reversal_preserves_history(
        self,
    ):
        bill = self.bill()
        deliver_units(
            organization=self.org,
            actor=self.owner,
            invoice=self.invoice,
            quantity=2,
            date=self.day,
            reference="COST-DELIVERY",
            evidence="Synthetic attendance",
        )
        journals = Journal.objects.count()
        facts = {
            "organization": self.org,
            "actor": self.owner,
            "invoice": self.invoice,
            "bill": bill,
            "quantity": 1,
            "amount": "450000",
            "date": self.day,
            "reason": "Provider unit cost",
            "evidence": "Provider invoice one tested unit",
        }
        first = costing.allocate_cost(**facts, reference="COST-1")
        self.assertFalse(
            costing.cost_facts(organization=self.org, invoice=self.invoice)["complete"]
        )
        second = costing.allocate_cost(**facts, reference="COST-2")
        self.assertEqual(
            costing.allocate_cost(**facts, reference="COST-2").pk, second.pk
        )
        cost = costing.cost_facts(organization=self.org, invoice=self.invoice)
        self.assertTrue(cost["complete"])
        self.assertEqual(cost["actual_cost"], Decimal(900000))
        self.assertEqual(cost["allocated_quantity"], 2)
        with self.assertRaises(ValidationError):
            costing.allocate_cost(**facts, reference="OVER-COST")
        costing.reverse_cost(
            organization=self.org,
            actor=self.owner,
            allocation=first,
            date=self.day,
            reference="CORRECTION-1",
            reason="Wrong provider assignment",
            evidence="Revised unit mapping",
        )
        revised = costing.cost_facts(organization=self.org, invoice=self.invoice)
        self.assertFalse(revised["complete"])
        self.assertIsNone(revised["actual_cost"])
        self.assertNotEqual(revised["fingerprint"], cost["fingerprint"])
        self.assertEqual(
            Journal.objects.count(),
            journals,
            "Cost assignment must not post supplier expense a second time",
        )
        self.assertEqual(self.invoice.cost_allocations.count(), 2)

    def test_finance_price_override_needs_owner_approval_bound_to_one_exact_invoice(
        self,
    ):
        finance = get_user_model().objects.create_user("cost-finance")
        Membership.objects.create(organization=self.org, user=finance, role="finance")
        facts = {
            "organization": self.org,
            "actor": finance,
            "party": self.party,
            "product": self.product,
            "quantity": 2,
            "date": self.day,
            "unit_price": "505000",
        }
        with self.assertRaises(ValidationError):
            create_invoice(
                **facts, override_reason="Finance alone cannot approve special price"
            )
        request = costing.request_price_override(
            **facts,
            reason="Mitra negotiated group price",
            evidence="Written offer reference 12",
        )
        with self.assertRaises(PermissionDenied):
            costing.decide_price_override(
                organization=self.org,
                actor=finance,
                request=request,
                expected_fingerprint=request.fingerprint,
                decision="approved",
                reason="Not owner",
                evidence="Offer",
            )
        costing.decide_price_override(
            organization=self.org,
            actor=self.owner,
            request=request,
            expected_fingerprint=request.fingerprint,
            decision="approved",
            reason="Margin reviewed",
            evidence="Approved offer reference 12",
        )
        with self.assertRaises(ValidationError):
            create_invoice(**{**facts, "quantity": 3}, price_override=request)
        invoice = create_invoice(**facts, price_override=request)
        self.assertEqual(invoice.unit_price, Decimal(505000))
        self.assertEqual(
            issue_invoice(organization=self.org, actor=finance, invoice=invoice).status,
            "issued",
        )
        with self.assertRaises(ValidationError):
            create_invoice(**facts, price_override=request)

    def test_cost_caps_bill_gross_completed_units_approved_status_and_closed_dates(
        self,
    ):
        bill = self.bill(amount="450000")
        facts = {
            "organization": self.org,
            "actor": self.owner,
            "invoice": self.invoice,
            "bill": bill,
            "quantity": 1,
            "amount": "450000",
            "date": self.day,
            "reference": "CAPPED-COST",
            "reason": "One tested unit",
            "evidence": "Provider invoice",
        }
        with self.assertRaisesMessage(ValidationError, "unit invoice"):
            costing.allocate_cost(**facts)
        deliver_units(
            organization=self.org,
            actor=self.owner,
            invoice=self.invoice,
            quantity=1,
            date=self.day,
            reference="ONE-CAPPED",
            evidence="One attendance",
        )
        with self.assertRaisesMessage(ValidationError, "bruto"):
            costing.allocate_cost(**{**facts, "amount": "450001"})
        with self.assertRaisesMessage(ValidationError, "unit invoice"):
            costing.allocate_cost(**{**facts, "quantity": 2})
        pending = create_bill(
            organization=self.org,
            actor=self.owner,
            supplier=bill.supplier,
            amount="450000",
            date=self.day,
            service_date=self.day,
            category="provider",
        )
        with self.assertRaisesMessage(ValidationError, "disahkan"):
            costing.allocate_cost(**{**facts, "bill": pending})
        allocation = costing.allocate_cost(**facts)
        period = AccountingPeriod.objects.get(
            organization=self.org, year=self.day.year, month=self.day.month
        )
        period.closed = True
        period.save()
        with self.assertRaises(ValidationError):
            costing.reverse_cost(
                organization=self.org,
                actor=self.owner,
                allocation=allocation,
                date=self.day,
                reference="CLOSED-REVERSE",
                reason="Correction",
                evidence="Changed mapping",
            )
        self.assertFalse(CostReversal.objects.exists())
        with self.assertRaises(ValidationError):
            allocation.delete()
        with self.assertRaises(ValidationError):
            CostAllocation.objects.filter(pk=allocation.pk).update(amount=1)

    def test_historical_cost_and_service_credit_keep_incurred_cost_until_explicit_reversal(
        self,
    ):
        from .fulfillment import credit_units

        bill = self.bill()
        deliver_units(
            organization=self.org,
            actor=self.owner,
            invoice=self.invoice,
            quantity=2,
            date=self.day,
            reference="HISTORICAL-COST",
            evidence="Attendance",
        )
        allocation = costing.allocate_cost(
            organization=self.org,
            actor=self.owner,
            invoice=self.invoice,
            bill=bill,
            quantity=2,
            amount="900000",
            date=self.day,
            reference="HISTORICAL-ALLOCATION",
            reason="Actual provider costs",
            evidence="Bill gross",
        )
        credit_units(
            organization=self.org,
            actor=self.owner,
            invoice=self.invoice,
            quantity=1,
            kind="service",
            date=self.day,
            reference="COST-SERVICE-CREDIT",
            evidence="Service correction to customer",
        )
        self.assertEqual(
            costing.cost_facts(organization=self.org, invoice=self.invoice)[
                "actual_cost"
            ],
            Decimal(900000),
        )
        costing.reverse_cost(
            organization=self.org,
            actor=self.owner,
            allocation=allocation,
            date=self.day + timedelta(days=1),
            reference="LATER-REVERSAL",
            reason="Wrong invoice mapping",
            evidence="Provider clarification",
        )
        self.assertEqual(
            costing.cost_facts(
                organization=self.org, invoice=self.invoice, as_of=self.day
            )["actual_cost"],
            Decimal(900000),
        )
        self.assertIsNone(
            costing.cost_facts(organization=self.org, invoice=self.invoice)[
                "actual_cost"
            ]
        )
        with self.assertRaisesMessage(ValidationError, "berurutan"):
            costing.allocate_cost(
                organization=self.org,
                actor=self.owner,
                invoice=self.invoice,
                bill=bill,
                quantity=2,
                amount="900000",
                date=self.day,
                reference="BACKDATED-COST",
                reason="Must fail",
                evidence="Date predates reversal",
            )

    def test_supplier_prepayment_requires_dated_expense_release_before_actual_cost_allocation(
        self,
    ):
        from .services import release_prepayment

        completed = self.day + timedelta(days=1)
        supplier = Party.objects.create(
            organization=self.org, name="Prepaid provider", kind="supplier"
        )
        bill = approve_bill(
            organization=self.org,
            actor=self.owner,
            bill=create_bill(
                organization=self.org,
                actor=self.owner,
                supplier=supplier,
                amount="900000",
                date=self.day,
                service_date=completed,
                category="provider",
            ),
        )
        deliver_units(
            organization=self.org,
            actor=self.owner,
            invoice=self.invoice,
            quantity=2,
            date=completed,
            reference="PREPAID-DELIVERY",
            evidence="Attendance",
        )
        facts = {
            "organization": self.org,
            "actor": self.owner,
            "invoice": self.invoice,
            "bill": bill,
            "quantity": 1,
            "amount": "450000",
            "date": completed,
            "reference": "PREPAID-COST-1",
            "reason": "Supplier cost",
            "evidence": "Supplier service completed",
        }
        with self.assertRaisesMessage(ValidationError, "dibayar di muka"):
            costing.allocate_cost(**facts)
        release_prepayment(
            organization=self.org,
            actor=self.owner,
            bill=bill,
            date=completed,
            amount="450000",
            reference="RELEASE-HALF",
            evidence="One supplier unit received",
        )
        costing.allocate_cost(**facts)
        with self.assertRaisesMessage(ValidationError, "dibayar di muka"):
            costing.allocate_cost(**{**facts, "reference": "PREPAID-COST-2"})
        self.assertFalse(
            costing.cost_facts(organization=self.org, invoice=self.invoice)["complete"]
        )

    def test_price_decisions_are_stale_safe_and_issue_rechecks_direct_draft_edits(self):
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
            **facts, reason="Discount", evidence="Offer"
        )
        decision = {
            "organization": self.org,
            "actor": self.owner,
            "request": request,
            "decision": "approved",
            "reason": "Reviewed",
            "evidence": "Approved offer",
        }
        with self.assertRaises(ValidationError):
            costing.decide_price_override(**decision, expected_fingerprint="stale")
        costing.decide_price_override(
            **decision, expected_fingerprint=request.fingerprint
        )
        with self.assertRaises(ValidationError):
            costing.decide_price_override(
                **decision, expected_fingerprint=request.fingerprint
            )
        invoice = create_invoice(**facts, price_override=request)
        invoice.quantity = 3
        invoice.save()
        before = Journal.objects.count()
        with self.assertRaises(ValidationError):
            issue_invoice(organization=self.org, actor=self.owner, invoice=invoice)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, "draft")
        self.assertEqual(Journal.objects.count(), before)
        self.assertEqual(PriceOverrideDecision.objects.count(), 1)
        # Old/manual drafts cannot bypass the command gate by writing a low price directly.
        manual = Invoice.objects.create(
            organization=self.org,
            party=self.party,
            product=self.product,
            number="MANUAL-LOW",
            date=self.day,
            due_date=self.day,
            service_date=self.day,
            quantity=1,
            unit_price="1",
        )
        with self.assertRaises(ValidationError):
            issue_invoice(organization=self.org, actor=self.owner, invoice=manual)

    def test_price_approval_rejects_changed_baseline_and_foreign_or_revoked_actor(self):
        facts = {
            "organization": self.org,
            "actor": self.owner,
            "party": self.party,
            "product": self.product,
            "quantity": 1,
            "date": self.day,
            "unit_price": "505000",
            "reason": "Discount",
            "evidence": "Offer",
        }
        request = costing.request_price_override(**facts)
        self.product.default_price = Decimal(530000)
        self.product.save()
        with self.assertRaisesMessage(ValidationError, "Dasar harga berubah"):
            costing.decide_price_override(
                organization=self.org,
                actor=self.owner,
                request=request,
                expected_fingerprint=request.fingerprint,
                decision="approved",
                reason="Review",
                evidence="Offer",
            )
        other = Organization.objects.create(name="Other costing company")
        Membership.objects.create(organization=other, user=self.owner, role="owner")
        with self.assertRaises(ValidationError):
            costing.request_price_override(**{**facts, "organization": other})
        Membership.all_objects.filter(organization=self.org, user=self.owner).update(
            is_active=False
        )
        with self.assertRaises(PermissionDenied):
            costing.request_price_override(**facts)

    def test_public_cost_and_price_forms_complete_workflow_without_role_bypass(self):
        bill = self.bill()
        deliver_units(
            organization=self.org,
            actor=self.owner,
            invoice=self.invoice,
            quantity=1,
            date=self.day,
            reference="WEB-COST-ONE",
            evidence="Attendance",
        )
        self.client.force_login(self.owner)
        url = reverse("invoice_costs", args=[self.invoice.pk])
        response = self.client.get(url)
        self.assertContains(response, "Biaya belum lengkap")
        reference = str(response.context["form"]["reference"].value())
        data = {
            "bill": bill.pk,
            "quantity": 1,
            "amount": "450000",
            "date": self.day,
            "reference": reference,
            "reason": "Allocate actual cost",
            "evidence": "Provider invoice",
        }
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertEqual(CostAllocation.objects.count(), 1)
        self.assertContains(self.client.get(url), "Biaya aktual unit")
        finance = get_user_model().objects.create_user("web-price-finance")
        Membership.objects.create(organization=self.org, user=finance, role="finance")
        self.client.force_login(finance)
        request_data = {
            "party": self.party.pk,
            "product": self.product.pk,
            "quantity": 1,
            "date": self.day,
            "unit_price": "505000",
            "reason": "Negotiated",
            "evidence": "Offer 15",
        }
        response = self.client.post(reverse("price_request_create"), request_data)
        self.assertEqual(response.status_code, 302)
        detail_url = response.url
        response = self.client.get(detail_url)
        item = response.context["item"]
        self.assertFalse(response.context["can_decide"])
        decision = {
            "action": "decision",
            "expected_fingerprint": item.fingerprint,
            "decision": "approved",
            "reason": "Approved",
            "evidence": "Reviewed offer",
        }
        self.assertEqual(self.client.post(detail_url, decision).status_code, 403)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.post(detail_url, decision).status_code, 302)
        self.client.force_login(finance)
        invoice_response = self.client.post(
            reverse("sale_create"),
            {
                "party": self.party.pk,
                "product": self.product.pk,
                "quantity": 1,
                "date": self.day,
                "due_date": self.day,
                "service_date": self.day,
                "price_override": item.pk,
            },
        )
        self.assertEqual(invoice_response.status_code, 302)
        invoice = Invoice.objects.latest("pk")
        self.assertEqual(invoice.unit_price, Decimal(505000))
        self.assertContains(
            self.client.get(reverse("sale_detail", args=[invoice.pk])),
            f'href="{reverse("invoice_costs", args=[invoice.pk])}"',
        )
