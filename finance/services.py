"""Transactional posting and reconciliation for the first finance release.

An actor is required at the HTTP boundary. actor=None is reserved for trusted
management commands/tests; it must never be populated from a request value.
"""

import calendar
import csv
import io
import re
from datetime import date as Date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction as db_transaction
from django.db.models import DecimalField, ExpressionWrapper, F, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from core.audit import record
from core.models import DomainEvent, Membership, Organization
from .models import AccountingPeriod, Allocation, BankAccount, BankTransaction, Bill, BillPayment, Invoice, Journal, JournalLine, Party, PriceVersion, Product, ZERO, assert_open_period


CENT = Decimal("0.01")
CSV_HEADERS = ["date", "reference", "description", "amount"]
MAX_CSV_BYTES = 2 * 1024 * 1024
MAX_CSV_ROWS = 10000


def _date(value):
    if isinstance(value, Date) and not isinstance(value, datetime):
        return value
    try:
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError
        return Date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError("Tanggal harus valid dengan format YYYY-MM-DD.") from exc


def _amount(value, *, positive=True):
    if isinstance(value, (float, bool)):
        raise ValidationError("Gunakan jumlah desimal, bukan floating point.")
    try:
        result = Decimal(value)
        if not result.is_finite() or result != result.quantize(CENT):
            raise InvalidOperation
        if abs(result) >= Decimal("1000000000000000000"):
            raise InvalidOperation
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationError("Jumlah harus berupa angka dengan maksimal dua desimal.") from exc
    if positive and result <= ZERO:
        raise ValidationError("Jumlah harus lebih besar dari nol.")
    return result.quantize(CENT)


def _authorize(organization, actor, *, close=False):
    if actor is None:
        return
    allowed = ["owner", "reviewer"] if close else ["owner", "finance"]
    if not getattr(actor, "is_authenticated", False) or not Membership.objects.filter(
        organization=organization, user=actor, role__in=allowed
    ).exists():
        raise PermissionDenied("Anda tidak memiliki peran untuk tindakan ini.")


def _lock_org(organization):
    pk = getattr(organization, "pk", organization)
    try:
        return Organization.objects.select_for_update().get(pk=pk)
    except (Organization.DoesNotExist, ValueError, TypeError) as exc:
        raise ValidationError("Organisasi tidak ditemukan.") from exc


def _scoped(model, value, organization, *, lock=False):
    pk = getattr(value, "pk", value)
    queryset = model.objects.filter(organization=organization)
    if lock:
        queryset = queryset.select_for_update()
    try:
        return queryset.get(pk=pk)
    except (model.DoesNotExist, ValueError, TypeError) as exc:
        raise ValidationError("Catatan tidak tersedia untuk organisasi ini.") from exc


def _period(organization, posting_date):
    assert_open_period(organization.pk, posting_date)
    period, _ = AccountingPeriod.objects.get_or_create(
        organization=organization, year=posting_date.year, month=posting_date.month
    )
    period = AccountingPeriod.objects.select_for_update().get(pk=period.pk)
    if period.closed:
        raise ValidationError("Periode akuntansi sudah dikunci.")
    return period


def _event(organization, topic, obj, payload):
    DomainEvent.objects.get_or_create(
        organization=organization,
        idempotency_key=f"{topic}:{obj.pk}",
        defaults={"topic": topic, "version": 1, "aggregate_type": obj._meta.label_lower,
                  "aggregate_id": str(obj.pk), "payload": payload},
    )


def _post(organization, posting_date, source_key, description, entries):
    """Called only while the organization lock and outer transaction are held."""
    existing = Journal.objects.filter(organization=organization, source_key=source_key).first()
    if existing:
        if existing.status != "posted":
            raise ValidationError("Jurnal sumber belum selesai; perlu ditinjau.")
        expected = [(account, _amount(debit, positive=False), _amount(credit, positive=False)) for account, debit, credit in entries]
        actual = list(existing.lines.order_by("pk").values_list("account", "debit", "credit"))
        if existing.date != posting_date or actual != expected:
            raise ValidationError("Kunci jurnal sudah dipakai untuk isi yang berbeda.")
        return existing
    _period(organization, posting_date)
    journal = Journal.objects.create(organization=organization, date=posting_date, source_key=source_key, description=description)
    for account, debit, credit in entries:
        JournalLine.objects.create(organization=organization, journal=journal, account=account,
                                   debit=_amount(debit, positive=False), credit=_amount(credit, positive=False))
    journal.status = "posted"
    journal.posted_at = timezone.now()
    journal._service_transition = True
    journal.save()
    return journal


