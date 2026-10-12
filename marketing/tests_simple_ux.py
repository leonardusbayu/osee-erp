"""Synthetic public journeys for clear daily work and preserved appointments."""

import re
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import AuditEvent, Membership, Organization

from . import services, workflow


class MarketingSimpleUXTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Synthetic simple marketing")
        self.owner = get_user_model().objects.create_user(username="simple-owner")
        self.staff = get_user_model().objects.create_user(username="simple-pic")
        self.other = get_user_model().objects.create_user(username="simple-other")
        for user, role in (
            (self.owner, "owner"),
            (self.staff, "marketing"),
            (self.other, "marketing"),
        ):
            Membership.objects.create(organization=self.org, user=user, role=role)
        self.member = services.create_member(
            organization=self.org,
            actor=self.owner,
            name="Synthetic PIC",
            discipline="sales",
        )
        self.member = workflow.configure_member(
            organization=self.org,
            actor=self.owner,
            member=self.member,
            expected_version=1,
            account=self.staff,
            active=True,
            reason="Pemetaan akun sintetis",
        )
        self.lead = services.create_lead(
            organization=self.org,
            actor=self.owner,
            reference="SIMPLE-APPOINTMENT",
            name="Synthetic appointment",
            received_at=timezone.now() - timedelta(days=2),
            channel="sales",
            owner=self.member,
        )
        self.due = timezone.localtime().replace(second=0, microsecond=0) + timedelta(
            days=1
        )
        self.event = workflow.record_contact(
            organization=self.org,
            actor=self.staff,
            lead=self.lead,
            expected_version=1,
            occurred_at=timezone.now().replace(microsecond=654321)
            - timedelta(minutes=1),
            method="whatsapp",
            outcome="responded",
            summary="Janji dari arsip sintetis",
            evidence="SYNTHETIC-CONTACT",
            first_response=True,
            followup_action="set",
            next_due_at=self.due,
        )
        self.lead.refresh_from_db()
        self.login(self.staff)

    def login(self, user):
        self.client.force_login(user)
        session = self.client.session
        session["organization_id"] = self.org.pk
        session.save()

    def summary_data(self, **changes):
        data = {
            "operation": "update",
            "expected_version": self.lead.version,
            "stage": self.lead.stage,
            "first_response_at": timezone.localtime(
                self.lead.first_response_at
            ).isoformat(),
            "next_follow_up": self.lead.next_follow_up.isoformat(),
            "notes": "Catatan kebutuhan diperbarui",
            "correction_reason": "",
            "responsibility_reason": "",
        }
        data.update(changes)
        return data

    def test_full_summary_post_preserves_unchanged_exact_appointment(self):
        response = self.client.post(
            reverse("marketing:lead_detail", args=[self.lead.pk]), self.summary_data()
        )
        self.assertEqual(response.status_code, 302)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.next_follow_up_at, self.due)
        self.assertEqual(self.lead.version, 3)
        self.assertEqual(self.lead.contact_events.count(), 1)
        self.assertTrue(
            AuditEvent.objects.filter(
                organization=self.org,
                action="marketing.lead.updated",
                object_id=str(self.lead.pk),
                actor=self.staff,
            ).exists()
        )

    def test_summary_get_rendered_timestamp_roundtrip_saves_notes_without_false_correction(
        self,
    ):
        url = reverse("marketing:lead_detail", args=[self.lead.pk])
        page = self.client.get(url).content.decode()
        input_tag = next(
            tag
            for tag in re.findall(r"<input\b[^>]*>", page)
            if 'name="first_response_at"' in tag
        )
        rendered_value = re.search(r'value="([^"]*)"', input_tag).group(1)
        original_response_at = self.lead.first_response_at
        self.assertEqual(
            rendered_value,
            timezone.localtime(original_response_at).strftime("%Y-%m-%dT%H:%M:%S"),
        )
        self.assertEqual(original_response_at.microsecond, 654321)
        response = self.client.post(
            url, self.summary_data(first_response_at=rendered_value)
        )
        self.assertEqual(response.status_code, 302)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.notes, "Catatan kebutuhan diperbarui")
        self.assertEqual(self.lead.first_response_at, original_response_at)
        self.assertEqual(self.lead.next_follow_up_at, self.due)
        self.assertEqual(self.lead.contact_events.count(), 1)

    def test_real_response_time_change_or_clear_still_requires_correction_reason(self):
        url = reverse("marketing:lead_detail", args=[self.lead.pk])
        original_response_at = self.lead.first_response_at
        for value in (
            (original_response_at + timedelta(seconds=1))
            .replace(microsecond=0)
            .isoformat(),
            (original_response_at + timedelta(minutes=1))
            .replace(microsecond=0)
            .isoformat(),
            "",
        ):
            with self.subTest(value=value):
                response = self.client.post(
                    url, self.summary_data(first_response_at=value)
                )
                self.assertContains(response, "Alasan wajib diisi")
                self.lead.refresh_from_db()
                self.assertEqual(self.lead.first_response_at, original_response_at)
                self.assertEqual(self.lead.version, 2)
                self.assertEqual(self.lead.contact_events.count(), 1)

    def test_full_summary_post_changed_or_cleared_date_removes_old_time(self):
        url = reverse("marketing:lead_detail", args=[self.lead.pk])
        changed_date = timezone.localdate(self.due) + timedelta(days=1)
        self.assertEqual(
            self.client.post(
                url, self.summary_data(next_follow_up=changed_date.isoformat())
            ).status_code,
            302,
        )
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.next_follow_up, changed_date)
        self.assertIsNone(self.lead.next_follow_up_at)
        # A newly recorded exact appointment must also be cleared when the date is cleared.
        workflow.record_contact(
            organization=self.org,
            actor=self.staff,
            lead=self.lead,
            expected_version=self.lead.version,
            occurred_at=timezone.now(),
            method="whatsapp",
            outcome="responded",
            summary="Janji baru sintetis",
            evidence="SYNTHETIC-RESCHEDULE",
            followup_action="set",
            next_due_at=self.due,
        )
        self.lead.refresh_from_db()
        self.assertEqual(
            self.client.post(url, self.summary_data(next_follow_up="")).status_code, 302
        )
        self.lead.refresh_from_db()
        self.assertIsNone(self.lead.next_follow_up)
        self.assertIsNone(self.lead.next_follow_up_at)

    def test_shared_update_without_date_and_contact_keep_preserve_time(self):
        services.update_lead(
            organization=self.org,
            actor=self.staff,
            lead=self.lead,
            expected_version=self.lead.version,
            notes="Ringkasan tanpa mengganti tanggal",
        )
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.next_follow_up_at, self.due)
        workflow.record_contact(
            organization=self.org,
            actor=self.staff,
            lead=self.lead,
            expected_version=self.lead.version,
            occurred_at=timezone.now(),
            method="phone",
            outcome="no_answer",
            summary="Mencoba menghubungi lagi",
            evidence="SYNTHETIC-KEEP",
            followup_action="keep",
        )
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.next_follow_up_at, self.due)

    def test_summary_is_secondary_but_errors_open_it_and_stale_post_does_not_save(self):
        url = reverse("marketing:lead_detail", args=[self.lead.pk])
        response = self.client.get(url)
        self.assertContains(
            response, '<details class="m-proof-details lead-summary-edit" >', html=False
        )
        self.assertContains(response, "Janji berikutnya")
        self.assertContains(response, self.due.strftime("%H:%M") + " WIB")
        self.assertContains(response, "Pembayaran harus diperiksa Finance")
        response = self.client.post(url, self.summary_data(expected_version=1))
        self.assertContains(response, "Catatan telah berubah")
        self.assertContains(
            response,
            '<details class="m-proof-details lead-summary-edit" open>',
            html=False,
        )
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.version, 2)
        self.assertEqual(self.lead.next_follow_up_at, self.due)
        self.assertEqual(self.lead.notes, "")

    def test_today_empty_still_shows_nonempty_urgent_queues_with_explicit_scope(self):
        overdue = services.create_lead(
            organization=self.org,
            actor=self.owner,
            reference="SIMPLE-OVERDUE",
            name="Synthetic overdue",
            received_at=timezone.now() - timedelta(days=3),
            channel="sales",
            owner=self.member,
            next_follow_up=timezone.localdate() - timedelta(days=1),
        )
        # Not assigned to this staff; must not leak into their alternate queue counts.
        services.create_lead(
            organization=self.org,
            actor=self.owner,
            reference="SIMPLE-UNASSIGNED",
            name="Synthetic triage",
            received_at=timezone.now() - timedelta(days=3),
            channel="sales",
            next_follow_up=timezone.localdate() - timedelta(days=1),
        )
        url = reverse("marketing:daily")
        response = self.client.get(url)
        self.assertEqual(response.context["page"].paginator.count, 0)
        self.assertContains(response, "Tidak ada pekerjaan pada pilihan ini")
        self.assertEqual(
            {
                item["key"]: item["count"]
                for item in response.context["attention_queues"]
            },
            {"overdue": 1, "unanswered": 1},
        )
        html = response.content.decode()
        self.assertIn('href="?scope=mine&amp;queue=overdue"', html)
        self.assertLess(
            html.index('class="m-callout daily-attention"'),
            html.index('<details class="m-proof-details"'),
        )
        response = self.client.get(url, {"scope": "mine", "queue": "overdue"})
        self.assertEqual([item.pk for item in response.context["page"]], [overdue.pk])
        self.assertNotIn(
            "overdue", [item["key"] for item in response.context["attention_queues"]]
        )
        self.assertContains(
            response, 'href="?scope=mine&amp;queue=unanswered"', html=False
        )
        response = self.client.get(url, {"scope": "all", "queue": "today"})
        self.assertEqual(
            next(
                item["count"]
                for item in response.context["attention_queues"]
                if item["key"] == "overdue"
            ),
            2,
        )
        self.assertContains(
            response, 'href="?scope=all&amp;queue=unassigned"', html=False
        )

    def test_history_actions_follow_pic_delegation_and_revoke_boundaries(self):
        detail = reverse("marketing:lead_detail", args=[self.lead.pk])
        history = reverse("marketing:lead_history", args=[self.lead.pk])
        contact = reverse("marketing:contact_create", args=[self.lead.pk])
        correction = reverse("marketing:contact_correct", args=[self.event.pk])
        for user, allowed in (
            (self.staff, True),
            (self.other, False),
            (self.owner, True),
        ):
            self.login(user)
            self.assertEqual(self.client.get(detail).context["can_execute"], allowed)
            response = self.client.get(history)
            self.assertEqual(response.context["can_execute"], allowed)
            for href in (f'href="{contact}"', f'href="{correction}"'):
                if allowed:
                    self.assertContains(response, href, html=False)
                else:
                    self.assertNotContains(response, href, html=False)
        delegation = workflow.grant_delegation(
            organization=self.org,
            actor=self.owner,
            member=self.member,
            expected_version=self.member.version,
            delegate=self.other,
            starts_on=timezone.localdate(),
            ends_on=timezone.localdate(),
            reason="Pengganti sintetis",
        )
        self.login(self.other)
        self.assertContains(self.client.get(history), f'href="{contact}"', html=False)
        workflow.revoke_delegation(
            organization=self.org,
            actor=self.owner,
            delegation=delegation,
            expected_version=delegation.version,
            reason="Selesai bertugas sintetis",
        )
        self.assertNotContains(
            self.client.get(history), f'href="{contact}"', html=False
        )
        self.assertEqual(self.client.post(detail, self.summary_data()).status_code, 403)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.version, 2)
        self.assertEqual(self.lead.next_follow_up_at, self.due)

    def test_contact_conditional_guidance_does_not_replace_server_checks(self):
        url = reverse("marketing:contact_create", args=[self.lead.pk])
        response = self.client.get(url)
        self.assertContains(response, "Wajib bila Hasil kontak dipilih Tidak lanjut.")
        self.assertContains(response, "Wajib bila memilih Tetapkan jadwal baru.")
        self.assertNotContains(
            response, "Kolom lain boleh dilengkapi jika datanya tersedia"
        )
        data = {
            "expected_version": self.lead.version,
            "occurred_at": timezone.localtime().isoformat(),
            "method": "whatsapp",
            "outcome": "lost",
            "summary": "Hasil kontak sintetis",
            "evidence": "SYNTHETIC-CONDITIONAL",
            "followup_action": "set",
        }
        response = self.client.post(url, data)
        self.assertContains(response, "Pilih alasan prospek tidak lanjut.")
        self.assertContains(response, "Isi tanggal dan jam janji berikutnya.")
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.version, 2)
        self.assertEqual(self.lead.contact_events.count(), 1)
        self.assertEqual(self.lead.next_follow_up_at, self.due)
