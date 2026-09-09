"""Regression scenarios reproduced during the company-readiness audit."""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import AuditEvent, Membership, Organization
from . import services as f
from .models import BankAccount, BankTransaction, Bill, Invoice, Journal, Party, Product


class FinanceRemediationTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Synthetic finance remediation")
        self.actor = get_user_model().objects.create_user(username="finance-remediation")
        Membership.objects.create(organization=self.org, user=self.actor, role="owner")
        self.party = Party.objects.create(organization=self.org, name="Synthetic reseller", kind="reseller")
        self.supplier = Party.objects.create(organization=self.org, name="Synthetic provider", kind="supplier")
        self.product = Product.objects.create(organization=self.org, code="ITP", name="ITP", kind="itp", default_price=Decimal("500000"))
        self.account = BankAccount.objects.create(organization=self.org, name="Synthetic bank", account_number="SYN-01")
        self.day = date(2026, 1, 10)

    def invoice(self, **changes):
        data = dict(organization=self.org, actor=self.actor, party=self.party, product=self.product,
                    quantity=1, date=self.day, service_date=self.day)
        data.update(changes)
        return f.create_invoice(**data)

    def movement(self, amount="500000", day=None, reference="R1", account=None):
        f.import_bank_csv(organization=self.org, account=account or self.account, actor=self.actor,
            content=f"date,reference,description,amount\n{day or self.day},{reference},Synthetic,{amount}\n")
        return BankTransaction.objects.get(organization=self.org, account=account or self.account, reference=reference)

    def test_later_closed_period_protects_earlier_balance_from_every_posting_entry(self):
        draft = self.invoice(service_date=date(2026, 3, 10))
        # Drafts would block closing, so use another empty organization for the cutoff fixture.
        org = Organization.objects.create(name="Synthetic closed cutoff")
        Membership.objects.create(organization=org, user=self.actor, role="owner")
        f.close_month(organization=org, year=2026, month=2, actor=self.actor)
        customer = Party.objects.create(organization=org, name="Customer", kind="customer")
        product = Product.objects.create(organization=org, code="P", name="P", kind="itp", default_price=Decimal("500000"))
        account = BankAccount.objects.create(organization=org, name="Bank", account_number="CUTOFF")
        before = f.monthly_summary(organization=org, year=2026, month=2)["balances"]
        with self.assertRaises(ValidationError):
            f.create_invoice(organization=org, actor=self.actor, party=customer, product=product, quantity=1,
                date=self.day, service_date=date(2026, 3, 10))
        with self.assertRaises(ValidationError):
            f.post_journal(organization=org, actor=self.actor, date=self.day, source_key="earlier",
                description="Earlier adjustment", entries=[("BANK", "100", "0"), ("REVENUE", "0", "100")])
        with self.assertRaises(ValidationError):
            f.import_bank_csv(organization=org, account=account, actor=self.actor,
                content="date,reference,description,amount\n2026-01-10,EARLIER,Synthetic,100\n")
        self.assertEqual(before, f.monthly_summary(organization=org, year=2026, month=2)["balances"])
        self.assertFalse(Journal.objects.filter(organization=org).exists())
        self.assertTrue(draft.pk)

    def test_future_bank_import_http_and_historical_delivery_cutoff_rejected(self):
        self.client.force_login(self.actor)
        future = timezone.localdate() + timedelta(days=10)
        response = self.client.post(reverse("bank_import"), {"account": self.account.pk,
            "file": SimpleUploadedFile("synthetic.csv", f"date,reference,description,amount\n{future},FUTURE,Synthetic,500000\n".encode())})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(BankTransaction.objects.exists())
        inv = f.issue_invoice(organization=self.org, invoice=self.invoice(), actor=self.actor)
        later = self.movement(day=date(2026, 2, 10))
        f.reconcile_receipt(organization=self.org, invoice=inv, transaction=later, amount="500000", actor=self.actor)
        with self.assertRaises(ValidationError):
            f.record_delivery(organization=self.org, invoice=inv, date=self.day, actor=self.actor)
        inv.refresh_from_db()
        self.assertEqual(inv.status, "issued")
        self.assertEqual(f.report_balances(organization=self.org)["REVENUE"], 0)

    def test_old_draft_instances_cannot_delete_posted_source_documents(self):
        old_invoice = self.invoice()
        f.issue_invoice(organization=self.org, invoice=old_invoice, actor=self.actor)
        old_bill = f.create_bill(organization=self.org, actor=self.actor, supplier=self.supplier,
            amount="444000", date=self.day, service_date=self.day, number="B1")
        f.approve_bill(organization=self.org, bill=old_bill, actor=self.actor)
        for old in (old_invoice, old_bill):
            with self.subTest(model=type(old).__name__), self.assertRaises(ValidationError):
                old.delete()
        self.assertTrue(Invoice.objects.filter(pk=old_invoice.pk).exists())
        self.assertTrue(Bill.objects.filter(pk=old_bill.pk).exists())
        self.assertEqual(Journal.objects.count(), 2)
