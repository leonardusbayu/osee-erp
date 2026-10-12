"""Company setup signals stay read-only, scoped, and behind role permissions."""

from calendar import monthrange
from datetime import time, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import AuditEvent, DomainEvent, Membership, Organization
from finance.models import BankAccount, Invoice, Journal, Product
from marketing import kpi, services, workflow
from marketing.kpi_models import KpiTarget
from marketing.models import TeamMember
from marketing.workflow_models import WorkPolicy
from operations.models import IntakeBatch, Registration


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class OnboardingHTTPTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Synthetic onboarding company")
        self.other = Organization.objects.create(name="Other onboarding company")
        self.owner = get_user_model().objects.create_user(username="onboarding-owner")
        self.finance = get_user_model().objects.create_user(
            username="onboarding-finance"
        )
        self.marketer = get_user_model().objects.create_user(
            username="onboarding-marketer"
        )
        Membership.objects.create(organization=self.org, user=self.owner, role="owner")
        self.login(self.owner)

    def login(self, user, organization=None):
        self.client.force_login(user)
        session = self.client.session
        session["organization_id"] = (organization or self.org).pk
        session.save()

    def steps(self, response):
        self.assertEqual(response.status_code, 200)
        return {step["key"]: step for step in response.context["steps"]}

    def record_counts(self):
        models = (
            get_user_model(),
            Membership,
            AuditEvent,
            DomainEvent,
            BankAccount,
            Invoice,
            Journal,
            Product,
            KpiTarget,
            WorkPolicy,
            TeamMember,
            IntakeBatch,
            Registration,
        )
        return {model._meta.label: model._base_manager.count() for model in models}

    def policy(self, organization=None):
        return workflow.configure_work_policy(
            organization=organization or self.org,
            actor=self.owner,
            expected_version=0,
            response_limit_minutes=30,
            review_limit_minutes=240,
            workdays=[0, 1, 2, 3, 4],
            work_start=time(9),
            work_end=time(17),
            holidays=[],
            reason="Synthetic approved schedule",
        )

    def test_empty_guide_links_are_valid_and_get_or_post_never_provisions_data(self):
        before = self.record_counts()
        response = self.client.get(reverse("onboarding"))
        steps = self.steps(response)
        for key in (
            "accounts",
            "pic",
            "calendar",
            "prices",
            "registrations",
            "bank",
            "exceptions",
            "targets",
        ):
            self.assertFalse(steps[key]["recorded"], key)
        self.assertContains(response, "0 akun Finance dan 0 akun Marketing aktif.")
        self.assertEqual(response.context["next_step"]["key"], "accounts")
        self.assertEqual(
            response.context["next_action"]["url"], reverse("member_create")
        )
        self.assertContains(response, reverse("import_exception_inbox"))
        self.assertContains(response, reverse("operations:intake_upload"))
        self.assertContains(response, "Menunggu tujuan server")
        self.assertEqual(self.record_counts(), before)
        self.assertEqual(
            self.client.post(reverse("onboarding"), {"role": "owner"}).status_code, 405
        )
        self.assertEqual(self.record_counts(), before)
        self.client.logout()
        self.assertEqual(self.client.get(reverse("onboarding")).status_code, 302)

    def test_finance_reads_guide_but_marketing_cannot_access_company_preparation(self):
        Membership.objects.create(
            organization=self.org, user=self.finance, role="finance"
        )
        self.login(self.finance)
        response = self.client.get(reverse("onboarding"))
        self.assertEqual(response.status_code, 200)
        for route in (
            "member_create",
            "marketing:workflow_policy",
            "marketing:ai_policy",
            "director:ai_policy",
        ):
            self.assertNotContains(response, f'href="{reverse(route)}"')
        self.assertContains(response, "Tambah akun staf — oleh Owner")
        self.assertIsNone(response.context["next_action"])
        Membership.objects.create(
            organization=self.org, user=self.marketer, role="marketing"
        )
        self.login(self.marketer)
        self.assertEqual(self.client.get(reverse("onboarding")).status_code, 403)

    def test_only_selected_company_records_count_and_revoked_mappings_need_attention(
        self,
    ):
        Membership.objects.create(
            organization=self.other, user=self.owner, role="owner"
        )
        Membership.objects.create(
            organization=self.other, user=self.finance, role="finance"
        )
        Membership.objects.create(
            organization=self.other, user=self.marketer, role="marketing"
        )
        self.policy(self.other)
        Product.objects.create(
            organization=self.other,
            code="OTHER",
            name="Private other product",
            kind="itp",
            default_price=500000,
        )
        response = self.client.get(reverse("onboarding"))
        steps = self.steps(response)
        for key in ("accounts", "calendar", "prices"):
            self.assertFalse(steps[key]["recorded"], key)
        self.assertNotContains(response, "Private other product")
        Membership.objects.create(
            organization=self.org, user=self.finance, role="finance"
        )
        membership = Membership.objects.create(
            organization=self.org, user=self.marketer, role="marketing"
        )
        member = services.create_member(
            organization=self.org,
            actor=self.owner,
            name="Synthetic PIC",
            discipline="sales",
        )
        workflow.configure_member(
            organization=self.org,
            actor=self.owner,
            member=member,
            expected_version=1,
            account=self.marketer,
            active=True,
            reason="Synthetic account mapping",
        )
        membership.is_active = False
        membership.save(update_fields=["is_active"])
        steps = self.steps(self.client.get(reverse("onboarding")))
        self.assertFalse(steps["pic"]["recorded"])
        self.assertIn("1 profil belum terhubung ke akun aktif", steps["pic"]["detail"])
        self.assertIn("0 akun Marketing", steps["accounts"]["detail"])

    def test_existing_approved_calendar_and_balanced_targets_are_shown_without_official_results(
        self,
    ):
        Membership.objects.create(
            organization=self.org, user=self.finance, role="finance"
        )
        Membership.objects.create(
            organization=self.org, user=self.marketer, role="marketing"
        )
        member = services.create_member(
            organization=self.org,
            actor=self.owner,
            name="Synthetic PIC",
            discipline="sales",
        )
        workflow.configure_member(
            organization=self.org,
            actor=self.owner,
            member=member,
            expected_version=1,
            account=self.marketer,
            active=True,
            reason="Synthetic account mapping",
        )
        self.policy()
        today = timezone.localdate()
        for metric, value in (
            ("leads", 20),
            ("spend", 1000000),
            ("median_response_minutes", 30),
        ):
            target = kpi.create_target(
                organization=self.org,
                actor=self.owner,
                title=f"Synthetic {metric}",
                metric_key=metric,
                target=value,
                period_start=today.replace(day=1),
                period_end=today.replace(day=monthrange(today.year, today.month)[1]),
                rationale="Synthetic target for this test",
            )
            kpi.activate_target(
                organization=self.org,
                actor=self.owner,
                target=target,
                expected_version=1,
                reason="Synthetic approval",
            )
        before = self.record_counts()
        response = self.client.get(reverse("onboarding"))
        steps = self.steps(response)
        for key in ("accounts", "pic", "calendar", "targets"):
            self.assertTrue(steps[key]["recorded"], key)
        self.assertFalse(steps["bank"]["recorded"])
        self.assertFalse(steps["registrations"]["recorded"])
        previous_end = today.replace(day=1) - timedelta(days=1)
        self.assertIn(f"{previous_end:%d/%m/%Y}", steps["bank"]["detail"])
        self.assertContains(response, "sebelum evaluasi resmi")
        self.assertEqual(self.record_counts(), before)
