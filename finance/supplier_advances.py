"""Supplier deposits: actual cash, noncash AP settlement and dated correction.

The organization lock is shared with ordinary cash reconciliation and close.
Source receipts are stable; current balances and action freshness are separate.
"""

import hashlib
import json
from datetime import date as Date
from decimal import Decimal
from functools import wraps
from uuid import UUID

from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from core.audit import record
from core.models import Membership, Organization

from . import services as ledger
from .models import (
    ZERO,
    BankPosting,
    BankTransaction,
    Bill,
    BillPayment,
    Journal,
    Party,
    SupplierAdvance,
    SupplierAdvanceApplication,
    SupplierAdvanceApplicationReversal,
    SupplierAdvanceRefund,
    WithholdingSettlement,
    assert_open_period,
)
from .supplier_advance_models import VERSION

MODELS = {
    "record": SupplierAdvance,
    "apply": SupplierAdvanceApplication,
    "refund": SupplierAdvanceRefund,
    "reverse": SupplierAdvanceApplicationReversal,
}
PAYLOAD_KEYS = {
    "action",
    "actor_id",
    "supplier_id",
    "advance_id",
    "bill_id",
    "transaction_id",
    "application_id",
    "amount",
    "date",
    "source_reference",
    "reason",
    "evidence",
    "purpose",
    "viewed_fingerprint",
}


def _hash(value):
    def encode(item):
        if isinstance(item, Date):
            return item.isoformat()
        if isinstance(item, (Decimal, UUID)):
            return str(item)
        raise TypeError("Unsupported source value")

    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=encode,
        ).encode()
    ).hexdigest()


