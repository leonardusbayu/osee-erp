"""Dated bank facts through real source review, ledger, and Director interfaces."""

import json
from datetime import date, datetime
from datetime import timezone as datetime_timezone
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import Membership, Organization
from finance.models import BankAccount, BankTransaction
from finance.services import (
    post_journal,
    reconcile_bank_adjustment,
    record_bank_opening,
)
from finance.statement_services import preview_statement_period, review_statement_period
from finance.test_statement_fixtures import reviewed_statement

from .analytics import dashboard_snapshot

NOW = datetime(2026, 10, 9, 6, tzinfo=datetime_timezone.utc)


@override_settings(MFA_REQUIRED=False)
class DirectorBankPositionTests(TestCase):
    def setUp(self):
        self.clock = patch("django.utils.timezone.now", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.org = Organization.objects.create(name="Synthetic dated bank company")
        self.owner = get_user_model().objects.create_user("dated-bank-owner")
        Membership.objects.create(organization=self.org, user=self.owner, role="owner")
        self.account = BankAccount.objects.create(
            organization=self.org,
            name="Synthetic bank",
            account_number="PRIVATE-ACCOUNT-DO-NOT-SEND",
        )
        self.client.force_login(self.owner)

    def reviewed_bank(self):
        record_bank_opening(
            organization=self.org,
            actor=self.owner,
            account=self.account,
            date="2026-01-01",
            amount="1000",
            reference="OPENING",
            evidence="Synthetic reviewed opening",
        )
        self.account.refresh_from_db()
        income = BankTransaction.objects.create(
            organization=self.org,
            account=self.account,
            date=date(2026, 9, 5),
            reference="FUNDING",
            amount=Decimal(500),
        )
        expense = BankTransaction.objects.create(
            organization=self.org,
            account=self.account,
            date=date(2026, 9, 6),
            reference="FEE",
            amount=Decimal(-100),
        )
        reconcile_bank_adjustment(
            organization=self.org,
            actor=self.owner,
            transaction=income,
            kind="owner_funding",
            amount="500",
            reference="MATCH-FUNDING",
            evidence="Synthetic funding proof",
        )
        reconcile_bank_adjustment(
            organization=self.org,
            actor=self.owner,
            transaction=expense,
            kind="bank_fee",
            amount="100",
            reference="MATCH-FEE",
            evidence="Synthetic fee proof",
        )
        return reviewed_statement(
            self,
            organization=self.org,
            actor=self.owner,
            account=self.account,
            start=date(2026, 1, 1),
            end=date(2026, 9, 30),
            opening="1000",
            closing="1400",
            total_in="500",
            total_out="100",
            count=2,
        )

    def test_current_year_shows_last_completed_month_balance_with_facts_and_review_provenance(
        self,
    ):
        review = self.reviewed_bank()
        snapshot = dashboard_snapshot(organization=self.org, year=2026)
        self.assertEqual(snapshot["finance"]["bank_balance"], "1400.00")
        position = snapshot["finance"]["bank_position"]
        self.assertEqual(position["cutoff"], "2026-09-30")
        self.assertEqual(position["age_days"], 9)
        self.assertEqual(position["status"], "available")
        self.assertEqual(position["book_balance"], "1400.00")
        self.assertEqual(position["source_references"][0]["review_ids"], [review.pk])
        fact = next(
            row for row in snapshot["facts"] if row["fact_id"] == "finance.bank_balance"
        )
        self.assertEqual(fact["value"], "1400.00")
        self.assertEqual(fact["as_of"], "2026-09-30")
        self.assertEqual(fact["basis"], "bank_reconciled")
        bank_readiness = next(
            row for row in snapshot["readiness"] if row["key"] == "bank"
        )
        self.assertEqual(bank_readiness["status"], "available")
        self.assertIn("2026-09-30", bank_readiness["detail"])
        self.assertNotIn(self.account.account_number, str(snapshot["facts"]))

    def test_director_pages_csv_and_pdf_display_same_dated_balance(self):
        from webapp.test_documents import pdf_text_commands

        self.reviewed_bank()
        for route in ("director:overview", "director:reports", "director:readiness"):
            with self.subTest(route=route):
                page = self.client.get(reverse(route), {"year": 2026})
                self.assertContains(page, "2026-09-30")
                if route != "director:readiness":
                    self.assertContains(page, "Rp1.400")
                self.assertNotContains(page, self.account.account_number)
        csv = self.client.get(reverse("director:export"), {"year": 2026})
        self.assertContains(csv, "1400.00")
        self.assertContains(csv, "2026-09-30")
        pdf = self.client.get(reverse("director:report_pdf"), {"year": 2026})
        self.assertEqual(pdf.status_code, 200)
        commands = pdf_text_commands(pdf.content)
        self.assertIn("Rp1.400", commands)
        self.assertIn("2026-09-30", commands)
        self.assertNotIn("Saldo awal dan rekonsiliasi lengkap belum disahkan", commands)

    def test_all_known_accounts_require_the_same_exact_cutoff_and_other_organization_is_excluded(
        self,
    ):
        self.reviewed_bank()
        other = Organization.objects.create(name="Another synthetic company")
        BankAccount.objects.create(
            organization=other, name="Foreign private bank", account_number="FOREIGN"
        )
        self.assertEqual(
            dashboard_snapshot(organization=self.org)["finance"]["bank_balance"],
            "1400.00",
        )
        BankAccount.objects.create(
            organization=self.org, name="Second bank", account_number="SECOND"
        )
        snapshot = dashboard_snapshot(organization=self.org)
        self.assertIsNone(snapshot["finance"]["bank_balance"])
        self.assertIn(
            "Second bank", " ".join(snapshot["finance"]["bank_position"]["issues"])
        )
        self.assertNotIn("Foreign private bank", str(snapshot))

    def test_source_changed_after_review_withholds_balance_and_replacement_uses_new_review(
        self,
    ):
        review = self.reviewed_bank()
        source = review.statement
        with source.file.open("rb") as handle:
            original = handle.read()
        with open(source.file.path, "wb") as handle:
            handle.write(original + b"changed")
        snapshot = dashboard_snapshot(organization=self.org)
        self.assertIsNone(snapshot["finance"]["bank_balance"])
        self.assertIn(
            "tidak sesuai", " ".join(snapshot["finance"]["bank_position"]["issues"])
        )
        with open(source.file.path, "wb") as handle:
            handle.write(original)
        facts = dict(
            organization=self.org,
            actor=self.owner,
            statement=source,
            period_start=review.period_start,
            period_end=review.period_end,
            opening_balance="1000",
            closing_balance="1400",
            total_in="500",
            total_out="100",
            transaction_count=2,
            evidence="Synthetic re-review with corrected evidence",
        )
        preview = preview_statement_period(**facts)
        replacement = review_statement_period(
            **facts,
            expected_fingerprint=preview["fingerprint"],
            confirmed=True,
            supersedes=review,
        )
        position = dashboard_snapshot(organization=self.org)["finance"]["bank_position"]
        self.assertEqual(position["balance"], "1400.00")
        self.assertEqual(
            position["source_references"][0]["review_ids"], [replacement.pk]
        )

    def test_matching_source_requires_matching_bank_ledger_and_no_offsetting_unmatched_lines(
        self,
    ):
        self.reviewed_bank()
        post_journal(
            organization=self.org,
            actor=self.owner,
            date="2026-09-30",
            source_key="UNLINKED-BANK",
            description="Synthetic ledger discrepancy",
            entries=[("BANK", "1", "0"), ("EQUITY", "0", "1")],
        )
        position = dashboard_snapshot(organization=self.org)["finance"]["bank_position"]
        self.assertIsNone(position["balance"])
        self.assertEqual(position["statement_balance"], "1400.00")
        self.assertEqual(position["book_balance"], "1401.00")
        self.assertEqual(position["difference"], "-1.00")
        self.assertTrue(any("berbeda" in issue for issue in position["issues"]))

    def test_equal_unmatched_receipt_and_payment_do_not_certify_even_when_totals_match(
        self,
    ):
        record_bank_opening(
            organization=self.org,
            actor=self.owner,
            account=self.account,
            date="2026-01-01",
            amount="1000",
            reference="OPENING",
            evidence="Synthetic reviewed opening",
        )
        self.account.refresh_from_db()
        for reference, amount in (("UNMATCHED-IN", "50"), ("UNMATCHED-OUT", "-50")):
            BankTransaction.objects.create(
                organization=self.org,
                account=self.account,
                date=date(2026, 9, 5),
                reference=reference,
                amount=Decimal(amount),
            )
        reviewed_statement(
            self,
            organization=self.org,
            actor=self.owner,
            account=self.account,
            start=date(2026, 1, 1),
            end=date(2026, 9, 30),
            opening="1000",
            closing="1000",
            total_in="50",
            total_out="50",
            count=2,
        )
        position = dashboard_snapshot(organization=self.org)["finance"]["bank_position"]
        self.assertIsNone(position["balance"])
        self.assertEqual(position["difference"], "0.00")
        self.assertTrue(any("2 mutasi" in issue for issue in position["issues"]))

    def test_zero_is_a_known_balance_only_after_explicit_zero_source_review(self):
        self.assertIsNone(
            dashboard_snapshot(organization=self.org)["finance"]["bank_balance"]
        )
        reviewed_statement(
            self,
            organization=self.org,
            actor=self.owner,
            account=self.account,
            start=date(2026, 1, 1),
            end=date(2026, 9, 30),
        )
        position = dashboard_snapshot(organization=self.org)["finance"]["bank_position"]
        self.assertEqual(position["status"], "available")
        self.assertEqual(position["balance"], "0.00")

    def test_new_zero_account_does_not_inherit_another_accounts_prior_opening(self):
        self.reviewed_bank()
        second = BankAccount.objects.create(
            organization=self.org, name="New zero bank", account_number="NEW-ZERO"
        )
        reviewed_statement(
            self,
            organization=self.org,
            actor=self.owner,
            account=second,
            start=date(2026, 7, 1),
            end=date(2026, 9, 30),
        )
        position = dashboard_snapshot(organization=self.org)["finance"]["bank_position"]
        self.assertEqual(position["status"], "available", position["issues"])
        self.assertEqual(position["balance"], "1400.00")

    def test_new_zero_account_accepts_another_accounts_prior_reconciled_movements(self):
        income = BankTransaction.objects.create(
            organization=self.org,
            account=self.account,
            date=date(2026, 3, 5),
            reference="OLD-ACCOUNT-FUNDING",
            amount=Decimal(500),
        )
        fee = BankTransaction.objects.create(
            organization=self.org,
            account=self.account,
            date=date(2026, 3, 6),
            reference="OLD-ACCOUNT-FEE",
            amount=Decimal(-100),
        )
        reconcile_bank_adjustment(
            organization=self.org,
            actor=self.owner,
            transaction=income,
            kind="owner_funding",
            amount="500",
            reference="OLD-MATCH-FUNDING",
            evidence="Synthetic early funding",
        )
        reconcile_bank_adjustment(
            organization=self.org,
            actor=self.owner,
            transaction=fee,
            kind="bank_fee",
            amount="100",
            reference="OLD-MATCH-FEE",
            evidence="Synthetic early fee",
        )
        reviewed_statement(
            self,
            organization=self.org,
            actor=self.owner,
            account=self.account,
            start=date(2026, 1, 1),
            end=date(2026, 9, 30),
            closing="400",
            total_in="500",
            total_out="100",
            count=2,
        )
        second = BankAccount.objects.create(
            organization=self.org, name="New zero bank", account_number="NEW-ZERO"
        )
        reviewed_statement(
            self,
            organization=self.org,
            actor=self.owner,
            account=second,
            start=date(2026, 7, 1),
            end=date(2026, 9, 30),
        )
        position = dashboard_snapshot(organization=self.org)["finance"]["bank_position"]
        self.assertEqual(position["status"], "available", position["issues"])
        self.assertEqual(position["balance"], "400.00")

    def test_new_zero_account_accepts_another_accounts_prior_invoice_and_bill_payments(
        self,
    ):
        from finance.models import Bill, Party, Product
        from finance.services import (
            approve_bill,
            create_invoice,
            issue_invoice,
            reconcile_bill_payment,
            reconcile_receipt,
        )

        customer = Party.objects.create(
            organization=self.org, name="Synthetic early customer", kind="customer"
        )
        supplier = Party.objects.create(
            organization=self.org, name="Synthetic early supplier", kind="supplier"
        )
        product = Product.objects.create(
            organization=self.org,
            code="EARLY-ITP",
            name="Synthetic test",
            kind="itp",
            default_price="500",
        )
        invoice = create_invoice(
            organization=self.org,
            actor=self.owner,
            party=customer,
            product=product,
            quantity=1,
            date=date(2026, 3, 1),
            service_date=date(2026, 3, 10),
        )
        issue_invoice(organization=self.org, actor=self.owner, invoice=invoice)
        income = BankTransaction.objects.create(
            organization=self.org,
            account=self.account,
            date=date(2026, 3, 5),
            reference="OLD-INVOICE-RECEIPT",
            amount=Decimal(500),
        )
        reconcile_receipt(
            organization=self.org,
            actor=self.owner,
            invoice=invoice,
            transaction=income,
            amount="500",
        )
        bill = Bill.objects.create(
            organization=self.org,
            supplier=supplier,
            number="OLD-SUPPLIER-BILL",
            date=date(2026, 3, 1),
            service_date=date(2026, 3, 1),
            amount=Decimal(100),
        )
        bill = approve_bill(organization=self.org, actor=self.owner, bill=bill)
        bill.tax_status, bill.tax_amount = "reviewed", Decimal(0)
        bill._tax_service_transition = (
            True  # Synthetic approved zero-withholding fixture.
        )
        bill.save()
        payment = BankTransaction.objects.create(
            organization=self.org,
            account=self.account,
            date=date(2026, 3, 6),
            reference="OLD-BILL-PAYMENT",
            amount=Decimal(-100),
        )
        reconcile_bill_payment(
            organization=self.org,
            actor=self.owner,
            bill=bill,
            transaction=payment,
            amount="100",
        )
        reviewed_statement(
            self,
            organization=self.org,
            actor=self.owner,
            account=self.account,
            start=date(2026, 1, 1),
            end=date(2026, 9, 30),
            closing="400",
            total_in="500",
            total_out="100",
            count=2,
        )
        second = BankAccount.objects.create(
            organization=self.org, name="New zero bank", account_number="NEW-ZERO"
        )
        reviewed_statement(
            self,
            organization=self.org,
            actor=self.owner,
            account=second,
            start=date(2026, 7, 1),
            end=date(2026, 9, 30),
        )
        position = dashboard_snapshot(organization=self.org)["finance"]["bank_position"]
        self.assertEqual(position["status"], "available", position["issues"])
        self.assertEqual(position["balance"], "400.00")

    def test_new_zero_account_cannot_hide_unassigned_prior_journals_with_equal_cutoff_total(
        self,
    ):
        self.reviewed_bank()
        post_journal(
            organization=self.org,
            actor=self.owner,
            date="2026-06-01",
            source_key="UNKNOWN-BANK-IN",
            description="Synthetic unassigned bank receipt",
            entries=[("BANK", "25", "0"), ("EQUITY", "0", "25")],
        )
        post_journal(
            organization=self.org,
            actor=self.owner,
            date="2026-08-01",
            source_key="UNKNOWN-BANK-OUT",
            description="Synthetic unassigned bank payment",
            entries=[("EQUITY", "25", "0"), ("BANK", "0", "25")],
        )
        second = BankAccount.objects.create(
            organization=self.org, name="New zero bank", account_number="NEW-ZERO"
        )
        reviewed_statement(
            self,
            organization=self.org,
            actor=self.owner,
            account=second,
            start=date(2026, 7, 1),
            end=date(2026, 9, 30),
        )
        position = dashboard_snapshot(organization=self.org)["finance"]["bank_position"]
        self.assertIsNone(position["balance"])
        self.assertEqual(position["difference"], "0.00")
        self.assertTrue(
            any("Saldo awal perlu disahkan" in issue for issue in position["issues"])
        )

    def test_zero_review_cannot_hide_unassigned_prior_journals_that_offset_before_start(
        self,
    ):
        for source, entries in (
            ("UNKNOWN-IN", [("BANK", "25", "0"), ("EQUITY", "0", "25")]),
            ("UNKNOWN-OUT", [("EQUITY", "25", "0"), ("BANK", "0", "25")]),
        ):
            post_journal(
                organization=self.org,
                actor=self.owner,
                date="2026-06-01",
                source_key=source,
                description="Synthetic unassigned prior bank journal",
                entries=entries,
            )
        reviewed_statement(
            self,
            organization=self.org,
            actor=self.owner,
            account=self.account,
            start=date(2026, 7, 1),
            end=date(2026, 9, 30),
        )
        position = dashboard_snapshot(organization=self.org)["finance"]["bank_position"]
        self.assertIsNone(position["balance"])
        self.assertEqual(position["difference"], "0.00")
        self.assertTrue(
            any("Saldo awal perlu disahkan" in issue for issue in position["issues"])
        )

    def test_zero_review_cannot_hide_earlier_bank_movements(self):
        BankTransaction.objects.create(
            organization=self.org,
            account=self.account,
            date=date(2026, 8, 1),
            reference="MISSING-HISTORY",
            amount=Decimal(10),
        )
        reviewed_statement(
            self,
            organization=self.org,
            actor=self.owner,
            account=self.account,
            start=date(2026, 9, 1),
            end=date(2026, 9, 30),
        )
        position = dashboard_snapshot(organization=self.org)["finance"]["bank_position"]
        self.assertIsNone(position["balance"])
        self.assertTrue(
            any("Saldo awal perlu disahkan" in issue for issue in position["issues"])
        )

    def test_journal_alone_cannot_replace_approved_opening_or_statement_review(self):
        post_journal(
            organization=self.org,
            actor=self.owner,
            date="2026-01-01",
            source_key="UNREVIEWED-OPENING",
            description="Synthetic generic bank journal",
            entries=[("BANK", "1400", "0"), ("EQUITY", "0", "1400")],
        )
        reviewed_statement(
            self,
            organization=self.org,
            actor=self.owner,
            account=self.account,
            start=date(2026, 1, 1),
            end=date(2026, 9, 30),
            opening="1400",
            closing="1400",
        )
        position = dashboard_snapshot(organization=self.org)["finance"]["bank_position"]
        self.assertIsNone(position["balance"])
        self.assertEqual(position["difference"], "0.00")
        self.assertTrue(
            any("Saldo awal perlu disahkan" in issue for issue in position["issues"])
        )

    def test_review_that_ends_later_does_not_supply_a_false_month_end_balance(self):
        reviewed_statement(
            self,
            organization=self.org,
            actor=self.owner,
            account=self.account,
            start=date(2026, 1, 1),
            end=date(2026, 10, 9),
        )
        position = dashboard_snapshot(organization=self.org)["finance"]["bank_position"]
        self.assertIsNone(position["balance"])
        self.assertTrue(
            any("tepat pada tanggal" in issue for issue in position["issues"])
        )

    def test_missing_source_interval_before_cutoff_blocks_coverage(self):
        reviewed_statement(
            self,
            organization=self.org,
            actor=self.owner,
            account=self.account,
            start=date(2026, 1, 1),
            end=date(2026, 1, 31),
        )
        reviewed_statement(
            self,
            organization=self.org,
            actor=self.owner,
            account=self.account,
            start=date(2026, 9, 1),
            end=date(2026, 9, 30),
        )
        position = dashboard_snapshot(organization=self.org)["finance"]["bank_position"]
        self.assertIsNone(position["balance"])
        self.assertTrue(any("terputus" in issue for issue in position["issues"]))

    def test_year_cutoffs_never_fall_back_to_old_statement_or_future_month(self):
        self.reviewed_bank()
        past = dashboard_snapshot(organization=self.org, year=2025)["finance"][
            "bank_position"
        ]
        self.assertEqual(past["cutoff"], "2025-12-31")
        self.assertIsNone(past["balance"])
        future = dashboard_snapshot(organization=self.org, year=2027)["finance"][
            "bank_position"
        ]
        self.assertIsNone(future["cutoff"])
        self.assertIsNone(future["balance"])
        with patch(
            "django.utils.timezone.now",
            return_value=datetime(2026, 1, 10, tzinfo=datetime_timezone.utc),
        ):
            january = dashboard_snapshot(organization=self.org, year=2026)["finance"][
                "bank_position"
            ]
        self.assertIsNone(january["cutoff"])
        self.assertIsNone(january["balance"])

    def test_previous_ai_disclosure_cannot_start_sending_newly_available_bank_balance(
        self,
    ):
        from .ai import ask_director, configure_ai_policy
        from .tests_ai import EXTERNAL, valid_response

        self.reviewed_bank()
        policy = configure_ai_policy(
            organization=self.org,
            actor=self.owner,
            enabled=True,
            monthly_budget_usd="1",
            request_cap_usd="0.1",
        )
        policy.disclosure_version = "director-aggregate-v2"
        policy.save(update_fields=["disclosure_version"])
        with (
            override_settings(**EXTERNAL),
            patch("director.ai.urlopen", side_effect=valid_response) as transport,
        ):
            answer = ask_director(
                organization=self.org,
                actor=self.owner,
                year=2026,
                question="Periksa saldo bank dan kewajiban",
            )
        self.assertEqual(answer["mode"], "local")
        transport.assert_not_called()

    def test_new_ai_consent_sends_only_bank_aggregate_and_cutoff_keeps_source_ids_local(
        self,
    ):
        from .ai import DISCLOSURE_VERSION, ask_director, configure_ai_policy
        from .tests_ai import EXTERNAL, valid_response

        review = self.reviewed_bank()
        policy = configure_ai_policy(
            organization=self.org,
            actor=self.owner,
            enabled=True,
            monthly_budget_usd="1",
            request_cap_usd="0.1",
        )
        self.assertEqual(policy.disclosure_version, DISCLOSURE_VERSION)
        # The budget aggregate extension requires a new explicit consent.
        # Keep the bank privacy/cutoff assertions below unchanged.
        self.assertEqual(policy.disclosure_version, "director-aggregate-v4")
        with (
            override_settings(**EXTERNAL),
            patch("director.ai.urlopen", side_effect=valid_response) as transport,
        ):
            answer = ask_director(
                organization=self.org,
                actor=self.owner,
                year=2026,
                question="Periksa saldo bank dan kewajiban",
            )
        self.assertEqual(answer["mode"], "openrouter")
        context = json.loads(
            json.loads(transport.call_args.args[0].data)["messages"][1]["content"]
        )
        bank = next(
            row for row in context["facts"] if row["fact_id"] == "finance.bank_balance"
        )
        self.assertEqual(
            bank,
            {
                "fact_id": "finance.bank_balance",
                "value": "1400.00",
                "unit": "IDR",
                "basis": "bank_reconciled",
                "as_of": "2026-09-30",
            },
        )
        outbound = json.dumps(context)
        for private in (
            self.account.name,
            self.account.account_number,
            self.org.name,
            "review_ids",
            "account_id",
            "statement_id",
            "Synthetic reviewed opening",
        ):
            self.assertNotIn(private, outbound)
        card = next(
            row
            for row in answer["source_fact_cards"]
            if row["fact_id"] == "finance.bank_balance"
        )
        self.assertEqual(card["value"], "1400.00")
        self.assertIn("2026-09-30", card["coverage"])
        self.assertIn("Bukan saldo saat ini", card["coverage"])
        self.assertEqual(
            answer["source_references"]["bank"][0]["review_ids"], [review.pk]
        )
