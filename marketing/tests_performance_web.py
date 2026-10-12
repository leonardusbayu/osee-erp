from datetime import datetime, time, timedelta

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import Membership, Organization
from finance.models import Invoice, Journal

from . import activities, kpi, services
from .activity_models import WorkActivity
from .kpi_models import KpiReview, KpiTarget


class PerformanceWebTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(name="Performance HTTP test")
        cls.other = Organization.objects.create(name="Another company")
        for role in ("owner", "marketing", "finance", "auditor", "reviewer"):
            actor = get_user_model().objects.create_user(username=f"performance-{role}")
            Membership.objects.create(organization=cls.org, user=actor, role=role)
            setattr(cls, role, actor)
        cls.foreign = get_user_model().objects.create_user(
            username="performance-foreign"
        )
        Membership.objects.create(
            organization=cls.other, user=cls.foreign, role="owner"
        )
        cls.member = services.create_member(
            organization=cls.org, actor=cls.owner, name="Staff test", discipline="sales"
        )
        from .workflow import configure_member

        configure_member(
            organization=cls.org,
            actor=cls.owner,
            member=cls.member,
            expected_version=cls.member.version,
            account=cls.marketing,
            active=True,
            reason="Assign login",
        )
        cls.foreign_member = services.create_member(
            organization=cls.other,
            actor=cls.foreign,
            name="CONFIDENTIAL FOREIGN STAFF",
            discipline="sales",
        )
        cls.today = timezone.localdate()
        cls.start, cls.end = (
            cls.today - timedelta(days=10),
            cls.today - timedelta(days=1),
        )
        cls.target = kpi.create_target(
            organization=cls.org,
            actor=cls.marketing,
            title="Target test",
            metric_key="leads",
            target="2",
            period_start=cls.start,
            period_end=cls.end,
            member=cls.member,
            channel="sales",
            rationale="Historical workflow QA",
        )
        cls.activity = activities.create_activity(
            organization=cls.org,
            actor=cls.owner,
            title="Assignment test",
            owner=cls.member,
            channel="sales",
            kind="follow_up",
            due_date=cls.today,
            description="Follow up existing leads",
        )
        cls.foreign_target = kpi.create_target(
            organization=cls.other,
            actor=cls.foreign,
            title="CONFIDENTIAL FOREIGN KPI",
            metric_key="leads",
            target="2",
            period_start=cls.start,
            period_end=cls.end,
            rationale="Other company",
        )
        cls.foreign_activity = activities.create_activity(
            organization=cls.other,
            actor=cls.foreign,
            title="CONFIDENTIAL FOREIGN WORK",
            owner=cls.foreign_member,
            channel="sales",
            kind="sales",
            due_date=cls.today,
        )
        services.create_lead(
            organization=cls.org,
            actor=cls.marketing,
            reference="LEAD-WEB-1",
            name="QA prospect",
            channel="sales",
            received_at=timezone.make_aware(datetime.combine(cls.start, time(10))),
            owner=cls.member,
        )

    def setUp(self):
        self.login(self.owner)

    def login(self, actor):
        self.client.force_login(actor)
        session = self.client.session
        session["organization_id"] = self.org.pk
        session.save()

    def test_pages_render_for_read_roles_and_scope_every_reference(self):
        for actor in (self.owner, self.marketing, self.finance, self.auditor):
            self.login(actor)
            for name, args in (
                ("kpis", []),
                ("kpi_detail", [self.target.pk]),
                ("activities", []),
                ("activity_detail", [self.activity.pk]),
                ("overview", []),
                ("audit", []),
            ):
                with self.subTest(role=actor.username, page=name):
                    response = self.client.get(reverse("marketing:" + name, args=args))
                    self.assertEqual(response.status_code, 200)
                    self.assertNotContains(response, "CONFIDENTIAL FOREIGN")
        self.login(self.owner)
        for name in ("kpi_create", "activity_create"):
            self.assertEqual(
                self.client.get(reverse("marketing:" + name)).status_code, 200
            )
        for name, obj in (
            ("kpi_detail", self.foreign_target),
            ("activity_detail", self.foreign_activity),
        ):
            self.assertEqual(
                self.client.get(
                    reverse("marketing:" + name, args=[obj.pk])
                ).status_code,
                404,
            )
        self.assertEqual(
            self.client.get(
                reverse("marketing:kpis"), {"member": self.foreign_member.pk}
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.get(
                reverse("marketing:activities"), {"owner": self.foreign_member.pk}
            ).status_code,
            400,
        )

    def test_roles_cannot_bypass_protected_actions_or_csrf(self):
        url = reverse("marketing:kpi_detail", args=[self.target.pk])
        self.login(self.marketing)
        self.assertEqual(
            self.client.post(
                url,
                {"operation": "activate", "reason": "Forged", "expected_version": 1},
            ).status_code,
            403,
        )
        self.login(self.auditor)
        for name in ("kpi_create", "activity_create"):
            self.assertEqual(
                self.client.post(reverse("marketing:" + name), {}).status_code, 403
            )
        self.assertEqual(
            self.client.post(
                reverse("marketing:activity_detail", args=[self.activity.pk]),
                {"operation": "start"},
            ).status_code,
            403,
        )
        self.login(self.reviewer)
        self.assertEqual(self.client.get(reverse("marketing:kpis")).status_code, 403)
        self.assertEqual(Client().get(url).status_code, 302)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        self.assertEqual(
            client.post(
                url,
                {
                    "operation": "activate",
                    "expected_version": 1,
                    "reason": "Missing CSRF",
                },
            ).status_code,
            403,
        )

    def test_kpi_approval_review_and_changed_facts_keep_original_snapshot(self):
        url = reverse("marketing:kpi_detail", args=[self.target.pk])
        payload = {
            "operation": "activate",
            "expected_version": 1,
            "reason": "Agreed target",
        }
        self.assertEqual(self.client.post(url, payload).status_code, 302)
        self.assertEqual(self.client.post(url, payload).status_code, 200)
        self.target.refresh_from_db()
        self.assertEqual(self.target.version, 2)
        from .tests_kpi import certify_lead_sources

        certify_lead_sources(self.org, self.owner, self.target)
        response = self.client.get(url)
        self.assertEqual(response.context["evaluation"]["actual"], "1.00")
        self.assertEqual(response.context["evaluation"]["attainment"], "50.00")
        payload = {
            "operation": "review",
            "expected_version": 2,
            "conclusion": "Continue follow up",
            "expected_fingerprint": response.context["evaluation"]["fingerprint"],
        }
        self.assertEqual(self.client.post(url, payload).status_code, 302)
        review = KpiReview.objects.get(target=self.target)
        self.assertEqual(review.snapshot["actual"], "1.00")
        response = self.client.get(url)
        self.assertFalse(list(response.context["reviews"])[0].is_stale)
        services.create_lead(
            organization=self.org,
            actor=self.marketing,
            reference="LEAD-WEB-2",
            name="Another prospect",
            channel="sales",
            received_at=timezone.make_aware(datetime.combine(self.start, time(11))),
            owner=self.member,
        )
        response = self.client.get(url)
        self.assertEqual(response.context["evaluation"]["actual"], "2.00")
        self.assertTrue(list(response.context["reviews"])[0].is_stale)
        review.refresh_from_db()
        self.assertEqual(review.snapshot["actual"], "1.00")
        self.assertFalse(Journal.objects.exists())
        self.assertFalse(Invoice.objects.exists())

    def test_owner_can_assign_marketing_submit_owner_review(self):
        url = reverse("marketing:activity_detail", args=[self.activity.pk])
        self.login(self.marketing)
        response = self.client.post(
            url,
            {
                "operation": "submit",
                "expected_version": 1,
                "occurred_on": self.today,
                "evidence": "Archived QA conversation #123",
                "outcome": "Next discussion scheduled",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.activity.refresh_from_db()
        self.assertEqual(self.activity.status, "submitted")
        self.assertEqual(
            self.client.post(
                url,
                {
                    "operation": "accept",
                    "expected_version": 2,
                    "review_notes": "Self approve",
                },
            ).status_code,
            403,
        )
        self.login(self.owner)
        self.assertTrue(self.client.get(url).context["can_decide"])
        response = self.client.post(
            url,
            {
                "operation": "accept",
                "expected_version": 2,
                "review_notes": "Checked against archive",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.activity.refresh_from_db()
        self.assertEqual(self.activity.status, "accepted")
        self.assertFalse(self.client.get(url).context["can_submit"])

    def test_empty_invalid_and_stale_posts_do_not_change_records(self):
        self.assertEqual(
            self.client.post(reverse("marketing:kpi_create"), {}).status_code, 200
        )
        self.assertEqual(
            self.client.post(reverse("marketing:activity_create"), {}).status_code, 200
        )
        self.assertEqual(KpiTarget.objects.filter(organization=self.org).count(), 1)
        self.assertEqual(WorkActivity.objects.filter(organization=self.org).count(), 1)
        url = reverse("marketing:activity_detail", args=[self.activity.pk])
        self.assertEqual(self.client.post(url, {"operation": "start"}).status_code, 200)
        self.assertEqual(
            self.client.post(
                url,
                {
                    "operation": "start",
                    "expected_version": 1,
                    "responsibility_reason": "Supervisor begins assigned work",
                },
            ).status_code,
            302,
        )
        self.assertEqual(
            self.client.post(
                url, {"operation": "start", "expected_version": 1}
            ).status_code,
            200,
        )
        self.activity.refresh_from_db()
        self.assertEqual(self.activity.version, 2)
        self.assertEqual(
            self.client.post(
                url,
                {
                    "operation": "submit",
                    "expected_version": 2,
                    "occurred_on": self.today,
                    "evidence": "",
                    "outcome": "No evidence",
                },
            ).status_code,
            200,
        )
        self.activity.refresh_from_db()
        self.assertEqual(self.activity.status, "in_progress")

    def test_create_forms_route_through_services_and_reject_foreign_references(self):
        self.login(self.marketing)
        payload = {
            "title": "New target",
            "metric_key": "response_coverage_percent",
            "target": "90",
            "period_start": self.start,
            "period_end": self.end,
            "member": self.member.pk,
            "channel": "sales",
            "rationale": "Improve responsiveness",
        }
        self.assertEqual(
            self.client.post(reverse("marketing:kpi_create"), payload).status_code, 302
        )
        target = KpiTarget.objects.get(title="New target")
        self.assertEqual(target.created_by, self.marketing)
        self.assertEqual(target.status, "draft")
        response = self.client.post(
            reverse("marketing:kpi_create"),
            {**payload, "title": "Forbidden", "member": self.foreign_member.pk},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(KpiTarget.objects.filter(title="Forbidden").exists())
        work = {
            "title": "New task",
            "owner": self.member.pk,
            "channel": "seo",
            "kind": "seo",
            "due_date": self.today,
            "description": "Publish reviewed article",
        }
        self.assertEqual(
            self.client.post(reverse("marketing:activity_create"), work).status_code,
            302,
        )
        self.assertEqual(
            WorkActivity.objects.get(title="New task").created_by, self.marketing
        )

    def test_overdue_filter_excludes_already_submitted_evidence(self):
        work = activities.create_activity(
            organization=self.org,
            actor=self.marketing,
            title="Past due",
            owner=self.member,
            channel="sales",
            kind="sales",
            due_date=self.end,
        )
        url = reverse("marketing:activities")
        self.assertContains(self.client.get(url, {"overdue": "1"}), "Past due")
        activities.update_activity(
            organization=self.org,
            actor=self.marketing,
            activity=work,
            expected_version=1,
            operation="submit",
            occurred_on=self.end,
            evidence="Work archive",
            outcome="Done yesterday",
        )
        self.assertNotContains(self.client.get(url, {"overdue": "1"}), "Past due")
