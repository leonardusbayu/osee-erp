"""Read-only, dated commercial facts for a reviewed monthly fiscal bridge.

This is deliberately not a tax calculator. Each REVENUE line is a candidate;
cash and source observations remain context. Tax/profile/legal decisions and
own-tax postings are excluded so generating or paying tax cannot self-stale
the income-source fingerprint. No external service is called.
"""

import calendar
import hashlib
import json
import re
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from core.models import Organization
from finance.models import (
    AdvanceApplication,
    Allocation,
    CreditRefund,
    CustomerAdvance,
    CustomerRefund,
    CustomerTaxSettlement,
    Invoice,
    InvoiceCancellation,
    InvoiceCredit,
    InvoiceDelivery,
    Journal,
    JournalLine,
    StatementPeriodReview,
)

EXTRACTION_VERSION = "monthly-finance-sources-v2"
ZERO = Decimal("0.00")
HARD_ISSUES = frozenset(
    {
        "source_scope",
        "source_journal_missing",
        "source_journal_date",
        "source_journal_key",
        "source_journal_unbalanced",
        "source_amount_mismatch",
        "source_ledger_mismatch",
        "revenue_multiline",
        "revenue_duplicate_source",
        "evidence_integrity",
        "future_actual_source",
        "supplier_advance_integrity",
        "opening_balance_integrity",
    }
)
ISSUE_CODES = HARD_ISSUES | {
    "orphan_revenue",
    "revenue_debit",
    "source_observation",
    "inventory_unknown",
}


def _money(value):
    return format(Decimal(value).quantize(Decimal("0.01")), ".2f")


def _digest(value):
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _period(value):
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}", value):
        try:
            value = date.fromisoformat(value + "-01")
        except ValueError as exc:
            raise ValidationError(
                "Pilih masa dalam bentuk YYYY-MM atau tanggal pertama bulan."
            ) from exc
    if (
        isinstance(value, datetime)
        or not isinstance(value, date)
        or value.day != 1
        or not 2000 <= value.year <= 2200
    ):
        raise ValidationError(
            "Pilih masa dalam bentuk YYYY-MM atau tanggal pertama bulan."
        )
    return value, date(
        value.year, value.month, calendar.monthrange(value.year, value.month)[1]
    )


def _observed_date(value):
    """An upload observation may describe a future date; no actual posting is inferred."""
    from webapp.statement_import import parse_date

    try:
        return parse_date(value)
    except (ValidationError, ValueError, TypeError):
        if isinstance(value, str):
            for pattern in (
                "%Y-%m-%d",
                "%Y/%m/%d",
                "%d/%m/%Y",
                "%d-%m-%Y",
                "%Y-%m-%dT%H:%M:%S",
            ):
                try:
                    return datetime.strptime(value.strip(), pattern).date()
                except ValueError:
                    continue
    return None


