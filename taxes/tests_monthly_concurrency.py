"""Real PostgreSQL row-lock proof at public monthly package services."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Barrier
from unittest import skipUnless

from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection, connections
from django.test import TransactionTestCase

from finance import services as finance
from taxes import tests_monthly_packages as fixtures
from taxes.models import (
    MonthlyFiscalApproval,
    MonthlyTaxSourcePackage,
    TaxObligation,
    TaxSource,
)
from taxes.monthly_source_workflow import package_status, prepare_monthly_package


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL row locks")
class MonthlyPackageRaceTests(TransactionTestCase):
    setUp = fixtures.MonthlySourcePackageTests.setUp
    evidence = fixtures.MonthlySourcePackageTests.evidence
    external = fixtures.MonthlySourcePackageTests.external
    profile = fixtures.MonthlySourcePackageTests.profile
    decision = fixtures.MonthlySourcePackageTests.decision
    approve = fixtures.MonthlySourcePackageTests.approve
    sale = fixtures.MonthlySourcePackageTests.sale
    generate = fixtures.MonthlySourcePackageTests.generate

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
                    return "committed", getattr(result, "pk", None)
                except ValidationError:
                    return "rejected", None
            finally:
                connections["default"].close()

        with ThreadPoolExecutor(max_workers=len(actions)) as workers:
            futures = [workers.submit(execute, action) for action in actions]
            return [future.result(timeout=30) for future in futures]

    def prepared(self):
        self.profile("final")
        self.sale()
        return prepare_monthly_package(
            organization=self.org, actor=self.finance, period="2026-09"
        )

    def late_revenue(self):
        return finance.post_journal(
            organization=self.org,
            actor=self.owner,
            date=date(2026, 9, 25),
            source_key="MONTHLY-RACE-LATE",
            description="Synthetic late revenue",
            entries=[("AR", "1", "0"), ("REVENUE", "0", "1")],
        )

    def test_two_preparations_share_one_immutable_source_revision(self):
        self.profile("final")
        self.sale()
        action = lambda: prepare_monthly_package(
            organization=self.org, actor=self.finance, period="2026-09"
        )
        results = self.race(action, action)
        self.assertEqual([row[0] for row in results], ["committed", "committed"])
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(MonthlyTaxSourcePackage.objects.count(), 1)

    def test_two_generations_share_one_draft_without_duplicate_final_payment(self):
        package = self.prepared()
        self.approve(package)
        results = self.race(
            lambda: self.generate(package), lambda: self.generate(package)
        )
        self.assertEqual([row[0] for row in results], ["committed", "committed"])
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(TaxObligation.objects.count(), 1)
        self.assertFalse(
            package_status(organization=self.org, package=package)["stale"]
        )

    def test_source_change_and_approval_serialization_never_approves_changed_snapshot(
        self,
    ):
        package = self.prepared()
        decision = self.decision(package)
        review = self.external(package, decision)
        from taxes.monthly_source_workflow import approve_monthly_package

        results = self.race(
            lambda: approve_monthly_package(
                organization=self.org,
                actor=self.owner,
                package=package,
                decision=decision,
                external_review=review,
                expected_fingerprint=package.package_hash,
                expected_version=package.revision,
            ),
            self.late_revenue,
        )
        self.assertEqual(results[1][0], "committed")
        self.assertIn(results[0][0], ("committed", "rejected"))
        self.assertEqual(
            MonthlyFiscalApproval.objects.count(), int(results[0][0] == "committed")
        )
        self.assertTrue(package_status(organization=self.org, package=package)["stale"])
        with self.assertRaises(ValidationError):
            self.generate(package)
        self.assertFalse(TaxObligation.objects.exists())

    def test_source_change_and_generation_leave_at_most_one_unusable_old_draft(self):
        package = self.prepared()
        self.approve(package)
        results = self.race(lambda: self.generate(package), self.late_revenue)
        self.assertEqual(results[1][0], "committed")
        self.assertIn(results[0][0], ("committed", "rejected"))
        self.assertEqual(
            TaxObligation.objects.count(), int(results[0][0] == "committed")
        )
        self.assertTrue(package_status(organization=self.org, package=package)["stale"])
        with self.assertRaises(ValidationError):
            self.generate(package)

    def test_legal_publication_and_generation_are_serialized_without_adopting_changed_rules(
        self,
    ):
        from taxes.knowledge import activate_source_revision, publish_catalog, snapshot
        from taxes.services import SOURCE_CATALOG
        from taxes.workflow import digest

        package = self.prepared()
        self.approve(package)
        item = dict(
            next(item for item in SOURCE_CATALOG if item["slug"] == "pmk164-2023")
        )
        item["summary"] += " Synthetic reviewed next publication."
        publish_catalog([item], date(2026, 10, 9))
        source = TaxSource.objects.get(slug=item["slug"])
        revision = source.revisions.exclude(
            content_hash=digest(snapshot(source))
        ).latest("pk")
        results = self.race(
            lambda: self.generate(package),
            lambda: activate_source_revision(
                revision=revision,
                reviewer_name="Synthetic publishing reviewer",
                review_reference="Synthetic racing source publication",
                confirm_reviewed=True,
            ),
        )
        self.assertEqual(results[1][0], "committed")
        self.assertIn(results[0][0], ("committed", "rejected"))
        self.assertEqual(
            TaxObligation.objects.count(), int(results[0][0] == "committed")
        )
        self.assertTrue(package_status(organization=self.org, package=package)["stale"])
        with self.assertRaises(ValidationError):
            self.generate(package)

    def test_same_review_revision_request_serializes_to_one_new_version(self):
        from taxes.monthly_source_workflow import revise_monthly_review

        package = self.prepared()
        self.approve(package)

        def revise():
            return revise_monthly_review(
                organization=self.org,
                actor=self.owner,
                package=package,
                reason="Synthetic same-reason concurrent professional review correction",
                expected_fingerprint=package.package_hash,
                expected_version=package.revision,
            )

        results = self.race(revise, revise)
        self.assertEqual([status for status, _ in results], ["committed", "committed"])
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(
            MonthlyTaxSourcePackage.objects.filter(period=package.period).count(), 2
        )
        self.assertTrue(
            package_status(organization=self.org, package=package)["superseded"]
        )

    def test_revision_and_dependent_draft_generation_cannot_both_commit(self):
        from taxes.monthly_source_workflow import revise_monthly_review

        self.profile("final")
        self.sale(
            suffix="RACE-FEB", issued=date(2026, 2, 5), delivered=date(2026, 2, 10)
        )
        february = prepare_monthly_package(
            organization=self.org, actor=self.finance, period="2026-02"
        )
        self.approve(february)
        self.sale(
            suffix="RACE-MAR", issued=date(2026, 3, 5), delivered=date(2026, 3, 10)
        )
        march = prepare_monthly_package(
            organization=self.org, actor=self.finance, period="2026-03"
        )
        self.approve(march, self.decision(march, prior_history_confirmed=True))
        results = self.race(
            lambda: revise_monthly_review(
                organization=self.org,
                actor=self.owner,
                package=february,
                reason="Synthetic racing review correction must preserve dependent liability boundary",
                expected_fingerprint=february.package_hash,
                expected_version=february.revision,
            ),
            lambda: self.generate(march),
        )
        self.assertEqual(
            sorted(status for status, _ in results), ["committed", "rejected"]
        )
        self.assertEqual(
            TaxObligation.objects.count(), int(results[1][0] == "committed")
        )
        self.assertEqual(
            MonthlyTaxSourcePackage.objects.filter(period=february.period).count(),
            1 + int(results[0][0] == "committed"),
        )
