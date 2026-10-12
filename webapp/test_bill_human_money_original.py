"""Original HTTP contract for human Rupiah on supplier bill entry."""

from datetime import date
from decimal import Decimal
from uuid import uuid4

import django.test as django_test
from django.contrib.auth import get_user_model
from django.urls import reverse

from core.models import (
    AuditEvent,
    DocumentCounter,
    DomainEvent,
    Membership,
    Organization,
)
from finance.models import (
    AccountingPeriod,
    Bill,
    BillPayment,
    Journal,
    JournalLine,
    Party,
)
from taxes.models import (
    BillTaxDecision,
    ExternalTaxReview,
    TaxEvidence,
    TaxObligation,
    TaxObligationWithdrawal,
    TaxProfile,
    TaxProfileDecision,
    TaxVerification,
)


DAY = date(2026, 1, 10)
TAX_MODELS = (
    TaxProfile,
    TaxProfileDecision,
    TaxEvidence,
    ExternalTaxReview,
    BillTaxDecision,
    TaxObligation,
    TaxObligationWithdrawal,
    TaxVerification,
)
MONEY_MODELS = (
    Bill,
    BillPayment,
    AccountingPeriod,
    Journal,
    JournalLine,
    DocumentCounter,
    AuditEvent,
    DomainEvent,
)


