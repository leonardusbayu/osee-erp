"""Regressions at the Finance service, HTTP and exported-document boundaries."""
from datetime import date, timedelta
from decimal import Decimal
from io import BytesIO
from uuid import uuid4
from pypdf import PdfReader

from django.core.exceptions import ValidationError
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone

from core.models import AuditEvent, DocumentCounter, Membership, Organization
from finance import services as f
from finance.models import AccountingPeriod, Bill, Invoice, Journal, Party
from finance import tests_settlement as settlement_tests


class DeliveryRecordingTests(TestCase):
    setUp = settlement_tests.SettlementFactsTests.setUp
    invoice = settlement_tests.SettlementFactsTests.invoice
    movement = settlement_tests.SettlementFactsTests.movement

    def paid_invoice(self):
        invoice = self.invoice()
        f.reconcile_receipt(organization=self.org, invoice=invoice,
            transaction=self.movement("paid", "500000"), amount="500000")
        return invoice

    def test_http_late_entry_uses_actual_completed_date_and_preserves_evidence(self):
        invoice = self.paid_invoice()
        self.client.force_login(self.owner)
        response = self.client.post(reverse("sale_deliver", args=[invoice.pk]),
            {"date": "2026-01-31", "evidence": "Synthetic completed test register TEST-31", "confirmed": "on"})
        self.assertRedirects(response, reverse("sale_detail", args=[invoice.pk]))
        invoice.refresh_from_db()
        self.assertEqual(invoice.delivered_at, date(2026, 1, 31))
        self.assertEqual(invoice.delivery_evidence, "Synthetic completed test register TEST-31")
        self.assertEqual(f.monthly_summary(organization=self.org, year=2026, month=1)["revenue"], Decimal("500000"))
        self.assertEqual(Journal.objects.get(source_key=f"invoice:{invoice.pk}:delivery").date, date(2026, 1, 31))

    def test_service_requires_explicit_date_and_evidence_for_authenticated_entry(self):
        invoice = self.paid_invoice()
        for extra in ({}, {"date": self.day}, {"evidence": "Completed register"}):
            with self.subTest(extra=extra), self.assertRaises(ValidationError):
                f.record_delivery(organization=self.org, invoice=invoice, actor=self.owner, **extra)
        self.assertFalse(Journal.objects.filter(source_key=f"invoice:{invoice.pk}:delivery").exists())

    def test_falsey_actual_dates_are_rejected_and_only_trusted_none_uses_schedule(self):
        invoice = self.paid_invoice()
        before = AuditEvent.objects.count(), Journal.objects.count()
        for invalid in (False, 0, ""):
            for actor in (self.owner, None):
                with self.subTest(date=invalid, actor=actor), self.assertRaises(ValidationError):
                    f.record_delivery(organization=self.org, invoice=invoice, actor=actor,
                        date=invalid, evidence="Synthetic completion register")
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, "issued")
        self.assertEqual((AuditEvent.objects.count(), Journal.objects.count()), before)
        self.assertFalse(Journal.objects.filter(source_key=f"invoice:{invoice.pk}:delivery").exists())
        delivered = f.record_delivery(organization=self.org, invoice=invoice, actor=None, date=None)
        self.assertEqual(delivered.delivered_at, invoice.service_date)
        self.assertEqual(Journal.objects.filter(source_key=f"invoice:{invoice.pk}:delivery").count(), 1)

    def test_empty_and_future_http_submissions_do_not_post(self):
        invoice = self.paid_invoice()
        self.client.force_login(self.owner)
        url = reverse("sale_deliver", args=[invoice.pk])
        self.assertContains(self.client.get(url), "Tanggal seluruh layanan")
        for payload in ({}, {"date": timezone.localdate() + timedelta(days=1), "evidence": "register", "confirmed": "on"},
                        {"date": self.day, "evidence": "register"}):
            self.assertEqual(self.client.post(url, payload).status_code, 200)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, "issued")
        self.assertFalse(Journal.objects.filter(source_key=f"invoice:{invoice.pk}:delivery").exists())

    def test_historical_payment_and_closed_period_are_checked_with_actual_date(self):
        invoice = self.invoice()
        f.reconcile_receipt(organization=self.org, invoice=invoice,
            transaction=self.movement("later", "500000", date(2026, 2, 1)), amount="500000")
        with self.assertRaises(ValidationError):
            f.record_delivery(organization=self.org, invoice=invoice, actor=self.owner,
                date=date(2026, 1, 31), evidence="register")
        prepaid = self.paid_invoice()
        # Synthetic locked-period fixture checks the posting guard separately
        # from the first invoice's insufficient historical payment.
        period = AccountingPeriod.objects.get(organization=self.org, year=2026, month=1)
        period.closed = True
        period.save()
        with self.assertRaisesRegex(ValidationError, "periode"):
            f.record_delivery(organization=self.org, invoice=prepaid, actor=self.owner,
                date=self.day, evidence="register")
        self.assertFalse(Journal.objects.filter(source_key=f"invoice:{invoice.pk}:delivery").exists())

    def test_exact_retry_once_conflicting_retry_and_model_mutation_rejected(self):
        invoice = self.paid_invoice()
        kwargs = dict(organization=self.org, invoice=invoice, actor=self.owner, date=self.day, evidence="Register A")
        delivered = f.record_delivery(**kwargs)
        before = AuditEvent.objects.count(), Journal.objects.count()
        f.record_delivery(**kwargs)
        self.assertEqual((AuditEvent.objects.count(), Journal.objects.count()), before)
        with self.assertRaises(ValidationError):
            f.record_delivery(**{**kwargs, "evidence": "Register B"})
        delivered.delivery_evidence = "Rewritten"
        with self.assertRaises(ValidationError):
            delivered.save()
        delivered.refresh_from_db()
        self.assertEqual(delivered.delivery_evidence, "Register A")

    def test_delivery_http_scope_role_and_csrf(self):
        invoice = self.paid_invoice()
        url = reverse("sale_deliver", args=[invoice.pk])
        secure = Client(enforce_csrf_checks=True)
        secure.force_login(self.owner)
        self.assertEqual(secure.post(url, {"date": self.day, "evidence": "Register", "confirmed": "on"}).status_code, 403)
        self.client.force_login(self.owner)
        Membership.objects.filter(user=self.owner).update(role="auditor")
        self.assertEqual(self.client.post(url, {}).status_code, 403)
        Membership.objects.filter(user=self.owner).update(role="owner")
        other = Organization.objects.create(name="Another company")
        Membership.objects.create(user=self.owner, organization=other, role="owner")
        session = self.client.session
        session["organization_id"] = other.pk
        session.save()
        self.assertEqual(self.client.get(url).status_code, 404)


