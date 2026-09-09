"""Regression scenarios reproduced during the company-readiness audit."""
from datetime import date, timedelta
from io import BytesIO
from tempfile import TemporaryDirectory
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import AuditEvent, Membership, Organization
from . import services as f
from .models import AccountingPeriod, BankAccount, BankTransaction, Bill, Invoice, Journal, Party, Product


class FinanceRemediationTests(TestCase):
    def setUp(self):
        self.storage = TemporaryDirectory(prefix="osee-finance-remediation-")
        self.addCleanup(self.storage.cleanup)
        settings_override = override_settings(MEDIA_ROOT=self.storage.name)
        settings_override.enable()
        self.addCleanup(settings_override.disable)
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

    def test_frozen_earlier_month_can_receive_its_own_close_record_without_reopening(self):
        f.close_month(organization=self.org, actor=self.actor, year=2026, month=2)
        f.close_month(organization=self.org, actor=self.actor, year=2026, month=1)
        self.assertEqual(AccountingPeriod.objects.filter(organization=self.org, closed=True).count(), 2)
        with self.assertRaises(ValidationError):
            self.invoice()

    def test_future_bank_import_http_and_historical_delivery_cutoff_rejected(self):
        self.client.force_login(self.actor)
        future = timezone.localdate() + timedelta(days=10)
        response = self.client.post(reverse("bank_import"), {"account": self.account.pk,
            "file": SimpleUploadedFile("synthetic.csv", f"date,reference,description,amount\n{future},FUTURE,Synthetic,500000\n".encode())})
        self.assertIn(response.status_code, (200, 302))  # Current UI may stage a statement before posting.
        self.assertFalse(BankTransaction.objects.exists())
        with self.assertRaises(ValidationError):
            self.movement(day=future, reference="FUTURE")
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

    def test_service_returned_invoice_cannot_be_reopened_or_repriced(self):
        inv = f.issue_invoice(organization=self.org, invoice=self.invoice(), actor=self.actor)
        count = AuditEvent.objects.count()
        inv.status = "draft"
        with self.assertRaises(ValidationError):
            inv.save()
        inv.refresh_from_db()
        inv.unit_price = Decimal("1")
        with self.assertRaises(ValidationError):
            inv.save()
        inv.refresh_from_db()
        self.assertEqual((inv.status, inv.total), ("issued", Decimal("500000")))
        self.assertEqual(f.report_balances(organization=self.org)["AR"], Decimal("500000"))
        self.assertEqual(AuditEvent.objects.count(), count)

    def test_deactivated_actor_is_rejected_from_current_persisted_user_state(self):
        get_user_model().objects.filter(pk=self.actor.pk).update(is_active=False)
        count = AuditEvent.objects.count()
        with self.assertRaises(PermissionDenied):
            self.invoice()
        self.assertFalse(Invoice.objects.exists())
        self.assertEqual(AuditEvent.objects.count(), count)

    def test_bank_fee_and_two_dated_transfer_clear_only_their_observed_movements(self):
        fee = self.movement("-15000", reference="FEE")
        kwargs = dict(organization=self.org, actor=self.actor, transaction=fee, kind="bank_fee",
            amount="15000", reference="fee-a", evidence="Statement fee line reviewed")
        first = f.reconcile_bank_adjustment(**kwargs)
        self.assertEqual(first.pk, f.reconcile_bank_adjustment(**kwargs).pk)
        second_account = BankAccount.objects.create(organization=self.org, name="Second", account_number="SYN-02")
        outgoing = self.movement("-100000", reference="OUT")
        incoming = self.movement("100000", reference="IN", day=date(2026, 1, 11), account=second_account)
        f.reconcile_bank_transfer(organization=self.org, actor=self.actor, outgoing=outgoing,
            incoming=incoming, amount="100000", reference="transfer-a", evidence="Both statement references checked")
        self.assertEqual(f.pending_bank_transactions(organization=self.org).count(), 0)
        before_incoming = f.report_balances(organization=self.org, as_of=self.day)
        self.assertEqual(before_incoming["BANK_TRANSFER"], Decimal("100000"))
        final = f.report_balances(organization=self.org)
        self.assertEqual(final["BANK_TRANSFER"], 0)
        self.assertEqual(final["EXPENSE"], Decimal("15000"))
        self.assertEqual(final["BANK"], Decimal("-15000"))

    def test_reviewed_bank_opening_and_future_service_release_preserve_cutoff(self):
        opening = f.record_bank_opening(organization=self.org, actor=self.actor, account=self.account,
            date=self.day, amount="1000000", reference="opening-a", evidence="Opening statement reviewed by owner")
        self.assertEqual(opening.amount, Decimal("1000000"))
        with self.assertRaises(ValidationError):
            self.movement(day=self.day - timedelta(days=1))
        bill = f.create_bill(organization=self.org, supplier=self.supplier, number="FUTURE",
            amount="444000", supplier_vat="44000", date=self.day, service_date=date(2026, 2, 10), category="provider", actor=self.actor)
        bill = f.approve_bill(organization=self.org, bill=bill, actor=self.actor)
        before = f.report_balances(organization=self.org, as_of=date(2026, 1, 31))
        self.assertEqual(before["EXPENSE"], 0)
        self.assertEqual(before["PREPAID"], Decimal("444000"))
        with self.assertRaises(ValidationError):
            f.release_prepayment(organization=self.org, actor=self.actor, bill=bill, date=self.day,
                amount="444000", reference="too-early", evidence="No service yet")
        f.release_prepayment(organization=self.org, actor=self.actor, bill=bill, date=date(2026, 2, 10),
            amount="444000", reference="release-a", evidence="Entire supplier obligation completed")
        self.assertEqual(bill.prepaid_remaining, 0)
        self.assertEqual(f.report_balances(organization=self.org)["EXPENSE"], Decimal("444000"))
        with self.assertRaises(ValidationError):
            f.release_prepayment(organization=self.org, actor=self.actor, bill=bill, date=date(2026, 2, 10),
                amount="1", reference="over-release", evidence="Invalid oversubscription")

    def test_customer_advance_application_cancellation_and_two_refunds_keep_liabilities_exact(self):
        incoming = self.movement("600000")
        advance = f.record_customer_advance(organization=self.org, actor=self.actor, party=self.party,
            transaction=incoming, amount="600000", reference="advance-a", evidence="Reseller paid before invoice")
        inv = f.issue_invoice(organization=self.org, invoice=self.invoice(date=date(2026, 1, 11)), actor=self.actor)
        f.apply_customer_advance(organization=self.org, actor=self.actor, advance=advance, invoice=inv,
            date=date(2026, 1, 11), amount="500000", reference="apply-a", evidence="Same reseller order allocation")
        self.assertEqual(inv.outstanding_amount, 0)
        self.assertEqual(advance.remaining_amount, Decimal("100000"))
        f.cancel_invoice(organization=self.org, actor=self.actor, invoice=inv, date=date(2026, 1, 12), reason="Test cancelled before service")
        balances = f.report_balances(organization=self.org)
        self.assertEqual(balances["AR"], 0)
        self.assertEqual(balances["DEFERRED_REVENUE"], 0)
        self.assertEqual(balances["CUSTOMER_REFUND"], Decimal("-500000"))
        self.assertEqual(balances["CUSTOMER_ADVANCE"], Decimal("-100000"))
        refund = self.movement("-600000", day=date(2026, 1, 12), reference="REFUND")
        f.refund_customer_advance(organization=self.org, actor=self.actor, advance=advance, transaction=refund,
            amount="100000", reference="refund-advance", evidence="Unapplied advance returned")
        f.refund_cancelled_invoice(organization=self.org, actor=self.actor, invoice=inv, transaction=refund,
            amount="500000", reference="refund-invoice", evidence="Cancelled order returned")
        self.assertEqual(f.report_balances(organization=self.org)["BANK"], 0)
        self.assertEqual(f.report_balances(organization=self.org)["CUSTOMER_REFUND"], 0)
        self.assertEqual(f.report_balances(organization=self.org)["CUSTOMER_ADVANCE"], 0)
        self.assertEqual(f.pending_bank_transactions(organization=self.org).count(), 0)
        self.assertEqual(f.monthly_summary(organization=self.org, year=2026, month=1)["blockers"], [])

    def test_partial_cash_cancellation_keeps_historical_subledgers_and_refund_separate(self):
        inv = f.issue_invoice(organization=self.org, invoice=self.invoice(service_date=date(2026, 3, 10)), actor=self.actor)
        receipt = self.movement("200000")
        f.reconcile_receipt(organization=self.org, actor=self.actor, invoice=inv, transaction=receipt, amount="200000")
        f.cancel_invoice(organization=self.org, actor=self.actor, invoice=inv, date=date(2026, 2, 10), reason="Order cancelled")
        january = f.monthly_summary(organization=self.org, year=2026, month=1)
        self.assertEqual(january["balances"]["AR"], Decimal("300000"))
        self.assertEqual(january["blockers"], [])
        february = f.monthly_summary(organization=self.org, year=2026, month=2)
        self.assertEqual(february["balances"]["AR"], 0)
        self.assertEqual(february["balances"]["CUSTOMER_REFUND"], Decimal("-200000"))
        self.assertEqual(february["blockers"], [])

    def test_reviewed_withholding_full_net_payment_then_partial_tax_remittance(self):
        from pypdf import PdfWriter
        from taxes.services import upload_evidence, record_external_review, review_bill_tax
        from taxes.workflow import bill_decision
        writer, stream = PdfWriter(), BytesIO()
        writer.add_blank_page(width=100, height=100)
        writer.write(stream)
        evidence = upload_evidence(organization=self.org, actor=self.actor,
            file=SimpleUploadedFile("synthetic-report.pdf", stream.getvalue()))
        bill = f.create_bill(organization=self.org, actor=self.actor, supplier=self.supplier, amount="111000",
            supplier_vat="11000", date=self.day, service_date=self.day, number="WITHHELD")
        bill = f.approve_bill(organization=self.org, actor=self.actor, bill=bill)
        data = dict(rule_code="pph23_service_2", base=Decimal("100000"), period=date(2026, 1, 1),
                    reason="Synthetic reviewed service withholding", object_code="24-104-99")
        review = record_external_review(organization=self.org, actor=self.actor, subject=bill,
            decision=bill_decision(**data), evidence=evidence, professional_name="Synthetic professional",
            qualification_reference="Synthetic engagement", reviewed_on=self.day,
            statement="Synthetic reviewed decision", report_confirmed=True)
        decision = review_bill_tax(organization=self.org, actor=self.actor, bill=bill, external_review=review, **data)
        supplier_payment = self.movement("-109000", reference="NET")
        result = f.reconcile_withheld_bill_payment(organization=self.org, actor=self.actor, bill=bill, transaction=supplier_payment)
        self.assertEqual(result.withholding_amount, Decimal("2000"))
        self.assertEqual(f.reconcile_withheld_bill_payment(organization=self.org, actor=self.actor, bill=bill, transaction=supplier_payment).pk, result.pk)
        self.assertEqual(bill.outstanding_amount, 0)
        self.assertEqual(f.report_balances(organization=self.org)["AP"], 0)
        self.assertEqual(f.report_balances(organization=self.org)["TAX_PAYABLE"], Decimal("-2000"))
        remittance = self.movement("-2000", reference="TAX")
        f.reconcile_tax_remittance(organization=self.org, actor=self.actor, obligation=decision.obligation,
            transaction=remittance, amount="1000", reference="remit-1", evidence="Synthetic first tax payment")
        self.assertEqual(f.report_balances(organization=self.org)["TAX_PAYABLE"], Decimal("-1000"))
        with self.assertRaises(ValidationError):
            f.reconcile_tax_remittance(organization=self.org, actor=self.actor, obligation=decision.obligation,
                transaction=remittance, amount="1001", reference="over-remit", evidence="Rejected oversubscription")
        f.reconcile_tax_remittance(organization=self.org, actor=self.actor, obligation=decision.obligation,
            transaction=remittance, amount="1000", reference="remit-2", evidence="Synthetic remaining tax payment")
        self.assertEqual(f.report_balances(organization=self.org)["TAX_PAYABLE"], 0)
        self.assertEqual(f.pending_bank_transactions(organization=self.org).count(), 0)
        self.assertEqual(f.monthly_summary(organization=self.org, year=2026, month=1)["blockers"], [])
        self.assertIsNone(decision.obligation.payment_verified_at)

    def test_supplier_tax_decision_requires_controlled_tax_review_write(self):
        bill = f.create_bill(organization=self.org, actor=self.actor, supplier=self.supplier,
            amount="100000", date=self.day, service_date=self.day, number="UNREVIEWED")
        bill.tax_status, bill.tax_amount = "reviewed", Decimal("0")
        with self.assertRaises(ValidationError):
            bill.save()
        bill.refresh_from_db()
        self.assertEqual((bill.tax_status, bill.tax_amount), ("review", None))

    def test_new_bank_flows_reject_scope_roles_overallocation_and_changed_retry_atomically(self):
        fee = self.movement("-10000")
        base = dict(organization=self.org, actor=self.actor, transaction=fee, kind="bank_fee",
                    amount="8000", reference="guarded-fee", evidence="Synthetic reviewed fee")
        posting = f.reconcile_bank_adjustment(**base)
        audits, journals = AuditEvent.objects.count(), Journal.objects.count()
        other = Organization.objects.create(name="Other financial company")
        marketing = get_user_model().objects.create_user(username="finance-marketer")
        Membership.objects.create(organization=self.org, user=marketing, role="marketing")
        cases = [base | {"organization": other}, base | {"actor": marketing}, base | {"amount": "8001"},
                 base | {"amount": "2001", "reference": "exceeds-residual"}, base | {"kind": "ar_writeoff"}]
        for case in cases:
            with self.subTest(case=case["reference"]), self.assertRaises((PermissionDenied, ValidationError)):
                f.reconcile_bank_adjustment(**case)
        self.assertEqual((AuditEvent.objects.count(), Journal.objects.count()), (audits, journals))
        self.assertEqual(fee.unallocated_amount, Decimal("-2000"))
        posting.signed_amount = Decimal("-1")
        with self.assertRaises(ValidationError):
            posting.save()
        with self.assertRaises(ValidationError):
            posting.delete()

    def test_advance_cannot_cross_customer_or_service_date_and_replays_do_not_add_audit(self):
        advance = f.record_customer_advance(organization=self.org, actor=self.actor, party=self.party,
            transaction=self.movement(), amount="500000", reference="guarded-advance", evidence="Reviewed payer")
        another = Party.objects.create(organization=self.org, name="Another reseller", kind="reseller")
        inv = f.issue_invoice(organization=self.org, actor=self.actor, invoice=self.invoice(party=another))
        with self.assertRaises(ValidationError):
            f.apply_customer_advance(organization=self.org, actor=self.actor, advance=advance, invoice=inv,
                amount="500000", date=self.day, reference="wrong-payer", evidence="Must reject cross-customer")
        correct = f.issue_invoice(organization=self.org, actor=self.actor, invoice=self.invoice())
        data = dict(organization=self.org, actor=self.actor, advance=advance, invoice=correct, amount="500000",
            date=date(2026, 1, 11), reference="right-payer", evidence="Reviewed correct payer")
        first = f.apply_customer_advance(**data)
        audits = AuditEvent.objects.count()
        self.assertEqual(first.pk, f.apply_customer_advance(**data).pk)
        self.assertEqual(AuditEvent.objects.count(), audits)
        with self.assertRaises(ValidationError):
            f.record_delivery(organization=self.org, actor=self.actor, invoice=correct, date=self.day)
        f.record_delivery(organization=self.org, actor=self.actor, invoice=correct, date=date(2026, 1, 11))
        with self.assertRaises(ValidationError):
            f.cancel_invoice(organization=self.org, actor=self.actor, invoice=correct, date=date(2026, 1, 11), reason="After-service credits use different workflow")

    def test_bank_opening_requires_owner_and_closed_through_boundary(self):
        finance = get_user_model().objects.create_user(username="opening-finance")
        Membership.objects.create(organization=self.org, user=finance, role="finance")
        args = dict(organization=self.org, actor=finance, account=self.account, date=self.day,
                    amount="100000", reference="opening-reviewed", evidence="Synthetic opening reviewed")
        with self.assertRaises(PermissionDenied):
            f.record_bank_opening(**args)
        f.close_month(organization=self.org, actor=self.actor, year=2026, month=2)
        with self.assertRaises(ValidationError):
            f.record_bank_opening(**(args | {"actor": self.actor}))
        self.assertEqual(f.report_balances(organization=self.org)["BANK"], 0)

    def test_due_prepayment_must_be_reviewed_and_released_before_close(self):
        bill = f.create_bill(organization=self.org, actor=self.actor, supplier=self.supplier,
            amount="100000", date=self.day, service_date=self.day, category="prepayment", number="DUE-PREPAY")
        bill = f.approve_bill(organization=self.org, actor=self.actor, bill=bill)
        bill.tax_status, bill.tax_amount, bill._tax_service_transition = "reviewed", Decimal("0"), True
        bill.save()
        with self.assertRaises(ValidationError):
            f.close_month(organization=self.org, actor=self.actor, year=2026, month=1)
        f.release_prepayment(organization=self.org, actor=self.actor, bill=bill, amount="100000", date=self.day,
            reference="due-release", evidence="Service completion confirmed")
        f.close_month(organization=self.org, actor=self.actor, year=2026, month=1)
