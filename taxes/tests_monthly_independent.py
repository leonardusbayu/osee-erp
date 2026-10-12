"""Independent adversarial public monthly contracts; synthetic facts only."""

from datetime import date, datetime
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase

from core.models import Membership, Organization
from finance import services as finance
from finance import tests_remediation as finance_fixtures
from finance.fulfillment import deliver_units

from . import tests_monthly_packages as package_fixtures
from .models import TaxObligation
from .monthly_source_workflow import (
    generate_monthly_obligation,
    monthly_decision,
    package_status,
    prepare_monthly_package,
)


class IndependentMonthlyContractTests(TestCase):
    invoice = finance_fixtures.FinanceRemediationTests.invoice
    movement = finance_fixtures.FinanceRemediationTests.movement
    evidence = package_fixtures.MonthlySourcePackageTests.evidence
    external = package_fixtures.MonthlySourcePackageTests.external
    profile = package_fixtures.MonthlySourcePackageTests.profile
    decision = package_fixtures.MonthlySourcePackageTests.decision
    approve = package_fixtures.MonthlySourcePackageTests.approve

    def setUp(self):
        finance_fixtures.FinanceRemediationTests.setUp(self)
        self.owner = self.actor

    def delivered_invoice(self, reference):
        day = date(2026, 9, 10)
        row = finance.issue_invoice(
            organization=self.org,
            actor=self.actor,
            invoice=self.invoice(date=day, service_date=day),
        )
        finance.reconcile_receipt(
            organization=self.org,
            actor=self.actor,
            invoice=row,
            transaction=self.movement(day=day, reference=reference),
            amount="500000",
        )
        deliver_units(
            organization=self.org,
            actor=self.actor,
            invoice=row,
            quantity=1,
            date=day,
            reference=reference,
            evidence="Synthetic attendance",
        )
        return row

    def test_cached_organization_cannot_hide_a_changed_legal_entity(self):
        package = prepare_monthly_package(
            organization=self.org, actor=self.actor, period="2026-09"
        )
        Organization.objects.filter(pk=self.org.pk).update(legal_form="foundation")
        # Intentionally retain the caller's ordinary-PT ORM instance.
        self.assertNotEqual(
            self.org.legal_form, Organization.objects.get(pk=self.org.pk).legal_form
        )
        self.assertTrue(package_status(organization=self.org, package=package)["stale"])
        replacement = prepare_monthly_package(
            organization=self.org, actor=self.actor, period="2026-09"
        )
        self.assertEqual(replacement.revision, 2)
        self.assertEqual(replacement.previous_id, package.pk)
        self.assertEqual(
            replacement.profile_snapshot["organization"]["legal_form"], "foundation"
        )

    def test_datetime_is_rejected_as_a_month_without_a_server_error(self):
        with self.assertRaises(ValidationError):
            prepare_monthly_package(
                organization=self.org,
                actor=self.actor,
                period=datetime(2026, 9, 1, 12, 30),
            )
        self.assertFalse(self.org.monthlytaxsourcepackage_set.exists())

    def test_legacy_read_only_role_cannot_prepare_through_public_module(self):
        Membership.objects.filter(organization=self.org, user=self.actor).update(
            role="auditor"
        )
        with self.assertRaises(PermissionDenied):
            prepare_monthly_package(
                organization=self.org, actor=self.actor, period="2026-09"
            )
        self.assertFalse(self.org.monthlytaxsourcepackage_set.exists())

    def test_other_company_cannot_read_a_package_by_a_known_id(self):
        package = prepare_monthly_package(
            organization=self.org, actor=self.actor, period="2026-09"
        )
        other = Organization.objects.create(name="Synthetic other monthly company")
        with self.assertRaises(ValidationError):
            package_status(organization=other, package=package.pk)

    def test_repeat_preparation_returns_the_original_without_rewriting_history(self):
        package = prepare_monthly_package(
            organization=self.org, actor=self.actor, period="2026-09"
        )
        old_snapshot = package.snapshot.copy()
        repeated = prepare_monthly_package(
            organization=self.org, actor=self.actor, period="2026-09"
        )
        package.refresh_from_db()
        self.assertEqual(repeated.pk, package.pk)
        self.assertEqual(package.snapshot, old_snapshot)
        self.assertEqual(self.org.monthlytaxsourcepackage_set.count(), 1)

    def test_gross_adjustment_is_a_delta_and_draft_generation_is_once_only(self):
        self.profile("final")
        invoice = self.delivered_invoice("INDEPENDENT-GROSS")
        package = prepare_monthly_package(
            organization=self.org, actor=self.actor, period="2026-09"
        )
        source = next(
            row for row in package.snapshot["rows"] if row["role"] == "candidate"
        )
        decision = self.decision(
            package,
            adjustments=[
                {
                    "kind": "gross_basis",
                    "amount": "40000",
                    "reason": "Synthetic contract establishes gross before actual discount",
                    "evidence_id": self.evidence().pk,
                    "source_row_ids": [source["row_id"]],
                }
            ],
        )
        approved = self.approve(package, decision)
        self.assertEqual(approved.result["taxable_turnover"], "540000")
        request = dict(
            organization=self.org,
            actor=self.actor,
            package=package,
            expected_fingerprint=package.package_hash,
            expected_version=package.revision,
        )
        first = generate_monthly_obligation(**request)
        again = generate_monthly_obligation(**request)
        self.assertEqual((first.base, first.amount), (Decimal(540000), Decimal(2700)))
        self.assertEqual(first.pk, again.pk)
        self.assertEqual(
            TaxObligation.objects.filter(source_package=package).count(), 1
        )
        invoice.refresh_from_db()
        self.assertEqual(
            (invoice.total, invoice.paid_amount), (Decimal(500000), Decimal(500000))
        )

    def test_excess_source_deduction_cannot_be_hidden_by_another_invoice(self):
        self.profile("final")
        first = self.delivered_invoice("INDEPENDENT-FIRST")
        self.delivered_invoice("INDEPENDENT-SECOND")
        package = prepare_monthly_package(
            organization=self.org, actor=self.actor, period="2026-09"
        )
        source = next(
            row
            for row in package.snapshot["rows"]
            if row["role"] == "candidate" and row["invoice_id"] == first.pk
        )
        with self.assertRaises(ValidationError):
            self.decision(
                package,
                adjustments=[
                    {
                        "kind": "correction",
                        "amount": "-600000",
                        "reason": "Synthetic excessive deduction exceeds source gross",
                        "evidence_id": self.evidence().pk,
                        "source_row_ids": [source["row_id"]],
                    }
                ],
            )
        self.assertFalse(TaxObligation.objects.exists())

    def test_source_absence_requires_explicit_completeness_and_nil_review(self):
        self.profile("final")
        package = prepare_monthly_package(
            organization=self.org, actor=self.actor, period="2026-09"
        )
        with self.assertRaises(ValidationError):
            monthly_decision(
                package,
                rows=[],
                completeness_confirmed=False,
                inventory_note="Synthetic inventory absent",
                nil_confirmed=True,
                self_payment_only_confirmed=True,
                object_code="411128-420",
                rule_reference="Synthetic review",
            )
        with self.assertRaises(ValidationError):
            self.decision(package, nil_confirmed=False)
        self.assertIsNone(
            package_status(organization=self.org, package=package)["taxable_turnover"]
        )
        self.assertFalse(TaxObligation.objects.exists())
