"""Independent FIN-05 public journeys; every source is synthetic test data.

Direct SQL is used only to simulate damaged persisted evidence. Original
identities, reports, certificates and money movements use public services.
"""

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.test import TestCase
from django.utils import timezone

from core.models import Membership, Organization
from finance import services as finance
from finance.models import (
    AccountingPeriod,
    AdvanceApplication,
    Allocation,
    BankAccount,
    BankPosting,
    BankTransaction,
    CustomerTaxSettlement,
    Journal,
    JournalLine,
    Party,
)
from finance.settlement import applied_payment_amounts, collectible_amounts
from marketing import kpi
from marketing import services as marketing
from marketing.analytics import marketing_snapshot
from operations import services as operations
from operations.analytics import commercial_snapshot
from taxes import tests_recipient_identity_independent as identity_fixtures
from taxes.monthly_sources import build_monthly_sources


class LateAdvanceCreditAdversarialTests(TestCase):
    setUp = identity_fixtures.RecipientIdentityIndependentTests.setUp
    evidence = identity_fixtures.RecipientIdentityIndependentTests.evidence
    external = identity_fixtures.RecipientIdentityIndependentTests.external
    prepare_identity = (
        identity_fixtures.RecipientIdentityIndependentTests.prepare_identity
    )
    approve_identity = (
        identity_fixtures.RecipientIdentityIndependentTests.approve_identity
    )
    own_identity = identity_fixtures.RecipientIdentityIndependentTests.own_identity
    draft = identity_fixtures.RecipientIdentityIndependentTests.draft
    approve = identity_fixtures.RecipientIdentityIndependentTests.approve
    credit = identity_fixtures.RecipientIdentityIndependentTests.credit

    def damage(self, row, field, value):
        self.assertTrue(connection.in_atomic_block)
        self.assertTrue(self.org.is_demo)
        quoted = connection.ops.quote_name
        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {quoted(row._meta.db_table)} SET {quoted(field)} = %s WHERE {quoted('id')} = %s",
                [value, row.pk],
            )
        row.refresh_from_db()

    def movement(self, amount, *, reference, day=date(2026, 9, 1)):
        finance.import_bank_csv(
            organization=self.org,
            actor=self.owner,
            account=self.account,
            content=f"date,reference,description,amount\n{day},{reference},Synthetic FIN05,{amount}\n",
        )
        return BankTransaction.objects.get(
            organization=self.org, account=self.account, reference=reference
        )

    def journey(
        self,
        *,
        advance_amount="490000",
        applied_amount="490000",
        point=None,
        basis="paid",
        bank_day=date(2026, 9, 1),
    ):
        suffix = str(AdvanceApplication.objects.count())
        bank = self.movement(
            advance_amount, reference=f"SYN-FIN05-BANK-{suffix}", day=bank_day
        )
        advance = finance.record_customer_advance(
            organization=self.org,
            actor=self.owner,
            party=self.customer,
            transaction=bank,
            amount=advance_amount,
            reference=f"SYN-FIN05-ADVANCE-{suffix}",
            evidence="Synthetic documented advance before invoice",
        )
        invoice = finance.create_invoice(
            organization=self.org,
            actor=self.owner,
            party=self.customer,
            product=self.product,
            quantity=1,
            date=date(2026, 9, 9),
            service_date=date(2026, 9, 10),
        )
        finance.issue_invoice(organization=self.org, actor=self.owner, invoice=invoice)
        application = finance.apply_customer_advance(
            organization=self.org,
            actor=self.owner,
            advance=advance,
            invoice=invoice,
            date=date(2026, 9, 10),
            amount=applied_amount,
            reference=f"SYN-FIN05-APPLICATION-{suffix}",
            evidence="Synthetic invoice-specific application",
        )
        obligation = self.approve(
            self.draft(invoice=invoice, point=point or bank.date, basis=basis)
        )
        self.credit(obligation)
        return invoice, bank, advance, application, obligation

    def recognize(self, obligation, application, **changes):
        data = {
            "organization": self.org,
            "actor": self.owner,
            "obligation": obligation,
            "source_advance_application": application,
            "date": date(2026, 10, 8),
            "reference": f"SYN-FIN05-CREDIT-{obligation.pk}",
            "evidence": "Synthetic late official certificate",
        }
        return finance.recognize_customer_tax_credit(**(data | changes))

    def history(self, bank, advance, application):
        journals = [advance.journal_id, application.journal_id]
        return {
            "journals": list(
                Journal.objects.filter(pk__in=journals)
                .order_by("pk")
                .values_list("pk", "organization_id", "date", "status", "source_key")
            ),
            "lines": list(
                JournalLine.objects.filter(journal_id__in=journals)
                .order_by("pk")
                .values_list(
                    "pk", "journal_id", "organization_id", "account", "debit", "credit"
                )
            ),
            "bank": (bank.pk, bank.date, bank.amount, bank.unallocated_amount),
            "advance": (
                advance.pk,
                advance.amount,
                advance.remaining_amount,
                advance.bank_posting_id,
            ),
            "application": (
                application.pk,
                application.date,
                application.amount,
                application.advance_id,
                application.invoice_id,
                application.journal_id,
            ),
            "source_counts": (
                BankPosting.objects.count(),
                Allocation.objects.count(),
                AdvanceApplication.objects.count(),
            ),
        }

    def assert_rejected_without_credit(self, obligation, application, **changes):
        before = Journal.objects.count()
        with self.assertRaises(ValidationError):
            self.recognize(obligation, application, **changes)
        self.assertFalse(
            CustomerTaxSettlement.objects.filter(obligation=obligation).exists()
        )
        self.assertEqual(Journal.objects.count(), before)

    def test_closed_source_period_and_preinvoice_bank_remain_intact_at_open_credit_cutoff(
        self,
    ):
        invoice, bank, advance, application, obligation = self.journey(
            bank_day=date(2026, 8, 31)
        )
        for month in (8, 9):
            period = AccountingPeriod.objects.get(
                organization=self.org, year=2026, month=month
            )
            period.closed, period.closed_at = True, timezone.now()
            period.save()
        original = self.history(bank, advance, application)
        before_journals = Journal.objects.count()
        row = self.recognize(obligation, application)
        self.assertEqual(self.history(bank, advance, application), original)
        self.assertEqual(Journal.objects.count(), before_journals + 1)
        self.assertEqual(
            (row.cash_amount, row.tax_amount, row.gross_amount),
            (Decimal(0), Decimal(10000), Decimal(10000)),
        )
        self.assertEqual(row.source_advance_application_id, application.pk)
        self.assertIsNone(row.source_allocation_id)
        self.assertIsNone(row.bank_posting_id)
        self.assertEqual(
            list(
                row.journal.lines.order_by("pk").values_list(
                    "account", "debit", "credit"
                )
            ),
            [
                ("TAX_RECEIVABLE", Decimal(10000), Decimal(0)),
                ("AR", Decimal(0), Decimal(10000)),
            ],
        )
        old = finance.report_balances(organization=self.org, as_of=date(2026, 9, 30))
        new = finance.report_balances(organization=self.org, as_of=date(2026, 10, 8))
        self.assertEqual(
            (old["BANK"], old["AR"], old["CUSTOMER_ADVANCE"], old["TAX_RECEIVABLE"]),
            (Decimal(490000), Decimal(10000), Decimal(0), Decimal(0)),
        )
        self.assertEqual(
            (new["BANK"], new["AR"], new["CUSTOMER_ADVANCE"], new["TAX_RECEIVABLE"]),
            (Decimal(490000), Decimal(0), Decimal(0), Decimal(10000)),
        )
        self.assertEqual(
            collectible_amounts(
                organization=self.org, invoice_ids=[invoice.pk], as_of=date(2026, 9, 30)
            ),
            {invoice.pk: Decimal(10000)},
        )
        self.assertEqual(
            applied_payment_amounts(
                organization=self.org, invoice_ids=[invoice.pk], as_of=date(2026, 10, 8)
            ),
            {invoice.pk: Decimal(500000)},
        )

    def test_late_credit_does_not_require_unspent_wallet_and_allows_unspent_remainder_refund(
        self,
    ):
        invoice, bank, advance, application, obligation = self.journey(
            advance_amount="500000"
        )
        refund = self.movement(
            "-10000", reference="SYN-FIN05-UNAPPLIED-REFUND", day=date(2026, 9, 11)
        )
        finance.refund_customer_advance(
            organization=self.org,
            actor=self.owner,
            advance=advance,
            transaction=refund,
            amount="10000",
            reference="SYN-FIN05-REFUND",
            evidence="Synthetic refund of remaining unapplied customer money",
        )
        self.assertEqual(advance.remaining_amount, 0)
        original = self.history(bank, advance, application)
        row = self.recognize(obligation, application)
        self.assertEqual(self.history(bank, advance, application), original)
        self.assertEqual(invoice.outstanding_amount, 0)
        self.assertEqual(row.cash_amount, 0)
        self.assertEqual(
            finance.report_balances(organization=self.org)["BANK"], Decimal(490000)
        )

    def test_exact_retry_is_one_fact_and_changed_payload_or_source_cannot_replace_it(
        self,
    ):
        invoice, bank, advance, application, obligation = self.journey()
        original = self.history(bank, advance, application)
        first = self.recognize(obligation, application)
        self.assertEqual(self.recognize(obligation, application).pk, first.pk)
        for change in (
            {"date": date(2026, 10, 7)},
            {"reference": "SYN-CHANGED-REF"},
            {"evidence": "Synthetic different explanation"},
        ):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                self.recognize(obligation, application, **change)
        other = self.journey()
        with self.assertRaises(ValidationError):
            self.recognize(obligation, other[3])
        self.assertEqual(
            CustomerTaxSettlement.objects.filter(obligation=obligation).count(), 1
        )
        self.assertEqual(
            Journal.objects.filter(
                organization=self.org, source_key=f"customer-tax-credit:{obligation.pk}"
            ).count(),
            1,
        )
        self.assertEqual(invoice.outstanding_amount, 0)
        # The other journey adds independent sources; the original two journals remain immutable.
        self.assertEqual(
            self.history(bank, advance, application)["journals"], original["journals"]
        )
        self.assertEqual(
            self.history(bank, advance, application)["lines"], original["lines"]
        )

    def test_exact_one_old_source_is_required_without_creating_new_payment(self):
        _, _, _, application, obligation = self.journey()
        other_invoice = finance.create_invoice(
            organization=self.org,
            actor=self.owner,
            party=self.customer,
            product=self.product,
            quantity=1,
            date=date(2026, 9, 9),
            service_date=date(2026, 9, 10),
        )
        finance.issue_invoice(
            organization=self.org, actor=self.owner, invoice=other_invoice
        )
        allocation = finance.reconcile_receipt(
            organization=self.org,
            actor=self.owner,
            invoice=other_invoice,
            transaction=self.movement(
                "1", reference="SYN-FIN05-OTHER-ALLOCATION", day=date(2026, 9, 10)
            ),
            amount="1",
        )
        self.assert_rejected_without_credit(
            obligation, application, source_allocation=allocation
        )
        self.assert_rejected_without_credit(obligation, None)

    def test_application_journal_amount_date_key_and_posting_status_are_required(self):
        _, _, _, application, obligation = self.journey()
        line = application.journal.lines.get(account="AR")
        damaged = [
            (line, "credit", Decimal(489999)),
            (application.journal, "date", date(2026, 9, 11)),
            (application.journal, "source_key", "synthetic-damaged-application-key"),
            (application.journal, "status", "draft"),
        ]
        for row, field, value in damaged:
            old = getattr(row, field)
            with self.subTest(field=field, value=value):
                self.damage(row, field, value)
                try:
                    self.assert_rejected_without_credit(obligation, application)
                finally:
                    self.damage(row, field, old)

    def test_original_advance_bank_journal_and_posting_identity_are_required(self):
        _, _, advance, application, obligation = self.journey()
        line = advance.journal.lines.get(account="BANK")
        posting = advance.bank_posting
        damaged = [
            (line, "debit", Decimal(489999)),
            (advance.journal, "date", date(2026, 9, 2)),
            (advance.journal, "source_key", "synthetic-damaged-advance-key"),
            (posting, "kind", "owner_funding"),
            (posting, "signed_amount", Decimal(489999)),
        ]
        for row, field, value in damaged:
            old = getattr(row, field)
            with self.subTest(field=field, value=value):
                self.damage(row, field, value)
                try:
                    self.assert_rejected_without_credit(obligation, application)
                finally:
                    self.damage(row, field, old)

    def test_foreign_bank_account_and_wrong_advance_party_or_application_invoice_are_rejected(
        self,
    ):
        invoice, bank, advance, application, obligation = self.journey()
        foreign = Organization.objects.create(
            name="Synthetic foreign FIN05", is_demo=True
        )
        foreign_account = BankAccount.objects.create(
            organization=foreign,
            name="Synthetic foreign bank",
            account_number="SYN-FIN05-FOREIGN",
        )
        wrong_party = Party.objects.create(
            organization=self.org, name="Synthetic other buyer", kind="customer"
        )
        other_invoice = finance.create_invoice(
            organization=self.org,
            actor=self.owner,
            party=self.customer,
            product=self.product,
            quantity=1,
            date=date(2026, 9, 9),
            service_date=date(2026, 9, 10),
        )
        finance.issue_invoice(
            organization=self.org, actor=self.owner, invoice=other_invoice
        )
        damaged = [
            (bank, "account_id", foreign_account.pk),
            (advance, "party_id", wrong_party.pk),
            (application, "invoice_id", other_invoice.pk),
            (application, "organization_id", foreign.pk),
        ]
        for row, field, value in damaged:
            old = getattr(row, field)
            with self.subTest(field=field):
                self.damage(row, field, value)
                try:
                    self.assert_rejected_without_credit(obligation, application)
                finally:
                    self.damage(row, field, old)
        self.assertEqual(invoice.outstanding_amount, Decimal(10000))

    def test_paid_point_uses_original_bank_date_not_later_application_date(self):
        _, _, _, application, obligation = self.journey(
            point=date(2026, 9, 10), basis="paid"
        )
        self.assert_rejected_without_credit(obligation, application)

    def test_original_bank_cannot_precede_professionally_declared_available_or_due_point(
        self,
    ):
        for basis in ("available", "due"):
            with self.subTest(basis=basis):
                _, _, _, application, obligation = self.journey(
                    point=date(2026, 9, 2), basis=basis
                )
                self.assert_rejected_without_credit(obligation, application)

    def test_credit_date_cannot_predate_application_or_enter_closed_new_period(self):
        _, _, _, application, obligation = self.journey()
        self.assert_rejected_without_credit(
            obligation, application, date=date(2026, 9, 9)
        )
        AccountingPeriod.objects.create(
            organization=self.org,
            year=2026,
            month=10,
            closed=True,
            closed_at=timezone.now(),
        )
        self.assert_rejected_without_credit(obligation, application)

    def test_stale_company_identity_and_damaged_matched_certificate_block_new_credit(
        self,
    ):
        _, _, _, application, obligation = self.journey()
        old_name = self.org.name
        self.damage(self.org, "name", "Synthetic unreviewed company rename")
        self.assert_rejected_without_credit(obligation, application)
        self.damage(self.org, "name", old_name)
        certificate = obligation.verifications.get(kind="credit")
        self.damage(certificate.evidence, "content", b"Synthetic damaged official PDF")
        self.assert_rejected_without_credit(obligation, application)

    def test_foreign_context_and_inactive_or_marketing_actor_cannot_book_credit(self):
        from django.contrib.auth import get_user_model

        _, _, _, application, obligation = self.journey()
        foreign = Organization.objects.create(
            name="Synthetic unauthorized FIN05", is_demo=True
        )
        Membership.objects.create(organization=foreign, user=self.owner, role="owner")
        self.assert_rejected_without_credit(
            obligation, application, organization=foreign
        )
        marketer = get_user_model().objects.create_user("synthetic-fin05-marketer")
        Membership.objects.create(
            organization=self.org, user=marketer, role="marketing"
        )
        for actor in (marketer, self.owner):
            if actor == self.owner:
                actor.is_active = False
                actor.save(update_fields=["is_active"])
            with self.subTest(actor=actor.pk), self.assertRaises(PermissionDenied):
                self.recognize(obligation, application, actor=actor)
        self.assertFalse(CustomerTaxSettlement.objects.exists())

    def test_source_package_kpi_and_operations_keep_cash_separate_from_dated_credit(
        self,
    ):
        invoice, bank, advance, application, obligation = self.journey()
        member = marketing.create_member(
            organization=self.org,
            actor=self.owner,
            name="Synthetic FIN05 sales",
            discipline="sales",
        )
        lead = marketing.create_lead(
            organization=self.org,
            actor=self.owner,
            reference="SYN-FIN05-LEAD",
            name="Synthetic FIN05 lead",
            owner=member,
            product=self.product,
            party=self.customer,
            channel="sales",
            received_at=datetime(2026, 9, 9, 8, tzinfo=ZoneInfo("Asia/Jakarta")),
        )
        marketing.link_invoice(
            organization=self.org,
            actor=self.owner,
            lead=lead,
            invoice=invoice,
            expected_version=lead.version,
        )
        order = operations.create_order(
            organization=self.org,
            actor=self.owner,
            reference="SYN-FIN05-ORDER",
            buyer=self.customer,
            date=date(2026, 9, 9),
            channel="sales",
        )
        line = operations.add_order_line(
            organization=self.org,
            actor=self.owner,
            order=order,
            product=self.product,
            quantity=1,
        )
        operations.link_order_invoice(
            organization=self.org,
            actor=self.owner,
            line=line,
            invoice=invoice,
            reason="Synthetic exact buyer/product/quantity source match",
        )
        targets = {
            metric: kpi.create_target(
                organization=self.org,
                actor=self.owner,
                title=f"Synthetic FIN05 {metric}",
                metric_key=metric,
                target="1",
                period_start="2026-09-01",
                period_end="2026-10-09",
                rationale="Synthetic reviewed source inventory",
            )
            for metric in ("cash_in", "cohort_paid_leads")
        }
        before_kpi = {
            metric: kpi.evaluate_target(target) for metric, target in targets.items()
        }
        before_marketing = marketing_snapshot(
            organization=self.org, start="2026-09-01", end="2026-10-09"
        )
        before_ops = commercial_snapshot(
            organization=self.org, start="2026-09-01", end="2026-10-09"
        )
        before_month = build_monthly_sources(self.org, date(2026, 9, 1))
        original = self.history(bank, advance, application)
        row = self.recognize(obligation, application)
        after_month = build_monthly_sources(self.org, date(2026, 9, 1))
        current_month = build_monthly_sources(self.org, date(2026, 10, 1))
        self.assertEqual(before_month["source_hash"], after_month["source_hash"])
        self.assertEqual(self.history(bank, advance, application), original)
        tax_rows = [
            item
            for item in current_month["rows"]
            if item["source_kind"] == "customer-tax" and item["source_id"] == row.pk
        ]
        self.assertEqual(len(tax_rows), 1)
        tax_row = tax_rows[0]
        self.assertEqual(
            (
                tax_row["date"],
                tax_row["cash_amount"],
                tax_row["gross_amount"],
                tax_row["tax_amount"],
            ),
            ("2026-10-08", "0.00", "10000.00", "10000.00"),
        )
        self.assertEqual(tax_row["source_advance_application_id"], application.pk)
        self.assertEqual(
            (tax_row["source_advance_id"], tax_row["source_application_date"]),
            (advance.pk, "2026-09-10"),
        )
        original_cash = [
            item
            for item in current_month["rows"]
            if item["source_kind"] == "customer-advance"
            and item["source_id"] == advance.pk
        ]
        self.assertEqual(len(original_cash), 1)
        self.assertEqual(
            (original_cash[0]["date"], original_cash[0]["cash_amount"]),
            ("2026-09-01", "490000.00"),
        )
        self.assertEqual(current_month["invoices"][0]["gross_settled"], "500000.00")
        after_kpi = {
            metric: kpi.evaluate_target(target) for metric, target in targets.items()
        }
        after_marketing = marketing_snapshot(
            organization=self.org, start="2026-09-01", end="2026-10-09"
        )
        self.assertEqual(
            (before_kpi["cash_in"]["actual"], after_kpi["cash_in"]["actual"]),
            ("0.00", "0.00"),
        )
        self.assertEqual(
            before_kpi["cash_in"]["basis"]["provenance"],
            after_kpi["cash_in"]["basis"]["provenance"],
        )
        # This existing KPI explicitly counts a partially paying lead; Operations counts full payment.
        self.assertEqual(
            (
                before_kpi["cohort_paid_leads"]["actual"],
                after_kpi["cohort_paid_leads"]["actual"],
            ),
            ("1.00", "1.00"),
        )
        self.assertEqual(
            (
                before_marketing["summary"]["cohort_applied_payments"],
                after_marketing["summary"]["cohort_applied_payments"],
            ),
            ("490000.00", "500000.00"),
        )
        paid_sources = after_kpi["cohort_paid_leads"]["basis"]["provenance"]["sources"][
            "customer_tax_settlements"
        ]
        self.assertEqual(paid_sources["count"], 1)
        self.assertEqual(paid_sources["records"], [{"id": row.pk}])
        self.assertNotEqual(
            before_kpi["cohort_paid_leads"]["basis"]["provenance"]["digest"],
            after_kpi["cohort_paid_leads"]["basis"]["provenance"]["digest"],
        )
        after_ops = commercial_snapshot(
            organization=self.org, start="2026-09-01", end="2026-10-09"
        )
        self.assertEqual(
            (
                before_ops["cohort_fully_paid_orders"],
                after_ops["cohort_fully_paid_orders"],
            ),
            (0, 1),
        )
        self.assertEqual(
            before_ops["period_gross_bank_receipts"],
            after_ops["period_gross_bank_receipts"],
        )
        self.assertNotEqual(before_ops["fingerprint"], after_ops["fingerprint"])
        historical_ops = commercial_snapshot(
            organization=self.org, start="2026-09-01", end="2026-09-30"
        )
        self.assertEqual(historical_ops["cohort_fully_paid_orders"], 0)

    def test_same_amount_source_identity_changes_invalidate_consumer_fingerprints(self):
        invoice, _, advance, application, obligation = self.journey(
            applied_amount="489999"
        )
        alternate = finance.apply_customer_advance(
            organization=self.org,
            actor=self.owner,
            advance=advance,
            invoice=invoice,
            date=date(2026, 9, 11),
            amount="1",
            reference="SYN-FIN05-SECOND-APPLICATION",
            evidence="Synthetic second part of same invoice application",
        )
        member = marketing.create_member(
            organization=self.org,
            actor=self.owner,
            name="Synthetic FIN05 source PIC",
            discipline="sales",
        )
        lead = marketing.create_lead(
            organization=self.org,
            actor=self.owner,
            reference="SYN-FIN05-SOURCE-LEAD",
            name="Synthetic source identity lead",
            owner=member,
            product=self.product,
            party=self.customer,
            channel="sales",
            received_at=datetime(2026, 9, 9, 8, tzinfo=ZoneInfo("Asia/Jakarta")),
        )
        marketing.link_invoice(
            organization=self.org,
            actor=self.owner,
            lead=lead,
            invoice=invoice,
            expected_version=lead.version,
        )
        order = operations.create_order(
            organization=self.org,
            actor=self.owner,
            reference="SYN-FIN05-SOURCE-ORDER",
            buyer=self.customer,
            date=date(2026, 9, 9),
            channel="sales",
        )
        line = operations.add_order_line(
            organization=self.org,
            actor=self.owner,
            order=order,
            product=self.product,
            quantity=1,
        )
        operations.link_order_invoice(
            organization=self.org,
            actor=self.owner,
            line=line,
            invoice=invoice,
            reason="Synthetic invoice identity evidence",
        )
        target = kpi.create_target(
            organization=self.org,
            actor=self.owner,
            title="Synthetic identity provenance",
            metric_key="cohort_paid_leads",
            target="1",
            period_start="2026-09-01",
            period_end="2026-10-09",
            rationale="Synthetic source identity must remain part of the reviewed result",
        )
        row = self.recognize(obligation, application)
        before_month = build_monthly_sources(self.org, date(2026, 10, 1))
        before_kpi = kpi.evaluate_target(target)
        before_ops = commercial_snapshot(
            organization=self.org, start="2026-09-01", end="2026-10-09"
        )
        # A corrupt source FK must be noticed even when every monetary result is unchanged.
        self.damage(row, "source_advance_application_id", alternate.pk)
        after_month = build_monthly_sources(self.org, date(2026, 10, 1))
        after_kpi = kpi.evaluate_target(target)
        after_ops = commercial_snapshot(
            organization=self.org, start="2026-09-01", end="2026-10-09"
        )
        self.assertEqual((before_kpi["actual"], after_kpi["actual"]), ("1.00", "1.00"))
        self.assertEqual(
            (
                before_ops["cohort_fully_paid_orders"],
                after_ops["cohort_fully_paid_orders"],
            ),
            (1, 1),
        )
        self.assertEqual(
            before_ops["period_gross_bank_receipts"],
            after_ops["period_gross_bank_receipts"],
        )
        self.assertNotEqual(before_month["source_hash"], after_month["source_hash"])
        self.assertNotEqual(
            before_kpi["basis"]["provenance"]["digest"],
            after_kpi["basis"]["provenance"]["digest"],
        )
        self.assertNotEqual(before_ops["fingerprint"], after_ops["fingerprint"])
