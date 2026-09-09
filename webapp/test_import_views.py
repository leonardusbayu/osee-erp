from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

from core.models import Membership, Organization
from imports import tests as source_test_helpers
from imports.models import SourceDocument


class ImportWebTests(TestCase):
    def setUp(self):
        source_test_helpers.SourceImportTests.setUp(self)
        self.client.force_login(self.owner)

    write_bundle = source_test_helpers.SourceImportTests.write_bundle
    run_import = source_test_helpers.SourceImportTests.run_import

    def test_real_source_totals_render_and_unknown_month_is_not_zero_data(self):
        self.run_import()
        for name in ["dashboard", "imports_overview", "partners", "reports", "bank", "sales"]:
            response = self.client.get(reverse(name))
            self.assertEqual(response.status_code, 200, name)
            self.assertContains(response, "Data 2026")
        response = self.client.get(reverse("imports_overview"))
        self.assertEqual(response.context["summary"]["printed_count"], 2)
        self.assertEqual(response.context["summary"]["detail_count"], 3)
        self.assertEqual(len(response.context["summary"]["missing_months"]), 11)
        response = self.client.get(reverse("imports_month", args=["2026-01"]))
        self.assertContains(response, "05-Jan-25")
        self.assertContains(response, "Tahun berbeda")
        self.assertEqual(self.client.get(reverse("imports_month", args=["2026-02"])).status_code, 404)
        self.assertIsNone(self.client.get(reverse("imports_overview") + "?year=2027").context["batch"])

    def test_imported_sources_are_private_and_organization_scoped(self):
        self.run_import()
        source = SourceDocument.objects.get()
        url = reverse("import_source", args=[source.pk])
        response = self.client.get(url)
        self.assertTrue(b"".join(response.streaming_content).startswith(b"%PDF-"))
        response.close()
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(Client().get(url).status_code, 302)
        outsider = User.objects.create_user("other-reader")
        Membership.objects.create(organization=self.other, user=outsider, role="auditor")
        self.client.force_login(outsider)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.get(reverse("imports_month", args=["2026-01"])).status_code, 404)
        self.assertNotContains(self.client.get(reverse("partners")), "RAW LABEL")

    def test_pending_import_requires_owner_signup_and_rejects_demo_login(self):
        self.run_import()
        Membership.objects.filter(organization=self.org).delete()
        self.client.logout()
        self.assertContains(self.client.get(reverse("login")), "Aktifkan akun pemilik")
        self.assertEqual(self.client.post(reverse("demo_login")).status_code, 404)
        before = Organization.objects.count()
        response = self.client.post(reverse("setup"), {"company_name": "Forged target", "organization": self.other.pk,
                    "username": "new-company-owner", "email": "owner@example.test",
                    "password1": "Local-test-password-8427", "password2": "Local-test-password-8427"})
        self.assertRedirects(response, reverse("dashboard"))
        self.assertEqual(Organization.objects.count(), before)
        member = Membership.objects.get(user__username="new-company-owner")
        self.assertEqual(member.organization_id, self.org.pk)
        self.org.refresh_from_db()
        self.assertEqual(self.org.name, "Synthetic source company")
        self.assertEqual(self.org.import_batches.count(), 1)

    def test_import_screens_are_read_only_and_source_labels_escape(self):
        self.bundle["channels"][0]["source_label"] = "RAW <script>bad()</script>"
        self.run_import()
        response = self.client.get(reverse("imports_month", args=["2026-01"]))
        self.assertContains(response, "&lt;script&gt;bad()&lt;/script&gt;")
        self.assertNotContains(response, "<script>bad()</script>")
        self.assertEqual(self.client.post(reverse("imports_overview")).status_code, 405)
        self.assertEqual(self.client.get(reverse("imports_overview") + "?year=bad").status_code, 400)
        exported = self.client.get(reverse("imports_export"))
        self.assertContains(exported, "belum menjadi jurnal")
