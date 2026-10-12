"""Recovery changes business reviews even when amounts or active sets match."""

import csv
import io
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import Membership, Organization
from finance.models import BankTransaction, Journal

from . import kpi, services
from .activity_models import WorkActivity
from .ad_recovery import (
    observation_fingerprint,
    replace_ad_observation,
    withdraw_ad_observation,
)
from .ad_sources import ad_source_state
from .ai import _outbound_body, advice_identity, build_recommendations, marketing_advice
from .analytics import marketing_snapshot
from .coverage import declaration_current, observation_state
from .management import management_snapshot
from .models import MarketingAction
from .workflow import transition_campaign

START, END = date(2026, 9, 1), date(2026, 9, 30)


@override_settings(MARKETING_AI_PRIVATE_AGGREGATES_ENABLED=False)
class AdRecoveryConsumerTests(TestCase):
    def setUp(self):
        clock = patch(
            "django.utils.timezone.now",
            return_value=datetime(2026, 10, 9, 6, tzinfo=timezone.utc),
        )
        clock.start()
        self.addCleanup(clock.stop)
        network = patch(
            "marketing.ai.urlopen",
            side_effect=AssertionError("Providers stay off in recovery QA"),
        )
        self.network = network.start()
        self.addCleanup(network.stop)
        self.org = Organization.objects.create(
            name="Synthetic source recovery consumers"
        )
        self.owner = get_user_model().objects.create_user(username="ad-consumer-owner")
        Membership.objects.create(organization=self.org, user=self.owner, role="owner")
        self.campaign = services.create_campaign(
            organization=self.org,
            actor=self.owner,
            name="Synthetic campaign",
            channel="meta_ads",
            start_date=START,
            end_date=END,
        )
        self.client.force_login(self.owner)
        session = self.client.session
        session["organization_id"] = self.org.pk
        session.save()

    def source(self, **changes):
        return services.save_ad_observation(
            organization=self.org,
            actor=self.owner,
            **{
                "campaign": self.campaign,
                "date": date(2026, 9, 10),
                "spend": "100",
                "impressions": 1000,
                "clicks": 10,
                "source": "PRIVATE-SOURCE-SENTINEL",
                "reference": "PRIVATE-DOC-SENTINEL",
                **changes,
            },
        )

    def confirm(self, amount, **changes):
        return services.confirm_source_coverage(
            organization=self.org,
            actor=self.owner,
            **{
                "campaign": self.campaign,
                "period_start": START,
                "period_end": END,
                "declared_spend": amount,
                "source": "All synthetic platform exports",
                "reference": "SOURCE-REVIEW",
                "evidence": "Entire source interval and inactive days checked",
                "confirm_complete": True,
                **changes,
            },
        )

    def target(self):
        obj = kpi.create_target(
            organization=self.org,
            actor=self.owner,
            title="Synthetic reviewed cost limit",
            metric_key="spend",
            target="200",
            period_start=START,
            period_end=END,
            campaign=self.campaign,
            rationale="Cost limit with independently reviewed full source interval",
        )
        return kpi.activate_target(
            organization=self.org,
            actor=self.owner,
            target=obj,
            expected_version=obj.version,
            reason="Scope and limit approved",
        )

    def review(self, target):
        result = kpi.evaluate_target(target)
        review = kpi.review_target(
            organization=self.org,
            actor=self.owner,
            target=target,
            expected_version=target.version,
            expected_fingerprint=result["fingerprint"],
            conclusion="Reviewed original source",
        )
        target.refresh_from_db()
        return review

    def recover(self, observation, *, replace=False, **changes):
        command = replace_ad_observation if replace else withdraw_ad_observation
        return command(
            organization=self.org,
            actor=self.owner,
            observation=observation,
            expected_version=observation.version,
            expected_fingerprint=observation_fingerprint(observation),
            request_key="RECOVERY-" + str(observation.pk),
            reason="PRIVATE-REASON-SENTINEL",
            evidence="PRIVATE-EVIDENCE-SENTINEL",
            **changes,
        )

    def snapshot(self):
        value = marketing_snapshot(organization=self.org, start=START, end=END)
        value["management"] = management_snapshot(
            organization=self.org, start=START, end=END
        )
        return value

    def test_withdrawal_holds_kpi_and_director_consumers_without_rewriting_saved_review(
        self,
    ):
        original = self.source()
        coverage = self.confirm("100")
        target = self.target()
        review = self.review(target)
        saved_snapshot = json.loads(json.dumps(review.snapshot))
        self.assertEqual(review.actual, Decimal(100))
        self.assertTrue(kpi.evaluate_target(target)["official"])
        self.recover(original)
        now = kpi.evaluate_target(target)
        self.assertFalse(declaration_current(coverage))
        self.assertEqual(now["state"], "incomplete")
        self.assertIsNone(now["actual"])
        self.assertFalse(now["official"])
        self.assertFalse(now["completeness"]["complete"])
        review.refresh_from_db()
        self.assertEqual(review.snapshot, saved_snapshot)
        self.assertEqual(review.actual, Decimal(100))
        self.assertNotEqual(review.fingerprint, now["fingerprint"])
        with self.assertRaises(ValidationError):
            kpi.review_target(
                organization=self.org,
                actor=self.owner,
                target=target,
                expected_version=target.version,
                expected_fingerprint=review.fingerprint,
                conclusion="Old source must not be reused",
            )
        management = management_snapshot(organization=self.org, start=START, end=END)
        self.assertEqual(
            [
                management["counts"][key]
                for key in (
                    "kpi_sources_incomplete",
                    "kpi_stale_reviews",
                    "kpi_met",
                    "kpi_not_met",
                )
            ],
            [1, 1, 0, 0],
        )
        response = self.client.get(reverse("director:snapshot_api"), {"year": "2026"})
        self.assertEqual(response.status_code, 200)
        api = response.json()
        self.assertEqual(
            api["marketing_management"]["counts"]["kpi_sources_incomplete"], 1
        )
        fact = next(
            item
            for item in api["facts"]
            if item["fact_id"] == "marketing.kpi_sources_incomplete"
        )
        self.assertEqual(fact["value"], 1)
        exported = self.client.get(reverse("director:export"), {"year": "2026"})
        self.assertEqual(exported.status_code, 200)
        csv_rows = list(csv.reader(io.StringIO(exported.content.decode("utf-8-sig"))))
        row = next(
            item for item in csv_rows if item[0] == "Target menunggu kelengkapan sumber"
        )
        self.assertEqual(row[1], "1")
        self.assertFalse(Journal.objects.filter(organization=self.org).exists())
        self.assertFalse(BankTransaction.objects.filter(organization=self.org).exists())

    def test_explicit_empty_zero_create_withdraw_cycle_stales_the_old_zero_and_review(
        self,
    ):
        coverage = self.confirm("0")
        target = self.target()
        review = self.review(target)
        before = self.snapshot()
        empty_fingerprint, empty_total = observation_state(
            organization=self.org, campaign=self.campaign, start=START, end=END
        )
        self.assertEqual(empty_total, Decimal(0))
        self.assertTrue(before["summary"]["source_period_complete"])
        self.assertEqual(kpi.evaluate_target(target)["actual"], "0.00")
        original = self.source()
        self.recover(original)
        after = self.snapshot()
        new_fingerprint, new_total = observation_state(
            organization=self.org, campaign=self.campaign, start=START, end=END
        )
        self.assertEqual(new_total, empty_total)
        self.assertNotEqual(new_fingerprint, empty_fingerprint)
        self.assertFalse(declaration_current(coverage))
        self.assertEqual(after["summary"]["spend_rows"], 0)
        self.assertIsNone(after["summary"]["spend"])
        self.assertFalse(after["summary"]["source_period_complete"])
        self.assertNotEqual(
            after["ad_sources"]["fingerprint"], before["ad_sources"]["fingerprint"]
        )
        self.assertNotEqual(advice_identity(before), advice_identity(after))
        self.assertFalse(kpi.evaluate_target(target)["official"])
        review.refresh_from_db()
        self.assertEqual(review.actual, Decimal(0))
        self.assertTrue(review.snapshot["official"])

    def test_equal_values_without_kpi_change_advice_and_reject_same_recommendation_token(
        self,
    ):
        original = self.source()
        page = self.client.get(
            reverse("marketing:advisor"), {"start": str(START), "end": str(END)}
        )
        self.assertEqual(page.status_code, 200)
        recommendation = next(
            item
            for item in page.context["advice"]["recommendations"]
            if item["id"] == "confirm_period_coverage"
        )
        token = recommendation["action_token"]
        before = self.snapshot()
        successor = self.recover(
            original, replace=True, ad_name="Actual real detail"
        ).successor
        after = self.snapshot()
        for key in ("spend", "spend_rows", "impressions", "clicks"):
            self.assertEqual(after["summary"][key], before["summary"][key])
        self.assertNotEqual(successor.pk, original.pk)
        self.assertEqual(after["management"]["target_count"], 0)
        self.assertNotEqual(
            before["management"]["fingerprint"], after["management"]["fingerprint"]
        )
        self.assertNotEqual(advice_identity(before), advice_identity(after))
        self.assertIn(
            recommendation["id"],
            [item["id"] for item in build_recommendations(after)["recommendations"]],
        )
        member = services.create_member(
            organization=self.org,
            actor=self.owner,
            name="Synthetic work PIC",
            discipline="content",
        )
        payload = {
            "recommendation_token": token,
            "title": "Keep my entered title",
            "description": "Keep entered description",
            "evidence": "Keep entered evidence",
            "metric_key": "spend_known_rows",
            "owner": member.pk,
            "due_date": "2026-10-20",
            "channel": "meta_ads",
            "kind": "content",
        }
        denied = self.client.post(reverse("marketing:action_create"), payload)
        self.assertEqual(denied.status_code, 400)
        self.assertContains(denied, "Data sumber berubah", status_code=400)
        for field in (
            "title",
            "description",
            "evidence",
            "owner",
            "due_date",
            "channel",
            "kind",
        ):
            self.assertEqual(
                str(denied.context["form"][field].value()), str(payload[field])
            )
        self.assertFalse(MarketingAction.objects.filter(organization=self.org).exists())
        self.assertFalse(WorkActivity.objects.filter(organization=self.org).exists())

    def test_moving_equal_source_changes_both_scopes_and_not_an_unaffected_period(self):
        original = self.source()
        second = services.create_campaign(
            organization=self.org,
            actor=self.owner,
            name="Synthetic actual account",
            channel="google_ads",
            start_date=START,
            end_date=END,
        )
        old = self.confirm("100")
        new = self.confirm("0", campaign=second, reference="NEW-CAMPAIGN-COVERAGE")
        before = [
            ad_source_state(
                organization=self.org,
                start=START,
                end=END,
                filters={"campaign": campaign.pk},
            )
            for campaign in (self.campaign, second)
        ]
        untouched = ad_source_state(
            organization=self.org, start=date(2026, 8, 1), end=date(2026, 8, 31)
        )
        self.recover(original, replace=True, campaign=second, date=date(2026, 9, 11))
        for campaign, previous in zip((self.campaign, second), before):
            self.assertNotEqual(
                ad_source_state(
                    organization=self.org,
                    start=START,
                    end=END,
                    filters={"campaign": campaign.pk},
                )["fingerprint"],
                previous["fingerprint"],
            )
        self.assertEqual(
            ad_source_state(
                organization=self.org, start=date(2026, 8, 1), end=date(2026, 8, 31)
            ),
            untouched,
        )
        self.assertFalse(declaration_current(old))
        self.assertFalse(declaration_current(new))
        old_report = marketing_snapshot(
            organization=self.org,
            start=START,
            end=END,
            filters={"campaign": self.campaign.pk},
        )
        new_report = marketing_snapshot(
            organization=self.org, start=START, end=END, filters={"campaign": second.pk}
        )
        self.assertEqual(old_report["summary"]["spend_rows"], 0)
        self.assertIsNone(old_report["summary"]["spend"])
        self.assertEqual(new_report["summary"]["spend"], "100.00")

    def test_a_withdrawn_zero_does_not_make_empty_report_a_certified_zero(self):
        original = self.source(spend="0")
        coverage = self.confirm("0")
        self.assertEqual(self.snapshot()["summary"]["spend"], "0.00")
        self.recover(original)
        after = self.snapshot()
        self.assertEqual(after["summary"]["spend_rows"], 0)
        self.assertIsNone(after["summary"]["spend"])
        self.assertFalse(declaration_current(coverage))
        original.refresh_from_db()
        self.assertEqual(original.spend, Decimal(0))

    def test_withdrawn_unknown_does_not_poison_current_known_facts_or_create_zero(self):
        unknown = self.source(spend=None)
        self.source(reference="KNOWN-OTHER-DATE", date=date(2026, 9, 12), spend="80")
        before = self.snapshot()
        self.assertFalse(before["summary"]["recorded_spend_complete"])
        self.recover(unknown)
        after = self.snapshot()
        self.assertEqual(after["summary"]["spend"], "80.00")
        self.assertEqual(after["summary"]["spend_rows"], 1)
        self.assertTrue(after["summary"]["recorded_spend_complete"])
        self.assertFalse(after["summary"]["source_period_complete"])

    def test_archived_wrong_date_stops_blocking_actual_campaign_completion(self):
        original = self.source(date=date(2026, 9, 20))
        command = {
            "organization": self.org,
            "actor": self.owner,
            "campaign": self.campaign,
            "expected_version": self.campaign.version,
            "operation": "complete",
            "effective_on": date(2026, 9, 15),
            "reason": "Actual campaign stopped on the fifteenth",
        }
        with self.assertRaises(ValidationError):
            transition_campaign(**command)
        self.recover(original)
        completed = transition_campaign(**command)
        self.assertEqual(completed.status, "completed")
        self.assertEqual(completed.actual_end_date, date(2026, 9, 15))
        original.refresh_from_db()
        self.assertEqual(original.date, date(2026, 9, 20))
        self.assertIsNotNone(original.withdrawn_at)

    def test_recovery_changes_local_provenance_without_disclosing_private_text_to_ai(
        self,
    ):
        original = self.source()
        before = marketing_advice(
            organization=self.org,
            actor=self.owner,
            snapshot=self.snapshot(),
            question="Prioritas iklan?",
        )
        self.recover(original, replace=True, ad_name="PRIVATE-AD-SENTINEL")
        snapshot = self.snapshot()
        after = marketing_advice(
            organization=self.org,
            actor=self.owner,
            snapshot=snapshot,
            question="PRIVATE-QUESTION-SENTINEL",
        )
        self.assertNotEqual(before["snapshot_id"], after["snapshot_id"])
        self.assertEqual(after["mode"], "local")
        body = _outbound_body(
            after, snapshot, {"model": "vendor/synthetic", "provider": "synthetic"}
        ).decode()
        for secret in (
            "PRIVATE-SOURCE-SENTINEL",
            "PRIVATE-DOC-SENTINEL",
            "PRIVATE-REASON-SENTINEL",
            "PRIVATE-EVIDENCE-SENTINEL",
            "PRIVATE-AD-SENTINEL",
            "PRIVATE-QUESTION-SENTINEL",
            "ad-consumer-owner",
            "source_references",
            "ad_sources",
            "fingerprint",
            "predecessor_id",
        ):
            self.assertNotIn(secret, body)
        self.network.assert_not_called()