@db_transaction.atomic
def post_journal(*, organization, date, source_key, description, entries, actor=None):
    """Controlled accounting entry point; do not expose arbitrary journals to ordinary UI users."""
    organization = _lock_org(organization)
    _authorize(organization, actor, close=True)
    result = _post(organization, _date(date), source_key, description, entries)
    record(organization, actor, "finance.journal.posted", obj=result)
    return result


def _resolve_price(organization, party, product, pricing_date):
    versions = PriceVersion.objects.filter(
        organization=organization, product=product, kind="selling", effective_from__lte=pricing_date,
    ).filter(Q(effective_to__isnull=True) | Q(effective_to__gte=pricing_date))
    specific = list(versions.filter(party=party))
    generic = list(versions.filter(party__isnull=True))
    candidates = specific or generic
    if len(candidates) > 1:
        raise ValidationError("Beberapa versi harga berlaku. Tinjau harga sebelum membuat tagihan.")
    if candidates:
        return candidates[0].amount, candidates[0]
    # The product price is an explicit configured fallback, frozen on creation.
    return product.default_price, None


@db_transaction.atomic
def replace_price_version(*, organization, price_version, amount, effective_from, reason, actor=None):
    organization = _lock_org(organization)
    if actor is not None and not Membership.objects.filter(organization=organization, user=actor, role="owner").exists():
        raise PermissionDenied("Hanya pemilik yang dapat mengganti versi harga.")
    old = _scoped(PriceVersion, price_version, organization, lock=True)
    start = _date(effective_from)
    amount = _amount(amount)
    if not isinstance(reason, str) or not reason.strip():
        raise ValidationError("Alasan perubahan harga wajib diisi.")
    if start < timezone.localdate() or start <= old.effective_from:
        raise ValidationError("Harga pengganti berlaku paling awal hari ini dan setelah awal versi lama.")
    if old.effective_to is not None and start > old.effective_to:
        raise ValidationError("Tanggal pengganti berada di luar masa berlaku versi yang dipilih.")
    previous_end = old.effective_to
    for period in AccountingPeriod.objects.filter(organization=organization, closed=True):
        closed_start, closed_end = _month_bounds(period.year, period.month)
        if closed_end >= start and (previous_end is None or closed_start <= previous_end):
            raise ValidationError("Perubahan harga tidak boleh memengaruhi periode yang dikunci.")
    old.effective_to = start - timedelta(days=1)
    old._allow_price_end_change = True
    old.save()
    replacement = PriceVersion.objects.create(
        organization=organization, party=old.party, product=old.product, kind=old.kind,
        amount=amount, effective_from=start, effective_to=previous_end,
    )
    record(organization, actor, "finance.price.replaced", obj=replacement,
           detail={"old_version_id": old.pk, "new_version_id": replacement.pk,
                   "old_amount": str(old.amount), "new_amount": str(amount),
                   "old_end_before": previous_end.isoformat() if previous_end else None,
                   "old_end_after": old.effective_to.isoformat(), "new_start": start.isoformat(),
                   "reason": reason.strip()})
    return replacement


@db_transaction.atomic
def create_invoice(*, organization, party, product, quantity, date, due_date=None,
                   service_date=None, number=None, unit_price=None, cost_estimate=None,
                   override_reason="", actor=None):
    organization = _lock_org(organization)
    _authorize(organization, actor)
    party = _scoped(Party, party, organization)
    product = _scoped(Product, product, organization)
    invoice_date = _date(date)
    _period(organization, invoice_date)
    if isinstance(quantity, bool) or not re.fullmatch(r"[1-9]\d*", str(quantity)):
        raise ValidationError("Jumlah peserta harus bilangan bulat positif.")
    resolved, price_version = _resolve_price(organization, party, product, invoice_date)
    price = _amount(resolved if unit_price is None else unit_price)
    if price != resolved and not override_reason.strip():
        raise ValidationError("Harga khusus memerlukan alasan persetujuan.")
    if price != resolved:
        price_version = None
    if not number:
        sequence = Invoice.objects.filter(organization=organization).count() + 1
        number = f"INV-{invoice_date:%Y%m}-{sequence:05d}"
        while Invoice.objects.filter(organization=organization, number=number).exists():
            sequence += 1
            number = f"INV-{invoice_date:%Y%m}-{sequence:05d}"
    invoice = Invoice.objects.create(
        organization=organization, party=party, product=product, quantity=int(quantity),
        unit_price=price, price_version=price_version, number=number,
        date=invoice_date, due_date=_date(due_date or invoice_date),
        service_date=_date(service_date or invoice_date),
        cost_estimate=None if cost_estimate in (None, "") else _amount(cost_estimate, positive=False),
    )
    record(organization, actor, "finance.invoice.created", obj=invoice,
           detail={"total": str(invoice.total), "price_version_id": price_version.pk if price_version else None,
                   "override_reason": override_reason})
    return invoice


