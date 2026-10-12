"""Public source recovery uses scoped real forms and immutable saved history."""

from datetime import date
from decimal import Decimal
from urllib.parse import urlencode

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from core.models import Membership, Organization
from finance.models import BankTransaction, Journal

from . import services
from .models import AdDailyObservation, AdObservationRecovery


@override_settings(MARKETING_AI_PRIVATE_AGGREGATES_ENABLED=False)
class AdRecoveryHTTPTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(name="Synthetic ad source recovery")
        cls.other = Organization.objects.create(name="Synthetic foreign ad sources")
        for role in (
            "owner",
            "director",
            "finance",
            "marketing",
            "auditor",
            "reviewer",
        ):
            actor = get_user_model().objects.create_user(
                username="ad-recovery-http-" + role
            )
            Membership.objects.create(organization=cls.org, user=actor, role=role)
            setattr(cls, role, actor)
        cls.foreign_actor = get_user_model().objects.create_user(
            username="ad-recovery-http-foreign"
        )
        Membership.objects.create(
            organization=cls.other, user=cls.foreign_actor, role="owner"
        )
        cls.campaign = services.create_campaign(
            organization=cls.org,
            actor=cls.owner,
            name="Synthetic January ads",
            channel="meta_ads",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 31),
        )
        cls.correct_campaign = services.create_campaign(
            organization=cls.org,
            actor=cls.owner,
            name="Synthetic corrected campaign",
            channel="google_ads",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 31),
        )
        cls.foreign_campaign = services.create_campaign(
            organization=cls.other,
            actor=cls.foreign_actor,
            name="FOREIGN CONFIDENTIAL AD CAMPAIGN",
            channel="meta_ads",
        )
        cls.foreign_ad = services.save_ad_observation(
            organization=cls.other,
            actor=cls.foreign_actor,
            campaign=cls.foreign_campaign,
            date=date(2026, 1, 10),
            spend="999999",
            source="FOREIGN PRIVATE AD SOURCE",
            reference="FOREIGN PRIVATE REFERENCE",
        )

    def setUp(self):
        self.login(self.marketing)
        response = self.client.post(
            reverse("marketing:ad_create"),
            {
                "campaign": self.campaign.pk,
                "date": "2026-01-10",
                "ad_name": "Semua iklan",
                "audience": "Semua audiens",
                "region": "Semua wilayah",
                "spend": "100000",
                "impressions": "1000",
                "clicks": "50",
                "source": "Synthetic full platform report",
                "reference": "SYNTHETIC-DOCUMENT-1",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.ad = AdDailyObservation.objects.get(
            organization=self.org, reference="SYNTHETIC-DOCUMENT-1"
        )
        self.original_creator = self.ad.created_by_id

    def login(self, actor, organization=None, client=None):
        client = client or self.client
        client.force_login(actor)
        session = client.session
        session["organization_id"] = (organization or self.org).pk
        session.save()

    def url(self, action, ad=None):
        return reverse("marketing:ad_" + action, args=[(ad or self.ad).pk])

    def payload(self, action="withdraw", ad=None, **changes):
        response = self.client.get(self.url(action, ad))
        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        values = {name: form[name].value() for name in form.fields}
        values.update(
            confirmed="on",
            reason="The actual platform report proves the original entry was wrong",
            evidence="Synthetic archived platform report; original date and campaign checked",
        )
        values.update(changes)
        return values

    def financial_identity(self):
        return (
            list(
                Journal.objects.filter(organization=self.org).values_list(
                    "pk", "source_key"
                )
            ),
            list(
                BankTransaction.objects.filter(organization=self.org).values_list(
                    "pk", "amount"
                )
            ),
        )

    def test_public_recovery_forms_have_flat_viewed_controls_and_plain_source(self):
        for action in ("withdraw", "replace"):
            response = self.client.get(self.url(action))
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "SYNTHETIC-DOCUMENT-1")
            for name in ("expected_version", "expected_fingerprint", "request_key"):
                self.assertTrue(response.context["form"][name].field.widget.is_hidden)
                self.assertTrue(response.context["form"][name].value())
            self.assertEqual(response.context["form"]["expected_version"].value(), 1)
        form = self.client.get(self.url("replace")).context["form"]
        self.assertEqual(form["date"].value(), date(2026, 1, 10))
        self.assertTrue(form.fields["reference"].disabled)

    def test_withdraw_exact_retry_keeps_history_and_changed_retry_is_visible(self):
        before = self.financial_identity()
        data = self.payload()
        self.assertEqual(self.client.post(self.url("withdraw"), data).status_code, 302)
        self.assertEqual(self.client.post(self.url("withdraw"), data).status_code, 302)
        self.ad.refresh_from_db()
        self.assertIsNotNone(self.ad.withdrawn_at)
        self.assertEqual(self.ad.spend, Decimal(100000))
        self.assertEqual(self.ad.created_by_id, self.original_creator)
        self.assertEqual(
            AdObservationRecovery.objects.filter(organization=self.org).count(), 1
        )
        failed = self.client.post(
            self.url("withdraw"),
            {**data, "reason": "A changed retry must not replace the saved decision"},
        )
        self.assertEqual(failed.status_code, 200)
        self.assertTrue(failed.context["form"].errors)
        self.assertContains(
            failed, "A changed retry must not replace the saved decision"
        )
        self.assertNotContains(failed, 'value="save"')
        self.assertEqual(self.financial_identity(), before)
        detail = self.client.get(self.url("detail"))
        self.assertContains(detail, data["reason"])
        self.assertContains(detail, "tidak menyatakan biaya sebenarnya nol")

    def test_replace_identity_retains_document_and_changes_only_one_active_source(self):
        before = self.financial_identity()
        data = self.payload(
            "replace",
            campaign=self.correct_campaign.pk,
            date="2026-01-11",
            ad_name="Actual ad A",
            audience="Actual audience",
            region="Jakarta",
            spend="125000",
            clicks="60",
            reference="FORGED-NEW-DOCUMENT",
        )
        response = self.client.post(self.url("replace"), data)
        self.assertEqual(response.status_code, 302)
        decision = AdObservationRecovery.objects.get(predecessor=self.ad)
        child = decision.successor
        self.assertEqual(response.url, self.url("detail", child))
        self.assertEqual(child.reference, "SYNTHETIC-DOCUMENT-1")
        self.assertEqual(child.source_root_id, self.ad.pk)
        self.assertEqual(child.campaign, self.correct_campaign)
        self.assertEqual(child.date, date(2026, 1, 11))
        self.assertEqual(child.ad_name, "Actual ad A")
        self.assertEqual(child.spend, Decimal(125000))
        self.ad.refresh_from_db()
        self.assertIsNotNone(self.ad.withdrawn_at)
        self.assertEqual(self.ad.spend, Decimal(100000))
        self.assertEqual(self.ad.created_by_id, self.original_creator)
        self.assertEqual(self.client.post(self.url("replace"), data).status_code, 302)
        self.assertEqual(AdObservationRecovery.objects.count(), 1)
        self.assertEqual(self.financial_identity(), before)

    def test_overlapping_replacement_is_atomic_and_keeps_inputs(self):
        services.save_ad_observation(
            organization=self.org,
            actor=self.owner,
            campaign=self.correct_campaign,
            date=date(2026, 1, 11),
            ad_name="Actual ad A",
            audience="Actual audience",
            region="Jakarta",
            spend="200000",
            source="Actual detail already imported",
            reference="SYNTHETIC-OTHER-DOCUMENT",
        )
        data = self.payload(
            "replace",
            campaign=self.correct_campaign.pk,
            date="2026-01-11",
            ad_name="Actual ad A",
            audience="Actual audience",
            region="Jakarta",
            spend="125000",
        )
        response = self.client.post(self.url("replace"), data)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors)
        self.assertEqual(response.context["form"]["spend"].value(), "125000")
        self.assertContains(response, data["reason"])
        self.ad.refresh_from_db()
        self.assertIsNone(self.ad.withdrawn_at)
        self.assertFalse(AdObservationRecovery.objects.exists())

    def test_stale_recheck_keeps_proposal_and_requires_confirmation_again(self):
        data = self.payload(
            "replace",
            campaign=self.correct_campaign.pk,
            date="2026-01-11",
            spend="125000",
        )
        services.save_ad_observation(
            organization=self.org,
            actor=self.owner,
            observation=self.ad,
            expected_version=self.ad.version,
            spend="110000",
            reason="Source amount independently corrected",
        )
        failed = self.client.post(self.url("replace"), data)
        self.assertEqual(failed.status_code, 200)
        self.assertContains(failed, "Periksa versi terbaru")
        self.assertEqual(
            failed.context["form"]["expected_version"].value(),
            str(data["expected_version"]),
        )
        self.assertEqual(failed.context["form"]["date"].value(), "2026-01-11")
        refreshed = self.client.post(self.url("replace"), {**data, "action": "recheck"})
        self.assertTrue(refreshed.context["rechecked"])
        self.assertEqual(
            str(refreshed.context["form"]["expected_version"].value()), "2"
        )
        self.assertEqual(refreshed.context["form"]["spend"].value(), "125000")
        self.assertFalse(refreshed.context["form"]["confirmed"].value())
        self.assertFalse(AdObservationRecovery.objects.exists())
        fresh = {
            name: refreshed.context["form"][name].value()
            for name in refreshed.context["form"].fields
        }
        fresh["confirmed"] = "on"
        self.assertEqual(self.client.post(self.url("replace"), fresh).status_code, 302)

    def test_current_version_wrong_fingerprint_is_rejected_without_refreshing_it(self):
        data = self.payload(expected_fingerprint="0" * 64)
        response = self.client.post(self.url("withdraw"), data)
        self.assertTrue(response.context["form"].errors)
        self.assertEqual(
            response.context["form"]["expected_fingerprint"].value(), "0" * 64
        )
        self.assertFalse(AdObservationRecovery.objects.exists())

    def test_confirmation_reason_evidence_and_real_numeric_fact_are_required(self):
        for changes in (
            {"confirmed": ""},
            {"reason": ""},
            {"evidence": ""},
            {"spend": "", "impressions": "", "clicks": ""},
            {"spend": "-1"},
        ):
            response = self.client.post(
                self.url("replace"), self.payload("replace", **changes)
            )
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context["form"].errors)
            self.assertFalse(AdObservationRecovery.objects.exists())

    def test_zero_is_a_real_value_and_unknown_metrics_stay_unknown(self):
        data = self.payload(
            "replace", date="2026-01-11", spend="0", impressions="", clicks=""
        )
        self.assertEqual(self.client.post(self.url("replace"), data).status_code, 302)
        child = AdObservationRecovery.objects.get(predecessor=self.ad).successor
        self.assertEqual(child.spend, Decimal(0))
        self.assertIsNone(child.impressions)
        self.assertIsNone(child.clicks)

    def test_auditor_drilldown_is_read_only_and_all_existing_writer_roles_can_open_forms(
        self,
    ):
        for actor in (self.owner, self.director, self.finance, self.marketing):
            self.login(actor)
            for action in ("withdraw", "replace"):
                self.assertEqual(self.client.get(self.url(action)).status_code, 200)
        self.login(self.auditor)
        response = self.client.get(self.url("detail"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, self.url("replace"))
        self.assertNotContains(response, self.url("withdraw"))
        audit = self.client.get(
            reverse("marketing:audit"),
            {"object_type": "AdDailyObservation", "object_id": self.ad.pk},
        )
        self.assertContains(audit, self.url("detail"))
        for action in ("withdraw", "replace"):
            self.assertEqual(self.client.get(self.url(action)).status_code, 403)
            self.assertEqual(self.client.post(self.url(action), {}).status_code, 403)
        self.login(self.reviewer)
        self.assertEqual(self.client.get(self.url("detail")).status_code, 403)

    def test_other_company_routes_and_campaign_choices_are_scoped(self):
        for action in ("detail", "withdraw", "replace"):
            self.assertEqual(
                self.client.get(self.url(action, self.foreign_ad)).status_code, 404
            )
            self.assertEqual(
                self.client.post(self.url(action, self.foreign_ad), {}).status_code,
                405 if action == "detail" else 404,
            )
        data = self.payload("replace", campaign=self.foreign_campaign.pk)
        failed = self.client.post(self.url("replace"), data)
        self.assertTrue(failed.context["form"].errors)
        self.assertNotContains(failed, "FOREIGN CONFIDENTIAL")
        self.assertNotContains(failed, "FOREIGN PRIVATE")
        self.assertFalse(
            AdObservationRecovery.objects.filter(organization=self.org).exists()
        )

    def test_both_mutations_require_csrf_and_get_never_writes(self):
        strict = Client(enforce_csrf_checks=True)
        self.login(self.marketing, client=strict)
        for action in ("withdraw", "replace"):
            self.assertEqual(strict.get(self.url(action)).status_code, 200)
            self.assertEqual(
                strict.post(self.url(action), self.payload(action)).status_code, 403
            )
        self.assertFalse(AdObservationRecovery.objects.exists())

    def test_duplicate_hidden_control_is_rejected_instead_of_using_last_value(self):
        data = self.payload()
        body = urlencode(
            list(data.items()) + [("expected_version", str(data["expected_version"]))]
        )
        response = self.client.post(
            self.url("withdraw"), body, content_type="application/x-www-form-urlencoded"
        )
        self.assertTrue(response.context["form"].errors)
        self.assertContains(response, "lebih dari sekali")
        self.assertFalse(AdObservationRecovery.objects.exists())

    def test_default_list_uses_active_sources_history_and_completed_get_stay_readable(
        self,
    ):
        data = self.payload()
        self.assertEqual(self.client.post(self.url("withdraw"), data).status_code, 302)
        active = self.client.get(reverse("marketing:campaigns"))
        self.assertNotContains(active, "SYNTHETIC-DOCUMENT-1")
        self.assertContains(active, "belum membuktikan biaya nol")
        history = self.client.get(reverse("marketing:campaigns"), {"history": "1"})
        self.assertContains(history, "SYNTHETIC-DOCUMENT-1")
        self.assertContains(history, self.url("detail"))
        completed = self.client.get(self.url("withdraw"))
        self.assertContains(completed, "Hasil sudah tercatat")
        self.assertNotContains(completed, 'value="save"')
        old_numeric = self.client.get(self.url("correct"))
        self.assertRedirects(old_numeric, self.url("detail"))

    def test_invalid_campaign_date_retains_original_active_source_and_input(self):
        data = self.payload("replace", date="2026-02-10")
        response = self.client.post(self.url("replace"), data)
        self.assertTrue(response.context["form"].errors)
        self.assertEqual(response.context["form"]["date"].value(), "2026-02-10")
        self.ad.refresh_from_db()
        self.assertIsNone(self.ad.withdrawn_at)
        self.assertFalse(AdObservationRecovery.objects.exists())