class _Extraction:
    def __init__(self, organization, start, end):
        self.org, self.start, self.end = organization, start, end
        self.rows, self.issues, self.manifest = [], [], []
        self.matches = defaultdict(list)
        self.invoice_rows = {}
        self.journals = {
            row.pk: row
            for row in Journal.objects.filter(
                organization=organization, date__lte=end
            ).order_by("pk")
        }
        self.by_key = {row.source_key: row for row in self.journals.values()}
        self.invoice_objects = {}
        self.lines = defaultdict(list)
        for row in JournalLine.objects.filter(journal_id__in=self.journals).order_by(
            "pk"
        ):
            self.lines[row.journal_id].append(row)

    def issue(self, code, detail, row_id=None):
        item = {
            "code": code,
            "row_id": row_id,
            "detail": detail,
            "hard_blocker": code in HARD_ISSUES,
        }
        if item not in self.issues:
            self.issues.append(item)

    def actual_date(self, day, row_id):
        if day > timezone.localdate():
            self.issue(
                "future_actual_source",
                "Kejadian aktual bertanggal di masa depan; perbaiki sumber sebelum pemeriksaan.",
                row_id,
            )

    def journal(self, row, journal, *, key=None, expected=None):
        """Bind a fact to all journal lines, not just one source-key match."""
        row_id = row["row_id"]
        if (
            journal is None
            or journal.organization_id != self.org.pk
            or journal.status != "posted"
        ):
            self.issue(
                "source_journal_missing",
                "Sumber belum mempunyai jurnal terposting yang sesuai perusahaan.",
                row_id,
            )
            return
        row["journal_id"], row["source_key"] = journal.pk, journal.source_key
        if journal.date.isoformat() != row["date"]:
            self.issue(
                "source_journal_date",
                "Tanggal jurnal berbeda dengan tanggal kejadian sumber.",
                row_id,
            )
        if key and journal.source_key != key:
            self.issue(
                "source_journal_key",
                "Identitas jurnal berbeda dengan sumber kejadian.",
                row_id,
            )
        lines = self.lines[journal.pk]
        if any(line.organization_id != self.org.pk for line in lines):
            self.issue(
                "source_scope",
                "Baris jurnal tidak sesuai ruang lingkup perusahaan.",
                row_id,
            )
        scoped = [line for line in lines if line.organization_id == self.org.pk]
        row["journal_lines"] = [
            {
                "id": line.pk,
                "account": line.account,
                "debit": _money(line.debit),
                "credit": _money(line.credit),
            }
            for line in scoped
        ]
        if len(scoped) < 2 or sum((line.debit for line in scoped), ZERO) != sum(
            (line.credit for line in scoped), ZERO
        ):
            self.issue(
                "source_journal_unbalanced",
                "Seluruh baris jurnal sumber harus lengkap dan seimbang.",
                row_id,
            )
        if expected is not None:
            actual = defaultdict(lambda: [ZERO, ZERO])
            for line in scoped:
                actual[line.account][0] += line.debit
                actual[line.account][1] += line.credit
            wanted = {
                account: [debit, credit]
                for account, debit, credit in expected
                if debit or credit
            }
            if dict(actual) != wanted:
                self.issue(
                    "source_ledger_mismatch",
                    "Jumlah dan akun jurnal berbeda dengan kejadian sumber.",
                    row_id,
                )
        self.matches[journal.pk].append(row)

    def context(
        self,
        kind,
        obj,
        *,
        amount,
        invoice_id=None,
        cash=ZERO,
        gross=None,
        expected=None,
        key=None,
        **facts,
    ):
        row = {
            "row_id": f"{kind}:{obj.pk}",
            "source_kind": kind,
            "source_id": obj.pk,
            "role": "context",
            "date": obj.date.isoformat(),
            "amount": _money(amount),
            "invoice_id": invoice_id,
            "journal_id": None,
            "source_key": None,
            "cash_amount": _money(cash),
            "gross_amount": _money(amount if gross is None else gross),
            "reference": obj.reference,
            "evidence": obj.evidence,
            "period_position": "current" if obj.date >= self.start else "carried",
            **facts,
        }
        self.actual_date(obj.date, row["row_id"])
        if invoice_id is not None:
            self.check_invoice(invoice_id, obj.date, row["row_id"])
        self.journal(row, self.journals.get(obj.journal_id), expected=expected, key=key)
        self.rows.append(row)
        return row

    def bank(self, bank, row_id):
        if (
            bank.organization_id != self.org.pk
            or bank.account.organization_id != self.org.pk
        ):
            self.issue(
                "source_scope",
                "Bukti uang bank berasal dari ruang lingkup berbeda.",
                row_id,
            )
            return None
        self.actual_date(bank.date, row_id)
        return {
            "id": bank.pk,
            "account_id": bank.account_id,
            "date": bank.date.isoformat(),
            "reference": bank.reference,
            "amount": _money(bank.amount),
        }

    def file_evidence(self, kind, obj, *, declared_size=None, **facts):
        manifest = {
            "kind": kind,
            "id": obj.pk,
            "sha256": obj.sha256,
            "size": declared_size,
            **facts,
        }
        try:
            digest, size = hashlib.sha256(), 0
            with obj.file.open("rb") as source:
                for chunk in iter(lambda: source.read(65536), b""):
                    size += len(chunk)
                    digest.update(chunk)
            manifest.update(
                actual_sha256=digest.hexdigest(),
                actual_size=size,
                integrity="verified"
                if obj.sha256 == digest.hexdigest()
                and (declared_size is None or size == declared_size)
                else "mismatch",
            )
        except (OSError, ValueError):
            manifest.update(
                actual_sha256=None, actual_size=None, integrity="unavailable"
            )
        if manifest["integrity"] != "verified":
            self.issue(
                "evidence_integrity",
                "Berkas sumber hilang atau berbeda dengan hash/ukuran yang tersimpan.",
                f"{kind}:{obj.pk}",
            )
        self.manifest.append(manifest)

    def invoices(self):
        # Current mutable status/delivered_at are intentionally not snapshotted.
        # State below comes from dated immutable journals/events as of month end.
        for inv in (
            Invoice.objects.filter(organization=self.org, date__lte=self.end)
            .select_related("party", "product")
            .order_by("pk")
        ):
            self.invoice_objects[inv.pk] = inv
            scope_ok = (
                inv.party.organization_id == self.org.pk
                and inv.product.organization_id == self.org.pk
            )
            if not scope_ok:
                self.issue(
                    "source_scope",
                    "Invoice memiliki pihak atau produk di luar perusahaan.",
                    f"invoice:{inv.pk}",
                )
            self.invoice_rows[inv.pk] = {
                "id": inv.pk,
                "number": inv.number,
                "party_id": inv.party_id if scope_ok else None,
                "product_id": inv.product_id if scope_ok else None,
                "date": inv.date.isoformat(),
                "due_date": inv.due_date.isoformat(),
                "service_date": inv.service_date.isoformat(),
                "quantity": inv.quantity,
                "unit_price": _money(inv.unit_price),
                "total": _money(inv.total),
                "price_version_id": inv.price_version_id,
                "issued": False,
                "delivered_quantity": 0,
                "credited_quantity": 0,
                "cancelled": False,
                "unfulfilled_credit_quantity": 0,
                "refund_liability": "0.00",
                "gross_settled": "0.00",
                "cash_received": "0.00",
                "credited_amount": "0.00",
            }
            issue = self.by_key.get(f"invoice:{inv.pk}:issue")
            if issue:
                row = {
                    "row_id": f"invoice-issue:{inv.pk}",
                    "source_kind": "invoice_issue",
                    "source_id": inv.pk,
                    "role": "context",
                    "date": inv.date.isoformat(),
                    "amount": _money(inv.total),
                    "invoice_id": inv.pk,
                    "cash_amount": "0.00",
                    "gross_amount": _money(inv.total),
                    "journal_id": None,
                    "source_key": None,
                    "period_position": "current"
                    if inv.date >= self.start
                    else "carried",
                }
                self.actual_date(inv.date, row["row_id"])
                self.journal(
                    row,
                    issue,
                    key=f"invoice:{inv.pk}:issue",
                    expected=[
                        ("AR", inv.total, ZERO),
                        ("DEFERRED_REVENUE", ZERO, inv.total),
                    ],
                )
                self.rows.append(row)
                self.invoice_rows[inv.pk]["issued"] = True

    def events(self):
        scoped = {"organization": self.org, "date__lte": self.end}
        for obj in InvoiceDelivery.objects.filter(**scoped).order_by("pk"):
            row = self.context(
                "delivery",
                obj,
                amount=obj.amount,
                invoice_id=obj.invoice_id,
                quantity=obj.quantity,
                expected=[
                    ("DEFERRED_REVENUE", obj.amount, ZERO),
                    ("REVENUE", ZERO, obj.amount),
                ],
                key=f"delivery-units:{obj.reference}",
                revenue_effect=_money(obj.amount),
            )
            self.invoice_change(obj, "delivered_quantity", obj.quantity, row)
            self.check_price(obj, row)
        for inv_id, inv in self.invoice_rows.items():
            legacy = self.by_key.get(f"invoice:{inv_id}:delivery")
            if legacy and legacy.status == "posted":
                source = self.invoice_objects[inv_id]
                row = {
                    "row_id": f"legacy-delivery:{inv_id}",
                    "source_kind": "legacy_delivery",
                    "source_id": inv_id,
                    "role": "context",
                    "date": legacy.date.isoformat(),
                    "invoice_id": inv_id,
                    "amount": inv["total"],
                    "quantity": inv["quantity"],
                    "cash_amount": "0.00",
                    "gross_amount": inv["total"],
                    "revenue_effect": inv["total"],
                    "evidence": source.delivery_evidence,
                    "journal_id": None,
                    "source_key": None,
                    "period_position": "current"
                    if legacy.date >= self.start
                    else "carried",
                }
                if inv["delivered_quantity"]:
                    self.issue(
                        "revenue_duplicate_source",
                        "Pelaksanaan per unit dan pelaksanaan lama sama-sama mengakui invoice ini.",
                        row["row_id"],
                    )
                if source.delivered_at != legacy.date:
                    self.issue(
                        "source_journal_date",
                        "Tanggal pelaksanaan lama berbeda dari tanggal jurnal.",
                        row["row_id"],
                    )
                self.actual_date(legacy.date, row["row_id"])
                total = Decimal(inv["total"])
                self.journal(
                    row,
                    legacy,
                    key=f"invoice:{inv_id}:delivery",
                    expected=[
                        ("DEFERRED_REVENUE", total, ZERO),
                        ("REVENUE", ZERO, total),
                    ],
                )
                self.rows.append(row)
                inv["delivered_quantity"] += inv["quantity"]
        for obj in InvoiceCredit.objects.filter(**scoped).order_by("pk"):
            revenue = -obj.amount if obj.kind == "service" else ZERO
            row = self.context(
                "invoice-credit",
                obj,
                amount=-obj.amount,
                invoice_id=obj.invoice_id,
                quantity=obj.quantity,
                credit_kind=obj.kind,
                unpaid_amount=_money(obj.unpaid_amount),
                refund_amount=_money(obj.refund_amount),
                revenue_effect=_money(revenue),
                key=f"invoice-credit:{obj.reference}",
                expected=[
                    (
                        "REVENUE" if obj.kind == "service" else "DEFERRED_REVENUE",
                        obj.amount,
                        ZERO,
                    ),
                    ("AR", ZERO, obj.unpaid_amount),
                    ("CUSTOMER_REFUND", ZERO, obj.refund_amount),
                ],
            )
            self.invoice_change(obj, "credited_quantity", obj.quantity, row)
            if obj.kind == "unfulfilled":
                self.invoice_change(
                    obj, "unfulfilled_credit_quantity", obj.quantity, row
                )
            self.invoice_money(obj.invoice_id, "credited_amount", obj.amount)
            self.invoice_money(obj.invoice_id, "refund_liability", obj.refund_amount)
            self.check_price(obj, row)
        for obj in InvoiceCancellation.objects.filter(**scoped).order_by("pk"):
            row = self.context(
                "invoice-cancellation",
                obj,
                amount=-obj.amount,
                invoice_id=obj.invoice_id,
                unpaid_amount=_money(obj.unpaid_amount),
                refund_amount=_money(obj.refund_amount),
                revenue_effect="0.00",
                key=f"invoice:{obj.invoice_id}:cancellation",
                expected=[
                    ("DEFERRED_REVENUE", obj.amount, ZERO),
                    ("AR", ZERO, obj.unpaid_amount),
                    ("CUSTOMER_REFUND", ZERO, obj.refund_amount),
                ],
            )
            if obj.invoice_id in self.invoice_rows:
                self.invoice_rows[obj.invoice_id]["cancelled"] = True
                self.invoice_money(
                    obj.invoice_id, "refund_liability", obj.refund_amount
                )
                if obj.amount != Decimal(self.invoice_rows[obj.invoice_id]["total"]):
                    self.issue(
                        "source_amount_mismatch",
                        "Nilai pembatalan berbeda dengan invoice asal.",
                        row["row_id"],
                    )
            else:
                self.issue(
                    "source_scope",
                    "Invoice pembatalan tidak tersedia dalam masa/ruang lingkup ini.",
                    row["row_id"],
                )
        for obj in (
            Allocation.objects.filter(
                organization=self.org, transaction__date__lte=self.end
            )
            .select_related("transaction__account")
            .order_by("pk")
        ):
            row = {
                "row_id": f"receipt:{obj.pk}",
                "source_kind": "receipt",
                "source_id": obj.pk,
                "role": "context",
                "invoice_id": obj.invoice_id,
                "date": obj.transaction.date.isoformat(),
                "amount": _money(obj.amount),
                "cash_amount": _money(obj.amount),
                "gross_amount": _money(obj.amount),
                "journal_id": None,
                "source_key": None,
                "bank": self.bank(obj.transaction, f"receipt:{obj.pk}"),
                "period_position": "current"
                if obj.transaction.date >= self.start
                else "carried",
            }
            self.check_invoice(obj.invoice_id, obj.transaction.date, row["row_id"])
            journal = self.by_key.get(f"allocation:{obj.pk}")
            self.journal(
                row,
                journal,
                key=f"allocation:{obj.pk}",
                expected=[("BANK", obj.amount, ZERO), ("AR", ZERO, obj.amount)],
            )
            self.rows.append(row)
            self.invoice_money(obj.invoice_id, "gross_settled", obj.amount)
            self.invoice_money(obj.invoice_id, "cash_received", obj.amount)
        for obj in (
            CustomerAdvance.objects.filter(**scoped)
            .select_related("bank_posting__transaction__account", "party")
            .order_by("pk")
        ):
            row = self.context(
                "customer-advance",
                obj,
                amount=obj.amount,
                cash=obj.amount,
                party_id=obj.party_id,
                bank=self.bank(
                    obj.bank_posting.transaction, f"customer-advance:{obj.pk}"
                ),
                expected=[
                    ("BANK", obj.amount, ZERO),
                    ("CUSTOMER_ADVANCE", ZERO, obj.amount),
                ],
                key=f"bank-event:{obj.reference}",
            )
            self.check_posting(obj, row)
            if obj.party.organization_id != self.org.pk:
                self.issue(
                    "source_scope",
                    "Pelanggan sumber uang muka tidak sesuai perusahaan.",
                    row["row_id"],
                )
        for obj in (
            AdvanceApplication.objects.filter(**scoped)
            .select_related("advance")
            .order_by("pk")
        ):
            row = self.context(
                "advance-application",
                obj,
                amount=obj.amount,
                invoice_id=obj.invoice_id,
                advance_id=obj.advance_id,
                expected=[
                    ("CUSTOMER_ADVANCE", obj.amount, ZERO),
                    ("AR", ZERO, obj.amount),
                ],
                key=f"advance-apply:{obj.reference}",
            )
            if (
                obj.advance.organization_id != self.org.pk
                or obj.advance.date > obj.date
            ):
                self.issue(
                    "source_scope",
                    "Sumber uang muka tidak sesuai perusahaan atau mendahului bukti uang muka.",
                    row["row_id"],
                )
            self.invoice_money(obj.invoice_id, "gross_settled", obj.amount)
        for obj in (
            CustomerTaxSettlement.objects.filter(**scoped)
            .select_related(
                "bank_posting__transaction__account",
                "source_allocation__transaction__account",
                "source_advance_application__advance__bank_posting__transaction__account",
                "source_advance_application__advance__party",
                "verification__evidence",
            )
            .order_by("pk")
        ):
            application = obj.source_advance_application
            if obj.bank_posting_id:
                bank = obj.bank_posting.transaction
            elif obj.source_allocation_id:
                bank = obj.source_allocation.transaction
            elif application:
                bank = application.advance.bank_posting.transaction
            else:
                bank = None
            row = self.context(
                "customer-tax",
                obj,
                amount=obj.gross_amount,
                invoice_id=obj.invoice_id,
                cash=obj.cash_amount,
                gross=obj.gross_amount,
                tax_amount=_money(obj.tax_amount),
                verification_id=obj.verification_id,
                source_allocation_id=obj.source_allocation_id,
                source_advance_application_id=obj.source_advance_application_id,
                source_advance_id=application.advance_id if application else None,
                source_application_date=application.date.isoformat()
                if application
                else None,
                bank=self.bank(bank, f"customer-tax:{obj.pk}") if bank else None,
                expected=[
                    ("BANK", obj.cash_amount, ZERO),
                    ("TAX_RECEIVABLE", obj.tax_amount, ZERO),
                    ("AR", ZERO, obj.gross_amount),
                ],
                key=f"bank-event:{obj.reference}"
                if obj.bank_posting_id
                else f"customer-tax-credit:{obj.obligation_id}",
            )
            if obj.bank_posting_id:
                self.check_posting(obj, row)
            elif obj.source_allocation_id and (
                obj.source_allocation.organization_id != self.org.pk
                or obj.source_allocation.invoice_id != obj.invoice_id
            ):
                self.issue(
                    "source_scope",
                    "Bukti pembayaran lama tidak sesuai invoice/perusahaan.",
                    row["row_id"],
                )
            elif application:
                advance, posting = application.advance, application.advance.bank_posting
                invoice = self.invoice_objects.get(obj.invoice_id)
                if (
                    application.organization_id != self.org.pk
                    or application.invoice_id != obj.invoice_id
                    or advance.organization_id != self.org.pk
                    or posting.organization_id != self.org.pk
                    or advance.party.organization_id != self.org.pk
                    or not invoice
                    or advance.party_id != invoice.party_id
                ):
                    self.issue(
                        "source_scope",
                        "Bukti penerapan uang muka tidak sesuai pelanggan/invoice/perusahaan.",
                        row["row_id"],
                    )
                if max(application.date, advance.date, bank.date) > obj.date:
                    self.issue(
                        "source_journal_date",
                        "Kredit pajak mendahului bukti penerapan uang muka asal.",
                        row["row_id"],
                    )
            if (
                sum(
                    bool(value)
                    for value in (
                        obj.bank_posting_id,
                        obj.source_allocation_id,
                        obj.source_advance_application_id,
                    )
                )
                != 1
            ):
                self.issue(
                    "source_scope",
                    "Kredit pajak harus menunjuk tepat satu jenis bukti pembayaran.",
                    row["row_id"],
                )
            verification = obj.verification
            doc = verification.evidence
            content_hash = hashlib.sha256(bytes(doc.content)).hexdigest()
            self.manifest.append(
                {
                    "kind": "customer_tax_certificate",
                    "id": doc.pk,
                    "verification_id": verification.pk,
                    "invoice_id": obj.invoice_id,
                    "period": verification.period.isoformat(),
                    "amount": _money(verification.amount),
                    "verification_kind": verification.kind,
                    "obligation_digest": verification.obligation_digest,
                    "reference": verification.reference,
                    "sha256": doc.sha256,
                    "actual_sha256": content_hash,
                    "integrity": "verified"
                    if content_hash == doc.sha256
                    else "mismatch",
                }
            )
            if (
                doc.organization_id != self.org.pk
                or verification.organization_id != self.org.pk
            ):
                self.issue(
                    "source_scope",
                    "Bukti potong tidak sesuai perusahaan.",
                    row["row_id"],
                )
            if content_hash != doc.sha256:
                self.issue(
                    "evidence_integrity",
                    "Isi bukti potong berbeda dari hash tersimpan.",
                    row["row_id"],
                )
            if (
                verification.kind != "credit"
                or verification.amount != obj.tax_amount
                or verification.obligation_id != obj.obligation_id
            ):
                self.issue(
                    "source_amount_mismatch",
                    "Identitas atau jumlah bukti potong berbeda dari kejadian pelunasan.",
                    row["row_id"],
                )
            self.invoice_money(obj.invoice_id, "gross_settled", obj.gross_amount)
            self.invoice_money(obj.invoice_id, "cash_received", obj.cash_amount)
        for obj in (
            CustomerRefund.objects.filter(**scoped)
            .select_related(
                "bank_posting__transaction__account", "advance", "cancellation"
            )
            .order_by("pk")
        ):
            invoice_id = obj.cancellation.invoice_id if obj.cancellation_id else None
            row = self.context(
                "refund",
                obj,
                amount=-obj.amount,
                invoice_id=invoice_id,
                cash=-obj.amount,
                cancellation_id=obj.cancellation_id,
                advance_id=obj.advance_id,
                revenue_effect="0.00",
                bank=self.bank(obj.bank_posting.transaction, f"refund:{obj.pk}"),
                key=f"bank-event:{obj.reference}",
                expected=[
                    (
                        "CUSTOMER_ADVANCE" if obj.advance_id else "CUSTOMER_REFUND",
                        obj.amount,
                        ZERO,
                    ),
                    ("BANK", ZERO, obj.amount),
                ],
            )
            self.check_posting(obj, row)
            origin = obj.cancellation if obj.cancellation_id else obj.advance
            if origin.organization_id != self.org.pk or origin.date > obj.date:
                self.issue(
                    "source_scope",
                    "Sumber refund tidak sesuai perusahaan atau tanggal refund mendahului sumber.",
                    row["row_id"],
                )
            if invoice_id:
                self.invoice_money(invoice_id, "refund_liability", -obj.amount)
        for obj in (
            CreditRefund.objects.filter(**scoped)
            .select_related("bank_posting__transaction__account", "credit")
            .order_by("pk")
        ):
            row = self.context(
                "credit-refund",
                obj,
                amount=-obj.amount,
                invoice_id=obj.credit.invoice_id,
                cash=-obj.amount,
                credit_id=obj.credit_id,
                revenue_effect="0.00",
                key=f"bank-event:{obj.reference}",
                bank=self.bank(obj.bank_posting.transaction, f"credit-refund:{obj.pk}"),
                expected=[
                    ("CUSTOMER_REFUND", obj.amount, ZERO),
                    ("BANK", ZERO, obj.amount),
                ],
            )
            self.check_posting(obj, row)
            if obj.credit.organization_id != self.org.pk or obj.credit.date > obj.date:
                self.issue(
                    "source_scope",
                    "Koreksi asal refund tidak sesuai perusahaan atau tanggal.",
                    row["row_id"],
                )
            self.invoice_money(obj.credit.invoice_id, "refund_liability", -obj.amount)

    def relevant_context(self):
        """Keep this month's facts and their historical anchors/open positions.

        Full history is read to reconstruct cutoff state, but completed/settled
        old invoices without a period event are not repeated in every package.
        Carried context needs timing review only; it is never another revenue
        candidate. An unapplied advance keeps its applications/refunds as proof.
        """
        keep_invoices = {
            row["invoice_id"]
            for row in self.rows
            if row.get("invoice_id") and row["date"] >= self.start.isoformat()
        }
        for key, inv in self.invoice_rows.items():
            remaining = (
                inv["quantity"]
                - inv["delivered_quantity"]
                - inv["unfulfilled_credit_quantity"]
            )
            outstanding = (
                Decimal(inv["total"])
                - Decimal(inv["credited_amount"])
                - Decimal(inv["gross_settled"])
            )
            if (
                inv["date"] >= self.start.isoformat()
                or inv["issued"]
                and not inv["cancelled"]
                and (remaining > 0 or outstanding > 0)
                or Decimal(inv["refund_liability"]) > 0
            ):
                keep_invoices.add(key)
        advance_balances = {}
        for row in self.rows:
            if row["source_kind"] == "customer-advance":
                advance_balances[row["source_id"]] = Decimal(row["amount"])
        for row in self.rows:
            if row.get("advance_id") in advance_balances:
                advance_balances[row["advance_id"]] -= abs(Decimal(row["amount"]))
        keep_advances = {key for key, amount in advance_balances.items() if amount > 0}
        for row in self.rows:
            if (
                row["date"] >= self.start.isoformat()
                or row.get("invoice_id") in keep_invoices
            ):
                if row.get("advance_id"):
                    keep_advances.add(row["advance_id"])
                if row["source_kind"] == "customer-advance":
                    keep_advances.add(row["source_id"])
        # An advance's dated applications are retained to explain its balance.
        keep_invoices |= {
            row["invoice_id"]
            for row in self.rows
            if row.get("advance_id") in keep_advances and row.get("invoice_id")
        }
        self.invoice_rows = {
            key: value
            for key, value in self.invoice_rows.items()
            if key in keep_invoices
        }
        self.rows = [
            row
            for row in self.rows
            if row["role"] == "candidate"
            or row["date"] >= self.start.isoformat()
            or row.get("invoice_id") in keep_invoices
            or row.get("advance_id") in keep_advances
            or row["source_kind"] == "customer-advance"
            and row["source_id"] in keep_advances
        ]
        row_ids = {row["row_id"] for row in self.rows} | {
            f"invoice:{key}" for key in keep_invoices
        }
        self.issues = [
            issue
            for issue in self.issues
            if issue["row_id"] is None
            or issue["row_id"] in row_ids
            or issue["row_id"].startswith("revenue-line:")
        ]
        self.manifest = [
            row
            for row in self.manifest
            if not row.get("invoice_id") or row["invoice_id"] in keep_invoices
        ]

    def check_posting(self, obj, row):
        posting = obj.bank_posting
        if (
            posting.organization_id != self.org.pk
            or posting.journal_id != obj.journal_id
            or posting.date != obj.date
            or posting.signed_amount != Decimal(row["cash_amount"])
        ):
            self.issue(
                "source_ledger_mismatch",
                "Kejadian dan alokasi bank tidak menunjuk jumlah/jurnal/tanggal yang sama.",
                row["row_id"],
            )

    def check_invoice(self, invoice_id, day, row_id):
        inv = self.invoice_rows.get(invoice_id)
        if not inv or not inv["issued"]:
            self.issue(
                "source_scope",
                "Kejadian memerlukan invoice terbit dalam ruang lingkup dan batas masa ini.",
                row_id,
            )
        elif day < date.fromisoformat(inv["date"]):
            self.issue(
                "source_journal_date",
                "Tanggal kejadian mendahului invoice sumber.",
                row_id,
            )

    def invoice_money(self, inv_id, field, amount):
        if inv_id not in self.invoice_rows:
            self.issue(
                "source_scope",
                "Invoice sumber tidak tersedia dalam masa/ruang lingkup ini.",
            )
            return
        item = self.invoice_rows[inv_id]
        item[field] = _money(Decimal(item[field]) + amount)

    def invoice_change(self, obj, field, value, row):
        if obj.invoice_id not in self.invoice_rows:
            self.issue(
                "source_scope",
                "Invoice kejadian tidak tersedia dalam masa/ruang lingkup ini.",
                row["row_id"],
            )
        else:
            self.invoice_rows[obj.invoice_id][field] += value

    def check_price(self, obj, row):
        inv = self.invoice_rows.get(obj.invoice_id)
        if inv and Decimal(inv["unit_price"]) * obj.quantity != obj.amount:
            self.issue(
                "source_amount_mismatch",
                "Nilai kejadian berbeda dari jumlah unit dan harga asli invoice.",
                row["row_id"],
            )

    def revenue(self):
        total = ZERO
        # Catch corrupted organization relationships without disclosing the
        # foreign source's identity/content or counting it as company revenue.
        for line in JournalLine.objects.filter(
            Q(organization=self.org) | Q(journal__organization=self.org),
            account="REVENUE",
            journal__status="posted",
            journal__date__gte=self.start,
            journal__date__lte=self.end,
        ).exclude(organization=self.org, journal__organization=self.org):
            self.issue(
                "source_scope",
                "Hubungan perusahaan pada baris pendapatan tidak konsisten.",
                f"revenue-line:{line.pk}",
            )
        scoped = (
            JournalLine.objects.filter(
                organization=self.org,
                journal__organization=self.org,
                journal__status="posted",
                account="REVENUE",
                journal__date__gte=self.start,
                journal__date__lte=self.end,
            )
            .select_related("journal")
            .order_by("pk")
        )
        for line in scoped:
            amount = line.credit - line.debit
            total += amount
            matches = self.matches.get(line.journal_id, [])
            match = matches[0] if len(matches) == 1 else None
            row_id, flags = f"revenue-line:{line.pk}", []
            all_lines = self.lines[line.journal_id]
            own_lines = [
                item for item in all_lines if item.organization_id == self.org.pk
            ]
            if len(all_lines) != len(own_lines):
                self.issue(
                    "source_scope",
                    "Jurnal pendapatan mempunyai baris dari perusahaan lain.",
                    row_id,
                )
                flags.append("source_scope")
            if len(own_lines) < 2 or sum(
                (item.debit for item in own_lines), ZERO
            ) != sum((item.credit for item in own_lines), ZERO):
                self.issue(
                    "source_journal_unbalanced",
                    "Seluruh baris jurnal pendapatan, termasuk akun lawan, harus lengkap dan seimbang.",
                    row_id,
                )
                flags.append("source_journal_unbalanced")
            if len(matches) > 1:
                self.issue(
                    "revenue_duplicate_source",
                    "Beberapa kejadian menggunakan jurnal pendapatan yang sama.",
                    row_id,
                )
                flags.append("revenue_duplicate_source")
            if not match:
                self.issue(
                    "orphan_revenue",
                    "Pendapatan manual/tidak terhubung memerlukan sumber dan keputusan pemeriksaan.",
                    row_id,
                )
                flags.append("orphan_revenue")
            else:
                revenue_lines = [
                    item
                    for item in self.lines[line.journal_id]
                    if item.account == "REVENUE"
                ]
                expected = Decimal(match.get("revenue_effect", "0.00"))
                if len(revenue_lines) != 1:
                    self.issue(
                        "revenue_multiline",
                        "Jurnal sumber memiliki beberapa baris pendapatan; seluruh baris harus diperbaiki/dijelaskan pada sumber.",
                        row_id,
                    )
                    flags.append("revenue_multiline")
                if (
                    sum((item.credit - item.debit for item in revenue_lines), ZERO)
                    != expected
                ):
                    self.issue(
                        "source_amount_mismatch",
                        "Jumlah pendapatan dalam jurnal berbeda dari kejadian sumber.",
                        row_id,
                    )
                    flags.append("source_amount_mismatch")
                flags += [
                    issue["code"]
                    for issue in self.issues
                    if issue["row_id"] == match["row_id"]
                ]
            if amount < 0:
                self.issue(
                    "revenue_debit",
                    "Pengurang pendapatan memerlukan keputusan periode dan perlakuan fiskal; jangan kurangi pajak otomatis.",
                    row_id,
                )
                flags.append("revenue_debit")
            self.actual_date(line.journal.date, row_id)
            flags += [
                issue["code"] for issue in self.issues if issue["row_id"] == row_id
            ]
            self.rows.append(
                {
                    "row_id": row_id,
                    "source_kind": "book_revenue",
                    "source_id": line.pk,
                    "role": "candidate",
                    "date": line.journal.date.isoformat(),
                    "amount": _money(amount),
                    "debit": _money(line.debit),
                    "credit": _money(line.credit),
                    "journal_id": line.journal_id,
                    "journal_lines": [
                        {
                            "id": item.pk,
                            "account": item.account,
                            "debit": _money(item.debit),
                            "credit": _money(item.credit),
                        }
                        for item in own_lines
                    ],
                    "source_key": line.journal.source_key,
                    "invoice_id": match["invoice_id"] if match else None,
                    "matched_event": {
                        "kind": match["source_kind"],
                        "id": match["source_id"],
                    }
                    if match
                    else None,
                    "review_flags": sorted(set(flags)),
                    "proposed_inclusion": bool(match and amount > 0 and not flags),
                }
            )
        return total

    def evidence(self):
        from evidence.models import Attachment
        from imports.models import (
            ImportException,
            ImportExceptionReview,
            MonthlySnapshot,
            SourceDocument,
        )

        for doc in Attachment.objects.filter(
            organization=self.org, invoice_id__in=self.invoice_rows
        ).order_by("pk"):
            self.file_evidence(
                "invoice_attachment",
                doc,
                declared_size=doc.size,
                invoice_id=doc.invoice_id,
                original_name=doc.original_name,
            )
        observations = []
        months = list(
            MonthlySnapshot.objects.filter(
                organization=self.org, year=self.start.year, month=self.start.month
            )
            .select_related("batch")
            .order_by("pk")
        )
        batch_ids = {row.batch_id for row in months}
        for month in months:
            if month.batch.organization_id != self.org.pk:
                self.issue(
                    "source_scope",
                    "Arsip bulanan menunjuk impor di luar perusahaan.",
                    f"monthly-import:{month.pk}",
                )
                continue
            payload = month.batch.payload
            observations.append(
                {
                    "kind": "monthly_import",
                    "id": month.pk,
                    "batch_id": month.batch_id,
                    "source_id": month.source_id,
                    "source_page": month.source_page,
                    "coverage": month.coverage,
                    "printed_count": month.printed_count,
                    "printed_amount": None
                    if month.printed_amount is None
                    else _money(month.printed_amount),
                    "detail_count": month.detail_count,
                    "payload_hash": _digest(month.payload),
                    "channels_hash": _digest(
                        [
                            row
                            for row in payload.get("channels", [])
                            if row.get("month_id") == month.source_id
                        ]
                    ),
                    "details_hash": _digest(
                        [
                            row
                            for row in payload.get("details", [])
                            if row.get("month_id") == month.source_id
                        ]
                    ),
                    "batch_source_hash": month.batch.source_hash,
                    "bundle_hash": month.batch.bundle_hash,
                    "metadata_hash": _digest(month.batch.metadata),
                }
            )
        for doc in SourceDocument.objects.filter(
            organization=self.org, batch_id__in=batch_ids
        ).order_by("pk"):
            self.file_evidence(
                "import_document",
                doc,
                declared_size=doc.size,
                batch_id=doc.batch_id,
                source_id=doc.source_id,
                original_name=doc.original_name,
            )
        source_ids = {row.source_id for row in months}
        exceptions = (
            ImportException.objects.filter(
                organization=self.org, batch_id__in=batch_ids
            )
            .filter(Q(month_source_id__in=source_ids) | Q(month_source_id=""))
            .order_by("pk")
        )
        for item in exceptions:
            reviews = []
            for review in ImportExceptionReview.objects.filter(
                organization=self.org, exception=item
            ).order_by("pk"):
                reviews.append(
                    {
                        "id": review.pk,
                        "decision": review.decision,
                        "reason": review.reason,
                        "evidence": review.evidence,
                        "correction": review.correction,
                        "fingerprint": review.fingerprint,
                        "snapshot_hash": _digest(review.snapshot),
                        "supersedes_id": review.supersedes_id,
                        "source_document_id": review.source_document_id,
                    }
                )
                if review.file:
                    self.file_evidence(
                        "import_review",
                        review,
                        declared_size=review.size,
                        exception_id=item.pk,
                    )
            observations.append(
                {
                    "kind": "import_exception",
                    "id": item.pk,
                    "batch_id": item.batch_id,
                    "code": item.code,
                    "severity": item.severity,
                    "message": item.message,
                    "source_locator": item.source_locator,
                    "payload_hash": _digest(item.payload),
                    "reviews": reviews,
                }
            )
            self.issue(
                "source_observation",
                "Catatan impor adalah observasi sumber; pemeriksa harus memastikan perlakuan dan kelengkapannya.",
                f"import-exception:{item.pk}",
            )
        reviewed_statement_ids = set()
        for review in (
            StatementPeriodReview.objects.filter(
                organization=self.org,
                period_start__lte=self.end,
                period_end__gte=self.start,
            )
            .select_related("statement")
            .order_by("pk")
        ):
            source = review.statement
            if (
                source.organization_id != self.org.pk
                or source.account_id != review.account_id
            ):
                self.issue(
                    "source_scope",
                    "Pemeriksaan e-statement tidak sesuai perusahaan/rekening.",
                    f"statement-review:{review.pk}",
                )
                continue
            self.file_evidence(
                "bank_statement",
                source,
                account_id=source.account_id,
                original_name=source.original_name,
                mapping_hash=_digest(source.mapping),
                rows_hash=_digest(source.rows),
                result_hash=_digest(source.result),
                confirmed=bool(source.confirmed_at),
            )
            reviewed_statement_ids.add(source.pk)
            observations.append(
                {
                    "kind": "statement_review",
                    "id": review.pk,
                    "statement_id": source.pk,
                    "account_id": review.account_id,
                    "start": review.period_start.isoformat(),
                    "end": review.period_end.isoformat(),
                    "fingerprint": review.fingerprint,
                    "snapshot_hash": _digest(review.snapshot),
                    "supersedes_id": review.supersedes_id,
                    "opening_balance": _money(review.opening_balance),
                    "closing_balance": _money(review.closing_balance),
                    "total_in": _money(review.total_in),
                    "total_out": _money(review.total_out),
                    "transaction_count": review.transaction_count,
                    "evidence": review.evidence,
                }
            )
        self.unreviewed_inventory(observations, reviewed_statement_ids)
        return observations

    def unreviewed_inventory(self, observations, reviewed_statement_ids):
        from operations.models import IntakeRow
        from webapp.models import StatementImport

        for source in (
            StatementImport.objects.filter(organization=self.org)
            .exclude(pk__in=reviewed_statement_ids)
            .select_related("account")
            .order_by("pk")
        ):
            dates = []
            # Only observed source dates select scope. Unknown/unmapped dates
            # remain an explicit inventory item rather than disappearing.
            try:
                column = int(source.mapping.get("date", ""))
            except (ValueError, TypeError):
                names = [str(label).strip().lower() for label in source.headers]
                column = next(
                    (
                        i
                        for i, label in enumerate(names)
                        if label
                        in {
                            "date",
                            "tanggal",
                            "tgl",
                            "tanggal transaksi",
                            "posting date",
                        }
                    ),
                    None,
                )
            for raw in source.rows:
                values = (
                    [raw[column]]
                    if column is not None
                    and isinstance(raw, list)
                    and column < len(raw)
                    else raw
                    if isinstance(raw, list)
                    else []
                )
                for value in values:
                    day = _observed_date(value)
                    if day:
                        dates.append(day)
            relevant = any(self.start <= day <= self.end for day in dates)
            if dates and not relevant:
                continue
            if source.account.organization_id != self.org.pk:
                self.issue(
                    "source_scope",
                    "Unggahan e-statement memiliki rekening di luar perusahaan.",
                    f"statement-upload:{source.pk}",
                )
                continue
            self.file_evidence(
                "bank_statement",
                source,
                account_id=source.account_id,
                original_name=source.original_name,
                mapping_hash=_digest(source.mapping),
                rows_hash=_digest(source.rows),
                result_hash=_digest(source.result),
                confirmed=bool(source.confirmed_at),
            )
            observations.append(
                {
                    "kind": "unreviewed_statement",
                    "id": source.pk,
                    "account_id": source.account_id,
                    "period_scope": "selected_month" if relevant else "unknown",
                    "confirmed": bool(source.confirmed_at),
                    "mapping_hash": _digest(source.mapping),
                    "rows_hash": _digest(source.rows),
                    "result_hash": _digest(source.result),
                }
            )
            self.issue(
                "source_observation",
                "Unggahan e-statement belum memiliki pemeriksaan cakupan masa yang sesuai.",
                f"statement-upload:{source.pk}",
            )
        for item in (
            IntakeRow.objects.filter(organization=self.org)
            .select_related("batch", "registration__line")
            .order_by("pk")
        ):
            inv_id = item.registration.line.invoice_id if item.registration_id else None
            planned = item.normalized.get("planned_date")
            day = _observed_date(planned)
            if day is None and not item.batch.mapping:
                labels = [str(value).strip().lower() for value in item.batch.headers]
                index = next(
                    (
                        i
                        for i, label in enumerate(labels)
                        if label
                        in {"planned_date", "test_date", "test date", "tanggal tes"}
                    ),
                    None,
                )
                if (
                    index is not None
                    and isinstance(item.raw, list)
                    and index < len(item.raw)
                ):
                    day = _observed_date(item.raw[index])
            if (
                inv_id not in self.invoice_rows
                and day
                and not self.start <= day <= self.end
            ):
                continue
            if (
                item.batch.organization_id != self.org.pk
                or item.registration_id
                and item.registration.organization_id != self.org.pk
            ):
                self.issue(
                    "source_scope",
                    "Baris pendaftaran memiliki sumber dari perusahaan lain.",
                    f"registration-intake:{item.pk}",
                )
                continue
            observations.append(
                {
                    "kind": "registration_intake",
                    "id": item.pk,
                    "batch_id": item.batch_id,
                    "batch_digest": item.batch.digest,
                    "mapping_hash": _digest(item.batch.mapping),
                    "raw_hash": _digest(item.raw),
                    "normalized_hash": _digest(item.normalized),
                    "issues_hash": _digest(item.issues),
                    "status": item.status,
                    "registration_id": item.registration_id,
                    "invoice_id": inv_id,
                    "planned_date": day.isoformat() if day else None,
                    "period_scope": "selected_month"
                    if day and self.start <= day <= self.end
                    else "linked_invoice"
                    if inv_id in self.invoice_rows
                    else "unknown",
                }
            )
            self.issue(
                "source_observation",
                "Observasi pendaftaran belum membuktikan penjualan/penerimaan; cocokkan sumber atau jelaskan pengecualiannya.",
                f"registration-intake:{item.pk}",
            )


