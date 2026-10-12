"""KPI commitments use stored operational facts, not editable self-reported scores."""

from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.utils import timezone

from core.models import AuditEvent, Membership, Organization
from finance.models import BankTransaction, Journal

from . import kpi, services
from . import tests_analytics as fixtures
from .kpi_models import KpiReview, KpiTarget


def certify_lead_sources(organization, actor, target):
    """Explicitly review the recorded lead inventory through the public workflow."""
    from .kpi_completeness import approve_source_period, create_source_period

    result = kpi.evaluate_target(target)
    source_counts = result["basis"]["provenance"]["sources"]
    count = source_counts["leads"]["count"]
    sequence = target.source_periods.count() + 1
    sources = [
        {
            "kind": "leads",
            "name": "Full registration export",
            "reference": f"EXPORT-{target.pk}-{sequence}",
            "expected_count": count,
            "imported_count": count,
            "duplicate_count": 0,
            "excluded_count": 0,
            "unresolved_count": 0,
            "evidence": "Original source checked",
        }
    ]
    if "followups" in source_counts:
        followup_count = source_counts["followups"]["count"]
        sources.append(
            {
                "kind": "followups",
                "name": "Riwayat janji follow-up",
                "reference": f"FOLLOWUPS-{target.pk}-{sequence}",
                "expected_count": followup_count,
                "imported_count": followup_count,
                "duplicate_count": 0,
                "excluded_count": 0,
                "unresolved_count": 0,
                "evidence": "Riwayat janji dan bukti penyelesaian diperiksa",
            }
        )
    period = create_source_period(
        organization=organization,
        actor=actor,
        target=target,
        reference=f"INVENTORY-{target.pk}-{sequence}",
        inventory_note="All lead sources verified against export",
        confirm_all_sources=True,
        sources=sources,
    )
    return approve_source_period(
        organization=organization,
        actor=actor,
        period=period,
        expected_version=period.version,
        expected_fingerprint=result["basis"]["provenance"]["digest"],
        reason="All sources and counts checked",
    )


