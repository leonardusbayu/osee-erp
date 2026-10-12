"""Bank matching keeps corrections usable without relaxing settlement controls."""

from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from core.models import AuditEvent, DomainEvent, Membership, Organization
from finance import services
from finance.models import (
    Allocation,
    BankAccount,
    BankTransaction,
    BillPayment,
    Journal,
    Party,
    Product,
)


DAY = date(2026, 1, 5)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class BankMatchUXTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Synthetic bank matching company")
        self.other = Organization.objects.create(
            name="Other synthetic bank matching company"
        )
        self.finance = get_user_model().objects.create_user(
            username="synthetic-bank-finance"
        )
        Membership.objects.create(
            organization=self.org, user=self.finance, role="finance"
        )
        self.marketing = get_user_model().objects.create_user(
            username="synthetic-bank-marketing"
        )
        Membership.objects.create(
            organization=self.org, user=self.marketing, role="marketing"
        )
        self.client.force_login(self.finance)
        self.account = BankAccount.objects.create(
            organization=self.org, name="Synthetic account", account_number="UX-TEST"
        )
        self.customer = Party.objects.create(
            organization=self.org, name="Synthetic customer", kind="customer"
        )
        self.supplier = Party.objects.create(
            organization=self.org, name="Synthetic supplier", kind="supplier"
        )
        self.product = Product.objects.create(
            organization=self.org,
            code="UX-ITP",
            name="Synthetic ITP",
            kind="itp",
            default_price=Decimal(500000),
        )

    def bank_line(self, amount="700000", reference="UX-RECEIPT"):
        return BankTransaction.objects.create(
            organization=self.org,
            account=self.account,
            date=DAY,
            reference=reference,
            description="Synthetic transfer",
            amount=Decimal(amount),
        )

    def invoice(self, number="UX-INVOICE"):
        draft = services.create_invoice(
            organization=self.org,
            party=self.customer,
            product=self.product,
            quantity=1,
            date=DAY,
            number=number,
            actor=self.finance,
        )
        return services.issue_invoice(
            organization=self.org, invoice=draft, actor=self.finance
        )

    def bill(self):
        bill = services.create_bill(
            organization=self.org,
            supplier=self.supplier,
            amount="500000",
            date=DAY,
            service_date=DAY,
            number="UX-BILL",
            actor=self.finance,
        )
        bill.tax_status, bill.tax_amount = "reviewed", Decimal(0)
        bill._tax_service_transition = (
            True  # Synthetic fixture: a completed zero-withholding review.
        )
        bill.save()
        return services.approve_bill(
            organization=self.org, bill=bill, actor=self.finance
        )

    def posting_counts(self):
        return (
            Allocation.objects.count(),
            BillPayment.objects.count(),
            Journal.objects.count(),
            AuditEvent.objects.count(),
            DomainEvent.objects.count(),
        )

    def test_failed_receipt_preserves_amount_selection_filter_page_and_posts_nothing(
        self,
    ):
        invoice = self.invoice()
        line = self.bank_line()
        for index in range(50):
            self.bank_line(reference=f"UX-PAGE-{index}")
        before = self.posting_counts()
        response = self.client.post(
            reverse("bank_match", args=[line.pk]) + "?status=unmatched&page=2",
            {"invoice": str(invoice.pk), "amount": "600000.25"},
        )
        self.assertEqual(
            response.status_code,
            200,
            "Correction should remain on the bank form, not discard input in a redirect",
        )
        self.assertEqual(self.posting_counts(), before)
        self.assertEqual(response.context["selected_status"], "unmatched")
        self.assertEqual(response.context["page_obj"].number, 2)
        html = response.content.decode()
        self.assertRegex(html, rf'<details[^>]*id="match-{line.pk}"[^>]*open')
        self.assertIn('value="600000.25"', html)
        self.assertRegex(html, rf'<option value="{invoice.pk}" selected>')
        self.assertIn("Alokasi melebihi sisa mutasi atau sisa tagihan.", html)
        self.assertIn(
            f'action="/bank/{line.pk}/match/?status=unmatched&amp;page=2"', html
        )

    def test_failed_bill_payment_preserves_values_and_shows_local_error(self):
        bill, line = self.bill(), self.bank_line("-700000")
        before = self.posting_counts()
        response = self.client.post(
            reverse("bank_match", args=[line.pk]),
            {"bill": str(bill.pk), "amount": "600000"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.posting_counts(), before)
        html = response.content.decode()
        self.assertRegex(html, rf'<details[^>]*id="match-{line.pk}"[^>]*open')
        self.assertIn('value="600000"', html)
        self.assertRegex(html, rf'<option value="{bill.pk}" selected>')
        self.assertIn("Pembayaran melebihi sisa mutasi keluar atau sisa tagihan.", html)

    def test_missing_or_invalid_amount_is_correctable_without_posting(self):
        invoice, line = self.invoice(), self.bank_line()
        before = self.posting_counts()
        for data, error in (
            ({"invoice": "", "amount": "10"}, "Pilih invoice tujuan."),
            (
                {"invoice": str(invoice.pk), "amount": "not-a-number"},
                "Masukkan jumlah alokasi yang valid.",
            ),
        ):
            response = self.client.post(reverse("bank_match", args=[line.pk]), data)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.posting_counts(), before)
            self.assertIn(error, response.content.decode())
            self.assertIn(f'value="{data["amount"]}"', response.content.decode())

    def test_successful_partial_receipt_retains_safe_location_and_remaining_transfer(
        self,
    ):
        invoice, line = self.invoice(), self.bank_line()
        response = self.client.post(
            reverse("bank_match", args=[line.pk])
            + "?status=unmatched&page=2&next=https://example.test/",
            {
                "invoice": invoice.pk,
                "amount": "200000",
                "next": "https://example.test/",
            },
        )
        self.assertRedirects(
            response,
            reverse("bank") + "?status=unmatched&page=2",
            fetch_redirect_response=False,
        )
        allocation = Allocation.objects.get(transaction=line, invoice=invoice)
        self.assertEqual(allocation.amount, Decimal(200000))
        self.assertEqual(line.unallocated_amount, Decimal(500000))
        self.assertEqual(invoice.outstanding_amount, Decimal(300000))
        other_invoice = self.invoice("UX-SECOND-INVOICE")
        response = self.client.post(
            reverse("bank_match", args=[line.pk])
            + "?status=unexpected&page=https://example.test/",
            {"invoice": other_invoice.pk, "amount": "500000"},
        )
        self.assertRedirects(response, reverse("bank"), fetch_redirect_response=False)
        self.assertEqual(line.unallocated_amount, Decimal(0))

    def test_document_that_is_no_longer_eligible_is_identified_without_reenabling_it(
        self,
    ):
        self.invoice()
        draft = services.create_invoice(
            organization=self.org,
            party=self.customer,
            product=self.product,
            quantity=1,
            date=DAY,
            number="UX-DRAFT",
            actor=self.finance,
        )
        line = self.bank_line()
        before = self.posting_counts()
        response = self.client.post(
            reverse("bank_match", args=[line.pk]),
            {"invoice": draft.pk, "amount": "123000"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.posting_counts(), before)
        html = response.content.decode()
        self.assertIn(f'<option value="{draft.pk}" selected disabled>', html)
        self.assertIn("UX-DRAFT · Tidak dapat dicocokkan; pilih invoice lain", html)
        self.assertIn("Penerimaan harus dialokasikan ke tagihan terbit.", html)
        self.assertIn('value="123000"', html)

    def test_no_eligible_documents_explain_next_action_instead_of_empty_required_select(
        self,
    ):
        incoming, outgoing = self.bank_line(), self.bank_line("-700000", "UX-OUT")
        response = self.client.get(reverse("bank"))
        html = response.content.decode()
        self.assertNotIn(f'id="invoice-{incoming.pk}"', html)
        self.assertNotIn(f'id="bill-{outgoing.pk}"', html)
        self.assertIn("Belum ada invoice yang dapat dicocokkan.", html)
        self.assertIn("Belum ada tagihan yang dapat dicocokkan.", html)
        for route in ("sales", "bills", "customer_advance"):
            self.assertIn(f'href="{reverse(route)}"', html)

    def test_roles_tenants_csrf_and_amount_limits_remain_enforced(self):
        invoice, line = self.invoice(), self.bank_line()
        foreign = BankAccount.objects.create(
            organization=self.other,
            name="Foreign synthetic account",
            account_number="UX-FOREIGN",
        )
        foreign_line = BankTransaction.objects.create(
            organization=self.other,
            account=foreign,
            date=DAY,
            reference="UX-FOREIGN",
            amount=Decimal(100000),
        )
        before = self.posting_counts()
        self.assertEqual(
            self.client.post(
                reverse("bank_match", args=[foreign_line.pk]),
                {"invoice": invoice.pk, "amount": "1"},
            ).status_code,
            404,
        )
        self.client.force_login(self.marketing)
        self.assertEqual(
            self.client.post(
                reverse("bank_match", args=[line.pk]),
                {"invoice": invoice.pk, "amount": "1"},
            ).status_code,
            403,
        )
        secure = Client(enforce_csrf_checks=True)
        secure.force_login(self.finance)
        self.assertEqual(
            secure.post(
                reverse("bank_match", args=[line.pk]),
                {"invoice": invoice.pk, "amount": "1"},
            ).status_code,
            403,
        )
        self.assertEqual(self.posting_counts(), before)
