"""Unfinished marketing work can move to a replacement without losing its origin."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import AuditEvent, Membership

from . import activities, services, workflow
from . import tests_activities as fixtures


class WorkHandoffTests(TestCase):
    def setUp(self):
        fixtures.MarketingActivityTests.setUp(self)
        self.member.refresh_from_db()

    create = fixtures.MarketingActivityTests.create
    change = fixtures.MarketingActivityTests.change
    submit = fixtures.MarketingActivityTests.submit

    def replacement(self):
        user = get_user_model().objects.create_user(username="handoff-replacement")
        Membership.objects.create(organization=self.org, user=user, role="marketing")
        member = services.create_member(
            organization=self.org,
            actor=self.owner,
            name="Replacement staff",
            discipline="sales",
        )
        member = workflow.configure_member(
            organization=self.org,
            actor=self.owner,
            member=member,
            expected_version=member.version,
            account=user,
            active=True,
            reason="Replacement staff login verified",
        )
        return user, member

    def test_owner_hands_off_unfinished_work_from_inactive_pic_without_changing_origin(
        self,
    ):
        item = self.create()
        user, replacement = self.replacement()
        workflow.configure_member(
            organization=self.org,
            actor=self.owner,
            member=self.member,
            expected_version=self.member.version,
            account=self.marketing,
            active=False,
            reason="Original staff has left",
        )

        handed = activities.handoff_activity(
            organization=self.org,
            actor=self.owner,
            activity=item,
            expected_version=item.version,
            member=replacement,
            reason="Replacement continues the existing task",
        )

        self.assertEqual(handed.owner, self.member)
        self.assertEqual(handed.current_owner, replacement)
        self.assertEqual(handed.created_by, self.marketing)
        self.assertEqual((handed.status, handed.version), ("planned", 2))
        self.assertEqual(
            list(
                workflow.daily_work(organization=self.org, actor=user, queue="work")[
                    "rows"
                ]
            ),
            [handed],
        )
        self.assertEqual(
            list(
                workflow.daily_work(
                    organization=self.org, actor=self.marketing, queue="work"
                )["rows"]
            ),
            [],
        )
        started = activities.update_activity(
            organization=self.org,
            actor=user,
            activity=handed,
            expected_version=handed.version,
            operation="start",
        )
        self.assertEqual(started.status, "in_progress")

    @override_settings(MFA_REQUIRED=False)
    def test_owner_uses_simple_handoff_form_then_replacement_sees_work_and_history(
        self,
    ):
        item = self.create()
        user, replacement = self.replacement()
        self.client.force_login(self.owner)
        page = self.client.get(reverse("marketing:activity_detail", args=[item.pk]))
        url = reverse("marketing:work_handoff", args=[item.pk])
        self.assertContains(page, url)
        form_page = self.client.get(url)
        self.assertContains(form_page, "Pilih staf pengganti")
        self.assertEqual(
            set(form_page.context["form"].fields),
            {"member", "reason", "expected_version"},
        )
        response = self.client.post(
            url,
            {
                "member": replacement.pk,
                "reason": "Staf pengganti melanjutkan pekerjaan",
                "expected_version": item.version,
            },
        )
        self.assertRedirects(
            response,
            reverse("marketing:activity_detail", args=[item.pk]),
            fetch_redirect_response=False,
        )
        self.client.force_login(user)
        queue = self.client.get(reverse("marketing:daily"), {"queue": "work"})
        self.assertContains(queue, item.title)
        self.assertContains(queue, "Replacement staff")
        detail = self.client.get(reverse("marketing:activity_detail", args=[item.pk]))
        self.assertContains(detail, "Penanggung jawab saat ini")
        self.assertContains(detail, "PIC saat tugas dibuat")
        self.assertContains(detail, "Staf pengganti melanjutkan pekerjaan")
        self.assertNotContains(detail, url)
        self.assertContains(detail, 'name="responsibility_reason"', html=False)
        self.assertEqual(
            detail.context["submit_form"]
            .fields["responsibility_reason"]
            .widget.input_type,
            "hidden",
        )

    def test_handoff_keeps_returned_evidence_and_history_then_new_staff_resubmits(self):
        item = self.change(
            self.submit(self.create()),
            "return",
            actor=self.owner,
            review_notes="Lengkapi bukti",
        )
        user, replacement = self.replacement()
        handed = activities.handoff_activity(
            organization=self.org,
            actor=self.director,
            activity=item,
            expected_version=item.version,
            member=replacement,
            reason="Staf baru menyelesaikan revisi",
        )
        self.assertEqual(
            (
                handed.evidence,
                handed.outcome,
                handed.submitted_by,
                handed.reviewed_by,
                handed.status,
            ),
            (
                "Catatan WA CRM/37",
                "Peserta meminta jadwal",
                self.marketing,
                self.owner,
                "needs_revision",
            ),
        )
        event = handed.assignment_events.get()
        self.assertEqual(
            (
                event.previous_member,
                event.member,
                event.created_by,
                event.activity_version,
            ),
            (self.member, replacement, self.director, 4),
        )
        audit = AuditEvent.objects.get(action="marketing.activity.handed_off")
        self.assertEqual(audit.detail["before"]["evidence"], "Catatan WA CRM/37")
        self.assertEqual(audit.detail["after"]["owner"], self.member.pk)
        self.assertEqual(audit.detail["after"]["current_owner"], replacement.pk)
        with self.assertRaises(PermissionDenied):
            self.change(handed, "start")
        submitted = self.submit(
            handed,
            actor=user,
            evidence="Bukti revisi baru",
            outcome="Staf pengganti menyelesaikan revisi",
        )
        self.assertEqual(submitted.submitted_by, user)
        self.assertEqual(submitted.owner, self.member)
        self.assertEqual(event.reason, "Staf baru menyelesaikan revisi")

    def test_handoff_requires_director_current_version_active_mapped_company_recipient_and_reason(
        self,
    ):
        item = self.create()
        user, replacement = self.replacement()
        command = dict(
            organization=self.org,
            activity=item,
            expected_version=item.version,
            member=replacement,
            reason="Staf baru mengambil tanggung jawab",
        )
        for actor in (self.marketing, self.finance, self.auditor, None):
            with self.subTest(actor=actor), self.assertRaises(PermissionDenied):
                activities.handoff_activity(actor=actor, **command)
        for change in (
            {"reason": " "},
            {"reason": "bad\x00reason"},
            {"expected_version": 0},
            {"member": self.member},
            {"member": True},
        ):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                activities.handoff_activity(actor=self.owner, **(command | change))
        unmapped = services.create_member(
            organization=self.org,
            actor=self.owner,
            name="No login",
            discipline="content",
        )
        with self.assertRaises(ValidationError):
            activities.handoff_activity(
                actor=self.owner, **(command | {"member": unmapped})
            )
        Membership.objects.create(
            organization=self.other, user=self.owner, role="owner"
        )
        foreign = services.create_member(
            organization=self.other,
            actor=self.owner,
            name="Other company",
            discipline="sales",
        )
        with self.assertRaises(ValidationError):
            activities.handoff_activity(
                actor=self.owner, **(command | {"member": foreign})
            )
        workflow.configure_member(
            organization=self.org,
            actor=self.owner,
            member=replacement,
            expected_version=replacement.version,
            account=user,
            active=False,
            reason="Replacement no longer active",
        )
        with self.assertRaises(ValidationError):
            activities.handoff_activity(actor=self.owner, **command)
        replacement.refresh_from_db()
        workflow.configure_member(
            organization=self.org,
            actor=self.owner,
            member=replacement,
            expected_version=replacement.version,
            account=user,
            active=True,
            reason="Replacement active again",
        )
        Membership.all_objects.filter(organization=self.org, user=user).update(
            is_active=False
        )
        with self.assertRaises(ValidationError):
            activities.handoff_activity(actor=self.owner, **command)
        item.refresh_from_db()
        self.assertEqual((item.assigned_owner, item.version), (self.member, 1))
        self.assertFalse(item.assignment_events.exists())
        self.assertFalse(
            AuditEvent.objects.filter(action="marketing.activity.handed_off").exists()
        )

    def test_only_unfinished_states_can_move_and_stale_repeat_cannot_add_history(self):
        user, replacement = self.replacement()
        unfinished = self.change(self.create(), "start")
        handed = activities.handoff_activity(
            organization=self.org,
            actor=self.director,
            activity=unfinished,
            expected_version=unfinished.version,
            member=replacement,
            reason="Bagi ulang pekerjaan berjalan",
        )
        self.assertEqual((handed.status, handed.version), ("in_progress", 3))
        with self.assertRaises(ValidationError):
            activities.handoff_activity(
                organization=self.org,
                actor=self.director,
                activity=handed,
                expected_version=2,
                member=self.member,
                reason="Tab lama tidak boleh menimpa staf baru",
            )
        self.assertEqual(handed.assignment_events.count(), 1)
        submitted = self.submit(self.create(title="Sedang diperiksa"))
        accepted = self.change(
            self.submit(self.create(title="Sudah diterima")),
            "accept",
            actor=self.owner,
            review_notes="Sesuai",
        )
        cancelled = self.change(
            self.create(title="Sudah dibatalkan"),
            "cancel",
            review_notes="Tidak diperlukan",
        )
        for item in (submitted, accepted, cancelled):
            with self.subTest(status=item.status), self.assertRaises(ValidationError):
                activities.handoff_activity(
                    organization=self.org,
                    actor=self.owner,
                    activity=item,
                    expected_version=item.version,
                    member=replacement,
                    reason="Status ini tidak boleh dialihkan",
                )
            self.assertFalse(item.assignment_events.exists())

    def test_audit_failure_rolls_back_assignment_and_permanent_history_cannot_be_modified(
        self,
    ):
        item = self.create()
        _, replacement = self.replacement()
        command = dict(
            organization=self.org,
            actor=self.owner,
            activity=item,
            expected_version=item.version,
            member=replacement,
            reason="Alih tugas dengan histori",
        )
        with (
            patch(
                "marketing.activities._audit",
                side_effect=ValidationError("Audit unavailable"),
            ),
            self.assertRaises(ValidationError),
        ):
            activities.handoff_activity(**command)
        item.refresh_from_db()
        self.assertEqual((item.assigned_owner, item.version), (self.member, 1))
        self.assertFalse(item.assignment_events.exists())
        handed = activities.handoff_activity(**command)
        event = handed.assignment_events.get()
        with self.assertRaises(ValidationError):
            event.delete()
        event.reason = "Rewrite history"
        with self.assertRaises(ValidationError):
            event.save()
        with self.assertRaises(ValidationError):
            handed.owner = replacement
            handed.save()
        event.refresh_from_db()
        self.assertEqual(event.reason, "Alih tugas dengan histori")

    @override_settings(MFA_REQUIRED=False)
    def test_handoff_route_rejects_staff_foreign_work_and_invalid_recipient_without_changes(
        self,
    ):
        item = self.create()
        _, replacement = self.replacement()
        url = reverse("marketing:work_handoff", args=[item.pk])
        data = {
            "member": replacement.pk,
            "reason": "Alih tugas",
            "expected_version": item.version,
        }
        for actor in (self.marketing, self.finance, self.auditor):
            self.client.force_login(actor)
            self.assertEqual(self.client.get(url).status_code, 403)
            self.assertEqual(self.client.post(url, data).status_code, 403)
        self.client.force_login(self.owner)
        self.assertEqual(
            self.client.get(
                reverse("marketing:work_handoff", args=[999999])
            ).status_code,
            404,
        )
        bad = self.client.post(url, data | {"member": self.member.pk})
        self.assertEqual(bad.status_code, 200)
        self.assertIn("member", bad.context["form"].errors)
        self.assertContains(bad, "Alih tugas")
        item.refresh_from_db()
        self.assertEqual((item.assigned_owner, item.version), (self.member, 1))
        self.assertFalse(item.assignment_events.exists())

    @override_settings(MFA_REQUIRED=False)
    def test_activity_filter_follows_current_pic_and_historical_owner_is_still_visible(
        self,
    ):
        item = self.create()
        _, replacement = self.replacement()
        activities.handoff_activity(
            organization=self.org,
            actor=self.owner,
            activity=item,
            expected_version=item.version,
            member=replacement,
            reason="Tanggung jawab dialihkan",
        )
        self.client.force_login(self.owner)
        original = self.client.get(
            reverse("marketing:activities"), {"owner": self.member.pk}
        )
        current = self.client.get(
            reverse("marketing:activities"), {"owner": replacement.pk}
        )
        self.assertEqual(original.context["page"].paginator.count, 0)
        self.assertEqual(current.context["page"].paginator.count, 1)
        self.assertContains(current, "Replacement staff")
        detail = self.client.get(reverse("marketing:activity_detail", args=[item.pk]))
        self.assertContains(detail, "PIC sales")
        self.assertContains(detail, "Tanggung jawab dialihkan")

    def test_handoff_preserves_private_evidence_and_new_staff_can_add_the_next_version(
        self,
    ):
        from io import BytesIO
        from tempfile import TemporaryDirectory

        from django.core.files.uploadedfile import SimpleUploadedFile
        from PIL import Image

        from .work_evidence import (
            active_evidence_manifest,
            attach_work_evidence,
            checked_evidence_bytes,
        )

        item = self.create()
        user, replacement = self.replacement()
        data = BytesIO()
        Image.new("RGB", (2, 2), "white").save(data, format="PNG")
        with TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=directory):
            proof = attach_work_evidence(
                organization=self.org,
                actor=self.marketing,
                activity=item,
                expected_version=item.version,
                upload=SimpleUploadedFile("original.png", data.getvalue()),
            )
            item.refresh_from_db()
            before = active_evidence_manifest(item)
            handed = activities.handoff_activity(
                organization=self.org,
                actor=self.owner,
                activity=item,
                expected_version=item.version,
                member=replacement,
                reason="Staf pengganti melengkapi bukti",
            )
            self.assertEqual(active_evidence_manifest(handed), before)
            self.assertEqual(checked_evidence_bytes(proof), data.getvalue())
            with self.assertRaises(PermissionDenied):
                attach_work_evidence(
                    organization=self.org,
                    actor=self.marketing,
                    activity=handed,
                    expected_version=handed.version,
                    upload=SimpleUploadedFile("old-pic.png", data.getvalue()),
                )
            following = attach_work_evidence(
                organization=self.org,
                actor=user,
                activity=handed,
                expected_version=handed.version,
                upload=SimpleUploadedFile("replacement.png", data.getvalue()),
            )
            self.assertEqual(
                (following.version, following.created_by, proof.created_by),
                (2, user, self.marketing),
            )
            self.assertEqual(handed.owner, self.member)