class MarketingKpiTests(TestCase):
    invoice = fixtures.MarketingAnalyticsTests.invoice
    lead = fixtures.MarketingAnalyticsTests.lead
    receipt = fixtures.MarketingAnalyticsTests.receipt
    ad = fixtures.MarketingAnalyticsTests.ad
    contact = fixtures.MarketingAnalyticsTests.contact

    def setUp(self):
        fixtures.MarketingAnalyticsTests.setUp(self)
        for role in ("finance", "marketing", "director", "auditor"):
            user = get_user_model().objects.create_user(username=f"kpi-{role}")
            Membership.objects.create(organization=self.org, user=user, role=role)
            setattr(self, role, user)

    def target(self, **fields):
        values = {
            "title": "Target Januari",
            "metric_key": "leads",
            "target": "2",
            "period_start": "2026-01-01",
            "period_end": "2026-01-31",
            "rationale": "Dasar target dicatat untuk ditinjau direktur.",
            **fields,
        }
        return kpi.create_target(organization=self.org, actor=self.marketing, **values)

    def activate(self, target):
        return kpi.activate_target(
            organization=self.org,
            actor=self.actor,
            target=target,
            expected_version=target.version,
            reason="Lingkup dan dasar target ditinjau",
        )

    def review(self, target, **changes):
        return kpi.review_target(
            **{
                "organization": self.org,
                "actor": self.actor,
                "target": target,
                "expected_version": target.version,
                "expected_fingerprint": kpi.evaluate_target(target)["fingerprint"],
                "conclusion": "Periksa temuan dan lanjutkan perbaikan kampanye.",
                **changes,
            }
        )

    def confirm(self, **changes):
        return services.confirm_source_coverage(
            **{
                "organization": self.org,
                "actor": self.actor,
                "campaign": self.campaign,
                "period_start": "2026-01-01",
                "period_end": "2026-01-31",
                "declared_spend": "10",
                "source": "Laporan semua akun",
                "reference": "COVER-JAN",
                "evidence": "Total diperiksa dan hari tanpa belanja dipastikan.",
                "confirm_complete": True,
                **changes,
            }
        )

    def test_roles_scope_and_inactive_accounts_cannot_authorize_targets(self):
        target = self.target()
        for actor in (self.marketing, self.finance, self.auditor, None):
            with self.subTest(actor=actor), self.assertRaises(PermissionDenied):
                kpi.activate_target(
                    organization=self.org,
                    actor=actor,
                    target=target,
                    expected_version=1,
                    reason="Approve",
                )
        other = Organization.objects.create(name="KPI other")
        with self.assertRaises(PermissionDenied):
            kpi.create_target(organization=other, actor=self.actor, title="Foreign")
        Membership.objects.create(organization=other, user=self.actor, role="owner")
        with self.assertRaises(ValidationError):
            kpi.activate_target(
                organization=other,
                actor=self.actor,
                target=target,
                expected_version=1,
                reason="Foreign",
            )
        self.marketing.is_active = False
        self.marketing.save(update_fields=["is_active"])
        with self.assertRaises(PermissionDenied):
            self.target()
        self.assertEqual(KpiTarget.objects.count(), 1)
        self.assertEqual(
            AuditEvent.objects.filter(action="marketing.kpi.activated").count(), 0
        )

    def test_metric_value_date_and_attribution_constraints(self):
        cases = [
            {"metric_key": "unknown"},
            {"target": "0"},
            {"target": "1.5"},
            {"target": "NaN"},
            {"target": True},
            {"metric_key": "response_coverage_percent", "target": "100.01"},
            {"metric_key": "cohort_fully_paid_conversion", "target": "100.01"},
            {"metric_key": "followup_on_time_percent", "target": "100.01"},
            {"metric_key": "cohort_fully_paid_leads", "target": "1.5"},
            {"period_start": "2026-02-01"},
            {"period_end": "2027-02-01"},
            {"period_start": "1999-12-31"},
            {"period_end": None},
            {"rationale": " "},
            {"status": "active"},
            {"metric_key": "spend", "member": self.member},
            {"metric_key": "cash_to_spend", "member": self.member},
            {"campaign": self.campaign, "channel": "seo"},
        ]
        for fields in cases:
            with self.subTest(fields=fields), self.assertRaises(ValidationError):
                self.target(**fields)
        target = self.target(campaign=self.campaign)
        self.assertEqual(target.channel, "meta_ads")
        other = Organization.objects.create(name="Foreign scope")
        Membership.objects.create(organization=other, user=self.actor, role="owner")
        member = services.create_member(
            organization=other, actor=self.actor, name="Foreign", discipline="sales"
        )
        campaign = services.create_campaign(
            organization=other, actor=self.actor, name="Foreign", channel="seo"
        )
        for foreign in ({"member": member}, {"campaign": campaign}):
            with self.assertRaises(ValidationError):
                self.target(**foreign)

    def test_kpi_snapshot_carries_the_owner_approved_first_source_rule(self):
        result = kpi.evaluate_target(self.target())

        self.assertIn(
            "Kredit penjualan mengikuti sumber pertama", result["basis"]["attribution"]
        )
        self.assertIn(
            "follow-up tidak memindahkan atribusi", result["basis"]["attribution"]
        )

    def test_commitments_cannot_be_rewritten_and_transitions_are_versioned(self):
        target = self.activate(self.target())
        with self.assertRaises(ValidationError):
            kpi.retire_target(
                organization=self.org,
                actor=self.actor,
                target=target,
                expected_version=1,
                reason="Old view",
            )
        for field, value in (
            ("target", Decimal(1)),
            ("title", "Easier target"),
            ("status", "retired"),
        ):
            target.refresh_from_db()
            setattr(target, field, value)
            target._service_write = True
            with self.assertRaises(ValidationError):
                target.save()
        target.refresh_from_db()
        retired = kpi.retire_target(
            organization=self.org,
            actor=self.director,
            target=target,
            expected_version=target.version,
            reason="Create corrected commitment instead",
        )
        self.assertEqual(retired.status, "retired")
        self.assertIsNone(kpi.evaluate_target(retired)["attainment"])
        with self.assertRaises(ValidationError):
            retired.delete()
        event = AuditEvent.objects.get(action="marketing.kpi.retired")
        self.assertEqual(event.actor_id, self.director.pk)
        self.assertEqual(event.detail["before"]["status"], "active")
        self.assertEqual(event.detail["after"]["status"], "retired")

    def test_overlapping_duplicate_active_scope_is_rejected_and_replacement_keeps_history(
        self,
    ):
        first = self.activate(self.target(member=self.member))
        duplicate = self.target(
            member=self.member, period_start="2026-01-15", period_end="2026-02-10"
        )
        with self.assertRaises(ValidationError):
            self.activate(duplicate)
        # Another employee's separate commitment is valid.
        self.activate(self.target(member=self.buyer))
        kpi.retire_target(
            organization=self.org,
            actor=self.actor,
            target=first,
            expected_version=first.version,
            reason="Revise target",
        )
        second = self.activate(duplicate)
        self.assertEqual(second.status, "active")
        self.assertEqual(KpiTarget.objects.filter(status="retired").count(), 1)

    def test_no_data_and_zero_denominators_do_not_claim_performance(self):
        for metric in (
            "leads",
            "cohort_paid_conversion",
            "cohort_fully_paid_conversion",
            "followup_on_time_percent",
            "median_response_minutes",
            "cash_in",
            "cash_to_spend",
        ):
            with self.subTest(metric=metric):
                target = self.activate(self.target(metric_key=metric))
                result = kpi.evaluate_target(target)
                self.assertIsNone(result["attainment"])
                self.assertFalse(result["official"])
                self.assertIn(result["state"], {"no_data", "incomplete"})
                with self.assertRaises(ValidationError):
                    self.review(target)
        self.assertFalse(KpiReview.objects.exists())

    def test_source_derived_partial_payment_conversion_cash_and_member_scope(self):
        invoice = self.invoice("KPI-PARTIAL")
        self.lead("PAID", invoice)
        self.lead("UNPAID")
        self.receipt(invoice, "25", "PAYMENT-JAN")
        self.receipt(invoice, "30", "PAYMENT-FEB", date(2026, 2, 1))
        cases = (
            ("leads", "2", "2.00", "100.00"),
            ("cohort_paid_leads", "2", "1.00", "50.00"),
            ("cohort_paid_conversion", "100", "50.00", "50.00"),
            ("cash_in", "100", "25.00", "25.00"),
        )
        for metric, value, actual, attainment in cases:
            result = kpi.evaluate_target(
                self.activate(
                    self.target(metric_key=metric, target=value, member=self.member)
                )
            )
            self.assertEqual(
                (result["actual"], result["attainment"]), (actual, attainment)
            )
            self.assertFalse(result["official"])
            self.assertEqual(result["completeness"]["state"], "missing")
            self.assertTrue(result["retroactive"])
        result = kpi.evaluate_target(self.activate(self.target(member=self.buyer)))
        self.assertEqual(result["actual"], "0.00")
        self.assertFalse(result["official"])

    def test_fully_paid_kpi_never_counts_partial_payment_as_lunas(self):
        partial_invoice = self.invoice("KPI-PARTIAL-FULLY-PAID")
        full_invoice = self.invoice("KPI-FULL-FULLY-PAID")
        self.lead("PARTIAL-PAID-LEAD", partial_invoice)
        self.lead("FULL-PAID-LEAD", full_invoice)
        self.receipt(partial_invoice, "50", "KPI-PARTIAL-FULLY-PAID-RCPT")
        self.receipt(full_invoice, "100", "KPI-FULL-FULLY-PAID-RCPT")

        result = kpi.evaluate_target(
            self.activate(
                self.target(metric_key="cohort_fully_paid_conversion", target="100")
            )
        )
        self.assertEqual(result["actual"], "50.00")
        self.assertFalse(result["official"])
        self.assertIn(result["completeness"]["state"], {"missing", "bank_incomplete"})

    def test_followup_kpi_requires_due_promise_inventory_and_can_be_reviewed(self):
        lead = self.lead("KPI-FOLLOWUP")
        due = datetime(2026, 1, 15, 10, tzinfo=ZoneInfo("Asia/Jakarta"))
        promise = self.contact(
            lead,
            occurred_at=datetime(2026, 1, 10, 10, tzinfo=ZoneInfo("Asia/Jakarta")),
            recorded_at=datetime(2026, 1, 10, 10, tzinfo=ZoneInfo("Asia/Jakarta")),
            followup_action="set",
            next_due_at=due,
        )
        self.contact(lead, occurred_at=due, recorded_at=due, completes=promise)
        target = self.activate(
            self.target(metric_key="followup_on_time_percent", target="100")
        )

        pending = kpi.evaluate_target(target)
        self.assertEqual(pending["actual"], "100.00")
        self.assertFalse(pending["official"])
        self.assertEqual(pending["completeness"]["state"], "missing")
        certified = certify_lead_sources(self.org, self.actor, target)
        self.assertIn("followups", {source["kind"] for source in certified.sources})

        result = kpi.evaluate_target(target)
        self.assertTrue(result["official"])
        self.assertEqual(result["attainment"], "100.00")
        self.assertEqual(result["completeness"]["state"], "complete")
        self.review(target)

    def test_followup_source_inventory_count_must_match_all_due_promises(self):
        from .kpi_completeness import approve_source_period, create_source_period

        lead = self.lead("KPI-FOLLOWUP-COUNT")
        due = datetime(2026, 1, 20, 10, tzinfo=ZoneInfo("Asia/Jakarta"))
        self.contact(
            lead,
            occurred_at=datetime(2026, 1, 10, 10, tzinfo=ZoneInfo("Asia/Jakarta")),
            recorded_at=datetime(2026, 1, 10, 10, tzinfo=ZoneInfo("Asia/Jakarta")),
            followup_action="set",
            next_due_at=due,
        )
        target = self.activate(
            self.target(metric_key="followup_on_time_percent", target="100")
        )
        result = kpi.evaluate_target(target)
        count = result["basis"]["provenance"]["sources"]["leads"]["count"]
        period = create_source_period(
            organization=self.org,
            actor=self.actor,
            target=target,
            reference="FOLLOWUP-COUNT",
            inventory_note="Semua sumber janji diperiksa",
            confirm_all_sources=True,
            sources=[
                {
                    "kind": "leads",
                    "name": "Sumber prospek",
                    "reference": "LEADS-COUNT",
                    "expected_count": count,
                    "imported_count": count,
                    "duplicate_count": 0,
                    "excluded_count": 0,
                    "unresolved_count": 0,
                    "evidence": "Diperiksa",
                },
                {
                    "kind": "followups",
                    "name": "Riwayat janji",
                    "reference": "FOLLOWUPS-COUNT",
                    "expected_count": 0,
                    "imported_count": 0,
                    "duplicate_count": 0,
                    "excluded_count": 0,
                    "unresolved_count": 0,
                    "evidence": "Belum cocok",
                },
            ],
        )
        with self.assertRaisesMessage(ValidationError, "Jumlah janji follow-up"):
            approve_source_period(
                organization=self.org,
                actor=self.actor,
                period=period,
                expected_version=period.version,
                expected_fingerprint=result["basis"]["provenance"]["digest"],
                reason="Periksa ulang jumlah",
            )

    def test_response_median_needs_complete_cohort_and_ignores_late_responses(self):
        start = datetime(2026, 1, 5, 9, tzinfo=ZoneInfo("Asia/Jakarta"))
        self.lead("RESPONDED", first_response_at=start + timedelta(minutes=30))
        unanswered = self.lead("UNANSWERED")
        target = self.activate(
            self.target(metric_key="median_response_minutes", target="60")
        )
        partial = kpi.evaluate_target(target)
        self.assertEqual(partial["actual"], "30.00")
        self.assertEqual(partial["state"], "incomplete")
        self.assertIsNone(partial["attainment"])
        with self.assertRaises(ValidationError):
            self.review(target)
        unanswered = services.update_lead(
            organization=self.org,
            actor=self.actor,
            lead=unanswered,
            expected_version=unanswered.version,
            first_response_at=start + timedelta(minutes=60),
            responsibility_reason="Owner verifies source response",
        )
        result = kpi.evaluate_target(target)
        self.assertEqual((result["actual"], result["attainment"]), ("45.00", "100.00"))
        self.lead(
            "AFTER-CUTOFF",
            first_response_at=datetime(2026, 2, 1, 9, tzinfo=ZoneInfo("Asia/Jakarta")),
        )
        result = kpi.evaluate_target(target)
        self.assertEqual(result["actual"], "45.00")
        self.assertIsNone(result["attainment"])

    def test_cost_and_ratio_require_current_full_scope_evidence(self):
        invoice = self.invoice("COST")
        self.lead("COST", invoice)
        self.receipt(invoice, "25", "COST-CASH")
        ad = self.ad(spend="10")
        cost = self.activate(
            self.target(metric_key="spend", target="20", campaign=self.campaign)
        )
        ratio = self.activate(
            self.target(metric_key="cash_to_spend", target="5", campaign=self.campaign)
        )
        self.assertIsNone(kpi.evaluate_target(cost)["actual"])
        self.assertIsNone(kpi.evaluate_target(ratio)["actual"])
        self.confirm()
        cost_result, ratio_result = (
            kpi.evaluate_target(cost),
            kpi.evaluate_target(ratio),
        )
        self.assertEqual(
            (cost_result["actual"], cost_result["attainment"]), ("10.00", "100.00")
        )
        self.assertEqual(
            (ratio_result["actual"], ratio_result["attainment"]), ("2.50", "50.00")
        )
        services.save_ad_observation(
            organization=self.org,
            actor=self.actor,
            observation=ad,
            expected_version=ad.version,
            spend="12",
            reason="Correct source",
        )
        self.assertIsNone(kpi.evaluate_target(cost)["actual"])
        self.assertIsNone(kpi.evaluate_target(ratio)["actual"])
        with self.assertRaises(ValidationError):
            self.review(cost)

    def test_confirmed_zero_cost_is_known_without_fabricated_observation_or_ratio(self):
        self.confirm(declared_spend="0")
        cost = self.activate(
            self.target(metric_key="spend", target="0", campaign=self.campaign)
        )
        ratio = self.activate(
            self.target(metric_key="cash_to_spend", campaign=self.campaign)
        )
        result = kpi.evaluate_target(cost)
        self.assertEqual(
            (result["actual"], result["attainment"], result["state"]),
            ("0.00", "100.00", "met"),
        )
        self.assertIsNone(kpi.evaluate_target(ratio)["actual"])
        self.assertFalse(self.campaign.observations.exists())

    def test_rounding_cannot_turn_near_target_ratio_or_response_into_a_pass(self):
        invoice = self.invoice("PRECISION")
        received = datetime(2026, 1, 5, 9, tzinfo=ZoneInfo("Asia/Jakarta"))
        self.lead(
            "PRECISION",
            invoice,
            first_response_at=received + timedelta(minutes=10, microseconds=240000),
        )
        self.receipt(invoice, "1", "PRECISION-PAY")
        self.ad(spend="6")
        self.confirm(declared_spend="6")
        ratio = self.activate(
            self.target(
                metric_key="cash_to_spend", target="0.17", campaign=self.campaign
            )
        )
        result = kpi.evaluate_target(ratio)
        self.assertEqual(result["actual"], "0.17")
        self.assertEqual(result["state"], "sources_incomplete")
        self.assertEqual(result["attainment"], "98.03")
        response = self.activate(
            self.target(metric_key="median_response_minutes", target="10")
        )
        certify_lead_sources(self.org, self.actor, response)
        result = kpi.evaluate_target(response)
        self.assertEqual(result["actual"], "10.00")
        self.assertEqual(result["state"], "not_met")
        self.assertEqual(result["attainment"], "99.96")
        self.assertEqual(result["basis"]["unrounded_actual"], "10.004")

    def test_confirmed_zero_cost_campaign_does_not_hide_total_ratio(self):
        invoice = self.invoice("ZERO-COST-CASH")
        self.lead("ZERO-COST-CASH", invoice)
        self.receipt(invoice, "25", "ZERO-COST-PAY")
        self.confirm(declared_spend="0")
        spending = services.create_campaign(
            organization=self.org,
            actor=self.actor,
            name="Paid channel",
            channel="meta_ads",
        )
        services.save_ad_observation(
            organization=self.org,
            actor=self.actor,
            campaign=spending,
            date=date(2026, 1, 10),
            spend="10",
            source="All accounts",
            reference="ACTUAL-COST",
        )
        self.confirm(campaign=spending, reference="COVER-PAID")
        target = self.activate(
            self.target(metric_key="cash_to_spend", target="5", channel="meta_ads")
        )
        result = kpi.evaluate_target(target)
        self.assertEqual((result["actual"], result["attainment"]), ("2.50", "50.00"))
        self.assertFalse(result["official"])

    def test_multiline_rationale_preserves_business_explanation(self):
        target = self.target(
            rationale="Dasar pertama.\nAsumsi kapasitas dan kanal kedua."
        )
        self.assertIn("\n", target.rationale)

    def test_review_rejects_source_change_after_displayed_evaluation(self):
        self.lead("DISPLAYED")
        target = self.activate(self.target())
        displayed_fingerprint = kpi.evaluate_target(target)["fingerprint"]
        self.lead("ARRIVED-BEFORE-POST")
        with self.assertRaisesMessage(
            ValidationError, "Data sumber atau hasil KPI berubah"
        ):
            self.review(target, expected_fingerprint=displayed_fingerprint)
        self.assertFalse(KpiReview.objects.exists())
        target.refresh_from_db()
        self.assertEqual(target.version, 2)
        certify_lead_sources(self.org, self.actor, target)
        current = self.review(target)
        self.assertEqual(current.actual, Decimal(2))

    def test_same_total_replacement_coverage_changes_provenance_and_invalidates_review(
        self,
    ):
        self.ad(spend="10")
        old_coverage = self.confirm()
        target = self.activate(
            self.target(metric_key="spend", target="20", campaign=self.campaign)
        )
        review = self.review(target)
        target.refresh_from_db()
        before = kpi.evaluate_target(target)
        services.withdraw_source_coverage(
            organization=self.org,
            actor=self.actor,
            declaration=old_coverage,
            reason="Wrong source attachment; replace with checked evidence",
        )
        new_coverage = self.confirm(
            reference="COVER-REPLACEMENT",
            evidence="Replacement source evidence with the same total",
        )
        after = kpi.evaluate_target(target)
        self.assertEqual(
            (before["actual"], before["attainment"]),
            (after["actual"], after["attainment"]),
        )
        self.assertNotEqual(before["fingerprint"], after["fingerprint"])
        self.assertNotEqual(
            before["basis"]["provenance"]["digest"],
            after["basis"]["provenance"]["digest"],
        )
        self.assertEqual(
            after["basis"]["provenance"]["sources"]["coverage_declarations"]["records"],
            [{"id": new_coverage.pk}],
        )
        self.assertEqual(
            review.snapshot["basis"]["provenance"]["sources"]["coverage_declarations"][
                "records"
            ],
            [{"id": old_coverage.pk}],
        )
        with self.assertRaisesMessage(
            ValidationError, "Data sumber atau hasil KPI berubah"
        ):
            self.review(target, expected_fingerprint=before["fingerprint"])
        review.refresh_from_db()
        self.assertEqual(review.fingerprint, before["fingerprint"])
        self.assertEqual(KpiReview.objects.count(), 1)

    def test_same_spend_source_correction_and_reconfirmation_change_fingerprint(self):
        ad = self.ad(spend="10")
        self.confirm()
        target = self.activate(
            self.target(metric_key="spend", target="20", campaign=self.campaign)
        )
        before = kpi.evaluate_target(target)
        services.save_ad_observation(
            organization=self.org,
            actor=self.actor,
            observation=ad,
            expected_version=ad.version,
            source="Correct source account label",
            reason="Fix source identity without changing cost",
        )
        self.confirm(reference="COVER-CORRECTED")
        after = kpi.evaluate_target(target)
        self.assertEqual(after["actual"], before["actual"])
        self.assertNotEqual(after["fingerprint"], before["fingerprint"])
        sources = after["basis"]["provenance"]["sources"]
        self.assertEqual(
            sources["ad_observations"]["records"], [{"id": ad.pk, "version": 2}]
        )
        self.assertNotIn(
            "Correct source account label", str(after["basis"]["provenance"])
        )
        with self.assertRaisesMessage(
            ValidationError, "Data sumber atau hasil KPI berubah"
        ):
            self.review(target, expected_fingerprint=before["fingerprint"])
        self.assertFalse(KpiReview.objects.exists())

    def test_nonfinancial_lead_correction_changes_source_version_without_storing_private_notes(
        self,
    ):
        lead = self.lead("LEAD-SOURCE")
        target = self.activate(self.target())
        before = kpi.evaluate_target(target)
        services.update_lead(
            organization=self.org,
            actor=self.actor,
            lead=lead,
            expected_version=lead.version,
            notes="Private personal correction; should never enter the KPI snapshot",
            responsibility_reason="Owner verifies corrected source",
        )
        after = kpi.evaluate_target(target)
        self.assertEqual(after["actual"], before["actual"])
        self.assertNotEqual(after["fingerprint"], before["fingerprint"])
        self.assertEqual(
            after["basis"]["provenance"]["sources"]["leads"]["records"],
            [{"id": lead.pk, "version": 2}],
        )
        self.assertNotIn("Private personal", str(after))

    def test_draft_and_current_period_cannot_be_reviewed_as_final(self):
        self.lead("ONE")
        draft = self.target()
        self.assertFalse(kpi.evaluate_target(draft)["official"])
        self.assertIsNone(kpi.evaluate_target(draft)["attainment"])
        with self.assertRaises(ValidationError):
            self.review(draft)
        today = timezone.localdate()
        current = self.activate(self.target(period_start=today, period_end=today))
        with self.assertRaises(ValidationError):
            self.review(current)
        future = self.activate(
            self.target(
                period_start=today + timedelta(days=1),
                period_end=today + timedelta(days=2),
            )
        )
        self.assertEqual(kpi.evaluate_target(future)["state"], "upcoming")
        self.assertIsNone(kpi.evaluate_target(future)["actual"])

    def test_immutable_snapshot_and_fingerprint_retain_prior_results_after_source_changes(
        self,
    ):
        self.lead("ORIGINAL")
        target = self.activate(self.target())
        certify_lead_sources(self.org, self.actor, target)
        prior = kpi.evaluate_target(target)
        review = self.review(target)
        self.assertEqual(review.snapshot, prior)
        self.assertEqual(review.actual, Decimal(1))
        self.assertEqual(review.fingerprint, kpi.evaluate_target(target)["fingerprint"])
        self.lead("LATE-IMPORT")
        current = kpi.evaluate_target(target)
        self.assertEqual(current["actual"], "2.00")
        self.assertNotEqual(current["fingerprint"], review.fingerprint)
        review.refresh_from_db()
        self.assertEqual(review.snapshot["actual"], "1.00")
        review.conclusion = "Rewrite old finding"
        review._service_write = True
        with self.assertRaises(ValidationError):
            review.save()
        with self.assertRaises(ValidationError):
            review.delete()
        with self.assertRaises(ValidationError):
            self.review(target)  # old browser version after first review
        target.refresh_from_db()
        certify_lead_sources(self.org, self.actor, target)
        self.assertEqual(self.review(target).actual, Decimal(2))

    def test_review_is_authorized_audited_atomic_and_does_not_touch_ledger(self):
        invoice = self.invoice("READ-ONLY")
        self.lead("READ-ONLY", invoice)
        self.receipt(invoice, "25", "READ-ONLY-PAY")
        target = self.activate(self.target(metric_key="leads", target="100"))
        certify_lead_sources(self.org, self.actor, target)
        before = (
            list(Journal.objects.values()),
            list(BankTransaction.objects.values()),
        )
        for actor in (self.marketing, self.finance, self.auditor):
            with self.assertRaises(PermissionDenied):
                self.review(target, actor=actor)
        with (
            patch(
                "marketing.kpi._audit", side_effect=ValidationError("Audit unavailable")
            ),
            self.assertRaises(ValidationError),
        ):
            self.review(target)
        self.assertFalse(KpiReview.objects.exists())
        target.refresh_from_db()
        self.assertEqual(target.version, 2)
        review = self.review(target, actor=self.director)
        event = AuditEvent.objects.get(action="marketing.kpi.reviewed")
        self.assertEqual(event.actor_id, self.director.pk)
        self.assertEqual(event.detail["fingerprint"], review.fingerprint)
        self.assertEqual(
            before,
            (list(Journal.objects.values()), list(BankTransaction.objects.values())),
        )
