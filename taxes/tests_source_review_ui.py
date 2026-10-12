from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import Membership, Organization
from taxes.knowledge import current_sources, publish_catalog
from taxes.models import TaxSourcePublication, TaxSourceRevision


@override_settings(MFA_REQUIRED=False)
class TaxSourceReviewPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.org = Organization.objects.create(name="Synthetic source review org")
        cls.owner = User.objects.create_user(username="source-review-owner")
        cls.finance = User.objects.create_user(username="source-review-finance")
        cls.staff = User.objects.create_user(
            username="source-review-staff", is_staff=True
        )
        cls.outside_staff = User.objects.create_user(
            username="source-review-outside", is_staff=True
        )
        cls.admin = User.objects.create_superuser(
            username="source-review-admin",
            email="admin@example.invalid",
            password="test-only",
        )
        Membership.objects.create(organization=cls.org, user=cls.owner, role="owner")
        Membership.objects.create(
            organization=cls.org, user=cls.finance, role="finance"
        )
        Membership.objects.create(organization=cls.org, user=cls.staff, role="finance")
        Membership.objects.create(organization=cls.org, user=cls.admin, role="owner")

    def setUp(self):
        publish_catalog(
            [
                {
                    "slug": "synthetic-source-review",
                    "title": "Rujukan pajak sintetis untuk tes",
                    "url": "https://example.invalid/tax-source",
                    "issuer": "Penerbit sintetis",
                    "article": "Pasal contoh",
                    "summary": "Ringkasan publik sintetis untuk pengujian alur tinjauan.",
                    "topics": ["deadline"],
                }
            ],
            timezone.localdate(),
        )
        self.revision = TaxSourceRevision.objects.get(
            source__slug="synthetic-source-review"
        )
        self.url = reverse("taxes:source_reviews")

    def publish_data(self, **overrides):
        return {
            "revision": self.revision.pk,
            "reviewer_name": "Synthetic tax reviewer",
            "review_reference": "Synthetic review record 001",
            "confirm_reviewed": "on",
            **overrides,
        }

    def test_anonymous_user_is_sent_to_login(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)

    def test_company_roles_can_read_but_cannot_publish_without_system_privilege(self):
        for user in (self.owner, self.finance):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                self.assertEqual(self.client.get(self.url).status_code, 200)
                self.assertEqual(
                    self.client.post(self.url, self.publish_data()).status_code, 403
                )
        self.assertFalse(TaxSourcePublication.objects.exists())
        self.assertEqual(current_sources(), [])

    def test_django_staff_without_company_access_is_denied(self):
        self.client.force_login(self.outside_staff)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_finance_can_find_source_status_from_monthly_tax_page(self):
        self.client.force_login(self.finance)
        response = self.client.get(reverse("tax_workspace"))
        self.assertContains(response, "Periksa status semua rujukan pajak")
        self.assertContains(response, self.url)

    def test_admin_sees_pending_source_and_plain_language_review_steps(self):
        self.client.force_login(self.admin)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Rujukan pajak sintetis untuk tes")
        self.assertContains(response, "Periksa sumber resmi")
        self.assertContains(response, "Catat nama pemeriksa")
        self.assertContains(
            response, "Penerbitan sumber tidak mengubah pajak perusahaan"
        )
        self.assertEqual(list(response.context["pending_revisions"]), [self.revision])
        self.assertFalse(TaxSourcePublication.objects.exists())
        self.assertEqual(current_sources(), [])

    def test_publication_requires_explicit_confirmation_and_review_reference(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            self.url,
            self.publish_data(confirm_reviewed="", review_reference=""),
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(TaxSourcePublication.objects.exists())
        self.assertEqual(current_sources(), [])

    def test_admin_can_publish_reviewed_revision_and_it_becomes_current(self):
        self.client.force_login(self.admin)
        response = self.client.post(self.url, self.publish_data())
        self.assertEqual(response.status_code, 302)
        publication = TaxSourcePublication.objects.get()
        self.assertEqual(publication.revision, self.revision)
        self.assertEqual(publication.reviewer_name, "Synthetic tax reviewer")
        self.assertEqual(publication.published_by, self.admin)
        self.assertEqual(
            [source.slug for source in current_sources(topic="deadline")],
            ["synthetic-source-review"],
        )
        self.assertEqual(Organization.objects.count(), 1)
        page = self.client.get(self.url)
        self.assertContains(page, "Rujukan umum diterbitkan")
        self.assertContains(page, "Aktif")
        self.assertContains(page, "Synthetic tax reviewer")
        self.assertContains(page, "Synthetic review record 001")
        self.assertContains(page, "source-review-admin")

    def test_published_revision_cannot_be_submitted_again(self):
        self.client.force_login(self.admin)
        self.client.post(self.url, self.publish_data())
        response = self.client.post(self.url, self.publish_data())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(TaxSourcePublication.objects.count(), 1)
        self.assertEqual(list(response.context["pending_revisions"]), [])

    def test_owner_can_read_immutable_publication_history(self):
        self.client.force_login(self.admin)
        self.client.post(self.url, self.publish_data())
        publication = TaxSourcePublication.objects.get()
        self.client.force_login(self.owner)
        response = self.client.get(self.url)
        self.assertEqual(list(response.context["publication_history"]), [publication])
        self.assertContains(response, "Riwayat penerbitan")
        self.assertContains(response, "Synthetic review record 001")