class SupplierIdentityTests(TestCase):
    def setUp(self):
        settlement_tests.SettlementFactsTests.setUp(self)
        self.supplier = Party.objects.create(organization=self.org, name="Synthetic supplier", kind="supplier")
        self.client.force_login(self.owner)

    def payload(self, **changes):
        payload = {"supplier": self.supplier.pk, "supplier_reference": "IIEF / ORIGINAL-01",
            "submission_key": str(uuid4()), "amount": "500000", "supplier_vat": "0",
            "date": "2026-01-10", "service_date": "2026-01-10", "category": "provider"}
        payload.update(changes)
        return payload

    def test_same_http_document_and_retry_produce_only_one_bill_and_payable(self):
        payload = self.payload()
        response = self.client.post(reverse("bill_create"), payload)
        self.assertEqual(response.status_code, 302)
        first = Bill.objects.get(organization=self.org)
        self.client.post(reverse("bill_approve", args=[first.pk]))
        self.assertEqual(self.client.post(reverse("bill_create"), payload).status_code, 302)
        self.assertEqual(Bill.objects.filter(organization=self.org).count(), 1)
        first.refresh_from_db()
        self.assertEqual(first.supplier_reference, "IIEF / ORIGINAL-01")
        self.assertNotEqual(first.number, first.supplier_reference)
        self.assertEqual(f.report_balances(organization=self.org)["AP"], Decimal("-500000"))
        duplicate = self.payload(supplier_reference="  iief  / original-01  ")
        self.assertEqual(self.client.post(reverse("bill_create"), duplicate).status_code, 200)
        self.assertEqual(Bill.objects.filter(organization=self.org).count(), 1)

    def test_distinct_documents_with_equal_price_and_punctuation_remain_valid(self):
        for reference in ("SOURCE-1", "SOURCE/1", "SOURCE-2"):
            self.assertEqual(self.client.post(reverse("bill_create"), self.payload(supplier_reference=reference)).status_code, 302)
        self.assertEqual(Bill.objects.count(), 3)

    def test_request_conflict_unknown_reference_and_invalid_key_do_not_create(self):
        payload = self.payload()
        self.client.post(reverse("bill_create"), payload)
        before = (AuditEvent.objects.count(), DocumentCounter.objects.get(organization=self.org, kind="BILL").value)
        for change in ({"amount": "600000"}, {"supplier_reference": "DIFFERENT"}, {"submission_key": "invalid"},
                       {"supplier_reference": ""}, {"submission_key": ""}):
            with self.subTest(change=change):
                self.assertEqual(self.client.post(reverse("bill_create"), {**payload, **change}).status_code, 200)
        self.assertEqual(Bill.objects.count(), 1)
        self.assertEqual((AuditEvent.objects.count(), DocumentCounter.objects.get(organization=self.org, kind="BILL").value), before)

    def test_same_reference_allowed_for_other_suppliers_and_organizations_but_cross_reference_rejected(self):
        other_supplier = Party.objects.create(organization=self.org, name="Second supplier", kind="supplier")
        for supplier in (self.supplier, other_supplier):
            self.assertEqual(self.client.post(reverse("bill_create"), self.payload(supplier=supplier.pk)).status_code, 302)
        other = Organization.objects.create(name="Other supplier company")
        foreign = Party.objects.create(organization=other, name="Foreign supplier", kind="supplier")
        response = self.client.post(reverse("bill_create"), self.payload(supplier=foreign.pk))
        self.assertEqual(response.status_code, 200)
        trusted = f.create_bill(organization=other, supplier=foreign, amount="500000", date=self.day,
            service_date=self.day, supplier_reference="IIEF / ORIGINAL-01", submission_key=uuid4())
        self.assertEqual(trusted.organization_id, other.pk)
        self.assertEqual(Bill.objects.filter(organization=self.org).count(), 2)

    def test_legacy_unknown_references_are_not_guessed_and_identity_is_immutable(self):
        for number in ("LEGACY-1", "LEGACY-2"):
            legacy = f.create_bill(organization=self.org, supplier=self.supplier, amount="500000",
                date=self.day, service_date=self.day, number=number)
            self.assertEqual(legacy.supplier_reference_key, "")
            self.assertIsNone(legacy.submission_key)
        self.client.post(reverse("bill_create"), self.payload())
        bill = Bill.objects.exclude(supplier_reference="").get()
        for field, value in (("supplier_reference", "CHANGED"), ("submission_key", uuid4())):
            setattr(bill, field, value)
            with self.assertRaises(ValidationError):
                bill.save()
            bill.refresh_from_db()

    def test_model_boundary_rejects_normalized_duplicate_and_controls(self):
        f.create_bill(organization=self.org, supplier=self.supplier, amount="500000", date=self.day,
            service_date=self.day, supplier_reference="ＩＩＥＦ  / A", submission_key=uuid4())
        with self.assertRaises(ValidationError):
            Bill.objects.create(organization=self.org, supplier=self.supplier, number="DIRECT-2", amount="500000",
                date=self.day, service_date=self.day, supplier_reference="iief / a")
        with self.assertRaises(ValidationError):
            f.create_bill(organization=self.org, supplier=self.supplier, amount="500000", date=self.day,
                service_date=self.day, supplier_reference="BAD\x00REFERENCE")
        self.assertEqual(Bill.objects.count(), 1)

    def test_expanding_unicode_reference_returns_validation_error_before_numbering(self):
        before = AuditEvent.objects.count()
        with self.assertRaisesMessage(ValidationError, "terlalu panjang setelah normalisasi"):
            f.create_bill(organization=self.org, actor=self.owner, supplier=self.supplier,
                amount="500000", date=self.day, service_date=self.day,
                supplier_reference=chr(0xFDFA) * 30, submission_key=uuid4())
        self.assertFalse(Bill.objects.exists())
        self.assertFalse(DocumentCounter.objects.filter(organization=self.org, kind="BILL").exists())
        self.assertEqual(AuditEvent.objects.count(), before)

    def test_expanding_unicode_reference_http_is_readable_form_error_without_side_effects(self):
        before = AuditEvent.objects.count()
        response = self.client.post(reverse("bill_create"), self.payload(supplier_reference=chr(0xFDFA) * 30))
        self.assertContains(response, "terlalu panjang setelah normalisasi", status_code=200)
        self.assertFalse(Bill.objects.exists())
        self.assertFalse(DocumentCounter.objects.filter(organization=self.org, kind="BILL").exists())
        self.assertEqual(AuditEvent.objects.count(), before)

    def test_supplier_form_scope_role_and_csrf_preserve_state(self):
        response = self.client.get(reverse("bill_create"))
        self.assertContains(response, 'name="supplier_reference"')
        self.assertContains(response, 'name="submission_key"')
        secure = Client(enforce_csrf_checks=True)
        secure.force_login(self.owner)
        self.assertEqual(secure.post(reverse("bill_create"), self.payload()).status_code, 403)
        Membership.objects.filter(user=self.owner).update(role="auditor")
        self.assertEqual(self.client.post(reverse("bill_create"), self.payload()).status_code, 403)
        self.assertFalse(Bill.objects.exists())


