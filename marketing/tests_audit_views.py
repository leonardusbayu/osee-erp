import csv
import io

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from core.audit import record
from core.models import AuditEvent, Membership, Organization

from .audit_views import csv_cell
from .services import create_member


class MarketingAuditTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(name="Audit marketing test")
        cls.other = Organization.objects.create(name="Another company")
        for role in ("owner", "marketing", "finance", "auditor", "reviewer"):
            actor = get_user_model().objects.create_user(username=f"audit-{role}")
            Membership.objects.create(organization=cls.org, user=actor, role=role)
            setattr(cls, role, actor)
        cls.foreign = get_user_model().objects.create_user(
            username="OTHER-ACTOR-CONFIDENTIAL"
        )
        Membership.objects.create(
            organization=cls.other, user=cls.foreign, role="owner"
        )
        create_member(
            organization=cls.org,
            actor=cls.marketing,
            name="Staff test",
            discipline="sales",
        )
        record(
            cls.org,
            cls.finance,
            "finance.private",
            detail={"secret": "FINANCE-CONFIDENTIAL"},
        )
        record(
            cls.other,
            cls.foreign,
            "marketing.activity.created",
            detail={"secret": "OTHER-CONFIDENTIAL"},
        )

    def setUp(self):
        self.client.force_login(self.owner)
        session = self.client.session
        session["organization_id"] = self.org.pk
        session.save()

    def test_scope_and_export_exclude_finance_and_other_company(self):
        url = reverse("marketing:audit")
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Staff test")
        self.assertNotContains(page, "CONFIDENTIAL")
        response = self.client.post(reverse("marketing:audit_export"), {})
        self.assertEqual(response.status_code, 200)
        text = response.content.decode("utf-8-sig")
        self.assertIn("marketing.member.created", text)
        self.assertNotIn("CONFIDENTIAL", text)
        event = AuditEvent.objects.get(action="marketing.audit.exported")
        self.assertEqual(event.actor, self.owner)
        self.assertEqual(event.organization, self.org)
        self.assertEqual(event.detail["rows"], 1)

    def test_invalid_and_foreign_filters_cannot_widen_results(self):
        cases = [
            {"actor": self.foreign.pk},
            {"start": "invalid"},
            {"start": "2026-10-02", "end": "2026-01-01"},
            {"action": "finance.private"},
            {"object_type": "Invoice"},
            {"object_id": "-2"},
        ]
        for params in cases:
            with self.subTest(params=params):
                self.assertEqual(
                    self.client.get(reverse("marketing:audit"), params).status_code, 400
                )
                self.assertEqual(
                    self.client.post(
                        reverse("marketing:audit_export"), params
                    ).status_code,
                    400,
                )
        self.assertEqual(
            self.client.get(reverse("marketing:audit"), {"page": "1000"}).status_code,
            400,
        )

    def test_roles_and_login_and_csrf(self):
        url, export = reverse("marketing:audit"), reverse("marketing:audit_export")
        self.assertEqual(Client().get(url).status_code, 302)
        self.assertEqual(self.client.get(export).status_code, 405)
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.owner)
        self.assertEqual(csrf.post(export, {}).status_code, 403)
        for role in ("owner", "marketing", "finance", "auditor"):
            self.client.force_login(getattr(self, role))
            self.assertEqual(self.client.get(url).status_code, 200)
            self.assertEqual(self.client.post(export, {}).status_code, 200)
        self.client.force_login(self.reviewer)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(export, {}).status_code, 403)

    def test_escaping_and_spreadsheet_formula_protection(self):
        self.marketing.username = "=SUM(1)"
        self.marketing.save()
        record(
            self.org,
            self.marketing,
            "marketing.lead.updated",
            detail={"notes": "<script>alert(1)</script>"},
        )
        page = self.client.get(reverse("marketing:audit"))
        self.assertNotContains(page, "<script>alert(1)</script>")
        self.assertContains(page, "&lt;script&gt;alert(1)&lt;/script&gt;")
        response = self.client.post(reverse("marketing:audit_export"), {})
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
        self.assertTrue(any(row[2] == "'=SUM(1)" for row in rows[1:]))
        for value in (
            "=SUM(1)",
            "+formula",
            "-formula",
            "@formula",
            "  =formula",
            "\tformula",
            "\nformula",
        ):
            self.assertTrue(csv_cell(value).startswith("'"))
        self.assertEqual(csv_cell("Ordinary note"), "Ordinary note")

    def test_target_drilldown_includes_review_without_other_records(self):
        record(
            self.org,
            self.owner,
            "marketing.kpi.activated",
            detail={"reason": "Target approved"},
        )
        event = AuditEvent.objects.create(
            organization=self.org,
            actor=self.owner,
            action="marketing.kpi.created",
            object_type="KpiTarget",
            object_id="73",
            detail={"values": {"title": "Chosen target"}},
        )
        review = AuditEvent.objects.create(
            organization=self.org,
            actor=self.owner,
            action="marketing.kpi.reviewed",
            object_type="KpiReview",
            object_id="12",
            detail={"target_id": 73, "conclusion": "Chosen review"},
        )
        AuditEvent.objects.create(
            organization=self.org,
            actor=self.owner,
            action="marketing.kpi.reviewed",
            object_type="KpiReview",
            object_id="13",
            detail={"target_id": 74, "conclusion": "Unrelated review"},
        )
        page = self.client.get(
            reverse("marketing:audit"), {"object_type": "KpiTarget", "object_id": 73}
        )
        self.assertEqual(
            [item.pk for item in page.context["page"]], [review.pk, event.pk]
        )
        self.assertNotContains(page, "Unrelated review")
        self.assertContains(page, reverse("marketing:kpi_detail", args=[73]))

    def test_export_limit_rejects_instead_of_silently_truncating(self):
        from unittest.mock import patch

        with patch("marketing.audit_views.EXPORT_LIMIT", 0):
            response = self.client.post(reverse("marketing:audit_export"), {})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(
            AuditEvent.objects.filter(action="marketing.audit.exported").exists()
        )

    def test_date_filter_can_hide_current_records(self):
        page = self.client.get(reverse("marketing:audit"), {"end": "2000-01-01"})
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context["page"].paginator.count, 0)
        page = self.client.get(
            reverse("marketing:audit"), {"start": timezone.localdate().isoformat()}
        )
        self.assertEqual(page.context["page"].paginator.count, 1)