@transaction.atomic
def build_monthly_sources(organization, period):
    """Return a detached canonical snapshot; extraction records no decisions.

    Organization lock shares Finance's mutation fence. HTTP authorization is
    the caller's responsibility; all joins and private documents remain scoped.
    Exact month-end cutoff permits a preview; package approval must additionally
    require a completed month and professional completeness/timing decisions.
    """
    start, end = _period(period)
    org_id = getattr(organization, "pk", organization)
    if isinstance(org_id, bool) or not isinstance(org_id, int):
        raise ValidationError("Pilih perusahaan yang tersimpan.")
    try:
        organization = Organization.objects.select_for_update().get(pk=org_id)
    except Organization.DoesNotExist as exc:
        raise ValidationError("Perusahaan tidak tersedia.") from exc
    source = _Extraction(organization, start, end)
    source.invoices()
    source.events()
    revenue = source.revenue()
    source.relevant_context()
    observations = source.evidence()
    from finance.opening_balances import (
        opening_balance_source_graph,
        opening_balance_source_snapshot,
        opening_cancellation_source_graph,
        opening_cancellation_source_snapshot,
        opening_correction_source_graph,
        opening_correction_source_snapshot,
        opening_settlement_source_graph,
        opening_settlement_source_snapshot,
    )

    from .opening_balance_review_dependencies import (
        opening_balance_source_inventory,
        opening_cash_recovery_metadata,
    )

    opening_observations = []
    opening_inventory = opening_balance_source_inventory(
        organization=organization, cutoff=end
    )
    for item in opening_inventory["items"]:
        try:
            proof = opening_balance_source_snapshot(
                organization=organization, item=item
            )
        except ValidationError as exc:
            proof = {
                "damaged_graph": opening_balance_source_graph(
                    organization=organization, item=item
                ),
                "issues": exc.messages,
            }
            for message in exc.messages:
                source.issue(
                    "opening_balance_integrity", message, f"opening-item:{item.pk}"
                )
        opening_observations.append(
            {
                "kind": "opening_principal",
                "item_id": item.pk,
                "date": item.date.isoformat(),
                "source": proof,
                "income_effect": "0.00",
            }
        )
    # Principal cash has no invoice or inferred gross/tax basis. Each current
    # incoming event requires an explicit include/exclude/timing decision in
    # the received monthly report. Old cash remains evidence, not new turnover.
    for event in opening_inventory["settlements"]:
        proof, intact = None, True
        try:
            proof = opening_settlement_source_snapshot(
                organization=organization, settlement=event
            )
        except ValidationError as exc:
            intact = False
            proof = {
                "damaged_graph": opening_settlement_source_graph(
                    organization=organization, settlement=event
                )
            }
            for message in exc.messages:
                source.issue(
                    "opening_balance_integrity",
                    message,
                    f"opening-settlement:{event.pk}",
                )
        opening_observations.append(
            {
                "kind": "opening_principal_cash",
                "settlement_id": event.pk,
                "item_id": event.item_id,
                "date": event.date.isoformat(),
                "source": proof,
                "income_effect": "0.00",
                "tax_treatment": "separate_professional_review",
                "intact": intact,
            }
        )
        if intact and event.item.kind == "receivable" and start <= event.date <= end:
            bank = event.bank_posting.transaction
            row = {
                "row_id": f"opening-receipt:{event.pk}",
                "source_kind": "opening-receipt",
                "source_id": event.pk,
                "source_item_id": event.item_id,
                "role": "candidate",
                "date": event.date.isoformat(),
                "amount": _money(event.amount),
                "invoice_id": None,
                "journal_id": None,
                "source_key": None,
                "cash_amount": _money(event.amount),
                "gross_amount": None,
                "reference": event.reference,
                "evidence": event.evidence,
                "party_id": event.item.party_id,
                "source_reference": event.item.source_reference,
                "source_date": event.item.source_date.isoformat(),
                "source_proof": proof,
                "party": {
                    "id": event.item.party_id,
                    "kind": event.item.party.kind,
                    "name": event.item.party.name,
                    "tax_id": event.item.party.tax_id,
                },
                "bank": source.bank(bank, f"opening-receipt:{event.pk}"),
                "period_position": "current",
                "proposed_inclusion": False,
                "review_flags": [
                    "Penerimaan pokok piutang lama. Kas belum membuktikan bruto atau omzet pajak masa ini; cocokkan laporan profesional."
                ],
            }
            try:
                row.update(
                    opening_cash_recovery_metadata(
                        organization=organization, settlement=event, cutoff=end
                    )
                )
            except ValidationError as exc:
                for message in exc.messages:
                    source.issue("opening_balance_integrity", message, row["row_id"])
            if "opening_cancellation" in row:
                row["review_flags"].append(
                    "Pencocokan ini dibatalkan. Pemeriksa harus mengecualikannya secara jelas dengan alasan; kas bank asli tetap tersimpan."
                )
            source.actual_date(event.date, row["row_id"])
            source.journal(
                row,
                source.journals.get(event.journal_id),
                key=f"bank-event:{event.reference}",
                expected=[("BANK", event.amount, ZERO), ("AR", ZERO, event.amount)],
            )
            source.rows.append(row)
    # An amount correction changes dated principal, never cash, book income,
    # an original cash review, or the monthly turnover candidate inventory.
    for event in opening_inventory["corrections"]:
        try:
            proof = opening_correction_source_snapshot(
                organization=organization, correction=event
            )
            intact = True
        except ValidationError as exc:
            intact = False
            proof = {
                "damaged_graph": opening_correction_source_graph(
                    organization=organization, correction=event
                )
            }
            for message in exc.messages:
                source.issue(
                    "opening_balance_integrity",
                    message,
                    f"opening-correction:{event.pk}",
                )
        opening_observations.append(
            {
                "kind": "opening_principal_correction",
                "correction_id": event.pk,
                "item_id": event.item_id,
                "date": event.date.isoformat(),
                "direction": event.direction,
                "amount": _money(event.amount),
                "source": proof,
                "income_effect": "0.00",
                "cash_effect": "0.00",
                "intact": intact,
            }
        )
    if opening_inventory["orphan_correction_journals"]:
        opening_observations.extend(opening_inventory["orphan_correction_journals"])
        source.issue(
            "opening_balance_integrity",
            "Ada jurnal koreksi saldo lama tanpa catatan keputusan dan bukti sumber. Periksa jurnal asli; nilainya belum dapat dinilai.",
        )
    for event in opening_inventory["cancellations"]:
        try:
            proof = opening_cancellation_source_snapshot(
                organization=organization, cancellation=event
            )
            intact = True
        except ValidationError as exc:
            intact = False
            proof = {
                "damaged_graph": opening_cancellation_source_graph(
                    organization=organization, cancellation=event
                )
            }
            for message in exc.messages:
                source.issue(
                    "opening_balance_integrity",
                    message,
                    f"opening-cancellation:{event.pk}",
                )
        opening_observations.append(
            {
                "kind": "opening_principal_cancellation",
                "cancellation_id": event.pk,
                "item_id": event.item_id,
                "settlement_id": event.settlement_id,
                "date": _observed_date(event.date),
                "amount": _money(event.amount) if intact else None,
                "source": proof,
                "income_effect": "0.00",
                "physical_cash_effect": "0.00",
                "intact": intact,
            }
        )
    if opening_inventory["orphan_cancellation_sources"]:
        opening_observations.extend(opening_inventory["orphan_cancellation_sources"])
        source.issue(
            "opening_balance_integrity",
            "Bukti pembatalan pencocokan atau jurnal aslinya terputus. Nilai sumber belum dapat dinilai.",
        )
    from finance.opening_balances import opening_refund_source_snapshot

    from .opening_refund_review_dependencies import (
        opening_refund_financial_dependency,
        opening_refund_source_inventory,
    )

    refund_inventory = opening_refund_source_inventory(
        organization=organization, cutoff=end
    )
    refund_financial = opening_refund_financial_dependency(
        organization=organization, cutoff=end
    )
    opening_observations.extend(refund_financial["inventory"])
    for message in refund_financial["blockers"]:
        source.issue("opening_balance_integrity", message)
    for event in refund_inventory["refunds"]:
        try:
            proof = opening_refund_source_snapshot(
                organization=organization, refund=event
            )
        except ValidationError:
            # The opaque failed source is already retained above; its amount
            # cannot become an invented income or cash candidate.
            continue
        if not start <= event.date <= end:
            continue
        bank, party = event.bank_posting.transaction, event.item.party
        signed = event.bank_posting.signed_amount
        row = {
            "row_id": f"opening-refund:{event.pk}",
            "source_kind": "opening-refund",
            "source_id": event.pk,
            "source_item_id": event.item_id,
            "source_settlement_id": event.settlement_id,
            "role": "candidate" if event.item.kind == "receivable" else "context",
            "date": event.date.isoformat(),
            "amount": _money(signed),
            "cash_amount": _money(signed),
            "gross_amount": None,
            "invoice_id": None,
            "journal_id": None,
            "source_key": None,
            "reference": event.reference,
            "evidence": event.evidence,
            "party_id": party.pk,
            "source_reference": event.item.source_reference,
            "source_date": event.item.source_date.isoformat(),
            "source_proof": proof,
            "party": {
                "id": party.pk,
                "kind": party.kind,
                "name": party.name,
                "tax_id": party.tax_id,
            },
            "bank": source.bank(bank, f"opening-refund:{event.pk}"),
            "period_position": "current",
            "proposed_inclusion": False,
            "review_flags": [
                "Kas pengembalian pokok saldo lama. Periksa laporan pengembalian tersendiri dan kecualikan secara jelas; kas ini tidak otomatis mengubah omzet atau pajak."
            ],
        }
        source.actual_date(event.date, row["row_id"])
        source.rows.append(row)
    from finance.models import SupplierAdvance
    from finance.supplier_advances import supplier_advance_financial_snapshot

    supplier_observations = []
    for advance in (
        SupplierAdvance.objects.filter(organization=organization)
        .filter(
            Q(date__lte=end)
            | Q(journal__date__lte=end)
            | Q(bank_posting__transaction__date__lte=end)
        )
        .order_by("date", "pk")
    ):
        facts = supplier_advance_financial_snapshot(
            organization=organization, advance=advance, as_of=end
        )
        if not facts["current"]:
            for message in facts["issues"]:
                source.issue(
                    "supplier_advance_integrity",
                    message,
                    f"supplier-advance:{advance.pk}",
                )
        supplier_observations.append(
            {
                "kind": "supplier_advance_principal",
                "advance_id": advance.pk,
                "date": advance.date.isoformat(),
                "source": facts["source"],
                "events": facts["events"],
                **{
                    key: _money(facts[key]) if facts[key] is not None else None
                    for key in ("paid", "applied", "refunded", "reversed", "remaining")
                },
                "current": facts["current"],
                "issues": facts["issues"],
                "income_effect": "0.00",
                "tax_treatment": "separate_professional_review",
            }
        )
    source.issue(
        "inventory_unknown",
        "Kelengkapan seluruh produk, kanal, cabang dan transaksi di luar aplikasi belum diperiksa.",
    )
    # Deduplicate document identities when several reviews bind the same file.
    manifest = {f"{row['kind']}:{row['id']}": row for row in source.manifest}
    payload = {
        "extraction_version": EXTRACTION_VERSION,
        "organization_id": organization.pk,
        "period": start.strftime("%Y-%m"),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "cutoff": end.isoformat(),
        "book_revenue": _money(revenue),
        "taxable_turnover": None,
        "rows": sorted(source.rows, key=lambda row: (row["date"], row["row_id"])),
        "invoices": list(source.invoice_rows.values()),
        "issues": sorted(
            source.issues,
            key=lambda row: (row["code"], row["row_id"] or "", row["detail"]),
        ),
        "evidence_manifest": [manifest[key] for key in sorted(manifest)],
        "inventory": {
            "completeness": "unknown",
            "observations": observations,
            "scope": "Posted commercial ledger and dated customer events; linked documents and reviewed statement/source archives.",
            "unverified_sources": [
                "off_erp_sales",
                "all_products_branches_channels",
                "unlinked_registration_observations",
                "unreviewed_bank_statement_uploads",
            ],
            "invoice_count": len(source.invoice_rows),
            "candidate_count": sum(row["role"] == "candidate" for row in source.rows),
        },
    }
    if supplier_observations:
        payload["inventory"]["supplier_advances"] = supplier_observations
    if opening_observations:
        payload["inventory"]["opening_balances"] = opening_observations
    payload["source_hash"] = _digest(payload)
    return payload
