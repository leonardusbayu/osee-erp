"""Public workflow commands and HTTP journeys for accountable daily marketing."""

from datetime import datetime, time, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import Membership, Organization

from . import services, workflow


class MarketingWorkflowTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Synthetic workflow company")
        self.owner = get_user_model().objects.create_user(username="workflow-owner")
        self.staff = get_user_model().objects.create_user(username="workflow-staff")
        self.other_staff = get_user_model().objects.create_user(
            username="workflow-cover"
        )
        for user, role in (
            (self.owner, "owner"),
            (self.staff, "marketing"),
            (self.other_staff, "marketing"),
        ):
            Membership.objects.create(organization=self.org, user=user, role=role)
        self.member = services.create_member(
            organization=self.org,
            actor=self.owner,
            name="Sales One",
            discipline="sales",
        )

    def lead(self, **fields):
        return services.create_lead(
            organization=self.org,
            actor=self.owner,
            reference="WORKFLOW-001",
            name="Synthetic prospect",
            received_at=timezone.now() - timedelta(days=1),
            channel="sales",
            **fields,
        )

    def test_owner_maps_login_and_assigns_unassigned_lead_without_rewriting_original_credit(
        self,
    ):
        mapped = workflow.configure_member(
            organization=self.org,
            actor=self.owner,
            member=self.member,
            expected_version=1,
            account=self.staff,
            active=True,
            reason="Akun pribadi PIC sudah diverifikasi",
        )
        lead = self.lead()
        assigned = workflow.assign_lead(
            organization=self.org,
            actor=self.owner,
            lead=lead,
            expected_version=1,
            member=mapped,
            reason="Prospek baru dibagikan ke tim sales",
        )
        self.assertEqual(assigned.current_owner, mapped)
        self.assertIsNone(assigned.owner)
        self.assertEqual(assigned.assignment_events.count(), 1)
        self.assertTrue(
            workflow.can_act_for_member(
                organization=self.org, actor=self.staff, member=mapped
            )
        )
        self.assertFalse(
            workflow.can_act_for_member(
                organization=self.org, actor=self.other_staff, member=mapped
            )
        )

    def test_explicit_delegation_is_bounded_revocable_and_cannot_be_granted_by_staff(
        self,
    ):
        member = workflow.configure_member(
            organization=self.org,
            actor=self.owner,
            member=self.member,
            expected_version=1,
            account=self.staff,
            active=True,
            reason="Pemetaan akun",
        )
        today = timezone.localdate()
        with self.assertRaises(PermissionDenied):
            workflow.grant_delegation(
                organization=self.org,
                actor=self.staff,
                member=member,
                expected_version=member.version,
                delegate=self.other_staff,
                starts_on=today,
                ends_on=today,
                reason="Coba mengalihkan sendiri",
            )
        delegation = workflow.grant_delegation(
            organization=self.org,
            actor=self.owner,
            member=member,
            expected_version=member.version,
            delegate=self.other_staff,
            starts_on=today,
            ends_on=today,
            reason="Pengganti cuti hari ini",
        )
        self.assertTrue(
            workflow.can_act_for_member(
                organization=self.org, actor=self.other_staff, member=member
            )
        )
        self.assertFalse(
            workflow.can_act_for_member(
                organization=self.org,
                actor=self.other_staff,
                member=member,
                at=today + timedelta(days=1),
            )
        )
        workflow.revoke_delegation(
            organization=self.org,
            actor=self.owner,
            delegation=delegation,
            expected_version=1,
            reason="PIC sudah kembali",
        )
        self.assertFalse(
            workflow.can_act_for_member(
                organization=self.org, actor=self.other_staff, member=member
            )
        )

    def test_mapping_rejects_foreign_account_and_reassignment_preserves_acquisition_owner(
        self,
    ):
        foreign_org = Organization.objects.create(name="Other synthetic company")
        foreign_user = get_user_model().objects.create_user(username="workflow-foreign")
        Membership.objects.create(
            organization=foreign_org, user=foreign_user, role="marketing"
        )
        with self.assertRaises(ValidationError):
            workflow.configure_member(
                organization=self.org,
                actor=self.owner,
                member=self.member,
                expected_version=1,
                account=foreign_user,
                active=True,
                reason="Tidak boleh lintas perusahaan",
            )
        member = workflow.configure_member(
            organization=self.org,
            actor=self.owner,
            member=self.member,
            expected_version=1,
            account=self.staff,
            active=True,
            reason="Akun benar",
        )
        lead = self.lead(owner=member)
        replacement = services.create_member(
            organization=self.org,
            actor=self.owner,
            name="Replacement",
            discipline="sales",
        )
        changed = workflow.assign_lead(
            organization=self.org,
            actor=self.owner,
            lead=lead,
            expected_version=lead.version,
            member=replacement,
            reason="Alih tugas karena cuti",
        )
        self.assertEqual(changed.current_owner, replacement)
        self.assertEqual(changed.owner, member)
        with self.assertRaises(ValidationError):
            workflow.assign_lead(
                organization=self.org,
                actor=self.owner,
                lead=changed,
                expected_version=1,
                member=member,
                reason="Layar lama tidak boleh menimpa",
            )

    def test_contacts_keep_history_and_deadline_and_require_a_reason_to_correct_first_response(
        self,
    ):
        member = workflow.configure_member(
            organization=self.org,
            actor=self.owner,
            member=self.member,
            expected_version=1,
            account=self.staff,
            active=True,
            reason="Pemetaan akun",
        )
        lead = self.lead(owner=member)
        response = timezone.now() - timedelta(hours=1)
        deadline = timezone.now() + timedelta(days=1)
        event = workflow.record_contact(
            organization=self.org,
            actor=self.staff,
            lead=lead,
            expected_version=1,
            occurred_at=response,
            method="whatsapp",
            outcome="responded",
            summary="Kebutuhan tes dikonfirmasi",
            evidence="Percakapan internal TEST-1",
            first_response=True,
            followup_action="set",
            next_due_at=deadline,
        )
        lead.refresh_from_db()
        self.assertEqual(lead.first_response_at, response)
        self.assertEqual(lead.next_follow_up_at, deadline)
        self.assertEqual(event.created_by, self.staff)
        with self.assertRaises(ValidationError):
            services.update_lead(
                organization=self.org,
                actor=self.staff,
                lead=lead,
                expected_version=lead.version,
                first_response_at=response - timedelta(minutes=1),
            )
        corrected = workflow.correct_first_response(
            organization=self.org,
            actor=self.staff,
            lead=lead,
            expected_version=lead.version,
            first_response_at=response - timedelta(minutes=1),
            reason="Waktu asli pada bukti percakapan berbeda satu menit",
            evidence="Arsip TEST-1 revisi waktu",
        )
        self.assertEqual(corrected.contact_events.count(), 2)
        self.assertEqual(
            corrected.contact_events.order_by("pk").first().occurred_at, response
        )

    def test_campaign_pause_resume_close_preserves_plan_and_blocks_late_ad_spend(self):
        today = timezone.localdate()
        planned_end = today + timedelta(days=10)
        campaign = services.create_campaign(
            organization=self.org,
            actor=self.owner,
            name="Synthetic October ads",
            channel="meta_ads",
            owner=self.member,
            start_date=today - timedelta(days=3),
            end_date=planned_end,
        )
        paused = workflow.transition_campaign(
            organization=self.org,
            actor=self.owner,
            campaign=campaign,
            expected_version=1,
            operation="pause",
            effective_on=today,
            reason="Periksa materi iklan",
        )
        self.assertEqual(paused.status, "paused")
        resumed = workflow.transition_campaign(
            organization=self.org,
            actor=self.owner,
            campaign=paused,
            expected_version=paused.version,
            operation="resume",
            effective_on=today,
            reason="Materi sudah diperbaiki",
        )
        closed = workflow.transition_campaign(
            organization=self.org,
            actor=self.owner,
            campaign=resumed,
            expected_version=resumed.version,
            operation="complete",
            effective_on=today,
            reason="Anggaran periode selesai digunakan",
        )
        self.assertEqual(closed.status, "completed")
        self.assertEqual(closed.end_date, planned_end)
        self.assertEqual(closed.actual_end_date, today)
        self.assertEqual(closed.lifecycle_events.count(), 3)
        with self.assertRaises(ValidationError):
            services.save_ad_observation(
                organization=self.org,
                actor=self.owner,
                campaign=closed,
                date=today + timedelta(days=1),
                spend="1000",
                source="Synthetic platform",
                reference="LATE-AD",
            )

    def test_my_daily_queue_uses_current_assignment_and_explicit_delegation(self):
        member = workflow.configure_member(
            organization=self.org,
            actor=self.owner,
            member=self.member,
            expected_version=1,
            account=self.staff,
            active=True,
            reason="Pemetaan akun",
        )
        lead = self.lead(owner=member, next_follow_up=timezone.localdate())
        mine = workflow.daily_work(
            organization=self.org, actor=self.staff, scope="mine", queue="today"
        )
        self.assertEqual([item.pk for item in mine["rows"]], [lead.pk])
        cover = workflow.daily_work(
            organization=self.org, actor=self.other_staff, scope="mine", queue="today"
        )
        self.assertEqual(list(cover["rows"]), [])
        self.assertTrue(cover["mapping_needed"])
        workflow.grant_delegation(
            organization=self.org,
            actor=self.owner,
            member=member,
            expected_version=member.version,
            delegate=self.other_staff,
            starts_on=timezone.localdate(),
            ends_on=timezone.localdate(),
            reason="Pengganti terjadwal",
        )
        delegated = workflow.daily_work(
            organization=self.org, actor=self.other_staff, scope="mine", queue="today"
        )
        self.assertEqual([item.pk for item in delegated["rows"]], [lead.pk])

    def test_timed_followup_uses_jakarta_day_across_local_midnight(self):
        member = workflow.configure_member(
            organization=self.org,
            actor=self.owner,
            member=self.member,
            expected_version=1,
            account=self.staff,
            active=True,
            reason="Pemetaan akun",
        )
        evening = datetime(2026, 10, 9, 23, 30, tzinfo=ZoneInfo("Asia/Jakarta"))
        deadline = evening + timedelta(hours=1)
        with (
            timezone.override("Asia/Jakarta"),
            patch("django.utils.timezone.now", return_value=evening),
        ):
            lead = self.lead(owner=member)
            workflow.record_contact(
                organization=self.org,
                actor=self.staff,
                lead=lead,
                expected_version=1,
                occurred_at=evening,
                method="whatsapp",
                outcome="responded",
                summary="Jadwal kontak besok",
                evidence="Arsip SYNTHETIC-MIDNIGHT",
                first_response=True,
                followup_action="set",
                next_due_at=deadline.astimezone(ZoneInfo("UTC")),
            )
            lead.refresh_from_db()
            self.assertEqual(lead.next_follow_up, deadline.date())
            self.assertEqual(lead.next_follow_up_at, deadline)
            self.assertEqual(
                list(
                    workflow.daily_work(
                        organization=self.org,
                        actor=self.staff,
                        scope="mine",
                        queue="today",
                    )["rows"]
                ),
                [],
            )
            self.assertEqual(
                list(
                    workflow.daily_work(
                        organization=self.org,
                        actor=self.staff,
                        scope="mine",
                        queue="overdue",
                    )["rows"]
                ),
                [],
            )
        with (
            timezone.override("Asia/Jakarta"),
            patch(
                "django.utils.timezone.now",
                return_value=evening + timedelta(minutes=31),
            ),
        ):
            self.assertEqual(
                [
                    row.pk
                    for row in workflow.daily_work(
                        organization=self.org,
                        actor=self.staff,
                        scope="mine",
                        queue="today",
                    )["rows"]
                ],
                [lead.pk],
            )
            self.assertEqual(
                list(
                    workflow.daily_work(
                        organization=self.org,
                        actor=self.staff,
                        scope="mine",
                        queue="overdue",
                    )["rows"]
                ),
                [],
            )

    def test_approved_work_calendar_excludes_weekends_from_response_sla(self):
        member = workflow.configure_member(
            organization=self.org,
            actor=self.owner,
            member=self.member,
            expected_version=1,
            account=self.staff,
            active=True,
            reason="Pemetaan akun",
        )
        monday = datetime(2026, 9, 28, 9, 15, tzinfo=ZoneInfo("Asia/Jakarta"))
        with patch("django.utils.timezone.now", return_value=monday):
            workflow.configure_work_policy(
                organization=self.org,
                actor=self.owner,
                expected_version=0,
                response_limit_minutes=30,
                review_limit_minutes=120,
                workdays=[0, 1, 2, 3, 4],
                work_start=time(9),
                work_end=time(17),
                holidays=[],
                reason="Jam kerja dan SLA disepakati owner",
            )
            lead = services.create_lead(
                organization=self.org,
                actor=self.owner,
                reference="FRIDAY-EVENING",
                name="Friday prospect",
                received_at=datetime(2026, 9, 25, 18, tzinfo=ZoneInfo("Asia/Jakarta")),
                channel="sales",
                owner=member,
            )
            within = workflow.daily_work(
                organization=self.org,
                actor=self.staff,
                scope="mine",
                queue="response_sla",
            )
            self.assertEqual(list(within["rows"]), [])
        with patch(
            "django.utils.timezone.now", return_value=monday + timedelta(minutes=20)
        ):
            late = workflow.daily_work(
                organization=self.org,
                actor=self.staff,
                scope="mine",
                queue="response_sla",
            )
            self.assertEqual([row.pk for row in late["rows"]], [lead.pk])
            again = workflow.daily_work(
                organization=self.org,
                actor=self.staff,
                scope="mine",
                queue="response_sla",
            )
            self.assertEqual([row.pk for row in again["rows"]], [lead.pk])

    def test_mapping_change_revokes_old_delegation_and_reactivation_does_not_revive_it(
        self,
    ):
        member = workflow.configure_member(
            organization=self.org,
            actor=self.owner,
            member=self.member,
            expected_version=1,
            account=self.staff,
            active=True,
            reason="Pemetaan akun",
        )
        delegation = workflow.grant_delegation(
            organization=self.org,
            actor=self.owner,
            member=member,
            expected_version=member.version,
            delegate=self.other_staff,
            starts_on=timezone.localdate(),
            ends_on=timezone.localdate(),
            reason="Pengganti hari ini",
        )
        member.refresh_from_db()
        member = workflow.configure_member(
            organization=self.org,
            actor=self.owner,
            member=member,
            expected_version=member.version,
            account=self.staff,
            active=False,
            reason="Anggota berhenti bekerja",
        )
        member = workflow.configure_member(
            organization=self.org,
            actor=self.owner,
            member=member,
            expected_version=member.version,
            account=self.staff,
            active=True,
            reason="Kembali bekerja dengan pengaturan baru",
        )
        delegation.refresh_from_db()
        self.assertIsNotNone(delegation.revoked_at)
        self.assertFalse(
            workflow.can_act_for_member(
                organization=self.org, actor=self.other_staff, member=member
            )
        )

    def test_empty_response_correction_cannot_bypass_contact_evidence(self):
        lead = self.lead()
        with self.assertRaises(ValidationError):
            workflow.correct_first_response(
                organization=self.org,
                actor=self.owner,
                lead=lead,
                expected_version=1,
                first_response_at=timezone.now(),
                reason="Ini seharusnya kontak baru",
                evidence="Bukti kontak baru",
                responsibility_reason="Owner membantu triase",
            )
        lead.refresh_from_db()
        self.assertIsNone(lead.first_response_at)

    def test_completed_campaign_creation_requires_lifecycle_instead_of_bypassing_closure(
        self,
    ):
        with self.assertRaises(ValidationError):
            services.create_campaign(
                organization=self.org,
                actor=self.owner,
                name="Bypass closure",
                channel="seo",
                status="completed",
            )

    def test_generic_lead_updates_cannot_bypass_assignment_or_supervisor_reason(self):
        member = workflow.configure_member(
            organization=self.org,
            actor=self.owner,
            member=self.member,
            expected_version=1,
            account=self.staff,
            active=True,
            reason="Pemetaan akun",
        )
        lead = self.lead(owner=member)
        with self.assertRaises(PermissionDenied):
            services.update_lead(
                organization=self.org,
                actor=self.other_staff,
                lead=lead,
                expected_version=1,
                stage="won",
                notes="Bukan pekerjaan saya",
            )
        with self.assertRaises(ValidationError):
            services.update_lead(
                organization=self.org,
                actor=self.owner,
                lead=lead,
                expected_version=1,
                notes="Bantuan tanpa alasan",
            )
        changed = services.update_lead(
            organization=self.org,
            actor=self.owner,
            lead=lead,
            expected_version=1,
            notes="Hasil pemeriksaan direktur",
            responsibility_reason="PIC meminta bantuan memeriksa kebutuhan",
        )
        self.assertEqual(changed.version, 2)


