"""HTTP workflows use real scoped forms, services, and templates with synthetic data."""
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from core.models import Membership, Organization
from finance.models import Party, Product
from finance.services import create_invoice, issue_invoice
from . import services
from .models import AdDailyObservation, Lead, MarketingAction, MarketingAIPolicy


PERIOD = {"start": "2026-01-01", "end": "2026-01-31"}
DAY = datetime(2026, 1, 5, 9, tzinfo=ZoneInfo("Asia/Jakarta"))


@override_settings(MARKETING_AI_PRIVATE_AGGREGATES_ENABLED=False)
class MarketingHTTPTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(name="Synthetic Marketing Workspace")
        cls.other = Organization.objects.create(name="Other synthetic workspace")
        for role in ("owner", "director", "finance", "marketing", "auditor", "reviewer"):
            actor = get_user_model().objects.create_user(username="marketing-http-" + role)
            Membership.objects.create(organization=cls.org, user=actor, role=role)
            setattr(cls, role, actor)
        cls.outsider = get_user_model().objects.create_user(username="marketing-http-outsider")
        Membership.objects.create(organization=cls.other, user=cls.outsider, role="owner")
        cls.member = services.create_member(organization=cls.org, actor=cls.owner, name="Synthetic team member", discipline="sales")
        cls.campaign = services.create_campaign(organization=cls.org, actor=cls.owner, name="Synthetic January campaign", channel="meta_ads", owner=cls.member)
        cls.lead = services.create_lead(organization=cls.org, actor=cls.owner, name="Synthetic prospect", reference="LEAD-1", received_at=DAY, campaign=cls.campaign, owner=cls.member)
        cls.action = services.create_action(organization=cls.org, actor=cls.owner, title="Review response process", description="Review the synthetic lead queue", owner=cls.member, due_date=date(2026, 1, 15))
        cls.foreign_member = services.create_member(organization=cls.other, actor=cls.outsider, name="FOREIGN CONFIDENTIAL MEMBER", discipline="sales")
        cls.foreign_campaign = services.create_campaign(organization=cls.other, actor=cls.outsider, name="FOREIGN CONFIDENTIAL CAMPAIGN", channel="meta_ads")
        cls.foreign_lead = services.create_lead(organization=cls.other, actor=cls.outsider, name="FOREIGN CONFIDENTIAL LEAD", reference="OTHER-1", received_at=DAY, campaign=cls.foreign_campaign)
        cls.foreign_action = services.create_action(organization=cls.other, actor=cls.outsider, title="FOREIGN CONFIDENTIAL ACTION", description="Private")

    def setUp(self):
        self.login(self.owner)
        network = patch("marketing.ai.urlopen", side_effect=AssertionError("HTTP tests must stay local"))
        self.network = network.start()
        self.addCleanup(network.stop)

    def login(self, actor, organization=None, client=None):
        client = client or self.client
        client.force_login(actor)
        session = client.session
        session["organization_id"] = (organization or self.org).pk
        session.save()

    def test_all_real_pages_render_and_do_not_expose_other_company(self):
        names = ("overview", "analysis", "leads", "lead_create", "campaigns", "campaign_create", "ad_create",
                 "ad_import", "members", "member_create", "advisor", "action_create", "ai_policy")
        for name in names:
            with self.subTest(page=name):
                response = self.client.get(reverse("marketing:" + name), PERIOD)
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, "FOREIGN CONFIDENTIAL")
                self.assertContains(response, "Marketing")
        for name, obj in (("lead_detail", self.lead), ("action_detail", self.action)):
            response = self.client.get(reverse("marketing:" + name, args=[obj.pk]))
            self.assertEqual(response.status_code, 200)
        self.network.assert_not_called()

    def test_all_dimensions_and_valid_scoped_filters_render(self):
        for dimension in ("channel", "campaign", "member", "product", "segment", "region", "ad"):
            response = self.client.get(reverse("marketing:analysis"), {**PERIOD, "group_by": dimension})
            self.assertEqual(response.status_code, 200)
        result = self.client.get(reverse("marketing:analysis"), {**PERIOD, "campaign": self.campaign.pk, "member": self.member.pk})
        self.assertEqual(result.context["snapshot"]["summary"]["leads"], 1)
        self.assertContains(result, "2 filter aktif")

    def test_invalid_dates_dimensions_filters_and_pages_are_400(self):
        for values in ({"start": "not-a-date"}, {"start": "2026-02-01", "end": "2026-01-01"},
                       {"group_by": "sql"}, {"channel": "unknown"}, {"campaign": self.foreign_campaign.pk},
                       {"member": self.foreign_member.pk}, {"member": "x"}, {"region": "x" * 121}):
            with self.subTest(values=values):
                self.assertEqual(self.client.get(reverse("marketing:analysis"), {**PERIOD, **values}).status_code, 400)
        for values in ({"page": "bad"}, {"page": "0"}, {"stage": "fake"}, {"due": "fake"}, {"q": "x" * 101}):
            self.assertEqual(self.client.get(reverse("marketing:leads"), values).status_code, 400)

    def test_authentication_tenant_scope_and_reviewer_denial(self):
        url = reverse("marketing:overview")
        self.assertEqual(Client().get(url).status_code, 302)
        for name, obj in (("lead_detail", self.foreign_lead), ("action_detail", self.foreign_action)):
            self.assertEqual(self.client.get(reverse("marketing:" + name, args=[obj.pk])).status_code, 404)
        self.login(self.owner, organization=self.other)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.login(self.reviewer)
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_marketing_has_operational_access_without_finance_link_or_owner_policy(self):
        self.login(self.marketing)
        for name in ("overview", "analysis", "leads", "campaigns", "members", "advisor", "lead_create"):
            self.assertEqual(self.client.get(reverse("marketing:" + name), PERIOD).status_code, 200)
        detail = self.client.get(reverse("marketing:lead_detail", args=[self.lead.pk]))
        self.assertNotContains(detail, "Hubungkan invoice")
        self.assertEqual(self.client.post(reverse("marketing:lead_detail", args=[self.lead.pk]), {"operation": "link_invoice"}).status_code, 403)
        self.assertEqual(self.client.get(reverse("marketing:ai_policy")).status_code, 403)
        self.assertEqual(self.client.get(reverse("sales")).status_code, 403)

    def test_auditor_can_read_but_every_write_is_denied(self):
        self.login(self.auditor)
        for name in ("overview", "analysis", "leads", "campaigns", "members", "advisor"):
            self.assertEqual(self.client.get(reverse("marketing:" + name), PERIOD).status_code, 200)
        for name in ("lead_create", "campaign_create", "ad_create", "ad_import", "member_create", "action_create", "advisor", "ai_policy"):
            self.assertEqual(self.client.post(reverse("marketing:" + name), {}).status_code, 403)
        for name, obj in (("lead_detail", self.lead), ("action_detail", self.action)):
            self.assertEqual(self.client.post(reverse("marketing:" + name, args=[obj.pk]), {}).status_code, 403)

    def test_create_lead_then_follow_up_rejects_stale_version(self):
        self.login(self.marketing)
        data = {"name": "Synthetic new prospect", "reference": "LEAD-2", "received_at": "2026-01-06T09:00",
                "campaign": self.campaign.pk, "channel": "meta_ads", "owner": self.member.pk,
                "segment": "direct", "region": "Jakarta", "stage": "new", "notes": "Needs schedule"}
        response = self.client.post(reverse("marketing:lead_create"), data)
        self.assertEqual(response.status_code, 302)
        lead = Lead.objects.get(reference="LEAD-2")
        update = {"operation": "update", "expected_version": lead.version, "stage": "contacted",
                  "first_response_at": "2026-01-06T09:15", "next_follow_up": "2026-01-08", "notes": "Sent schedule"}
        url = reverse("marketing:lead_detail", args=[lead.pk])
        self.assertEqual(self.client.post(url, update).status_code, 302)
        lead.refresh_from_db()
        self.assertEqual(lead.stage, "contacted")
        self.assertEqual(lead.version, 2)
        response = self.client.post(url, {**update, "notes": "Stale overwrite"})
        self.assertEqual(response.status_code, 200)
        lead.refresh_from_db()
        self.assertEqual(lead.notes, "Sent schedule")

    def test_foreign_form_references_do_not_create_records(self):
        before = Lead.objects.count()
        response = self.client.post(reverse("marketing:lead_create"), {"name": "Synthetic invalid", "reference": "INVALID",
            "received_at": "2026-01-06T09:00", "campaign": self.foreign_campaign.pk, "owner": self.foreign_member.pk,
            "channel": "meta_ads", "segment": "direct", "region": "Jakarta", "stage": "new"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Lead.objects.count(), before)
        self.assertTrue(response.context["form"].errors)

    def test_verified_recommendation_creates_owned_action_and_results_require_evidence(self):
        response = self.client.post(reverse("marketing:advisor"), {**PERIOD, "question": "Apa prioritas cash-in?"})
        self.assertEqual(response.status_code, 200)
        advice = response.context["advice"]
        self.assertEqual(advice["mode"], "local")
        rec = advice["recommendations"][0]
        token = rec["action_token"]
        review = self.client.get(reverse("marketing:action_create"), {"recommendation_token": token})
        self.assertEqual(review.status_code, 200)
        created = self.client.post(reverse("marketing:action_create"), {"recommendation_token": token,
            "title": "FORGED TITLE", "evidence": "FORGED EVIDENCE", "description": "FORGED ACTION", "metric_key": "profit",
            "owner": self.member.pk, "due_date": "2026-01-20"})
        self.assertEqual(created.status_code, 302)
        action = MarketingAction.objects.order_by("-pk").first()
        self.assertEqual(action.title, rec["title"])
        self.assertNotIn("FORGED", action.evidence)
        self.assertEqual(action.owner, self.member)
        url = reverse("marketing:action_detail", args=[action.pk])
        invalid = self.client.post(url, {"expected_version": action.version, "status": "done", "outcome": ""})
        self.assertEqual(invalid.status_code, 200)
        action.refresh_from_db()
        self.assertEqual(action.status, "open")
        done = self.client.post(url, {"expected_version": action.version, "status": "done", "outcome": "Reviewed the queue and assigned responses; payment impact not yet observed."})
        self.assertEqual(done.status_code, 302)
        action.refresh_from_db()
        self.assertEqual(action.status, "done")
        self.network.assert_not_called()

    def test_recommendation_tokens_reject_tampering_and_cross_tenant_use(self):
        response = self.client.get(reverse("marketing:advisor"), PERIOD)
        token = response.context["advice"]["recommendations"][0]["action_token"]
        self.assertEqual(self.client.get(reverse("marketing:action_create"), {"recommendation_token": token + "x"}).status_code, 400)
        self.login(self.outsider, organization=self.other)
        self.assertEqual(self.client.get(reverse("marketing:action_create"), {"recommendation_token": token}).status_code, 400)

    def test_owner_policy_is_separate_and_requires_positive_limits_when_enabled(self):
        url = reverse("marketing:ai_policy")
        bad = self.client.post(url, {"enabled": "on", "monthly_budget_usd": "0", "request_cap_usd": "0"})
        self.assertEqual(bad.status_code, 200)
        self.assertFalse(MarketingAIPolicy.objects.exists())
        good = self.client.post(url, {"enabled": "on", "monthly_budget_usd": "1", "request_cap_usd": "0.01"})
        self.assertEqual(good.status_code, 302)
        self.assertTrue(MarketingAIPolicy.objects.get(organization=self.org).enabled)
        self.network.assert_not_called()

    def test_finance_can_confirm_identity_and_link_invoice_without_marking_paid(self):
        party = Party.objects.create(organization=self.org, name="Synthetic buyer", kind="customer")
        product = Product.objects.create(organization=self.org, code="SYN-ITP", name="Synthetic test", kind="itp", default_price=Decimal("100"))
        invoice = create_invoice(organization=self.org, actor=self.owner, party=party, product=product,
                                 quantity=1, date=date(2026, 1, 7), service_date=date(2026, 1, 7), number="SYN-INVOICE")
        invoice = issue_invoice(organization=self.org, actor=self.owner, invoice=invoice)
        self.login(self.finance)
        url = reverse("marketing:lead_detail", args=[self.lead.pk])
        response = self.client.post(url, {"operation": "link_invoice", "expected_version": self.lead.version,
                                         "invoice": invoice.pk, "confirm_identity": "on"})
        self.assertEqual(response.status_code, 302)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.invoice, invoice)
        self.assertEqual(self.lead.stage, "new")
        rendered = self.client.get(url)
        self.assertContains(rendered, "SYN-INVOICE")

    def test_csrf_is_required_for_operational_writes(self):
        client = Client(enforce_csrf_checks=True)
        self.login(self.marketing, client=client)
        self.assertEqual(client.post(reverse("marketing:member_create"), {"name": "Synthetic", "discipline": "sales"}).status_code, 403)

    def test_csv_template_and_import_form_work(self):
        template = self.client.get(reverse("marketing:ad_template"))
        self.assertEqual(template.status_code, 200)
        self.assertIn("attachment", template["Content-Disposition"])
        file = SimpleUploadedFile("ads.csv", "tanggal,iklan,audiens,wilayah,biaya,tayangan,klik,sumber,referensi\n2026-01-10,Iklan A,Audiens A,Jakarta,10000,500,25,Laporan sintetis,IMPORT-1\n".encode("utf-8"), content_type="text/csv")
        response = self.client.post(reverse("marketing:ad_import"), {"campaign": self.campaign.pk, "file": file})
        self.assertEqual(response.status_code, 302)

    def test_ads_correction_preserves_source_identity_and_rejects_stale_version(self):
        ad = services.save_ad_observation(organization=self.org, actor=self.owner, campaign=self.campaign,
            date=date(2026, 1, 10), spend="100", impressions=500, clicks=10, source="Synthetic source", reference="CORRECT-1")
        url = reverse("marketing:ad_correct", args=[ad.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        data = {"expected_version": ad.version, "spend": "120", "impressions": "500", "clicks": "12",
                "source": "Corrected synthetic source", "reason": "Source export corrected the reported spend",
                "campaign": self.foreign_campaign.pk, "reference": "FORGED-REFERENCE"}
        self.assertEqual(self.client.post(url, data).status_code, 302)
        ad.refresh_from_db()
        self.assertEqual(ad.spend, Decimal("120"))
        self.assertEqual(ad.reference, "CORRECT-1")
        self.assertEqual(ad.campaign, self.campaign)
        self.assertEqual(ad.version, 2)
        self.assertEqual(self.client.post(url, {**data, "spend": "999"}).status_code, 200)
        ad.refresh_from_db()
        self.assertEqual(ad.spend, Decimal("120"))
        self.login(self.outsider, organization=self.other)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.login(self.auditor)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(url, data).status_code, 403)

    def test_empty_cash_basis_is_explicit_and_overdue_includes_agreed_unpaid_prospects(self):
        response = self.client.get(reverse("marketing:overview"), PERIOD)
        self.assertContains(response, "Belum terhubung")
        lead = services.update_lead(organization=self.org, actor=self.owner, lead=self.lead, expected_version=self.lead.version,
                                    stage="won", next_follow_up=date(2026, 1, 10))
        result = self.client.get(reverse("marketing:leads"), {"due": "overdue"})
        self.assertContains(result, lead.name)
