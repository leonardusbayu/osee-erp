from datetime import date, datetime
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase

from core.models import Membership, Organization
from finance.fulfillment import credit_units, deliver_units, refund_credit
from finance.models import BankAccount, BankTransaction, Party, Product
from finance.services import (
    cancel_invoice,
    create_invoice,
    issue_invoice,
    reconcile_receipt,
    refund_cancelled_invoice,
)

from .analytics import marketing_snapshot
from .services import (
    create_campaign,
    create_lead,
    create_member,
    link_invoice,
    save_ad_observation,
)
from .workflow import record_contact

JAKARTA = ZoneInfo("Asia/Jakarta")


class MarketingAnalyticsTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Marketing metrics")
        self.actor = get_user_model().objects.create_user(username="metrics-owner")
        Membership.objects.create(organization=self.org, user=self.actor, role="owner")
        self.party = Party.objects.create(
            organization=self.org, kind="customer", name="Customer private"
        )
        self.product = Product.objects.create(
            organization=self.org,
            code="ITP",
            name="ITP",
            kind="itp",
            default_price=Decimal(100),
        )
        self.account = BankAccount.objects.create(
            organization=self.org, name="Bank", account_number="PRIVATE-ACCOUNT"
        )
        self.member = create_member(
            organization=self.org,
            actor=self.actor,
            name="Lead owner private",
            discipline="sales",
        )
        self.buyer = create_member(
            organization=self.org,
            actor=self.actor,
            name="Media buyer private",
            discipline="media_buyer",
        )
        self.campaign = create_campaign(
            organization=self.org,
            actor=self.actor,
            name="Campaign private",
            channel="meta_ads",
            product=self.product,
            owner=self.buyer,
        )

    def invoice(self, number, day=date(2026, 1, 5)):
        invoice = create_invoice(
            organization=self.org,
            actor=self.actor,
            party=self.party,
            product=self.product,
            quantity=1,
            date=day,
            service_date=day,
            number=number,
        )
        return issue_invoice(organization=self.org, actor=self.actor, invoice=invoice)

    def lead(
        self,
        reference,
        invoice=None,
        day=datetime(2026, 1, 5, 9, tzinfo=JAKARTA),
        **fields,
    ):
        lead = create_lead(
            organization=self.org,
            actor=self.actor,
            reference=reference,
            name="Lead private " + reference,
            received_at=day,
            campaign=self.campaign,
            owner=self.member,
            product=self.product,
            party=self.party,
            **fields,
        )
        if invoice:
            lead = link_invoice(
                organization=self.org,
                actor=self.actor,
                lead=lead,
                invoice=invoice,
                expected_version=lead.version,
            )
        return lead

    def receipt(self, invoice, amount, reference, day=date(2026, 1, 10)):
        bank = BankTransaction.objects.create(
            organization=self.org,
            account=self.account,
            date=day,
            reference=reference,
            amount=Decimal(amount),
        )
        return reconcile_receipt(
            organization=self.org,
            actor=self.actor,
            invoice=invoice,
            transaction=bank,
            amount=amount,
        )

    def ad(self, reference="ADS1", day=date(2026, 1, 10), spend="10", **fields):
        return save_ad_observation(
            organization=self.org,
            actor=self.actor,
            campaign=self.campaign,
            date=day,
            spend=spend,
            impressions=1000,
            clicks=50,
            source="Private source",
            reference=reference,
            **fields,
        )

    def snapshot(self, **kwargs):
        return marketing_snapshot(
            organization=self.org, start="2026-01-01", end="2026-01-31", **kwargs
        )

    def test_first_source_keeps_campaign_revenue_after_whatsapp_followup(self):
        invoice = self.invoice("FIRST-SOURCE")
        lead = self.lead("FIRST-SOURCE", invoice)
        self.receipt(invoice, "100", "FIRST-SOURCE-RECEIPT")
        self.contact(
            lead,
            occurred_at=datetime(2026, 1, 6, 10, tzinfo=JAKARTA),
            followup_action="keep",
        )

        lead.refresh_from_db()
        campaign_result = self.snapshot(group_by="campaign")
        channel_result = self.snapshot(group_by="channel")
        self.assertEqual(lead.channel, "meta_ads")
        self.assertEqual(lead.campaign_id, self.campaign.pk)
        self.assertEqual(lead.contact_events.count(), 1)
        self.assertIn(
            "Kredit penjualan mengikuti sumber pertama",
            campaign_result["attribution_model"],
        )
        self.assertIn(
            "follow-up tidak memindahkan atribusi", campaign_result["attribution_model"]
        )
        self.assertEqual(campaign_result["rows"][0]["label"], "Campaign private")
        self.assertEqual(campaign_result["rows"][0]["cash_in"], "100.00")
        self.assertEqual(channel_result["rows"][0]["label"], "Meta Ads")
        self.assertEqual(channel_result["rows"][0]["cash_in"], "100.00")

    def contact(
        self,
        lead,
        *,
        occurred_at,
        recorded_at=None,
        followup_action="keep",
        next_due_at=None,
        completes=None,
        corrects=None,
    ):
        current = type(lead).objects.get(pk=lead.pk)
        values = {
            "organization": self.org,
            "actor": self.actor,
            "lead": current,
            "expected_version": current.version,
            "occurred_at": occurred_at,
            "method": "whatsapp",
            "outcome": "responded",
            "summary": "Catatan kontak",
            "evidence": "Bukti internal",
            "followup_action": followup_action,
            "next_due_at": next_due_at,
            "completes": completes,
            "corrects": corrects,
            "correction_reason": "Koreksi bukti sumber" if corrects else "",
            "responsibility_reason": "Owner memverifikasi catatan",
        }
        if recorded_at:
            with patch(
                "django.db.models.fields.timezone.now", return_value=recorded_at
            ):
                return record_contact(**values)
        return record_contact(**values)

    def test_partial_receipts_cash_period_and_lead_cohort_are_independent(self):
        old_invoice = self.invoice("OLD", date(2026, 1, 1))
        self.lead(
            "OLD",
            old_invoice,
            day=datetime(2025, 12, 31, 9, tzinfo=ZoneInfo("Asia/Jakarta")),
        )
        self.receipt(old_invoice, "30", "OLD1")
        self.receipt(old_invoice, "20", "OLD2", date(2026, 2, 5))
        invoice = self.invoice("NEW")
        self.lead(
            "NEW",
            invoice,
            first_response_at=datetime(
                2026, 1, 5, 9, 30, tzinfo=ZoneInfo("Asia/Jakarta")
            ),
        )
        first = self.receipt(invoice, "20", "NEW1")
        reconcile_receipt(
            organization=self.org,
            actor=self.actor,
            invoice=invoice,
            transaction=first.transaction,
            amount="20",
        )
        self.receipt(self.invoice("UNLINKED"), "40", "UNLINKED1")
        BankTransaction.objects.create(
            organization=self.org,
            account=self.account,
            date=date(2026, 1, 10),
            reference="UNALLOCATED",
            amount=Decimal(900),
        )
        BankTransaction.objects.create(
            organization=self.org,
            account=self.account,
            date=date(2026, 1, 10),
            reference="NEGATIVE",
            amount=Decimal(-5),
        )
        self.ad()
        self.ad("ADS2", date(2026, 1, 11))
        result = self.snapshot()
        self.assertEqual(result["summary"]["cash_in"], "50.00")
        self.assertEqual(result["summary"]["actual_refunds"], "0.00")
        self.assertEqual(result["summary"]["net_cash_in"], "50.00")
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

    def test_actual_invoice_refunds_reduce_net_cash_on_bank_date_and_reconcile(self):
        linked_invoice = self.invoice("REFUNDED-LINKED")
        self.lead("REFUNDED-LINKED", linked_invoice)
        self.receipt(linked_invoice, "100", "REFUNDED-LINKED-RECEIPT")
        cancel_invoice(
            organization=self.org,
            actor=self.actor,
            invoice=linked_invoice,
            date=date(2026, 1, 15),
            reason="Synthetic cancellation; refund still pending",
        )

        unlinked_invoice = self.invoice("REFUNDED-UNLINKED")
        self.receipt(unlinked_invoice, "50", "REFUNDED-UNLINKED-RECEIPT")
        cancel_invoice(
            organization=self.org,
            actor=self.actor,
            invoice=unlinked_invoice,
            date=date(2026, 1, 16),
            reason="Synthetic unlinked cancellation",
        )

        pending = self.snapshot()
        self.assertEqual(pending["summary"]["cash_in"], "100.00")
        self.assertEqual(pending["summary"]["actual_refunds"], "0.00")
        self.assertEqual(pending["summary"]["net_cash_in"], "100.00")

        linked_refund_bank = BankTransaction.objects.create(
            organization=self.org,
            account=self.account,
            date=date(2026, 1, 20),
            reference="REFUNDED-LINKED-BANK",
            amount=Decimal(-40),
        )
        refund_cancelled_invoice(
            organization=self.org,
            actor=self.actor,
            invoice=linked_invoice,
            transaction=linked_refund_bank,
            amount="40",
            reference="REFUNDED-LINKED-REFUND",
            evidence="Synthetic bank refund evidence",
        )
        unlinked_refund_bank = BankTransaction.objects.create(
            organization=self.org,
            account=self.account,
            date=date(2026, 1, 21),
            reference="REFUNDED-UNLINKED-BANK",
            amount=Decimal(-10),
        )
        refund_cancelled_invoice(
            organization=self.org,
            actor=self.actor,
            invoice=unlinked_invoice,
            transaction=unlinked_refund_bank,
            amount="10",
            reference="REFUNDED-UNLINKED-REFUND",
            evidence="Synthetic unlinked bank refund evidence",
        )
        self.ad("REFUND-KPI-1", date(2026, 1, 10))
        self.ad("REFUND-KPI-2", date(2026, 1, 11))

        result = self.snapshot()
        self.assertEqual(result["summary"]["cash_in"], "100.00")
        self.assertEqual(result["summary"]["actual_refunds"], "40.00")
        self.assertEqual(result["summary"]["net_cash_in"], "60.00")
        self.assertEqual(result["summary"]["cash_to_spend"], "5.00")
        self.assertEqual(result["coverage"]["total_customer_receipts"], "150.00")
        self.assertEqual(result["coverage"]["attributed_receipts"], "100.00")
        self.assertEqual(result["coverage"]["total_invoice_refunds"], "50.00")
        self.assertEqual(result["coverage"]["attributed_invoice_refunds"], "40.00")
        self.assertEqual(result["coverage"]["unattributed_invoice_refunds"], "10.00")
        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(result["rows"][0]["cash_in"], "100.00")
        self.assertEqual(result["rows"][0]["actual_refunds"], "40.00")
        self.assertEqual(result["rows"][0]["net_cash_in"], "60.00")

    def test_refunds_can_make_later_period_net_cash_negative_without_gross_receipts(
        self,
    ):
        invoice = self.invoice("LATER-REFUND")
        self.lead("LATER-REFUND", invoice)
        self.receipt(invoice, "100", "LATER-REFUND-RECEIPT")
        cancel_invoice(
            organization=self.org,
            actor=self.actor,
            invoice=invoice,
            date=date(2026, 1, 15),
            reason="Synthetic cancellation before later refund",
        )
        refund_bank = BankTransaction.objects.create(
            organization=self.org,
            account=self.account,
            date=date(2026, 2, 5),
            reference="LATER-REFUND-BANK",
            amount=Decimal(-100),
        )
        refund_cancelled_invoice(
            organization=self.org,
            actor=self.actor,
            invoice=invoice,
            transaction=refund_bank,
            amount="100",
            reference="LATER-REFUND-POSTED",
            evidence="Synthetic later bank refund evidence",
        )

        result = marketing_snapshot(
            organization=self.org,
            start="2026-02-01",
            end="2026-02-28",
        )
        self.assertEqual(result["summary"]["cash_in"], "0.00")
        self.assertEqual(result["summary"]["actual_refunds"], "100.00")
        self.assertEqual(result["summary"]["net_cash_in"], "-100.00")
        self.assertEqual(result["rows"][0]["actual_refunds"], "100.00")
        self.assertEqual(result["rows"][0]["net_cash_in"], "-100.00")

    def test_credit_refunds_are_included_only_after_the_bank_payment(self):
        invoice = self.invoice("CREDIT-REFUND")
        self.lead("CREDIT-REFUND", invoice)
        self.receipt(invoice, "100", "CREDIT-REFUND-RECEIPT")
        deliver_units(
            organization=self.org,
            actor=self.actor,
            invoice=invoice,
            quantity=1,
            date=date(2026, 1, 11),
            reference="CREDIT-REFUND-DELIVERY",
            evidence="Synthetic delivery evidence",
        )
        credit = credit_units(
            organization=self.org,
            actor=self.actor,
            invoice=invoice,
            quantity=1,
            kind="service",
            date=date(2026, 1, 15),
            reference="CREDIT-REFUND-CREDIT",
            evidence="Synthetic approved service credit",
        )
        pending = self.snapshot()
        self.assertEqual(pending["summary"]["actual_refunds"], "0.00")
        self.assertEqual(pending["summary"]["net_cash_in"], "100.00")

        refund_bank = BankTransaction.objects.create(
            organization=self.org,
            account=self.account,
            date=date(2026, 1, 20),
            reference="CREDIT-REFUND-BANK",
            amount=Decimal(-30),
        )
        refund_credit(
            organization=self.org,
            actor=self.actor,
            credit=credit,
            transaction=refund_bank,
            amount="30",
            reference="CREDIT-REFUND-POSTED",
            evidence="Synthetic credit refund evidence",
        )
        paid = self.snapshot()
        self.assertEqual(paid["summary"]["cash_in"], "100.00")
        self.assertEqual(paid["summary"]["actual_refunds"], "30.00")
        self.assertEqual(paid["summary"]["net_cash_in"], "70.00")

    def test_customer_advance_refund_is_not_reported_as_invoice_refund(self):
        from finance.services import record_customer_advance, refund_customer_advance

        incoming = BankTransaction.objects.create(
            organization=self.org,
            account=self.account,
            date=date(2026, 1, 6),
            reference="ADVANCE-IN",
            amount=Decimal(25),
        )
        advance = record_customer_advance(
            organization=self.org,
            actor=self.actor,
            party=self.party,
            transaction=incoming,
            amount="25",
            reference="ADVANCE-RECEIPT",
            evidence="Synthetic advance receipt",
        )
        outgoing = BankTransaction.objects.create(
            organization=self.org,
            account=self.account,
            date=date(2026, 1, 12),
            reference="ADVANCE-OUT",
            amount=Decimal(-25),
        )
        refund_customer_advance(
            organization=self.org,
            actor=self.actor,
            advance=advance,
            transaction=outgoing,
            amount="25",
            reference="ADVANCE-REFUND",
            evidence="Synthetic advance refund",
        )

        result = self.snapshot()
        self.assertEqual(result["coverage"]["total_invoice_refunds"], "0.00")
        self.assertEqual(result["summary"]["actual_refunds"], "0.00")
        self.assertEqual(result["summary"]["net_cash_in"], "0.00")

    def test_missing_spend_is_known_subtotal_and_unsupported_dimensions_stay_missing(
        self,
    ):
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
        result = marketing_snapshot(
            organization=other, start="2026-01-01", end="2026-01-31"
        )
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

    def test_followup_kpi_uses_due_date_denominator_and_completion_history(self):
        timely = self.lead("FOLLOWUP-TIMELY")
        late = self.lead("FOLLOWUP-LATE")
        open_promise = self.lead("FOLLOWUP-OPEN")
        next_month = self.lead("FOLLOWUP-NEXT-MONTH")
        due_timely = datetime(2026, 1, 31, 23, 59, 59, tzinfo=ZoneInfo("Asia/Jakarta"))
        promise_timely = self.contact(
            timely,
            occurred_at=datetime(2026, 1, 30, 10, tzinfo=ZoneInfo("Asia/Jakarta")),
            recorded_at=datetime(2026, 1, 30, 10, tzinfo=ZoneInfo("Asia/Jakarta")),
            followup_action="set",
            next_due_at=due_timely,
        )
        self.contact(
            timely,
            occurred_at=due_timely,
            recorded_at=due_timely,
            completes=promise_timely,
        )

        due_late = datetime(2026, 1, 12, 10, tzinfo=ZoneInfo("Asia/Jakarta"))
        promise_late = self.contact(
            late,
            occurred_at=datetime(2026, 1, 10, 10, tzinfo=ZoneInfo("Asia/Jakarta")),
            recorded_at=datetime(2026, 1, 10, 10, tzinfo=ZoneInfo("Asia/Jakarta")),
            followup_action="set",
            next_due_at=due_late,
        )
        self.contact(
            late,
            occurred_at=datetime(2026, 1, 13, 10, tzinfo=ZoneInfo("Asia/Jakarta")),
            recorded_at=datetime(2026, 1, 13, 10, tzinfo=ZoneInfo("Asia/Jakarta")),
            completes=promise_late,
        )

        self.contact(
            open_promise,
            occurred_at=datetime(2026, 1, 14, 10, tzinfo=ZoneInfo("Asia/Jakarta")),
            recorded_at=datetime(2026, 1, 14, 10, tzinfo=ZoneInfo("Asia/Jakarta")),
            followup_action="set",
            next_due_at=datetime(2026, 1, 16, 10, tzinfo=ZoneInfo("Asia/Jakarta")),
        )
        self.contact(
            next_month,
            occurred_at=datetime(2026, 1, 30, 10, tzinfo=ZoneInfo("Asia/Jakarta")),
            recorded_at=datetime(2026, 1, 30, 10, tzinfo=ZoneInfo("Asia/Jakarta")),
            followup_action="set",
            next_due_at=datetime(2026, 2, 1, 10, tzinfo=ZoneInfo("Asia/Jakarta")),
        )

        summary = self.snapshot()["summary"]
        self.assertEqual(summary["followups_due"], 3)
        self.assertEqual(summary["followups_on_time"], 1)
        self.assertEqual(summary["followup_on_time_percent"], "33.33")

    def test_post_deadline_correction_does_not_rewrite_on_time_history(self):
        lead = self.lead("FOLLOWUP-CORRECTED")
        due = datetime(2026, 1, 15, 10, tzinfo=ZoneInfo("Asia/Jakarta"))
        promise = self.contact(
            lead,
            occurred_at=datetime(2026, 1, 10, 10, tzinfo=ZoneInfo("Asia/Jakarta")),
            recorded_at=datetime(2026, 1, 10, 10, tzinfo=ZoneInfo("Asia/Jakarta")),
            followup_action="set",
            next_due_at=due,
        )
        completion = self.contact(
            lead, occurred_at=due, recorded_at=due, completes=promise
        )
        self.contact(
            lead,
            occurred_at=datetime(2026, 1, 16, 10, tzinfo=ZoneInfo("Asia/Jakarta")),
            recorded_at=datetime(2026, 1, 16, 10, tzinfo=ZoneInfo("Asia/Jakarta")),
            corrects=completion,
        )

        summary = self.snapshot()["summary"]
        self.assertEqual(summary["followups_due"], 1)
        self.assertEqual(summary["followups_on_time"], 0)
        self.assertEqual(summary["followup_on_time_percent"], "0.00")

    def test_fully_paid_sales_excludes_partial_payment(self):
        partial_invoice = self.invoice("KPI-PARTIAL-SALE")
        full_invoice = self.invoice("KPI-FULL-SALE")
        self.lead("PARTIAL-SALE", partial_invoice)
        self.lead("FULL-SALE", full_invoice)
        self.receipt(partial_invoice, "50", "KPI-PARTIAL-PAYMENT")
        self.receipt(full_invoice, "100", "KPI-FULL-PAYMENT")

        summary = self.snapshot()["summary"]
        self.assertEqual(summary["cohort_paid_leads"], 2)
        self.assertEqual(summary["cohort_fully_paid_leads"], 1)
        self.assertEqual(summary["cohort_fully_paid_conversion"], "50.00")

    def test_future_cash_is_excluded_and_won_partial_payment_stays_in_followup(self):
        invoice = self.invoice("PARTIAL")
        self.lead("PARTIAL", invoice, stage="won", next_follow_up=date(2026, 1, 9))
        self.receipt(invoice, "20", "REAL", date(2026, 1, 8))
        self.receipt(invoice, "30", "FUTURE", date(2026, 1, 11))
        fully_paid = self.invoice("FULL")
        self.lead("FULL", fully_paid, stage="won", next_follow_up=date(2026, 1, 9))
        self.receipt(fully_paid, "100", "FULL", date(2026, 1, 8))
        self.ad("FUTURE-AD", date(2026, 1, 11))
        with patch(
            "marketing.analytics.timezone.localdate", return_value=date(2026, 1, 10)
        ):
            result = self.snapshot()
            future = marketing_snapshot(
                organization=self.org, start="2026-01-11", end="2026-01-31"
            )
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
        broad = create_campaign(
            organization=self.org,
            actor=self.actor,
            name="Broad campaign",
            channel="meta_ads",
            owner=self.buyer,
        )
        invoice = self.invoice("BROAD")
        lead = create_lead(
            organization=self.org,
            actor=self.actor,
            reference="BROAD",
            name="Known product lead",
            received_at=datetime(2026, 1, 5, 9, tzinfo=ZoneInfo("Asia/Jakarta")),
            campaign=broad,
            owner=self.member,
            product=self.product,
            party=self.party,
        )
        link_invoice(
            organization=self.org,
            actor=self.actor,
            lead=lead,
            invoice=invoice,
            expected_version=lead.version,
        )
        self.receipt(invoice, "100", "BROAD")
        self.ad(spend="10")
        save_ad_observation(
            organization=self.org,
            actor=self.actor,
            campaign=broad,
            date=date(2026, 1, 10),
            spend="50",
            source="Report",
            reference="BROAD-SPEND",
        )
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
            filtered = self.snapshot(
                group_by=dimension, filters={"product": self.product.pk}
            )
            self.assertEqual(filtered["summary"]["cash_in"], "100.00")
            self.assertEqual(filtered["summary"]["spend"], "10.00")
            self.assertFalse(filtered["summary"]["spend_complete"])
            self.assertIsNone(filtered["summary"]["cash_to_spend"])

    def test_missing_cash_campaign_cost_evidence_is_not_zero_spend(self):
        missing = create_campaign(
            organization=self.org,
            actor=self.actor,
            name="Missing cost campaign",
            channel="meta_ads",
            product=self.product,
        )
        invoice = self.invoice("MISSING-COST")
        lead = create_lead(
            organization=self.org,
            actor=self.actor,
            reference="MISSING-COST",
            name="Paid lead",
            received_at=datetime(2026, 1, 5, 9, tzinfo=ZoneInfo("Asia/Jakarta")),
            campaign=missing,
            product=self.product,
            party=self.party,
        )
        link_invoice(
            organization=self.org,
            actor=self.actor,
            lead=lead,
            invoice=invoice,
            expected_version=lead.version,
        )
        self.receipt(invoice, "100", "MISSING-COST")
        self.ad(spend="10")
        result = self.snapshot(group_by="campaign")
        self.assertEqual(result["summary"]["spend"], "10.00")
        self.assertFalse(result["summary"]["spend_complete"])
        self.assertIsNone(result["summary"]["cash_to_spend"])
        self.assertIn("tanpa bukti belanja", result["summary"]["spend_note"])
        missing_row = next(
            row for row in result["rows"] if row["key"] == str(missing.pk)
        )
        self.assertFalse(missing_row["cash_cost_coverage_complete"])
        self.assertIsNone(missing_row["cash_to_spend"])
        save_ad_observation(
            organization=self.org,
            actor=self.actor,
            campaign=missing,
            date=date(2026, 1, 10),
            spend="0",
            source="Verified zero spend",
            reference="ZERO-SPEND",
        )
        result = self.snapshot()
        self.assertTrue(result["summary"]["spend_complete"])
        self.assertEqual(result["summary"]["cash_to_spend"], "10.00")

    def test_cash_without_campaign_cannot_borrow_other_campaign_spend(self):
        invoice = self.invoice("NO-CAMPAIGN")
        lead = create_lead(
            organization=self.org,
            actor=self.actor,
            reference="NO-CAMPAIGN",
            name="Unmapped source lead",
            received_at=datetime(2026, 1, 5, 9, tzinfo=ZoneInfo("Asia/Jakarta")),
            channel="meta_ads",
            product=self.product,
            party=self.party,
        )
        link_invoice(
            organization=self.org,
            actor=self.actor,
            lead=lead,
            invoice=invoice,
            expected_version=lead.version,
        )
        self.receipt(invoice, "100", "NO-CAMPAIGN")
        self.ad()
        result = self.snapshot()
        self.assertEqual(result["summary"]["cash_in"], "100.00")
        self.assertEqual(result["summary"]["spend"], "10.00")
        self.assertFalse(result["summary"]["spend_complete"])
        self.assertIsNone(result["summary"]["cash_to_spend"])
