"""Adding current responsibility preserves existing work and its historical facts."""

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class WorkHandoffMigrationTests(TransactionTestCase):
    def test_existing_work_preserves_owner_evidence_and_status_without_fabricated_handoffs(
        self,
    ):
        executor = MigrationExecutor(connection)
        restore = executor.loader.graph.leaf_nodes()
        old_target = [
            (app, "0006_content_delivery" if app == "marketing" else name)
            for app, name in restore
        ]
        executor.migrate(old_target)
        try:
            old_apps = executor.loader.project_state(old_target).apps
            organization = old_apps.get_model("core", "Organization").objects.create(
                name="Synthetic migration company"
            )
            user = old_apps.get_model("auth", "User").objects.create(
                username="migration-pic"
            )
            member = old_apps.get_model("marketing", "TeamMember").objects.create(
                organization_id=organization.pk,
                created_by_id=user.pk,
                name="Existing PIC",
                discipline="sales",
                account_id=user.pk,
            )
            old_work = old_apps.get_model("marketing", "WorkActivity")
            records = []
            for status in (
                "planned",
                "in_progress",
                "submitted",
                "needs_revision",
                "accepted",
                "cancelled",
            ):
                work = old_work.objects.create(
                    organization_id=organization.pk,
                    created_by_id=user.pk,
                    owner_id=member.pk,
                    title=f"Existing {status}",
                    kind="follow_up",
                    channel="sales",
                    due_date=timezone.localdate(),
                    status=status,
                    version=6,
                    evidence="Existing evidence",
                    outcome="Existing outcome",
                    review_notes="Existing review",
                )
                records.append(work.pk)
            executor = MigrationExecutor(connection)
            executor.migrate(restore)
            new_apps = executor.loader.project_state(restore).apps
            current = new_apps.get_model("marketing", "WorkActivity")
            for pk, status in zip(
                records,
                (
                    "planned",
                    "in_progress",
                    "submitted",
                    "needs_revision",
                    "accepted",
                    "cancelled",
                ),
            ):
                work = current.objects.get(pk=pk)
                self.assertEqual(
                    (work.owner_id, work.current_owner_id, work.created_by_id),
                    (member.pk, member.pk, user.pk),
                )
                self.assertEqual(
                    (
                        work.status,
                        work.version,
                        work.evidence,
                        work.outcome,
                        work.review_notes,
                    ),
                    (
                        status,
                        6,
                        "Existing evidence",
                        "Existing outcome",
                        "Existing review",
                    ),
                )
            self.assertEqual(
                new_apps.get_model("marketing", "ActivityAssignment").objects.count(), 0
            )
        finally:
            MigrationExecutor(connection).migrate(restore)