@db_transaction.atomic
def issue_invoice(*, organization, invoice, actor=None):
    organization = _lock_org(organization)
    _authorize(organization, actor)
    invoice = _scoped(Invoice, invoice, organization, lock=True)
    if invoice.status in ("issued", "delivered"):
        return invoice
    if invoice.status != "draft":
        raise ValidationError("Hanya draf dapat diterbitkan.")
    _post(organization, invoice.date, f"invoice:{invoice.pk}:issue", f"Tagihan {invoice.number}",
          [("AR", invoice.total, ZERO), ("DEFERRED_REVENUE", ZERO, invoice.total)])
    invoice.status = "issued"
    invoice._service_transition = True
    invoice.save()
    record(organization, actor, "finance.invoice.issued", obj=invoice, detail={"total": str(invoice.total)})
    _event(organization, "invoice.issued", invoice, {"invoice_id": invoice.pk, "total": str(invoice.total), "date": invoice.date.isoformat()})
    return invoice


@db_transaction.atomic
def record_delivery(*, organization, invoice, date=None, actor=None):
    organization = _lock_org(organization)
    _authorize(organization, actor)
    invoice = _scoped(Invoice, invoice, organization, lock=True)
    delivery_date = _date(date or invoice.service_date)
    if delivery_date > timezone.localdate():
        raise ValidationError("Tanggal penyelesaian aktual tidak boleh di masa depan.")
    if invoice.status == "delivered":
        if invoice.delivered_at != delivery_date:
            raise ValidationError("Penyelesaian sudah dicatat pada tanggal lain.")
        return invoice
    if invoice.status != "issued":
        raise ValidationError("Terbitkan tagihan sebelum mengakui layanan selesai.")
    if delivery_date < invoice.date:
        raise ValidationError("Tanggal layanan tidak boleh sebelum tanggal tagihan dalam alur ini.")
    paid_at_delivery = invoice.allocations.filter(transaction__date__lte=delivery_date).aggregate(value=Sum("amount"))["value"] or ZERO
    if invoice.party.kind == "reseller" and paid_at_delivery < invoice.total:
        raise ValidationError("Mitra harus melunasi pesanan sebelum tes. Selesaikan alokasi pembayaran.")
    _post(organization, delivery_date, f"invoice:{invoice.pk}:delivery", f"Layanan selesai {invoice.number}",
          [("DEFERRED_REVENUE", invoice.total, ZERO), ("REVENUE", ZERO, invoice.total)])
    invoice.status = "delivered"
    invoice.delivered_at = delivery_date
    invoice._service_transition = True
    invoice.save()
    record(organization, actor, "finance.invoice.delivered", obj=invoice, detail={"date": delivery_date.isoformat()})
    _event(organization, "invoice.delivered", invoice, {"invoice_id": invoice.pk, "total": str(invoice.total), "date": delivery_date.isoformat()})
    return invoice


