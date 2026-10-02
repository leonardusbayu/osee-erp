"""Budget settlement and correction are distinct user-authorized workflows."""
from datetime import date
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse

from .services import record_budget_payment, replace_budget_entry, void_budget_entry
from . import tests_workflows as workflow_fixtures


@override_settings(MFA_REQUIRED=False)
class BudgetRemediationTests(TestCase):
    setUp = workflow_fixtures.DirectorWorkflowTests.setUp
    budget = workflow_fixtures.DirectorWorkflowTests.budget
    entry = workflow_fixtures.DirectorWorkflowTests.entry

    def test_cross_kind_correction_requires_acknowledging_withdrawal_of_prior_cost(self):
        budget = self.budget("100")
        cost = self.entry(budget, "incurred", "60", "cost")
        with self.assertRaisesMessage(ValidationError, "konfirmasi"):
            replace_budget_entry(organization=self.org, actor=self.finance, entry=cost,
                reason="Paid the bill", kind="payment", amount="60", date=date(2026, 9, 8),
                description="Payment", idempotency_key="replacement")
        self.assertEqual(budget.available, Decimal("40"))
        cost.refresh_from_db()
        self.assertIsNone(cost.voided_at)

    def test_linked_payment_http_keeps_cost_consumption_and_retries_once(self):
        budget = self.budget("100")
        cost = self.entry(budget, "incurred", "60", "cost")
        self.client.force_login(self.finance)
        url = reverse("director:budget_payment", args=[budget.pk, cost.pk])
        payload = {"amount": "60", "date": "2026-09-09", "description": "Paid supplier",
                   "reference": "PAY-REF", "idempotency_key": "payment"}
        for _ in range(2):
            response = self.client.post(url, payload)
            self.assertEqual(response.status_code, 302)
        detail = self.client.get(reverse("director:budget_detail", args=[budget.pk]))
        self.assertEqual(detail.context["budget"].available, Decimal("40"))
        self.assertEqual(detail.context["budget"].payments, Decimal("60"))
        cost.refresh_from_db()
        self.assertIsNone(cost.voided_at)
        self.assertContains(detail, "Pembayaran terkait")

    def test_acknowledged_legitimate_cross_kind_correction_remains_available(self):
        budget = self.budget("100")
        cost = self.entry(budget, "incurred", "60", "mistaken-cost")
        replacement = replace_budget_entry(organization=self.org, actor=self.finance, entry=cost,
            reason="Source was a deposit payment, not an incurred expense", kind="payment", amount="60",
            date=date(2026, 9, 8), description="Corrected fact", idempotency_key="correction",
            confirm_reclassification=True)
        self.assertEqual(replacement.replaces_id, cost.pk)
        self.assertEqual(budget.available, Decimal("100"))
        cost.refresh_from_db()
        self.assertIsNotNone(cost.voided_at)

    def test_linked_payment_limits_and_source_correction_guard(self):
        budget = self.budget("100")
        source = self.entry(budget, "open_commitment", "60", "commitment")
        args = dict(organization=self.org, actor=self.finance, entry=source, amount="40",
                    date=date(2026, 9, 9), description="Part payment", reference="PART", idempotency_key="part")
        payment = record_budget_payment(**args)
        with self.assertRaises(ValidationError):
            record_budget_payment(**(args | {"amount": "21", "reference": "EXCESS", "idempotency_key": "excess"}))
        with self.assertRaises(ValidationError):
            void_budget_entry(organization=self.org, actor=self.finance, entry=source, reason="Cannot drop settled source silently")
        self.assertEqual(budget.available, Decimal("40"))
        void_budget_entry(organization=self.org, actor=self.finance, entry=payment, reason="Wrong payment evidence")
        self.assertEqual(budget.payments, Decimal("0"))
        self.assertEqual(budget.available, Decimal("40"))

    def test_linked_payment_tenant_role_and_get_boundaries(self):
        budget = self.budget("100")
        source = self.entry(budget, "incurred", "60", "cost")
        args = dict(organization=self.org, actor=self.marketing, entry=source, amount="10",
                    date=date(2026, 9, 9), description="Payment", reference="PAY", idempotency_key="pay")
        with self.assertRaises(PermissionDenied):
            record_budget_payment(**args)
        url = reverse("director:budget_payment", args=[budget.pk, source.pk])
        self.client.force_login(self.finance)
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(budget.payments, Decimal("0"))
        self.client.force_login(self.auditor)
        self.assertEqual(self.client.post(url, {}).status_code, 403)
        with self.assertRaises(PermissionDenied):
            record_budget_payment(**(args | {"organization": self.other, "actor": self.finance}))
