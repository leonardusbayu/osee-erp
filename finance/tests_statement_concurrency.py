"""A stale double confirmation cannot create competing period approvals."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless

from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection, connections
from django.test import TransactionTestCase, override_settings

from core.models import AuditEvent
from webapp import test_statements as intake

from . import tests_statement_periods as coverage
from .statement_models import StatementPeriodReview
from .statement_services import preview_statement_period, review_statement_period


@override_settings(MFA_REQUIRED=False)
@skipUnless(connection.vendor == "postgresql", "Requires real PostgreSQL row locks")
class ConcurrentStatementReviewTests(TransactionTestCase):
    setUp = intake.StatementWorkflowTests.setUp
    upload = intake.StatementWorkflowTests.upload
    preview = intake.StatementWorkflowTests.preview
    confirm = intake.StatementWorkflowTests.confirm
    imported = coverage.StatementCoverageTests.imported
    facts = coverage.StatementCoverageTests.facts

    def test_two_confirmations_record_only_one_period_approval(self):
        source = self.imported()
        facts = self.facts(source)
        preview = preview_statement_period(**facts)
        ready = Barrier(2)

        def approve():
            close_old_connections()
            try:
                self.assertTrue(
                    connections["default"].settings_dict["NAME"].startswith("test_")
                )
                ready.wait(timeout=10)
                try:
                    review_statement_period(
                        **facts,
                        expected_fingerprint=preview["fingerprint"],
                        confirmed=True,
                    )
                    return "committed"
                except ValidationError:
                    return "rejected"
            finally:
                connections["default"].close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = [pool.submit(approve) for _ in range(2)]
            result = [future.result(timeout=30) for future in pending]
        self.assertCountEqual(result, ["committed", "rejected"])
        self.assertEqual(StatementPeriodReview.objects.count(), 1)
        self.assertEqual(
            AuditEvent.objects.filter(
                action="finance.statement.period_reviewed"
            ).count(),
            1,
        )
