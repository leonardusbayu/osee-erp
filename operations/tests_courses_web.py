from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import Membership, Organization
from finance.models import Party, Product
from operations import course_services, services
from operations.models import CourseAttendance, CoursePlan


class CourseSessionWebTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Synthetic course UI")
        self.finance = get_user_model().objects.create_user(
            username="course-ui-finance"
        )
        self.marketing = get_user_model().objects.create_user(
            username="course-ui-marketing"
        )
        self.operations = get_user_model().objects.create_user(
            username="course-ui-operations"
        )
        Membership.objects.create(
            organization=self.org, user=self.finance, role="finance"
        )
        Membership.objects.create(
            organization=self.org, user=self.marketing, role="marketing"
        )
        Membership.objects.create(
            organization=self.org, user=self.operations, role="operations"
        )
        self.client = Client()
        self.client.force_login(self.finance)
        self.buyer = Party.objects.create(
            organization=self.org, name="Synthetic course buyer", kind="customer"
        )
        self.product = Product.objects.create(
            organization=self.org,
            code="COURSE",
            name="English course",
            kind="course",
            default_price="1200000",
        )
        self.order = services.create_order(
            organization=self.org,
            actor=self.finance,
            reference="COURSE-UI-1",
            buyer=self.buyer,
            date=timezone.localdate(),
        )
        self.line = services.add_order_line(
            organization=self.org,
            actor=self.finance,
            order=self.order,
            product=self.product,
            quantity=1,
        )
        self.registration = services.register_participant(
            organization=self.org,
            actor=self.finance,
            line=self.line,
            name="Synthetic learner",
            source="test",
            external_id="COURSE-UI-R1",
        )

    def test_finance_can_set_up_meetings_and_record_one_attendee(self):
        url = reverse("operations:course_plan", args=[self.line.pk])
        first = self.client.get(url)
        self.assertEqual(first.status_code, 200)
        self.assertContains(first, "Jumlah pertemuan")

        configured = self.client.post(
            url,
            {
                "action": "configure",
                "total_sessions": "2",
                "reason": "Dua pertemuan sesuai penawaran kursus",
            },
        )
        self.assertRedirects(configured, url)
        plan = CoursePlan.objects.get(line=self.line)
        starts_at = timezone.now() - timedelta(days=1)
        scheduled = self.client.post(
            url,
            {
                "action": "schedule",
                "expected_version": str(plan.version),
                "starts_at": timezone.localtime(starts_at).strftime("%Y-%m-%dT%H:%M"),
                "reason": "Waktu disepakati dengan peserta",
            },
        )
        self.assertRedirects(scheduled, url)
        plan.refresh_from_db()
        meeting = plan.meetings.get(number=1)
        attendance_url = reverse(
            "operations:course_attendance",
            args=[self.registration.pk, meeting.pk],
        )
        attendance = self.client.post(
            attendance_url,
            {
                "expected_version": "0",
                "attended": "yes",
                "evidence": "Daftar hadir kelas, 11 Oktober",
            },
        )
        self.assertRedirects(attendance, url)
        result = self.client.get(url)
        self.assertContains(result, "1 / 2")
        self.assertContains(result, "tidak membuat invoice atau membukukan pendapatan")
        self.assertContains(result, "Daftar hadir kelas, 11 Oktober")
        corrected = self.client.post(
            attendance_url,
            {
                "expected_version": "1",
                "attended": "no",
                "evidence": "Daftar hadir yang telah ditinjau ulang",
                "reason": "Pelatih mengonfirmasi nama peserta pada daftar terbaru",
                "confirm": "yes",
            },
        )
        self.assertRedirects(corrected, url)
        audited = self.client.get(url)
        self.assertContains(audited, "Daftar hadir yang telah ditinjau ulang")
        self.assertContains(
            audited, "Pelatih mengonfirmasi nama peserta pada daftar terbaru"
        )
        self.assertContains(audited, "Bukti sebelumnya: Daftar hadir kelas, 11 Oktober")
        self.assertContains(
            audited, "Bukti baru: Daftar hadir yang telah ditinjau ulang"
        )
        detail = self.client.get(
            reverse("operations:registration", args=[self.registration.pk])
        )
        self.assertContains(detail, "Lihat jadwal dan kehadiran kursus")
        self.assertNotContains(detail, "Jadwalkan / pindah sesi")
        self.assertNotContains(detail, "Konfirmasikan layanan selesai")

    def test_marketing_cannot_view_course_participants_or_change_attendance(self):
        plan = course_services.create_course_plan(
            organization=self.org,
            actor=self.finance,
            line=self.line,
            total_sessions=1,
            reason="Satu pertemuan dalam paket kursus",
        )
        meeting = course_services.schedule_course_meeting(
            organization=self.org,
            actor=self.finance,
            plan=plan,
            starts_at=timezone.now() - timedelta(days=1),
            expected_version=1,
            reason="Jadwal telah dikonfirmasi",
        )
        self.client.force_login(self.marketing)
        self.assertEqual(
            self.client.get(
                reverse("operations:course_plan", args=[self.line.pk])
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(
                reverse(
                    "operations:course_attendance",
                    args=[self.registration.pk, meeting.pk],
                )
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(
                reverse("operations:course_meeting_reschedule", args=[meeting.pk])
            ).status_code,
            403,
        )

    def test_finance_can_reschedule_a_future_meeting_with_a_reason(self):
        plan = course_services.create_course_plan(
            organization=self.org,
            actor=self.finance,
            line=self.line,
            total_sessions=1,
            reason="Satu pertemuan kursus",
        )
        meeting = course_services.schedule_course_meeting(
            organization=self.org,
            actor=self.finance,
            plan=plan,
            starts_at=timezone.now() + timedelta(days=2),
            expected_version=1,
            reason="Jadwal awal disepakati",
        )
        url = reverse("operations:course_meeting_reschedule", args=[meeting.pk])
        self.assertContains(self.client.get(url), "Tanggal dan waktu pertemuan")
        new_start = timezone.now() + timedelta(days=3)
        response = self.client.post(
            url,
            {
                "expected_version": str(meeting.version),
                "starts_at": timezone.localtime(new_start).strftime("%Y-%m-%dT%H:%M"),
                "reason": "Peserta meminta waktu baru dan menyetujuinya",
            },
        )
        self.assertRedirects(
            response, reverse("operations:course_plan", args=[self.line.pk])
        )
        meeting.refresh_from_db()
        self.assertEqual(
            timezone.localtime(meeting.starts_at).strftime("%Y-%m-%dT%H:%M"),
            timezone.localtime(new_start).strftime("%Y-%m-%dT%H:%M"),
        )
        plan_page = self.client.get(
            reverse("operations:course_plan", args=[self.line.pk])
        )
        self.assertContains(plan_page, "Ubah jadwal")
        self.assertContains(plan_page, "Peserta meminta waktu baru dan menyetujuinya")

    def test_operations_can_record_course_attendance_but_cannot_change_course_terms(
        self,
    ):
        plan = course_services.create_course_plan(
            organization=self.org,
            actor=self.finance,
            line=self.line,
            total_sessions=2,
            reason="Paket dua pertemuan",
        )
        meeting = course_services.schedule_course_meeting(
            organization=self.org,
            actor=self.finance,
            plan=plan,
            starts_at=timezone.now() - timedelta(days=1),
            expected_version=plan.version,
            reason="Jadwal disepakati",
        )
        self.client.force_login(self.operations)
        plan_url = reverse("operations:course_plan", args=[self.line.pk])
        self.assertEqual(self.client.get(plan_url).status_code, 200)

        attendance_url = reverse(
            "operations:course_attendance", args=[self.registration.pk, meeting.pk]
        )
        attendance = self.client.post(
            attendance_url,
            {
                "expected_version": "0",
                "attended": "yes",
                "evidence": "Daftar hadir kelas sintetis",
            },
        )
        self.assertRedirects(attendance, plan_url)
        self.assertEqual(CourseAttendance.objects.count(), 1)

        revision = self.client.post(
            plan_url,
            {
                "action": "revise",
                "expected_version": str(plan.version),
                "total_sessions": "8",
                "reason": "Perubahan paket sintetis",
            },
        )
        self.assertEqual(revision.status_code, 403)
        plan.refresh_from_db()
        self.assertEqual(plan.total_sessions, 2)
