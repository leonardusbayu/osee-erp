from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from core.models import Membership
from finance.fulfillment_models import InvoiceDelivery
from finance.models import InvoiceCredit, Journal
from operations import course_services, services
from operations.course_test_support import CourseTestSupport
from operations.models import Registration, TestSession


class CourseSessionWorkflowTests(CourseTestSupport, TestCase):
    def test_course_plan_creation_is_idempotent_and_schedules_meetings(self):
        plan = course_services.create_course_plan(
            organization=self.org,
            actor=self.actor,
            line=self.line,
            total_sessions=2,
            reason="Two meetings confirmed in the course order",
        )
        retry = course_services.create_course_plan(
            organization=self.org,
            actor=self.actor,
            line=self.line,
            total_sessions=2,
            reason="Retry of the same course setup",
        )
        self.assertEqual(retry.pk, plan.pk)

        now = timezone.now()
        first = course_services.schedule_course_meeting(
            organization=self.org,
            actor=self.actor,
            plan=plan,
            starts_at=now - timedelta(days=2),
            expected_version=1,
            reason="First meeting confirmed with the learners",
        )
        plan.refresh_from_db()
        second = course_services.schedule_course_meeting(
            organization=self.org,
            actor=self.actor,
            plan=plan,
            starts_at=now - timedelta(days=1),
            expected_version=2,
            reason="Second meeting confirmed with the learners",
        )

        self.assertEqual([item.number for item in plan.meetings.all()], [1, 2])
        self.assertLess(first.starts_at, second.starts_at)

    def test_attendance_retry_is_idempotent_and_locks_meeting_schedule(self):
        plan = course_services.create_course_plan(
            organization=self.org,
            actor=self.actor,
            line=self.line,
            total_sessions=1,
            reason="One meeting confirmed in the course order",
        )
        meeting = course_services.schedule_course_meeting(
            organization=self.org,
            actor=self.actor,
            plan=plan,
            starts_at=timezone.now() - timedelta(days=1),
            expected_version=1,
            reason="Meeting confirmed with the learners",
        )
        recorded = self.record_course_attendance(
            organization=self.org,
            actor=self.actor,
            registration=self.first,
            meeting=meeting,
            expected_version=0,
            attended=True,
            evidence="Synthetic attendance list",
        )

        with self.assertRaises(ValidationError):
            course_services.reschedule_course_meeting(
                organization=self.org,
                actor=self.actor,
                meeting=meeting,
                starts_at=timezone.now() + timedelta(days=2),
                expected_version=meeting.version,
                reason="Synthetic attempt to move a meeting after attendance",
            )
        retry = self.record_course_attendance(
            organization=self.org,
            actor=self.actor,
            registration=self.first,
            meeting=meeting,
            expected_version=0,
            attended=True,
            evidence="Synthetic attendance list",
        )
        self.assertEqual(retry.pk, recorded.pk)

    def test_course_progress_tracks_attendance_without_posting_money(self):
        plan = course_services.create_course_plan(
            organization=self.org,
            actor=self.actor,
            line=self.line,
            total_sessions=2,
            reason="Two meetings confirmed in the course order",
        )
        now = timezone.now()
        first_meeting = course_services.schedule_course_meeting(
            organization=self.org,
            actor=self.actor,
            plan=plan,
            starts_at=now - timedelta(days=2),
            expected_version=1,
            reason="First meeting confirmed with the learners",
        )
        plan.refresh_from_db()
        second_meeting = course_services.schedule_course_meeting(
            organization=self.org,
            actor=self.actor,
            plan=plan,
            starts_at=now - timedelta(days=1),
            expected_version=2,
            reason="Second meeting confirmed with the learners",
        )
        self.record_course_attendance(
            organization=self.org,
            actor=self.actor,
            registration=self.first,
            meeting=first_meeting,
            expected_version=0,
            attended=True,
            evidence="Synthetic attendance list, first meeting",
        )
        self.record_course_attendance(
            organization=self.org,
            actor=self.actor,
            registration=self.first,
            meeting=second_meeting,
            expected_version=0,
            attended=False,
            evidence="Synthetic attendance list, second meeting",
        )

        progress = course_services.course_progress(
            organization=self.org, registration=self.first
        )
        self.assertEqual(
            (
                progress["total_sessions"],
                progress["scheduled_sessions"],
                progress["attended_sessions"],
                progress["absent_sessions"],
                progress["remaining_sessions"],
                progress["all_sessions_attended"],
            ),
            (2, 2, 1, 1, 1, False),
        )
        other_progress = course_services.course_progress(
            organization=self.org, registration=self.second
        )
        self.assertEqual(other_progress["attended_sessions"], 0)
        self.assertEqual(
            Registration.objects.get(pk=self.first.pk).status, "registered"
        )
        self.assertFalse(InvoiceDelivery.objects.exists())
        self.assertFalse(Journal.objects.exists())

    def test_course_plan_is_only_available_for_course_products(self):
        test_line = services.add_order_line(
            organization=self.org,
            actor=self.actor,
            order=self.order,
            product=self.itp,
            quantity=1,
            reference="TOEFL-ITP",
        )
        with self.assertRaises(ValidationError):
            course_services.create_course_plan(
                organization=self.org,
                actor=self.actor,
                line=test_line,
                total_sessions=2,
                reason="This is a one-time test product",
            )

    def test_stale_attendance_and_foreign_participants_cannot_change_a_course(self):
        plan = course_services.create_course_plan(
            organization=self.org,
            actor=self.actor,
            line=self.line,
            total_sessions=1,
            reason="One meeting confirmed in the course order",
        )
        meeting = course_services.schedule_course_meeting(
            organization=self.org,
            actor=self.actor,
            plan=plan,
            starts_at=timezone.now() - timedelta(hours=2),
            expected_version=1,
            reason="Meeting confirmed with the learners",
        )
        with self.assertRaises(ValidationError):
            self.record_course_attendance(
                organization=self.org,
                actor=self.actor,
                registration=self.first,
                meeting=meeting,
                expected_version=0,
                attended=True,
                evidence=" ",
            )
        with self.assertRaises(ValidationError):
            self.record_course_attendance(
                organization=self.org,
                actor=self.actor,
                registration=self.first,
                meeting=meeting,
                expected_version=3,
                attended=True,
                evidence="Synthetic attendance list",
            )

        other_order = services.create_order(
            organization=self.org,
            actor=self.actor,
            reference="COURSE-ORDER-2",
            buyer=self.buyer,
            date=timezone.localdate(),
        )
        other_line = services.add_order_line(
            organization=self.org,
            actor=self.actor,
            order=other_order,
            product=self.course,
            quantity=1,
        )
        foreign_registration = services.register_participant(
            organization=self.org,
            actor=self.actor,
            line=other_line,
            name="Synthetic learner outside this group",
            source="test",
            external_id="COURSE-R-3",
        )
        with self.assertRaises(ValidationError):
            self.record_course_attendance(
                organization=self.org,
                actor=self.actor,
                registration=foreign_registration,
                meeting=meeting,
                expected_version=0,
                attended=True,
                evidence="Synthetic attendance list",
            )
        progress = course_services.course_progress(
            organization=self.org, registration=self.first
        )
        self.assertEqual(progress["attended_sessions"], 0)

    def test_legacy_single_session_actions_cannot_change_course_attendance(self):
        now = timezone.now()
        legacy_session = TestSession(
            organization=self.org,
            created_by=self.actor,
            reference="LEGACY-COURSE-SESSION",
            product=self.course,
            starts_at=now - timedelta(hours=2),
            capacity=10,
        )
        legacy_session._service_write = True
        legacy_session.save()
        self.first.session = legacy_session
        self.first.status = "scheduled"
        self.first._service_write = True
        self.first.save()
        self.first.refresh_from_db()

        with self.assertRaises(ValidationError):
            services.record_attendance(
                organization=self.org,
                actor=self.actor,
                registration=self.first,
                expected_version=self.first.version,
                attended=True,
                evidence="Synthetic legacy course roster",
            )
        with self.assertRaises(ValidationError):
            services.complete_registration(
                organization=self.org,
                actor=self.actor,
                registration=self.first,
                expected_version=self.first.version,
                date=timezone.localdate(),
                evidence="Synthetic legacy course completion attempt",
            )

        self.first.status = "attended"
        self.first._service_write = True
        self.first.save()
        self.first.refresh_from_db()
        with self.assertRaises(ValidationError):
            services.correct_attendance(
                organization=self.org,
                actor=self.actor,
                registration=self.first,
                expected_version=self.first.version,
                attended=False,
                reason="Synthetic legacy course correction attempt",
            )

    def test_course_cannot_be_cancelled_after_attended_meeting(self):
        plan = course_services.create_course_plan(
            organization=self.org,
            actor=self.actor,
            line=self.line,
            total_sessions=1,
            reason="One meeting in this course",
        )
        meeting = course_services.schedule_course_meeting(
            organization=self.org,
            actor=self.actor,
            plan=plan,
            starts_at=timezone.now() - timedelta(hours=1),
            expected_version=1,
            reason="Course meeting scheduled",
        )
        self.record_course_attendance(
            organization=self.org,
            actor=self.actor,
            registration=self.first,
            meeting=meeting,
            expected_version=0,
            attended=True,
            evidence="Synthetic signed course roster",
        )
        owner = get_user_model().objects.create_user(username="course-ops-owner")
        Membership.objects.create(organization=self.org, user=owner, role="owner")
        before_quantity = self.line.quantity

        with self.assertRaises(ValidationError):
            services.cancel_registration(
                organization=self.org,
                actor=owner,
                registration=self.first,
                expected_version=self.first.version,
                date=timezone.localdate(),
                reason="Synthetic attempt to cancel after an attended course meeting",
            )
        self.line.refresh_from_db()
        self.first.refresh_from_db()
        self.assertEqual(self.line.quantity, before_quantity)
        self.assertNotEqual(self.first.status, "cancelled")
        self.assertFalse(InvoiceCredit.objects.exists())
