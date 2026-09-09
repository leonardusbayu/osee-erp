"""HTTP regressions for the controlled Finance evidence forms."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from config.urls import urlpatterns as project_urls
from webapp.operations_views import urlpatterns as operation_urls
from core.models import AuditEvent, Membership, Organization
from finance import services as f
from finance.models import BankAccount, BankPosting, BankTransaction, CustomerAdvance, Journal
from . import tests_remediation as fixtures

urlpatterns = operation_urls + project_urls


@override_settings(ROOT_URLCONF=__name__)
class FinancialOperationHttpTests(TestCase):
    setUp = fixtures.FinanceRemediationTests.setUp
    invoice = fixtures.FinanceRemediationTests.invoice
    movement = fixtures.FinanceRemediationTests.movement

    def test_owner_fee_form_exact_full_allocation_retry_is_successful_without_duplicate(self):
        self.client.force_login(self.actor)
        bank = self.movement("-15000")
        url = reverse("bank_adjustment", args=[bank.pk])
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        payload = {"kind": "bank_fee", "amount": "15000", "reference": page.context["form"]["reference"].value(),
                   "evidence": "Statement fee verified", "confirmed": "on"}
        self.assertRedirects(self.client.post(url, payload), reverse("financial_movements"))
        counts = (Journal.objects.count(), BankPosting.objects.count(), AuditEvent.objects.count())
        self.assertRedirects(self.client.post(url, payload), reverse("financial_movements"))
        self.assertEqual((Journal.objects.count(), BankPosting.objects.count(), AuditEvent.objects.count()), counts)
        self.assertEqual(bank.unallocated_amount, 0)

    def test_customer_advance_form_full_bank_retry_and_invalid_amount_have_no_duplicate_side_effects(self):
        self.client.force_login(self.actor)
        bank = self.movement("500000")
        url = reverse("customer_advance")
        page = self.client.get(url)
        payload = {"party": self.party.pk, "transaction": bank.pk, "amount": "500000",
                   "reference": page.context["form"]["reference"].value(), "evidence": "Payer and statement checked", "confirmed": "on"}
        self.assertRedirects(self.client.post(url, payload), reverse("financial_movements"))
        self.assertRedirects(self.client.post(url, payload), reverse("financial_movements"))
        self.assertEqual(CustomerAdvance.objects.count(), 1)
        bad = self.client.post(url, payload | {"amount": "NaN", "reference": "BAD"})
        self.assertEqual(bad.status_code, 200)
        self.assertTrue(bad.context["form"].errors)
        self.assertEqual(CustomerAdvance.objects.count(), 1)

    def test_scope_roles_and_csrf_protect_operation_writes(self):
        self.client.force_login(self.actor)
        other = Organization.objects.create(name="Foreign operation company")
        other_account = BankAccount.objects.create(organization=other, name="Foreign bank", account_number="FOREIGN")
        foreign_bank = BankTransaction.objects.create(organization=other, account=other_account,
            date=self.day, reference="FOREIGN", amount=Decimal("-1"))
        self.assertEqual(self.client.get(reverse("bank_adjustment", args=[foreign_bank.pk])).status_code, 404)
        bank = self.movement("-1")
        for role in ("auditor", "marketing", "finance"):
            user = get_user_model().objects.create_user(username=f"operation-{role}")
            Membership.objects.create(organization=self.org, user=user, role=role)
            self.client.force_login(user)
            expected = 200 if role == "finance" else 403
            self.assertEqual(self.client.get(reverse("bank_adjustment", args=[bank.pk])).status_code, expected)
            self.assertEqual(self.client.post(reverse("bank_opening", args=[self.account.pk]), {}).status_code, 403)
        secure = Client(enforce_csrf_checks=True)
        secure.force_login(self.actor)
        self.assertEqual(secure.post(reverse("bank_adjustment", args=[bank.pk]), {}).status_code, 403)
        self.assertFalse(BankPosting.objects.exists())

    def test_finance_cannot_forge_owner_funding_and_huge_bank_id_is_a_form_error(self):
        finance = get_user_model().objects.create_user(username="operation-finance-forge")
        Membership.objects.create(organization=self.org, user=finance, role="finance")
        self.client.force_login(finance)
        receipt = self.movement("100000")
        response = self.client.post(reverse("bank_adjustment", args=[receipt.pk]), {"kind": "owner_funding",
            "amount": "100000", "reference": "forged-owner", "evidence": "Not authorized", "confirmed": "on"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("kind", response.context["form"].errors)
        response = self.client.post(reverse("customer_advance"), {"party": self.party.pk,
            "transaction": "9" * 5000, "amount": "100000", "reference": "bad-id", "evidence": "Invalid selection", "confirmed": "on"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors)
        self.assertFalse(BankPosting.objects.exists())

    def test_owner_operation_pages_and_advance_application_are_usable(self):
        self.client.force_login(self.actor)
        receipt = self.movement()
        advance = f.record_customer_advance(organization=self.org, actor=self.actor, party=self.party,
            transaction=receipt, amount="500000", reference="fixture-advance", evidence="Synthetic advance")
        inv = f.issue_invoice(organization=self.org, actor=self.actor, invoice=self.invoice())
        bill = f.create_bill(organization=self.org, actor=self.actor, supplier=self.supplier,
            amount="100000", date=self.day, service_date=self.day, category="prepayment", number="FORM-BILL")
        routes = [("financial_movements", []), ("bank_transfer", []), ("bank_opening", [self.account.pk]),
                  ("prepayment_release", [bill.pk]), ("customer_advance_apply", [advance.pk]),
                  ("customer_advance_refund", [advance.pk]), ("sale_cancel", [inv.pk]),
                  ("sale_refund", [inv.pk]), ("bill_net_payment", [bill.pk])]
        for name, args in routes:
            with self.subTest(route=name):
                response = self.client.get(reverse(name, args=args))
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, "Traceback")
        url = reverse("customer_advance_apply", args=[advance.pk])
        page = self.client.get(url)
        payload = {"invoice": inv.pk, "date": self.day.isoformat(), "amount": "500000",
                   "reference": page.context["form"]["reference"].value(), "evidence": "Invoice and payer checked", "confirmed": "on"}
        self.assertRedirects(self.client.post(url, payload), reverse("financial_movements"))
        self.assertEqual(inv.outstanding_amount, 0)