def _parse_csv(content):
    if hasattr(content, "read"):
        content = content.read(MAX_CSV_BYTES + 1)
    if isinstance(content, bytes):
        if len(content) > MAX_CSV_BYTES:
            raise ValidationError("CSV maksimum 2 MB.")
        try:
            content = content.decode("utf-8-sig", errors="strict")
        except UnicodeDecodeError as exc:
            raise ValidationError("CSV harus menggunakan UTF-8.") from exc
    if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_CSV_BYTES:
        raise ValidationError("CSV harus berupa teks UTF-8 maksimum 2 MB.")
    if "\x00" in content:
        raise ValidationError("CSV mengandung karakter tidak valid.")
    reader = csv.DictReader(io.StringIO(content.lstrip("\ufeff")), strict=True)
    try:
        if reader.fieldnames != CSV_HEADERS:
            raise ValidationError("Header CSV harus persis: date,reference,description,amount")
        parsed = []
        for line_number, row in enumerate(reader, start=2):
            if line_number > MAX_CSV_ROWS + 1:
                raise ValidationError("CSV maksimum 10.000 baris data.")
            if None in row or any(row.get(key) is None for key in CSV_HEADERS):
                raise ValidationError(f"Kolom tidak lengkap pada baris {line_number}.")
            reference = row["reference"].strip()
            if not reference or len(reference) > 160 or len(row["description"]) > 500:
                raise ValidationError(f"Referensi/deskripsi tidak valid pada baris {line_number}.")
            if not re.fullmatch(r"-?\d+(?:\.\d{1,2})?", row["amount"].strip()):
                raise ValidationError(f"Jumlah baris {line_number} harus angka bertitik desimal tanpa pemisah ribuan.")
            amount = _amount(row["amount"].strip(), positive=False)
            if amount == ZERO:
                raise ValidationError(f"Jumlah mutasi baris {line_number} tidak boleh nol.")
            parsed.append({"date": _date(row["date"].strip()), "reference": reference,
                           "description": row["description"], "amount": amount})
    except csv.Error as exc:
        raise ValidationError("Struktur CSV tidak valid.") from exc
    if not parsed:
        raise ValidationError("CSV tidak memiliki data mutasi.")
    return parsed


@db_transaction.atomic
def import_bank_csv(*, organization, account, content, actor=None):
    organization = _lock_org(organization)
    _authorize(organization, actor)
    account = _scoped(BankAccount, account, organization, lock=True)
    rows = _parse_csv(content)
    created = duplicates = 0
    for row in rows:
        existing = BankTransaction.objects.filter(organization=organization, account=account, reference=row["reference"]).first()
        if existing:
            if any(getattr(existing, key) != value for key, value in row.items()):
                raise ValidationError(f"Referensi {row['reference']} sudah ada dengan isi berbeda. Seluruh impor dibatalkan.")
            duplicates += 1
            continue
        _period(organization, row["date"])
        BankTransaction.objects.create(organization=organization, account=account, **row)
        created += 1
    result = {"created": created, "duplicates": duplicates}
    record(organization, actor, "finance.bank.imported", obj=account, detail=result)
    return result


@db_transaction.atomic
def reconcile_receipt(*, organization, invoice, transaction, amount, actor=None):
    organization = _lock_org(organization)
    _authorize(organization, actor)
    invoice = _scoped(Invoice, invoice, organization, lock=True)
    bank_line = _scoped(BankTransaction, transaction, organization, lock=True)
    amount = _amount(amount)
    if bank_line.date > timezone.localdate():
        raise ValidationError("Mutasi bank aktual tidak boleh bertanggal di masa depan.")
    existing = Allocation.objects.filter(organization=organization, invoice=invoice, transaction=bank_line).first()
    if existing:
        if existing.amount != amount:
            raise ValidationError("Pasangan mutasi/tagihan sudah dialokasikan dengan jumlah berbeda.")
        return existing
    if invoice.status not in ("issued", "delivered"):
        raise ValidationError("Penerimaan harus dialokasikan ke tagihan terbit.")
    if bank_line.amount <= ZERO:
        raise ValidationError("Mutasi keluar tidak dapat menjadi penerimaan pelanggan.")
    if bank_line.date < invoice.date:
        raise ValidationError("Penerimaan sebelum invoice memerlukan alur uang muka; belum didukung dalam alokasi ini.")
    if amount > bank_line.unallocated_amount or amount > invoice.outstanding_amount:
        raise ValidationError("Alokasi melebihi sisa mutasi atau sisa tagihan.")
    _period(organization, bank_line.date)
    allocation = Allocation(organization=organization, invoice=invoice, transaction=bank_line, amount=amount)
    allocation._service_transition = True
    allocation.save()
    _post(organization, bank_line.date, f"allocation:{allocation.pk}", f"Penerimaan {invoice.number}",
          [("BANK", amount, ZERO), ("AR", ZERO, amount)])
    record(organization, actor, "finance.receipt.allocated", obj=allocation,
           detail={"invoice_id": invoice.pk, "bank_transaction_id": bank_line.pk, "amount": str(amount)})
    _event(organization, "payment.allocated", allocation, {"invoice_id": invoice.pk, "transaction_id": bank_line.pk, "amount": str(amount)})
    return allocation


