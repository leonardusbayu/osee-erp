"""Public budget figures stay consistent across management readers and private AI."""

import json
from datetime import date, datetime
from datetime import timezone as datetime_timezone
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import Membership, Organization
from finance.models import Bill, Journal, JournalLine, Party
from finance.services import approve_bill

from . import services as legacy
from . import spend_services as spends
from .ai import ask_director, configure_ai_policy
from .analytics import dashboard_snapshot
from .budget_reporting import budget_summary
from .tests_ai import EXTERNAL, valid_response

NOW = datetime(2026, 10, 9, 6, tzinfo=datetime_timezone.utc)


@override_settings(MFA_REQUIRED=False, DIRECTOR_AI_PRIVATE_AGGREGATES_ENABLED=False)
class BudgetSpendConsumerTests(TestCase):
    def setUp(self):
        clock = patch("django.utils.timezone.now", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)
        self.org = Organization.objects.create(
            name="PRIVATE-BUDGET-COMPANY-DO-NOT-SEND", is_demo=True
        )
        self.other = Organization.objects.create(
            name="FOREIGN-BUDGET-COMPANY", is_demo=True
        )
        self.finance = get_user_model().objects.create_user("budget-reader-finance")
        self.owner = get_user_model().objects.create_user("budget-reader-owner")
        self.outsider = get_user_model().objects.create_user(
            "budget-reader-foreign-owner"
        )
        Membership.objects.create(
            organization=self.org, user=self.finance, role="finance"
        )
        Membership.objects.create(organization=self.org, user=self.owner, role="owner")
        Membership.objects.create(
            organization=self.other, user=self.outsider, role="owner"
        )
        self.budget = self._budget(
            self.org,
            self.finance,
            self.owner,
            "80000000",
            "PRIVATE-CAMPAIGN-DO-NOT-SEND",
        )
        self.entry = legacy.add_budget_entry(
            organization=self.org,
            actor=self.finance,
            budget=self.budget,
            kind="open_commitment",
            amount="80000000",
            date=date(2026, 9, 8),
            description="PRIVATE-MANUAL-EVIDENCE-DO-NOT-SEND",
            idempotency_key="reader-commitment",
        )
        legacy.record_budget_payment(
            organization=self.org,
            actor=self.finance,
            entry=self.entry,
            amount="30000000",
            date=date(2026, 9, 9),
            description="PRIVATE-PAYMENT-DO-NOT-SEND",
            reference="PRIVATE-REFERENCE-DO-NOT-SEND",
            idempotency_key="reader-payment",
        )
        position = spends.budget_position(
            organization=self.org, budget=self.budget, as_of=date(2026, 10, 9)
        )
        self.spend = spends.adopt_budget_entry(
            organization=self.org,
            actor=self.finance,
            entry=self.entry,
            date=date(2026, 9, 10),
            reference="reader-adoption",
            reason="Preserve original payment history",
            evidence="Synthetic reviewed manual records, not bank proof",
            expected_version=self.budget.version,
            expected_fingerprint=position["fingerprint"],
        )
        position = spends.spend_position(
            organization=self.org, spend=self.spend, as_of=date(2026, 10, 9)
        )
        spends.convert_spend(
            organization=self.org,
            actor=self.finance,
            spend=self.spend,
            from_kind="open_commitment",
            to_kind="incurred",
            amount="20000000",
            date=date(2026, 9, 20),
            reference="reader-partial",
            reason="Partial work completed",
            evidence="Synthetic partial performance",
            expected_version=self.spend.version,
            expected_fingerprint=position["fingerprint"],
        )
        self.client.force_login(self.owner)

    @staticmethod
    def _budget(org, finance, owner, amount, title):
        budget = legacy.create_budget(
            organization=org,
            actor=finance,
            title=title,
            year=2026,
            month=9,
            channel="meta_ads",
            amount=amount,
        )
        budget = legacy.transition_budget(
            organization=org,
            actor=finance,
            budget=budget,
            action="submit",
            expected_version=budget.version,
        )
        return legacy.transition_budget(
            organization=org,
            actor=owner,
            budget=budget,
            action="approve",
            expected_version=budget.version,
            notes="Synthetic authorization only",
            self_approval_reason="Synthetic same-owner documented exception"
            if finance == owner
            else "",
        )

    def _link_expense(self, amount="85000000"):
        from finance.budget_adapter import bill_budget_facts

        supplier = Party.objects.create(
            organization=self.org,
            name="PRIVATE-SUPPLIER-DO-NOT-SEND",
            kind=Party.Kind.SUPPLIER,
        )
        bill = Bill.objects.create(
            organization=self.org,
            supplier=supplier,
            number="PRIVATE-BILL-DO-NOT-SEND",
            amount=amount,
            date=date(2026, 9, 8),
            service_date=date(2026, 9, 8),
        )
        approve_bill(organization=self.org, bill=bill, actor=self.finance)
        position = spends.spend_position(
            organization=self.org, spend=self.spend, as_of=date(2026, 10, 9)
        )
        coverage = [
            {"source_key": row["key"], "amount": row["available"]}
            for row in position["coverable_sources"]
            if Decimal(row["available"]) > 0
        ]
        self.spend.refresh_from_db()
        before = list(
            Journal.objects.filter(organization=self.org).order_by("pk").values()
        )
        spends.link_spend_bill(
            organization=self.org,
            actor=self.finance,
            spend=self.spend,
            bill=bill,
            coverage=coverage,
            date=date(2026, 10, 9),
            reference="reader-bill-link",
            reason="Replace overlapping manual observations",
            evidence="Reviewed original synthetic Bill",
            expected_version=self.spend.version,
            expected_fingerprint=position["fingerprint"],
            expected_bill_fingerprint=bill_budget_facts(
                organization=self.org, bill=bill, as_of=date(2026, 10, 9)
            )["fingerprint"],
        )
        self.assertEqual(
            list(Journal.objects.filter(organization=self.org).order_by("pk").values()),
            before,
        )
        return bill

    def test_same_partial_amounts_in_page_model_snapshot_csv_pdf_api_and_ai(self):
        from webapp.test_documents import pdf_text_commands

        canonical = spends.budget_position(
            organization=self.org, budget=self.budget, as_of=date(2026, 10, 9)
        )
        summary = budget_summary(organization=self.org, year=2026)
        snapshot = dashboard_snapshot(organization=self.org, year=2026)
        self.assertEqual(summary["incurred"], canonical["incurred"])
        self.assertEqual(snapshot["budget"]["fingerprint"], summary["fingerprint"])
        self.assertEqual(self.budget.consumed, Decimal(80000000))
        self.assertEqual(self.budget.open_commitments, Decimal(60000000))
        detail = self.client.get(
            reverse("director:budget_detail", args=[self.budget.pk])
        )
        self.assertEqual(
            detail.context["position"]["fingerprint"], canonical["fingerprint"]
        )
        api = self.client.get(reverse("director:snapshot_api"), {"year": 2026}).json()
        self.assertEqual(api["budget"]["fingerprint"], summary["fingerprint"])
        csv = self.client.get(reverse("director:export"), {"year": 2026})
        self.assertContains(csv, "20000000.00")
        self.assertContains(csv, "60000000.00")
        self.assertContains(csv, summary["fingerprint"])
        pdf = self.client.get(reverse("director:report_pdf"), {"year": 2026})
        self.assertEqual(pdf.status_code, 200)
        text = pdf_text_commands(pdf.content)
        self.assertIn("Rp20.000.000", text)
        self.assertIn("Rp60.000.000", text)
        self.assertIn(summary["fingerprint"], "".join(text.split()))
        answer = ask_director(
            organization=self.org, actor=self.owner, question="Tinjau budget", year=2026
        )
        facts = {row["fact_id"]: row for row in answer["source_fact_cards"]}
        self.assertEqual(facts["planning.budget_consumed"]["value"], "80000000.00")
        self.assertEqual(facts["planning.budget_incurred"]["value"], "20000000.00")
        self.assertEqual(facts["planning.budget_verified_cash"]["value"], "0.00")
        self.assertEqual(facts["planning.budget_incurred"]["as_of"], "2026-10-09")
        self.assertEqual(
            answer["source_references"]["budget"]["fingerprint"], summary["fingerprint"]
        )

    def test_partial_recognition_and_adoption_do_not_erase_earlier_cutoff(self):
        before = spends.budget_position(
            organization=self.org, budget=self.budget, as_of=date(2026, 9, 9)
        )
        after = spends.budget_position(
            organization=self.org, budget=self.budget, as_of=date(2026, 9, 20)
        )
        self.assertEqual(before["incurred"], "0.00")
        self.assertEqual(before["open_commitments"], "80000000.00")
        self.assertEqual(before["reported_payments"], "30000000.00")
        self.assertEqual(after["incurred"], "20000000.00")
        self.assertEqual(after["open_commitments"], "60000000.00")
        self.assertEqual(before["consumed"], after["consumed"])

    def test_finance_actual_above_estimate_remains_visible_with_owner_task(self):
        self._link_expense()
        summary = budget_summary(organization=self.org, year=2026)
        self.assertTrue(summary["current"])
        self.assertEqual(summary["incurred"], "85000000.00")
        self.assertEqual(summary["consumed"], "85000000.00")
        self.assertEqual(summary["available"], "-5000000.00")
        self.assertEqual(summary["overrun"], "5000000.00")
        self.assertEqual(summary["verified_cash"], "0.00")
        self.assertTrue(summary["tasks"])
        self.budget.refresh_from_db()
        self.assertEqual(self.budget.amount, Decimal(80000000))
        answer = ask_director(
            organization=self.org, actor=self.owner, question="Tinjau budget", year=2026
        )
        self.assertIn("budget_overrun", {row["code"] for row in answer["observations"]})

    def test_corrupt_finance_proof_yields_unknown_total_and_clear_repair_task(self):
        bill = self._link_expense()
        journal = Journal.objects.get(
            organization=self.org, source_key=f"bill:{bill.pk}:approval"
        )
        # Simulate persisted corruption without weakening the public write guard.
        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {connection.ops.quote_name(JournalLine._meta.db_table)} "
                "SET credit=%s WHERE journal_id=%s AND account=%s",
                ["84000000", journal.pk, "AP"],
            )
        summary = budget_summary(organization=self.org, year=2026)
        self.assertFalse(summary["current"])
        self.assertEqual(summary["status"], "review_required")
        self.assertIsNone(summary["consumed"])
        self.assertIsNone(summary["available"])
        self.assertTrue(summary["issues"])
        page = self.client.get(reverse("director:budget_detail", args=[self.budget.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertFalse(page.context["position"]["current"])
        answer = ask_director(
            organization=self.org, actor=self.owner, question="Tinjau budget", year=2026
        )
        facts = {row["fact_id"]: row["value"] for row in answer["source_fact_cards"]}
        self.assertIsNone(facts["planning.budget_consumed"])
        self.assertIn(
            "budget_sources_required", {row["code"] for row in answer["observations"]}
        )

    def test_approved_budgets_in_other_organization_never_enter_local_summary(self):
        foreign = self._budget(
            self.other,
            self.outsider,
            self.outsider,
            "99000000",
            "Foreign private budget",
        )
        legacy.add_budget_entry(
            organization=self.other,
            actor=self.outsider,
            budget=foreign,
            kind="incurred",
            amount="99000000",
            date=date(2026, 9, 8),
            description="Foreign actual observation",
            idempotency_key="foreign-consumer",
        )
        summary = budget_summary(organization=self.org, year=2026)
        self.assertEqual(summary["approved_count"], 1)
        self.assertEqual(summary["amount"], "80000000.00")
        self.assertEqual(summary["incurred"], "20000000.00")
        self.assertNotIn("Foreign private budget", json.dumps(summary))

    @override_settings(**EXTERNAL)
    def test_external_ai_sees_only_approved_aggregate_and_keeps_required_overrun(self):
        self._link_expense()
        configure_ai_policy(
            organization=self.org,
            actor=self.owner,
            enabled=True,
            monthly_budget_usd="1",
            request_cap_usd="0.10",
        )
        with patch("director.ai.urlopen", side_effect=valid_response) as transport:
            answer = ask_director(
                organization=self.org,
                actor=self.owner,
                question="PRIVATE-QUESTION-DO-NOT-SEND",
                year=2026,
                topic="budget",
            )
        self.assertEqual(answer["mode"], "openrouter")
        self.assertIn("budget_overrun", {row["code"] for row in answer["observations"]})
        self.assertIn(
            "review_budget_overrun", {row["code"] for row in answer["options"]}
        )
        payload = json.loads(transport.call_args.args[0].data)
        body = json.dumps(payload)
        for private in (
            self.org.name,
            self.budget.title,
            self.entry.description,
            "PRIVATE-SUPPLIER-DO-NOT-SEND",
            "PRIVATE-BILL-DO-NOT-SEND",
            "PRIVATE-QUESTION-DO-NOT-SEND",
            "PRIVATE-REFERENCE-DO-NOT-SEND",
        ):
            self.assertNotIn(private, body)
        context = json.loads(payload["messages"][1]["content"])
        facts = {row["fact_id"]: row for row in context["facts"]}
        self.assertEqual(facts["planning.budget_overrun"]["value"], "5000000.00")
        self.assertEqual(facts["planning.budget_overrun"]["as_of"], "2026-10-09")
        self.assertNotIn("source_references", context)

    @override_settings(**EXTERNAL)
    def test_old_ai_consent_does_not_authorize_new_budget_aggregates(self):
        from .models import DirectorAIPolicy

        configure_ai_policy(
            organization=self.org,
            actor=self.owner,
            enabled=True,
            monthly_budget_usd="1",
            request_cap_usd="0.10",
        )
        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {connection.ops.quote_name(DirectorAIPolicy._meta.db_table)} "
                "SET disclosure_version=%s WHERE organization_id=%s",
                ["director-aggregate-v3", self.org.pk],
            )
        with patch("director.ai.urlopen", side_effect=valid_response) as transport:
            answer = ask_director(
                organization=self.org,
                actor=self.owner,
                question="Tinjau budget",
                year=2026,
            )
        self.assertEqual(answer["mode"], "local")
        transport.assert_not_called()
