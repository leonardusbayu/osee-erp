"""Public monthly package contracts, using synthetic company data only."""

import io
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from pypdf import PdfWriter

from core.models import Membership, Organization
from taxes import workflow as work
from taxes.knowledge import publish_catalog, reviewed_sources_for_period
from taxes.models import (
    MonthlyFiscalApproval,
    TaxObligation,
    TaxProfile,
    TaxSource,
    TaxVerification,
)
from taxes.monthly_source_workflow import package_status, prepare_monthly_package


@override_settings(OPENROUTER_API_KEY="")
class MonthlySourcePackageTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(
            name="Synthetic monthly package", is_demo=True
        )
        self.owner = get_user_model().objects.create_user(
            username="monthly-source-owner"
        )
        self.finance = get_user_model().objects.create_user(
            username="monthly-source-finance"
        )
        self.marketing = get_user_model().objects.create_user(
            username="monthly-source-marketing"
        )
        for user, role in (
            (self.owner, "owner"),
            (self.finance, "finance"),
            (self.marketing, "marketing"),
        ):
            Membership.objects.create(organization=self.org, user=user, role=role)

    def evidence(self):
        output = io.BytesIO()
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.write(output)
        return work.upload_evidence(
            organization=self.org,
            actor=self.owner,
            file=SimpleUploadedFile("synthetic-monthly.pdf", output.getvalue()),
        )

    def external(self, subject, decision):
        return work.record_external_review(
            organization=self.org,
            actor=self.owner,
            subject=subject,
            decision=decision,
            evidence=self.evidence(),
            professional_name="Synthetic professional",
            qualification_reference="Synthetic monthly engagement",
            reviewed_on=date(2026, 10, 9),
            statement="Synthetic report confirms exactly the displayed classifications",
            report_confirmed=True,
        )

    def profile(self, regime="normal"):
        from taxes.services import SOURCE_CATALOG

        publish_catalog(
            SOURCE_CATALOG,
            date(2026, 10, 9),
            reviewed_by="Synthetic source reviewer",
            review_reference="Synthetic legal review",
        )
        row = TaxProfile.objects.create(
            organization=self.org,
            registration_date=date(2024, 1, 1),
            registration_evidence="Synthetic SKT",
            taxpayer_reference="1234567890123456",
            vat_status_evidence="Synthetic non-PKP",
            vat_status_effective_from=date(2024, 1, 1),
        )
        values = {"regime": regime, "effective_from": date(2026, 1, 1)}
        if regime == "final":
            values.update(
                final_regime_start_date=date(2024, 1, 1),
                final_regime_end_date=date(2026, 12, 31),
                transition_evidence="Synthetic legacy facility",
                qualifying_turnover="1000000000",
                legacy_eligibility_confirmed=True,
                no_normal_election_confirmed=True,
            )
        review = self.external(row, work.profile_decision(**values))
        return work.approve_tax_profile(
            row,
            self.owner,
            review_note="Synthetic reviewed profile",
            external_review=review,
            **values,
        )

    def decision(self, package, **changes):
        from taxes.monthly_source_workflow import monthly_decision

        values = {
            "rows": [
                {
                    "row_id": row["row_id"],
                    "decision": "include"
                    if row["role"] == "candidate" and row.get("proposed_inclusion")
                    else "exclude",
                    "reason": "Synthetic documented period classification",
                }
                for row in package.snapshot["rows"]
            ],
            "completeness_confirmed": True,
            "inventory_note": "Synthetic all products channels and branches reviewed",
            "nil_confirmed": True,
            "self_payment_only_confirmed": True,
            "customer_final_withholding": False,
            "object_code": "411128-420",
            "rule_reference": "Synthetic reviewed period rules",
        }
        values.update(changes)
        return monthly_decision(package, **values)

    def approve(self, package, decision=None, **changes):
        from taxes.monthly_source_workflow import approve_monthly_package

        decision = decision or self.decision(package)
        values = dict(
            organization=self.org,
            actor=self.owner,
            package=package,
            decision=decision,
            external_review=self.external(package, decision),
            expected_fingerprint=package.package_hash,
            expected_version=package.revision,
        )
        values.update(changes)
        return approve_monthly_package(**values)

    def sale(
        self,
        amount="500000",
        suffix="1",
        issued=date(2026, 9, 5),
        delivered=date(2026, 9, 10),
    ):
        from finance import services as finance
        from finance.models import Party, Product

        buyer = Party.objects.create(
            organization=self.org, name=f"Synthetic buyer {suffix}", kind="customer"
        )
        product = Product.objects.create(
            organization=self.org,
            code=f"SYN-{suffix}",
            name=f"Synthetic test {suffix}",
            kind="itp",
            default_price=amount,
        )
        invoice = finance.create_invoice(
            organization=self.org,
            actor=self.owner,
            party=buyer,
            product=product,
            quantity=1,
            date=issued,
            service_date=delivered,
        )
        invoice = finance.issue_invoice(
            organization=self.org, actor=self.owner, invoice=invoice
        )
        if delivered:
            from finance.fulfillment import deliver_units

            deliver_units(
                organization=self.org,
                actor=self.owner,
                invoice=invoice,
                quantity=1,
                date=delivered,
                reference=f"SYN-DELIVERY-{suffix}",
                evidence="Synthetic attendance",
            )
        return invoice

    def generate(self, package, **changes):
        from taxes.monthly_source_workflow import generate_monthly_obligation

        values = dict(
            organization=self.org,
            actor=self.finance,
            package=package,
            expected_fingerprint=package.package_hash,
            expected_version=package.revision,
        )
        values.update(changes)
        return generate_monthly_obligation(**values)

    def test_finance_prepares_immutable_empty_source_without_claiming_nil(self):
        package = prepare_monthly_package(
            organization=self.org, actor=self.finance, period="2026-09"
        )
        self.assertEqual(package.period, date(2026, 9, 1))
        self.assertEqual(package.cutoff, date(2026, 9, 30))
        self.assertEqual(package.revision, 1)
        status = package_status(organization=self.org, package=package)
        self.assertIsNone(status["taxable_turnover"])
        self.assertFalse(status["can_generate"])
        self.assertEqual(package.snapshot["inventory"]["completeness"], "unknown")
        package.source_hash = "0" * 64
        with self.assertRaises(ValidationError):
            package.save()
        with self.assertRaises(PermissionDenied):
            prepare_monthly_package(
                organization=self.org, actor=self.marketing, period="2026-09"
            )

    def test_owner_records_reviewed_normal_nil_package_without_final_tax(self):
        self.profile()
        package = prepare_monthly_package(
            organization=self.org, actor=self.finance, period="2026-09"
        )
        approval = self.approve(package)
        self.assertEqual(approval.result["reviewed_bridge_amount"], "0.00")
        self.assertIsNone(approval.result["taxable_turnover"])
        status = package_status(organization=self.org, package=package)
        self.assertEqual(status["status"], "approved")
        self.assertIsNone(status["final_tax"])
        self.assertFalse(status["can_generate"])
        approval.result = {"final_tax": "1"}
        with self.assertRaises(ValidationError):
            approval.save()

    def test_negative_correction_cannot_be_hidden_by_another_invoice_income(self):
        self.profile("final")
        first = self.sale("100000", suffix="NEG-A")
        self.sale("200000", suffix="NEG-B")
        package = prepare_monthly_package(
            organization=self.org, actor=self.finance, period="2026-09"
        )
        candidate = next(
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
                        "amount": "-150000",
                        "reason": "Synthetic invalid excessive correction",
                        "evidence_id": self.evidence().pk,
                        "source_row_ids": [candidate["row_id"]],
                    }
                ],
            )

    def test_reviewed_final_gross_delta_generates_exact_idempotent_draft(self):
        self.profile("final")
        self.sale()
        package = prepare_monthly_package(
            organization=self.org, actor=self.finance, period="2026-09"
        )
        candidate = next(
            row for row in package.snapshot["rows"] if row["role"] == "candidate"
        )
        decision = self.decision(
            package,
            adjustments=[
                {
                    "kind": "gross_basis",
                    "amount": "40000",
                    "reason": "Synthetic documented gross before discount",
                    "evidence_id": self.evidence().pk,
                    "source_row_ids": [candidate["row_id"]],
                }
            ],
        )
        approval = self.approve(package, decision)
        self.assertEqual(approval.result["taxable_turnover"], "540000")
        row = self.generate(package)
        self.assertEqual(
            (row.base, row.amount, row.status),
            (Decimal(540000), Decimal(2700), "review"),
        )
        self.assertEqual(row.source_package_id, package.pk)
        self.assertEqual(self.generate(package).pk, row.pk)
        self.assertEqual(TaxObligation.objects.count(), 1)
        self.assertFalse(
            package_status(organization=self.org, package=package)["stale"]
        )
        approved = work.approve_tax_obligation(
            row,
            self.owner,
            external_review=self.external(row, {"action": "approve_obligation"}),
        )
        self.assertEqual(approved.status, "approved")

    def test_linked_draft_cannot_bypass_new_module_roles_through_legacy_services(self):
        self.profile("final")
        self.sale()
        package = prepare_monthly_package(
            organization=self.org, actor=self.finance, period="2026-09"
        )
        self.approve(package)
        reviewer = get_user_model().objects.create_user(
            username="monthly-bound-reviewer"
        )
        Membership.objects.create(organization=self.org, user=reviewer, role="reviewer")
        with self.assertRaises(PermissionDenied):
            work.prepare_obligation(
                organization=self.org,
                actor=reviewer,
                period="2026-09",
                rule_code="final_turnover_005",
                base="500000",
                direction="own_tax",
                source_reference="Synthetic source bypass",
                rule_reference="Synthetic rules",
                object_code="411128-420",
                source_evidence=self.evidence(),
                source_package=package,
            )
        row = self.generate(package)
        with self.assertRaises(PermissionDenied):
            work.approve_tax_obligation(
                row,
                reviewer,
                external_review=self.external(row, {"action": "approve_obligation"}),
            )
        self.assertEqual(TaxObligation.objects.get(pk=row.pk).status, "review")

    def test_negative_supported_delta_changes_one_invoice_without_double_reduction(
        self,
    ):
        self.profile("final")
        self.sale()
        package = prepare_monthly_package(
            organization=self.org, actor=self.finance, period="2026-09"
        )
        candidate = next(
            row for row in package.snapshot["rows"] if row["role"] == "candidate"
        )
        approval = self.approve(
            package,
            self.decision(
                package,
                adjustments=[
                    {
                        "kind": "correction",
                        "amount": "-100000",
                        "reason": "Synthetic evidenced fiscal difference",
                        "evidence_id": self.evidence().pk,
                        "source_row_ids": [candidate["row_id"]],
                    }
                ],
            ),
        )
        self.assertEqual(approval.result["reviewed_bridge_amount"], "400000.00")
        self.assertEqual(self.generate(package).amount, Decimal(2000))

    def test_all_context_must_be_reviewed_and_cannot_be_implicitly_included(self):
        self.profile("final")
        self.sale()
        package = prepare_monthly_package(
            organization=self.org, actor=self.finance, period="2026-09"
        )
        rows = self.decision(package)["rows"]
        with self.assertRaises(ValidationError):
            self.decision(
                package,
                rows=[row for row in rows if row["row_id"].startswith("revenue-line:")],
            )
        contextual = next(
            row for row in rows if not row["row_id"].startswith("revenue-line:")
        )
        for choice in ("include", "adjustment"):
            with self.subTest(choice=choice), self.assertRaises(ValidationError):
                self.decision(
                    package,
                    rows=[
                        {**row, "decision": choice}
                        if row["row_id"] == contextual["row_id"]
                        else row
                        for row in rows
                    ],
                )

    def test_final_withheld_by_customer_and_normal_generation_are_safe_gated(self):
        self.profile("final")
        package = prepare_monthly_package(
            organization=self.org, actor=self.finance, period="2026-09"
        )
        for changes in (
            {"customer_final_withholding": True},
            {"self_payment_only_confirmed": False},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.decision(package, **changes)
        # Separate ordinary-PT company, reviewed as normal despite small turnover.
        self.org = Organization.objects.create(
            name="Synthetic normal regime", is_demo=True
        )
        Membership.objects.create(organization=self.org, user=self.owner, role="owner")
        Membership.objects.create(
            organization=self.org, user=self.finance, role="finance"
        )
        self.profile()
        normal = prepare_monthly_package(
            organization=self.org, actor=self.finance, period="2026-09"
        )
        self.approve(normal)
        with self.assertRaisesMessage(ValidationError, "22%"):
            self.generate(normal)
        self.assertFalse(TaxObligation.objects.filter(organization=self.org).exists())

    def test_missing_completeness_nil_and_false_boolean_do_not_approve_empty_month(
        self,
    ):
        self.profile("final")
        package = prepare_monthly_package(
            organization=self.org, actor=self.finance, period="2026-09"
        )
        for changes in (
            {"completeness_confirmed": False},
            {"inventory_note": ""},
            {"nil_confirmed": False},
            {"completeness_confirmed": "yes"},
            {"rule_reference": ""},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.decision(package, **changes)
        self.assertFalse(MonthlyFiscalApproval.objects.exists())

    def test_wrong_roles_foreign_package_and_page_version_rejected_publicly(self):
        self.profile("final")
        package = prepare_monthly_package(
            organization=self.org, actor=self.finance, period="2026-09"
        )
        reviewer = get_user_model().objects.create_user(
            username="monthly-legacy-reviewer"
        )
        Membership.objects.create(organization=self.org, user=reviewer, role="reviewer")
        with self.assertRaises(PermissionDenied):
            prepare_monthly_package(
                organization=self.org, actor=reviewer, period="2026-09"
            )
        for actor in (self.finance, self.marketing, reviewer):
            with (
                self.subTest(actor=actor.username),
                self.assertRaises(PermissionDenied),
            ):
                self.approve(package, actor=actor)
        for changes in (
            {"expected_fingerprint": "0" * 64},
            {"expected_version": 2},
            {"expected_version": True},
            {"expected_fingerprint": None},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.approve(package, **changes)
        other = Organization.objects.create(name="Synthetic foreign", is_demo=True)
        Membership.objects.create(organization=other, user=self.owner, role="owner")
        with self.assertRaises(ValidationError):
            package_status(organization=other, package=package)

    def test_stale_data_rejects_generation_obligation_approval_and_official_verification(
        self,
    ):
        from finance import services as finance

        self.profile("final")
        self.sale()
        package = prepare_monthly_package(
            organization=self.org, actor=self.finance, period="2026-09"
        )
        self.approve(package)
        row = self.generate(package)
        review = self.external(row, {"action": "approve_obligation"})
        finance.post_journal(
            organization=self.org,
            actor=self.owner,
            date=date(2026, 9, 25),
            source_key="SYN-LATE-REVENUE",
            description="Synthetic late income",
            entries=[("AR", "1", "0"), ("REVENUE", "0", "1")],
        )
        self.assertTrue(package_status(organization=self.org, package=package)["stale"])
        with self.assertRaises(ValidationError):
            self.generate(package)
        with self.assertRaises(ValidationError):
            work.approve_tax_obligation(row, self.owner, external_review=review)
        with self.assertRaises(ValidationError):
            work.verify_obligation_evidence(
                organization=self.org,
                actor=self.owner,
                obligation=row,
                kind="payment",
                evidence=self.evidence(),
                reference="SYN-NTPN-LATE",
                taxpayer_reference="1234567890123456",
                period="2026-09",
                amount="2500",
                note="Synthetic proof",
                matches_confirmed=True,
            )
        self.assertFalse(TaxVerification.objects.exists())
        replacement = prepare_monthly_package(
            organization=self.org, actor=self.finance, period="2026-09"
        )
        self.assertEqual(
            (replacement.revision, replacement.previous_id), (2, package.pk)
        )

    def test_changed_report_decision_and_legacy_obligation_conflict_cannot_generate(
        self,
    ):
        self.profile("final")
        self.sale()
        package = prepare_monthly_package(
            organization=self.org, actor=self.finance, period="2026-09"
        )
        decision = self.decision(package)
        review = self.external(package, decision)
        from taxes.monthly_source_workflow import approve_monthly_package

        with self.assertRaises(ValidationError):
            approve_monthly_package(
                organization=self.org,
                actor=self.owner,
                package=package,
                decision={**decision, "inventory_note": "Changed report scope"},
                external_review=review,
                expected_fingerprint=package.package_hash,
                expected_version=package.revision,
            )
        self.approve(package, decision)
        work.prepare_obligation(
            organization=self.org,
            actor=self.owner,
            period="2026-09",
            rule_code="final_turnover_005",
            base="500000",
            direction="own_tax",
            source_reference="Synthetic manual final",
            rule_reference="Synthetic manual report",
            object_code="411128-420",
            source_evidence=self.evidence(),
        )
        with self.assertRaises(ValidationError):
            self.generate(package)
        self.assertEqual(TaxObligation.objects.count(), 1)

    def test_prepaid_receipt_cannot_be_taxed_again_at_later_delivery(self):
        from finance import services as finance
        from finance.models import BankAccount, BankTransaction

        self.profile("final")
        invoice = self.sale(issued=date(2026, 1, 5), delivered=date(2026, 3, 10))
        account = BankAccount.objects.create(
            organization=self.org,
            name="Synthetic prepaid bank",
            account_number="MONTHLY-PREPAID",
        )
        finance.import_bank_csv(
            organization=self.org,
            actor=self.owner,
            account=account,
            content="date,reference,description,amount\n2026-02-10,MONTHLY-PREPAID,Synthetic,500000\n",
        )
        bank = BankTransaction.objects.get(
            organization=self.org, reference="MONTHLY-PREPAID"
        )
        finance.reconcile_receipt(
            organization=self.org,
            actor=self.owner,
            invoice=invoice,
            transaction=bank,
            amount="500000",
        )
        january = prepare_monthly_package(
            organization=self.org, actor=self.finance, period="2026-01"
        )
        self.assertEqual(january.snapshot["book_revenue"], "0.00")
        self.approve(
            january
        )  # Explicitly reviewed issue exclusion and nil, no implied cash taxation.
        february = prepare_monthly_package(
            organization=self.org, actor=self.finance, period="2026-02"
        )
        rows = self.decision(february)["rows"]
        receipt = next(
            row for row in february.snapshot["rows"] if row["source_kind"] == "receipt"
        )
        rows = [
            {**row, "decision": "adjustment"}
            if row["row_id"] == receipt["row_id"]
            else row
            for row in rows
        ]
        self.approve(
            february,
            self.decision(
                february,
                rows=rows,
                adjustments=[
                    {
                        "kind": "timing",
                        "amount": "500000",
                        "reason": "Synthetic reviewed receipt-month timing",
                        "evidence_id": self.evidence().pk,
                        "source_row_ids": [receipt["row_id"]],
                    }
                ],
            ),
        )
        march = prepare_monthly_package(
            organization=self.org, actor=self.finance, period="2026-03"
        )
        self.assertTrue(march.fiscal_history)
        with self.assertRaises(ValidationError):
            self.decision(march, prior_history_confirmed=True)
        excluded = [
            {
                "row_id": row["row_id"],
                "decision": "exclude",
                "reason": "Synthetic already assessed in receipt month",
            }
            for row in march.snapshot["rows"]
        ]
        candidate = next(
            row for row in march.snapshot["rows"] if row["role"] == "candidate"
        )
        with self.assertRaises(ValidationError):
            self.decision(
                march,
                rows=excluded,
                prior_history_confirmed=True,
                adjustments=[
                    {
                        "kind": "timing",
                        "amount": "500000",
                        "reason": "Synthetic duplicated full replacement",
                        "evidence_id": self.evidence().pk,
                        "source_row_ids": [candidate["row_id"]],
                    }
                ],
            )
        approval = self.approve(
            march, self.decision(march, rows=excluded, prior_history_confirmed=True)
        )
        self.assertEqual(approval.result["taxable_turnover"], "0")

    def test_completed_source_month_with_large_review_still_preserves_exact_payload(
        self,
    ):
        self.profile()
        for index in range(15):
            self.sale(suffix=f"LARGE-{index}")
        package = prepare_monthly_package(
            organization=self.org, actor=self.finance, period="2026-09"
        )
        decision = self.decision(package)
        for row in decision["rows"]:
            row["reason"] = (
                "Synthetic documented classification with supporting report. " * 10
            )
        decision = self.decision(package, rows=decision["rows"])
        import json

        self.assertGreater(len(json.dumps(decision).encode("utf-8")), 20000)
        approval = self.approve(package, decision)
        self.assertEqual(approval.external_review.decision, decision)
        with self.assertRaises(ValidationError):
            self.external(package, {"padding": "x" * (1024 * 1024)})
        legacy = TaxProfile.objects.get(organization=self.org)
        with self.assertRaises(ValidationError):
            self.external(legacy, {"padding": "x" * 21000})

    def test_pending_review_revision_preserves_prior_allocation_and_blocks_dependent_month(
        self,
    ):
        from taxes.monthly_source_workflow import revise_monthly_review

        self.profile("final")
        invoice = self.sale(
            suffix="PENDING-FEB", issued=date(2026, 2, 5), delivered=date(2026, 2, 10)
        )
        february = prepare_monthly_package(
            organization=self.org, actor=self.finance, period="2026-02"
        )
        prior = self.approve(february)
        self.sale(
            suffix="PENDING-MAR", issued=date(2026, 3, 5), delivered=date(2026, 3, 10)
        )
        march = prepare_monthly_package(
            organization=self.org, actor=self.finance, period="2026-03"
        )
        revised = revise_monthly_review(
            organization=self.org,
            actor=self.owner,
            package=february,
            reason="Synthetic professional review reopened before dependent month approval",
            expected_fingerprint=february.package_hash,
            expected_version=february.revision,
        )
        self.assertTrue(
            package_status(organization=self.org, package=february)["superseded"]
        )
        self.assertTrue(package_status(organization=self.org, package=march)["stale"])
        pending = prepare_monthly_package(
            organization=self.org, actor=self.finance, period="2026-03"
        )
        history = next(
            item for item in pending.fiscal_history if item["approval_id"] == prior.pk
        )
        self.assertTrue(history["pending_review"])
        self.assertEqual(history["pending_package_id"], revised.pk)
        self.assertEqual(
            sum(
                Decimal(row["amount"])
                for row in history["allocations"]
                if row["invoice_id"] == invoice.pk
            ),
            Decimal(500000),
        )
        with self.assertRaises(ValidationError):
            self.approve(pending, self.decision(pending, prior_history_confirmed=True))
        new_approval = self.approve(revised)
        fresh = prepare_monthly_package(
            organization=self.org, actor=self.finance, period="2026-03"
        )
        self.assertNotEqual(fresh.pk, pending.pk)
        self.assertEqual(fresh.fiscal_history[0]["approval_id"], new_approval.pk)
        self.assertFalse(fresh.fiscal_history[0].get("pending_review", False))
        self.approve(fresh, self.decision(fresh, prior_history_confirmed=True))


class MonthlyLegalPeriodTests(TestCase):
    def publish(self, *, start=None, until=None, summary="Synthetic original"):
        publish_catalog(
            [
                {
                    "slug": "synthetic-period-rule",
                    "title": "Synthetic dated rule",
                    "issuer": "Synthetic",
                    "article": "Synthetic article",
                    "url": "https://example.invalid/synthetic",
                    "summary": summary,
                    "topics": ["final"],
                    "effective_from": start,
                    "effective_until": until,
                }
            ],
            date(2026, 10, 9),
            reviewed_by="Synthetic operator",
            review_reference="Synthetic publication",
        )

    def test_tampered_unpublished_head_cannot_fall_back_to_old_approved_revision(self):
        self.publish(start=date(2025, 1, 1))
        source = TaxSource.objects.get(slug="synthetic-period-rule")
        source.summary = "Unpublished content change"
        source.save()
        with self.assertRaises(ValidationError):
            reviewed_sources_for_period(date(2026, 9, 1), slugs=[source.slug])

    def test_period_helper_rejects_datetime_with_a_validation_message(self):
        from datetime import datetime

        self.publish(start=date(2025, 1, 1))
        with self.assertRaises(ValidationError):
            reviewed_sources_for_period(
                datetime(2026, 9, 1), slugs=["synthetic-period-rule"]
            )

    def test_valid_future_head_preserves_published_historical_rule(self):
        self.publish(start=date(2025, 1, 1), until=date(2026, 9, 30))
        self.publish(start=date(2026, 10, 1), summary="Synthetic future rule")
        rows = reviewed_sources_for_period(
            date(2026, 9, 1), slugs=["synthetic-period-rule"]
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["effective_until"], "2026-09-30")

    def test_midmonth_revisions_retain_both_application_intervals(self):
        self.publish(start=date(2025, 1, 1), until=date(2026, 9, 14))
        self.publish(start=date(2026, 9, 15), summary="Synthetic midmonth replacement")
        rows = reviewed_sources_for_period(
            date(2026, 9, 1), slugs=["synthetic-period-rule"]
        )
        self.assertEqual(
            [(row["application_from"], row["application_until"]) for row in rows],
            [("2026-09-01", "2026-09-14"), ("2026-09-15", "2026-09-30")],
        )