def _proof_errors(function):
    @wraps(function)
    def checked(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (ObjectDoesNotExist, ValueError, TypeError, ArithmeticError) as exc:
            raise ValidationError(
                "Bukti sumber uang muka tidak lengkap atau berubah; minta Owner memeriksa catatan asli."
            ) from exc

    return checked


def _key(value):
    try:
        key = UUID(str(value))
        if str(value) != str(key):
            raise ValueError
        return key
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValidationError(
            "Kunci permintaan harus berupa UUID yang valid. Periksa ulang sumber."
        ) from exc


def _scope(obj, org):
    if obj.organization_id != org.pk:
        raise ValidationError("Sumber uang muka berasal dari perusahaan lain.")
    return obj


def _select(model, value, org, *, lock=False):
    pk = getattr(value, "pk", value)
    if (
        isinstance(pk, bool)
        or not isinstance(pk, (int, str))
        or (isinstance(pk, str) and not pk.isdecimal())
        or int(pk) <= 0
    ):
        raise ValidationError("Pilih identitas sumber yang valid dari perusahaan ini.")
    return ledger._scoped(model, int(pk), org, lock=lock)


def _historical_actor(org, actor_id):
    if (
        not actor_id
        or not Membership.all_objects.filter(
            organization=org, user_id=actor_id
        ).exists()
    ):
        raise ValidationError(
            "Pelaku keputusan asli tidak mempunyai riwayat keanggotaan perusahaan ini."
        )


def _supplier(org, row):
    _scope(row, org)
    if row.kind != "supplier":
        raise ValidationError("Sumber harus memakai pemasok perusahaan yang sama.")
    return row


def _journal(org, journal, date, key, entries):
    from .budget_adapter import _journal as original_journal

    _scope(journal, org)
    return original_journal(
        org, key=key, date=date, entries=entries, journal_id=journal.pk
    )


def _bank_identity(org, bank):
    _scope(bank, org)
    _scope(bank.account, org)
    value = ledger._amount(bank.amount, positive=False)
    if value == ZERO:
        raise ValidationError("Bukti bank tidak boleh bernilai nol.")
    ledger._actual_date(bank.date)
    # Names and tax identifiers are mutable legal/display identity, not cash.
    return {
        "id": bank.pk,
        "organization_id": bank.organization_id,
        "account_id": bank.account_id,
        "date": bank.date.isoformat(),
        "reference": bank.reference,
        "amount": str(value),
        "description_sha256": _hash(bank.description),
    }


@_proof_errors
def _bank_state(org, bank):
    identity = _bank_identity(org, bank)
    claims, allocated = [], ZERO
    for row in bank.allocations.select_related("invoice").order_by("pk"):
        _scope(row, org)
        _scope(row.invoice, org)
        _scope(row.invoice.party, org)
        value = ledger._amount(row.amount)
        if bank.amount <= ZERO or bank.date < row.invoice.date:
            raise ValidationError(
                "Alokasi penerimaan tidak sesuai arah atau tanggal bank asli."
            )
        journal = Journal.objects.get(
            organization=org, source_key=f"allocation:{row.pk}"
        )
        proof = _journal(
            org,
            journal,
            bank.date,
            f"allocation:{row.pk}",
            [("BANK", value, ZERO), ("AR", ZERO, value)],
        )
        allocated += value
        claims.append(
            {
                "kind": "allocation",
                "id": row.pk,
                "invoice_id": row.invoice_id,
                "amount": str(value),
                "journal": proof,
            }
        )
    for row in bank.bill_payments.select_related("bill").order_by("pk"):
        _scope(row, org)
        _scope(row.bill, org)
        _supplier(org, row.bill.supplier)
        value = ledger._amount(row.amount)
        if bank.amount >= ZERO or bank.date < row.bill.date:
            raise ValidationError(
                "Alokasi pembayaran tidak sesuai arah atau tanggal bank asli."
            )
        journal = Journal.objects.get(
            organization=org, source_key=f"bill-payment:{row.pk}"
        )
        proof = _journal(
            org,
            journal,
            bank.date,
            f"bill-payment:{row.pk}",
            [("AP", value, ZERO), ("BANK", ZERO, value)],
        )
        allocated -= value
        claims.append(
            {
                "kind": "bill_payment",
                "id": row.pk,
                "bill_id": row.bill_id,
                "amount": str(value),
                "journal": proof,
            }
        )
    for row in bank.postings.select_related("journal").order_by("pk"):
        _scope(row, org)
        _scope(row.journal, org)
        value = ledger._amount(row.signed_amount, positive=False)
        lines = list(
            row.journal.lines.order_by("pk").values(
                "pk", "organization_id", "account", "debit", "credit"
            )
        )
        debit = credit = bank_net = ZERO
        for line in lines:
            d, c = (
                ledger._amount(line["debit"], positive=False),
                ledger._amount(line["credit"], positive=False),
            )
            if (
                line["organization_id"] != org.pk
                or line["account"] not in ledger.JournalLine.Account.values
                or d < ZERO
                or c < ZERO
                or (d > ZERO) == (c > ZERO)
            ):
                raise ValidationError(
                    "Baris jurnal klaim bank rusak atau berasal dari perusahaan lain."
                )
            debit += d
            credit += c
            if line["account"] == "BANK":
                bank_net += d - c
        if (
            row.kind not in dict(BankPosting._meta.get_field("kind").flatchoices)
            or not value
            or value * bank.amount <= ZERO
            or row.date != bank.date
            or row.journal.date != bank.date
            or row.journal.status != "posted"
            or not row.journal.posted_at
            or row.journal.source_key != f"bank-event:{row.reference}"
            or len(lines) < 2
            or debit <= ZERO
            or debit != credit
            or bank_net != value
        ):
            raise ValidationError(
                "Klaim bank dan seluruh jurnalnya tidak sesuai uang asli."
            )
        allocated += value
        claims.append(
            {
                "kind": "bank_posting",
                "id": row.pk,
                "posting_kind": row.kind,
                "amount": str(value),
                "reference": row.reference,
                "evidence_sha256": _hash(row.evidence),
                "journal_id": row.journal_id,
                "lines": [
                    {**line, "debit": str(line["debit"]), "credit": str(line["credit"])}
                    for line in lines
                ],
            }
        )
    # A cancellation retains the original posting. Release its signed claim
    # only through an independent sealed recovery proof, never a raw sum.
    from .opening_balances import (
        opening_cash_cancellation_facts,
        opening_cash_refund_facts,
    )

    recovery = opening_cash_cancellation_facts(organization=org, transaction=bank)
    allocated -= recovery["released"]
    remaining = bank.amount - allocated
    if (
        allocated * bank.amount < ZERO
        or remaining * bank.amount < ZERO
        or abs(allocated) > abs(bank.amount)
    ):
        raise ValidationError(
            "Seluruh klaim bank melampaui kapasitas atau berlawanan arah dengan sumber."
        )
    state = {"identity": identity, "claims": claims, "remaining": str(remaining)}
    if recovery["releases"]:
        state["opening_cancellations"] = recovery["releases"]
    refunds = opening_cash_refund_facts(organization=org, transaction=bank)
    if refunds["refunds"]:
        state["opening_refunds"] = refunds["refunds"]
    return state


def _payload(
    *,
    action,
    actor,
    amount,
    date,
    source_reference,
    reason,
    evidence,
    expected_fingerprint,
    advance=None,
    bill=None,
    bank=None,
    application=None,
    supplier=None,
    purpose="",
):
    if (
        not isinstance(expected_fingerprint, str)
        or len(expected_fingerprint) != 64
        or any(c not in "0123456789abcdef" for c in expected_fingerprint)
    ):
        raise ValidationError("Periksa sumber terlebih dahulu sebelum menyimpan.")
    if not isinstance(purpose, str) or len(purpose.strip()) > 300:
        raise ValidationError("Tujuan uang muka maksimal 300 karakter.")
    if action == "record":
        source_reference = ledger._review_text(
            source_reference, "Referensi pemasok", limit=160
        )
    elif not isinstance(source_reference, str) or len(source_reference.strip()) > 160:
        raise ValidationError("Referensi pemasok maksimal 160 karakter.")
    else:
        source_reference = source_reference.strip()
    return {
        "action": action,
        "actor_id": actor.pk,
        "supplier_id": supplier.pk if supplier else None,
        "advance_id": advance.pk if advance else None,
        "bill_id": bill.pk if bill else None,
        "transaction_id": bank.pk if bank else None,
        "application_id": application.pk if application else None,
        "amount": str(amount),
        "date": date.isoformat(),
        "source_reference": source_reference,
        "reason": ledger._review_text(reason, "Alasan"),
        "evidence": ledger._review_text(evidence, "Bukti"),
        "purpose": purpose.strip(),
        "viewed_fingerprint": expected_fingerprint,
    }


def _common(org, row):
    _scope(row, org)
    _historical_actor(org, row.actor_id)
    key = _key(row.request_key)
    amount = ledger._amount(row.amount)
    ledger._actual_date(row.date)
    payload = row.request_payload
    if (
        not isinstance(payload, dict)
        or set(payload) != PAYLOAD_KEYS
        or row.workflow_version != VERSION
    ):
        raise ValidationError(
            "Skema permintaan asli tidak lengkap atau tidak didukung."
        )
    for name in (
        "actor_id",
        "supplier_id",
        "advance_id",
        "bill_id",
        "transaction_id",
        "application_id",
    ):
        value = payload[name]
        if value is not None and (type(value) is not int or value <= 0):
            raise ValidationError("Identitas dalam permintaan asli tidak valid.")
    expected = {
        "action": row.action,
        "actor_id": row.actor_id,
        "amount": str(amount),
        "date": row.date.isoformat(),
        "source_reference": row.source_reference,
        "reason": row.reason,
        "evidence": row.evidence,
        "purpose": getattr(row, "purpose", ""),
        "supplier_id": getattr(row, "supplier_id", None),
        "advance_id": getattr(row, "advance_id", None),
        "bill_id": getattr(row, "bill_id", None),
        "application_id": getattr(row, "application_id", None),
        "transaction_id": row.bank_posting.transaction_id
        if hasattr(row, "bank_posting_id")
        else None,
    }
    if (
        any(payload.get(k) != v for k, v in expected.items())
        or row.reference != f"supplier-{row.action}:{key}"
    ):
        raise ValidationError(
            "Permintaan, nominal, tanggal atau referensi berbeda dari keputusan asli."
        )
    fp = payload.get("viewed_fingerprint")
    if (
        not isinstance(fp, str)
        or len(fp) != 64
        or any(c not in "0123456789abcdef" for c in fp)
    ):
        raise ValidationError("Keputusan asli tidak mempunyai bukti periksa sumber.")
    return {
        "version": VERSION,
        "action": row.action,
        "organization_id": org.pk,
        "actor_id": row.actor_id,
        "request_key": str(key),
        "request_payload": payload,
        "date": row.date.isoformat(),
        "amount": str(amount),
        "reference": row.reference,
        "source_reference": row.source_reference,
        "reason": row.reason,
        "evidence_sha256": _hash(row.evidence),
        "purpose": getattr(row, "purpose", ""),
    }


def _sealed(row, proof, capture):
    if not capture and (
        row.source_snapshot != proof or row.source_hash != _hash(proof)
    ):
        raise ValidationError(
            "Sumber keputusan uang muka berubah dari bukti asli yang disimpan."
        )
    return proof


@_proof_errors
def _cash_proof(org, row, *, capture=False):
    proof = _common(org, row)
    posting = _scope(row.bank_posting, org)
    bank = _bank_identity(org, posting.transaction)
    record_cash = row.action == "record"
    if record_cash:
        _supplier(org, row.supplier)
        proof["supplier_id"] = row.supplier_id
        entries = [("SUPPLIER_ADVANCE", row.amount, ZERO), ("BANK", ZERO, row.amount)]
    else:
        source = _cash_proof(org, row.advance)
        proof["advance_id"] = row.advance_id
        proof["advance_source_hash"] = _hash(source)
        if row.date < row.advance.date:
            raise ValidationError("Pengembalian mendahului uang muka asli.")
        entries = [("BANK", row.amount, ZERO), ("SUPPLIER_ADVANCE", ZERO, row.amount)]
    sign = -row.amount if record_cash else row.amount
    if (
        posting.transaction.amount * sign <= ZERO
        or posting.kind
        != ("supplier_advance" if record_cash else "supplier_advance_refund")
        or posting.signed_amount != sign
        or posting.date != row.date
        or posting.date != posting.transaction.date
        or posting.journal_id != row.journal_id
        or posting.reference != row.reference
        or posting.evidence != row.evidence
    ):
        raise ValidationError(
            "Uang muka/pengembalian tidak sesuai posting bank aslinya."
        )
    proof["bank"] = bank
    proof["bank_posting"] = {
        "id": posting.pk,
        "kind": posting.kind,
        "signed_amount": str(sign),
        "reference": posting.reference,
        "date": posting.date.isoformat(),
        "journal_id": posting.journal_id,
        "evidence_sha256": _hash(posting.evidence),
    }
    proof["journal"] = _journal(
        org, row.journal, row.date, f"bank-event:{row.reference}", entries
    )
    return _sealed(row, proof, capture)


@transaction.atomic
def supplier_advance_source_snapshot(*, organization, advance):
    """Validated original cash only; safe for tax review without recursion."""
    org = ledger._lock_org(organization)
    row = _select(SupplierAdvance, advance, org)
    try:
        proof = _cash_proof(org, row)
        return {**proof, "advance_id": row.pk, "fingerprint": _hash(proof)}
    except (ObjectDoesNotExist, ValueError, TypeError, ArithmeticError) as exc:
        raise ValidationError(
            "Bukti asli uang muka tidak lengkap; minta Owner memeriksa sumber."
        ) from exc


def _organization(value):
    pk = getattr(value, "pk", value)
    if isinstance(pk, bool):
        raise ValidationError("Organisasi tidak valid.")
    try:
        return Organization.objects.get(pk=pk)
    except (Organization.DoesNotExist, ValueError, TypeError) as exc:
        raise ValidationError("Organisasi tidak tersedia.") from exc


@_proof_errors
def _bill_proof(org, bill):
    _scope(bill, org)
    _supplier(org, bill.supplier)
    amount = ledger._amount(bill.amount)
    vat = ledger._amount(bill.supplier_vat, positive=False)
    ledger._actual_date(bill.date)
    if (
        bill.status != "approved"
        or not ZERO <= vat <= amount
        or bill.category not in dict(Bill._meta.get_field("category").flatchoices)
        or bill.supplier_reference_key
        != ledger.normalize_supplier_reference(bill.supplier_reference)
    ):
        raise ValidationError("Sahkan tagihan pemasok sebelum menggunakan uang muka.")
    account = (
        "PREPAID"
        if bill.category == "prepayment" or bill.service_date > bill.date
        else "EXPENSE"
    )
    journal = Journal.objects.get(
        organization=org, source_key=f"bill:{bill.pk}:approval"
    )
    return {
        "id": bill.pk,
        "organization_id": org.pk,
        "supplier_id": bill.supplier_id,
        "number": bill.number,
        "date": bill.date.isoformat(),
        "service_date": bill.service_date.isoformat(),
        "amount": str(amount),
        "supplier_vat": str(bill.supplier_vat),
        "category": bill.category,
        "status": bill.status,
        "supplier_reference": bill.supplier_reference,
        "supplier_reference_key": bill.supplier_reference_key,
        "submission_key": str(bill.submission_key) if bill.submission_key else None,
        "journal": _journal(
            org,
            journal,
            bill.date,
            f"bill:{bill.pk}:approval",
            [(account, amount, ZERO), ("AP", ZERO, amount)],
        ),
    }


@_proof_errors
def _zero_decision(org, bill):
    from taxes.models import BillTaxDecision
    from taxes.workflow import _assert_review

    decision = (
        BillTaxDecision.objects.filter(bill_id=bill.pk)
        .select_related("external_review__evidence")
        .first()
    )
    if (
        not decision
        or decision.organization_id != org.pk
        or bill.tax_status != "reviewed"
        or bill.tax_amount != ZERO
        or decision.rule_code != "no_withholding"
        or decision.amount != ZERO
        or not ZERO <= decision.base <= bill.amount
        or decision.rate != ZERO
        or decision.obligation_id is not None
    ):
        raise ValidationError(
            "Penerapan memerlukan keputusan profesional potongan nol yang sesuai tagihan ini."
        )
    _historical_actor(org, decision.actor_id)
    review = decision.external_review
    _scope(review.evidence, org)
    # Preserve the professional's exact canonical payload. DecimalField storage
    # adds scale ("0" -> Decimal("0.00")); rebuilding that payload changes its
    # digest even though the amount is numerically identical. Validate the
    # recorded decision against typed facts before checking its original seal.
    expected = review.decision
    if (
        not isinstance(expected, dict)
        or set(expected) != {"rule_code", "base", "period", "reason", "object_code"}
        or expected["rule_code"] != decision.rule_code
        or not isinstance(expected["base"], str)
        or ledger._amount(expected["base"], positive=False) != decision.base
        or expected["period"] != decision.period.isoformat()
        or expected["reason"] != decision.reason
        or expected["object_code"] != ""
    ):
        raise ValidationError(
            "Kesimpulan profesional tidak sesuai keputusan potongan nol tagihan asli."
        )
    _assert_review(org, bill, expected, review)
    if not review.evidence_sha256:
        raise ValidationError(
            "Laporan profesional harus mengikat PDF asli keputusan tagihan."
        )
    return {
        "id": decision.pk,
        "rule_code": decision.rule_code,
        "amount": str(decision.amount),
        "actor_id": decision.actor_id,
        "review_id": review.pk,
        "subject_digest": review.subject_digest,
        "evidence_id": review.evidence_id,
        "evidence_sha256": review.evidence_sha256,
        "decision": review.decision,
    }


@_proof_errors
def _application_proof(org, row, *, capture=False):
    proof = _common(org, row)
    source = _cash_proof(org, row.advance)
    bill = _bill_proof(org, row.bill)
    if row.advance.supplier_id != row.bill.supplier_id or row.date < max(
        row.advance.date, row.bill.date
    ):
        raise ValidationError(
            "Pemasok atau tanggal penerapan tidak sesuai uang muka dan tagihan."
        )
    proof.update(
        advance_id=row.advance_id,
        advance_source_hash=_hash(source),
        bill=bill,
        tax_decision=_zero_decision(org, row.bill),
        journal=_journal(
            org,
            row.journal,
            row.date,
            f"supplier-apply:{row.request_key}",
            [("AP", row.amount, ZERO), ("SUPPLIER_ADVANCE", ZERO, row.amount)],
        ),
    )
    return _sealed(row, proof, capture)


@_proof_errors
def _reversal_proof(org, row, *, capture=False):
    proof = _common(org, row)
    application = _application_proof(org, row.application)
    if row.date < row.application.date or row.amount != row.application.amount:
        raise ValidationError(
            "Pembalikan harus utuh dan tidak mendahului penerapan asli."
        )
    proof.update(
        application_id=row.application_id,
        application_source_hash=_hash(application),
        journal=_journal(
            org,
            row.journal,
            row.date,
            f"supplier-reverse:{row.request_key}",
            [("SUPPLIER_ADVANCE", row.amount, ZERO), ("AP", ZERO, row.amount)],
        ),
    )
    return _sealed(row, proof, capture)


def _proof(org, row, *, capture=False):
    if row.action in ("record", "refund"):
        return _cash_proof(org, row, capture=capture)
    if row.action == "apply":
        return _application_proof(org, row, capture=capture)
    return _reversal_proof(org, row, capture=capture)


@_proof_errors
def _graph(org, advance_ids=(), bill_ids=()):
    """Unfiltered connected graph: a foreign/damaged child must not disappear."""
    a_ids, b_ids = set(advance_ids), set(bill_ids)
    while True:
        rows = list(
            SupplierAdvanceApplication.objects.filter(
                Q(advance_id__in=a_ids) | Q(bill_id__in=b_ids)
            ).order_by("pk")
        )
        new_a, new_b = (
            a_ids | {r.advance_id for r in rows},
            b_ids | {r.bill_id for r in rows},
        )
        if new_a == a_ids and new_b == b_ids:
            break
        a_ids, b_ids = new_a, new_b
    cash = list(SupplierAdvance.objects.filter(pk__in=a_ids).order_by("pk"))
    bills = list(Bill.objects.filter(pk__in=b_ids).order_by("pk"))
    if len(cash) != len(a_ids) or len(bills) != len(b_ids):
        raise ValidationError("Sumber uang muka/tagihan tertaut hilang.")
    applications = rows
    refunds = list(
        SupplierAdvanceRefund.objects.filter(advance_id__in=a_ids).order_by("pk")
    )
    reversals = list(
        SupplierAdvanceApplicationReversal.objects.filter(
            application_id__in=[r.pk for r in applications]
        ).order_by("pk")
    )
    events, proofs, dates = [], [], set()
    bank_ids = set()
    for row in cash + applications + refunds + reversals:
        proofs.append(
            {"model": row._meta.label_lower, "id": row.pk, "proof": _proof(org, row)}
        )
        dates.add(row.date)
        if row.action == "record":
            events.append((row.date, row.pk, ZERO, row.amount))
            bank_ids.add(row.bank_posting.transaction_id)
        elif row.action == "apply":
            events.append((row.date, row.advance_id, row.bill_id, -row.amount))
        elif row.action == "refund":
            events.append((row.date, row.advance_id, ZERO, -row.amount))
            bank_ids.add(row.bank_posting.transaction_id)
        else:
            events.append(
                (
                    row.date,
                    row.application.advance_id,
                    row.application.bill_id,
                    row.amount,
                )
            )
    payments = []
    for bill in bills:
        proofs.append({"bill": _bill_proof(org, bill)})
        dates.add(bill.date)
        for payment in (
            BillPayment.objects.filter(bill_id=bill.pk)
            .select_related("transaction")
            .order_by("pk")
        ):
            _scope(payment, org)
            _scope(payment.transaction, org)
            value = ledger._amount(payment.amount)
            if payment.transaction.date < bill.date:
                raise ValidationError("Pembayaran biasa mendahului tagihan asli.")
            bank_ids.add(payment.transaction_id)
            journal = Journal.objects.get(
                organization=org, source_key=f"bill-payment:{payment.pk}"
            )
            proofs.append(
                {
                    "payment_id": payment.pk,
                    "bill_id": bill.pk,
                    "bank": _bank_identity(org, payment.transaction),
                    "journal": _journal(
                        org,
                        journal,
                        payment.transaction.date,
                        f"bill-payment:{payment.pk}",
                        [("AP", value, ZERO), ("BANK", ZERO, value)],
                    ),
                }
            )
            payments.append((payment.transaction.date, bill.pk, value))
            dates.add(payment.transaction.date)
        for settlement in (
            WithholdingSettlement.objects.filter(bill_id=bill.pk)
            .select_related("bank_posting__transaction", "obligation")
            .order_by("pk")
        ):
            _scope(settlement, org)
            _scope(settlement.obligation, org)
            value = ledger._amount(settlement.gross_amount)
            if (
                settlement.date < bill.date
                or value != bill.amount
                or settlement.obligation.source_bill_id != bill.pk
            ):
                raise ValidationError("Pelunasan potongan tidak sesuai tagihan asli.")
            posting = _scope(settlement.bank_posting, org)
            if (
                posting.journal_id != settlement.journal_id
                or posting.date != settlement.date
                or posting.kind != "withheld_supplier"
                or posting.signed_amount != -settlement.net_amount
                or settlement.net_amount + settlement.withholding_amount != value
                or posting.reference != settlement.reference
                or posting.evidence != settlement.evidence
            ):
                raise ValidationError("Jurnal pelunasan potongan tidak sesuai sumber.")
            bank_ids.add(posting.transaction_id)
            proofs.append(
                {
                    "settlement_id": settlement.pk,
                    "bill_id": bill.pk,
                    "journal": _journal(
                        org,
                        settlement.journal,
                        settlement.date,
                        f"bank-event:{posting.reference}",
                        [
                            ("AP", value, ZERO),
                            ("BANK", ZERO, settlement.net_amount),
                            ("TAX_PAYABLE", ZERO, settlement.withholding_amount),
                        ],
                    ),
                }
            )
            payments.append((settlement.date, bill.pk, value))
            dates.add(settlement.date)
    banks = [
        _bank_state(org, BankTransaction.objects.get(pk=pk)) for pk in sorted(bank_ids)
    ]
    return {
        "cash": cash,
        "bills": bills,
        "applications": applications,
        "refunds": refunds,
        "reversals": reversals,
        "events": events,
        "payments": payments,
        "dates": dates,
        "proofs": proofs,
        "banks": banks,
    }


def _conserve(graph, *, candidate=None):
    events, payments, dates = (
        list(graph["events"]),
        list(graph["payments"]),
        set(graph["dates"]),
    )
    if candidate:
        dates.add(candidate["date"])
        if candidate["kind"] == "payment":
            payments.append(
                (candidate["date"], candidate["bill_id"], candidate["amount"])
            )
        else:
            events.append(
                (
                    candidate["date"],
                    candidate["advance_id"],
                    candidate.get("bill_id", ZERO),
                    candidate["delta"],
                )
            )
    for cutoff in sorted(dates):
        for advance in graph["cash"]:
            balance = sum(
                (
                    delta
                    for day, aid, _, delta in events
                    if aid == advance.pk and day <= cutoff
                ),
                ZERO,
            )
            if balance < ZERO:
                raise ValidationError(
                    f"Saldo uang muka negatif pada {cutoff.isoformat()}. Tanggal bank asli tetap; periksa koreksi yang ditinjau."
                )
        for bill in graph["bills"]:
            approved = bill.amount if bill.date <= cutoff else ZERO
            paid = sum(
                (
                    value
                    for day, bid, value in payments
                    if bid == bill.pk and day <= cutoff
                ),
                ZERO,
            )
            application_net = -sum(
                (
                    delta
                    for day, _, bid, delta in events
                    if bid == bill.pk and day <= cutoff
                ),
                ZERO,
            )
            if approved - paid - application_net < ZERO:
                raise ValidationError(
                    f"Utang tagihan negatif pada {cutoff.isoformat()}. Periksa pembayaran dan penerapan bertanggal asli."
                )


def validate_supplier_advance_bill_payment(*, organization, bill, date, amount):
    """Called under the existing organization lock before ordinary cash posting."""
    org = _organization(organization)
    row = _select(Bill, bill, org)
    graph = _graph(org, bill_ids=[row.pk])
    _conserve(
        graph,
        candidate={
            "kind": "payment",
            "date": ledger._actual_date(date),
            "bill_id": row.pk,
            "amount": ledger._amount(amount),
        },
    )


@transaction.atomic
def supplier_advance_bill_settlement(*, organization, bill, as_of):
    """Validated net noncash settlement and immutable source facts for adapters."""
    org = ledger._lock_org(organization)
    row = _select(Bill, bill, org)
    cutoff = ledger._date(as_of)
    graph = _graph(org, bill_ids=[row.pk])
    _conserve(graph)
    applications = [
        r for r in graph["applications"] if r.bill_id == row.pk and r.date <= cutoff
    ]
    reversals = [
        r
        for r in graph["reversals"]
        if r.application.bill_id == row.pk and r.date <= cutoff
    ]
    return {
        "amount": sum((r.amount for r in applications), ZERO)
        - sum((r.amount for r in reversals), ZERO),
        "applications": [
            {
                "id": r.pk,
                "date": r.date,
                "amount": r.amount,
                "proof": _application_proof(org, r),
            }
            for r in applications
        ],
        "reversals": [
            {
                "id": r.pk,
                "application_id": r.application_id,
                "date": r.date,
                "amount": r.amount,
                "proof": _reversal_proof(org, r),
            }
            for r in reversals
        ],
        "fingerprint": _hash({"proofs": graph["proofs"], "banks": graph["banks"]}),
    }


@transaction.atomic
def supplier_advance_application_source_snapshot(*, organization, application):
    """Stable exact application and original cash proof for budget attribution."""
    org = ledger._lock_org(organization)
    row = _select(SupplierAdvanceApplication, application, org)
    try:
        proof = _application_proof(org, row)
        return {
            **proof,
            "application_id": row.pk,
            "advance_id": row.advance_id,
            "bill_id": row.bill_id,
            "source": supplier_advance_source_snapshot(
                organization=org, advance=row.advance
            ),
            "fingerprint": _hash(proof),
        }
    except (ObjectDoesNotExist, ValueError, TypeError, ArithmeticError) as exc:
        raise ValidationError("Bukti penerapan uang muka tidak lengkap.") from exc


def _position(org, row, cutoff, graph, *, include_tax=True):
    _conserve(graph)
    applications = [
        r for r in graph["applications"] if r.advance_id == row.pk and r.date <= cutoff
    ]
    refunds = [
        r for r in graph["refunds"] if r.advance_id == row.pk and r.date <= cutoff
    ]
    reversals = [
        r
        for r in graph["reversals"]
        if r.application.advance_id == row.pk and r.date <= cutoff
    ]
    paid = row.amount if row.date <= cutoff else ZERO
    applied, refunded, reversed_amount = (
        sum((r.amount for r in items), ZERO)
        for items in (applications, refunds, reversals)
    )
    history = []
    for event in (
        ([row] if row.date <= cutoff else []) + applications + refunds + reversals
    ):
        reversal = (
            next((r for r in reversals if r.application_id == event.pk), None)
            if event.action == "apply"
            else None
        )
        history.append(
            {
                "kind": event.action,
                "id": event.pk,
                "date": event.date,
                "amount": event.amount,
                "reference": event.source_reference,
                "reason": event.reason,
                "evidence": event.evidence,
                "journal_id": event.journal_id,
                "bill_id": getattr(event, "bill_id", None)
                or (event.application.bill_id if event.action == "reverse" else None),
                "actor_id": event.actor_id,
                "actor_name": event.actor.get_full_name() or event.actor.get_username(),
                "application_id": event.pk
                if event.action == "apply"
                else getattr(event, "application_id", None),
                "reversed": reversal is not None,
                "reversal_id": reversal.pk if reversal else None,
            }
        )
    history.sort(key=lambda event: (event["date"], event["kind"], event["id"]))
    result = {
        "current": True,
        "issues": [],
        "paid": paid,
        "applied": applied,
        "refunded": refunded,
        "reversed": reversed_amount,
        "effective_applied": applied - reversed_amount,
        "remaining": paid - applied + reversed_amount - refunded,
        "history": history,
        "tax_review_status": "pending",
        "tax_blockers": [],
        "fingerprint": _hash(
            {
                "proofs": graph["proofs"],
                "banks": graph["banks"],
                "cutoff": cutoff.isoformat(),
            }
        ),
    }
    if include_tax:
        from taxes.supplier_advance_review import supplier_advance_tax_context

        review = supplier_advance_tax_context(organization=org, advance=row)
        result.update(
            tax_review_status=review["status"],
            tax_blockers=review["blockers"],
            tax_context=review,
        )
    return result


@transaction.atomic
def supplier_advance_financial_snapshot(*, organization, advance, as_of=None):
    """Validated dated Finance-only facts; no tax-review state in its digest."""
    org = ledger._lock_org(organization)
    row = _select(SupplierAdvance, advance, org)
    cutoff = ledger._date(as_of or timezone.localdate())
    try:
        graph = _graph(org, advance_ids=[row.pk])
        position = _position(org, row, cutoff, graph, include_tax=False)
        events = []
        for event in (
            [row] + graph["applications"] + graph["refunds"] + graph["reversals"]
        ):
            aid = (
                event.pk
                if event.action == "record"
                else event.application.advance_id
                if event.action == "reverse"
                else event.advance_id
            )
            if aid != row.pk or event.date > cutoff:
                continue
            events.append(
                {
                    "kind": event.action,
                    "id": event.pk,
                    "date": event.date.isoformat(),
                    "amount": str(event.amount),
                    "journal_id": event.journal_id,
                    "source_hash": event.source_hash,
                    "advance_id": row.pk,
                    "bill_id": getattr(event, "bill_id", None)
                    or (
                        event.application.bill_id if event.action == "reverse" else None
                    ),
                    "application_id": getattr(event, "application_id", None),
                    "proof": _proof(org, event),
                }
            )
        events.sort(key=lambda item: (item["date"], item["kind"], item["id"]))
        source = supplier_advance_source_snapshot(organization=org, advance=row)
        result = {
            key: position[key]
            for key in (
                "current",
                "issues",
                "paid",
                "applied",
                "refunded",
                "reversed",
                "effective_applied",
                "remaining",
            )
        }
        result.update(
            source=source,
            events=events,
            fingerprint=_hash(
                {
                    "source": source,
                    "events": events,
                    "graph": {"proofs": graph["proofs"], "banks": graph["banks"]},
                    "cutoff": cutoff.isoformat(),
                }
            ),
        )
        return result
    except (
        ValidationError,
        ObjectDoesNotExist,
        ValueError,
        TypeError,
        ArithmeticError,
    ) as exc:
        issues = (
            exc.messages
            if isinstance(exc, ValidationError)
            else ["Bukti uang muka tidak lengkap; minta Owner memeriksa sumber."]
        )
        return {
            "source": None,
            "events": [],
            "current": False,
            "issues": issues,
            "paid": None,
            "applied": None,
            "refunded": None,
            "reversed": None,
            "remaining": None,
            "effective_applied": None,
            "fingerprint": _hash(
                {"version": VERSION, "advance_id": row.pk, "invalid": issues}
            ),
        }


@transaction.atomic
def supplier_advance_position(*, organization, advance, as_of=None):
    org = ledger._lock_org(organization)
    row = _select(SupplierAdvance, advance, org)
    cutoff = ledger._date(as_of or timezone.localdate())
    try:
        return _position(org, row, cutoff, _graph(org, advance_ids=[row.pk]))
    except (
        ValidationError,
        ObjectDoesNotExist,
        ValueError,
        TypeError,
        ArithmeticError,
    ) as exc:
        issues = (
            exc.messages
            if isinstance(exc, ValidationError)
            else ["Bukti uang muka tidak lengkap; minta Owner memeriksa sumber."]
        )
        return {
            "current": False,
            "issues": issues,
            "paid": None,
            "applied": None,
            "refunded": None,
            "reversed": None,
            "effective_applied": None,
            "remaining": None,
            "history": [],
            "tax_review_status": "held",
            "tax_blockers": issues,
            "fingerprint": _hash(
                {"version": VERSION, "advance_id": row.pk, "invalid": issues}
            ),
        }


@transaction.atomic
def supplier_advance_action_context(
    *,
    organization,
    action,
    supplier=None,
    transaction=None,
    advance=None,
    bill=None,
    application=None,
    date=None,
    amount=None,
):
    org = ledger._lock_org(organization)
    if action not in MODELS:
        raise ValidationError("Tindakan uang muka tidak didukung.")
    before, position, graph, sources = {}, None, None, {}
    try:
        row = _select(SupplierAdvance, advance, org) if advance is not None else None
        target = _select(Bill, bill, org) if bill is not None else None
        original = (
            _select(SupplierAdvanceApplication, application, org)
            if application is not None
            else None
        )
        bank = (
            _select(BankTransaction, transaction, org)
            if transaction is not None
            else None
        )
        party = _select(Party, supplier, org) if supplier is not None else None
        if original:
            row, target = (
                _select(SupplierAdvance, original.advance_id, org),
                _select(Bill, original.bill_id, org),
            )
            _application_proof(org, original)
            before["application"] = {
                "id": original.pk,
                "date": original.date,
                "amount": original.amount,
                "bill_id": original.bill_id,
                "advance_id": original.advance_id,
            }
        if row or target:
            graph = _graph(
                org,
                advance_ids=[row.pk] if row else [],
                bill_ids=[target.pk] if target else [],
            )
            _conserve(graph)
            sources["graph"] = {"proofs": graph["proofs"], "banks": graph["banks"]}
        if row:
            position = _position(org, row, timezone.localdate(), graph)
            review = position["tax_context"]
            # Context also contains model instances for normal template/history
            # rendering. Freshness binds its sealed tokens, never their repr.
            sources["tax"] = {
                key: review[key]
                for key in ("fingerprint", "source_fingerprint", "status", "version")
            }
            party = row.supplier
            before["advance"] = {
                "id": row.pk,
                "date": row.date,
                "source_reference": row.source_reference,
                "amount": row.amount,
            }
        if party:
            _supplier(org, party)
            sources["supplier"] = {
                "id": party.pk,
                "organization_id": party.organization_id,
                "kind": party.kind,
            }
            before["supplier"] = {"id": party.pk, "name": party.name}
        if bank:
            sources["bank"] = _bank_state(org, bank)
            before["transaction"] = {
                "id": bank.pk,
                "date": bank.date,
                "reference": bank.reference,
                "description": bank.description,
                "amount": bank.amount,
                "remaining": Decimal(sources["bank"]["remaining"]),
                "account_id": bank.account_id,
                "account_name": bank.account.name,
            }
        if target:
            sources["bill"] = _bill_proof(org, target)
            if action == "apply":
                sources["bill_tax"] = _zero_decision(org, target)
                if not row or row.supplier_id != target.supplier_id:
                    raise ValidationError(
                        "Uang muka dan tagihan harus milik pemasok yang sama."
                    )
            before["bill"] = {
                "id": target.pk,
                "number": target.number,
                "date": target.date,
                "amount": target.amount,
                "outstanding": target.outstanding_amount,
            }
        if action == "record" and (not party or not bank or bank.amount >= ZERO):
            raise ValidationError("Pilih pemasok dan mutasi bank keluar asli.")
        if action == "apply" and (not row or not target):
            raise ValidationError("Pilih uang muka dan tagihan yang disahkan.")
        if action == "refund" and (not row or not bank or bank.amount <= ZERO):
            raise ValidationError(
                "Pilih uang muka dan penerimaan pengembalian bank asli."
            )
        if action == "reverse" and not original:
            raise ValidationError("Pilih penerapan asli yang akan dibalik seluruhnya.")
        blockers = []
        posting_date = (
            bank.date
            if bank
            else ledger._actual_date(date)
            if date is not None
            else None
        )
        if posting_date:
            try:
                assert_open_period(org.pk, posting_date)
            except ValidationError as exc:
                blockers.extend(exc.messages)
        if action == "reverse":
            if SupplierAdvanceApplicationReversal.objects.filter(
                application_id=original.pk
            ).exists():
                blockers.append(
                    "Penerapan ini sudah dibalik. Catatan asli tetap ada pada riwayat."
                )
            from .supplier_advance_budget import (
                validate_supplier_advance_reversal_budget,
            )

            try:
                validate_supplier_advance_reversal_budget(
                    organization=org,
                    application=original,
                    as_of=posting_date or timezone.localdate(),
                )
            except ValidationError as exc:
                blockers.extend(exc.messages)
        # Candidate feasibility belongs to preview, not the stable source hash.
        # The command repeats all guards under the shared organization lock.
        if amount is not None or (action == "reverse" and posting_date is not None):
            try:
                value = (
                    original.amount if action == "reverse" else ledger._amount(amount)
                )
                if action == "record":
                    if value > -Decimal(sources["bank"]["remaining"]):
                        raise ValidationError(
                            "Jumlah melebihi sisa uang keluar bank asli."
                        )
                elif action == "apply" and posting_date is not None:
                    if posting_date < max(row.date, target.date):
                        raise ValidationError(
                            "Tanggal penerapan mendahului uang muka atau tagihan."
                        )
                    _conserve(
                        graph,
                        candidate={
                            "kind": "apply",
                            "date": posting_date,
                            "advance_id": row.pk,
                            "bill_id": target.pk,
                            "delta": -value,
                        },
                    )
                elif action == "refund":
                    if (
                        value > Decimal(sources["bank"]["remaining"])
                        or posting_date < row.date
                    ):
                        raise ValidationError(
                            "Jumlah/tanggal pengembalian tidak sesuai sisa bank dan uang muka."
                        )
                    _conserve(
                        graph,
                        candidate={
                            "kind": "refund",
                            "date": posting_date,
                            "advance_id": row.pk,
                            "delta": -value,
                        },
                    )
                elif action == "reverse":
                    if posting_date < original.date:
                        raise ValidationError(
                            "Pembalikan tidak boleh mendahului penerapan asli."
                        )
                    _conserve(
                        graph,
                        candidate={
                            "kind": "reverse",
                            "date": posting_date,
                            "advance_id": row.pk,
                            "bill_id": target.pk,
                            "delta": value,
                        },
                    )
            except ValidationError as exc:
                blockers.extend(exc.messages)
        return {
            "current": True,
            "issues": [],
            "blockers": blockers,
            "before": before,
            "position": position,
            "fingerprint": _hash(
                {"version": VERSION, "action": action, "sources": sources}
            ),
        }
    except (
        ValidationError,
        ObjectDoesNotExist,
        ValueError,
        TypeError,
        ArithmeticError,
    ) as exc:
        issues = (
            exc.messages
            if isinstance(exc, ValidationError)
            else ["Sumber tidak lengkap; periksa catatan asli bersama Owner."]
        )
        return {
            "current": False,
            "issues": issues,
            "blockers": issues,
            "before": {},
            "position": position,
            "fingerprint": _hash(
                {"version": VERSION, "action": action, "invalid": issues}
            ),
        }


def _replay(org, action, key, payload):
    old = MODELS[action].objects.filter(organization=org, request_key=key).first()
    if old:
        _proof(org, old)
        if old.request_payload != payload:
            raise ValidationError(
                "Kunci permintaan sudah dipakai untuk keputusan berbeda. Periksa ulang sumber."
            )
        return old
    return None


def _fresh(org, action, expected_fingerprint, **sources):
    context = supplier_advance_action_context(
        organization=org, action=action, **sources
    )
    if not context["current"] or context["blockers"]:
        raise ValidationError(context["issues"] + context["blockers"])
    if context["fingerprint"] != expected_fingerprint:
        raise ValidationError(
            "Sumber berubah sejak diperiksa. Isian dipertahankan; periksa ulang dan konfirmasi kembali."
        )


def _save(org, action, actor, key, payload, journal, **sources):
    row = MODELS[action](
        organization=org,
        actor=actor,
        amount=Decimal(payload["amount"]),
        date=Date.fromisoformat(payload["date"]),
        reference=f"supplier-{action}:{key}",
        source_reference=payload["source_reference"],
        reason=payload["reason"],
        evidence=payload["evidence"],
        request_key=key,
        request_payload=payload,
        journal=journal,
        **sources,
    )
    row.source_snapshot = _proof(org, row, capture=True)
    row.source_hash = _hash(row.source_snapshot)
    row._service_transition = True
    try:
        row.save()
    finally:
        row.__dict__.pop("_service_transition", None)
    detail = {
        "action": action,
        "cash": action in ("record", "refund"),
        "amount": str(row.amount),
        "journal_id": row.journal_id,
        "request_key": str(key),
        "source_hash": row.source_hash,
        **{
            name: getattr(row, name, None)
            for name in (
                "advance_id",
                "supplier_id",
                "bill_id",
                "application_id",
                "bank_posting_id",
            )
        },
    }
    record(org, actor, f"finance.supplier_advance.{action}", obj=row, detail=detail)
    ledger._event(org, f"supplier_advance.{action}", row, detail)
    return row


def _confirmed(value):
    if value is not True:
        raise ValidationError(
            "Periksa sumber dan konfirmasi keputusan sebelum menyimpan."
        )


@transaction.atomic
def record_supplier_advance(
    *,
    organization,
    actor,
    supplier,
    transaction,
    amount,
    source_reference,
    reason,
    evidence,
    request_key,
    expected_fingerprint,
    confirmed,
    purpose="",
):
    org = ledger._lock_org(organization)
    ledger._write_actor(org, actor)
    party = _select(Party, supplier, org, lock=True)
    bank = _select(BankTransaction, transaction, org, lock=True)
    _supplier(org, party)
    _bank_identity(org, bank)
    amount, key, day = (
        ledger._amount(amount),
        _key(request_key),
        ledger._actual_date(bank.date),
    )
    payload = _payload(
        action="record",
        actor=actor,
        amount=amount,
        date=day,
        source_reference=source_reference,
        reason=reason,
        evidence=evidence,
        expected_fingerprint=expected_fingerprint,
        bank=bank,
        supplier=party,
        purpose=purpose,
    )
    _confirmed(confirmed)
    old = _replay(org, "record", key, payload)
    if old:
        if old.supplier_id != party.pk:
            raise ValidationError("Kunci permintaan sudah menunjuk pemasok lain.")
        return old
    _fresh(org, "record", expected_fingerprint, supplier=party, transaction=bank)
    if bank.amount >= ZERO or amount > -Decimal(_bank_state(org, bank)["remaining"]):
        raise ValidationError("Jumlah uang muka melebihi sisa uang keluar bank asli.")
    posting = ledger._bank_post(
        org,
        bank,
        -amount,
        "supplier_advance",
        f"supplier-record:{key}",
        payload["evidence"],
        [("SUPPLIER_ADVANCE", amount, ZERO), ("BANK", ZERO, amount)],
        actor,
    )
    return _save(
        org,
        "record",
        actor,
        key,
        payload,
        posting.journal,
        supplier=party,
        bank_posting=posting,
        purpose=payload["purpose"],
    )


@transaction.atomic
def apply_supplier_advance(
    *,
    organization,
    actor,
    advance,
    bill,
    date,
    amount,
    reason,
    evidence,
    request_key,
    expected_fingerprint,
    confirmed,
    source_reference="",
):
    org = ledger._lock_org(organization)
    ledger._write_actor(org, actor)
    row = _select(SupplierAdvance, advance, org, lock=True)
    target = _select(Bill, bill, org, lock=True)
    _cash_proof(org, row)
    _bill_proof(org, target)
    _zero_decision(org, target)
    amount, key, day = (
        ledger._amount(amount),
        _key(request_key),
        ledger._actual_date(date),
    )
    payload = _payload(
        action="apply",
        actor=actor,
        amount=amount,
        date=day,
        source_reference=source_reference,
        reason=reason,
        evidence=evidence,
        expected_fingerprint=expected_fingerprint,
        advance=row,
        bill=target,
    )
    _confirmed(confirmed)
    old = _replay(org, "apply", key, payload)
    if old:
        return old
    _fresh(org, "apply", expected_fingerprint, advance=row, bill=target)
    if row.supplier_id != target.supplier_id or day < max(row.date, target.date):
        raise ValidationError(
            "Pemasok dan tanggal penerapan harus sesuai uang muka/tagihan asli."
        )
    graph = _graph(org, advance_ids=[row.pk], bill_ids=[target.pk])
    _conserve(
        graph,
        candidate={
            "kind": "apply",
            "date": day,
            "advance_id": row.pk,
            "bill_id": target.pk,
            "delta": -amount,
        },
    )
    journal = ledger._post(
        org,
        day,
        f"supplier-apply:{key}",
        "Penerapan uang muka pemasok",
        [("AP", amount, ZERO), ("SUPPLIER_ADVANCE", ZERO, amount)],
    )
    result = _save(org, "apply", actor, key, payload, journal, advance=row, bill=target)
    from .budget_adapter import sync_bill_budget

    sync_bill_budget(organization=org, bill=target, actor=actor)
    return result


@transaction.atomic
def refund_supplier_advance(
    *,
    organization,
    actor,
    advance,
    transaction,
    amount,
    reason,
    evidence,
    request_key,
    expected_fingerprint,
    confirmed,
    source_reference="",
):
    org = ledger._lock_org(organization)
    ledger._write_actor(org, actor)
    row = _select(SupplierAdvance, advance, org, lock=True)
    bank = _select(BankTransaction, transaction, org, lock=True)
    _cash_proof(org, row)
    _bank_identity(org, bank)
    amount, key, day = (
        ledger._amount(amount),
        _key(request_key),
        ledger._actual_date(bank.date),
    )
    payload = _payload(
        action="refund",
        actor=actor,
        amount=amount,
        date=day,
        source_reference=source_reference,
        reason=reason,
        evidence=evidence,
        expected_fingerprint=expected_fingerprint,
        advance=row,
        bank=bank,
    )
    _confirmed(confirmed)
    old = _replay(org, "refund", key, payload)
    if old:
        return old
    _fresh(org, "refund", expected_fingerprint, advance=row, transaction=bank)
    if (
        bank.amount <= ZERO
        or amount > Decimal(_bank_state(org, bank)["remaining"])
        or day < row.date
    ):
        raise ValidationError(
            "Pengembalian memerlukan sisa penerimaan bank asli setelah uang muka."
        )
    graph = _graph(org, advance_ids=[row.pk])
    _conserve(
        graph,
        candidate={
            "kind": "refund",
            "date": day,
            "advance_id": row.pk,
            "delta": -amount,
        },
    )
    posting = ledger._bank_post(
        org,
        bank,
        amount,
        "supplier_advance_refund",
        f"supplier-refund:{key}",
        payload["evidence"],
        [("BANK", amount, ZERO), ("SUPPLIER_ADVANCE", ZERO, amount)],
        actor,
    )
    return _save(
        org,
        "refund",
        actor,
        key,
        payload,
        posting.journal,
        advance=row,
        bank_posting=posting,
    )


@transaction.atomic
def reverse_supplier_advance_application(
    *,
    organization,
    actor,
    application,
    date,
    reason,
    evidence,
    request_key,
    expected_fingerprint,
    confirmed,
    source_reference="",
):
    org = ledger._lock_org(organization)
    ledger._owner(org, actor)
    original = _select(SupplierAdvanceApplication, application, org, lock=True)
    _application_proof(org, original)
    row, target = original.advance, original.bill
    amount, key, day = original.amount, _key(request_key), ledger._actual_date(date)
    payload = _payload(
        action="reverse",
        actor=actor,
        amount=amount,
        date=day,
        source_reference=source_reference,
        reason=reason,
        evidence=evidence,
        expected_fingerprint=expected_fingerprint,
        application=original,
    )
    _confirmed(confirmed)
    old = _replay(org, "reverse", key, payload)
    if old:
        return old
    _fresh(org, "reverse", expected_fingerprint, application=original)
    if (
        day < original.date
        or SupplierAdvanceApplicationReversal.objects.filter(
            application_id=original.pk
        ).exists()
    ):
        raise ValidationError(
            "Penerapan sudah dibalik atau tanggal mendahului penerapan asli."
        )
    from .supplier_advance_budget import validate_supplier_advance_reversal_budget

    validate_supplier_advance_reversal_budget(
        organization=org, application=original, as_of=day
    )
    graph = _graph(org, advance_ids=[row.pk], bill_ids=[target.pk])
    _conserve(
        graph,
        candidate={
            "kind": "reverse",
            "date": day,
            "advance_id": row.pk,
            "bill_id": target.pk,
            "delta": amount,
        },
    )
    journal = ledger._post(
        org,
        day,
        f"supplier-reverse:{key}",
        "Pembalikan penerapan uang muka pemasok",
        [("SUPPLIER_ADVANCE", amount, ZERO), ("AP", ZERO, amount)],
    )
    result = _save(org, "reverse", actor, key, payload, journal, application=original)
    from .budget_adapter import sync_bill_budget

    sync_bill_budget(organization=org, bill=target, actor=actor)
    return result


@transaction.atomic
def supplier_advance_close_facts(*, organization, cutoff):
    """Dated asset/AP tie plus unresolved tax gates, including exhausted cash."""
    org = ledger._lock_org(organization)
    end = ledger._date(cutoff)
    asset = applied = ZERO
    blockers = []
    rows = SupplierAdvance.objects.filter(
        Q(date__lte=end) | Q(journal__date__lte=end), organization=org
    ).order_by("pk")
    for row in rows:
        try:
            position = _position(
                org, row, end, _graph(org, advance_ids=[row.pk]), include_tax=False
            )
            asset += position["remaining"]
            applied += position["applied"] - position["reversed"]
        except (
            ValidationError,
            ObjectDoesNotExist,
            ValueError,
            TypeError,
            ArithmeticError,
        ) as exc:
            blockers.extend(
                exc.messages
                if isinstance(exc, ValidationError)
                else ["Bukti uang muka pemasok tidak lengkap."]
            )
    from taxes.supplier_advance_review import supplier_advance_tax_gates

    gates = supplier_advance_tax_gates(organization=org, cutoff=end)
    blockers.extend(gates["blockers"])
    return {
        "asset": asset,
        "applied": applied,
        "blockers": blockers,
        "tax_fingerprint": gates["fingerprint"],
    }
