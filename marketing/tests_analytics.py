from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase

from core.models import Membership, Organization
from finance.models import BankAccount, BankTransaction, Party, Product
from finance.services import create_invoice, issue_invoice, reconcile_receipt
from .analytics import marketing_snapshot
from .services import create_campaign, create_lead, create_member, link_invoice, save_ad_observation


class MarketingAnalyticsTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Marketing metrics")
        self.actor = get_user_model().objects.create_user(username="metrics-owner")
        Membership.objects.create(organization=self.org, user=self.actor, role="owner")
        self.party = Party.objects.create(organization=self.org, kind="customer", name="Customer private")
        self.product = Product.objects.create(organization=self.org, code="ITP", name="ITP", kind="itp", default_price=Decimal("100"))
        self.account = BankAccount.objects.create(organization=self.org, name="Bank", account_number="PRIVATE-ACCOUNT")
        self.member = create_member(organization=self.org, actor=self.actor, name="Lead owner private", discipline="sales")
        self.buyer = create_member(organization=self.org, actor=self.actor, name="Media buyer private", discipline="media_buyer")
        self.campaign = create_campaign(organization=self.org, actor=self.actor, name="Campaign private", channel="meta_ads", product=self.product, owner=self.buyer)

    def invoice(self, number, day=date(2026, 1, 5)):
        invoice = create_invoice(organization=self.org, actor=self.actor, party=self.party, product=self.product,
                                 quantity=1, date=day, service_date=day, number=number)
        return issue_invoice(organization=self.org, actor=self.actor, invoice=invoice)

    def lead(self, reference, invoice=None, day=datetime(2026, 1, 5, 9, tzinfo=ZoneInfo("Asia/Jakarta")), **fields):
        lead = create_lead(organization=self.org, actor=self.actor, reference=reference, name="Lead private " + reference,
                           received_at=day, campaign=self.campaign, owner=self.member, product=self.product, party=self.party, **fields)
        if invoice:
            lead = link_invoice(organization=self.org, actor=self.actor, lead=lead, invoice=invoice, expected_version=lead.version)
        return lead

    def receipt(self, invoice, amount, reference, day=date(2026, 1, 10)):
        bank = BankTransaction.objects.create(organization=self.org, account=self.account, date=day, reference=reference, amount=Decimal(amount))
        return reconcile_receipt(organization=self.org, actor=self.actor, invoice=invoice, transaction=bank, amount=amount)

    def ad(self, reference="ADS1", day=date(2026, 1, 10), spend="10", **fields):
        return save_ad_observation(organization=self.org, actor=self.actor, campaign=self.campaign, date=day,
                                  spend=spend, impressions=1000, clicks=50, source="Private source", reference=reference, **fields)

    def snapshot(self, **kwargs):
        return marketing_snapshot(organization=self.org, start="2026-01-01", end="2026-01-31", **kwargs)

    def test_partial_receipts_cash_period_and_lead_cohort_are_independent(self):
        old_invoice = self.invoice("OLD", date(2026, 1, 1))
        self.lead("OLD", old_invoice, day=datetime(2025, 12, 31, 9, tzinfo=ZoneInfo("Asia/Jakarta")))
        self.receipt(old_invoice, "30", "OLD1")
        self.receipt(old_invoice, "20", "OLD2", date(2026, 2, 5))
        invoice = self.invoice("NEW")
        self.lead("NEW", invoice, first_response_at=datetime(2026, 1, 5, 9, 30, tzinfo=ZoneInfo("Asia/Jakarta")))
        first = self.receipt(invoice, "20", "NEW1")
        reconcile_receipt(organization=self.org, actor=self.actor, invoice=invoice, transaction=first.transaction, amount="20")
        self.receipt(self.invoice("UNLINKED"), "40", "UNLINKED1")
        BankTransaction.objects.create(organization=self.org, account=self.account, date=date(2026, 1, 10), reference="UNALLOCATED", amount=Decimal("900"))
        BankTransaction.objects.create(organization=self.org, account=self.account, date=date(2026, 1, 10), reference="NEGATIVE", amount=Decimal("-5"))
        self.ad()
        self.ad("ADS2", date(2026, 1, 11))
        result = self.snapshot()
        self.assertEqual(result["summary"]["cash_in"], "50.00")
        self.assertIsNone(result["summary"]["net_cash_in"])
        self.assertEqual(result["coverage"]["total_customer_receipts"], "90.00")
        self.assertEqual(result["coverage"]["unattributed_receipts"], "40.00")
        self.assertEqual(result["summary"]["spend"], "20.00")
        self.assertEqual(result["summary"]["leads"], 1)
        self.assertEqual(result["summary"]["cohort_paid_leads"], 1)
        self.assertEqual(result["summary"]["cohort_cash_in"], "20.00")
        self.assertEqual(result["summary"]["median_response_minutes"], "30.00")
        self.assertEqual(result["summary"]["cash_to_spend"], "2.50")
        self.assertEqual(result["summary"]["ctr"], "5.00")
        self.assertEqual(result["summary"]["cpc"], "0.20")
        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(result["rows"][0]["label"], "Meta Ads")

    def test_missing_spend_is_known_subtotal_and_unsupported_dimensions_stay_missing(self):
        invoice = self.invoice("I")
        self.lead("L", invoice)
        self.receipt(invoice, "25", "R")
        self.ad()
        self.ad("MISSING", date(2026, 1, 11), spend=None)
        result = self.snapshot()
        self.assertEqual(result["summary"]["spend"], "10.00")
        self.assertEqual(result["summary"]["spend_known_rows"], 1)
        self.assertFalse(result["summary"]["spend_complete"])
        self.assertIsNone(result["summary"]["cash_to_spend"])
        self.assertIsNone(result["summary"]["cpc"])
        self.assertEqual(result["summary"]["ctr"], "5.00")
        for dimension in ("member", "segment", "region"):
            result = self.snapshot(group_by=dimension)
            self.assertEqual(result["summary"]["cash_in"], "25.00")
            self.assertIsNone(result["summary"]["spend"])
            self.assertIsNone(result["summary"]["cash_to_spend"])
        result = self.snapshot(group_by="ad")
        self.assertIsNone(result["summary"]["cash_in"])
        self.assertIsNone(result["summary"]["leads"])
        self.assertEqual(result["summary"]["spend"], "10.00")
        result = self.snapshot(filters={"member": self.member.pk})
        self.assertIsNone(result["summary"]["spend"])
        self.assertEqual(result["summary"]["leads"], 1)
        result = self.snapshot(filters={"member": self.buyer.pk})
        self.assertEqual(result["summary"]["leads"], 0)

    def test_filters_and_tenant_scope_do_not_leak_or_reuse_foreign_cash(self):
        invoice = self.invoice("I")
        self.lead("L", invoice)
        self.receipt(invoice, "25", "R")
        self.ad()
        other = Organization.objects.create(name="Other tenant")
        result = marketing_snapshot(organization=other, start="2026-01-01", end="2026-01-31")
        self.assertEqual(result["summary"]["cash_in"], "0.00")
        self.assertEqual(result["summary"]["leads"], 0)
        self.assertIsNone(result["summary"]["spend"])
        self.assertEqual(result["rows"], [])
        result = self.snapshot(filters={"campaign": 999999})
        self.assertEqual(result["summary"]["cash_in"], "0.00")
        self.assertEqual(result["summary"]["leads"], 0)
        self.assertIsNone(result["summary"]["spend"])
        self.assertEqual(result["coverage"]["attributed_receipts"], "25.00")
        with self.assertRaises(ValidationError):
            self.snapshot(filters={"organization_id": other.pk})

    def test_empty_data_and_zero_denominators_do_not_create_ratios(self):
        result = self.snapshot()
        self.assertIsNone(result["summary"]["cohort_paid_conversion"])
        self.assertIsNone(result["summary"]["median_response_minutes"])
        self.assertIsNone(result["summary"]["cash_to_spend"])
        self.assertIsNone(result["coverage"]["attribution_percent"])
        self.assertIsNone(result["summary"]["overdue_followups"])
        self.assertEqual(result["period"]["previous_start"], "2025-12-01")
        self.assertEqual(result["period"]["previous_end"], "2025-12-31")

    def test_future_cash_is_excluded_and_won_partial_payment_stays_in_followup(self):
        invoice = self.invoice("PARTIAL")
        self.lead("PARTIAL", invoice, stage="won", next_follow_up=date(2026, 1, 9))
        self.receipt(invoice, "20", "REAL", date(2026, 1, 8))
        self.receipt(invoice, "30", "FUTURE", date(2026, 1, 11))
        fully_paid = self.invoice("FULL")
        self.lead("FULL", fully_paid, stage="won", next_follow_up=date(2026, 1, 9))
        self.receipt(fully_paid, "100", "FULL", date(2026, 1, 8))
        self.ad("FUTURE-AD", date(2026, 1, 11))
        with patch("marketing.analytics.timezone.localdate", return_value=date(2026, 1, 10)):
            result = self.snapshot()
            future = marketing_snapshot(organization=self.org, start="2026-01-11", end="2026-01-31")
        self.assertEqual(result["observation_end"], "2026-01-10")
        self.assertEqual(result["summary"]["cash_in"], "120.00")
        self.assertEqual(result["summary"]["cohort_cash_in"], "120.00")
        self.assertEqual(result["summary"]["overdue_followups"], 1)
        self.assertEqual(result["summary"]["unpaid_won_leads"], 1)
        self.assertEqual(result["summary"]["unpaid_won_amount"], "80.00")
        self.assertIsNone(result["summary"]["spend"])
        self.assertIsNone(future["summary"]["cash_in"])
        self.assertIsNone(future["summary"]["leads"])
        self.assertEqual(result["period"]["previous_start"], "2025-12-22")

    def test_product_breakdown_does_not_omit_costs_of_broad_campaign(self):
        broad = create_campaign(organization=self.org, actor=self.actor, name="Broad campaign", channel="meta_ads", owner=self.buyer)
        invoice = self.invoice("BROAD")
        lead = create_lead(organization=self.org, actor=self.actor, reference="BROAD", name="Known product lead",
                           received_at=datetime(2026, 1, 5, 9, tzinfo=ZoneInfo("Asia/Jakarta")),
                           campaign=broad, owner=self.member, product=self.product, party=self.party)
        link_invoice(organization=self.org, actor=self.actor, lead=lead, invoice=invoice, expected_version=lead.version)
        self.receipt(invoice, "100", "BROAD")
        self.ad(spend="10")
        save_ad_observation(organization=self.org, actor=self.actor, campaign=broad, date=date(2026, 1, 10),
                            spend="50", source="Report", reference="BROAD-SPEND")
        total = self.snapshot()
        self.assertEqual(total["summary"]["spend"], "60.00")
        self.assertEqual(total["summary"]["cash_to_spend"], "1.67")
        self.assertTrue(total["summary"]["spend_complete"])
        grouped = self.snapshot(group_by="product")
        self.assertEqual(grouped["summary"]["cash_to_spend"], "1.67")
        rows = {row["key"]: row for row in grouped["rows"]}
        known = rows[str(self.product.pk)]
        self.assertEqual(known["cash_in"], "100.00")
        self.assertEqual(known["spend"], "10.00")
        self.assertFalse(known["spend_complete"])
        self.assertIsNone(known["cash_to_spend"])
        self.assertIn("produk yang belum diketahui", known["spend_note"])
        self.assertEqual(rows["unknown"]["spend"], "50.00")
        self.assertIsNone(rows["unknown"]["cash_to_spend"])
        for dimension in ("channel", "campaign", "product"):
            filtered = self.snapshot(group_by=dimension, filters={"product": self.product.pk})
            self.assertEqual(filtered["summary"]["cash_in"], "100.00")
            self.assertEqual(filtered["summary"]["spend"], "10.00")
            self.assertFalse(filtered["summary"]["spend_complete"])
            self.assertIsNone(filtered["summary"]["cash_to_spend"])

    def test_missing_cash_campaign_cost_evidence_is_not_zero_spend(self):
        missing = create_campaign(organization=self.org, actor=self.actor, name="Missing cost campaign", channel="meta_ads", product=self.product)
        invoice = self.invoice("MISSING-COST")
        lead = create_lead(organization=self.org, actor=self.actor, reference="MISSING-COST", name="Paid lead",
                           received_at=datetime(2026, 1, 5, 9, tzinfo=ZoneInfo("Asia/Jakarta")),
                           campaign=missing, product=self.product, party=self.party)
        link_invoice(organization=self.org, actor=self.actor, lead=lead, invoice=invoice, expected_version=lead.version)
        self.receipt(invoice, "100", "MISSING-COST")
        self.ad(spend="10")
        result = self.snapshot(group_by="campaign")
        self.assertEqual(result["summary"]["spend"], "10.00")
        self.assertFalse(result["summary"]["spend_complete"])
        self.assertIsNone(result["summary"]["cash_to_spend"])
        self.assertIn("tanpa bukti belanja", result["summary"]["spend_note"])
        missing_row = next(row for row in result["rows"] if row["key"] == str(missing.pk))
        self.assertFalse(missing_row["cash_cost_coverage_complete"])
        self.assertIsNone(missing_row["cash_to_spend"])
        save_ad_observation(organization=self.org, actor=self.actor, campaign=missing, date=date(2026, 1, 10),
                            spend="0", source="Verified zero spend", reference="ZERO-SPEND")
        result = self.snapshot()
        self.assertTrue(result["summary"]["spend_complete"])
        self.assertEqual(result["summary"]["cash_to_spend"], "10.00")

    def test_cash_without_campaign_cannot_borrow_other_campaign_spend(self):
        invoice = self.invoice("NO-CAMPAIGN")
        lead = create_lead(organization=self.org, actor=self.actor, reference="NO-CAMPAIGN", name="Unmapped source lead",
                           received_at=datetime(2026, 1, 5, 9, tzinfo=ZoneInfo("Asia/Jakarta")),
                           channel="meta_ads", product=self.product, party=self.party)
        link_invoice(organization=self.org, actor=self.actor, lead=lead, invoice=invoice, expected_version=lead.version)
        self.receipt(invoice, "100", "NO-CAMPAIGN")
        self.ad()
        result = self.snapshot()
        self.assertEqual(result["summary"]["cash_in"], "100.00")
        self.assertEqual(result["summary"]["spend"], "10.00")
        self.assertFalse(result["summary"]["spend_complete"])
        self.assertIsNone(result["summary"]["cash_to_spend"])