@db_transaction.atomic
def create_bill(*, organization, supplier, amount, supplier_vat=ZERO, date,
                service_date, category="expense", number, actor=None):
    """Record a supplier document without inventing its withholding treatment."""
    organization = _lock_org(organization)
    _authorize(organization, actor)
    supplier = _scoped(Party, supplier, organization)
    bill_date, performed_date = _date(date), _date(service_date)
    # Suppliers may invoice after the service; only the bill's posting period
    # must be open. Both dates stay within the supported reporting horizon.
    _month_bounds(bill_date.year, bill_date.month)
    _month_bounds(performed_date.year, performed_date.month)
    _period(organization, bill_date)
    gross_amount = _amount(amount)
    observed_vat = _amount(supplier_vat, positive=False)
    if observed_vat < ZERO or observed_vat > gross_amount:
        raise ValidationError({"supplier_vat": "PPN pemasok harus antara nol dan total tagihan bruto."})
    bill = Bill.objects.create(
        organization=organization, supplier=supplier, number=number,
        amount=gross_amount, supplier_vat=observed_vat, date=bill_date,
        service_date=performed_date, category=category, status="draft",
        tax_status="review", tax_amount=None,
    )
    record(organization, actor, "finance.bill.created", obj=bill,
           detail={"gross_amount": str(bill.amount),
                   "supplier_vat_included": str(bill.supplier_vat),
                   "tax_status": bill.tax_status})
    return bill


@db_transaction.atomic
def approve_bill(*, organization, bill, actor=None):
    organization = _lock_org(organization)
    _authorize(organization, actor)
    bill = _scoped(Bill, bill, organization, lock=True)
    if bill.status == "approved":
        return bill
    if bill.status != "draft":
        raise ValidationError("Tagihan pembelian tidak dapat disahkan.")
    # Non-PKP supported workflow: amount already includes supplier VAT.
    # Tax review remains independent; no guessed withholding or VAT credit.
    expense_account = "PREPAID" if bill.category == "prepayment" else "EXPENSE"
    _post(organization, bill.date, f"bill:{bill.pk}:approval", f"Pembelian {bill.number}",
          [(expense_account, bill.amount, ZERO), ("AP", ZERO, bill.amount)])
    bill.status = "approved"
    bill._service_transition = True
    bill.save()
    record(organization, actor, "finance.bill.approved", obj=bill, detail={"gross_amount": str(bill.amount), "supplier_vat_included": str(bill.supplier_vat), "tax_status": bill.tax_status})
    _event(organization, "bill.approved", bill, {"bill_id": bill.pk, "gross_amount": str(bill.amount), "tax_status": bill.tax_status})
    return bill


@db_transaction.atomic
def reconcile_bill_payment(*, organization, bill, transaction, amount, actor=None):
    """Reconcile existing settled bank evidence; never initiate a bank payment."""
    organization = _lock_org(organization)
    _authorize(organization, actor)
    bill = _scoped(Bill, bill, organization, lock=True)
    bank_line = _scoped(BankTransaction, transaction, organization, lock=True)
    amount = _amount(amount)
    if bank_line.date > timezone.localdate():
        raise ValidationError("Mutasi bank aktual tidak boleh bertanggal di masa depan.")
    existing = BillPayment.objects.filter(organization=organization, bill=bill, transaction=bank_line).first()
    if existing:
        if existing.amount != amount:
            raise ValidationError("Pasangan mutasi/tagihan sudah memiliki alokasi berbeda.")
        return existing
    if bill.status != "approved":
        raise ValidationError("Sahkan tagihan pembelian sebelum rekonsiliasi pembayaran.")
    if bill.tax_status != "reviewed" or bill.tax_amount is None:
        raise ValidationError("Tinjauan pajak dan penetapan nominal potongan wajib diselesaikan dahulu.")
    if bill.tax_amount != ZERO:
        raise ValidationError("Tagihan dengan pemotongan pajak memerlukan alur penyelesaian pajak tersendiri; belum didukung di sini.")
    if bank_line.amount >= ZERO:
        raise ValidationError("Pilih mutasi bank keluar untuk pembayaran pemasok.")
    if bank_line.date < bill.date:
        raise ValidationError("Pembayaran sebelum tagihan memerlukan alur uang muka pemasok.")
    if amount > abs(bank_line.unallocated_amount) or amount > bill.outstanding_amount:
        raise ValidationError("Pembayaran melebihi sisa mutasi keluar atau sisa tagihan.")
    _period(organization, bank_line.date)
    payment = BillPayment(organization=organization, bill=bill, transaction=bank_line, amount=amount)
    payment._service_transition = True
    payment.save()
    _post(organization, bank_line.date, f"bill-payment:{payment.pk}", f"Pembayaran {bill.number}",
          [("AP", amount, ZERO), ("BANK", ZERO, amount)])
    record(organization, actor, "finance.bill.payment_reconciled", obj=payment,
           detail={"bill_id": bill.pk, "bank_transaction_id": bank_line.pk, "amount": str(amount)})
    _event(organization, "bill.payment_allocated", payment, {"bill_id": bill.pk, "transaction_id": bank_line.pk, "amount": str(amount)})
    return payment


