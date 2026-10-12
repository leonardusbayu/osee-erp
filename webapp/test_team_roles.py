"""Team roles, scoped account provisioning, and preserved legacy privileges."""

import re
from datetime import date

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from core.models import AuditEvent, Membership, Organization
from taxes.models import TaxProfile
from taxes.services import approve_tax_profile
from .forms import TeamMemberForm


CANONICAL_ROLES = [
    ("owner", "Owner/Direktur"),
    ("marketing", "Marketing"),
    ("finance", "Finance"),
    ("operations", "Operations"),
]
SYNTHETIC_PASSWORD = "T8!oL9@qR4#pV2zK7"


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class TeamRoleHTTPTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(name="Synthetic Team Company")
        cls.other = Organization.objects.create(name="Other Synthetic Team Company")
        for role in (
            "owner",
            "director",
            "finance",
            "marketing",
            "operations",
            "reviewer",
            "auditor",
        ):
            user = get_user_model().objects.create_user(
                username=f"team-role-{role}",
                password=SYNTHETIC_PASSWORD,
                first_name=f"Synthetic {role}",
            )
            Membership.objects.create(organization=cls.org, user=user, role=role)
            setattr(cls, role, user)
        cls.profile = TaxProfile.objects.create(
            organization=cls.org,
            registration_date=date(2024, 1, 1),
            registration_evidence="Synthetic registration evidence",
        )

    def setUp(self):
        self.login(self.owner)

    def login(self, user, organization=None, client=None):
        client = client or self.client
        client.force_login(user)
        session = client.session
        session["organization_id"] = (organization or self.org).pk
        session.save()

    def account_data(self, role, *, suffix="new"):
        return {
            "first_name": "Synthetic Team Member",
            "username": f"created-team-{suffix}",
            "email": f"{suffix}@example.test",
            "role": role,
            "password1": SYNTHETIC_PASSWORD,
            "password2": SYNTHETIC_PASSWORD,
        }

    def counts(self):
        return (
            get_user_model().objects.count(),
            Membership.objects.count(),
            AuditEvent.objects.count(),
        )

    def assert_profile_pending(self):
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.regime, TaxProfile.Regime.UNDECIDED)
        self.assertIsNone(self.profile.reviewed_by)
        self.assertIsNone(self.profile.reviewed_at)
        self.assertFalse(
            AuditEvent.objects.filter(action="tax.profile.approved").exists()
        )

    def test_account_form_exposes_exactly_four_canonical_roles(self):
        self.assertEqual(list(Membership.TEAM_ROLES), CANONICAL_ROLES)
        self.assertEqual(list(TeamMemberForm().fields["role"].choices), CANONICAL_ROLES)
        response = self.client.get(reverse("member_create"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            list(response.context["form"].fields["role"].choices), CANONICAL_ROLES
        )
        self.assertEqual(response.context["form"]["role"].value(), "finance")
        for value, label in CANONICAL_ROLES:
            selected = " selected" if value == "finance" else ""
            self.assertContains(
                response,
                f'<option value="{value}"{selected}>{label}</option>',
                html=True,
            )
        for value in ("director", "reviewer", "auditor", "operation"):
            self.assertNotContains(response, f'<option value="{value}">')

    def test_forged_legacy_or_unknown_role_creates_no_user_membership_or_audit(self):
        before = self.counts()
        for index, role in enumerate(
            (
                "director",
                "reviewer",
                "auditor",
                "operation",
                "operator",
                "admin",
                "",
            )
        ):
            with self.subTest(role=role):
                data = self.account_data(role, suffix=f"forged-{index}")
                response = self.client.post(reverse("member_create"), data)
                self.assertEqual(response.status_code, 200)
                self.assertIn("role", response.context["form"].errors)
                self.assertFalse(
                    get_user_model().objects.filter(username=data["username"]).exists()
                )
                self.assertEqual(self.counts(), before)
        self.assert_profile_pending()

    def test_nonowner_cannot_open_or_post_privileged_team_creation(self):
        before = self.counts()
        for role in (
            "finance",
            "marketing",
            "operations",
            "director",
            "reviewer",
            "auditor",
        ):
            with self.subTest(role=role):
                self.login(getattr(self, role))
                self.assertEqual(
                    self.client.get(reverse("member_create")).status_code, 403
                )
                response = self.client.post(
                    reverse("member_create"),
                    self.account_data("owner", suffix=f"denied-{role}"),
                )
                self.assertEqual(response.status_code, 403)
                self.assertEqual(self.counts(), before)
        self.assert_profile_pending()

    def test_owner_creates_each_canonical_role_with_scoped_membership_and_audit(self):
        for role, _label in CANONICAL_ROLES:
            with self.subTest(role=role):
                data = self.account_data(role, suffix=role)
                response = self.client.post(
                    reverse("member_create"),
                    data
                    | {
                        "organization": self.other.pk,
                        "organization_id": self.other.pk,
                        "created_by": self.finance.pk,
                        "is_staff": "on",
                        "is_superuser": "on",
                    },
                )
                self.assertRedirects(
                    response, reverse("settings"), fetch_redirect_response=False
                )
                user = get_user_model().objects.get(username=data["username"])
                membership = Membership.objects.get(user=user)
                self.assertEqual(membership.organization, self.org)
                self.assertEqual(membership.role, role)
                self.assertTrue(user.is_active)
                self.assertFalse(user.is_staff)
                self.assertFalse(user.is_superuser)
                self.assertTrue(user.check_password(SYNTHETIC_PASSWORD))
                self.assertNotEqual(user.password, SYNTHETIC_PASSWORD)
                event = AuditEvent.objects.get(
                    action="organization.member.created", object_id=str(membership.pk)
                )
                self.assertEqual(event.organization, self.org)
                self.assertEqual(event.actor, self.owner)
                self.assertEqual(event.detail["role"], role)
                self.assertNotIn(SYNTHETIC_PASSWORD, str(event.detail))
        self.assertFalse(Membership.objects.filter(organization=self.other).exists())
        self.assertEqual(
            AuditEvent.objects.filter(action="organization.member.created").count(), 4
        )
        self.assert_profile_pending()

    def test_owner_permission_is_checked_in_selected_company_not_another_membership(
        self,
    ):
        Membership.objects.create(
            organization=self.other, user=self.owner, role="finance"
        )
        self.login(self.owner, organization=self.other)
        before = self.counts()
        response = self.client.post(
            reverse("member_create"),
            self.account_data("owner", suffix="wrong-company")
            | {"organization": self.org.pk},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.counts(), before)
        session = self.client.session
        session["organization_id"] = 999999
        session.save()
        self.assertEqual(
            self.client.post(
                reverse("member_create"), self.account_data("finance")
            ).status_code,
            403,
        )
        self.assertEqual(self.counts(), before)

    def test_missing_csrf_cannot_create_another_owner(self):
        secure = Client(enforce_csrf_checks=True)
        self.login(self.owner, client=secure)
        before = self.counts()
        response = secure.post(
            reverse("member_create"), self.account_data("owner", suffix="csrf-denied")
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.counts(), before)

    def test_login_default_matches_canonical_workspace_and_retains_legacy_director(
        self,
    ):
        targets = (
            (self.owner, "director:overview"),
            (self.director, "director:overview"),
            (self.marketing, "marketing:daily"),
            (self.finance, "dashboard"),
        )
        for user, target in targets:
            with self.subTest(role=user.username):
                browser = Client()
                response = browser.post(
                    reverse("login"),
                    {"username": user.username, "password": SYNTHETIC_PASSWORD},
                )
                self.assertRedirects(
                    response, reverse(target), fetch_redirect_response=False
                )
                self.assertEqual(browser.get(response["Location"]).status_code, 200)
        requested = reverse("reports")
        browser = Client()
        response = browser.post(
            reverse("login"),
            {
                "username": self.owner.username,
                "password": SYNTHETIC_PASSWORD,
                "next": requested,
            },
        )
        self.assertRedirects(response, requested, fetch_redirect_response=False)
        browser = Client()
        response = browser.post(
            reverse("login"),
            {
                "username": self.owner.username,
                "password": SYNTHETIC_PASSWORD,
                "next": "https://untrusted.example/",
            },
        )
        self.assertRedirects(
            response, reverse("director:overview"), fetch_redirect_response=False
        )

    def test_legacy_membership_codes_display_and_existing_permissions_remain_intact(
        self,
    ):
        old = {
            role: Membership.objects.get(
                user=getattr(self, role), organization=self.org
            )
            for role in ("director", "reviewer", "auditor")
        }
        before = {role: member.pk for role, member in old.items()}
        response = self.client.get(reverse("settings"))
        self.assertEqual(response.status_code, 200)
        for role, member in old.items():
            with self.subTest(role=role):
                member.full_clean()
                self.assertIn(role, dict(Membership.ROLES))
                self.assertNotEqual(member.get_role_display(), role)
                self.assertContains(response, member.user.username)
                self.assertContains(response, member.get_role_display())
                member.refresh_from_db()
                self.assertEqual(member.role, role)
                self.assertEqual(member.pk, before[role])
        self.login(self.director)
        self.assertEqual(self.client.get(reverse("director:overview")).status_code, 200)
        self.login(self.auditor)
        self.assertEqual(self.client.get(reverse("reports")).status_code, 200)
        self.assertEqual(self.client.get(reverse("sale_create")).status_code, 403)
        self.login(self.reviewer)
        self.assertEqual(self.client.get(reverse("tax_workspace")).status_code, 200)

    def test_new_owner_and_finance_roles_do_not_gain_tax_profile_approval(self):
        for role in ("owner", "finance"):
            response = self.client.post(
                reverse("member_create"), self.account_data(role, suffix=f"tax-{role}")
            )
            self.assertRedirects(
                response, reverse("settings"), fetch_redirect_response=False
            )
            user = get_user_model().objects.get(username=f"created-team-tax-{role}")
            with self.assertRaises(PermissionDenied):
                approve_tax_profile(
                    self.profile,
                    user,
                    regime=TaxProfile.Regime.NORMAL,
                    effective_from=date(2026, 1, 1),
                    review_note="Attempted implied tax authority",
                )
            self.assert_profile_pending()
        # Legacy specialist access also requires a subject-bound professional report.
        import io
        from pypdf import PdfWriter
        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.core.exceptions import ValidationError
        from taxes import workflow as work

        with self.assertRaises(ValidationError):
            approve_tax_profile(
                self.profile,
                self.reviewer,
                regime=TaxProfile.Regime.NORMAL,
                effective_from=date(2026, 1, 1),
                review_note="Unchecked legacy role",
            )
        self.assert_profile_pending()
        self.profile.taxpayer_reference = "1234567890123456"
        self.profile.save(update_fields=["taxpayer_reference"])
        output = io.BytesIO()
        pdf = PdfWriter()
        pdf.add_blank_page(width=100, height=100)
        pdf.write(output)
        evidence = work.upload_evidence(
            organization=self.org,
            actor=self.reviewer,
            file=SimpleUploadedFile("synthetic-review.pdf", output.getvalue()),
        )
        review = work.record_external_review(
            organization=self.org,
            actor=self.reviewer,
            subject=self.profile,
            decision=work.profile_decision(
                regime="normal", effective_from=date(2026, 1, 1)
            ),
            evidence=evidence,
            professional_name="Synthetic professional",
            qualification_reference="Synthetic engagement",
            reviewed_on=date(2026, 9, 9),
            statement="Synthetic report confirms this profile decision",
            report_confirmed=True,
        )
        approved = approve_tax_profile(
            self.profile,
            self.reviewer,
            regime=TaxProfile.Regime.NORMAL,
            effective_from=date(2026, 1, 1),
            review_note="Synthetic documented tax review",
            external_review=review,
        )
        self.assertEqual(approved.reviewed_by, self.reviewer)
        self.assertEqual(approved.regime, TaxProfile.Regime.NORMAL)
        self.assertEqual(approved.external_review, review)

    def test_owner_tax_evidence_form_cannot_forge_approval_after_role_simplification(
        self,
    ):
        response = self.client.post(
            reverse("tax_profile_setup"),
            {
                "registration_date": "2024-01-01",
                "registration_evidence": "Updated synthetic evidence",
                "vat_status_effective_from": "2026-01-01",
                "vat_status_evidence": "Synthetic VAT evidence",
                "regime": "normal",
                "reviewed_by": self.owner.pk,
                "reviewed_at": "2026-01-01T00:00:00Z",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assert_profile_pending()

    def test_modules_and_settings_show_canonical_role_guide_and_upcoming_operations(
        self,
    ):
        for page in ("modules", "settings"):
            with self.subTest(page=page):
                response = self.client.get(reverse(page))
                self.assertEqual(response.status_code, 200)
                self.assertTemplateUsed(response, "app/team_role_guide.html")
                self.assertContains(
                    response,
                    '<h2 id="team-role-heading">Empat peran tim OSEE</h2>',
                    html=True,
                )
                for _role, label in CANONICAL_ROLES:
                    self.assertContains(
                        response, f"<strong>{label}</strong>", html=True
                    )
                self.assertEqual(
                    response.context["membership"].get_role_display(), "Owner/Direktur"
                )
                self.assertContains(response, "Pesanan &amp; Peserta — Operations")
                self.assertContains(response, "tutor/kontraktor")
                self.assertContains(
                    response, "PPh 21 dan BPJS dicatat sesuai nominal yang disetujui"
                )
                self.assertContains(response, "perhitungan otomatis belum dijalankan")
                if page == "modules":
                    self.assertContains(response, "Rencana pengembangan")
                else:
                    self.assertContains(response, "Daftar modul perusahaan")
                operations = next(
                    item
                    for item in response.context["module_registry"]
                    if item.key == "test_operations"
                )
                self.assertEqual(operations.label, "Pesanan & Peserta")
                self.assertTrue(operations.available)
                self.assertEqual(operations.url, "/operations/")

    def test_settings_document_title_contains_only_title_not_duplicated_page_sections(
        self,
    ):
        response = self.client.get(reverse("settings"))
        self.assertEqual(response.status_code, 200)
        titles = re.findall(
            r"<title\b[^>]*>(.*?)</title\s*>",
            response.content.decode("utf-8"),
            flags=re.I | re.S,
        )
        self.assertEqual(len(titles), 1)
        self.assertIn("Pengaturan", titles[0])
        self.assertNotIn("Anggota tim", titles[0])
        self.assertNotIn(
            "<",
            titles[0],
            "Page sections must not be rendered inside the browser title",
        )
        self.assertContains(response, "<h2>Anggota tim</h2>", count=1, html=True)

    def test_marketing_module_picker_has_role_guide_without_finance_or_director_cards(
        self,
    ):
        self.login(self.marketing)
        response = self.client.get(reverse("modules"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/team_role_guide.html")
        self.assertContains(response, "Empat peran tim OSEE")
        self.assertContains(response, "Owner/Direktur")
        available = {
            item.key: item.url
            for item in response.context["module_registry"]
            if item.available
        }
        self.assertEqual(available, {"marketing": reverse("marketing:daily")})
        self.assertContains(
            response, '<h2 id="module-primary-heading">Marketing</h2>', html=True
        )
        self.assertNotContains(response, "<h2>Finance</h2>", html=True)
        self.assertNotContains(response, "<h2>Direktur</h2>", html=True)
        # The brand link may still lead to '/', so check the actual module links.
        html = response.content.decode("utf-8")
        module_links = re.findall(
            r'<a\b[^>]*class="button [^"]*"[^>]*href="([^"]+)"[^>]*>\s*(?:Mulai di|Buka)\b',
            html,
        )
        self.assertEqual(module_links, [reverse("marketing:daily")])
        self.assertContains(response, "Pesanan &amp; Peserta — Operations")

    def test_operations_role_has_a_limited_home_workspace_and_account_security(self):
        self.login(self.operations)
        response = self.client.get(reverse("modules"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["primary_module"].key, "test_operations")
        self.assertEqual(response.context["primary_module"].url, "/operations/")
        self.assertEqual(response.context["other_modules"], ())
        self.assertEqual(
            {
                module.key
                for module in response.context["module_registry"]
                if module.available
            },
            {"test_operations"},
        )
        self.assertContains(response, "Operations")
        self.assertContains(response, "Tidak dapat menerbitkan invoice")
        self.assertNotContains(response, "Tanya Pajak")
        self.assertNotContains(response, 'id="global-search"')
        self.assertEqual(
            self.client.get(reverse("operations:overview")).status_code, 200
        )
        self.assertEqual(self.client.get(reverse("password_change")).status_code, 200)
        self.assertEqual(self.client.get(reverse("security_settings")).status_code, 200)

        denied_urls = (
            "dashboard",
            "sales",
            "bank",
            "reports",
            "tax_workspace",
            "annual_tax",
            "chat",
            "settings",
            "imports_overview",
        )
        for name in denied_urls:
            with self.subTest(url=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 403)
        for url in (
            reverse("director:overview"),
            reverse("marketing:daily"),
            reverse("people:index"),
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 403)

    def test_operations_login_uses_its_workspace_and_rejects_finance_next_target(self):
        client = Client()
        response = client.post(
            reverse("login"),
            {"username": self.operations.username, "password": SYNTHETIC_PASSWORD},
        )
        self.assertRedirects(
            response, reverse("operations:overview"), fetch_redirect_response=False
        )

        client = Client()
        response = client.post(
            reverse("login") + "?next=/bank/",
            {"username": self.operations.username, "password": SYNTHETIC_PASSWORD},
        )
        self.assertRedirects(
            response, reverse("operations:overview"), fetch_redirect_response=False
        )

    def test_module_picker_emphasizes_one_role_home_and_hides_other_work_until_opened(
        self,
    ):
        cases = (
            (
                self.owner,
                "director",
                {"finance", "marketing", "people", "test_operations"},
            ),
            (
                self.finance,
                "finance",
                {"director", "marketing", "people", "test_operations"},
            ),
            (self.marketing, "marketing", set()),
            (self.operations, "test_operations", set()),
        )
        for user, primary_key, expected_other_keys in cases:
            with self.subTest(role=user.username):
                self.login(user)
                response = self.client.get(reverse("modules"))
                self.assertEqual(response.status_code, 200)
                primary = response.context["primary_module"]
                other_modules = response.context["other_modules"]
                self.assertEqual(primary.key, primary_key)
                self.assertEqual(
                    {module.key for module in other_modules}, expected_other_keys
                )
                self.assertContains(
                    response, '<section class="module-primary"', html=False
                )
                self.assertContains(
                    response,
                    f'<h2 id="module-primary-heading">{primary.label}</h2>',
                    html=True,
                )
                self.assertContains(response, f'href="{primary.url}"', html=False)
                self.assertContains(response, "Bagian utama Anda")
                if expected_other_keys:
                    self.assertContains(
                        response, '<details class="module-others">', html=False
                    )
                    self.assertContains(response, "Buka bagian lain")
                    for module in other_modules:
                        self.assertContains(
                            response, f"<h2>{module.label}</h2>", html=True
                        )
                else:
                    self.assertNotContains(
                        response, '<details class="module-others">', html=False
                    )
                    self.assertNotContains(response, "<h2>Finance</h2>", html=False)
                    self.assertNotContains(response, "<h2>Direktur</h2>", html=False)
