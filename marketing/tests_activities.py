from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.utils import timezone

from core.models import AuditEvent, Membership, Organization
from finance.models import BankTransaction, Invoice, Journal

from . import activities, services
from .activity_models import WorkActivity


class MarketingActivityTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Activity controls")
        self.other = Organization.objects.create(name="Other activity company")
        for role in ("owner", "director", "finance", "marketing", "auditor"):
            user = get_user_model().objects.create_user(username=f"activity-{role}")
            Membership.objects.create(organization=self.org, user=user, role=role)
            setattr(self, role, user)
        self.day = timezone.localdate()
        self.member = services.create_member(
            organization=self.org,
            actor=self.marketing,
            name="PIC sales",
            discipline="sales",
        )
        from .workflow import configure_member

        configure_member(
            organization=self.org,
            actor=self.owner,
            member=self.member,
            expected_version=self.member.version,
            account=self.marketing,
            active=True,
            reason="Staff login assigned to PIC",
        )

    def create(self, actor=None, **fields):
        return activities.create_activity(
            organization=self.org,
            actor=actor or self.marketing,
            **{
                "title": "Follow-up peserta",
                "owner": self.member,
                "channel": "whatsapp",
                "kind": "follow_up",
                "due_date": self.day,
                "description": "Hubungi peserta",
                **fields,
            },
        )

    def change(self, item, operation, actor=None, **fields):
        return activities.update_activity(
            organization=self.org,
            actor=actor or self.marketing,
            activity=item,
            expected_version=item.version,
            operation=operation,
            **fields,
        )

    def submit(self, item, **fields):
        return self.change(
            item,
            "submit",
            **{
                "evidence": "Catatan WA CRM/37",
                "outcome": "Peserta meminta jadwal",
                "occurred_on": self.day,
                **fields,
            },
        )

    def test_marketing_submits_evidence_and_independent_owner_accepts(self):
        item = self.create()
        self.assertEqual((item.status, item.version), ("planned", 1))
        item = self.change(item, "start")
        self.assertEqual((item.status, item.version), ("in_progress", 2))
        item = self.submit(item)
        self.assertEqual(
            (item.status, item.submitted_by), ("submitted", self.marketing)
        )
        item = self.change(
            item,
            "accept",
            actor=self.owner,
            review_notes="Bukti dan hasil telah diperiksa",
        )
        self.assertEqual(
            (item.status, item.version, item.reviewed_by), ("accepted", 4, self.owner)
        )
        events = AuditEvent.objects.filter(
            object_type="WorkActivity", object_id=str(item.pk)
        ).order_by("pk")
        self.assertEqual(
            list(events.values_list("action", flat=True)),
            [
                "marketing.activity.created",
                "marketing.activity.started",
                "marketing.activity.submitted",
                "marketing.activity.accepted",
            ],
        )
        accepted = events.last()
        self.assertEqual(accepted.actor, self.owner)
        self.assertEqual(accepted.detail["before"]["status"], "submitted")
        self.assertEqual(accepted.detail["after"]["status"], "accepted")
        self.assertEqual(accepted.detail["after"]["evidence"], "Catatan WA CRM/37")
        self.assertFalse(Invoice.objects.exists())
        self.assertFalse(Journal.objects.exists())
        self.assertFalse(BankTransaction.objects.exists())

    def test_submission_requires_nonblank_evidence_outcome_and_past_or_today_date(self):
        item = self.create()
        for field, invalid in (
            ("evidence", " "),
            ("evidence", "x" * 4001),
            ("evidence", 0),
            ("outcome", ""),
            ("outcome", "unsafe\x00text"),
            ("occurred_on", None),
            ("occurred_on", False),
            ("occurred_on", "02/10/2026"),
            ("occurred_on", "2026-02-30"),
            ("occurred_on", self.day + timedelta(days=1)),
            ("occurred_on", timezone.now()),
        ):
            with (
                self.subTest(field=field, invalid=invalid),
                self.assertRaises(ValidationError),
            ):
                self.submit(item, **{field: invalid})
        item.refresh_from_db()
        self.assertEqual((item.status, item.version), ("planned", 1))
        self.assertEqual(
            AuditEvent.objects.filter(object_type="WorkActivity").count(), 1
        )

    def test_review_requires_separate_submitter_and_records_reason(self):
        item = self.submit(self.create())
        for actor in (self.marketing, self.finance, self.auditor):
            for operation in ("accept", "return"):
                with (
                    self.subTest(actor=actor.username, operation=operation),
                    self.assertRaises(PermissionDenied),
                ):
                    self.change(item, operation, actor=actor, review_notes="Diperiksa")
        for operation in ("accept", "return"):
            with self.assertRaises(ValidationError):
                self.change(item, operation, actor=self.owner, review_notes=" ")
        owner_created = self.submit(self.create(actor=self.owner, title="Dibuat owner"))
        reviewed_plan = self.change(
            owner_created,
            "accept",
            actor=self.owner,
            review_notes="Bukti tim telah diperiksa",
        )
        self.assertEqual(
            (
                reviewed_plan.created_by,
                reviewed_plan.submitted_by,
                reviewed_plan.reviewed_by,
            ),
            (self.owner, self.marketing, self.owner),
        )
        owner_submitted = self.submit(
            self.create(title="Diajukan owner"),
            actor=self.owner,
            responsibility_reason="Supervisor handles exception",
        )
        with self.assertRaises(PermissionDenied):
            self.change(
                owner_submitted, "accept", actor=self.owner, review_notes="Sendiri"
            )
        reviewed = self.change(
            owner_submitted,
            "accept",
            actor=self.director,
            review_notes="Pemeriksa lain",
        )
        self.assertEqual(reviewed.status, "accepted")

    def test_return_resubmit_keeps_prior_evidence_and_complete_correction_history(self):
        item = self.submit(self.create())
        item = self.change(
            item, "return", actor=self.owner, review_notes="Lengkapi tanggal dan hasil"
        )
        old_evidence = item.evidence
        item = self.change(item, "start")
        self.assertEqual(item.evidence, old_evidence)
        item = self.submit(
            item, evidence="CRM/37 dan tiket/81", outcome="Jadwal sudah dikirim"
        )
        self.assertIsNone(item.reviewed_by)
        self.assertEqual(item.review_notes, "")
        event = AuditEvent.objects.filter(
            action="marketing.activity.submitted", object_id=str(item.pk)
        ).first()
        self.assertEqual(event.detail["before"]["evidence"], old_evidence)
        self.assertEqual(
            event.detail["before"]["review_notes"], "Lengkapi tanggal dan hasil"
        )
        self.assertEqual(event.detail["after"]["evidence"], "CRM/37 dan tiket/81")
        self.assertEqual(event.detail["after"]["outcome"], "Jadwal sudah dikirim")
        item = self.change(
            item, "accept", actor=self.owner, review_notes="Perbaikan sesuai"
        )
        self.assertEqual(item.status, "accepted")

    def test_stale_doubleclick_and_wrong_status_leave_one_transition(self):
        item = self.create()
        first = self.change(item, "start")
        with self.assertRaises(ValidationError):
            self.change(item, "start")
        with self.assertRaises(ValidationError):
            self.change(first, "start")
        with self.assertRaises(ValidationError):
            self.change(
                first, "accept", actor=self.owner, review_notes="Belum diajukan"
            )
        self.assertEqual(
            AuditEvent.objects.filter(action="marketing.activity.started").count(), 1
        )
        for version in (None, True, "2", 0, 999):
            with self.subTest(version=version), self.assertRaises(ValidationError):
                activities.update_activity(
                    organization=self.org,
                    actor=self.marketing,
                    activity=first,
                    expected_version=version,
                    operation="cancel",
                    review_notes="Tidak sah",
                )
        first.refresh_from_db()
        self.assertEqual((first.status, first.version), ("in_progress", 2))

    def test_accept_and_cancel_are_terminal_with_no_direct_bulk_or_delete_bypass(self):
        accepted = self.change(
            self.submit(self.create()),
            "accept",
            actor=self.owner,
            review_notes="Sesuai",
        )
        cancelled = self.change(
            self.create(title="Tidak dijalankan"),
            "cancel",
            review_notes="Jadwal berubah",
        )
        for item in (accepted, cancelled):
            for operation in ("start", "submit", "accept", "return", "cancel"):
                with (
                    self.subTest(status=item.status, operation=operation),
                    self.assertRaises(ValidationError),
                ):
                    self.change(item, operation, actor=self.owner, review_notes="Ubah")
            item.status = "planned"
            with self.assertRaises(ValidationError):
                item.save()
            with self.assertRaises(ValidationError):
                item.delete()
            with self.assertRaises(ValidationError):
                WorkActivity.objects.filter(pk=item.pk).update(status="planned")
            with self.assertRaises(ValidationError):
                WorkActivity.objects.filter(pk=item.pk).delete()
        self.assertEqual(WorkActivity.objects.count(), 2)

    def test_origin_and_submission_cannot_be_silently_rewritten(self):
        item = self.create()
        for field, value in (
            ("title", "Diganti"),
            ("owner", self.member),
            ("kind", "content"),
            ("due_date", self.day),
            ("status", "accepted"),
            ("created_by", self.owner),
            ("evidence", "Dihapus"),
        ):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                self.change(item, "start", **{field: value})
        item.title = "Ubah tanpa audit"
        with self.assertRaises(ValidationError):
            item.save()
        item._service_write = True
        with self.assertRaises(ValidationError):
            item.save()
        with self.assertRaises(ValidationError):
            self.create(status="accepted")
        with self.assertRaises(ValidationError):
            WorkActivity.objects.create(
                organization=self.org,
                created_by=self.owner,
                title="Bypass",
                owner=self.member,
                channel="whatsapp",
                kind="follow_up",
                due_date=self.day,
            )

    def test_cancel_needs_reason_and_preserves_returned_submission(self):
        item = self.submit(self.create())
        with self.assertRaises(ValidationError):
            self.change(item, "cancel", review_notes="Masih menunggu review")
        item = self.change(
            item, "return", actor=self.owner, review_notes="Periksa ulang"
        )
        with self.assertRaises(ValidationError):
            self.change(item, "cancel")
        item = self.change(
            item, "cancel", review_notes="Peserta membatalkan permintaan"
        )
        self.assertEqual(item.evidence, "Catatan WA CRM/37")
        self.assertEqual(item.outcome, "Peserta meminta jadwal")
        self.assertEqual(item.status, "cancelled")
        self.assertIsNone(item.reviewed_by)
        self.assertIsNone(item.reviewed_at)
        event = AuditEvent.objects.get(action="marketing.activity.cancelled")
        self.assertEqual(
            event.detail["after"]["review_notes"], "Peserta membatalkan permintaan"
        )

    def test_activity_requires_active_scoped_owner_and_consistent_lead_campaign(self):
        Membership.objects.create(
            organization=self.other, user=self.owner, role="owner"
        )
        foreign_member = services.create_member(
            organization=self.other,
            actor=self.owner,
            name="Other PIC",
            discipline="sales",
        )
        inactive_member = services.create_member(
            organization=self.org,
            actor=self.owner,
            name="Inactive PIC",
            discipline="sales",
            active=False,
        )
        for owner in (None, foreign_member, inactive_member):
            with self.subTest(owner=owner), self.assertRaises(ValidationError):
                self.create(owner=owner)
        campaign = services.create_campaign(
            organization=self.org,
            actor=self.marketing,
            name="WA campaign",
            channel="whatsapp",
        )
        lead = services.create_lead(
            organization=self.org,
            actor=self.marketing,
            name="Prospek WA",
            reference="ACTIVITY-LEAD",
            received_at=timezone.now() - timedelta(days=2),
            channel="whatsapp",
            campaign=campaign,
        )
        with self.assertRaises(ValidationError):
            self.create(campaign=campaign, channel="seo")
        with self.assertRaises(ValidationError):
            self.create(lead=lead)
        item = self.submit(self.create(campaign=campaign, lead=lead))
        self.assertEqual(item.lead_id, lead.pk)
        lead.refresh_from_db()
        self.assertEqual((lead.stage, lead.version), ("new", 1))
        self.assertIsNone(lead.first_response_at)
        self.assertEqual(lead.stage_events.count(), 1)
        foreign_lead = services.create_lead(
            organization=self.other,
            actor=self.owner,
            name="Foreign",
            reference="FOREIGN",
            received_at=timezone.now() - timedelta(days=2),
            channel="whatsapp",
        )
        with self.assertRaises(ValidationError):
            self.create(lead=foreign_lead)
        with self.assertRaises(ValidationError):
            activities.update_activity(
                organization=self.other,
                actor=self.owner,
                activity=item,
                expected_version=item.version,
                operation="accept",
                review_notes="Wrong company",
            )

    def test_inactive_revoked_unauthorized_accounts_cannot_create_or_update(self):
        item = self.create()
        for actor in (self.auditor, None):
            with self.subTest(actor=actor), self.assertRaises(PermissionDenied):
                activities.update_activity(
                    organization=self.org,
                    actor=actor,
                    activity=item,
                    expected_version=item.version,
                    operation="start",
                )
        with self.assertRaises(PermissionDenied):
            self.create(actor=self.auditor)
        self.marketing.is_active = False
        self.marketing.save(update_fields=["is_active"])
        with self.assertRaises(PermissionDenied):
            self.create()
        with self.assertRaises(PermissionDenied):
            self.change(item, "start")
        Membership.all_objects.filter(organization=self.org, user=self.finance).update(
            is_active=False
        )
        with self.assertRaises(PermissionDenied):
            self.create(actor=self.finance)

    def test_failed_audit_rolls_back_creation_and_transition(self):
        with (
            patch(
                "marketing.activities._audit",
                side_effect=ValidationError("Audit unavailable"),
            ),
            self.assertRaises(ValidationError),
        ):
            self.create()
        self.assertFalse(WorkActivity.objects.exists())
        item = self.create()
        with (
            patch(
                "marketing.activities._audit",
                side_effect=ValidationError("Audit unavailable"),
            ),
            self.assertRaises(ValidationError),
        ):
            self.submit(item)
        item.refresh_from_db()
        self.assertEqual((item.status, item.version, item.evidence), ("planned", 1, ""))
        self.assertIsNone(item.submitted_by)
        self.assertEqual(
            AuditEvent.objects.filter(object_type="WorkActivity").count(), 1
        )

    def test_submitter_can_withdraw_preserving_evidence_then_staff_resubmits_for_owner(
        self,
    ):
        item = self.submit(
            self.create(actor=self.owner),
            actor=self.owner,
            responsibility_reason="Supervisor handles exception",
        )
        submitted_at = item.submitted_at
        item = self.change(
            item,
            "withdraw",
            actor=self.owner,
            review_notes="Penugasan perlu dilaksanakan dan diajukan staf",
        )
        self.assertEqual((item.status, item.version), ("in_progress", 3))
        self.assertEqual(item.submitted_by, self.owner)
        self.assertEqual(item.submitted_at, submitted_at)
        self.assertEqual(item.evidence, "Catatan WA CRM/37")
        self.assertEqual(item.outcome, "Peserta meminta jadwal")
        event = AuditEvent.objects.get(action="marketing.activity.withdrawn")
        self.assertEqual(event.actor, self.owner)
        self.assertEqual(event.detail["before"]["status"], "submitted")
        self.assertEqual(event.detail["after"]["status"], "in_progress")
        self.assertEqual(
            event.detail["after"]["review_notes"],
            "Penugasan perlu dilaksanakan dan diajukan staf",
        )
        item = self.submit(
            item,
            evidence="Arsip tindak lanjut staf/81",
            outcome="Staf mengirim jadwal baru",
        )
        self.assertEqual(item.submitted_by, self.marketing)
        self.assertEqual(item.review_notes, "")
        item = self.change(
            item, "accept", actor=self.owner, review_notes="Bukti staf sesuai"
        )
        self.assertEqual(item.status, "accepted")

    def test_withdrawal_requires_actual_submitter_reason_and_current_submitted_state(
        self,
    ):
        planned = self.create()
        with self.assertRaises(ValidationError):
            self.change(planned, "withdraw", review_notes="Belum pernah diajukan")
        item = self.submit(planned)
        for actor in (self.owner, self.director, self.finance):
            with self.subTest(actor=actor), self.assertRaises(PermissionDenied):
                self.change(item, "withdraw", actor=actor, review_notes="Bukan pengaju")
        with self.assertRaises(ValidationError):
            self.change(item, "withdraw", review_notes=" ")
        with self.assertRaises(ValidationError):
            self.change(
                item,
                "withdraw",
                review_notes="Tarik",
                evidence="Tidak boleh menimpa bukti",
            )
        withdrawn = self.change(item, "withdraw", review_notes="Perbaiki bukti dahulu")
        with self.assertRaises(ValidationError):
            self.change(item, "withdraw", review_notes="Klik ganda dengan versi lama")
        with self.assertRaises(ValidationError):
            self.change(withdrawn, "withdraw", review_notes="Tidak lagi diajukan")
        accepted = self.change(
            self.submit(withdrawn),
            "accept",
            actor=self.owner,
            review_notes="Sudah sesuai",
        )
        with self.assertRaises(ValidationError):
            self.change(accepted, "withdraw", review_notes="Sudah diterima")
        cancelled = self.change(
            self.create(title="Dibatalkan"), "cancel", review_notes="Tidak diperlukan"
        )
        with self.assertRaises(ValidationError):
            self.change(cancelled, "withdraw", review_notes="Sudah dibatalkan")
        self.assertEqual(
            AuditEvent.objects.filter(action="marketing.activity.withdrawn").count(), 1
        )
