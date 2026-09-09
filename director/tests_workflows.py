from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.urls import reverse

from core.models import AuditEvent, Membership, Organization
from finance.models import BankTransaction, Invoice, Journal
from .models import BudgetEntry, CashPlanLine, Decision, MarketingObservation
from .services import (add_budget_entry, add_cash_line, create_budget, create_cash_plan,
                       create_decision, create_marketing_observation, create_objective,
                       replace_budget_entry, replace_cash_line, replace_marketing_observation,
                       save_scenario, transition_budget, transition_decision, update_budget,
                       update_cash_plan, update_decision, update_objective, void_budget_entry,
                       void_cash_line, void_marketing_observation)


DAY = date(2026, 9, 8)


class DirectorWorkflowTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Director workflow tests")
        self.other = Organization.objects.create(name="Other company")
        for role in ("owner", "director", "finance", "marketing", "auditor"):
            user = get_user_model().objects.create_user(username=f"director-test-{role}")
            Membership.objects.create(organization=self.org, user=user, role=role)
            setattr(self, role, user)

    def budget(self, amount="20000000"):
        obj = create_budget(organization=self.org, actor=self.finance, title="September campaign", year=2026,
                            month=9, channel="meta_ads", amount=amount)
        obj = transition_budget(organization=self.org, actor=self.finance, budget=obj, action="submit", expected_version=obj.version)
        return transition_budget(organization=self.org, actor=self.director, budget=obj, action="approve",
                                 expected_version=obj.version, notes="Envelope reviewed; cash execution remains separate.")

    def decision(self):
        return create_decision(organization=self.org, actor=self.finance, title="Review campaign",
                               problem="Incomplete conversion mapping", evidence="Monthly source review",
                               option="Run a bounded measurement pilot", risks="Attribution may remain incomplete",
                               owner_name="Marketing lead", due_date=DAY, review_date=date(2026, 9, 30), amount="1000000")

    def entry(self, budget, kind, amount, key):
        return add_budget_entry(organization=self.org, actor=self.finance, budget=budget, kind=kind,
                                amount=amount, date=DAY, description=kind, idempotency_key=key)

    def test_budget_payments_do_not_consume_authorization_twice(self):
        budget = self.budget()
        self.entry(budget, "incurred", "6000000", "incurred")
        self.entry(budget, "open_commitment", "4000000", "commitment")
        self.entry(budget, "payment", "3000000", "payment")
        self.assertEqual(budget.consumed, Decimal("10000000"))
        self.assertEqual(budget.available, Decimal("10000000"))
        self.assertEqual(budget.payments, Decimal("3000000"))
        self.assertFalse(Journal.objects.exists())
        self.assertFalse(BankTransaction.objects.exists())

    def test_budget_replay_conflict_oversubscription_and_period_validation(self):
        budget = self.budget("100")
        first = self.entry(budget, "reservation", "60", "same")
        self.assertEqual(self.entry(budget, "reservation", "60", "same").pk, first.pk)
        for amount, key in [("61", "same"), ("41", "new")]:
            with self.assertRaises(ValidationError):
                self.entry(budget, "reservation", amount, key)
        with self.assertRaises(ValidationError):
            add_budget_entry(organization=self.org, actor=self.finance, budget=budget, kind="incurred", amount="1",
                             date=date(2026, 10, 1), description="Wrong cost period", idempotency_key="wrong-period")
        self.assertEqual(BudgetEntry.objects.count(), 1)

    def test_budget_replacement_is_atomic_and_old_amount_is_not_double_counted(self):
        budget = self.budget("100")
        old = self.entry(budget, "reservation", "80", "reserve")
        args = {"organization": self.org, "actor": self.finance, "entry": old, "reason": "Replace estimate with bill",
                "kind": "incurred", "amount": "110", "date": DAY, "description": "Actual cost", "idempotency_key": "replacement"}
        with self.assertRaises(ValidationError):
            replace_budget_entry(**args)
        old.refresh_from_db()
        self.assertIsNone(old.voided_at)
        replacement = replace_budget_entry(**(args | {"amount": "70"}))
        self.assertEqual(budget.available, Decimal("30"))
        self.assertEqual(replacement.replaces_id, old.pk)
        self.assertEqual(replace_budget_entry(**(args | {"amount": "70"})).pk, replacement.pk)
        self.assertEqual(BudgetEntry.objects.count(), 2)
        void_budget_entry(organization=self.org, actor=self.finance, entry=replacement, reason="Cancelled")
        void_budget_entry(organization=self.org, actor=self.finance, entry=replacement, reason="Cancelled")
        self.assertEqual(budget.available, Decimal("100"))

    def test_proposal_freezes_on_submit_and_review_precedes_approval(self):
        obj = self.decision()
        obj = transition_decision(organization=self.org, actor=self.finance, decision=obj, action="submit", expected_version=obj.version)
        with self.assertRaises(ValidationError):
            update_decision(organization=self.org, actor=self.finance, decision=obj, expected_version=obj.version, amount="2")
        with self.assertRaises(ValidationError):
            transition_decision(organization=self.org, actor=self.director, decision=obj, action="approve", expected_version=obj.version, notes="Too early")
        obj = transition_decision(organization=self.org, actor=self.finance, decision=obj, action="review", expected_version=obj.version, notes="Evidence and scope checked")
        obj = transition_decision(organization=self.org, actor=self.director, decision=obj, action="approve", expected_version=obj.version, notes="Approved planning pilot")
        obj = transition_decision(organization=self.org, actor=self.director, decision=obj, action="activate", expected_version=obj.version, notes="Manual execution responsibility confirmed")
        obj = transition_decision(organization=self.org, actor=self.director, decision=obj, action="complete", expected_version=obj.version, notes="Pilot results reviewed")
        self.assertEqual(obj.status, "completed")
        self.assertFalse(Invoice.objects.exists())
        self.assertFalse(Journal.objects.exists())

    def test_self_approval_needs_explicit_recorded_exception_and_finance_cannot_approve(self):
        obj = create_budget(organization=self.org, actor=self.owner, title="Own proposal", year=2026, month=9, channel="seo", amount="100")
        obj = transition_budget(organization=self.org, actor=self.owner, budget=obj, action="submit", expected_version=obj.version)
        with self.assertRaises(PermissionDenied):
            transition_budget(organization=self.org, actor=self.finance, budget=obj, action="approve", expected_version=obj.version, notes="Not allowed")
        with self.assertRaises(ValidationError):
            transition_budget(organization=self.org, actor=self.owner, budget=obj, action="approve", expected_version=obj.version, notes="Missing exception")
        obj = transition_budget(organization=self.org, actor=self.owner, budget=obj, action="approve", expected_version=obj.version,
                                notes="Approved envelope only", self_approval_reason="Single owner; manual independent follow-up recorded")
        event = AuditEvent.objects.filter(action="director.budget.approve", object_id=str(obj.pk)).get()
        self.assertIn("Single owner", event.detail["self_approval_reason"])

    def test_review_and_reject_role_denials_leave_decision_and_audit_unchanged(self):
        obj = self.decision()
        obj = transition_decision(organization=self.org, actor=self.finance, decision=obj,
                                  action="submit", expected_version=obj.version)
        before = Decision.objects.filter(pk=obj.pk).values().get()
        audit_ids = list(AuditEvent.objects.values_list("pk", flat=True))
        denied = (("review", self.director), ("review", self.marketing), ("review", self.auditor),
                  ("reject", self.finance), ("reject", self.marketing), ("reject", self.auditor))
        for action, actor in denied:
            with self.subTest(action=action, actor=actor.username):
                with self.assertRaises(PermissionDenied):
                    transition_decision(organization=self.org, actor=actor, decision=obj, action=action,
                                        expected_version=obj.version, notes="Attempt outside assigned role")
                self.assertEqual(Decision.objects.filter(pk=obj.pk).values().get(), before)
                self.assertEqual(list(AuditEvent.objects.values_list("pk", flat=True)), audit_ids)

    def test_authorized_review_and_rejection_roles_still_work(self):
        for reviewer in (self.owner, self.finance):
            for rejector in (self.owner, self.director):
                with self.subTest(reviewer=reviewer.username, rejector=rejector.username):
                    obj = self.decision()
                    obj = transition_decision(organization=self.org, actor=self.director, decision=obj,
                                              action="submit", expected_version=obj.version)
                    obj = transition_decision(organization=self.org, actor=reviewer, decision=obj,
                                              action="review", expected_version=obj.version, notes="Evidence reviewed")
                    self.assertEqual(obj.reviewed_by, reviewer)
                    obj = transition_decision(organization=self.org, actor=rejector, decision=obj,
                                              action="reject", expected_version=obj.version, notes="Proposal declined")
                    self.assertEqual(obj.status, "rejected")
                    self.assertEqual(obj.version, 4)
                    event = AuditEvent.objects.get(action="director.decision.reject", object_id=str(obj.pk))
                    self.assertEqual(event.actor, rejector)

    def test_unknown_decision_action_is_validation_error_without_state_or_audit_mutation(self):
        obj = self.decision()
        before = Decision.objects.filter(pk=obj.pk).values().get()
        audit_count = AuditEvent.objects.count()
        for action in ("unknown", "", None, 7, ["review"], {"action": "reject"}):
            with self.subTest(action=action), self.assertRaises(ValidationError):
                transition_decision(organization=self.org, actor=self.owner, decision=obj, action=action,
                                    expected_version=obj.version, notes="Invalid action")
            self.assertEqual(Decision.objects.filter(pk=obj.pk).values().get(), before)
            self.assertEqual(AuditEvent.objects.count(), audit_count)

    def test_hidden_decision_actions_cannot_be_forged_through_http(self):
        obj = self.decision()
        obj = transition_decision(organization=self.org, actor=self.finance, decision=obj,
                                  action="submit", expected_version=obj.version)
        audit_count = AuditEvent.objects.count()
        for actor, action in ((self.director, "review"), (self.finance, "reject")):
            with self.subTest(actor=actor.username, action=action):
                self.client.force_login(actor)
                response = self.client.post(reverse("director:decision_action", args=[obj.pk, action]),
                                            {"expected_version": obj.version, "notes": "Forged hidden action"})
                self.assertEqual(response.status_code, 403)
                obj.refresh_from_db()
                self.assertEqual(obj.status, "submitted")
                self.assertEqual(obj.version, 2)
                self.assertIsNone(obj.reviewed_by)
                self.assertEqual(AuditEvent.objects.count(), audit_count)

    def test_version_scope_and_current_actor_status_are_checked(self):
        obj = self.decision()
        changed = update_decision(organization=self.org, actor=self.finance, decision=obj, expected_version=1, title="Updated")
        with self.assertRaises(ValidationError):
            update_decision(organization=self.org, actor=self.finance, decision=obj, expected_version=1, title="Stale update")
        Membership.objects.create(organization=self.other, user=self.finance, role="finance")
        with self.assertRaises(ValidationError):
            update_decision(organization=self.other, actor=self.finance, decision=obj, expected_version=changed.version, title="Cross company")
        get_user_model().objects.filter(pk=self.finance.pk).update(is_active=False)
        with self.assertRaises(PermissionDenied):
            update_decision(organization=self.org, actor=self.finance, decision=obj, expected_version=changed.version, title="Disabled cached actor")
        with self.assertRaises(PermissionDenied):
            create_budget(organization=self.org, actor=None, title="No actor", year=2026, month=9, channel="seo", amount="1")

    def test_cash_plan_is_always_assumed_and_line_corrections_preserve_history(self):
        plan = create_cash_plan(organization=self.org, actor=self.finance, title="Cash assumptions", as_of=DAY,
                                opening_balance="100", reserve="20", unknown_obligations="Supplier timing unconfirmed")
        line = add_cash_line(organization=self.org, actor=self.finance, plan=plan, direction="outflow", amount="30",
                             date=DAY, category="supplier", description="Expected payment")
        replacement = replace_cash_line(organization=self.org, actor=self.finance, line=line, reason="Updated date", direction="outflow",
                                        amount="30", date=date(2026, 9, 9), category="supplier", description="Expected payment")
        line.refresh_from_db(); plan.refresh_from_db()
        self.assertEqual(line.status, "void")
        self.assertEqual(replacement.replaces_id, line.pk)
        self.assertEqual(plan.basis, "assumed")
        with self.assertRaises(ValidationError):
            update_cash_plan(organization=self.org, actor=self.finance, plan=plan, expected_version=plan.version, basis="verified")
        with self.assertRaises(ValidationError):
            update_cash_plan(organization=self.org, actor=self.finance, plan=plan, expected_version=plan.version, as_of=date(2026, 10, 1))
        void_cash_line(organization=self.org, actor=self.finance, line=replacement, reason="Removed assumption")
        self.assertEqual(CashPlanLine.objects.filter(status="planned").count(), 0)

    def test_objective_completion_and_target_revision_are_audited(self):
        obj = create_objective(organization=self.org, actor=self.director, title="Measure actual contribution", metric_key="contribution",
                               target=None, period_start=DAY, period_end=date(2026, 9, 30), owner_name="Finance")
        with self.assertRaises(ValidationError):
            update_objective(organization=self.org, actor=self.director, objective=obj, expected_version=obj.version, status="completed")
        obj = update_objective(organization=self.org, actor=self.director, objective=obj, expected_version=obj.version, target="100", progress_notes="Target established", status="active")
        event = AuditEvent.objects.filter(action="director.objective.updated", object_id=str(obj.pk)).get()
        self.assertIsNone(event.detail["before"]["target"])
        self.assertEqual(event.detail["after"]["target"], "100.00")

    def test_marketing_nullable_reported_records_replay_and_correction(self):
        data = {"organization": self.org, "actor": self.marketing, "date": DAY, "channel": "google_ads", "spend": "0",
                "leads": None, "paid_orders": None, "reported_value": None, "source": "Manual platform export", "reference": "ROW1"}
        first = create_marketing_observation(**data)
        self.assertEqual(create_marketing_observation(**data).pk, first.pk)
        self.assertIsNone(first.leads)
        self.assertEqual(first.spend, Decimal("0"))
        with self.assertRaises(ValidationError):
            create_marketing_observation(**(data | {"spend": "1"}))
        second = replace_marketing_observation(**(data | {"observation": first, "reason": "Correct source spend", "spend": "1"}))
        first.refresh_from_db()
        self.assertEqual(first.status, "void")
        self.assertEqual(second.replaces_id, first.pk)
        self.assertEqual(MarketingObservation.objects.filter(status="active").count(), 1)
        void_marketing_observation(organization=self.org, actor=self.marketing, observation=second, reason="Source withdrawn")
        with self.assertRaises(PermissionDenied):
            create_marketing_observation(**(data | {"actor": self.auditor}))
        with self.assertRaises(PermissionDenied):
            create_budget(organization=self.org, actor=self.marketing, title="Unauthorized", year=2026, month=9, channel="seo", amount="1")
        self.assertFalse(Journal.objects.exists())

    def test_invalid_floats_nonfinite_amounts_and_uncontrolled_model_writes_are_rejected(self):
        for amount in (1.5, "NaN", "1.001", "-1"):
            with self.subTest(amount=amount), self.assertRaises(ValidationError):
                create_budget(organization=self.org, actor=self.finance, title="Invalid", year=2026, month=9, channel="seo", amount=amount)
        with self.assertRaises(ValidationError):
            Decision.objects.create(organization=self.org, created_by=self.owner, title="Bypass service")

    def test_scenario_results_are_computed_not_accepted_from_client(self):
        computed = {"formula_version": "synthetic-v1", "basis": "assumed", "proposed": {"revenue": "200.00"}}
        with patch("director.calculators.price_cost_scenario", return_value=computed) as calculator:
            obj = save_scenario(organization=self.org, actor=self.director, title="What if", kind="price_cost",
                                inputs={"quantity": 2, "current_price": Decimal("100"), "current_cost": Decimal("70")})
        calculator.assert_called_once()
        self.assertEqual(obj.results, computed)
        self.assertEqual(obj.inputs["current_price"], "100")
        with self.assertRaises(ValidationError):
            save_scenario(organization=self.org, actor=self.director, title="Fake", kind="price_cost", inputs={"results": {"revenue": "999"}})
