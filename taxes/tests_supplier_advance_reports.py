"""Real synthetic source/review/package journeys across report consumers."""

from copy import deepcopy
from datetime import date

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from finance.models import JournalLine

from . import tests_monthly_packages as package_fixtures
from . import workflow
from .monthly_source_workflow import (
    package_status,
    prepare_monthly_package,
    revise_monthly_review,
)
from .monthly_sources import build_monthly_sources
from .supplier_advance_dependencies import supplier_advance_review_correction_blockers
from .supplier_advance_review import record_supplier_advance_review
from .tests_supplier_advance_review import SupplierAdvanceReviewFixtures


class SupplierAdvanceReportTests(SupplierAdvanceReviewFixtures, TestCase):
    profile = package_fixtures.MonthlySourcePackageTests.profile
    decision = package_fixtures.MonthlySourcePackageTests.decision
    approve = package_fixtures.MonthlySourcePackageTests.approve

    def setUp(self):
        super().setUp()
        self.owner = self.finance = self.actor

    def package(self):
        return prepare_monthly_package(
            organization=self.org, actor=self.actor, period=date(2026, 9, 1)
        )

    def test_professional_review_changes_package_dependency_without_changing_income_source(
        self,
    ):
        advance = self.advance()
        first = self.package()
        saved = deepcopy(first.profile_snapshot)
        self.zero_review(advance)
        second = self.package()
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(second.source_hash, first.source_hash)
        self.assertNotEqual(second.package_hash, first.package_hash)
        self.assertTrue(package_status(organization=self.org, package=first)["stale"])
        first.refresh_from_db()
        self.assertEqual(first.profile_snapshot, saved)

    def test_application_and_refund_do_not_change_original_cash_review(self):
        advance = self.advance()
        self.zero_review(advance)
        original = self.context(advance)["source_fingerprint"]
        bill = self.bill("600", day=date(2026, 1, 2))
        self.command(
            "apply", advance=advance, bill=bill, amount="600", date=date(2026, 1, 3)
        )
        self.command(
            "refund",
            advance=advance,
            amount="400",
            transaction=self.movement(
                "400", day=date(2026, 1, 4), reference="REPORT-REFUND"
            ),
        )
        self.assertEqual(
            (
                self.context(advance)["status"],
                self.context(advance)["source_fingerprint"],
            ),
            ("reviewed", original),
        )
        source = build_monthly_sources(self.org, date(2026, 9, 1))
        principal = source["inventory"]["supplier_advances"][0]
        self.assertEqual(
            (
                principal["remaining"],
                principal["income_effect"],
                source["book_revenue"],
            ),
            ("0.00", "0.00", "0.00"),
        )
        self.assertFalse(
            any(row["source_kind"].startswith("supplier") for row in source["rows"])
        )

    def test_annual_snapshot_tracks_review_revision_and_no_advance_preserves_old_shape(
        self,
    ):
        before = workflow.annual_tax_snapshot(
            self.org, date(2026, 1, 1), date(2026, 12, 31)
        )
        self.assertNotIn("supplier_advance_reviews", before)
        advance = self.advance()
        pending = workflow.annual_tax_snapshot(
            self.org, date(2026, 1, 1), date(2026, 12, 31)
        )
        self.zero_review(advance)
        reviewed = workflow.annual_tax_snapshot(
            self.org, date(2026, 1, 1), date(2026, 12, 31)
        )
        self.assertNotEqual(pending, reviewed)
        self.assertEqual(
            reviewed["supplier_advance_reviews"]["inventory"][0]["status"], "reviewed"
        )

    def test_pending_advance_blocks_package_approval_even_with_received_monthly_report(
        self,
    ):
        self.profile()
        self.advance()
        package = self.package()
        with self.assertRaises(ValidationError):
            self.approve(package)

    def test_used_review_needs_explicit_monthly_revision_before_new_cash_decision(self):
        self.profile()
        advance = self.advance()
        self.zero_review(advance)
        package = self.package()
        self.approve(package)
        self.assertTrue(
            supplier_advance_review_correction_blockers(
                organization=self.org, advance=advance
            )
        )
        data = self.review_data(advance)
        with self.assertRaises(ValidationError):
            record_supplier_advance_review(**data)
        revised = revise_monthly_review(
            organization=self.org,
            actor=self.actor,
            package=package,
            reason="Synthetic explicit review revision preserving old package.",
            expected_fingerprint=package.package_hash,
            expected_version=package.revision,
        )
        self.assertNotEqual(revised.pk, package.pk)
        self.assertFalse(
            supplier_advance_review_correction_blockers(
                organization=self.org, advance=advance
            )
        )
        record_supplier_advance_review(**self.review_data(advance))
        self.assertTrue(package_status(organization=self.org, package=revised)["stale"])

    def test_damaged_original_journal_is_hard_source_issue_and_not_fake_zero(self):
        advance = self.advance()
        line = JournalLine.objects.get(
            journal_id=advance.journal_id, account="SUPPLIER_ADVANCE"
        )
        self.tamper(JournalLine, line.pk, "account", "EXPENSE")
        source = build_monthly_sources(self.org, date(2026, 9, 1))
        self.assertIsNone(source["inventory"]["supplier_advances"][0]["remaining"])
        self.assertTrue(
            any(
                row["code"] == "supplier_advance_integrity" and row["hard_blocker"]
                for row in source["issues"]
            )
        )

    def test_csv_keeps_cash_review_separate_without_inventing_gross_or_tax_amount(self):
        advance = self.advance()
        response = self.client.get(reverse("taxes:export"), {"period": "2026-09"})
        self.assertEqual(response.status_code, 200)
        import csv
        import io

        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
        candidate = next(
            row
            for row in rows
            if row and row[0] == "supplier_advance_cash_review_candidate"
        )
        self.assertEqual(candidate[4:8], ["", "", "", ""])
        self.assertEqual(candidate[11], f"supplier-advance:{advance.pk}")

    def test_corrupted_approved_dependency_inventory_cannot_release_a_used_review(self):
        import json

        from .models import MonthlyTaxSourcePackage

        self.profile()
        advance = self.advance()
        self.zero_review(advance)
        package = self.package()
        self.approve(package)
        damaged = deepcopy(package.profile_snapshot)
        damaged["supplier_advance_reviews"]["inventory"] = []
        self.tamper(
            MonthlyTaxSourcePackage, package.pk, "profile_snapshot", json.dumps(damaged)
        )
        with self.assertRaises(ValidationError):
            record_supplier_advance_review(**self.review_data(advance))

    def test_malformed_fiscal_dependency_is_a_validation_hold_not_type_error(self):
        import json

        from .models import MonthlyTaxSourcePackage

        advance = self.advance()
        self.zero_review(advance)
        package = self.package()
        damaged = deepcopy(package.profile_snapshot)
        damaged["fiscal_history"] = [{"package_id": []}]
        self.tamper(
            MonthlyTaxSourcePackage, package.pk, "profile_snapshot", json.dumps(damaged)
        )
        self.tamper(
            MonthlyTaxSourcePackage,
            package.pk,
            "package_hash",
            workflow.digest(
                {
                    "snapshot": package.snapshot,
                    "profile": damaged,
                    "legal": package.legal_sources,
                }
            ),
        )
        with self.assertRaises(ValidationError):
            supplier_advance_review_correction_blockers(
                organization=self.org, advance=advance
            )

    def test_changed_date_cannot_hide_an_original_bank_event_from_monthly_tax_queue(
        self,
    ):
        from finance.models import SupplierAdvance

        from .services import monthly_tax_context

        advance = self.advance()
        self.tamper(SupplierAdvance, advance.pk, "date", "2027-01-01")
        context = monthly_tax_context(self.org, date(2026, 9, 1))
        self.assertEqual(
            context["supplier_advance_tax_candidates"][0]["advance_id"], advance.pk
        )
        self.assertNotEqual(
            context["supplier_advance_tax_candidates"][0]["status"], "reviewed"
        )
