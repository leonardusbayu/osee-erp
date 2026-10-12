"""Marketing approval races on real PostgreSQL locks in Django's test database."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection, connections
from django.test import TransactionTestCase
from django.utils import timezone

from core.models import AuditEvent, Membership, Organization

from . import activities, kpi, services
from .activity_models import WorkActivity
from .kpi_models import KpiTarget


@skipUnless(connection.vendor == "postgresql", "Requires real PostgreSQL row locks")
class ConcurrentMarketingPerformanceTests(TransactionTestCase):
    def setUp(self):
        self.assertTrue(connection.settings_dict["NAME"].startswith("test_"))
        self.org = Organization.objects.create(
            name="Synthetic marketing approval races"
        )
        for name, role in (
            ("owner_a", "owner"),
            ("owner_b", "owner"),
            ("marketing", "marketing"),
        ):
            actor = get_user_model().objects.create_user(
                username=f"marketing-race-{name}"
            )
            Membership.objects.create(organization=self.org, user=actor, role=role)
            setattr(self, name, actor)
        self.today = timezone.localdate()
        self.member = services.create_member(
            organization=self.org,
            actor=self.owner_a,
            name="Synthetic marketing PIC",
            discipline="sales",
        )
        from .workflow import configure_member

        configure_member(
            organization=self.org,
            actor=self.owner_a,
            member=self.member,
            expected_version=self.member.version,
            account=self.marketing,
            active=True,
            reason="Assign execution login",
        )

    def race(self, *actions):
        ready = Barrier(len(actions))

        def execute(action):
            close_old_connections()
            try:
                self.assertTrue(
                    connections["default"].settings_dict["NAME"].startswith("test_")
                )
                ready.wait(timeout=10)
                try:
                    result = action()
                    return {"status": "committed", "pk": result.pk}
                except ValidationError as exc:
                    return {"status": "rejected", "messages": exc.messages}
            finally:
                connections["default"].close()

        with ThreadPoolExecutor(max_workers=len(actions)) as workers:
            futures = [workers.submit(execute, action) for action in actions]
            return [future.result(timeout=30) for future in futures]

    def test_overlapping_target_approvals_commit_only_one_active_target(self):
        drafts = [
            kpi.create_target(
                organization=self.org,
                actor=self.marketing,
                title=f"Overlapping commitment {index}",
                metric_key="leads",
                target="20",
                period_start=self.today - timedelta(days=20 - index * 5),
                period_end=self.today - timedelta(days=10 - index * 5),
                member=self.member,
                channel="sales",
                rationale="Independent approval of synthetic targets",
            )
            for index in range(2)
        ]
        actors = (self.owner_a, self.owner_b)
        results = self.race(
            *[
                lambda draft=draft, actor=actor: kpi.activate_target(
                    organization=self.org.pk,
                    actor=actor,
                    target=draft.pk,
                    expected_version=1,
                    reason="Scope and period reviewed",
                )
                for draft, actor in zip(drafts, actors)
            ]
        )
        self.assertCountEqual(
            [result["status"] for result in results], ["committed", "rejected"]
        )
        self.assertEqual(
            KpiTarget.objects.filter(organization=self.org, status="active").count(), 1
        )
        active = KpiTarget.objects.get(organization=self.org, status="active")
        remaining = KpiTarget.objects.get(organization=self.org, status="draft")
        winner_index = next(
            index
            for index, result in enumerate(results)
            if result["status"] == "committed"
        )
        self.assertEqual(active.pk, drafts[winner_index].pk)
        self.assertEqual(
            (active.version, active.approved_by), (2, actors[winner_index])
        )
        self.assertEqual(remaining.version, 1)
        self.assertIsNone(remaining.approved_by)
        self.assertIsNone(remaining.approved_at)
        approvals = AuditEvent.objects.filter(
            organization=self.org, action="marketing.kpi.activated"
        )
        self.assertEqual(approvals.count(), 1)
        approval = approvals.get()
        self.assertEqual(
            (approval.object_id, approval.actor_id),
            (str(active.pk), actors[winner_index].pk),
        )
        self.assertEqual(approval.detail["after"]["version"], 2)

    def test_competing_activity_reviews_have_one_winner_and_one_stale_rejection(self):
        item = activities.create_activity(
            organization=self.org,
            actor=self.owner_a,
            title="Synthetic reviewed follow-up",
            owner=self.member,
            channel="sales",
            kind="follow_up",
            due_date=self.today,
            description="Evidence should receive one review",
        )
        item = activities.update_activity(
            organization=self.org,
            actor=self.marketing,
            activity=item,
            expected_version=1,
            operation="submit",
            occurred_on=self.today,
            evidence="Synthetic archive/approval-race",
            outcome="Prospect received a schedule",
        )
        operations = (
            ("accept", self.owner_a, "Evidence accepted"),
            ("return", self.owner_b, "Evidence needs clarification"),
        )
        results = self.race(
            *[
                lambda operation=operation, actor=actor, note=note: (
                    activities.update_activity(
                        organization=self.org.pk,
                        actor=actor,
                        activity=item.pk,
                        expected_version=2,
                        operation=operation,
                        review_notes=note,
                    )
                )
                for operation, actor, note in operations
            ]
        )
        self.assertCountEqual(
            [result["status"] for result in results], ["committed", "rejected"]
        )
        loser = next(result for result in results if result["status"] == "rejected")
        self.assertIn(
            "Catatan telah berubah. Muat ulang sebelum menyimpan.", loser["messages"]
        )
        winner_index = next(
            index
            for index, result in enumerate(results)
            if result["status"] == "committed"
        )
        operation, actor, note = operations[winner_index]
        stored = WorkActivity.objects.get(pk=item.pk)
        expected_status = "accepted" if operation == "accept" else "needs_revision"
        self.assertEqual(
            (stored.status, stored.version, stored.reviewed_by, stored.review_notes),
            (expected_status, 3, actor, note),
        )
        self.assertEqual(stored.submitted_by, self.marketing)
        self.assertEqual(stored.evidence, "Synthetic archive/approval-race")
        reviews = AuditEvent.objects.filter(
            organization=self.org,
            object_type="WorkActivity",
            object_id=str(item.pk),
            action__in=("marketing.activity.accepted", "marketing.activity.returned"),
        )
        self.assertEqual(reviews.count(), 1)
        event = reviews.get()
        self.assertEqual(event.actor_id, actor.pk)
        self.assertEqual(event.detail["before"]["status"], "submitted")
        self.assertEqual(event.detail["before"]["version"], 2)
        self.assertEqual(event.detail["after"]["status"], expected_status)
        self.assertEqual(event.detail["after"]["version"], 3)

    def test_competing_handoffs_keep_one_current_pic_and_one_permanent_history(self):
        from .workflow import configure_member

        recipients = []
        for user in (self.owner_a, self.owner_b):
            member = services.create_member(
                organization=self.org,
                actor=self.owner_a,
                name=f"Replacement {user.username}",
                discipline="sales",
            )
            recipients.append(
                configure_member(
                    organization=self.org,
                    actor=self.owner_a,
                    member=member,
                    expected_version=member.version,
                    account=user,
                    active=True,
                    reason="Mapped replacement login",
                )
            )
        item = activities.create_activity(
            organization=self.org,
            actor=self.owner_a,
            title="One assignment winner",
            owner=self.member,
            channel="sales",
            kind="follow_up",
            due_date=self.today,
        )
        actors = (self.owner_a, self.owner_b)
        results = self.race(
            *[
                lambda actor=actor, recipient=recipient: activities.handoff_activity(
                    organization=self.org.pk,
                    actor=actor,
                    activity=item.pk,
                    expected_version=1,
                    member=recipient.pk,
                    reason="Replacement continues task",
                )
                for actor, recipient in zip(actors, recipients)
            ]
        )
        self.assertCountEqual(
            [row["status"] for row in results], ["committed", "rejected"]
        )
        winner = next(
            index for index, row in enumerate(results) if row["status"] == "committed"
        )
        stored = WorkActivity.objects.get(pk=item.pk)
        self.assertEqual(
            (stored.owner_id, stored.current_owner_id, stored.version, stored.status),
            (self.member.pk, recipients[winner].pk, 2, "planned"),
        )
        assignment = stored.assignment_events.get()
        self.assertEqual(
            (
                assignment.previous_member_id,
                assignment.member_id,
                assignment.created_by_id,
                assignment.activity_version,
            ),
            (self.member.pk, recipients[winner].pk, actors[winner].pk, 2),
        )
        self.assertEqual(
            AuditEvent.objects.filter(
                organization=self.org, action="marketing.activity.handed_off"
            ).count(),
            1,
        )
