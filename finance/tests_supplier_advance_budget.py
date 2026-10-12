"""Synthetic public contracts for supplier principal in Director budgets."""

from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase
from django.utils import timezone

from director import spend_services as spending
from director.spend_models import BudgetPaymentMatch

from . import tests_budget_link as budget_fixtures
from .tests_supplier_advances import SupplierAdvanceFixtures


class SupplierAdvanceBudgetTests(SupplierAdvanceFixtures, TestCase):
    spend = budget_fixtures.BillBudgetLinkTests.spend
    link = budget_fixtures.BillBudgetLinkTests.link
    facts = budget_fixtures.BillBudgetLinkTests.facts

    def setUp(self):
        super().setUp()
        self.day = date(2026, 9, 10)

    def supplied(self):
        advance = self.advance("1000", day=date(2026, 9, 9))
        bill = self.bill("600", day=self.day)
        return advance, bill

    def test_application_settles_ap_without_another_bank_outflow_or_cost(self):
        advance, bill = self.supplied()
        application = self.command(
            "apply", advance=advance, bill=bill, amount="400", date=date(2026, 9, 12)
        )
        early = self.facts(bill, date(2026, 9, 11))
        now = self.facts(bill, date(2026, 9, 12))
        self.assertEqual(
            (early["ap_outstanding"], early["verified_cash_outflow"]),
            ("600.00", "0.00"),
        )
        self.assertEqual(
            (now["incurred"], now["ap_outstanding"], now["verified_cash_outflow"]),
            ("600.00", "200.00", "400.00"),
        )
        event = next(
            row
            for row in now["events"]
            if row["kind"] == "supplier_advance_application"
        )
        self.assertEqual(
            (event["ap_settled"], event["cash_outflow"], event["incurred"]),
            ("400.00", "0.00", "0.00"),
        )
        payment = next(row for row in now["payments"] if row["id"] == application.pk)
        self.assertEqual(
            (
                payment["kind"],
                payment["date"],
                payment["available_from"],
                payment["amount"],
            ),
            ("supplier_advance_application", "2026-09-09", "2026-09-12", "400.00"),
        )

    def test_healthy_sibling_application_does_not_change_existing_cash_receipt(self):
        advance, bill = self.supplied()
        self.command(
            "apply", advance=advance, bill=bill, amount="400", date=date(2026, 9, 12)
        )
        original = self.facts(bill)
        sibling = self.bill("600", day=self.day)
        self.command(
            "apply", advance=advance, bill=sibling, amount="600", date=date(2026, 9, 13)
        )
        updated = self.facts(bill)
        self.assertEqual(
            updated["payments"][0]["source_hash"],
            original["payments"][0]["source_hash"],
        )
        self.assertNotEqual(updated["fingerprint"], original["fingerprint"])
        self.assertEqual(
            Decimal(updated["verified_cash_outflow"])
            + Decimal(self.facts(sibling)["verified_cash_outflow"]),
            Decimal(1000),
        )

    def test_refund_of_unused_principal_never_becomes_budget_income(self):
        advance, bill = self.supplied()
        self.command(
            "apply", advance=advance, bill=bill, amount="400", date=date(2026, 9, 12)
        )
        self.command(
            "refund",
            advance=advance,
            amount="600",
            transaction=self.movement(
                "600", day=date(2026, 9, 13), reference="UNUSED-PRINCIPAL"
            ),
        )
        facts = self.facts(bill)
        self.assertEqual(
            (facts["incurred"], facts["verified_cash_outflow"]), ("600.00", "400.00")
        )

    def action(self, spend, reference, day):
        spend.refresh_from_db()
        row = spending.spend_position(
            organization=self.org, spend=spend, as_of=timezone.localdate()
        )
        return dict(
            organization=self.org,
            actor=self.actor,
            date=day,
            reference=reference,
            reason="Synthetic exact correspondence reviewed.",
            evidence="Synthetic financial proof.",
            expected_version=spend.version,
            expected_fingerprint=row["fingerprint"],
        )

    def linked(self):
        advance, bill = self.supplied()
        _, entry, spend = self.spend(amount="1000")
        self.link(spend, entry, bill)
        application = self.command(
            "apply", advance=advance, bill=bill, amount="400", date=date(2026, 9, 12)
        )
        manual = spending.record_spend_payment(
            spend=spend, amount="400", **self.action(spend, "MANUAL-SUPPLIER", self.day)
        )
        return advance, bill, spend, application, manual

    def test_matching_cannot_be_backdated_before_application_even_if_cash_is_older(
        self,
    ):
        _, _, spend, application, manual = self.linked()
        before = BudgetPaymentMatch.objects.count()
        with self.assertRaises(ValidationError):
            spending.match_spend_payment(
                spend=spend,
                manual_source_key=f"movement:{manual.pk}",
                finance_event_id=f"supplier_advance_application:{application.pk}",
                amount="400",
                **self.action(spend, "EARLY-MATCH", date(2026, 9, 11)),
            )
        self.assertEqual(BudgetPaymentMatch.objects.count(), before)

    def test_reversal_requires_explicit_cash_match_correction_then_reopens_ap(self):
        advance, bill, spend, application, manual = self.linked()
        match = spending.match_spend_payment(
            spend=spend,
            manual_source_key=f"movement:{manual.pk}",
            finance_event_id=f"supplier_advance_application:{application.pk}",
            amount="400",
            **self.action(spend, "MATCH-SUPPLIER", date(2026, 9, 12)),
        )
        with self.assertRaises(ValidationError):
            self.command("reverse", application=application, date=date(2026, 9, 14))
        spending.void_spend_payment_match(
            match=match, **self.action(spend, "VOID-MATCH-SUPPLIER", date(2026, 9, 14))
        )
        with self.assertRaises(ValidationError):
            self.command("reverse", application=application, date=date(2026, 9, 13))
        self.command("reverse", application=application, date=date(2026, 9, 14))
        old, current = (
            self.facts(bill, date(2026, 9, 13)),
            self.facts(bill, date(2026, 9, 14)),
        )
        self.assertEqual(
            (old["ap_outstanding"], old["verified_cash_outflow"]), ("200.00", "400.00")
        )
        self.assertEqual(
            (
                current["ap_outstanding"],
                current["verified_cash_outflow"],
                current["incurred"],
            ),
            ("600.00", "0.00", "600.00"),
        )
        self.assertEqual(
            self.position(advance, date(2026, 9, 30))["remaining"], Decimal(1000)
        )

    def test_pending_tax_review_survives_full_refund_and_is_carried_to_later_reports(
        self,
    ):
        from taxes.monthly_source_workflow import prepare_monthly_package
        from taxes.services import annual_readiness, monthly_tax_context

        advance = self.advance("1000", day=date(2026, 1, 1))
        self.command(
            "refund",
            advance=advance,
            amount="1000",
            transaction=self.movement(
                "1000", day=date(2026, 1, 2), reference="FULL-PRINCIPAL-REFUND"
            ),
        )
        context = monthly_tax_context(self.org, date(2026, 9, 1))
        self.assertEqual(
            context["supplier_advance_tax_candidates"][0]["advance_id"], advance.pk
        )
        self.assertTrue(
            any(g["code"] == "supplier_advance_review" for g in context["gates"])
        )
        package = prepare_monthly_package(
            organization=self.org, actor=self.actor, period=date(2026, 9, 1)
        )
        self.assertEqual(
            package.profile_snapshot["supplier_advance_reviews"]["inventory"][0][
                "advance_id"
            ],
            advance.pk,
        )
        self.assertTrue(
            any(
                g["code"] == "supplier_advance_review"
                for g in annual_readiness(self.org, 2026)["gates"]
            )
        )

    def test_opaque_repair_identity_binds_transitive_damaged_advance_and_bank_account(
        self,
    ):
        from core.models import Organization

        from .budget_adapter import bill_budget_source_identity
        from .models import BankAccount, BankTransaction

        first, bill = self.supplied()
        sibling = self.bill("600", day=self.day)
        self.command(
            "apply", advance=first, bill=bill, amount="300", date=date(2026, 9, 12)
        )
        self.command(
            "apply", advance=first, bill=sibling, amount="300", date=date(2026, 9, 12)
        )
        other = self.advance("1000", day=date(2026, 9, 9))
        self.command(
            "apply", advance=other, bill=sibling, amount="100", date=date(2026, 9, 12)
        )
        foreign = Organization.objects.create(
            name="Synthetic foreign source", is_demo=True
        )
        account = BankAccount.objects.create(
            organization=foreign,
            name="Foreign synthetic account",
            account_number="SYN-FOREIGN",
        )
        quote = connection.ops.quote_name
        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {quote(BankTransaction._meta.db_table)} SET account_id=%s WHERE id=%s",
                [account.pk, other.bank_posting.transaction_id],
            )
        damaged = bill_budget_source_identity(
            organization=self.org, bill=bill, as_of=timezone.localdate()
        )
        with self.assertRaises(ValidationError):
            self.facts(bill)

        # The graph remains damaged, but a changed invalid account identity must
        # still invalidate a previously viewed Owner repair intention.
        account.account_number = "SYN-FOREIGN-CHANGED"
        account.save()
        changed = bill_budget_source_identity(
            organization=self.org, bill=bill, as_of=timezone.localdate()
        )
        self.assertNotEqual(changed, damaged)
        with self.assertRaises(ValidationError):
            self.facts(bill)

    def test_opaque_repair_identity_binds_damaged_shared_customer_claim(self):
        from . import services as ledger
        from .budget_adapter import bill_budget_source_identity
        from .models import JournalLine

        advance, bill = self.supplied()
        self.command(
            "apply", advance=advance, bill=bill, amount="400", date=date(2026, 9, 12)
        )
        bank = self.movement(
            "1000", day=date(2026, 9, 13), reference="SHARED-SUPPLIER-CUSTOMER"
        )
        invoice = ledger.issue_invoice(
            organization=self.org, actor=self.actor, invoice=self.invoice()
        )
        allocation = ledger.reconcile_receipt(
            organization=self.org,
            actor=self.actor,
            invoice=invoice,
            transaction=bank,
            amount="400",
        )
        self.command("refund", advance=advance, transaction=bank, amount="600")
        line = JournalLine.objects.get(
            journal__organization=self.org,
            journal__source_key=f"allocation:{allocation.pk}",
            account="AR",
        )
        quote = connection.ops.quote_name
        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {quote(JournalLine._meta.db_table)} SET account=%s WHERE id=%s",
                ["EXPENSE", line.pk],
            )
        damaged = bill_budget_source_identity(
            organization=self.org, bill=bill, as_of=timezone.localdate()
        )
        with self.assertRaises(ValidationError):
            self.facts(bill)
        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {quote(JournalLine._meta.db_table)} SET account=%s WHERE id=%s",
                ["PREPAID", line.pk],
            )
        changed = bill_budget_source_identity(
            organization=self.org, bill=bill, as_of=timezone.localdate()
        )
        self.assertNotEqual(changed, damaged)
        with self.assertRaises(ValidationError):
            self.facts(bill)
