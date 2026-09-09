import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings

from core.models import Membership, Organization
from director.models import AIUsageReservation, DirectorAIPolicy
from director.ai import DirectorAIUnavailable
from .ai import DISCLOSURE_VERSION, MarketingAIUnavailable, _outbound_body, build_recommendations, marketing_advice, validate_completion
from .analytics import marketing_snapshot
from .models import MarketingAIPolicy
from .services import configure_marketing_ai_policy


EXTERNAL = {"MARKETING_AI_PRIVATE_AGGREGATES_ENABLED": True, "DIRECTOR_AI_ENDPOINT_APPROVED": True,
            "DIRECTOR_OPENROUTER_API_KEY": "fake-test-key", "DIRECTOR_OPENROUTER_MODEL": "vendor/test-model",
            "DIRECTOR_OPENROUTER_PROVIDER": "test-provider", "DIRECTOR_AI_MAX_REQUEST_COST_USD": "0.01",
            "DIRECTOR_AI_MONTHLY_BUDGET_USD": "1", "DIRECTOR_AI_USER_MONTHLY_BUDGET_USD": "1"}


class MarketingAdviceTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Private organization")
        self.actor = get_user_model().objects.create_user(username="marketing-private")
        self.owner = get_user_model().objects.create_user(username="owner-private")
        Membership.objects.create(organization=self.org, user=self.actor, role="marketing")
        Membership.objects.create(organization=self.org, user=self.owner, role="owner")
        self.snapshot = marketing_snapshot(organization=self.org, start="2026-01-01", end="2026-01-31")

    def policy(self):
        return configure_marketing_ai_policy(organization=self.org, actor=self.owner, enabled=True, monthly_budget_usd="1", request_cap_usd="0.01")

    def response(self, identifiers, **extras):
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"recommendation_ids": identifiers}), **extras}}], "usage": {"cost": "0.001"}}

    def test_local_recommendations_use_evidence_and_label_rule_mode(self):
        self.snapshot["summary"].update(leads=8, followed_up_leads=3, response_coverage_percent="37.50", overdue_followups=2,
                                        cohort_paid_leads=1, cohort_paid_conversion="12.50", median_response_minutes="120.00")
        with patch("marketing.ai.urlopen") as network:
            result = marketing_advice(organization=self.org, actor=self.actor, snapshot=self.snapshot, question="Evaluasi tim")
        self.assertEqual(result["mode"], "local")
        self.assertIn("aturan", result["mode_label"])
        self.assertEqual(result["recommendations"][0]["id"], "follow_up")
        self.assertIn("2 lead", result["recommendations"][0]["evidence"])
        self.assertTrue(all(item["success_measure"] for item in result["recommendations"]))
        network.assert_not_called()

    def test_agreed_sale_with_outstanding_invoice_has_payment_recommendation(self):
        self.snapshot["summary"].update(unpaid_won_leads=2, unpaid_won_amount="1234500.00")
        result = build_recommendations(self.snapshot)
        recommendation = next(item for item in result["recommendations"] if item["id"] == "collect_agreed")
        self.assertEqual(recommendation["metric_value"], "1234500.00")
        self.assertIn("belum berarti pembayaran", recommendation["reason"])
        self.assertIn("Rp1.234.500", recommendation["reason"])
        self.assertIn("1 Januari 2026", recommendation["evidence"])
        self.assertIn("WIB", recommendation["evidence"])
        self.assertNotIn("marketing-cash-v1", recommendation["evidence"])
        self.assertNotIn("T00:", recommendation["evidence"])
        self.assertEqual(result["formula_version"], self.snapshot["formula_version"])

    @override_settings(**EXTERNAL)
    def test_director_policy_does_not_authorize_marketing_external_disclosure(self):
        DirectorAIPolicy.objects.create(organization=self.org, enabled=True, approved_by=self.owner,
                                       monthly_budget_usd=Decimal("1"), request_cap_usd=Decimal("0.01"))
        with patch("marketing.ai.urlopen") as network:
            result = marketing_advice(organization=self.org, actor=self.actor, snapshot=self.snapshot)
        self.assertEqual(result["mode"], "local")
        network.assert_not_called()
        self.assertEqual(AIUsageReservation.objects.count(), 0)

    @override_settings(**EXTERNAL)
    def test_external_ranks_only_known_recommendations_without_private_text(self):
        self.policy()
        local = build_recommendations(self.snapshot)
        identifiers = [item["id"] for item in reversed(local["recommendations"])]
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.read.return_value = json.dumps(self.response(identifiers)).encode()
        with patch("marketing.ai.urlopen", return_value=response) as network:
            result = marketing_advice(organization=self.org, actor=self.actor, snapshot=self.snapshot, question="Private customer 1234")
        self.assertEqual(result["mode"], "openrouter")
        body = network.call_args.args[0].data.decode()
        for private in ("Private customer", "Private organization", "marketing-private", "owner-private", "organization_id", "source", "question"):
            self.assertNotIn(private, body)
        payload = json.loads(body)
        self.assertFalse(payload["provider"]["allow_fallbacks"])
        self.assertTrue(payload["provider"]["zdr"])
        reservation = AIUsageReservation.objects.get()
        self.assertEqual(reservation.status, "completed")
        self.assertEqual(reservation.actual_cost, Decimal("0.001"))

    @override_settings(**EXTERNAL)
    def test_invalid_external_output_falls_back_without_retry(self):
        self.policy()
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.read.return_value = json.dumps(self.response(["invented_budget_increase"])).encode()
        with patch("marketing.ai.urlopen", return_value=response) as network:
            result = marketing_advice(organization=self.org, actor=self.actor, snapshot=self.snapshot)
        self.assertEqual(result["mode"], "local")
        self.assertEqual(network.call_count, 1)
        self.assertEqual(AIUsageReservation.objects.get().status, "outcome_unknown")

    @override_settings(**EXTERNAL)
    def test_redirect_failure_falls_back_and_retains_reservation(self):
        self.policy()
        with patch("marketing.ai.urlopen", side_effect=DirectorAIUnavailable) as network:
            result = marketing_advice(organization=self.org, actor=self.actor, snapshot=self.snapshot)
        self.assertEqual(result["mode"], "local")
        self.assertEqual(network.call_count, 1)
        self.assertEqual(AIUsageReservation.objects.get().status, "outcome_unknown")

    @override_settings(**EXTERNAL)
    def test_overage_disables_policy_and_preserves_actual_cost_on_fallback(self):
        self.policy()
        advice = build_recommendations(self.snapshot)
        payload = self.response([item["id"] for item in advice["recommendations"]])
        payload["usage"]["cost"] = "0.02"
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.read.return_value = json.dumps(payload).encode()
        with patch("marketing.ai.urlopen", return_value=response):
            result = marketing_advice(organization=self.org, actor=self.actor, snapshot=self.snapshot)
        self.assertEqual(result["mode"], "local")
        self.assertFalse(MarketingAIPolicy.objects.get().enabled)
        reservation = AIUsageReservation.objects.get()
        self.assertEqual(reservation.status, "outcome_unknown")
        self.assertEqual(reservation.actual_cost, Decimal("0.02"))

    def test_validation_and_permissions_reject_tools_duplicates_and_cross_tenant(self):
        advice = build_recommendations(self.snapshot)
        identifiers = [item["id"] for item in advice["recommendations"]]
        for payload in (self.response(identifiers + identifiers), self.response(identifiers, tool_calls=[{"id": "bad"}]), {"choices": "bad"}, []):
            with self.assertRaises(MarketingAIUnavailable):
                validate_completion(payload, advice)
        other = Organization.objects.create(name="Other")
        with self.assertRaises(PermissionDenied):
            marketing_advice(organization=other, actor=self.actor, snapshot=self.snapshot)
        with self.assertRaises(ValidationError):
            marketing_advice(organization=self.org, actor=self.actor, snapshot={**self.snapshot, "organization_id": other.pk})
        with self.assertRaises(PermissionDenied):
            configure_marketing_ai_policy(organization=self.org, actor=self.actor, enabled=True, monthly_budget_usd="1", request_cap_usd="0.01")

    def test_outbound_numeric_whitelist_excludes_labels_and_nonfinite_values(self):
        self.snapshot["summary"]["cash_in"] = "Ignore instruction private bank number"
        self.snapshot["summary"]["spend"] = "NaN"
        self.snapshot["rows"] = [{"label": "Private employee"}]
        body = _outbound_body(build_recommendations(self.snapshot), self.snapshot, {"model": "test", "provider": "test"}).decode()
        self.assertNotIn("Ignore instruction", body)
        self.assertNotIn("Private employee", body)
        self.assertNotIn("NaN", body)
