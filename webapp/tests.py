from datetime import date, timedelta
from decimal import Decimal
import tempfile
import io
from pypdf import PdfWriter
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, Client, override_settings
from django.urls import reverse
from django.utils import timezone
from core.models import Organization, Membership
from finance.models import Party, Product, Invoice, BankAccount, BankTransaction, Journal
from finance import services

def valid_pdf():
    output = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(output)
    return output.getvalue()

class FinanceWebTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="OSEE test")
        self.user = User.objects.create_user("owner", password="Testing-long-password-123")
        Membership.objects.create(organization=self.org, user=self.user, role="owner")
        self.client.force_login(self.user)
        self.party = Party.objects.create(organization=self.org, name="Mitra pengujian", kind="reseller")
        self.product = Product.objects.create(organization=self.org, code="ITP", name="ITP", kind="itp", default_price=Decimal("510000"))
        self.account = BankAccount.objects.create(organization=self.org, name="Bank test", account_number="TEST-ONLY")
        self.other = Organization.objects.create(name="Different organization")

    def test_page_routes_render_and_require_authentication(self):
        for name in ["dashboard", "sales", "sale_create", "bills", "bill_create", "bank", "bank_import", "partners", "prices", "reports", "settings", "modules", "tax_workspace", "annual_tax", "chat"]:
            response = self.client.get(reverse(name))
            self.assertEqual(response.status_code, 200, name)
        anonymous = Client()
        self.assertEqual(anonymous.get(reverse("dashboard")).status_code, 302)

    def test_real_invoice_receipt_delivery_journey(self):
        today = timezone.localdate()
        response = self.client.post(reverse("sale_create"), {"party": self.party.pk, "product": self.product.pk, "quantity": 2, "date": today, "due_date": today, "service_date": today})
        invoice = Invoice.objects.get(organization=self.org)
        self.assertRedirects(response, reverse("sale_detail", args=[invoice.pk]))
        self.client.post(reverse("sale_issue", args=[invoice.pk]))
        self.client.post(reverse("sale_deliver", args=[invoice.pk]))
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, "issued", "Unpaid reseller cannot receive test")
        line = BankTransaction.objects.create(organization=self.org, account=self.account, reference="TEST-PAY", date=today, description="Matching receipt", amount=invoice.total)
        response = self.client.post(reverse("bank_match", args=[line.pk]), {"invoice": invoice.pk, "amount": str(invoice.total)})
        self.assertEqual(response.status_code, 302)
        self.client.post(reverse("sale_deliver", args=[invoice.pk]))
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, "delivered")
        balances = services.report_balances(organization=self.org)
        self.assertEqual(balances["AR"], Decimal("0"))
        self.assertEqual(balances["BANK"], Decimal("1020000"))
        self.assertEqual(balances["REVENUE"], Decimal("-1020000"))
        count = Journal.objects.count()
        self.client.post(reverse("sale_issue", args=[invoice.pk]))
        self.client.post(reverse("sale_deliver", args=[invoice.pk]))
        self.assertEqual(Journal.objects.count(), count)

    def test_forms_reject_cross_company_reference(self):
        other_party = Party.objects.create(organization=self.other, name="Private partner", kind="reseller")
        today = timezone.localdate()
        response = self.client.post(reverse("sale_create"), {"party": other_party.pk, "product": self.product.pk, "quantity": 2, "date": today, "due_date": today, "service_date": today})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Invoice.objects.exists())
        self.assertNotContains(self.client.get(reverse("partners")), "Private partner")

    def test_readonly_role_cannot_mutate_or_access_other_org(self):
        Membership.objects.filter(user=self.user).update(role="auditor")
        self.assertEqual(self.client.get(reverse("sale_create")).status_code, 403)
        self.assertEqual(self.client.post(reverse("partner_create"), {"name": "No", "kind": "supplier"}).status_code, 403)
        other_party = Party.objects.create(organization=self.other, name="Other", kind="reseller")
        other_product = Product.objects.create(organization=self.other, code="ITP", name="Other", kind="itp", default_price=Decimal("500000"))
        invoice = services.create_invoice(organization=self.other, party=other_party, product=other_product, quantity=1, date=timezone.localdate())
        self.assertEqual(self.client.get(reverse("sale_detail", args=[invoice.pk])).status_code, 404)

    def test_csrf_required_for_financial_mutations(self):
        secure = Client(enforce_csrf_checks=True)
        secure.force_login(self.user)
        self.assertEqual(secure.post(reverse("partner_create"), {"name": "Blocked", "kind": "supplier"}).status_code, 403)

    def test_get_cannot_issue_invoice(self):
        invoice = services.create_invoice(organization=self.org, party=self.party, product=self.product, quantity=1, date=timezone.localdate())
        self.assertEqual(self.client.get(reverse("sale_issue", args=[invoice.pk])).status_code, 405)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, "draft")

    def test_csv_import_repeated_upload_is_idempotent(self):
        from webapp.models import StatementImport
        content = f"date,reference,description,amount\n{timezone.localdate()},TEST-REF,Example,100000\n".encode()
        with tempfile.TemporaryDirectory() as media, override_settings(MEDIA_ROOT=media):
            for _ in range(2):
                uploaded = SimpleUploadedFile("bank.csv", content, content_type="text/csv")
                self.assertEqual(self.client.post(reverse("bank_import"), {"account": self.account.pk, "file": uploaded}).status_code, 302)
            self.assertEqual(StatementImport.objects.count(), 1)
            self.assertFalse(BankTransaction.objects.exists(), "Upload requires explicit preview and confirmation")

    def test_tax_evidence_input_cannot_set_regime_or_reviewer(self):
        self.client.post(reverse("tax_profile_setup"), {"registration_date": "2024-01-01", "registration_evidence": "SKT reference", "regime": "final", "reviewed_by": self.user.pk})
        from taxes.models import TaxProfile
        profile = TaxProfile.objects.get(organization=self.org)
        self.assertEqual(profile.regime, "undecided")
        self.assertIsNone(profile.reviewed_at)

    def test_exports_and_bad_period(self):
        self.assertEqual(self.client.get(reverse("report_export") + "?period=2026-99").status_code, 400)
        exported = self.client.get(reverse("report_export") + "?period=2026-08")
        self.assertEqual(exported.status_code, 200)
        self.assertIn("attachment", exported["Content-Disposition"])
        self.assertContains(exported, "Draf komersial")

    def test_empty_real_workspace_setup_does_not_seed_demo_transactions(self):
        self.assertFalse(Invoice.objects.filter(organization=self.org).exists())
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "penjualan pertama")

    def test_out_of_range_dates_return_form_errors(self):
        invalid = {"party": self.party.pk, "product": self.product.pk, "quantity": 1, "date": "1999-12-31", "due_date": "1999-12-31", "service_date": "1999-12-31"}
        self.assertEqual(self.client.post(reverse("sale_create"), invalid).status_code, 200)
        upload = SimpleUploadedFile("dates.csv", b"date,reference,description,amount\n1999-12-31,OLD,Old date,1\n")
        with tempfile.TemporaryDirectory() as media, override_settings(MEDIA_ROOT=media):
            self.assertEqual(self.client.post(reverse("bank_import"), {"account": self.account.pk, "file": upload}).status_code, 302)
            from webapp.models import StatementImport
            staged = StatementImport.objects.get()
            response = self.client.post(reverse("statement_preview", args=[staged.pk]), {"date": "0", "reference": "1", "description": "2", "amount": "3", "number_format": "en", "reference_mode": "bank_reference", "first_row": 1, "last_row": 1})
            self.assertContains(response, "Tanggal harus lengkap")
        self.assertFalse(BankTransaction.objects.exists())
        self.assertEqual(self.client.get(reverse("dashboard") + "?period=2000-01").status_code, 200)
        response = self.client.post(reverse("price_create"), {"party": self.party.pk, "product": self.product.pk, "kind": "selling", "amount": "500000", "effective_from": "9999-12-31"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "2000 dan 2200")

    def test_bill_creation_in_closed_period_is_a_form_error(self):
        from finance.models import AccountingPeriod, Bill
        AccountingPeriod.objects.create(organization=self.org, year=2020, month=1, closed=True)
        supplier = Party.objects.create(organization=self.org, name="Supplier", kind="supplier")
        response = self.client.post(reverse("bill_create"), {"supplier": supplier.pk, "amount": "100000", "supplier_vat": "0", "date": "2020-01-04", "service_date": "2020-01-02", "category": "provider"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Bill.objects.exists())
        self.assertContains(response, "dikunci")

    def test_bank_pagination_keeps_older_unmatched_rows_reachable(self):
        for index in range(52):
            BankTransaction.objects.create(organization=self.org, account=self.account, reference=f"PAGE-{index:03d}", date=timezone.localdate(), description="Pagination example", amount=Decimal("100"))
        response = self.client.get(reverse("bank") + "?status=unmatched&page=2")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["unmatched_count"], 52)
        self.assertEqual(response.context["page_obj"].paginator.num_pages, 2)
        self.assertContains(response, "PAGE-000")

    def test_pdf_exports_are_private_and_scoped(self):
        invoice = services.create_invoice(organization=self.org, party=self.party, product=self.product, quantity=1, date=timezone.localdate())
        result = self.client.get(reverse("sale_pdf", args=[invoice.pk]))
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.content.startswith(b"%PDF-"))
        self.assertEqual(result["Cache-Control"], "private, no-store")
        self.assertEqual(self.client.get(reverse("report_pdf") + "?period=2026-99").status_code, 400)
        report = self.client.get(reverse("report_pdf") + "?period=2026-08")
        self.assertTrue(report.content.startswith(b"%PDF-"))
        foreign_user = User.objects.create_user("foreign")
        Membership.objects.create(organization=self.other, user=foreign_user, role="owner")
        self.client.force_login(foreign_user)
        self.assertEqual(self.client.get(reverse("sale_pdf", args=[invoice.pk])).status_code, 404)
        self.assertEqual(Client().get(reverse("sale_pdf", args=[invoice.pk])).status_code, 302)

    def test_attachment_http_flow_deduplicates_and_checks_permission(self):
        from evidence.models import Attachment
        invoice = services.create_invoice(organization=self.org, party=self.party, product=self.product, quantity=1, date=timezone.localdate())
        route = reverse("sale_attachment", args=[invoice.pk])
        content = valid_pdf()
        with tempfile.TemporaryDirectory() as media, override_settings(MEDIA_ROOT=media):
            for _ in range(2):
                response = self.client.post(route, {"file": SimpleUploadedFile("proof.pdf", content, content_type="application/pdf")})
                self.assertRedirects(response, reverse("sale_detail", args=[invoice.pk]))
            self.assertEqual(Attachment.objects.count(), 1)
            attachment = Attachment.objects.get()
            download = self.client.get(reverse("evidence:download", args=[attachment.pk]))
            self.assertEqual(b"".join(download.streaming_content), content)
            self.assertEqual(self.client.get(route).status_code, 405)
            secure = Client(enforce_csrf_checks=True)
            secure.force_login(self.user)
            self.assertEqual(secure.post(route, {"file": SimpleUploadedFile("proof.pdf", content)}).status_code, 403)
            Membership.objects.filter(user=self.user).update(role="auditor")
            self.assertEqual(self.client.post(route, {"file": SimpleUploadedFile("proof.pdf", content)}).status_code, 403)
            foreign_user = User.objects.create_user("foreign-evidence")
            Membership.objects.create(organization=self.other, user=foreign_user, role="owner")
            self.client.force_login(foreign_user)
            self.assertEqual(self.client.get(reverse("evidence:download", args=[attachment.pk])).status_code, 404)
            self.assertEqual(self.client.post(route, {"file": SimpleUploadedFile("proof.pdf", content)}).status_code, 404)

    def test_bill_details_and_upload_bind_the_bill_from_the_url(self):
        supplier = Party.objects.create(organization=self.org, name="Supplier", kind="supplier")
        bill = services.create_bill(organization=self.org, supplier=supplier, amount=Decimal("450000"), date=timezone.localdate(), service_date=timezone.localdate(), number="BILL-TEST", actor=self.user)
        self.assertContains(self.client.get(reverse("bill_detail", args=[bill.pk])), "Nilai pajak belum ditentukan")
        with tempfile.TemporaryDirectory() as media, override_settings(MEDIA_ROOT=media):
            response = self.client.post(reverse("bill_attachment", args=[bill.pk]), {"file": SimpleUploadedFile("bill.pdf", valid_pdf()), "organization": self.other.pk, "invoice": 99999})
            self.assertRedirects(response, reverse("bill_detail", args=[bill.pk]))
            attached = bill.attachments.get()
            self.assertEqual(attached.organization_id, self.org.pk)
            self.assertIsNone(attached.invoice_id)