class MarketingWorkflowHTTPTests(TestCase):
    def setUp(self):
        MarketingWorkflowTests.setUp(self)
        self.login(self.owner)

    def login(self, user):
        self.client.force_login(user)
        session = self.client.session
        session["organization_id"] = self.org.pk
        session.save()

    @patch(
        "django.utils.timezone.now",
        return_value=datetime(2026, 10, 9, 10, tzinfo=ZoneInfo("Asia/Jakarta")),
    )
    def test_owner_maps_and_assigns_then_pic_records_contact_and_daily_queue(
        self, _clock
    ):
        member_url = reverse("marketing:member_detail", args=[self.member.pk])
        self.assertEqual(self.client.get(member_url).status_code, 200)
        result = self.client.post(
            member_url,
            {
                "operation": "configure",
                "expected_version": 1,
                "account": self.staff.pk,
                "active": "on",
                "reason": "Akun pribadi sudah diperiksa",
            },
        )
        self.assertEqual(result.status_code, 302)
        lead = MarketingWorkflowTests.lead(self)
        result = self.client.post(
            reverse("marketing:lead_assign", args=[lead.pk]),
            {
                "expected_version": 1,
                "member": self.member.pk,
                "reason": "Bagikan prospek baru",
            },
        )
        self.assertEqual(result.status_code, 302)
        self.login(self.staff)
        lead.refresh_from_db()
        url = reverse("marketing:contact_create", args=[lead.pk])
        self.assertContains(self.client.get(url), 'type="datetime-local"')
        now = timezone.localtime().replace(microsecond=0)
        result = self.client.post(
            url,
            {
                "expected_version": lead.version,
                "occurred_at": (now - timedelta(minutes=1)).isoformat(),
                "method": "whatsapp",
                "outcome": "responded",
                "summary": "Tanya kebutuhan dan jadwal tes",
                "evidence": "Arsip SYNTHETIC-1",
                "first_response": "on",
                "followup_action": "set",
                "next_due_at": (now + timedelta(hours=2)).isoformat(),
            },
        )
        self.assertEqual(result.status_code, 302)
        self.assertContains(
            self.client.get(reverse("marketing:daily")), "Synthetic prospect"
        )
        history = self.client.get(reverse("marketing:lead_history", args=[lead.pk]))
        self.assertContains(history, "Tanya kebutuhan dan jadwal tes")
        self.assertContains(history, "Bagikan prospek baru")
        self.assertEqual(lead.contact_events.count(), 1)

    def test_daily_and_policy_are_bounded_owner_approved_and_stale_safe(self):
        url = reverse("marketing:workflow_policy")
        data = {
            "expected_version": 0,
            "response_limit_minutes": 45,
            "review_limit_minutes": 180,
            "workdays": ["0", "1", "2", "3", "4"],
            "work_start": "09:00",
            "work_end": "17:00",
            "holidays": "2026-12-25",
            "reason": "SLA diuji pada hari kerja",
        }
        self.assertContains(self.client.get(url), 'name="expected_version" value="0"')
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertEqual(workflow.current_work_policy(self.org).version, 1)
        self.assertContains(self.client.post(url, data), "Kebijakan sudah berubah")
        summary = workflow.daily_work_summary(organization=self.org, actor=self.owner)
        self.assertEqual(summary["policy_version"], 1)
        self.assertEqual(summary["counts"]["response_sla"], 0)
        self.login(self.staff)
        self.assertEqual(self.client.post(url, data).status_code, 403)
        self.assertEqual(
            self.client.get(
                reverse("marketing:daily"), {"queue": "invented"}
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.get(reverse("marketing:daily"), {"page": "0"}).status_code, 400
        )

    def test_staff_cannot_reassign_or_change_mapping_and_foreign_objects_are_404(self):
        lead = MarketingWorkflowTests.lead(self)
        self.login(self.staff)
        self.assertEqual(
            self.client.post(
                reverse("marketing:member_detail", args=[self.member.pk]),
                {
                    "operation": "configure",
                    "expected_version": 1,
                    "active": "on",
                    "account": self.staff.pk,
                    "reason": "Tidak boleh mengatur sendiri",
                },
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                reverse("marketing:lead_assign", args=[lead.pk]),
                {
                    "expected_version": 1,
                    "member": self.member.pk,
                    "reason": "Tidak boleh alih sendiri",
                },
            ).status_code,
            403,
        )
        other = Organization.objects.create(name="Foreign synthetic")
        Membership.objects.create(organization=other, user=self.owner, role="owner")
        foreign = services.create_lead(
            organization=other,
            actor=self.owner,
            reference="FOREIGN-PRIVATE",
            name="Other company private",
            channel="sales",
            received_at=timezone.now(),
        )
        for name in ("contact_create", "lead_history", "response_correct"):
            self.assertEqual(
                self.client.get(
                    reverse("marketing:" + name, args=[foreign.pk])
                ).status_code,
                404,
            )
        self.assertNotContains(
            self.client.get(reverse("marketing:daily"), {"scope": "all"}),
            "Other company private",
        )

    def test_invalid_contact_rolls_back_all_history_and_stale_submission_cannot_duplicate(
        self,
    ):
        member = workflow.configure_member(
            organization=self.org,
            actor=self.owner,
            member=self.member,
            expected_version=1,
            account=self.staff,
            active=True,
            reason="Pemetaan akun",
        )
        lead = services.create_lead(
            organization=self.org,
            actor=self.owner,
            reference="ROLLBACK-1",
            name="Synthetic rollback",
            channel="sales",
            owner=member,
            received_at=timezone.now() - timedelta(hours=1),
        )
        self.login(self.staff)
        url = reverse("marketing:contact_create", args=[lead.pk])
        data = {
            "expected_version": 1,
            "occurred_at": timezone.localtime().replace(microsecond=0).isoformat(),
            "method": "whatsapp",
            "outcome": "lost",
            "summary": "Tidak lanjut",
            "evidence": "Arsip SYN-2",
            "followup_action": "keep",
        }
        self.assertContains(
            self.client.post(url, data), "Pilih alasan prospek tidak lanjut"
        )
        lead.refresh_from_db()
        self.assertEqual(lead.version, 1)
        self.assertEqual(lead.contact_events.count(), 0)
        self.assertEqual(lead.stage_events.count(), 1)
        data["loss_reason"] = "price"
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertContains(self.client.post(url, data), "Catatan telah berubah")
        self.assertEqual(lead.contact_events.count(), 1)

    def test_campaign_detail_decisions_and_empty_post_keep_current_state(self):
        campaign = services.create_campaign(
            organization=self.org,
            actor=self.owner,
            name="Synthetic campaign",
            channel="seo",
            owner=self.member,
        )
        url = reverse("marketing:campaign_detail", args=[campaign.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.post(url, {}).status_code, 200)
        campaign.refresh_from_db()
        self.assertEqual(campaign.version, 1)
        self.assertEqual(
            self.client.post(
                url,
                {
                    "expected_version": 1,
                    "operation": "pause",
                    "effective_on": timezone.localdate().isoformat(),
                    "reason": "Periksa brief publikasi",
                },
            ).status_code,
            302,
        )
        self.assertContains(self.client.get(url), "Periksa brief publikasi")
        self.login(self.staff)
        self.assertEqual(
            self.client.post(
                url,
                {
                    "expected_version": 2,
                    "operation": "resume",
                    "effective_on": timezone.localdate().isoformat(),
                    "reason": "Tidak boleh",
                },
            ).status_code,
            403,
        )

    def test_audit_supports_new_contact_types_and_parent_drilldown(self):
        member = workflow.configure_member(
            organization=self.org,
            actor=self.owner,
            member=self.member,
            expected_version=1,
            account=self.staff,
            active=True,
            reason="Pemetaan akun",
        )
        lead = services.create_lead(
            organization=self.org,
            actor=self.owner,
            reference="AUDIT-CONTACT-1",
            name="Synthetic audit prospect",
            channel="sales",
            owner=member,
            received_at=timezone.now() - timedelta(hours=1),
        )
        event = workflow.record_contact(
            organization=self.org,
            actor=self.staff,
            lead=lead,
            expected_version=1,
            occurred_at=timezone.now(),
            method="whatsapp",
            outcome="responded",
            summary="Jejak kontak",
            evidence="SYNTHETIC-EVIDENCE",
        )
        audit = reverse("marketing:audit")
        response = self.client.get(
            audit, {"object_type": "LeadContact", "object_id": event.pk}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("marketing:lead_history", args=[lead.pk]))
        response = self.client.get(audit, {"object_type": "Lead", "object_id": lead.pk})
        self.assertIn(
            "marketing.lead.contact_recorded",
            [row.action for row in response.context["page"]],
        )