class CancellationPdfTests(TestCase):
    setUp = settlement_tests.SettlementFactsTests.setUp
    invoice = settlement_tests.SettlementFactsTests.invoice
    movement = settlement_tests.SettlementFactsTests.movement

    def test_cancelled_unpaid_partial_paid_and_refunded_pdf_has_zero_collectible(self):
        self.client.force_login(self.owner)
        for index, (paid, refund) in enumerate(((0, 0), (200000, 0), (500000, 0), (200000, 200000))):
            with self.subTest(paid=paid, refund=refund):
                invoice = self.invoice()
                if paid:
                    f.reconcile_receipt(organization=self.org, invoice=invoice,
                        transaction=self.movement(f"receipt-{index}", str(paid)), amount=str(paid))
                f.cancel_invoice(organization=self.org, actor=self.owner, invoice=invoice, date=self.day, reason="Synthetic cancellation")
                if refund:
                    f.refund_cancelled_invoice(organization=self.org, actor=self.owner, invoice=invoice,
                        transaction=self.movement(f"refund-{index}", str(-refund)), amount=str(refund),
                        reference=f"refund-post-{index}", evidence="Synthetic returned funds")
                before = Journal.objects.count()
                response = self.client.get(reverse("sale_pdf", args=[invoice.pk]))
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.content.startswith(b"%PDF-"))
                text = "\n".join(page.extract_text() for page in PdfReader(BytesIO(response.content)).pages)
                self.assertIn("DIBATALKAN", text)
                self.assertRegex(text, r"Saldo yang masih dapat ditagih\s+Rp0,00")
                expected_refund = f"Rp{paid-refund:,},00".replace(",", ".", 1) if paid-refund >= 1000 else f"Rp{paid-refund},00"
                self.assertRegex(text, rf"Sisa kewajiban pengembalian\s+{expected_refund.replace('.', '[.]')}")
                self.assertIn("Nilai invoice sebelum pembatalan", text)
                self.assertEqual(Journal.objects.count(), before)
