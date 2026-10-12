"""Independent synthetic probes at the monthly package public service boundary.

The explicit SQL probes simulate storage corruption only; normal public APIs
preserve immutable approvals/documents. No provider or company data is used.
"""

from copy import deepcopy
from datetime import date
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase
from django.urls import reverse

from finance import services as finance
from finance.fulfillment import deliver_units
from finance.models import (
    BankAccount,
    BankTransaction,
    Invoice,
    Journal,
    JournalLine,
    Party,
    Product,
)

from . import monthly_source_workflow as monthly
from . import tests_monthly_packages as fixtures
from . import workflow
from .models import (
    MonthlyFiscalApproval,
    MonthlyTaxSourcePackage,
    TaxObligation,
    TaxSource,
)


class MonthlyAdversarialTests(TestCase):
    setUp = fixtures.MonthlySourcePackageTests.setUp
    evidence = fixtures.MonthlySourcePackageTests.evidence
    external = fixtures.MonthlySourcePackageTests.external
    profile = fixtures.MonthlySourcePackageTests.profile
    decision = fixtures.MonthlySourcePackageTests.decision
    approve = fixtures.MonthlySourcePackageTests.approve
    sale = fixtures.MonthlySourcePackageTests.sale

    def package(self, period):
        return monthly.prepare_monthly_package(
            organization=self.org, actor=self.finance, period=period
        )

    def generate(self, package):
        return monthly.generate_monthly_obligation(
            organization=self.org,
            actor=self.finance,
            package=package,
            expected_fingerprint=package.package_hash,
            expected_version=package.revision,
        )

    def revise(self, package, **changes):
        values = {
            "organization": self.org,
            "actor": self.owner,
            "package": package,
            "reason": "Synthetic professional correction of the original classification",
            "expected_fingerprint": package.package_hash,
            "expected_version": package.revision,
        }
        values.update(changes)
        return monthly.revise_monthly_review(**values)

    def bank(self, amount, day, reference):
        account, _ = BankAccount.objects.get_or_create(
            organization=self.org,
            account_number="SYNTHETIC-MONTHLY",
            defaults={"name": "Synthetic source bank"},
        )
        finance.import_bank_csv(
            organization=self.org,
            actor=self.owner,
            account=account,
            content=f"date,reference,description,amount\n{day},{reference},Synthetic only,{amount}\n",
        )
        return BankTransaction.objects.get(
            organization=self.org, account=account, reference=reference
        )

    def corrupt_json(self, obj, field, value):
        prepared = obj._meta.get_field(field).get_db_prep_value(value, connection)
        table, column = (
            connection.ops.quote_name(obj._meta.db_table),
            connection.ops.quote_name(field),
        )
        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {table} SET {column} = %s WHERE id = %s", [prepared, obj.pk]
            )

    def test_prepaid_unallocated_advance_taxed_before_invoice_cannot_be_taxed_again_on_delivery(
        self,
    ):
        self.profile("final")
        buyer = Party.objects.create(
            organization=self.org, name="Synthetic advance buyer", kind="customer"
        )
        product = Product.objects.create(
            organization=self.org,
            code="ADV-LINK",
            name="Synthetic prepaid test",
            kind="itp",
            default_price="500000",
        )
        advance = finance.record_customer_advance(
            organization=self.org,
            actor=self.owner,
            party=buyer,
            transaction=self.bank("500000", date(2026, 1, 10), "JAN-ADV"),
            amount="500000",
            reference="JAN-ADV",
            evidence="Synthetic original customer advance",
        )
        january = self.package("2026-01")
        source_id = f"customer-advance:{advance.pk}"
        decision = self.decision(
            january,
            rows=[
                {
                    "row_id": row["row_id"],
                    "decision": "adjustment"
                    if row["row_id"] == source_id
                    else "exclude",
                    "reason": "Synthetic professional reviewed January income timing",
                }
                for row in january.snapshot["rows"]
            ],
            adjustments=[
                {
                    "kind": "timing",
                    "amount": "500000",
                    "reason": "Synthetic supported January timing review",
                    "evidence_id": self.evidence().pk,
                    "source_row_ids": [source_id],
                }
            ],
        )
        prior = self.approve(january, decision=decision)
        self.assertEqual(Decimal(prior.result["taxable_turnover"]), Decimal(500000))
        invoice = finance.create_invoice(
            organization=self.org,
            actor=self.owner,
            party=buyer,
            product=product,
            quantity=1,
            date=date(2026, 2, 10),
            service_date=date(2026, 3, 10),
        )
        invoice = finance.issue_invoice(
            organization=self.org, actor=self.owner, invoice=invoice
        )
        finance.apply_customer_advance(
            organization=self.org,
            actor=self.owner,
            advance=advance,
            invoice=invoice,
            date=date(2026, 2, 10),
            amount="500000",
            reference="FEB-APPLY",
            evidence="Synthetic exact January advance applied",
        )
        deliver_units(
            organization=self.org,
            actor=self.owner,
            invoice=invoice,
            quantity=1,
            date=date(2026, 3, 10),
            reference="MARCH-TEST",
            evidence="Synthetic completed attendance",
        )
        march = self.package("2026-03")
        self.assertTrue(march.fiscal_history)
        self.assertEqual(
            march.fiscal_history[0]["allocations"][0]["source_row_ids"], [source_id]
        )
        with self.assertRaises(ValidationError):
            self.decision(march, prior_history_confirmed=True)

    def test_new_month_approval_rejects_corrupted_prior_allocation_even_with_intact_report(
        self,
    ):
        self.profile("final")
        self.sale(
            "500000",
            suffix="PRIOR",
            issued=date(2026, 2, 5),
            delivered=date(2026, 2, 10),
        )
        february = self.package("2026-02")
        prior = self.approve(february)
        changed = deepcopy(prior.result)
        changed["allocations"][0]["amount"] = "0.00"
        self.corrupt_json(prior, "result", changed)
        self.sale(
            "200000",
            suffix="CURRENT",
            issued=date(2026, 3, 5),
            delivered=date(2026, 3, 10),
        )
        with self.assertRaises(ValidationError):
            march = self.package("2026-03")
            self.approve(
                march, decision=self.decision(march, prior_history_confirmed=True)
            )
        self.assertFalse(
            MonthlyFiscalApproval.objects.filter(
                package__period=date(2026, 3, 1)
            ).exists()
        )

    def test_history_keeps_latest_reviewed_revision_once_after_original_draft_withdrawal(
        self,
    ):
        self.profile("final")
        prior_invoice = self.sale(
            "500000",
            suffix="REVISION",
            issued=date(2026, 2, 5),
            delivered=date(2026, 2, 10),
        )
        original = self.package("2026-02")
        first_approval = self.approve(original)
        first_draft = self.generate(original)
        workflow.withdraw_obligation(
            organization=self.org,
            actor=self.finance,
            obligation=first_draft,
            reason="Synthetic draft replaced after source inventory review",
            expected_version=first_draft.updated_at.isoformat(),
        )
        finance.post_journal(
            organization=self.org,
            actor=self.owner,
            date=date(2026, 2, 20),
            source_key="SYNTHETIC-INVENTORY-RECHECK",
            description="Synthetic source requiring explicit exclusion",
            entries=[("BANK", "100", "0"), ("REVENUE", "0", "100")],
        )
        replacement = self.package("2026-02")
        self.assertEqual(
            (replacement.revision, replacement.previous_id), (2, original.pk)
        )
        second_approval = self.approve(
            replacement
        )  # New manual source is explicitly excluded by the report.
        second_draft = self.generate(replacement)
        self.assertNotEqual(first_draft.pk, second_draft.pk)
        self.sale(
            "200000",
            suffix="REVISION-MARCH",
            issued=date(2026, 3, 5),
            delivered=date(2026, 3, 10),
        )
        march = self.package("2026-03")
        self.assertEqual(len(march.fiscal_history), 1)
        self.assertEqual(march.fiscal_history[0]["approval_id"], second_approval.pk)
        allocations = [
            row
            for row in march.fiscal_history[0]["allocations"]
            if row["invoice_id"] == prior_invoice.pk
        ]
        self.assertEqual(
            sum((Decimal(row["amount"]) for row in allocations), Decimal(0)),
            Decimal(500000),
        )
        child = self.approve(
            march, decision=self.decision(march, prior_history_confirmed=True)
        )
        self.assertEqual(Decimal(child.result["taxable_turnover"]), Decimal(200000))
        first_draft.refresh_from_db()
        self.assertEqual(first_draft.status, "withdrawn")
        self.assertTrue(
            MonthlyFiscalApproval.objects.filter(pk=first_approval.pk).exists()
        )

    def test_corrupted_original_report_prevents_new_month_approval_without_erasing_history(
        self,
    ):
        self.profile("final")
        self.sale(
            "500000",
            suffix="REPORT",
            issued=date(2026, 2, 5),
            delivered=date(2026, 2, 10),
        )
        original = self.package("2026-02")
        prior = self.approve(original)
        evidence = prior.external_review.evidence
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE taxes_taxevidence SET content = %s WHERE id = %s",
                [b"changed synthetic professional PDF", evidence.pk],
            )
        self.sale(
            "200000",
            suffix="REPORT-MARCH",
            issued=date(2026, 3, 5),
            delivered=date(2026, 3, 10),
        )
        with self.assertRaises(ValidationError):
            march = self.package("2026-03")
            self.approve(
                march, decision=self.decision(march, prior_history_confirmed=True)
            )
        self.assertTrue(MonthlyFiscalApproval.objects.filter(pk=prior.pk).exists())

    def test_new_unreviewed_period_upload_stales_approved_nil_package_without_changing_snapshot(
        self,
    ):
        self.profile("final")
        original = self.package("2026-03")
        self.approve(original)
        snapshot = deepcopy(original.snapshot)
        self.assertFalse(
            monthly.package_status(organization=self.org, package=original)["stale"]
        )
        account = BankAccount.objects.create(
            organization=self.org,
            name="Synthetic unreviewed source bank",
            account_number="RAW-SOURCE",
        )
        self.client.force_login(self.finance)
        response = self.client.post(
            reverse("bank_import"),
            {
                "account": account.pk,
                "file": SimpleUploadedFile(
                    "synthetic-march.csv",
                    b"date,reference,description,amount\n2026-03-10,SOURCE,Synthetic unclassified,100\n",
                ),
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            monthly.package_status(organization=self.org, package=original)["stale"]
        )
        with self.assertRaises(ValidationError):
            self.generate(original)
        revised = self.package("2026-03")
        self.assertEqual(revised.previous_id, original.pk)
        self.assertNotEqual(revised.source_hash, original.source_hash)
        self.assertEqual(revised.snapshot["book_revenue"], "0.00")
        original.refresh_from_db()
        self.assertEqual(snapshot, original.snapshot)
        self.assertFalse(TaxObligation.objects.filter(period=original.period).exists())

    def test_normal_actual_income_package_never_generates_monthly_final_or_corporate_turnover_tax(
        self,
    ):
        self.profile("normal")
        self.sale("500000", suffix="NORMAL")
        package = self.package("2026-09")
        approval = self.approve(package)
        self.assertEqual(
            Decimal(approval.result["reviewed_bridge_amount"]), Decimal(500000)
        )
        self.assertIsNone(approval.result["taxable_turnover"])
        self.assertIsNone(approval.result["final_tax"])
        with self.assertRaises(ValidationError):
            self.generate(package)
        self.assertFalse(TaxObligation.objects.exists())

    def test_corrupted_published_legal_head_cannot_silently_fall_back_to_older_rule(
        self,
    ):
        from .knowledge import publish_catalog, reviewed_sources_for_period

        slug = "synthetic-corrupted-published-head"
        base = {
            "slug": slug,
            "title": "Synthetic reviewed period rule",
            "url": "https://example.invalid/synthetic-period-rule",
            "issuer": "Synthetic source",
            "article": "Synthetic provisions",
            "topics": ["final"],
            "effective_until": None,
        }
        publish_catalog(
            [
                {
                    **base,
                    "summary": "Synthetic original rule",
                    "effective_from": date(2025, 1, 1),
                }
            ],
            date(2026, 10, 9),
            reviewed_by="Synthetic operator",
            review_reference="Synthetic original publication",
        )
        publish_catalog(
            [
                {
                    **base,
                    "summary": "Synthetic replacement rule",
                    "effective_from": date(2026, 1, 1),
                }
            ],
            date(2026, 10, 9),
            reviewed_by="Synthetic operator",
            review_reference="Synthetic replacement publication",
        )
        source = TaxSource.objects.get(slug=slug)
        published = source.revisions.order_by("-pk").first()
        changed = deepcopy(published.snapshot)
        changed["summary"] = "Synthetic corrupt replacement content"
        self.corrupt_json(published, "snapshot", changed)
        with self.assertRaises(ValidationError):
            reviewed_sources_for_period(date(2026, 9, 1), slugs=[slug])

    def test_owner_corrects_same_fact_review_with_new_version_without_mutating_financial_sources(
        self,
    ):
        self.profile("final")
        self.sale("500000", suffix="SAME-FACTS")
        original = self.package("2026-09")
        wrong = self.decision(
            original,
            rows=[
                {
                    "row_id": row["row_id"],
                    "decision": "exclude",
                    "reason": "Synthetic initial classification later corrected by professional",
                }
                for row in original.snapshot["rows"]
            ],
        )
        original_approval = self.approve(original, decision=wrong)
        self.assertEqual(
            Decimal(original_approval.result["taxable_turnover"]), Decimal(0)
        )
        source_snapshot = deepcopy(original.snapshot)
        journals = list(
            Journal.objects.filter(organization=self.org).order_by("pk").values()
        )
        lines = list(
            JournalLine.objects.filter(organization=self.org).order_by("pk").values()
        )
        invoices = list(
            Invoice.objects.filter(organization=self.org).order_by("pk").values()
        )
        revised = self.revise(original)
        self.assertEqual((revised.revision, revised.previous_id), (2, original.pk))
        self.assertEqual(revised.source_hash, original.source_hash)
        self.assertEqual(revised.snapshot, source_snapshot)
        self.assertFalse(
            monthly.package_status(organization=self.org, package=original)[
                "can_generate"
            ]
        )
        with self.assertRaises(ValidationError):
            self.generate(original)
        self.assertEqual(self.package("2026-09").pk, revised.pk)
        corrected_approval = self.approve(revised)
        corrected_draft = self.generate(revised)
        self.assertEqual(
            Decimal(corrected_approval.result["taxable_turnover"]), Decimal(500000)
        )
        self.assertEqual(corrected_draft.base, Decimal(500000))
        self.assertEqual(corrected_draft.amount, Decimal(2500))
        original_approval.refresh_from_db()
        original.refresh_from_db()
        self.assertEqual(
            Decimal(original_approval.result["taxable_turnover"]), Decimal(0)
        )
        self.assertEqual(original.snapshot, source_snapshot)
        self.assertEqual(
            journals,
            list(Journal.objects.filter(organization=self.org).order_by("pk").values()),
        )
        self.assertEqual(
            lines,
            list(
                JournalLine.objects.filter(organization=self.org)
                .order_by("pk")
                .values()
            ),
        )
        self.assertEqual(
            invoices,
            list(Invoice.objects.filter(organization=self.org).order_by("pk").values()),
        )

    def test_same_fact_revision_request_is_owner_scoped_current_and_idempotent(self):
        from core.models import Membership, Organization

        self.profile("final")
        self.sale("500000", suffix="REVISION-GUARDS")
        original = self.package("2026-09")
        self.approve(original)
        before = MonthlyTaxSourcePackage.objects.count()
        with self.assertRaises(PermissionDenied):
            self.revise(original, actor=self.finance)
        with self.assertRaises(ValidationError):
            self.revise(original, expected_fingerprint="0" * 64)
        with self.assertRaises(ValidationError):
            self.revise(original, expected_version=original.revision + 1)
        with self.assertRaises(ValidationError):
            self.revise(original, reason="short")
        other = Organization.objects.create(name="Synthetic foreign monthly revision")
        Membership.objects.create(organization=other, user=self.owner, role="owner")
        foreign = monthly.prepare_monthly_package(
            organization=other, actor=self.owner, period="2026-09"
        )
        with self.assertRaises(ValidationError):
            self.revise(foreign)
        self.assertEqual(
            MonthlyTaxSourcePackage.objects.filter(organization=self.org).count(),
            before,
        )
        revised = self.revise(original)
        self.assertEqual(self.revise(original).pk, revised.pk)
        with self.assertRaises(ValidationError):
            self.revise(
                original,
                reason="Synthetic changed request must not silently create another correction",
            )
        with self.assertRaises(ValidationError):
            self.revise(revised)  # Pending review is not another approved decision.
        self.assertEqual(
            MonthlyTaxSourcePackage.objects.filter(organization=self.org).count(),
            before + 1,
        )
        with self.assertRaises(ValidationError):
            self.approve(revised, external_review=original.approval.external_review)
        self.assertFalse(MonthlyFiscalApproval.objects.filter(package=revised).exists())

    def test_stale_financial_source_requires_normal_refresh_instead_of_same_fact_review_revision(
        self,
    ):
        self.profile("final")
        self.sale("500000", suffix="STALE-REVISION")
        original = self.package("2026-09")
        self.approve(original)
        finance.post_journal(
            organization=self.org,
            actor=self.owner,
            date=date(2026, 9, 20),
            source_key="NEW-SOURCE-AFTER-REVIEW",
            description="Synthetic new backdated source",
            entries=[("BANK", "100", "0"), ("REVENUE", "0", "100")],
        )
        with self.assertRaises(ValidationError):
            self.revise(original)
        self.assertEqual(
            MonthlyTaxSourcePackage.objects.filter(organization=self.org).count(), 1
        )
        refreshed = self.package("2026-09")
        self.assertEqual(refreshed.previous_id, original.pk)
        self.assertNotEqual(refreshed.source_hash, original.source_hash)

    def test_active_draft_requires_explicit_withdrawal_before_same_fact_review_revision(
        self,
    ):
        self.profile("final")
        self.sale("500000", suffix="WITHDRAW-REVISION")
        original = self.package("2026-09")
        self.approve(original)
        draft = self.generate(original)
        with self.assertRaises(ValidationError):
            self.revise(original)
        draft.refresh_from_db()
        self.assertEqual(draft.status, "review")
        self.assertEqual(
            MonthlyTaxSourcePackage.objects.filter(organization=self.org).count(), 1
        )
        workflow.withdraw_obligation(
            organization=self.org,
            actor=self.finance,
            obligation=draft,
            reason="Synthetic explicit withdrawal before professional classification correction",
            expected_version=draft.updated_at.isoformat(),
        )
        revised = self.revise(original)
        self.assertEqual(revised.previous_id, original.pk)
        draft.refresh_from_db()
        self.assertEqual(draft.status, "withdrawn")
        self.assertFalse(
            monthly.package_status(organization=self.org, package=original)[
                "can_generate"
            ]
        )

    def test_approved_and_officially_paid_final_liability_cannot_be_silently_revised(
        self,
    ):
        self.profile("final")
        self.sale("500000", suffix="PAID-REVISION")
        original = self.package("2026-09")
        self.approve(original)
        obligation = self.generate(original)
        obligation = workflow.approve_tax_obligation(
            obligation,
            self.owner,
            external_review=self.external(obligation, {"action": "approve_obligation"}),
        )
        with self.assertRaises(ValidationError):
            self.revise(original)
        recognition = finance.recognize_own_tax(
            organization=self.org,
            actor=self.owner,
            obligation=obligation,
            date=date(2026, 9, 15),
            reference="SYN-TAX-RECOGNITION",
            evidence="Synthetic reviewed final tax liability",
        )
        finance.reconcile_own_tax_payment(
            organization=self.org,
            actor=self.owner,
            recognition=recognition,
            transaction=self.bank("-2500", date(2026, 9, 20), "SYN-TAX-PAYMENT"),
            amount="2500",
            reference="SYN-TAX-PAYMENT",
            evidence="Synthetic complete bank payment",
        )
        verification = workflow.verify_obligation_evidence(
            organization=self.org,
            actor=self.owner,
            obligation=obligation,
            kind="payment",
            evidence=self.evidence(),
            reference="SYN-VERIFIED-FINAL-SEP",
            taxpayer_reference="1234567890123456",
            period=obligation.period,
            amount="2500",
            note="Synthetic original official self-payment receipt matched",
            matches_confirmed=True,
        )
        with self.assertRaises(ValidationError):
            self.revise(original)
        obligation.refresh_from_db()
        self.assertEqual(obligation.status, "approved")
        self.assertTrue(obligation.verifications.filter(pk=verification.pk).exists())
        self.assertEqual(
            MonthlyTaxSourcePackage.objects.filter(organization=self.org).count(), 1
        )

    def test_prior_review_cannot_stale_later_officially_paid_liability_that_depends_on_its_history(
        self,
    ):
        self.profile("final")
        self.sale(
            "500000",
            suffix="DEPENDENT-FEB",
            issued=date(2026, 2, 5),
            delivered=date(2026, 2, 10),
        )
        prior = self.package("2026-02")
        self.approve(
            prior
        )  # Deliberately no February obligation; same-period-only checks are insufficient.
        self.sale(
            "200000",
            suffix="DEPENDENT-MARCH",
            issued=date(2026, 3, 5),
            delivered=date(2026, 3, 10),
        )
        later = self.package("2026-03")
        self.assertEqual(later.fiscal_history[0]["package_id"], prior.pk)
        self.approve(later, decision=self.decision(later, prior_history_confirmed=True))
        obligation = self.generate(later)
        obligation = workflow.approve_tax_obligation(
            obligation,
            self.owner,
            external_review=self.external(obligation, {"action": "approve_obligation"}),
        )
        recognition = finance.recognize_own_tax(
            organization=self.org,
            actor=self.owner,
            obligation=obligation,
            date=date(2026, 3, 15),
            reference="DEPENDENT-MARCH-TAX",
            evidence="Synthetic reviewed dependent tax liability",
        )
        finance.reconcile_own_tax_payment(
            organization=self.org,
            actor=self.owner,
            recognition=recognition,
            transaction=self.bank(
                "-1000", date(2026, 3, 20), "DEPENDENT-MARCH-PAYMENT"
            ),
            amount="1000",
            reference="DEPENDENT-MARCH-PAYMENT",
            evidence="Synthetic full dependent bank tax payment",
        )
        verification = workflow.verify_obligation_evidence(
            organization=self.org,
            actor=self.owner,
            obligation=obligation,
            kind="payment",
            evidence=self.evidence(),
            reference="DEPENDENT-MARCH-OFFICIAL",
            taxpayer_reference="1234567890123456",
            period=obligation.period,
            amount="1000",
            note="Synthetic original official dependent-month self-payment receipt matched",
            matches_confirmed=True,
        )
        with self.assertRaises(ValidationError):
            self.revise(prior)
        self.assertEqual(
            MonthlyTaxSourcePackage.objects.filter(organization=self.org).count(), 2
        )
        self.assertFalse(
            monthly.package_status(organization=self.org, package=prior)["stale"]
        )
        self.assertFalse(
            monthly.package_status(organization=self.org, package=later)["stale"]
        )
        self.assertTrue(obligation.verifications.filter(pk=verification.pk).exists())

    def test_prior_review_requires_explicit_withdrawal_of_active_dependent_month_draft(
        self,
    ):
        self.profile("final")
        self.sale(
            "500000",
            suffix="DEPENDENT-DRAFT-FEB",
            issued=date(2026, 2, 5),
            delivered=date(2026, 2, 10),
        )
        prior = self.package("2026-02")
        self.approve(prior)
        self.sale(
            "200000",
            suffix="DEPENDENT-DRAFT-MARCH",
            issued=date(2026, 3, 5),
            delivered=date(2026, 3, 10),
        )
        later = self.package("2026-03")
        self.approve(later, decision=self.decision(later, prior_history_confirmed=True))
        draft = self.generate(later)
        with self.assertRaises(ValidationError):
            self.revise(prior)
        workflow.withdraw_obligation(
            organization=self.org,
            actor=self.finance,
            obligation=draft,
            reason="Synthetic explicit withdrawal of dependent draft before earlier professional review correction",
            expected_version=draft.updated_at.isoformat(),
        )
        revised = self.revise(prior)
        self.assertEqual(revised.previous_id, prior.pk)
        with self.assertRaises(ValidationError):
            self.generate(later)
        draft.refresh_from_db()
        self.assertEqual(draft.status, "withdrawn")
        later.refresh_from_db()
        self.assertEqual(
            later.fiscal_history[0]["package_id"], prior.pk
        )  # Historical package is not rewritten.
