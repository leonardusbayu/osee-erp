"""Marketing cash excludes tax credit even when gross AR is settled."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from django.test import TestCase

from finance import services as finance
from finance.tests_tax_money import TaxMoneyFixtures
from marketing import kpi, services
from marketing.analytics import marketing_snapshot


class MarketingTaxReceiptTests(TaxMoneyFixtures, TestCase):
    def setUp(self):
        super().setUp()
        self.profile()
        self.member = services.create_member(
            organization=self.org,
            actor=self.actor,
            name="Synthetic sales PIC",
            discipline="sales",
        )
        self.campaign = services.create_campaign(
            organization=self.org,
            actor=self.actor,
            name="Synthetic campaign",
            channel="meta_ads",
            owner=self.member,
            product=self.product,
        )
        self.invoice_row = finance.issue_invoice(
            organization=self.org, actor=self.actor, invoice=self.invoice()
        )
        self.lead = services.create_lead(
            organization=self.org,
            actor=self.actor,
            reference="SYN-TAX-LEAD",
            name="Synthetic lead",
            owner=self.member,
            campaign=self.campaign,
            received_at=datetime(2026, 9, 1, 9, tzinfo=ZoneInfo("Asia/Jakarta")),
            product=self.product,
            party=self.party,
            segment="reseller",
        )
        services.link_invoice(
            organization=self.org,
            actor=self.actor,
            lead=self.lead,
            invoice=self.invoice_row,
            expected_version=self.lead.version,
        )
        self.obligation = self.approved(invoice=self.invoice_row)
        self.credit(self.obligation)

    def snapshot(self):
        return marketing_snapshot(
            organization=self.org, start="2026-09-01", end="2026-09-30"
        )

    def test_mixed_cash_and_credit_settlement_counts_cash_and_gross_separately(self):
        self.net_receipt(self.obligation, amount="480000")
        finance.reconcile_receipt(
            organization=self.org,
            actor=self.actor,
            invoice=self.invoice_row,
            transaction=self.movement("10000", reference="SYN-CASH-REMAINDER"),
            amount="10000",
        )
        report = self.snapshot()
        self.assertEqual(report["summary"]["cash_in"], "490000.00")
        self.assertEqual(report["summary"]["cohort_cash_in"], "490000.00")
        self.assertEqual(report["summary"]["cohort_applied_payments"], "500000.00")
        self.assertEqual(report["summary"]["cohort_paid_leads"], 1)
        self.assertEqual(report["coverage"]["total_customer_receipts"], "490000.00")
        self.assertEqual(report["coverage"]["unattributed_receipts"], "0.00")

    def test_cash_and_conversion_kpi_include_dated_immutable_tax_receipt_sources(self):
        cash = kpi.create_target(
            organization=self.org,
            actor=self.actor,
            title="Synthetic cash target",
            metric_key="cash_in",
            target="400000",
            period_start="2026-09-01",
            period_end="2026-09-30",
            rationale="Synthetic source-checked cash",
        )
        conversion = kpi.create_target(
            organization=self.org,
            actor=self.actor,
            title="Synthetic conversion",
            metric_key="cohort_paid_leads",
            target="1",
            period_start="2026-09-01",
            period_end="2026-09-30",
            rationale="Synthetic source-checked conversion",
        )
        before = kpi.evaluate_target(cash)
        receipt = self.net_receipt(self.obligation)
        after = kpi.evaluate_target(cash)
        self.assertEqual(after["actual"], "490000.00")
        self.assertNotEqual(
            before["basis"]["provenance"]["digest"],
            after["basis"]["provenance"]["digest"],
        )
        sources = after["basis"]["provenance"]["sources"]["customer_tax_settlements"]
        self.assertEqual(sources["count"], 1)
        self.assertEqual(sources["records"], [{"id": receipt.pk}])
        self.assertFalse(after["official"])
        paid = kpi.evaluate_target(conversion)
        self.assertEqual(paid["actual"], "1.00")
        self.assertEqual(
            paid["basis"]["provenance"]["sources"]["customer_tax_settlements"]["count"],
            1,
        )
        self.assertEqual(
            marketing_snapshot(
                organization=self.org, start="2026-08-01", end="2026-08-31"
            )["summary"]["cash_in"],
            "0.00",
        )

    def test_late_credit_closes_ar_without_second_cash_or_new_campaign_receipt(self):
        allocation = finance.reconcile_receipt(
            organization=self.org,
            actor=self.actor,
            invoice=self.invoice_row,
            transaction=self.movement("490000", reference="SYN-EARLIER-CASH"),
            amount="490000",
        )
        cash = kpi.create_target(
            organization=self.org,
            actor=self.actor,
            title="Synthetic received cash",
            metric_key="cash_in",
            target="400000",
            period_start="2026-09-01",
            period_end="2026-10-08",
            rationale="Synthetic cash with a later certificate",
        )
        conversion = kpi.create_target(
            organization=self.org,
            actor=self.actor,
            title="Synthetic paying leads",
            metric_key="cohort_paid_leads",
            target="1",
            period_start="2026-09-01",
            period_end="2026-10-08",
            rationale="Synthetic certificate provenance",
        )
        before_cash = kpi.evaluate_target(cash)
        before_conversion = kpi.evaluate_target(conversion)
        event = finance.recognize_customer_tax_credit(
            organization=self.org,
            actor=self.actor,
            obligation=self.obligation,
            source_allocation=allocation,
            date=date(2026, 10, 5),
            reference="SYN-LATE-CREDIT",
            evidence="Synthetic verified certificate after matching",
        )
        report = marketing_snapshot(
            organization=self.org, start="2026-09-01", end="2026-10-08"
        )
        self.assertEqual(report["summary"]["cash_in"], "490000.00")
        self.assertEqual(report["summary"]["cohort_applied_payments"], "500000.00")
        self.assertEqual(
            self.snapshot()["summary"]["cohort_applied_payments"], "490000.00"
        )
        october = marketing_snapshot(
            organization=self.org, start="2026-10-01", end="2026-10-08"
        )
        self.assertEqual(october["summary"]["cash_in"], "0.00")
        self.assertEqual(october["coverage"]["total_customer_receipts"], "0.00")
        self.assertTrue(october["summary"]["cash_cost_coverage_complete"])
        self.assertEqual(october["rows"], [])
        after_cash = kpi.evaluate_target(cash)
        after_conversion = kpi.evaluate_target(conversion)
        self.assertEqual(after_cash["actual"], "490000.00")
        self.assertEqual(
            before_cash["basis"]["provenance"], after_cash["basis"]["provenance"]
        )
        self.assertNotEqual(
            before_conversion["basis"]["provenance"]["digest"],
            after_conversion["basis"]["provenance"]["digest"],
        )
        source = after_conversion["basis"]["provenance"]["sources"][
            "customer_tax_settlements"
        ]
        self.assertEqual(source["count"], 1)
        self.assertEqual(source["records"], [{"id": event.pk}])