@django_test.override_settings(
    MFA_REQUIRED=False,
    OPENROUTER_API_KEY="",
    DIRECTOR_OPENROUTER_API_KEY="",
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class BillHumanRupiahInputOriginalDraft(django_test.TestCase):
    def setUp(self):
        self.org = Organization.objects.create(
            name="Synthetic human Rupiah supplier bills",
            vat_status="non_pkp",
        )
        self.owner = get_user_model().objects.create_user(
            username="synthetic-bill-money-owner",
        )
        self.finance = get_user_model().objects.create_user(
            username="synthetic-bill-money-finance",
        )
        Membership.objects.create(
            organization=self.org,
            user=self.owner,
            role="owner",
        )
        Membership.objects.create(
            organization=self.org,
            user=self.finance,
            role="finance",
        )
        self.supplier = Party.objects.create(
            organization=self.org,
            name="Synthetic original-document supplier",
            kind="supplier",
        )
        self.url = reverse("bill_create")
        self.finance_client = self.csrf_client(self.finance)
        self.owner_client = self.csrf_client(self.owner)

    def csrf_client(self, user):
        client = django_test.Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        self.select_org(client)
        page = client.get(self.url)
        self.assertEqual(page.status_code, 200)
        self.assertIn("csrftoken", client.cookies)
        if user.pk != self.owner.pk:
            client.force_login(user)
            self.select_org(client)
        return client

    def select_org(self, client):
        session = client.session
        session["organization_id"] = self.org.pk
        session.save()

    def payload(self, label, **changes):
        values = {
            "supplier": self.supplier.pk,
            "supplier_reference": f"SYN-BILL-MONEY-{label}",
            "submission_key": str(uuid4()),
            "amount": "50000",
            "supplier_vat": "0",
            "date": DAY.isoformat(),
            "service_date": DAY.isoformat(),
            "category": "expense",
        }
        values.update(changes)
        return values

    def post(self, client, url, values):
        return client.post(
            url,
            values,
            HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value,
        )

    def state(self, models):
        return {
            model._meta.label_lower: list(model.objects.order_by("pk").values())
            for model in models
        }

    def create_draft(self, payload, *, gross, vat, client=None):
        client = client or self.finance_client
        original_journals = self.state((Journal, JournalLine))
        original_tax = self.state(TAX_MODELS)
        response = self.post(client, self.url, payload)
        self.assertRedirects(
            response,
            reverse("bills"),
            fetch_redirect_response=False,
        )
        bill = Bill.objects.get(
            organization=self.org,
            submission_key=payload["submission_key"],
        )
        self.assertEqual(
            (
                bill.amount,
                bill.supplier_vat,
                bill.status,
                bill.tax_status,
                bill.tax_amount,
            ),
            (Decimal(gross), Decimal(vat), "draft", "review", None),
        )
        self.assertEqual(bill.supplier_id, self.supplier.pk)
        self.assertEqual(bill.supplier_reference, payload["supplier_reference"])
        self.assertNotEqual(bill.number, bill.supplier_reference)
        self.assertEqual(
            (bill.date, bill.service_date, bill.category),
            (DAY, DAY, "expense"),
        )
        self.assertEqual(bill.paid_amount, Decimal(0))
        self.assertEqual(bill.outstanding_amount, Decimal(gross))
        self.assertFalse(BillPayment.objects.filter(bill=bill).exists())
        self.assertEqual(self.state((Journal, JournalLine)), original_journals)
        self.assertEqual(self.state(TAX_MODELS), original_tax)
        return bill

    def approve_gross(self, bill, *, gross):
        original_tax = self.state(TAX_MODELS)
        response = self.post(
            self.finance_client,
            reverse("bill_approve", args=[bill.pk]),
            {},
        )
        self.assertRedirects(
            response,
            reverse("bills"),
            fetch_redirect_response=False,
        )
        bill.refresh_from_db()
        self.assertEqual(
            (bill.amount, bill.status, bill.tax_status, bill.tax_amount),
            (Decimal(gross), "approved", "review", None),
        )
        self.assertEqual(bill.paid_amount, Decimal(0))
        self.assertEqual(bill.outstanding_amount, Decimal(gross))
        journal = Journal.objects.get(
            organization=self.org,
            source_key=f"bill:{bill.pk}:approval",
        )
        self.assertEqual((journal.date, journal.status), (DAY, "posted"))
        self.assertEqual(
            list(
                journal.lines.order_by("account").values_list(
                    "account",
                    "debit",
                    "credit",
                )
            ),
            [
                ("AP", Decimal(0), Decimal(gross)),
                ("EXPENSE", Decimal(gross), Decimal(0)),
            ],
        )
        self.assertEqual(self.state(TAX_MODELS), original_tax)

    def reject_without_write(self, payload, *, field, client=None, status=200):
        client = client or self.finance_client
        before = self.state(MONEY_MODELS + TAX_MODELS)
        response = self.post(client, self.url, payload)
        self.assertEqual(response.status_code, status)
        if status == 200:
            self.assertIn(field, response.context["form"].errors)
            self.assertContains(response, 'role="alert"')
        self.assertEqual(self.state(MONEY_MODELS + TAX_MODELS), before)

    def test_amount_grouping_and_plain_decimals_write_literal_gross(self):
        cases = (
            ("plain", "50000", "50000"),
            ("canonical-cents", "50000.25", "50000.25"),
            ("small-dot-decimal", "50.00", "50.00"),
            ("grouped", "50.000", "50000"),
            ("grouped-cents", "50.000,25", "50000.25"),
        )
        for label, entered, expected in cases:
            with self.subTest(entered=entered):
                values = self.payload(f"AMOUNT-{label}", amount=entered)
                bill = self.create_draft(values, gross=expected, vat="0")
                self.approve_gross(bill, gross=expected)
                if label == "grouped":
                    before = self.state(MONEY_MODELS + TAX_MODELS)
                    response = self.post(self.finance_client, self.url, values)
                    self.assertRedirects(
                        response,
                        reverse("bills"),
                        fetch_redirect_response=False,
                    )
                    self.assertEqual(
                        self.state(MONEY_MODELS + TAX_MODELS),
                        before,
                    )
                    self.reject_without_write(
                        {**values, "submission_key": str(uuid4())},
                        field="__all__",
                    )

    def test_vat_grouping_is_included_without_increasing_literal_gross(self):
        cases = (
            ("zero", "0", "0"),
            ("canonical-cents", "5000.25", "5000.25"),
            ("small-dot-decimal", "50.00", "50.00"),
            ("grouped", "50.000", "50000"),
            ("grouped-cents", "50.000,25", "50000.25"),
            ("equals-gross", "60000.25", "60000.25"),
        )
        for label, entered, expected in cases:
            with self.subTest(entered=entered):
                values = self.payload(
                    f"VAT-{label}",
                    amount="60000.25",
                    supplier_vat=entered,
                )
                bill = self.create_draft(values, gross="60000.25", vat=expected)
                self.approve_gross(bill, gross="60000.25")
                bill.refresh_from_db()
                self.assertEqual(bill.supplier_vat, Decimal(expected))

    def test_required_vat_default_and_invalid_money_do_not_write(self):
        page = self.finance_client.get(self.url)
        self.assertEqual(page.status_code, 200)
        self.assertRegex(
            page.content.decode(),
            r'<input\b(?=[^>]*\bname="supplier_vat")'
            r'(?=[^>]*\bvalue="0(?:\.00)?")(?=[^>]*\brequired\b)[^>]*>',
        )
        self.reject_without_write(
            self.payload("BLANK-VAT", supplier_vat=""),
            field="supplier_vat",
        )
        missing = self.payload("MISSING-VAT")
        missing.pop("supplier_vat")
        self.reject_without_write(missing, field="supplier_vat")
        invalid = (
            "50,000",
            "50.00.0",
            "50.000,001",
            "Rp 50.000",
            "-50.000",
            "5e4",
            "50_000",
            "\uff15\uff10\uff10\uff10\uff10",
            "NaN",
            "Infinity",
            "1000000000000000000",
        )
        for field in ("amount", "supplier_vat"):
            for index, entered in enumerate(invalid):
                with self.subTest(field=field, entered=entered):
                    self.reject_without_write(
                        self.payload(f"INVALID-{field}-{index}", **{field: entered}),
                        field=field,
                    )
        for label, changes, field in (
            ("ZERO-GROSS", {"amount": "0"}, "amount"),
            ("NEGATIVE-VAT", {"supplier_vat": "-0.01"}, "supplier_vat"),
            ("EXCESS-PRECISION", {"amount": "50000.001"}, "amount"),
            ("VAT-OVER-GROSS", {"supplier_vat": "50000.01"}, "__all__"),
        ):
            with self.subTest(label=label):
                self.reject_without_write(
                    self.payload(label, **changes),
                    field=field,
                )

    def test_csrf_roles_and_supplier_scope_remain_enforced(self):
        before = self.state(MONEY_MODELS + TAX_MODELS)
        response = self.finance_client.post(self.url, self.payload("NO-CSRF"))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.state(MONEY_MODELS + TAX_MODELS), before)
        for role in ("marketing", "director", "reviewer", "auditor"):
            with self.subTest(role=role):
                user = get_user_model().objects.create_user(
                    username=f"synthetic-bill-money-denied-{role}",
                )
                Membership.objects.create(
                    organization=self.org,
                    user=user,
                    role=role,
                )
                self.reject_without_write(
                    self.payload(f"DENIED-{role}"),
                    field=None,
                    client=self.csrf_client(user),
                    status=403,
                )
        customer = Party.objects.create(
            organization=self.org,
            name="Synthetic customer, not supplier",
            kind="customer",
        )
        other = Organization.objects.create(name="Synthetic foreign bill company")
        foreign = Party.objects.create(
            organization=other,
            name="Synthetic foreign supplier",
            kind="supplier",
        )
        for label, supplier in (("CUSTOMER", customer), ("FOREIGN", foreign)):
            with self.subTest(label=label):
                self.reject_without_write(
                    self.payload(label, supplier=supplier.pk),
                    field="supplier",
                )
        self.create_draft(
            self.payload("OWNER-ALLOWED"),
            gross="50000",
            vat="0",
            client=self.owner_client,
        )
