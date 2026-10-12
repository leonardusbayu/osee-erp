"""Content delivery uses the existing reviewed work lifecycle and real services."""

from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import activities, tests_workflow, workflow


class ContentDeliveryTests(TestCase):
    def setUp(self):
        tests_workflow.MarketingWorkflowTests.setUp(self)
        self.member = workflow.configure_member(
            organization=self.org,
            actor=self.owner,
            member=self.member,
            expected_version=1,
            account=self.staff,
            active=True,
            reason="Pemetaan akun",
        )
        self.work = activities.create_activity(
            organization=self.org,
            actor=self.owner,
            title="Panduan tes resmi",
            owner=self.member,
            kind="seo",
            channel="seo",
            due_date=timezone.localdate(),
            description="Brief: jawab kebutuhan peserta dan tautkan halaman pendaftaran yang benar.",
        )

    def accepted_work(self):
        self.work = activities.update_activity(
            organization=self.org,
            actor=self.staff,
            activity=self.work,
            expected_version=self.work.version,
            operation="submit",
            evidence="Arsip draf SYN-CONTENT",
            outcome="Draf dan CTA diperiksa",
            occurred_on=timezone.localdate(),
        )
        self.work = activities.update_activity(
            organization=self.org,
            actor=self.owner,
            activity=self.work,
            expected_version=self.work.version,
            operation="accept",
            review_notes="Draf siap diterbitkan sesuai brief",
        )
        return self.work

    def publish(self, **changes):
        from .workflow_content import record_publication

        fields = dict(
            organization=self.org,
            actor=self.staff,
            activity=self.work,
            expected_version=self.work.version,
            expected_latest_id=None,
            published_at=timezone.now(),
            url="https://example.com/toefl-guide",
            topic="Panduan TOEFL ITP",
            evidence="Arsip publikasi SYN-PUB",
        )
        fields.update(changes)
        return record_publication(**fields)

    def test_publication_requires_independently_accepted_work_and_preserves_approval(
        self,
    ):
        with self.assertRaises(ValidationError):
            self.publish()
        self.accepted_work()
        publication = self.publish()
        self.assertEqual(publication.approval["reviewed_by_id"], self.owner.pk)
        self.assertEqual(publication.approval["activity_version"], self.work.version)
        self.assertEqual(publication.created_by, self.staff)
        self.assertEqual(self.work.status, "accepted")
        with self.assertRaises(ValidationError):
            self.publish(published_at=self.work.reviewed_at - timedelta(seconds=1))

    def test_publication_corrections_keep_old_url_and_reject_stale_or_unassigned_actor(
        self,
    ):
        self.accepted_work()
        first = self.publish()
        with self.assertRaises(PermissionDenied):
            self.publish(actor=self.other_staff)
        with self.assertRaises(ValidationError):
            self.publish(
                expected_latest_id=first.pk, url="https://example.com/corrected"
            )
        revised = self.publish(
            expected_latest_id=first.pk,
            url="https://example.com/corrected",
            correction_reason="URL salinan sebelumnya salah",
        )
        self.assertEqual(revised.supersedes_id, first.pk)
        first.refresh_from_db()
        self.assertEqual(first.url, "https://example.com/toefl-guide")
        with self.assertRaises(ValidationError):
            self.publish(expected_latest_id=first.pk, correction_reason="Layar lama")

    def test_measurement_replay_and_correction_never_double_count_or_replace_evidence(
        self,
    ):
        from .workflow_content import record_measurement

        self.accepted_work()
        publication = self.publish()
        params = dict(
            organization=self.org,
            actor=self.staff,
            publication=publication,
            period_start=timezone.localdate(),
            period_end=timezone.localdate(),
            source="Search Console synthetic",
            reference="SYN-MEASURE-1",
            evidence="Arsip laporan SYN-1",
            impressions=100,
            clicks=5,
            visits=None,
        )
        first = record_measurement(**params)
        self.assertEqual(record_measurement(**params).pk, first.pk)
        with self.assertRaises(ValidationError):
            record_measurement(**{**params, "clicks": 6})
        correction = record_measurement(
            **{
                **params,
                "reference": "SYN-MEASURE-2",
                "clicks": 6,
                "supersedes": first,
                "correction_reason": "Ekspor sumber sebelumnya tertunda",
            }
        )
        self.assertEqual(correction.supersedes_id, first.pk)
        first.refresh_from_db()
        self.assertEqual(first.clicks, 5)
        with self.assertRaises(ValidationError):
            record_measurement(
                **{
                    **params,
                    "reference": "SYN-UNKNOWN",
                    "impressions": None,
                    "clicks": None,
                }
            )

    def test_content_http_reuses_activity_and_displays_approval_and_measurement(self):
        self.accepted_work()
        self.client.force_login(self.staff)
        session = self.client.session
        session["organization_id"] = self.org.pk
        session.save()
        url = reverse("marketing:content_delivery", args=[self.work.pk])
        self.assertContains(self.client.get(url), "Catat publikasi")
        response = self.client.post(
            url,
            {
                "operation": "publish",
                "expected_version": self.work.version,
                "expected_latest_id": "",
                "published_at": timezone.localtime().isoformat(),
                "url": "https://example.com/toefl",
                "topic": "TOEFL ITP",
                "evidence": "Arsip SYN-CONTENT",
            },
        )
        self.assertEqual(response.status_code, 302)
        page = self.client.get(url)
        self.assertContains(page, "https://example.com/toefl")
        self.assertContains(page, "workflow-owner")
        self.assertContains(page, "bukan pendapatan")
