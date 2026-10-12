from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from finance.services import _close_blockers
from operations import course_services
from operations.course_test_support import CourseTestSupport
from operations.services import invoice_line


class CourseFinanceCloseTests(CourseTestSupport, TestCase):
    def test_finance_close_holds_for_delivered_course_sessions_without_policy(self):
        today = timezone.localdate()
        invoice_line(
            organization=self.org, actor=self.actor, line=self.line, date=today
        )
        plan = course_services.create_course_plan(
            organization=self.org,
            actor=self.actor,
            line=self.line,
            total_sessions=2,
            reason="Two meetings in the course package",
        )
        first = course_services.schedule_course_meeting(
            organization=self.org,
            actor=self.actor,
            plan=plan,
            starts_at=timezone.now() - timedelta(days=1),
            expected_version=1,
            reason="First course meeting completed",
        )
        plan.refresh_from_db()
        course_services.schedule_course_meeting(
            organization=self.org,
            actor=self.actor,
            plan=plan,
            starts_at=timezone.now() + timedelta(days=1),
            expected_version=2,
            reason="Second course meeting confirmed",
        )
        self.record_course_attendance(
            organization=self.org,
            actor=self.actor,
            registration=self.first,
            meeting=first,
            expected_version=0,
            attended=True,
            evidence="Synthetic signed course roster",
        )
        self.record_course_attendance(
            organization=self.org,
            actor=self.actor,
            registration=self.second,
            meeting=first,
            expected_version=0,
            attended=True,
            evidence="Synthetic signed course roster for learner two",
        )

        blockers = _close_blockers(self.org, today)
        self.assertTrue(
            any(
                "kursus" in message.lower() and "kebijakan" in message.lower()
                for message in blockers
            ),
            blockers,
        )

    def test_course_invoice_with_only_future_meetings_does_not_block_current_close(
        self,
    ):
        today = timezone.localdate()
        invoice_line(
            organization=self.org, actor=self.actor, line=self.line, date=today
        )
        plan = course_services.create_course_plan(
            organization=self.org,
            actor=self.actor,
            line=self.line,
            total_sessions=2,
            reason="Two upcoming course meetings",
        )
        future = timezone.now() + timedelta(days=1)
        course_services.schedule_course_meeting(
            organization=self.org,
            actor=self.actor,
            plan=plan,
            starts_at=future,
            expected_version=1,
            reason="First future course meeting confirmed",
        )
        plan.refresh_from_db()
        course_services.schedule_course_meeting(
            organization=self.org,
            actor=self.actor,
            plan=plan,
            starts_at=future + timedelta(days=1),
            expected_version=2,
            reason="Second future course meeting confirmed",
        )

        blockers = _close_blockers(self.org, today)
        self.assertFalse(
            any("Layanan jatuh tempo" in message for message in blockers), blockers
        )

    def test_past_course_meeting_missing_attendance_blocks_close_even_if_more_sessions_are_planned(
        self,
    ):
        today = timezone.localdate()
        invoice_line(
            organization=self.org, actor=self.actor, line=self.line, date=today
        )
        plan = course_services.create_course_plan(
            organization=self.org,
            actor=self.actor,
            line=self.line,
            total_sessions=2,
            reason="Two course meetings",
        )
        past = course_services.schedule_course_meeting(
            organization=self.org,
            actor=self.actor,
            plan=plan,
            starts_at=timezone.now() - timedelta(days=1),
            expected_version=1,
            reason="First course meeting",
        )
        plan.refresh_from_db()
        course_services.schedule_course_meeting(
            organization=self.org,
            actor=self.actor,
            plan=plan,
            starts_at=timezone.now() + timedelta(days=1),
            expected_version=2,
            reason="Second course meeting",
        )
        self.record_course_attendance(
            organization=self.org,
            actor=self.actor,
            registration=self.first,
            meeting=past,
            expected_version=0,
            attended=False,
            evidence="Synthetic course roster marks absence",
        )

        blockers = _close_blockers(self.org, today)
        self.assertTrue(
            any(
                "lewat" in message.lower() and "hadiri" in message.lower()
                for message in blockers
            ),
            blockers,
        )