def pending_bank_transactions(*, organization):
    """Return every unresolved signed movement without multiplying joined sums."""
    money = DecimalField(max_digits=20, decimal_places=2)
    receipts = Allocation.objects.filter(
        organization=organization, transaction_id=OuterRef("pk")
    ).order_by().values("transaction_id").annotate(total=Sum("amount")).values("total")
    payments = BillPayment.objects.filter(
        organization=organization, transaction_id=OuterRef("pk")
    ).order_by().values("transaction_id").annotate(total=Sum("amount")).values("total")
    return BankTransaction.objects.filter(organization=organization).annotate(
        receipt_total=Coalesce(Subquery(receipts, output_field=money), Value(ZERO), output_field=money),
        payment_total=Coalesce(Subquery(payments, output_field=money), Value(ZERO), output_field=money),
    ).annotate(
        remaining_amount=ExpressionWrapper(
            F("amount") - F("receipt_total") + F("payment_total"), output_field=money
        ),
    ).exclude(remaining_amount=ZERO)


def report_balances(*, organization, as_of=None):
    queryset = JournalLine.objects.filter(organization=organization, journal__organization=organization, journal__status="posted")
    if as_of is not None:
        queryset = queryset.filter(journal__date__lte=_date(as_of))
    balances = {key: ZERO for key in JournalLine.Account.values}
    for row in queryset.values("account").annotate(debit=Sum("debit"), credit=Sum("credit")):
        balances[row["account"]] = row["debit"] - row["credit"]
    return balances


def _month_bounds(year, month):
    try:
        year, month = int(year), int(month)
        if not 2000 <= year <= 2200:
            raise ValueError
        return Date(year, month, 1), Date(year, month, calendar.monthrange(year, month)[1])
    except (ValueError, TypeError, calendar.IllegalMonthError) as exc:
        raise ValidationError("Periode tahun/bulan tidak valid.") from exc


