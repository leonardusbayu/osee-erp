from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.utils import timezone

from core.models import AuditEvent, DomainEvent, Membership, Organization
from .models import AccountingPeriod, Allocation, BankAccount, BankTransaction, Bill, BillPayment, Invoice, Journal, JournalLine, Party, PriceVersion, Product
from .services import approve_bill, close_month, create_bill, create_invoice, import_bank_csv, issue_invoice, monthly_summary, pending_bank_transactions, post_journal, reconcile_bill_payment, reconcile_receipt, record_delivery, replace_price_version, report_balances


D = Decimal
DAY = date(2026, 1, 5)


class FinanceInvariantTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="OSEE Test", is_demo=True)
        self.other = Organization.objects.create(name="Other")
        self.owner = get_user_model().objects.create_user(username="finance-owner", password="test-only-password")
        Membership.objects.create(organization=self.org, user=self.owner, role="owner")
        self.reseller = Party.objects.create(organization=self.org, name="Mitra A", kind="reseller")
        self.customer = Party.objects.create(organization=self.org, name="Customer", kind="customer")
        self.supplier = Party.objects.create(organization=self.org, name="Provider", kind="supplier")
        self.product = Product.objects.create(organization=self.org, code="ITP", name="ITP", kind="itp", default_price=D("510000"))
        self.account = BankAccount.objects.create(organization=self.org, name="BNI Demo", account_number="TEST-001")

    def invoice(self, *, quantity=1, party=None, number="INV-001", service_date=DAY):
        return create_invoice(organization=self.org, actor=self.owner, party=party or self.reseller,
                              product=self.product, quantity=quantity, date=DAY,
                              service_date=service_date, number=number)

    def bank(self, amount="510000", reference="R1", day=DAY):
        return BankTransaction.objects.create(organization=self.org, account=self.account,
                                               date=day, reference=reference, amount=D(amount))

    def bill(self, amount="100000", **kwargs):
        return Bill.objects.create(organization=self.org, supplier=self.supplier,
                                    number="B-001", amount=D(amount), date=DAY, service_date=DAY, **kwargs)

    def test_wholesale_invoice_receipt_and_delivery_post_once(self):
        invoice = issue_invoice(organization=self.org, invoice=self.invoice(quantity=2), actor=self.owner)
        self.assertEqual(report_balances(organization=self.org)["DEFERRED_REVENUE"], D("-1020000"))
        bank = self.bank("1020000")
        first = reconcile_receipt(organization=self.org, invoice=invoice, transaction=bank, amount="1020000", actor=self.owner)
        second = reconcile_receipt(organization=self.org, invoice=invoice, transaction=bank, amount="1020000", actor=self.owner)
        self.assertEqual(first.pk, second.pk)
        record_delivery(organization=self.org, invoice=invoice, actor=self.owner)
        record_delivery(organization=self.org, invoice=invoice, actor=self.owner)
        issue_invoice(organization=self.org, invoice=invoice, actor=self.owner)
        balances = report_balances(organization=self.org)
        self.assertEqual(balances["BANK"], D("1020000"))
        self.assertEqual(balances["AR"], 0)
        self.assertEqual(balances["DEFERRED_REVENUE"], 0)
        self.assertEqual(balances["REVENUE"], D("-1020000"))
        self.assertEqual(sum(balances.values()), 0)
        self.assertEqual(Journal.objects.count(), 3)
        self.assertEqual(DomainEvent.objects.count(), 3)
        self.assertTrue(AuditEvent.objects.filter(action="finance.invoice.delivered").exists())

    def test_prepaid_reseller_cannot_deliver_on_partial_payment(self):
        invoice = issue_invoice(organization=self.org, invoice=self.invoice())
        bank = self.bank("100000")
        reconcile_receipt(organization=self.org, invoice=invoice, transaction=bank, amount="100000")
        with self.assertRaises(ValidationError):
            record_delivery(organization=self.org, invoice=invoice)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, "issued")
        self.assertEqual(report_balances(organization=self.org)["REVENUE"], 0)

    def test_price_snapshot_survives_product_price_change(self):
        price = PriceVersion.objects.create(organization=self.org, party=self.reseller, product=self.product,
                                             kind="selling", amount=D("520000"), effective_from=DAY)
        invoice = issue_invoice(organization=self.org, invoice=self.invoice())
        self.assertEqual(invoice.unit_price, D("520000"))
        self.assertEqual(invoice.price_version, price)
        self.product.default_price = D("600000")
        self.product.save()
        invoice.refresh_from_db()
        self.assertEqual(invoice.total, D("520000"))
        invoice.unit_price = D("1")
        with self.assertRaises(ValidationError):
            invoice.save()
        with self.assertRaises(ValidationError):
            Invoice.objects.filter(pk=invoice.pk).update(unit_price=1)
        price.amount = D("530000")
        with self.assertRaises(ValidationError):
            price.save()

    def test_price_overlap_and_cross_product_selection_are_rejected(self):
        PriceVersion.objects.create(organization=self.org, product=self.product, kind="selling", amount=D("500000"),
                                    effective_from=DAY, effective_to=date(2026, 1, 31))
        with self.assertRaises(ValidationError):
            PriceVersion.objects.create(organization=self.org, product=self.product, kind="selling", amount=D("510000"),
                                        effective_from=date(2026, 1, 31))
        other_product = Product.objects.create(organization=self.org, code="IBT", name="IBT", kind="ibt", default_price=D("3000000"))
        invoice = create_invoice(organization=self.org, party=self.reseller, product=other_product, quantity=1, date=DAY)
        self.assertEqual(invoice.total, D("3000000"))

    def test_cross_organization_references_are_rejected_by_models_and_services(self):
        foreign_party = Party.objects.create(organization=self.other, name="Foreign", kind="reseller")
        with self.assertRaises(ValidationError):
            create_invoice(organization=self.org, party=foreign_party, product=self.product, quantity=1, date=DAY)
        with self.assertRaises(ValidationError):
            Invoice.objects.create(organization=self.org, party=foreign_party, product=self.product,
                                   number="BAD", quantity=1, unit_price=D("1"), date=DAY, due_date=DAY, service_date=DAY)
        invoice = self.invoice()
        with self.assertRaises(ValidationError):
            issue_invoice(organization=self.other, invoice=invoice)
        foreign_account = BankAccount.objects.create(organization=self.other, name="Other", account_number="OTHER")
        with self.assertRaises(ValidationError):
            import_bank_csv(organization=self.org, account=foreign_account, content="date,reference,description,amount\n2026-01-05,X,Test,10\n")

    def test_csv_replay_is_safe_and_conflicting_duplicate_rolls_back_all_rows(self):
        content = b"date,reference,description,amount\n2026-01-05,R1,Receipt,510000.00\n2026-01-05,C1,Charge,-1000.00\n"
        self.assertEqual(import_bank_csv(organization=self.org, account=self.account, content=content), {"created": 2, "duplicates": 0})
        self.assertEqual(import_bank_csv(organization=self.org, account=self.account, content=content), {"created": 0, "duplicates": 2})
        conflict = "date,reference,description,amount\n2026-01-05,NEW,New,30\n2026-01-05,R1,Receipt,9\n"
        with self.assertRaises(ValidationError):
            import_bank_csv(organization=self.org, account=self.account, content=conflict)
        self.assertFalse(BankTransaction.objects.filter(reference="NEW").exists())
        self.assertEqual(BankTransaction.objects.count(), 2)
        self.assertEqual(Journal.objects.count(), 0)

    def test_csv_invalid_encoding_header_and_precision_rejected_atomically(self):
        cases = [b"date,reference,description,amount\n2026-01-05,A,\xff,10\n",
                 "reference,date,description,amount\nA,2026-01-05,B,10\n",
                 "date,reference,description,amount\n2026-01-05,A,B,1.001\n",
                 "date,reference,description,amount\n2026-01-05,A,B,NaN\n",
                 "date,reference,description,amount\n2026-01-05,A,B,0\n"]
        for content in cases:
            with self.subTest(content=content), self.assertRaises(ValidationError):
                import_bank_csv(organization=self.org, account=self.account, content=content)
        self.assertEqual(BankTransaction.objects.count(), 0)

    def test_split_receipts_and_allocation_limits(self):
        one = issue_invoice(organization=self.org, invoice=self.invoice(number="I1"))
        two = issue_invoice(organization=self.org, invoice=self.invoice(number="I2"))
        bank = self.bank("1020000")
        reconcile_receipt(organization=self.org, invoice=one, transaction=bank, amount="510000")
        reconcile_receipt(organization=self.org, invoice=two, transaction=bank, amount="510000")
        self.assertEqual(bank.unallocated_amount, 0)
        extra = self.bank("1", "R2")
        with self.assertRaises(ValidationError):
            reconcile_receipt(organization=self.org, invoice=one, transaction=extra, amount="1")
        with self.assertRaises(ValidationError):
            reconcile_receipt(organization=self.org, invoice=one, transaction=bank, amount="500000")
        self.assertEqual(Allocation.objects.count(), 2)

    def test_negative_bank_rows_remain_visible_and_cannot_be_customer_receipts(self):
        invoice = issue_invoice(organization=self.org, invoice=self.invoice())
        bank = self.bank("-1000")
        with self.assertRaises(ValidationError):
            reconcile_receipt(organization=self.org, invoice=invoice, transaction=bank, amount="1000")
        summary = monthly_summary(organization=self.org, year=2026, month=1)
        self.assertEqual(summary["bank_out"], 1000)
        self.assertEqual(summary["unmatched_count"], 1)
        self.assertEqual(summary["balances"]["BANK"], 0)
        with self.assertRaises(ValidationError):
            close_month(organization=self.org, year=2026, month=1)

    def test_posted_journal_and_lines_are_immutable(self):
        invoice = issue_invoice(organization=self.org, invoice=self.invoice())
        journal = Journal.objects.get(source_key=f"invoice:{invoice.pk}:issue")
        journal.description = "Changed"
        with self.assertRaises(ValidationError):
            journal.save()
        with self.assertRaises(ValidationError):
            journal.delete()
        line = journal.lines.first()
        line.debit = D("3")
        with self.assertRaises(ValidationError):
            line.save()
        with self.assertRaises(ValidationError):
            JournalLine.objects.create(organization=self.org, journal=journal, account="BANK", debit=D("1"))
        with self.assertRaises(ValidationError):
            journal.lines.all().delete()

    def test_unbalanced_and_conflicting_journal_sources_are_rejected(self):
        with self.assertRaises(ValidationError):
            post_journal(organization=self.org, date=DAY, source_key="bad", description="Bad", entries=[("BANK", D("2"), D("0")), ("AR", D("0"), D("1"))])
        self.assertEqual(Journal.objects.count(), 0)
        entries = [("BANK", D("1"), D("0")), ("AR", D("0"), D("1"))]
        first = post_journal(organization=self.org, date=DAY, source_key="unique", description="One", entries=entries)
        self.assertEqual(post_journal(organization=self.org, date=DAY, source_key="unique", description="One", entries=entries).pk, first.pk)
        with self.assertRaises(ValidationError):
            post_journal(organization=self.org, date=DAY, source_key="unique", description="Other", entries=[("BANK", D("2"), D("0")), ("AR", D("0"), D("2"))])

    def test_supplier_vat_is_included_in_cost_and_tax_review_not_invented(self):
        bill = approve_bill(organization=self.org, bill=self.bill("444000", supplier_vat=D("44000")))
        self.assertEqual(bill.tax_status, "review")
        self.assertIsNone(bill.tax_amount)
        self.assertEqual(report_balances(organization=self.org)["EXPENSE"], D("444000"))
        self.assertEqual(report_balances(organization=self.org)["AP"], D("-444000"))
        with self.assertRaises(ValidationError):
            close_month(organization=self.org, year=2026, month=1)

    def test_controlled_bill_creation_preserves_document_dates_and_requires_tax_review(self):
        bill = create_bill(organization=self.org, supplier=self.supplier, amount="444000",
                           supplier_vat="44000", date=DAY, service_date=DAY - timedelta(days=2),
                           category="provider", number="DOCUMENT-1", actor=self.owner)
        self.assertEqual(bill.amount, D("444000"))
        self.assertEqual(bill.service_date, DAY - timedelta(days=2))
        self.assertEqual((bill.status, bill.tax_status, bill.tax_amount), ("draft", "review", None))
        self.assertFalse(Journal.objects.exists())
        self.assertEqual(AuditEvent.objects.filter(action="finance.bill.created", object_id=str(bill.pk)).count(), 1)
        with self.assertRaises(ValidationError):
            create_bill(organization=self.org, supplier=self.supplier, amount="444000",
                        date=DAY, service_date=DAY, number="DOCUMENT-1", actor=self.owner)
        self.assertEqual(Bill.objects.count(), 1)

    def test_controlled_bill_creation_cannot_add_draft_to_closed_period(self):
        close_month(organization=self.org, year=2026, month=1, actor=self.owner)
        with self.assertRaisesMessage(ValidationError, "dikunci"):
            create_bill(organization=self.org, supplier=self.supplier, amount="1000",
                        date=DAY, service_date=DAY, number="CLOSED", actor=self.owner)
        self.assertFalse(Bill.objects.exists())
        self.assertFalse(AuditEvent.objects.filter(action="finance.bill.created").exists())

    def test_controlled_bill_creation_rejects_invalid_scope_role_amount_and_dates(self):
        foreign_supplier = Party.objects.create(organization=self.other, name="Other supplier", kind="supplier")
        auditor = get_user_model().objects.create_user(username="bill-auditor")
        Membership.objects.create(organization=self.org, user=auditor, role="auditor")
        base = {"organization": self.org, "supplier": self.supplier, "amount": "1000",
                "date": DAY, "service_date": DAY, "number": "INVALID", "actor": self.owner}
        for override in [{"supplier": foreign_supplier}, {"supplier": self.customer},
                         {"amount": "0"}, {"amount": 1.25}, {"amount": "NaN"},
                         {"supplier_vat": "1001"}, {"supplier_vat": "-1"},
                         {"date": "1999-12-31"}, {"service_date": "2201-01-01"}]:
            with self.subTest(override=override), self.assertRaises(ValidationError):
                create_bill(**(base | override))
        with self.assertRaises(PermissionDenied):
            create_bill(**(base | {"actor": auditor}))
        self.assertFalse(Bill.objects.exists())

    def test_pending_bank_query_sums_split_allocations_and_keeps_signed_remainders(self):
        positive = self.bank("1020001", "SPLIT-IN")
        for number in ["QUERY-I1", "QUERY-I2"]:
            invoice = issue_invoice(organization=self.org, invoice=self.invoice(number=number))
            reconcile_receipt(organization=self.org, invoice=invoice, transaction=positive, amount="510000")
        negative = self.bank("-200001", "SPLIT-OUT")
        for number in ["QUERY-B1", "QUERY-B2"]:
            bill = create_bill(organization=self.org, supplier=self.supplier, amount="100000",
                               date=DAY, service_date=DAY, number=number)
            bill = approve_bill(organization=self.org, bill=bill)
            bill.tax_status, bill.tax_amount = "reviewed", D("0")
            bill._tax_service_transition = True  # Synthetic reviewed decision fixture.
            bill.save()
            reconcile_bill_payment(organization=self.org, bill=bill, transaction=negative, amount="100000")
        settled = self.bank("1", "SETTLED")
        invoice = issue_invoice(organization=self.org, invoice=self.invoice(number="QUERY-I3"))
        reconcile_receipt(organization=self.org, invoice=invoice, transaction=settled, amount="1")
        untouched = self.bank("50", "UNMATCHED")
        foreign_account = BankAccount.objects.create(organization=self.other, name="Foreign", account_number="OTHER-QUERY")
        BankTransaction.objects.create(organization=self.other, account=foreign_account,
                                       reference="FOREIGN", date=DAY, amount=D("99"))
        pending = pending_bank_transactions(organization=self.org)
        with self.assertNumQueries(1):
            self.assertEqual(pending.count(), 3)
        rows = {row.pk: row for row in pending}
        self.assertEqual(set(rows), {positive.pk, negative.pk, untouched.pk})
        self.assertEqual(rows[positive.pk].receipt_total, D("1020000"))
        self.assertEqual(rows[negative.pk].payment_total, D("200000"))
        self.assertEqual(rows[positive.pk].remaining_amount, D("1"))
        self.assertEqual(rows[negative.pk].remaining_amount, D("-1"))
        self.assertEqual(rows[untouched.pk].remaining_amount, D("50"))
        for row in rows.values():
            self.assertEqual(row.remaining_amount, row.unallocated_amount)

    def test_supplier_payment_requires_review_and_zero_withholding(self):
        bill = approve_bill(organization=self.org, bill=self.bill())
        bank = self.bank("-100000")
        with self.assertRaises(ValidationError):
            reconcile_bill_payment(organization=self.org, bill=bill, transaction=bank, amount="100000")
        bill.tax_status = "reviewed"
        bill.tax_amount = D("2000")
        bill._tax_service_transition = True  # Synthetic reviewed decision fixture.
        bill.save()
        with self.assertRaises(ValidationError):
            reconcile_bill_payment(organization=self.org, bill=bill, transaction=bank, amount="100000")
        bill.tax_amount = D("0")
        bill._tax_service_transition = True  # Synthetic alternative decision fixture.
        bill.save()
        payment = reconcile_bill_payment(organization=self.org, bill=bill, transaction=bank, amount="100000")
        replay = reconcile_bill_payment(organization=self.org, bill=bill, transaction=bank, amount="100000")
        self.assertEqual(payment.pk, replay.pk)
        self.assertEqual(bank.unallocated_amount, 0)
        self.assertEqual(bill.outstanding_amount, 0)
        self.assertEqual(report_balances(organization=self.org)["AP"], 0)
        self.assertEqual(report_balances(organization=self.org)["BANK"], D("-100000"))
        self.assertEqual(BillPayment.objects.count(), 1)

    def test_closed_period_rejects_new_sources_and_posting_but_allows_exact_replay(self):
        invoice = issue_invoice(organization=self.org, invoice=self.invoice())
        csv_data = "date,reference,description,amount\n2026-01-05,R1,Paid,510000\n"
        import_bank_csv(organization=self.org, account=self.account, content=csv_data)
        bank = BankTransaction.objects.get(reference="R1")
        reconcile_receipt(organization=self.org, invoice=invoice, transaction=bank, amount="510000")
        record_delivery(organization=self.org, invoice=invoice)
        period = close_month(organization=self.org, year=2026, month=1, actor=self.owner)
        self.assertTrue(period.closed)
        self.assertTrue(monthly_summary(organization=self.org, year=2026, month=1)["closed"])
        self.assertEqual(import_bank_csv(organization=self.org, account=self.account, content=csv_data)["duplicates"], 1)
        with self.assertRaises(ValidationError):
            self.invoice(number="LOCKED")
        with self.assertRaises(ValidationError):
            import_bank_csv(organization=self.org, account=self.account, content=csv_data.replace("R1", "R2"))
        with self.assertRaises(ValidationError):
            post_journal(organization=self.org, date=DAY, source_key="backdate", description="Bad", entries=[("BANK", 1, 0), ("AR", 0, 1)])

    def test_roles_and_report_organization_scoping(self):
        reviewer = get_user_model().objects.create_user(username="reviewer")
        Membership.objects.create(organization=self.org, user=reviewer, role="reviewer")
        with self.assertRaises(PermissionDenied):
            create_invoice(organization=self.org, actor=reviewer, party=self.reseller, product=self.product, quantity=1, date=DAY)
        invoice = issue_invoice(organization=self.org, invoice=self.invoice())
        self.assertEqual(report_balances(organization=self.other)["AR"], 0)
        self.assertEqual(monthly_summary(organization=self.other, year=2026, month=1)["invoice_count"], 0)
        with self.assertRaises(PermissionDenied):
            issue_invoice(organization=self.other, invoice=invoice, actor=self.owner)

    def test_future_delivery_and_float_money_are_rejected(self):
        invoice = issue_invoice(organization=self.org, invoice=self.invoice(party=self.customer))
        with self.assertRaises(ValidationError):
            record_delivery(organization=self.org, invoice=invoice, date=timezone.localdate() + timedelta(days=1))
        with self.assertRaises(ValidationError):
            create_invoice(organization=self.org, party=self.customer, product=self.product, quantity=1, date=DAY, unit_price=510000.0)

    def test_acceptance_does_not_invent_cost_from_estimate(self):
        invoice = create_invoice(organization=self.org, party=self.customer, product=self.product, quantity=1,
                                  date=DAY, cost_estimate="444000")
        issue_invoice(organization=self.org, invoice=invoice)
        record_delivery(organization=self.org, invoice=invoice)
        self.assertEqual(report_balances(organization=self.org)["EXPENSE"], 0)

    def test_nonzero_bank_opening_requires_explicit_migration_workflow(self):
        with self.assertRaises(ValidationError):
            BankAccount.objects.create(organization=self.org, name="Invalid opening", account_number="BAD", opening_balance=D("10"))

    def test_owner_price_replacement_preserves_accepted_invoice_and_future_price(self):
        today = timezone.localdate()
        old = PriceVersion.objects.create(organization=self.org, product=self.product, party=self.reseller,
                                           kind="selling", amount=D("500000"), effective_from=DAY)
        invoice = issue_invoice(organization=self.org, invoice=self.invoice())
        replacement = replace_price_version(organization=self.org, actor=self.owner, price_version=old,
                                              amount="530000", effective_from=today, reason="Harga IIEF berubah")
        old.refresh_from_db()
        invoice.refresh_from_db()
        self.assertEqual(old.effective_to, today - timedelta(days=1))
        self.assertEqual(invoice.unit_price, D("500000"))
        self.assertEqual(invoice.price_version_id, old.pk)
        new_invoice = create_invoice(organization=self.org, actor=self.owner, party=self.reseller,
                                       product=self.product, quantity=1, date=today)
        self.assertEqual(new_invoice.unit_price, D("530000"))
        self.assertEqual(new_invoice.price_version_id, replacement.pk)
        self.assertTrue(AuditEvent.objects.filter(action="finance.price.replaced").exists())

    def test_price_replacement_rejects_backdate_missing_reason_and_nonowner(self):
        old = PriceVersion.objects.create(organization=self.org, product=self.product, kind="selling",
                                           amount=D("500000"), effective_from=DAY)
        args = dict(organization=self.org, price_version=old, amount="520000", reason="New price", actor=self.owner)
        with self.assertRaises(ValidationError):
            replace_price_version(**args, effective_from=timezone.localdate() - timedelta(days=1))
        with self.assertRaises(ValidationError):
            replace_price_version(**{**args, "reason": ""}, effective_from=timezone.localdate())
        finance_user = get_user_model().objects.create_user(username="price-finance")
        Membership.objects.create(organization=self.org, user=finance_user, role="finance")
        with self.assertRaises(PermissionDenied):
            replace_price_version(**{**args, "actor": finance_user}, effective_from=timezone.localdate())
        old.refresh_from_db()
        self.assertIsNone(old.effective_to)
        self.assertEqual(PriceVersion.objects.count(), 1)

    def test_unconfigured_product_cannot_create_zero_price_invoice(self):
        unconfigured = Product.objects.create(organization=self.org, code="UNKNOWN_IBT", name="IBT not configured",
                                               kind="ibt", default_price=D("0"))
        with self.assertRaises(ValidationError):
            create_invoice(organization=self.org, party=self.reseller, product=unconfigured, quantity=1, date=DAY)

    def test_posted_line_cannot_be_changed_via_stale_cached_draft_journal(self):
        journal = Journal.objects.create(organization=self.org, date=DAY, source_key="stale", description="Stale cache")
        line = JournalLine.objects.create(organization=self.org, journal=journal, account="BANK", debit=D("1"))
        JournalLine.objects.create(organization=self.org, journal=journal, account="AR", credit=D("1"))
        fresh = Journal.objects.get(pk=journal.pk)
        fresh.status = "posted"
        fresh._service_transition = True
        fresh.save()
        line.debit = D("2")
        with self.assertRaises(ValidationError):
            line.save()

    def test_completed_delivery_date_cannot_be_rewritten(self):
        invoice = issue_invoice(organization=self.org, invoice=self.invoice(party=self.customer))
        invoice = record_delivery(organization=self.org, invoice=invoice)
        invoice.delivered_at = DAY + timedelta(days=1)
        with self.assertRaises(ValidationError):
            invoice.save()
