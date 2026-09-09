from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.utils import timezone

from core.models import AuditEvent, Membership, Organization
from finance.models import BankTransaction, Invoice, Journal, Party, Product
from . import services
from .forms import CampaignForm, InvoiceLinkForm, LeadForm, LeadUpdateForm
from .models import AdDailyObservation, Campaign, Lead, LeadStageEvent, MarketingAction, MarketingAIPolicy, TeamMember


class MarketingServiceTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Marketing service tests")
        self.other = Organization.objects.create(name="Other company")
        for role in ("owner", "director", "finance", "marketing", "auditor"):
            user = get_user_model().objects.create_user(username=f"marketing-service-{role}")
            Membership.objects.create(organization=self.org, user=user, role=role)
            setattr(self, role, user)
        self.product = Product.objects.create(organization=self.org, code="M-ITP", name="ITP", kind="itp", default_price="100")
        self.party = Party.objects.create(organization=self.org, name="Pembeli", kind="customer")
        self.received = timezone.now() - timedelta(days=2)
        self.day = timezone.localdate()

    def member(self, **fields):
        return services.create_member(organization=self.org, actor=self.marketing, **{"name": "Anggota", "discipline": "sales", **fields})

    def campaign(self, **fields):
        return services.create_campaign(organization=self.org, actor=self.marketing, **{"name": "Kampanye ITP", "channel": "meta_ads", "product": self.product, **fields})

    def lead(self, **fields):
        return services.create_lead(organization=self.org, actor=self.marketing, **{"reference": "LEAD-1", "name": "Prospek", "received_at": self.received, "channel": "meta_ads", "product": self.product, "party": self.party, **fields})

    def invoice(self, **fields):
        values = {"organization": self.org, "number": "INV-M-1", "party": self.party, "product": self.product, "quantity": 1, "unit_price": "100", "date": self.day, "due_date": self.day, "service_date": self.day, "status": "issued", **fields}
        obj = Invoice(**values)
        obj._service_transition = True
        obj.save()
        return obj

    def ad(self, campaign, **fields):
        return services.save_ad_observation(organization=self.org, actor=self.marketing, **{"campaign": campaign, "date": self.day, "spend": "100.00", "source": "Laporan platform", "reference": "AD-1", **fields})

    def test_active_membership_and_allowed_fields_are_enforced(self):
        for actor in (self.auditor, None):
            with self.assertRaises(PermissionDenied):
                services.create_member(organization=self.org, actor=actor, name="No", discipline="sales")
        self.marketing.is_active = False
        self.marketing.save(update_fields=["is_active"])
        with self.assertRaises(PermissionDenied):
            self.member()
        with self.assertRaises(ValidationError):
            services.create_member(organization=self.org, actor=self.owner, name="No", discipline="sales", created_by=self.owner)
        self.assertEqual(TeamMember.objects.count(), 0)

    def test_scoped_references_and_origin_dimensions_are_preserved(self):
        foreign = Product.objects.create(organization=self.other, code="OTHER", name="Other", kind="itp", default_price="100")
        with self.assertRaises(ValidationError):
            self.campaign(product=foreign)
        member = self.member(active=False)
        with self.assertRaises(ValidationError):
            self.campaign(owner=member)
        campaign = self.campaign()
        lead = self.lead(campaign=campaign, channel="", product=None)
        self.assertEqual((lead.channel, lead.product_id), ("meta_ads", self.product.pk))
        with self.assertRaises(ValidationError):
            services.update_lead(organization=self.org, actor=self.owner, lead=lead, expected_version=1, channel="google_ads")
        campaign.channel = "google_ads"
        with self.assertRaises(ValidationError):
            campaign.save()
        self.assertEqual(Campaign.objects.get(pk=campaign.pk).channel, "meta_ads")

    def test_ad_unknown_zero_replay_correction_and_finance_isolation(self):
        campaign = self.campaign()
        ad = self.ad(campaign, impressions=None, clicks=0)
        self.assertIsNone(ad.impressions)
        self.assertEqual(ad.clicks, 0)
        audit_count = AuditEvent.objects.count()
        self.assertEqual(self.ad(campaign, impressions=None, clicks=0).pk, ad.pk)
        self.assertEqual(AuditEvent.objects.count(), audit_count)
        with self.assertRaises(ValidationError):
            self.ad(campaign, spend="101")
        ad = services.save_ad_observation(organization=self.org, actor=self.marketing, observation=ad, expected_version=1, spend="101", reason="Revisi laporan sumber")
        self.assertEqual(ad.version, 2)
        event = AuditEvent.objects.get(action="marketing.ad.corrected")
        self.assertEqual(event.detail["before"]["spend"], "100.00")
        self.assertEqual(event.detail["after"]["spend"], "101.00")
        with self.assertRaises(ValidationError):
            services.save_ad_observation(organization=self.org, actor=self.marketing, observation=ad, expected_version=1, spend="102", reason="Lama")
        self.assertFalse(Invoice.objects.exists())
        self.assertFalse(Journal.objects.exists())
        self.assertFalse(BankTransaction.objects.exists())

    def test_ad_grain_rejects_total_detail_overlap_in_both_directions(self):
        campaign = self.campaign()
        self.ad(campaign)
        with self.assertRaises(ValidationError):
            self.ad(campaign, reference="DETAIL", ad_name="A")
        detail_campaign = self.campaign(name="Rincian")
        self.ad(detail_campaign, reference="DETAIL-A", ad_name="A")
        self.ad(detail_campaign, reference="DETAIL-B", ad_name="B")
        with self.assertRaises(ValidationError):
            self.ad(detail_campaign, reference="TOTAL")
        with self.assertRaises(ValidationError):
            self.ad(detail_campaign, reference="AUDIENCE", ad_name="A", audience="Pelajar")
        self.assertEqual(AdDailyObservation.objects.count(), 3)

    def test_invalid_money_counts_and_campaign_dates_do_not_write(self):
        campaign = self.campaign(start_date=self.day, end_date=self.day)
        for spend in (float("nan"), True, -1, "NaN", "Infinity", "1.001"):
            with self.subTest(spend=spend), self.assertRaises(ValidationError):
                self.ad(campaign, spend=spend)
        for clicks in (True, -1, 1.2, "1"):
            with self.subTest(clicks=clicks), self.assertRaises(ValidationError):
                self.ad(campaign, clicks=clicks)
        with self.assertRaises(ValidationError):
            self.ad(campaign, spend=None)
        with self.assertRaises(ValidationError):
            self.ad(campaign, date=self.day - timedelta(days=1))
        self.assertFalse(AdDailyObservation.objects.exists())

    def test_stage_history_response_invariants_and_optimistic_version(self):
        lead = self.lead()
        lead = services.update_lead(organization=self.org, actor=self.marketing, lead=lead, expected_version=1, stage="contacted", first_response_at=self.received + timedelta(hours=1))
        self.assertEqual(lead.version, 2)
        self.assertEqual(list(lead.stage_events.values_list("from_stage", "to_stage")), [("", "new"), ("new", "contacted")])
        with self.assertRaises(ValidationError):
            services.update_lead(organization=self.org, actor=self.marketing, lead=lead, expected_version=1, stage="won")
        with self.assertRaises(ValidationError):
            services.update_lead(organization=self.org, actor=self.marketing, lead=lead, expected_version=2, first_response_at=self.received - timedelta(seconds=1))
        lead.refresh_from_db()
        self.assertEqual(lead.stage, "contacted")
        self.assertEqual(lead.stage_events.count(), 2)
        with self.assertRaises(ValidationError):
            self.lead(reference="NAIVE", received_at=self.received.replace(tzinfo=None))
        with self.assertRaises(ValidationError):
            self.lead(reference="FUTURE", received_at=timezone.now() + timedelta(days=1))

    def test_stage_and_audit_rollback_if_history_write_fails(self):
        lead = self.lead()
        before_audits = AuditEvent.objects.count()
        with patch("marketing.services._save", wraps=services._save) as save:
            def fail_history(obj):
                if isinstance(obj, LeadStageEvent):
                    raise ValidationError("History unavailable")
                obj._service_write = True
                obj.save()
                return obj
            save.side_effect = fail_history
            with self.assertRaises(ValidationError):
                services.update_lead(organization=self.org, actor=self.marketing, lead=lead, expected_version=1, stage="contacted")
        lead.refresh_from_db()
        self.assertEqual((lead.stage, lead.version), ("new", 1))
        self.assertEqual(AuditEvent.objects.count(), before_audits)

    def test_finance_link_checks_role_party_product_status_and_duplicate(self):
        lead = self.lead()
        invoice = self.invoice()
        with self.assertRaises(PermissionDenied):
            services.link_invoice(organization=self.org, actor=self.marketing, lead=lead, invoice=invoice, expected_version=1)
        wrong_party = Party.objects.create(organization=self.org, name="Lain", kind="customer")
        wrong = self.invoice(number="WRONG", party=wrong_party)
        with self.assertRaises(ValidationError):
            services.link_invoice(organization=self.org, actor=self.finance, lead=lead, invoice=wrong, expected_version=1)
        draft = self.invoice(number="DRAFT", status="draft")
        with self.assertRaises(ValidationError):
            services.link_invoice(organization=self.org, actor=self.finance, lead=lead, invoice=draft, expected_version=1)
        journal_count = Journal.objects.count()
        linked = services.link_invoice(organization=self.org, actor=self.finance, lead=lead, invoice=invoice, expected_version=1)
        self.assertEqual(linked.invoice_id, invoice.pk)
        self.assertEqual(linked.stage, "new")
        other_lead = self.lead(reference="LEAD-2")
        with self.assertRaises(ValidationError):
            services.link_invoice(organization=self.org, actor=self.finance, lead=other_lead, invoice=invoice, expected_version=1)
        self.assertEqual(Journal.objects.count(), journal_count)
        self.assertFalse(BankTransaction.objects.exists())

    def test_finance_link_rejects_unknown_identity_and_prior_invoice(self):
        invoice = self.invoice()
        unknown = self.lead(party=None)
        with self.assertRaises(ValidationError):
            services.link_invoice(organization=self.org, actor=self.finance, lead=unknown, invoice=invoice, expected_version=1)
        unknown = services.link_invoice(organization=self.org, actor=self.finance, lead=unknown, invoice=invoice, expected_version=1, confirm_identity=True)
        self.assertEqual(unknown.party_id, invoice.party_id)
        event = AuditEvent.objects.get(action="marketing.lead.invoice_linked")
        self.assertIsNone(event.detail["before_mapping"]["party_id"])
        self.assertEqual(event.detail["after_mapping"]["party_id"], invoice.party_id)
        known = self.lead(reference="KNOWN")
        prior = self.invoice(number="PRIOR", date=timezone.localdate(self.received) - timedelta(days=1))
        with self.assertRaises(ValidationError):
            services.link_invoice(organization=self.org, actor=self.finance, lead=known, invoice=prior, expected_version=1)

    def test_action_completion_requires_outcome_and_records_version(self):
        action = services.create_action(organization=self.org, actor=self.marketing, title="Tindak lanjuti prospek", description="Hubungi prospek yang meminta penawaran", owner=self.member())
        with self.assertRaises(ValidationError):
            services.update_action(organization=self.org, actor=self.marketing, action=action, expected_version=1, status="done")
        action = services.update_action(organization=self.org, actor=self.marketing, action=action, expected_version=1, status="done", outcome="Tiga prospek merespons; cash-in belum terhubung")
        self.assertEqual(action.version, 2)
        with self.assertRaises(ValidationError):
            services.update_action(organization=self.org, actor=self.marketing, action=action, expected_version=1, status="open")
        self.assertEqual(AuditEvent.objects.filter(action="marketing.action.updated").count(), 1)

    def test_direct_and_bulk_mutations_cannot_bypass_services(self):
        with self.assertRaises(ValidationError):
            TeamMember.objects.create(organization=self.org, created_by=self.owner, name="Bypass", discipline="sales")
        lead = self.lead()
        with self.assertRaises(ValidationError):
            Lead.objects.filter(pk=lead.pk).update(stage="won")
        with self.assertRaises(ValidationError):
            lead.delete()

    def test_form_choices_are_scoped_and_campaign_derives_lead_product(self):
        foreign = Product.objects.create(organization=self.other, code="OTHER", name="Other", kind="itp", default_price="100")
        form = CampaignForm(organization=self.org)
        self.assertNotIn(foreign, form.fields["product"].queryset)
        campaign = self.campaign()
        form = LeadForm(organization=self.org, data={"reference": "FORM", "name": "Prospek", "received_at": timezone.localtime(self.received).strftime("%Y-%m-%dT%H:%M"), "campaign": campaign.pk, "segment": "direct", "region": "Jakarta", "stage": "new"})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["product"], self.product)
        self.assertEqual(form.cleaned_data["channel"], campaign.channel)

    def test_ai_policy_is_separate_owner_controlled_and_versioned(self):
        from .ai import DISCLOSURE_VERSION
        for actor in (self.marketing, self.director, self.finance, self.auditor):
            with self.subTest(actor=actor.username), self.assertRaises(PermissionDenied):
                services.configure_marketing_ai_policy(organization=self.org, actor=actor, enabled=True, monthly_budget_usd="10", request_cap_usd="0.02")
        for monthly, cap in (("0", "0.02"), ("10", "0"), ("1", "2"), ("NaN", "1"), ("10", "0.0000001")):
            with self.subTest(monthly=monthly, cap=cap), self.assertRaises(ValidationError):
                services.configure_marketing_ai_policy(organization=self.org, actor=self.owner, enabled=True, monthly_budget_usd=monthly, request_cap_usd=cap)
        self.assertFalse(MarketingAIPolicy.objects.exists())
        policy = services.configure_marketing_ai_policy(organization=self.org, actor=self.owner, enabled=True, monthly_budget_usd="10", request_cap_usd="0.02")
        self.assertEqual(policy.disclosure_version, DISCLOSURE_VERSION)
        self.assertEqual(policy.approved_by, self.owner)
        self.assertEqual(policy.request_cap_usd, Decimal("0.020000"))
        policy = services.configure_marketing_ai_policy(organization=self.org, actor=self.owner, enabled=False, monthly_budget_usd="0", request_cap_usd="0")
        self.assertEqual(policy.policy_version, 2)
        self.assertFalse(policy.enabled)
        self.assertEqual(AuditEvent.objects.filter(action="marketing.ai.policy_changed").count(), 2)

    def test_cross_company_lead_and_invoice_references_are_rejected(self):
        lead = self.lead()
        other_party = Party.objects.create(organization=self.other, name="Other", kind="customer")
        other_product = Product.objects.create(organization=self.other, code="X", name="Other", kind="itp", default_price="100")
        other_invoice = self.invoice(organization=self.other, party=other_party, product=other_product)
        with self.assertRaises(ValidationError):
            services.link_invoice(organization=self.org, actor=self.finance, lead=lead, invoice=other_invoice, expected_version=1)
        with self.assertRaises(PermissionDenied):
            services.update_lead(organization=self.other, actor=self.finance, lead=lead, expected_version=1, stage="won")
        with self.assertRaises(ValidationError):
            self.lead(reference="OTHER-PARTY", party=other_party)
        lead.refresh_from_db()
        self.assertIsNone(lead.invoice_id)

    def test_replay_missing_required_campaign_is_a_validation_error(self):
        self.ad(self.campaign())
        with self.assertRaises(ValidationError):
            services.save_ad_observation(organization=self.org, actor=self.marketing, date=self.day, spend="100", source="Laporan platform", reference="AD-1")
        self.assertEqual(AdDailyObservation.objects.count(), 1)

    def test_response_corrections_retain_audit_and_form_preserves_seconds(self):
        first = (self.received + timedelta(hours=1)).replace(second=37, microsecond=0)
        lead = self.lead(stage="contacted", first_response_at=first)
        form = LeadUpdateForm(organization=self.org, instance=lead)
        self.assertIn(timezone.localtime(first).strftime("%Y-%m-%dT%H:%M:%S"), str(form["first_response_at"]))
        corrected = first + timedelta(minutes=5)
        lead = services.update_lead(organization=self.org, actor=self.marketing, lead=lead, expected_version=1, first_response_at=corrected)
        event = AuditEvent.objects.get(action="marketing.lead.updated")
        self.assertEqual(event.detail["before"]["first_response_at"], str(first))
        self.assertEqual(event.detail["after"]["first_response_at"], str(corrected))
        self.assertEqual(lead.stage_events.count(), 1)

    def test_lost_and_won_reopening_is_operational_history_not_payment(self):
        lead = self.lead()
        for stage in ("lost", "contacted", "won", "qualified"):
            lead = services.update_lead(organization=self.org, actor=self.marketing, lead=lead, expected_version=lead.version, stage=stage, notes="Hasil follow-up dan perubahan kebutuhan")
        self.assertEqual(list(lead.stage_events.values_list("to_stage", flat=True)), ["new", "lost", "contacted", "won", "qualified"])
        self.assertIsNone(lead.invoice_id)
        self.assertFalse(BankTransaction.objects.exists())
        self.assertFalse(Journal.objects.exists())