def _close_blockers(organization, end):
    blockers = []
    if Invoice.objects.filter(organization=organization, date__lte=end, status="draft").exists():
        blockers.append("Masih ada draf tagihan penjualan pada atau sebelum periode ini.")
    if Invoice.objects.filter(organization=organization, status="issued", service_date__lte=end).exists():
        blockers.append("Layanan jatuh tempo belum dikonfirmasi selesai atau dijadwal ulang.")
    if Bill.objects.filter(organization=organization, date__lte=end, status="draft").exists():
        blockers.append("Masih ada draf tagihan pembelian.")
    if Bill.objects.filter(organization=organization, date__lte=end).exclude(tax_status="reviewed").exists():
        blockers.append("Tinjauan pajak tagihan pembelian belum selesai.")
    if Bill.objects.filter(organization=organization, date__lte=end, status="approved", tax_status="reviewed").filter(Q(tax_amount__isnull=True) | Q(tax_amount__gt=0)).exists():
        blockers.append("Nominal potongan pajak belum ditetapkan nol atau memerlukan alur penyelesaian pajak yang belum didukung.")
    if pending_bank_transactions(organization=organization).filter(date__lte=end).exists():
        blockers.append("Ada mutasi bank masuk/keluar yang belum direkonsiliasi.")
    if Journal.objects.filter(organization=organization, date__lte=end, status="draft").exists():
        blockers.append("Ada jurnal draf yang belum diposting.")
    for journal in Journal.objects.filter(organization=organization, date__lte=end, status="posted"):
        totals = journal.lines.aggregate(debit=Sum("debit"), credit=Sum("credit"))
        if journal.lines.count() < 2 or not totals["debit"] or totals["debit"] != totals["credit"]:
            blockers.append("Ditemukan jurnal tidak seimbang.")
            break
    balances = report_balances(organization=organization, as_of=end)
    invoiced = sum((i.total for i in Invoice.objects.filter(organization=organization, date__lte=end, status__in=["issued", "delivered"])), ZERO)
    allocated = Allocation.objects.filter(organization=organization, transaction__date__lte=end).aggregate(value=Sum("amount"))["value"] or ZERO
    bills = Bill.objects.filter(organization=organization, status="approved", date__lte=end).aggregate(value=Sum("amount"))["value"] or ZERO
    bill_payments = BillPayment.objects.filter(organization=organization, transaction__date__lte=end).aggregate(value=Sum("amount"))["value"] or ZERO
    if balances["AR"] != invoiced - allocated:
        blockers.append("Saldo piutang tidak sama dengan subledger tagihan/alokasi.")
    if balances["AP"] != -(bills - bill_payments):
        blockers.append("Saldo utang tidak sama dengan subledger tagihan pembelian.")
    recognized = sum((i.total for i in Invoice.objects.filter(organization=organization, delivered_at__lte=end, status="delivered")), ZERO)
    if balances["DEFERRED_REVENUE"] != -(invoiced - recognized):
        blockers.append("Saldo pendapatan diterima di muka tidak sama dengan kewajiban layanan.")
    return blockers


def monthly_summary(*, organization, year, month):
    start, end = _month_bounds(year, month)
    rows = JournalLine.objects.filter(organization=organization, journal__organization=organization, journal__status="posted", journal__date__range=(start, end))
    movements = {key: ZERO for key in JournalLine.Account.values}
    for row in rows.values("account").annotate(debit=Sum("debit"), credit=Sum("credit")):
        movements[row["account"]] = row["debit"] - row["credit"]
    bank = BankTransaction.objects.filter(organization=organization, date__range=(start, end))
    invoices = Invoice.objects.filter(organization=organization, date__range=(start, end)).exclude(status__in=["draft", "cancelled"])
    return {
        "year": start.year, "month": start.month, "start": start, "end": end,
        "revenue": -movements["REVENUE"], "expenses": movements["EXPENSE"],
        "profit": -movements["REVENUE"] - movements["EXPENSE"],
        "invoiced": sum((i.total for i in invoices), ZERO), "invoice_count": invoices.count(),
        "bank_in": bank.filter(amount__gt=0).aggregate(value=Sum("amount"))["value"] or ZERO,
        "bank_out": -(bank.filter(amount__lt=0).aggregate(value=Sum("amount"))["value"] or ZERO),
        "unmatched_count": pending_bank_transactions(organization=organization).filter(date__range=(start, end)).count(),
        "balances": report_balances(organization=organization, as_of=end),
        "blockers": _close_blockers(organization, end),
        "closed": AccountingPeriod.objects.filter(organization=organization, year=start.year, month=start.month, closed=True).exists(),
        "basis": "Akuntansi komersial; bukan peredaran bruto pajak atau bukti kas lengkap.",
    }


@db_transaction.atomic
def close_month(*, organization, year, month, actor=None):
    organization = _lock_org(organization)
    _authorize(organization, actor, close=True)
    start, end = _month_bounds(year, month)
    if end >= timezone.localdate():
        raise ValidationError("Tutup bulan hanya tersedia setelah bulan tersebut berakhir.")
    current = AccountingPeriod.objects.filter(organization=organization, year=start.year, month=start.month).first()
    if current and current.closed:
        return current
    blockers = _close_blockers(organization, end)
    if blockers:
        raise ValidationError(blockers)
    period = _period(organization, start)
    period.closed = True
    period.closed_at = timezone.now()
    period.save()
    record(organization, actor, "finance.month.closed", obj=period,
           detail={"year": start.year, "month": start.month,
                   "balances": {key: str(value) for key, value in report_balances(organization=organization, as_of=end).items()}})
    return period
