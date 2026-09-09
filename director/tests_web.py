"""HTTP contracts using synthetic data, real forms/services, and no external AI."""
import csv
import io
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from core.models import Membership, Organization
from finance.models import BankTransaction, Invoice, Journal
from webapp.test_documents import pdf_text_commands
from . import services
from .models import (Budget, BudgetEntry, CashPlan, CashPlanLine, Decision,
                     DirectorConversation, DirectorMessage, MarketingObservation,
                     Objective, Scenario)


DAY = date(2026, 9, 8)


@override_settings(DIRECTOR_AI_PRIVATE_AGGREGATES_ENABLED=False,
                   DIRECTOR_OPENROUTER_API_KEY="")
class DirectorHTTPTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(name="Synthetic Director Company")
        cls.other = Organization.objects.create(name="Another Synthetic Company")
        for role in ("owner", "director", "finance", "marketing", "auditor"):
            user = get_user_model().objects.create_user(username=f"http-director-{role}")
            Membership.objects.create(organization=cls.org, user=user, role=role)
            setattr(cls, role, user)
        cls.outsider = get_user_model().objects.create_user(username="http-director-outsider")
        Membership.objects.create(organization=cls.other, user=cls.outsider, role="owner")
        cls.objective = services.create_objective(
            organization=cls.org, actor=cls.finance, **cls.objective_data())
        cls.decision = services.create_decision(
            organization=cls.org, actor=cls.finance, **cls.decision_data())
        cls.budget = cls.make_budget(cls.org, cls.finance, cls.director)
        cls.plan = services.create_cash_plan(
            organization=cls.org, actor=cls.finance, **cls.cash_data())
        cls.scenario = services.save_scenario(
            organization=cls.org, actor=cls.finance, title="Synthetic price scenario", kind="price_cost",
            inputs={"quantity": 10, "current_price": "500000", "current_cost": "440000"})
        cls.foreign_objective = services.create_objective(
            organization=cls.other, actor=cls.outsider,
            **(cls.objective_data() | {"title": "Foreign confidential objective"}))
        cls.foreign_decision = services.create_decision(
            organization=cls.other, actor=cls.outsider,
            **(cls.decision_data() | {"title": "Foreign confidential decision"}))
        cls.foreign_budget = cls.make_budget(cls.other, cls.outsider, cls.outsider)
        cls.foreign_plan = services.create_cash_plan(
            organization=cls.other, actor=cls.outsider, **cls.cash_data())
        cls.foreign_scenario = services.save_scenario(
            organization=cls.other, actor=cls.outsider, title="Foreign scenario", kind="price_cost",
            inputs={"quantity": 1, "current_price": "100", "current_cost": "50"})

    @staticmethod
    def objective_data():
        return {"title": "Measure paid orders", "metric_key": "paid_orders", "target": "30",
                "period_start": date(2026, 8, 1), "period_end": date(2026, 9, 30),
                "owner_name": "Operations", "status": "active", "progress_notes": "Target in orders"}

    @staticmethod
    def decision_data():
        return {"title": "Measurement pilot", "problem": "Conversion mapping is incomplete",
                "evidence": "Synthetic source review", "option": "Run a bounded pilot",
                "risks": "Reported conversions may overlap", "owner_name": "Marketing lead",
                "due_date": date(2026, 9, 20), "review_date": date(2026, 9, 30), "amount": "1000000"}

    @staticmethod
    def cash_data():
        return {"title": "Synthetic thirteen week plan", "as_of": date(2026, 8, 1),
                "opening_balance": "10000000", "reserve": "2000000",
                "unknown_obligations": "Future obligations remain unconfirmed"}

    @staticmethod
    def marketing_data():
        return {"date": DAY, "channel": "meta_ads", "spend": "", "leads": "12",
                "paid_orders": "", "reported_value": "", "source": "Synthetic platform export",
                "reference": "SYNTHETIC-CAMPAIGN-1", "basis": "reported"}

    @classmethod
    def make_budget(cls, organization, maker, approver):
        obj = services.create_budget(organization=organization, actor=maker, title="Synthetic campaign",
                                     year=2026, month=9, channel="meta_ads", amount="1000000")
        obj = services.transition_budget(organization=organization, actor=maker, budget=obj, action="submit",
                                         expected_version=obj.version)
        return services.transition_budget(
            organization=organization, actor=approver, budget=obj, action="approve",
            expected_version=obj.version, notes="Planning envelope checked",
            self_approval_reason="Owner records a documented exception" if maker == approver else "")

    def setUp(self):
        self.login(self.owner)
        transport = patch("director.ai.urlopen", side_effect=AssertionError("HTTP tests must not call AI"))
        self.transport = transport.start()
        self.addCleanup(transport.stop)

    def login(self, user, organization=None, client=None):
        client = client or self.client
        client.force_login(user)
        session = client.session
        session["organization_id"] = (organization or self.org).pk
        session.save()

    def assert_redirect(self, response, name, args=None):
        self.assertRedirects(response, reverse(name, args=args), fetch_redirect_response=False)

    def action(self, decision, action, **extra):
        decision.refresh_from_db()
        return self.client.post(reverse("director:decision_action", args=[decision.pk, action]),
                                {"expected_version": decision.version, "notes": "Checked synthetic evidence", **extra})

    def test_owner_pages_render_with_real_templates_and_scoped_data(self):
        pages = [(name, None) for name in (
            "overview", "growth", "readiness", "objectives", "objective_create", "decisions",
            "decision_create", "budgets", "budget_create", "cash", "cash_create", "marketing",
            "marketing_create", "marketing_import", "scenarios", "scenario_create", "assistant", "ai_policy", "reports")]
        pages += [("objective_edit", [self.objective.pk]), ("decision_detail", [self.decision.pk]),
                  ("decision_edit", [self.decision.pk]), ("budget_detail", [self.budget.pk]),
                  ("budget_entry", [self.budget.pk]), ("cash_detail", [self.plan.pk]),
                  ("cash_edit", [self.plan.pk]), ("cash_line", [self.plan.pk]),
                  ("scenario_detail", [self.scenario.pk])]
        for name, args in pages:
            with self.subTest(page=name):
                response = self.client.get(reverse(f"director:{name}", args=args), {"year": 2026})
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, "Foreign confidential")
        self.transport.assert_not_called()

    def test_anonymous_redirect_and_unowned_session_company_are_denied(self):
        url = reverse("director:objectives")
        self.assertRedirects(Client().get(url), reverse("login") + "?next=" + url,
                             fetch_redirect_response=False)
        session = self.client.session
        session["organization_id"] = self.other.pk
        session.save()
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_marketing_can_record_its_data_but_cannot_open_private_director_or_finance_pages(self):
        self.login(self.marketing)
        self.assert_redirect(self.client.get(reverse("director:overview")), "director:marketing")
        for name in ("marketing", "marketing_create"):
            self.assertEqual(self.client.get(reverse(f"director:{name}")).status_code, 200)
        for name in ("growth", "readiness", "objectives", "decisions", "budgets", "cash", "scenarios",
                     "assistant", "reports", "export", "report_pdf", "snapshot_api", "ai_policy"):
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(f"director:{name}")).status_code, 403)
        for name in ("dashboard", "sales", "bills", "bank", "reports", "imports_overview", "tax_workspace"):
            with self.subTest(finance_page=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 403)
        result = self.client.post(reverse("director:marketing_create"), self.marketing_data())
        self.assert_redirect(result, "director:marketing")
        self.assertEqual(MarketingObservation.objects.get().created_by, self.marketing)
        self.assertEqual(self.client.post(reverse("director:budget_create"), {}).status_code, 403)

    def test_auditor_can_read_but_cannot_write_or_configure_ai(self):
        self.login(self.auditor)
        for name in ("overview", "objectives", "marketing", "budgets", "cash", "scenarios", "reports"):
            self.assertEqual(self.client.get(reverse(f"director:{name}")).status_code, 200, name)
        for name in ("objective_create", "decision_create", "budget_create", "cash_create",
                     "marketing_create", "scenario_create", "ai_policy"):
            with self.subTest(write=name):
                self.assertEqual(self.client.post(reverse(f"director:{name}"), {}).status_code, 403)
        self.assertEqual(self.action(self.decision, "submit").status_code, 403)
        self.assertEqual(self.client.post(reverse("director:assistant"),
                                          {"question": "Read this", "year": 2026}).status_code, 403)
        self.assertFalse(DirectorMessage.objects.exists())

    def test_create_and_complete_objective_scopes_ignored_client_identity_and_checks_version(self):
        response = self.client.post(reverse("director:objective_create"), self.objective_data() | {
            "title": "New objective", "target": "", "organization": self.other.pk,
            "created_by": self.outsider.pk})
        self.assert_redirect(response, "director:objectives")
        obj = Objective.objects.get(title="New objective")
        self.assertEqual(obj.organization, self.org)
        self.assertEqual(obj.created_by, self.owner)
        self.assertIsNone(obj.target)
        data = self.objective_data() | {"title": obj.title, "status": "completed", "progress_notes": "Reviewed results", "expected_version": obj.version}
        url = reverse("director:objective_edit", args=[obj.pk])
        self.assert_redirect(self.client.post(url, data), "director:objectives")
        obj.refresh_from_db()
        self.assertEqual(obj.status, "completed")
        stale = self.client.post(url, data | {"title": "Stale overwrite"})
        self.assertEqual(stale.status_code, 200)
        self.assertTrue(stale.context["form"].errors)
        obj.refresh_from_db()
        self.assertEqual(obj.title, "New objective")

    def test_edit_get_preserves_stored_periods_and_cash_reserve(self):
        with patch("director.views.timezone.localdate", return_value=DAY):
            objective = self.client.get(reverse("director:objective_edit", args=[self.objective.pk]))
            self.assertEqual(objective.context["form"]["period_start"].value(), self.objective.period_start)
            draft = services.create_budget(organization=self.org, actor=self.finance, title="Old draft", year=2025,
                                           month=3, channel="seo", amount="100")
            budget = self.client.get(reverse("director:budget_edit", args=[draft.pk]))
            self.assertEqual(budget.context["form"]["year"].value(), 2025)
            self.assertEqual(budget.context["form"]["month"].value(), 3)
            cash = self.client.get(reverse("director:cash_edit", args=[self.plan.pk]))
            self.assertEqual(cash.context["form"]["as_of"].value(), self.plan.as_of)
            self.assertEqual(cash.context["form"]["reserve"].value(), Decimal("2000000"))

    def test_decision_http_workflow_freezes_proposal_and_requires_separate_approval(self):
        self.login(self.finance)
        result = self.client.post(reverse("director:decision_create"), self.decision_data() | {"title": "HTTP decision"})
        decision = Decision.objects.get(title="HTTP decision")
        self.assert_redirect(result, "director:decision_detail", [decision.pk])
        self.assert_redirect(self.action(decision, "submit"), "director:decision_detail", [decision.pk])
        decision.refresh_from_db()
        self.assertEqual(decision.status, "submitted")
        self.assertEqual(self.client.get(reverse("director:decision_detail", args=[decision.pk])).status_code, 200)
        tampered = self.client.post(reverse("director:decision_edit", args=[decision.pk]),
                                     self.decision_data() | {"title": "Changed after submission", "expected_version": decision.version})
        self.assertEqual(tampered.status_code, 200)
        self.assertTrue(tampered.context["form"].errors)
        decision.refresh_from_db()
        self.assertEqual(decision.title, "HTTP decision")
        self.assert_redirect(self.action(decision, "review"), "director:decision_detail", [decision.pk])
        self.assertEqual(self.action(decision, "approve").status_code, 403)
        self.login(self.director)
        for action, status in (("approve", "approved"), ("activate", "active"), ("complete", "completed")):
            self.assert_redirect(self.action(decision, action), "director:decision_detail", [decision.pk])
            decision.refresh_from_db()
            self.assertEqual(decision.status, status)
            self.assertEqual(self.client.get(reverse("director:decision_detail", args=[decision.pk])).status_code, 200)
        self.assertFalse(Journal.objects.exists())
        self.assertFalse(BankTransaction.objects.exists())

    def test_invalid_or_stale_decision_actions_redirect_without_changing_state(self):
        for action, version in (("approve", 1), ("submit", 99), ("unknown-action", 1)):
            result = self.client.post(reverse("director:decision_action", args=[self.decision.pk, action]),
                                      {"expected_version": version, "notes": "Synthetic review"})
            self.assert_redirect(result, "director:decision_detail", [self.decision.pk])
            self.decision.refresh_from_db()
            self.assertEqual(self.decision.status, "draft")
        self.assertEqual(self.client.get(reverse("director:decision_action", args=[self.decision.pk, "submit"])).status_code, 405)

    def test_csrf_token_is_required_for_creation_and_state_transition(self):
        secure = Client(enforce_csrf_checks=True)
        self.login(self.finance, client=secure)
        create = reverse("director:decision_create")
        self.assertEqual(secure.post(create, self.decision_data()).status_code, 403)
        self.assertEqual(secure.get(create).status_code, 200)
        token = secure.cookies["csrftoken"].value
        result = secure.post(create, self.decision_data() | {"title": "CSRF protected", "csrfmiddlewaretoken": token})
        obj = Decision.objects.get(title="CSRF protected")
        self.assert_redirect(result, "director:decision_detail", [obj.pk])
        action = reverse("director:decision_action", args=[obj.pk, "submit"])
        data = {"expected_version": obj.version, "notes": "Review ready"}
        self.assertEqual(secure.post(action, data).status_code, 403)
        self.assert_redirect(secure.post(action, data | {"csrfmiddlewaretoken": token}), "director:decision_detail", [obj.pk])
        obj.refresh_from_db()
        self.assertEqual(obj.status, "submitted")

    def test_budget_creation_approval_and_payment_observation_do_not_double_consume(self):
        self.login(self.finance)
        result = self.client.post(reverse("director:budget_create"), {
            "title": "HTTP budget", "year": 2026, "month": 9, "channel": "google_ads", "amount": "1000"})
        budget = Budget.objects.get(title="HTTP budget")
        self.assert_redirect(result, "director:budget_detail", [budget.pk])
        data = {"expected_version": budget.version, "notes": "Envelope proposed"}
        self.assert_redirect(self.client.post(reverse("director:budget_action", args=[budget.pk, "submit"]), data),
                             "director:budget_detail", [budget.pk])
        self.login(self.director)
        budget.refresh_from_db()
        self.assert_redirect(self.client.post(reverse("director:budget_action", args=[budget.pk, "approve"]),
                                              data | {"expected_version": budget.version}), "director:budget_detail", [budget.pk])
        entry_url = reverse("director:budget_entry", args=[budget.pk])
        common = {"date": DAY, "description": "Synthetic cost", "reference": "test"}
        for kind, amount in (("incurred", "600"), ("payment", "500")):
            response = self.client.post(entry_url, common | {"kind": kind, "amount": amount, "idempotency_key": kind})
            self.assert_redirect(response, "director:budget_detail", [budget.pk])
        over = self.client.post(entry_url, common | {"kind": "reservation", "amount": "401", "idempotency_key": "over"})
        self.assertEqual(over.status_code, 200)
        self.assertTrue(over.context["form"].errors)
        self.assertEqual(budget.consumed, Decimal("600"))
        self.assertEqual(budget.available, Decimal("400"))
        self.assertEqual(budget.payments, Decimal("500"))
        self.assertFalse(Journal.objects.exists())

    def test_budget_replacement_form_uses_new_record_and_preserves_old_economics(self):
        old = services.add_budget_entry(organization=self.org, actor=self.finance, budget=self.budget,
                                        kind="open_commitment", amount="800000", date=DAY,
                                        description="Old commitment", idempotency_key="old")
        url = reverse("director:budget_entry_replace", args=[self.budget.pk, old.pk])
        get = self.client.get(url)
        self.assertIsNone(get.context["form"].instance.pk)
        self.assertEqual(get.context["form"]["amount"].value(), Decimal("800000"))
        data = {"kind": "incurred", "amount": "700000", "date": DAY, "description": "Final cost",
                "reference": "bill-test", "reason": "Invoice replaces estimate", "idempotency_key": "new"}
        result = self.client.post(url, data)
        self.assert_redirect(result, "director:budget_detail", [self.budget.pk])
        old.refresh_from_db()
        self.assertIsNotNone(old.voided_at)
        self.assertEqual(old.amount, Decimal("800000"))
        replacement = BudgetEntry.objects.get(replaces=old)
        self.assertEqual(replacement.amount, Decimal("700000"))
        self.assertEqual(self.budget.consumed, Decimal("700000"))
        self.assert_redirect(self.client.post(url, data), "director:budget_detail", [self.budget.pk])
        self.assertEqual(BudgetEntry.objects.filter(budget=self.budget).count(), 2)

    def test_failed_budget_replacement_keeps_original_active(self):
        old = services.add_budget_entry(organization=self.org, actor=self.finance, budget=self.budget,
                                        kind="reservation", amount="800000", date=DAY,
                                        description="Reservation", idempotency_key="rollback-old")
        result = self.client.post(reverse("director:budget_entry_replace", args=[self.budget.pk, old.pk]), {
            "kind": "incurred", "amount": "1000001", "date": DAY, "description": "Too large",
            "reason": "Incorrect adjustment", "idempotency_key": "rollback-new"})
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.context["form"].errors)
        old.refresh_from_db()
        self.assertIsNone(old.voided_at)
        self.assertFalse(BudgetEntry.objects.filter(replaces=old).exists())

    def test_cash_plan_line_replacement_and_void_are_explicit_planning_actions(self):
        result = self.client.post(reverse("director:cash_create"), self.cash_data() | {"title": "HTTP cash plan"})
        plan = CashPlan.objects.get(title="HTTP cash plan")
        self.assert_redirect(result, "director:cash_detail", [plan.pk])
        self.assertEqual(plan.basis, "assumed")
        data = {"direction": "outflow", "amount": "500000", "date": DAY,
                "category": "supplier", "description": "Expected supplier payment", "source_reference": "SYNTHETIC-1",
                "evidence": "Planning estimate"}
        self.assert_redirect(self.client.post(reverse("director:cash_line", args=[plan.pk]), data), "director:cash_detail", [plan.pk])
        old = CashPlanLine.objects.get(plan=plan)
        url = reverse("director:cash_line_replace", args=[plan.pk, old.pk])
        self.assertIsNone(self.client.get(url).context["form"].instance.pk)
        self.assert_redirect(self.client.post(url, data | {"amount": "450000", "reason": "Updated estimate"}),
                             "director:cash_detail", [plan.pk])
        old.refresh_from_db()
        self.assertEqual(old.status, "void")
        self.assertEqual(old.amount, Decimal("500000"))
        current = CashPlanLine.objects.get(replaces=old)
        void_url = reverse("director:void_record", args=["cash", current.pk])
        self.assertEqual(self.client.get(void_url).status_code, 200)
        current.refresh_from_db()
        self.assertEqual(current.status, "planned")
        self.assert_redirect(self.client.post(void_url, {"reason": "Plan cancelled"}), "director:cash_detail", [plan.pk])
        current.refresh_from_db()
        self.assertEqual(current.status, "void")
        self.assertFalse(BankTransaction.objects.exists())

    def test_marketing_blank_means_unknown_and_correction_retains_source_history(self):
        self.login(self.marketing)
        data = self.marketing_data()
        for _ in range(2):
            self.assert_redirect(self.client.post(reverse("director:marketing_create"), data), "director:marketing")
        old = MarketingObservation.objects.get()
        self.assertIsNone(old.spend)
        self.assertIsNone(old.paid_orders)
        self.assertEqual(old.leads, 12)
        url = reverse("director:marketing_replace", args=[old.pk])
        form = self.client.get(url).context["form"]
        self.assertIsNone(form.instance.pk)
        self.assertEqual(form["leads"].value(), 12)
        correction = data | {"spend": "0", "leads": "14", "reason": "Correct source count"}
        self.assert_redirect(self.client.post(url, correction), "director:marketing")
        old.refresh_from_db()
        self.assertEqual(old.status, "void")
        self.assertEqual(old.leads, 12)
        corrected = MarketingObservation.objects.get(replaces=old)
        self.assertEqual(corrected.spend, Decimal("0"))
        self.assertEqual(corrected.leads, 14)
        self.assertFalse(Invoice.objects.exists())
        self.assertFalse(Journal.objects.exists())

    def test_scenario_http_persists_calculated_result_and_ignores_client_results(self):
        result = self.client.post(reverse("director:scenario_create"), {
            "title": "HTTP scenario", "quantity": 10, "current_price": "500000", "current_cost": "440000",
            "variable_cost": "0", "new_price": "510000", "new_cost": "450000", "new_quantity": "12",
            "additional_marketing_spend": "100000", "results": '{"profit":999999999}',
            "organization": self.other.pk})
        scenario = Scenario.objects.get(title="HTTP scenario")
        self.assert_redirect(result, "director:scenario_detail", [scenario.pk])
        self.assertEqual(scenario.organization, self.org)
        self.assertEqual(Decimal(scenario.results["baseline"]["net_contribution"]), Decimal("600000"))
        self.assertEqual(Decimal(scenario.results["proposed"]["net_contribution"]), Decimal("620000"))
        self.assertNotIn("profit", scenario.results)
        self.assertFalse(Invoice.objects.exists())

    def test_cross_company_object_routes_return_404_for_get_and_post(self):
        foreign_marketing = services.create_marketing_observation(
            organization=self.other, actor=self.outsider, date=DAY, channel="seo", leads=7,
            source="Foreign private report", reference="FOREIGN-TEST", basis="reported")
        routes = [("objective_edit", [self.foreign_objective.pk]),
                  ("decision_detail", [self.foreign_decision.pk]), ("decision_edit", [self.foreign_decision.pk]),
                  ("budget_detail", [self.foreign_budget.pk]), ("budget_edit", [self.foreign_budget.pk]),
                  ("budget_entry", [self.foreign_budget.pk]), ("cash_detail", [self.foreign_plan.pk]),
                  ("cash_edit", [self.foreign_plan.pk]), ("cash_line", [self.foreign_plan.pk]),
                  ("scenario_detail", [self.foreign_scenario.pk]),
                  ("marketing_replace", [foreign_marketing.pk]),
                  ("void_record", ["marketing", foreign_marketing.pk])]
        for name, args in routes:
            with self.subTest(route=name):
                url = reverse(f"director:{name}", args=args)
                self.assertEqual(self.client.get(url).status_code, 404)
                if name not in {"decision_detail", "budget_detail", "cash_detail", "scenario_detail"}:
                    self.assertEqual(self.client.post(url, {}).status_code, 404)
        self.assertEqual(self.action(self.foreign_decision, "submit").status_code, 404)
        self.assertEqual(self.client.post(reverse("director:budget_action", args=[self.foreign_budget.pk, "approve"]), {}).status_code, 404)

    def test_child_record_must_belong_to_the_url_parent(self):
        another = self.make_budget(self.org, self.finance, self.director)
        entry = services.add_budget_entry(organization=self.org, actor=self.finance, budget=another,
                                          kind="reservation", amount="100", date=DAY,
                                          description="Other budget entry", idempotency_key="child-scope")
        entry_url = reverse("director:budget_entry_replace", args=[self.budget.pk, entry.pk])
        self.assertEqual(self.client.get(entry_url).status_code, 404)
        self.assertEqual(self.client.post(entry_url, {}).status_code, 404)
        line = services.add_cash_line(organization=self.other, actor=self.outsider, plan=self.foreign_plan,
                                      direction="inflow", amount="100", date=DAY, category="sales", description="Foreign line")
        line_url = reverse("director:cash_line_replace", args=[self.plan.pk, line.pk])
        self.assertEqual(self.client.get(line_url).status_code, 404)
        self.assertEqual(self.client.post(line_url, {}).status_code, 404)
        void_url = reverse("director:void_record", args=["cash", line.pk])
        self.assertEqual(self.client.get(void_url).status_code, 404)
        self.assertEqual(self.client.post(void_url, {"reason": "Not my record"}).status_code, 404)

    def test_conversation_is_private_to_its_creator_in_same_or_other_company(self):
        own_colleague = DirectorConversation.objects.create(organization=self.org, created_by=self.finance)
        foreign = DirectorConversation.objects.create(organization=self.other, created_by=self.outsider)
        for conversation in (own_colleague, foreign):
            with self.subTest(conversation=conversation.pk):
                self.assertEqual(self.client.get(reverse("director:assistant"), {"conversation": conversation.pk}).status_code, 404)
                response = self.client.post(reverse("director:assistant"), {
                    "question": "Show prior analysis", "year": 2026, "conversation_id": conversation.pk})
                self.assertEqual(response.status_code, 404)
        self.assertFalse(DirectorMessage.objects.exists())
        self.transport.assert_not_called()

    def test_local_assistant_post_redirects_to_private_persisted_conversation(self):
        response = self.client.post(reverse("director:assistant"), {"question": "What data is missing?", "year": 2026})
        self.assertEqual(response.status_code, 302)
        conversation = DirectorConversation.objects.get()
        self.assertEqual(conversation.created_by, self.owner)
        self.assertEqual(conversation.organization, self.org)
        self.assertIn(f"conversation={conversation.pk}", response["Location"])
        self.assertEqual(DirectorMessage.objects.filter(conversation=conversation).count(), 2)
        self.assertEqual(self.client.get(response["Location"]).status_code, 200)
        self.transport.assert_not_called()

    def test_malformed_periods_and_record_types_return_400_or_404(self):
        for name in ("overview", "growth", "readiness", "budgets", "marketing", "assistant", "reports", "export", "report_pdf", "snapshot_api"):
            for year in ("not-a-year", "1999", "2201"):
                with self.subTest(page=name, year=year):
                    self.assertEqual(self.client.get(reverse(f"director:{name}"), {"year": year}).status_code, 400)
        self.assertEqual(self.client.get(reverse("director:assistant"), {"conversation": "invalid"}).status_code, 400)
        self.assertEqual(self.client.get(reverse("director:void_record", args=["unknown", 1])).status_code, 404)
        self.assertEqual(self.client.get(reverse("director:decisions"), {"page": "nonsense"}).status_code, 200)

    def test_invalid_amount_date_and_completion_inputs_render_form_errors(self):
        cases = [("budget_create", {"title": "Invalid", "year": 2026, "month": 13, "channel": "meta_ads", "amount": "100"}),
                 ("budget_create", {"title": "Invalid", "year": 2026, "month": 9, "channel": "meta_ads", "amount": "NaN"}),
                 ("cash_create", self.cash_data() | {"as_of": "invalid"}),
                 ("cash_create", self.cash_data() | {"as_of": "9999-12-31"}),
                 ("objective_create", self.objective_data() | {"status": "completed", "progress_notes": ""}),
                 ("marketing_create", self.marketing_data() | {"leads": "-1"}),
                 ("marketing_create", self.marketing_data() | {"leads": ""})]
        counts = (Budget.objects.count(), CashPlan.objects.count(), Objective.objects.count(), MarketingObservation.objects.count())
        for name, data in cases:
            with self.subTest(form=name, data=data):
                response = self.client.post(reverse(f"director:{name}"), data)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context["form"].errors)
        self.assertEqual(counts, (Budget.objects.count(), CashPlan.objects.count(), Objective.objects.count(), MarketingObservation.objects.count()))

    def test_export_and_snapshot_are_scoped_read_only_and_unknown_is_not_reported_as_actual(self):
        api = self.client.get(reverse("director:snapshot_api"), {"year": 2026})
        self.assertEqual(api.status_code, 200)
        self.assertEqual(api.json()["organization_id"], self.org.pk)
        facts = {row["fact_id"]: row for row in api.json()["facts"]}
        self.assertIsNone(facts["finance.revenue"]["value"])
        self.assertIsNone(facts["finance.bank_balance"]["value"])
        export = self.client.get(reverse("director:export"), {"year": 2026})
        self.assertEqual(export.status_code, 200)
        self.assertIn("attachment", export["Content-Disposition"])
        self.assertIn("text/csv", export["Content-Type"])
        self.assertNotContains(export, "Foreign confidential")
        self.assertFalse(Journal.objects.exists())

    def test_director_pdf_has_scoped_labels_and_never_promotes_assumed_cash_to_verified_cash(self):
        url = reverse("director:report_pdf")
        for user in (self.owner, self.auditor):
            with self.subTest(role=user.username):
                self.login(user)
                response = self.client.get(url, {"year": 2026})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["Content-Type"], "application/pdf")
                self.assertIn("attachment", response["Content-Disposition"])
                self.assertTrue(response.content.startswith(b"%PDF-"))
                self.assertTrue(response.content.rstrip().endswith(b"%%EOF"))
                self.assertNotIn(b"/JavaScript", response.content)
                text = pdf_text_commands(response.content)
                compact_text = "".join(text.split())
                for label in ("Laporan Direktur 2026", "Laporan manajemen internal", "Belum tersedia",
                              "Saldo kas terverifikasi", "Saldo awal dan rekonsiliasi lengkap belum disahkan",
                              self.org.name, self.objective.title, self.decision.title):
                    # Text may wrap into adjacent Tj commands without a joining space.
                    self.assertTrue("".join(label.split()) in compact_text, f"Missing PDF label: {label}")
                self.assertFalse("Rp10.000.000,00" in text, "Assumed plan opening is not verified cash")
                self.assertFalse("Rp0,00" in text, "Absent ledger/cash facts must remain unavailable")
                self.assertFalse("Foreign confidential" in text, "Foreign records leaked into PDF")
                self.assertFalse(self.other.name in text, "Foreign organization leaked into PDF")
        self.assertEqual(self.client.post(url, {}).status_code, 405)
        self.login(self.marketing)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertFalse(Journal.objects.exists())

    def test_marketing_template_round_trip_upload_duplicate_and_conflict_are_atomic(self):
        self.login(self.marketing)
        template = self.client.get(reverse("director:marketing_template"))
        self.assertEqual(template.status_code, 200)
        self.assertIn("text/csv", template["Content-Type"])
        self.assertIn("attachment", template["Content-Disposition"])
        columns = list(csv.reader(io.StringIO(template.content.decode("utf-8-sig"))))
        self.assertEqual(columns, [["date", "channel", "spend", "leads", "paid_orders", "reported_value", "source", "reference", "basis"]])
        upload_url = reverse("director:marketing_import")
        self.assertEqual(self.client.get(upload_url).status_code, 200)

        def upload(rows):
            body = io.StringIO(newline="")
            writer = csv.DictWriter(body, fieldnames=columns[0])
            writer.writeheader()
            writer.writerows(rows)
            return self.client.post(upload_url, {"file": SimpleUploadedFile(
                "synthetic-marketing.csv", body.getvalue().encode("utf-8-sig"), content_type="text/csv"),
                "organization": self.other.pk, "created_by": self.outsider.pk})

        data = self.marketing_data()
        for _ in range(2):
            self.assert_redirect(upload([data]), "director:marketing")
        item = MarketingObservation.objects.get()
        self.assertEqual(item.organization, self.org)
        self.assertEqual(item.created_by, self.marketing)
        self.assertEqual(item.leads, 12)
        self.assertIsNone(item.spend)
        self.assertIsNone(item.paid_orders)
        result = upload([data | {"reference": "NEW-ROW-BEFORE-CONFLICT"}, data | {"leads": "99"}])
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.context["form"].errors)
        self.assertEqual(MarketingObservation.objects.count(), 1)
        item.refresh_from_db()
        self.assertEqual(item.leads, 12)
        self.assertFalse(Journal.objects.exists())
        self.assertFalse(BankTransaction.objects.exists())
        self.assertFalse(Invoice.objects.exists())

    def test_marketing_upload_rejects_bad_file_at_http_boundary_and_auditor_cannot_import(self):
        url = reverse("director:marketing_import")
        self.login(self.marketing)
        for filename, content in (("wrong.txt", b"date,channel\n"),
                                  ("oversize.csv", b"x" * (1024 * 1024 + 1)),
                                  ("bad-header.csv", b"date,channel\n2026-09-08,seo\n")):
            with self.subTest(filename=filename):
                response = self.client.post(url, {"file": SimpleUploadedFile(filename, content, content_type="text/csv")})
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context["form"].errors)
        self.assertFalse(MarketingObservation.objects.exists())
        self.login(self.auditor)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(url, {}).status_code, 403)
        self.assertEqual(self.client.get(reverse("director:marketing_template")).status_code, 403)

    @override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
    def test_login_defaults_follow_department_role_and_keep_safe_requested_destination(self):
        password = "Synthetic-only-password-625!"
        for user, destination in ((self.director, "director:overview"),
                                  (self.marketing, "marketing:overview"), (self.finance, "dashboard")):
            with self.subTest(user=user.username):
                user.set_password(password)
                user.save(update_fields=["password"])
                browser = Client()
                response = browser.post(reverse("login"), {"username": user.username, "password": password})
                self.assert_redirect(response, destination)
                self.assertEqual(browser.get(response["Location"]).status_code, 200)
        requested = reverse("director:decisions")
        browser = Client()
        response = browser.post(reverse("login"), {"username": self.director.username, "password": password, "next": requested})
        self.assertRedirects(response, requested, fetch_redirect_response=False)
        browser = Client()
        response = browser.post(reverse("login"), {"username": self.marketing.username, "password": password,
                                                   "next": "https://untrusted.example/redirect"})
        self.assert_redirect(response, "marketing:overview")
