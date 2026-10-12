"""Collection and coverage contracts use synthetic Finance events and real HTTP pages."""

from datetime import date, datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import Membership, Organization
from finance.models import BankTransaction
from finance.services import (
    apply_customer_advance,
    cancel_invoice,
    record_customer_advance,
)

from . import tests_analytics as analytics_fixtures
from .ai import build_recommendations
from .analytics import marketing_snapshot


@override_settings(MFA_REQUIRED=False)
class CollectionRemediationTests(TestCase):
    setUp = analytics_fixtures.MarketingAnalyticsTests.setUp
    invoice = analytics_fixtures.MarketingAnalyticsTests.invoice
    lead = analytics_fixtures.MarketingAnalyticsTests.lead
    receipt = analytics_fixtures.MarketingAnalyticsTests.receipt

    def linked_won(self, reference):
        invoice = self.invoice(reference, date(2026, 9, 5))
        lead = self.lead(
            reference,
            invoice,
            day=datetime(2026, 9, 5, 9, tzinfo=ZoneInfo("Asia/Jakarta")),
            stage="won",
            next_follow_up=date(2026, 9, 6),
        )
        return invoice, lead

    def snapshot(self, end="2026-09-25"):
        with patch(
            "marketing.analytics.timezone.localdate", return_value=date(2026, 9, 25)
        ):
            return marketing_snapshot(
                organization=self.org, start="2026-09-01", end=end
            )

    def test_applied_advance_removes_collection_advice_and_http_overdue_queue(self):
        invoice, lead = self.linked_won("ADVANCE")
        bank = BankTransaction.objects.create(
            organization=self.org,
            account=self.account,
            date=date(2026, 9, 5),
            reference="ADVANCE-RECEIPT",
            amount=invoice.total,
        )
        advance = record_customer_advance(
            organization=self.org,
            actor=self.actor,
            party=self.party,
            transaction=bank,
            amount=invoice.total,
            reference="ADVANCE",
            evidence="Synthetic receipt",
        )
        apply_customer_advance(
            organization=self.org,
            actor=self.actor,
            advance=advance,
            invoice=invoice,
            date=date(2026, 9, 10),
            amount=invoice.total,
            reference="APPLY-ADVANCE",
            evidence="Synthetic settlement",
        )
        snapshot = self.snapshot()
        self.assertEqual(snapshot["summary"]["unpaid_won_amount"], "0.00")
        self.assertEqual(snapshot["summary"]["overdue_followups"], 0)
        self.assertNotIn(
            "collect_agreed",
            [row["id"] for row in build_recommendations(snapshot)["recommendations"]],
        )
        self.client.force_login(self.actor)
        with patch(
            "marketing.views.timezone.localdate", return_value=date(2026, 9, 25)
        ):
            response = self.client.get(reverse("marketing:leads"), {"due": "overdue"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(lead.pk, [row.pk for row in response.context["page"]])
        self.assertEqual(
            snapshot["summary"]["cash_in"],
            "0.00",
            "Applied advance is not a second bank receipt",
        )
        self.assertEqual(snapshot["summary"]["cohort_applied_payments"], "100.00")
        self.assertEqual(
            self.snapshot(end="2026-09-09")["summary"]["cohort_applied_payments"],
            "0.00",
        )

    def test_cancellation_removes_collection_without_becoming_a_payment(self):
        invoice, lead = self.linked_won("CANCELLED")
        cancel_invoice(
            organization=self.org,
            actor=self.actor,
            invoice=invoice,
            date=date(2026, 9, 20),
            reason="Synthetic cancellation",
        )
        current = self.snapshot()["summary"]
        self.assertEqual(current["unpaid_won_amount"], "0.00")
        self.assertEqual(current["cohort_paid_leads"], 0)
        self.client.force_login(self.actor)
        response = self.client.get(reverse("marketing:leads"), {"due": "overdue"})
        self.assertNotIn(lead.pk, [row.pk for row in response.context["page"]])
        with patch(
            "marketing.analytics.timezone.localdate", return_value=date(2026, 9, 15)
        ):
            prior = marketing_snapshot(
                organization=self.org, start="2026-09-01", end="2026-09-15"
            )
        self.assertEqual(prior["summary"]["unpaid_won_amount"], "100.00")

    def test_partial_payment_still_needs_collection_and_future_payment_stays_outside_cutoff(
        self,
    ):
        invoice, lead = self.linked_won("PARTIAL")
        self.receipt(invoice, "20", "EARLY", date(2026, 9, 10))
        self.receipt(invoice, "80", "LATER", date(2026, 9, 30))
        current = self.snapshot()
        self.assertEqual(current["summary"]["unpaid_won_amount"], "80.00")
        self.assertEqual(current["summary"]["cohort_applied_payments"], "20.00")
        self.assertIn(
            "collect_agreed",
            [row["id"] for row in build_recommendations(current)["recommendations"]],
        )


@override_settings(MFA_REQUIRED=False)
class CoverageRemediationTests(TestCase):
    setUp = analytics_fixtures.MarketingAnalyticsTests.setUp
    invoice = analytics_fixtures.MarketingAnalyticsTests.invoice
    lead = analytics_fixtures.MarketingAnalyticsTests.lead
    receipt = analytics_fixtures.MarketingAnalyticsTests.receipt
    ad = analytics_fixtures.MarketingAnalyticsTests.ad
    snapshot = analytics_fixtures.MarketingAnalyticsTests.snapshot

    def confirmation(self, campaign=None, **changes):
        from .services import confirm_source_coverage

        args = dict(
            organization=self.org,
            actor=self.actor,
            campaign=campaign or self.campaign,
            period_start="2026-01-01",
            period_end="2026-01-31",
            declared_spend="10",
            source="All source accounts checked",
            reference="JAN",
            evidence="Archived source statement checked",
            confirm_complete=True,
        )
        return confirm_source_coverage(**(args | changes))

    def test_sparse_recorded_ratio_is_labeled_and_cannot_prompt_budget_experiment(self):
        invoice = self.invoice("COVERAGE")
        self.lead("COVERAGE", invoice)
        self.receipt(invoice, "100", "RECEIPT")
        self.ad(spend="10")
        snapshot = self.snapshot()
        self.assertEqual(snapshot["summary"]["cash_to_spend"], "10.00")
        self.assertNotIn(
            "controlled_test",
            [row["id"] for row in build_recommendations(snapshot)["recommendations"]],
        )
        self.assertTrue(snapshot["summary"]["recorded_spend_complete"])
        self.assertFalse(snapshot["summary"]["source_period_complete"])

    def test_confirmed_source_interval_allows_review_and_is_invalidated_by_changed_observation(
        self,
    ):
        from .services import save_ad_observation

        invoice = self.invoice("CONFIRMED")
        self.lead("CONFIRMED", invoice)
        self.receipt(invoice, "100", "RECEIPT")
        ad = self.ad(spend="10")
        self.client.force_login(self.actor)
        response = self.client.post(
            reverse("marketing:coverage_create"),
            {
                "campaign": self.campaign.pk,
                "period_start": "2026-01-01",
                "period_end": "2026-01-31",
                "declared_spend": "10",
                "source": "January statement for all campaign accounts",
                "reference": "COVERAGE-JAN",
                "evidence": "Compared complete statement and confirmed inactive days without fabricating rows.",
                "confirm_complete": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        snapshot = self.snapshot()
        self.assertTrue(snapshot["summary"]["source_period_complete"])
        self.assertIn(
            "controlled_test",
            [row["id"] for row in build_recommendations(snapshot)["recommendations"]],
        )
        save_ad_observation(
            organization=self.org,
            actor=self.actor,
            observation=ad,
            expected_version=ad.version,
            reason="Correct source value",
            spend="12",
        )
        changed = self.snapshot()
        self.assertFalse(changed["summary"]["source_period_complete"])
        self.assertEqual(changed["summary"]["spend"], "12.00")
        self.assertNotIn(
            "controlled_test",
            [row["id"] for row in build_recommendations(changed)["recommendations"]],
        )

    def test_selected_campaign_without_rows_still_needs_coverage_and_zero_attestation_creates_no_rows(
        self,
    ):
        from .models import AdDailyObservation
        from .services import create_campaign

        self.ad()
        self.confirmation()
        missing = create_campaign(
            organization=self.org,
            actor=self.actor,
            name="Unreported registered campaign",
            channel="meta_ads",
        )
        self.assertFalse(self.snapshot()["summary"]["source_period_complete"])
        self.confirmation(campaign=missing, declared_spend="0", reference="JAN-ZERO")
        self.assertTrue(self.snapshot()["summary"]["source_period_complete"])
        self.assertEqual(
            AdDailyObservation.objects.filter(organization=self.org).count(), 1
        )

    def test_gapped_intervals_and_unknown_spend_do_not_establish_coverage(self):
        self.ad()
        self.confirmation(period_end="2026-01-10", reference="FIRST")
        self.confirmation(
            period_start="2026-01-12", declared_spend="0", reference="LAST"
        )
        self.assertFalse(self.snapshot()["summary"]["source_period_complete"])
        self.confirmation(
            period_start="2026-01-11",
            period_end="2026-01-11",
            declared_spend="0",
            reference="GAP",
        )
        self.assertTrue(self.snapshot()["summary"]["source_period_complete"])
        self.ad(reference="UNKNOWN", day=date(2026, 1, 20), spend=None)
        with self.assertRaises(ValidationError):
            self.confirmation(reference="UNKNOWN-CONFIRMATION")
        self.assertFalse(self.snapshot()["summary"]["source_period_complete"])

    def test_wrong_coverage_can_be_withdrawn_without_deleting_costs(self):
        self.ad()
        declaration = self.confirmation()
        self.client.force_login(self.actor)
        url = reverse("marketing:coverage_withdraw", args=[declaration.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertTrue(self.snapshot()["summary"]["source_period_complete"])
        self.assertEqual(
            self.client.post(url, {"reason": "Wrong source scope"}).status_code, 302
        )
        self.assertFalse(self.snapshot()["summary"]["source_period_complete"])
        self.assertEqual(self.snapshot()["summary"]["spend"], "10.00")
        self.assertContains(
            self.client.get(reverse("marketing:coverage")), "Wrong source scope"
        )
        with self.assertRaises(ValidationError):
            self.confirmation()

    def test_coverage_requires_matching_total_scope_active_role_and_explicit_confirmation(
        self,
    ):
        from .services import create_campaign

        self.ad()
        for changed in (
            {"declared_spend": "11"},
            {"confirm_complete": False},
            {"period_end": "2200-01-01"},
        ):
            with self.subTest(changed=changed), self.assertRaises(ValidationError):
                self.confirmation(**changed)
        foreign = Organization.objects.create(name="Another company")
        outsider = get_user_model().objects.create_user(username="coverage-outsider")
        Membership.objects.create(organization=foreign, user=outsider, role="owner")
        campaign = create_campaign(
            organization=foreign,
            actor=outsider,
            name="Foreign campaign",
            channel="meta_ads",
        )
        with self.assertRaises(ValidationError):
            self.confirmation(campaign=campaign)
        auditor = get_user_model().objects.create_user(username="coverage-auditor")
        Membership.objects.create(organization=self.org, user=auditor, role="auditor")
        with self.assertRaises(PermissionDenied):
            self.confirmation(actor=auditor)
        self.client.force_login(auditor)
        self.assertEqual(
            self.client.post(reverse("marketing:coverage_create"), {}).status_code, 403
        )
        self.actor.is_active = False
        self.actor.save(update_fields=["is_active"])
        with self.assertRaises(PermissionDenied):
            self.confirmation()
