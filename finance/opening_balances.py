"""Opening principal and evidenced cash behind one dated, immutable interface.

An opening item carries an outstanding principal established outside this app.
It creates no invoice, Bill, cash, sale, cost, delivery, or tax decision. Cash
matching records the actual amount allocated to that principal. Fiscal review
is a separate dependency and must not turn missing evidence into zero tax.
"""

import hashlib
import json
from datetime import date as Date
from decimal import Decimal
from functools import wraps
from uuid import UUID

from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import transaction
from django.db.models import Q, Subquery
from django.utils import timezone

from core.audit import record
from core.models import AuditEvent, DomainEvent, Membership, Organization

from . import services as ledger
from .budget_adapter import _journal
from .models import (
    ZERO,
    AccountingPeriod,
    Allocation,
    BankAccount,
    BankPosting,
    BankTransaction,
    Bill,
    BillPayment,
    Invoice,
    Journal,
    JournalLine,
    OpeningBalanceCancellation,
    OpeningBalanceCorrection,
    OpeningBalanceItem,
    OpeningBalanceRefund,
    OpeningBalanceSettlement,
    Party,
    assert_open_period,
    normalize_supplier_reference,
)
from .opening_balance_models import VERSION
from .supplier_advances import _bank_identity, _bank_state

MODELS = {
    "record": OpeningBalanceItem,
    "settle": OpeningBalanceSettlement,
    "correct": OpeningBalanceCorrection,
    "cancel": OpeningBalanceCancellation,
    "refund": OpeningBalanceRefund,
}
PAYLOAD_KEYS = {
    "action",
    "actor_id",
    "kind",
    "party_id",
    "item_id",
    "transaction_id",
    "amount",
    "date",
    "source_date",
    "source_reference",
    "reason",
    "evidence",
    "viewed_fingerprint",
}
CORRUPT = (
    "Bukti saldo awal tidak lengkap atau berubah. Minta Owner memeriksa catatan asli."
)


def _hash(value):
    def encode(item):
        if isinstance(item, Date):
            return item.isoformat()
        if isinstance(item, (Decimal, UUID)):
            return str(item)
        raise TypeError("Unsupported opening source value")

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
        except (
            ObjectDoesNotExist,
            ValueError,
            TypeError,
            ArithmeticError,
            KeyError,
        ) as exc:
            raise ValidationError(CORRUPT) from exc

    return checked


def _scope(obj, org):
    if obj.organization_id != org.pk:
        raise ValidationError("Sumber saldo awal berasal dari perusahaan lain.")
    return obj


def _select(model, value, org, *, lock=False):
    pk = getattr(value, "pk", value)
    if (
        isinstance(pk, bool)
        or not isinstance(pk, (int, str))
        or (isinstance(pk, str) and not pk.isdecimal())
        or int(pk) <= 0
    ):
        raise ValidationError(
            "Pilih sumber saldo awal yang tersedia untuk perusahaan ini."
        )
    return ledger._scoped(model, int(pk), org, lock=lock)


def _org(value):
    pk = getattr(value, "pk", value)
    if isinstance(pk, bool):
        raise ValidationError("Perusahaan tidak valid.")
    try:
        return Organization.objects.get(pk=pk)
    except (Organization.DoesNotExist, ValueError, TypeError) as exc:
        raise ValidationError("Perusahaan tidak tersedia.") from exc


def _key(value):
    try:
        key = UUID(str(value))
        if str(value) != str(key):
            raise ValueError
        return key
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValidationError(
            "Periksa ulang sumber untuk mendapatkan kunci permintaan yang valid."
        ) from exc


def _fingerprint(value):
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise ValidationError("Periksa sumber terlebih dahulu sebelum menyimpan.")
    return value


def _party(org, row, kind):
    _scope(row, org)
    if kind not in OpeningBalanceItem.Kind.values or row.kind not in Party.Kind.values:
        raise ValidationError(
            "Pilih piutang pelanggan/mitra atau utang pemasok yang valid."
        )
    if (kind == "payable") != (row.kind == "supplier"):
        raise ValidationError(
            "Piutang memerlukan pelanggan/mitra; utang memerlukan pemasok."
        )
    return row


def _document_identity(org, party, kind, reference):
    """Reject the same document in the ordinary and opening subledgers."""
    identity = normalize_supplier_reference(reference)
    if not identity:
        raise ValidationError("Nomor dokumen asal wajib diisi.")
    if kind == "receivable":
        sources = Invoice.objects.filter(organization=org, party=party).values_list(
            "number", flat=True
        )
        identities = [normalize_supplier_reference(number) for number in sources]
    else:
        sources = Bill.objects.filter(organization=org, supplier=party).values_list(
            "number", "supplier_reference"
        )
        identities = [
            normalize_supplier_reference(value)
            for source in sources
            for value in source
            if value
        ]
    if identity in identities:
        raise ValidationError(
            "Dokumen ini sudah ada sebagai invoice/tagihan biasa. Gunakan saldo dokumen tersebut agar tidak dihitung dua kali."
        )
    return identity


def _payload(
    *,
    action,
    actor,
    amount,
    date,
    source_date,
    source_reference,
    reason,
    evidence,
    expected_fingerprint,
    kind,
    party,
    item=None,
    bank=None,
    direction=None,
    settlement=None,
):
    payload = {
        "action": action,
        "actor_id": actor.pk,
        "kind": kind,
        "party_id": party.pk,
        "item_id": item.pk if item else None,
        "transaction_id": bank.pk if bank else None,
        "amount": str(amount),
        "date": date.isoformat(),
        "source_date": source_date.isoformat(),
        "source_reference": ledger._review_text(
            source_reference, "Nomor dokumen asal", limit=160
        ),
        "reason": ledger._review_text(reason, "Alasan saldo awal"),
        "evidence": ledger._review_text(evidence, "Bukti saldo / pembayaran"),
        "viewed_fingerprint": _fingerprint(expected_fingerprint),
    }
    if action == "correct":
        payload["direction"] = _direction(direction)
    if action in {"cancel", "refund"}:
        payload["settlement_id"] = settlement.pk
    return payload


def _common(org, row):
    _scope(row, org)
    if not Membership.all_objects.filter(
        organization=org, user_id=row.actor_id
    ).exists():
        raise ValidationError(
            "Pelaku asli tidak mempunyai riwayat keanggotaan perusahaan ini."
        )
    key, amount, day = (
        _key(row.request_key),
        ledger._amount(row.amount),
        ledger._actual_date(row.date),
    )
    payload = row.request_payload
    keys = PAYLOAD_KEYS | (
        {"direction"}
        if row.action == "correct"
        else {"settlement_id"}
        if row.action in {"cancel", "refund"}
        else set()
    )
    if (
        row.workflow_version != VERSION
        or not isinstance(payload, dict)
        or set(payload) != keys
    ):
        raise ValidationError(CORRUPT)
    for name in (
        "actor_id",
        "party_id",
        "item_id",
        "transaction_id",
        *(["settlement_id"] if row.action in {"cancel", "refund"} else []),
    ):
        if payload[name] is not None and (
            type(payload[name]) is not int or payload[name] <= 0
        ):
            raise ValidationError(CORRUPT)
    _fingerprint(payload["viewed_fingerprint"])
    for name, label in (("reason", "Alasan"), ("evidence", "Bukti")):
        if ledger._review_text(getattr(row, name), label) != getattr(row, name):
            raise ValidationError(CORRUPT)
    item = row if row.action == "record" else row.item
    source_date = ledger._actual_date(item.source_date)
    expected = {
        "action": row.action,
        "actor_id": row.actor_id,
        "kind": item.kind,
        "party_id": item.party_id,
        "item_id": None if row.action == "record" else item.pk,
        "transaction_id": row.bank_posting.transaction_id
        if row.action in {"settle", "refund"}
        else row.settlement.bank_posting.transaction_id
        if row.action == "cancel"
        else None,
        "amount": str(amount),
        "date": day.isoformat(),
        "source_date": source_date.isoformat(),
        "source_reference": item.source_reference,
        "reason": row.reason,
        "evidence": row.evidence,
    }
    if row.action == "correct":
        expected["direction"] = _direction(row.direction)
    if row.action in {"cancel", "refund"}:
        expected["settlement_id"] = row.settlement_id
    if (
        any(payload[name] != value for name, value in expected.items())
        or row.reference != f"opening-{row.action}:{key}"
    ):
        raise ValidationError(
            "Nominal, tanggal atau sumber berbeda dari keputusan saldo awal asli."
        )
    return {
        "version": VERSION,
        "action": row.action,
        "organization_id": org.pk,
        "actor_id": row.actor_id,
        "request_key": str(key),
        "request_payload": payload,
        "date": day.isoformat(),
        "amount": str(amount),
        "reference": row.reference,
        "reason": row.reason,
        "evidence_sha256": _hash(row.evidence),
    }


def _seal(row, proof, capture):
    if not capture and (
        row.source_snapshot != proof or row.source_hash != _hash(proof)
    ):
        raise ValidationError(
            "Bukti saldo awal berbeda dari sumber asli yang telah disimpan."
        )
    return proof


@_proof_errors
def _item_proof(org, row, *, capture=False):
    proof = _common(org, row)
    party = _party(org, row.party, row.kind)
    if (
        row.source_date > row.date
        or not row.source_reference.strip()
        or row.source_identity != normalize_supplier_reference(row.source_reference)
    ):
        raise ValidationError(
            "Tanggal atau nomor dokumen asal tidak sesuai saldo awal."
        )
    _scope(row.journal, org)
    entries = (
        [("AR", row.amount, ZERO), ("EQUITY", ZERO, row.amount)]
        if row.kind == "receivable"
        else [("EQUITY", row.amount, ZERO), ("AP", ZERO, row.amount)]
    )
    proof.update(
        kind=row.kind,
        party_id=party.pk,
        source_date=row.source_date.isoformat(),
        source_reference=row.source_reference,
        source_identity=row.source_identity,
        journal=_journal(
            org,
            key=f"opening:{row.reference}",
            date=row.date,
            entries=entries,
            journal_id=row.journal_id,
        ),
    )
    return _seal(row, proof, capture)


@_proof_errors
def _settlement_proof(org, row, *, capture=False):
    proof = _common(org, row)
    original = _item_proof(org, row.item)
    posting = _scope(row.bank_posting, org)
    bank = _bank_identity(org, posting.transaction)
    if row.date < row.item.date:
        raise ValidationError("Pembayaran tidak boleh mendahului tanggal saldo awal.")
    incoming = row.item.kind == "receivable"
    sign = row.amount if incoming else -row.amount
    kind = "opening_receipt" if incoming else "opening_payment"
    if (
        posting.kind != kind
        or posting.signed_amount != sign
        or posting.date != row.date
        or posting.transaction.date != row.date
        or posting.journal_id != row.journal_id
        or posting.reference != row.reference
        or posting.evidence != row.evidence
        or posting.transaction.amount * sign <= ZERO
    ):
        raise ValidationError(
            "Pembayaran saldo awal tidak sesuai uang bank dan jurnal aslinya."
        )
    entries = (
        [("BANK", row.amount, ZERO), ("AR", ZERO, row.amount)]
        if incoming
        else [("AP", row.amount, ZERO), ("BANK", ZERO, row.amount)]
    )
    proof.update(
        item_id=row.item_id,
        item_source_hash=_hash(original),
        bank=bank,
        bank_posting={
            "id": posting.pk,
            "kind": kind,
            "signed_amount": str(sign),
            "date": row.date.isoformat(),
            "journal_id": posting.journal_id,
            "reference": posting.reference,
            "evidence_sha256": _hash(posting.evidence),
        },
        journal=_journal(
            org,
            key=f"bank-event:{row.reference}",
            date=row.date,
            entries=entries,
            journal_id=row.journal_id,
        ),
    )
    return _seal(row, proof, capture)


def _direction(value):
    if value not in OpeningBalanceCorrection.Direction.values:
        raise ValidationError("Pilih tambah atau kurangi pokok saldo awal.")
    return value


def _correction_entries(kind, direction, amount):
    increase = direction == "increase"
    if kind == "receivable":
        return (
            [("AR", amount, ZERO), ("EQUITY", ZERO, amount)]
            if increase
            else [("EQUITY", amount, ZERO), ("AR", ZERO, amount)]
        )
    return (
        [("EQUITY", amount, ZERO), ("AP", ZERO, amount)]
        if increase
        else [("AP", amount, ZERO), ("EQUITY", ZERO, amount)]
    )


@_proof_errors
def _correction_proof(org, row, *, capture=False):
    proof = _common(org, row)
    original = _item_proof(org, row.item)
    if row.date < row.item.date:
        raise ValidationError(
            "Tanggal koreksi tidak boleh mendahului tanggal saldo awal."
        )
    _scope(row.journal, org)
    proof.update(
        item_id=row.item_id,
        item_source_hash=_hash(original),
        direction=row.direction,
        journal=_journal(
            org,
            key=f"opening:{row.reference}",
            date=row.date,
            entries=_correction_entries(row.item.kind, row.direction, row.amount),
            journal_id=row.journal_id,
        ),
    )
    return _seal(row, proof, capture)


@_proof_errors
def _cancellation_proof(org, row, *, capture=False):
    """Validate only sealed source links and journals, never bank capacity.

    Shared bank capacity calls this validator. Re-entering inventory or the
    shared bank-state validator here would make recovery proofs cyclic.
    """
    proof = _common(org, row)
    original = _settlement_proof(org, row.settlement)
    if (
        row.item_id != row.settlement.item_id
        or row.date != row.settlement.date
        or row.amount != row.settlement.amount
    ):
        raise ValidationError(
            "Pembatalan harus mengikuti seluruh jumlah dan tanggal bank pencocokan asli."
        )
    entries = (
        [("AR", row.amount, ZERO), ("BANK", ZERO, row.amount)]
        if row.item.kind == "receivable"
        else [("BANK", row.amount, ZERO), ("AP", ZERO, row.amount)]
    )
    _scope(row.journal, org)
    proof.update(
        item_id=row.item_id,
        settlement_id=row.settlement_id,
        settlement_source_hash=_hash(original),
        bank_posting_id=row.settlement.bank_posting_id,
        transaction_id=row.settlement.bank_posting.transaction_id,
        journal=_journal(
            org,
            key=f"opening:{row.reference}",
            date=row.date,
            entries=entries,
            journal_id=row.journal_id,
        ),
    )
    return _seal(row, proof, capture)


def _proof_for(action):
    return {
        "record": _item_proof,
        "settle": _settlement_proof,
        "correct": _correction_proof,
        "cancel": _cancellation_proof,
        "refund": _refund_proof,
    }[action]


@_proof_errors
def _refund_proof(org, row, *, capture=False):
    """An independent opposite-bank receipt, never a bank-capacity calculation."""
    proof = _common(org, row)
    original = _settlement_proof(org, row.settlement)
    posting = _scope(row.bank_posting, org)
    bank = _bank_identity(org, posting.transaction)
    incoming_original = row.item.kind == "receivable"
    signed = -row.amount if incoming_original else row.amount
    kind = "opening_receipt_refund" if incoming_original else "opening_payment_refund"
    if (
        row.item_id != row.settlement.item_id
        or row.date < row.settlement.date
        or posting.transaction_id == row.settlement.bank_posting.transaction_id
        or posting.transaction.amount * signed <= ZERO
        or posting.kind != kind
        or posting.signed_amount != signed
        or posting.date != row.date
        or posting.transaction.date != row.date
        or posting.journal_id != row.journal_id
        or posting.reference != row.reference
        or posting.evidence != row.evidence
    ):
        raise ValidationError(
            "Pengembalian tidak sesuai pembayaran asal dan mutasi bank baru dengan arah berlawanan."
        )
    entries = (
        [("AR", row.amount, ZERO), ("BANK", ZERO, row.amount)]
        if incoming_original
        else [("BANK", row.amount, ZERO), ("AP", ZERO, row.amount)]
    )
    proof.update(
        item_id=row.item_id,
        settlement_id=row.settlement_id,
        settlement_source_hash=_hash(original),
        original_transaction_id=row.settlement.bank_posting.transaction_id,
        transaction_id=posting.transaction_id,
        bank_posting_id=posting.pk,
        bank=bank,
        bank_posting={
            "id": posting.pk,
            "kind": kind,
            "signed_amount": str(signed),
            "date": row.date.isoformat(),
            "journal_id": posting.journal_id,
            "reference": posting.reference,
            "evidence_sha256": _hash(posting.evidence),
        },
        journal=_journal(
            org,
            key=f"bank-event:{row.reference}",
            date=row.date,
            entries=entries,
            journal_id=row.journal_id,
        ),
    )
    return _seal(row, proof, capture)


@transaction.atomic
def opening_balance_source_snapshot(*, organization, item):
    org = ledger._lock_org(organization)
    row = _select(OpeningBalanceItem, item, org)
    # Current principal, duplicate identities and shared cash claims must be
    # valid, but do not become mutable fields inside this original receipt.
    _inventory(org)
    proof = _item_proof(org, row)
    return {**proof, "item_id": row.pk, "fingerprint": _hash(proof)}


@transaction.atomic
def opening_settlement_source_snapshot(*, organization, settlement):
    org = ledger._lock_org(organization)
    row = _select(OpeningBalanceSettlement, settlement, org)
    _inventory(org)
    proof = _settlement_proof(org, row)
    return {**proof, "settlement_id": row.pk, "fingerprint": _hash(proof)}


@transaction.atomic
def opening_correction_source_snapshot(*, organization, correction):
    """Stable receipt for the correction itself, never a replacement origin."""
    org = ledger._lock_org(organization)
    row = _select(OpeningBalanceCorrection, correction, org)
    _inventory(org)
    proof = _correction_proof(org, row)
    return {**proof, "correction_id": row.pk, "fingerprint": _hash(proof)}


@transaction.atomic
def opening_cancellation_source_snapshot(*, organization, cancellation):
    """Receipt for the cancellation; the original cash receipt stays intact."""
    org = ledger._lock_org(organization)
    row = _select(OpeningBalanceCancellation, cancellation, org)
    _inventory(org)
    proof = _cancellation_proof(org, row)
    return {**proof, "cancellation_id": row.pk, "fingerprint": _hash(proof)}


@transaction.atomic
def opening_refund_source_snapshot(*, organization, refund):
    """Stable receipt for actual refund cash, separate from its original receipt."""
    org = ledger._lock_org(organization)
    row = _select(OpeningBalanceRefund, refund, org)
    _inventory(org)
    proof = _refund_proof(org, row)
    return {**proof, "refund_id": row.pk, "fingerprint": _hash(proof)}


def _opening_event_rows(model, org, related_organization_paths, *, durable_ids=()):
    """Find scoped events through independent indexed paths, then hydrate once.

    Combining every relationship in one OR query creates a large join tree and
    requires DISTINCT across all selected columns. Keeping the identity paths
    separate avoids that join multiplication while still surfacing corrupt
    cross-organization links for validation.
    """
    direct_scope = Q(organization=org) | Q(source_snapshot__organization_id=org.pk)
    if durable_ids:
        direct_scope |= Q(pk__in=durable_ids)
    identity_queries = [model.objects.filter(direct_scope).order_by().values("pk")]
    identity_queries.extend(
        model.objects.filter(**{path: org}).order_by().values("pk")
        for path in related_organization_paths
    )
    scoped_ids = identity_queries[0].union(*identity_queries[1:], all=True)
    return model.objects.filter(pk__in=Subquery(scoped_ids))


def _refund_rows(org):
    audits = AuditEvent.objects.filter(
        organization=org,
        action="finance.opening_balance.refund",
        object_type="OpeningBalanceRefund",
    ).values_list("object_id", flat=True)
    events = DomainEvent.objects.filter(
        organization=org,
        topic="opening_balance.refund",
        aggregate_type="finance.openingbalancerefund",
    ).values_list("aggregate_id", flat=True)
    original_ids = [
        int(value)
        for value in [*audits, *events]
        if isinstance(value, str) and value.isdecimal() and int(value) > 0
    ]
    return _opening_event_rows(
        OpeningBalanceRefund,
        org,
        (
            "item__organization",
            "settlement__organization",
            "settlement__bank_posting__organization",
            "settlement__bank_posting__transaction__organization",
            "bank_posting__organization",
            "bank_posting__transaction__organization",
            "journal__organization",
            "journal__lines__organization",
        ),
        durable_ids=original_ids,
    )


@_proof_errors
def opening_cash_refund_facts(*, organization, transaction=None):
    """Validate typed refund claims without releasing any original cash claim.

    No inventory, bank state or capacity property is called from this proof.
    The new signed posting is already counted by ordinary bank consumers.
    """
    org = _org(organization)
    bank = (
        _select(BankTransaction, transaction, org) if transaction is not None else None
    )
    rows = list(
        _refund_rows(org)
        .select_related(
            "item__party",
            "item__journal",
            "journal",
            "settlement__item__party",
            "settlement__item__journal",
            "settlement__journal",
            "settlement__bank_posting__transaction__account",
            "bank_posting__transaction__account",
        )
        .order_by("pk")
    )
    journals = set(
        Journal.objects.filter(
            Q(organization=org) | Q(lines__organization=org),
            source_key__startswith="bank-event:opening-refund:",
        )
        .distinct()
        .values_list("pk", flat=True)
    )
    postings = set(
        BankPosting.objects.filter(
            Q(organization=org)
            | Q(transaction__organization=org)
            | Q(journal__organization=org),
            kind__in=["opening_receipt_refund", "opening_payment_refund"],
        ).values_list("pk", flat=True)
    )
    if journals != {row.journal_id for row in rows} or postings != {
        row.bank_posting_id for row in rows
    }:
        raise ValidationError(
            "Ada jurnal atau klaim bank pengembalian tanpa keputusan asli yang lengkap."
        )
    # Durable IDs are evidence of a missing decision, never proof of money.
    anchors = [
        *AuditEvent.objects.filter(
            organization=org,
            action="finance.opening_balance.refund",
            object_type="OpeningBalanceRefund",
        ).values_list("object_id", flat=True),
        *DomainEvent.objects.filter(
            organization=org,
            topic="opening_balance.refund",
            aggregate_type="finance.openingbalancerefund",
        ).values_list("aggregate_id", flat=True),
    ]
    row_ids = {row.pk for row in rows}
    if any(
        not isinstance(value, str) or not value.isdecimal() or int(value) not in row_ids
        for value in anchors
    ):
        raise ValidationError(
            "Keputusan pengembalian yang tercatat pada riwayat asli tidak tersedia atau berubah."
        )
    cancelled = set(_cancellation_rows(org).values_list("settlement_id", flat=True))
    totals, facts = {}, []
    for row in rows:
        proof = _refund_proof(org, row)
        if row.settlement_id in cancelled:
            raise ValidationError(
                "Pembayaran yang dibatalkan tidak dapat sekaligus menjadi sumber pengembalian uang."
            )
        totals[row.settlement_id] = totals.get(row.settlement_id, ZERO) + row.amount
        if totals[row.settlement_id] > row.settlement.amount:
            raise ValidationError("Pengembalian melebihi seluruh uang pembayaran asal.")
        if bank is None or bank.pk in {
            proof["transaction_id"],
            proof["original_transaction_id"],
        }:
            facts.append(
                {
                    "id": row.pk,
                    "settlement_id": row.settlement_id,
                    "transaction_id": proof["transaction_id"],
                    "original_transaction_id": proof["original_transaction_id"],
                    "bank_posting_id": row.bank_posting_id,
                    "amount": str(row.amount),
                    "proof": proof,
                }
            )
    graph = {
        "version": VERSION,
        "organization_id": org.pk,
        "transaction_id": bank.pk if bank else None,
        "refunds": facts,
    }
    return {**graph, "fingerprint": _hash(graph)}


def _refund_raw_graph(org, identity=None, *, bank_id=None):
    """Opaque current, sealed and audited anchors, including orphan refund cash."""
    queue, seen, sources = [], set(), []

    def enqueue(model, pk):
        if type(pk) is int and pk > 0 and (model, pk) not in seen:
            queue.append((model, pk))

    enqueue(BankTransaction, bank_id)
    if identity is not None:
        enqueue(OpeningBalanceRefund, identity)
    else:
        for pk in _refund_rows(org).values_list("pk", flat=True):
            enqueue(OpeningBalanceRefund, pk)
    audits = AuditEvent.objects.filter(
        organization=org,
        action="finance.opening_balance.refund",
        object_type="OpeningBalanceRefund",
    ).order_by("pk")
    for event in audits:
        detail = event.detail if isinstance(event.detail, dict) else {}
        original_id = (
            int(event.object_id)
            if isinstance(event.object_id, str) and event.object_id.isdecimal()
            else None
        )
        if identity is not None and identity != original_id:
            continue
        sources.append(
            {
                "model": AuditEvent._meta.label_lower,
                "id": event.pk,
                "hash": _hash(
                    {
                        "organization_id": event.organization_id,
                        "object_id": event.object_id,
                        "detail": detail,
                    }
                ),
            }
        )
        enqueue(OpeningBalanceRefund, original_id)
        for field, model in (
            ("item_id", OpeningBalanceItem),
            ("settlement_id", OpeningBalanceSettlement),
            ("journal_id", Journal),
            ("bank_posting_id", BankPosting),
            ("transaction_id", BankTransaction),
            ("original_transaction_id", BankTransaction),
        ):
            enqueue(model, detail.get(field))
    events = DomainEvent.objects.filter(
        organization=org,
        topic="opening_balance.refund",
        aggregate_type="finance.openingbalancerefund",
    ).order_by("pk")
    for event in events:
        detail = event.payload if isinstance(event.payload, dict) else {}
        original_id = (
            int(event.aggregate_id)
            if isinstance(event.aggregate_id, str) and event.aggregate_id.isdecimal()
            else None
        )
        if identity is not None and identity != original_id:
            continue
        sources.append(
            {
                "model": DomainEvent._meta.label_lower,
                "id": event.pk,
                "hash": _hash(
                    {
                        "organization_id": event.organization_id,
                        "aggregate_id": event.aggregate_id,
                        "payload": detail,
                    }
                ),
            }
        )
        enqueue(OpeningBalanceRefund, original_id)
        for field, model in (
            ("item_id", OpeningBalanceItem),
            ("settlement_id", OpeningBalanceSettlement),
            ("journal_id", Journal),
            ("bank_posting_id", BankPosting),
            ("transaction_id", BankTransaction),
            ("original_transaction_id", BankTransaction),
        ):
            enqueue(model, detail.get(field))
    if identity is None:
        for pk in (
            Journal.objects.filter(
                Q(organization=org) | Q(lines__organization=org),
                source_key__startswith="bank-event:opening-refund:",
            )
            .distinct()
            .values_list("pk", flat=True)
        ):
            enqueue(Journal, pk)
        for pk in BankPosting.objects.filter(
            Q(organization=org)
            | Q(transaction__organization=org)
            | Q(journal__organization=org),
            kind__in=["opening_receipt_refund", "opening_payment_refund"],
        ).values_list("pk", flat=True):
            enqueue(BankPosting, pk)
    links = {
        OpeningBalanceRefund: (
            ("item_id", OpeningBalanceItem),
            ("settlement_id", OpeningBalanceSettlement),
            ("journal_id", Journal),
            ("bank_posting_id", BankPosting),
        ),
        OpeningBalanceSettlement: (
            ("item_id", OpeningBalanceItem),
            ("journal_id", Journal),
            ("bank_posting_id", BankPosting),
        ),
        OpeningBalanceItem: (("party_id", Party), ("journal_id", Journal)),
        BankPosting: (("transaction_id", BankTransaction), ("journal_id", Journal)),
        BankTransaction: (("account_id", BankAccount),),
        Allocation: (("invoice_id", Invoice),),
        BillPayment: (("bill_id", Bill),),
        Invoice: (("party_id", Party),),
        Bill: (("supplier_id", Party),),
    }
    while queue:
        model, pk = queue.pop(0)
        if (model, pk) in seen:
            continue
        seen.add((model, pk))
        row = model.objects.filter(pk=pk).values().first()
        sources.append(
            {
                "model": model._meta.label_lower,
                "id": pk,
                "hash": _hash(row) if row is not None else None,
                "missing": row is None,
            }
        )
        if row is None:
            continue
        for field, target in links.get(model, ()):
            enqueue(target, row.get(field))
        if model in {
            OpeningBalanceRefund,
            OpeningBalanceSettlement,
            OpeningBalanceItem,
        }:
            memberships = list(
                Membership.all_objects.filter(user_id=row["actor_id"])
                .order_by("pk")
                .values()
            )
            sources.append(
                {
                    "actor_id": row["actor_id"],
                    "membership_history_hash": _hash(memberships),
                }
            )
        if model is Journal:
            lines = list(
                JournalLine.objects.filter(journal_id=pk).order_by("pk").values()
            )
            sources.append(
                {"journal_id": pk, "lines_hash": _hash(lines), "line_count": len(lines)}
            )
        if model is BankTransaction:
            for linked in (Allocation, BillPayment, BankPosting):
                rows = list(
                    linked.objects.filter(transaction_id=pk).order_by("pk").values()
                )
                sources.append(
                    {
                        "bank_id": pk,
                        "model": linked._meta.label_lower,
                        "claims_hash": _hash(rows),
                    }
                )
                for claim in rows:
                    enqueue(linked, claim["id"])
                    if linked in {Allocation, BillPayment}:
                        prefix = (
                            "allocation" if linked is Allocation else "bill-payment"
                        )
                        for journal_id in Journal.objects.filter(
                            organization_id=claim["organization_id"],
                            source_key=f"{prefix}:{claim['id']}",
                        ).values_list("pk", flat=True):
                            enqueue(Journal, journal_id)
        sealed = (
            row.get("source_snapshot")
            if isinstance(row.get("source_snapshot"), dict)
            else {}
        )
        for field, target in (
            ("item_id", OpeningBalanceItem),
            ("settlement_id", OpeningBalanceSettlement),
            ("party_id", Party),
            ("bank_posting_id", BankPosting),
            ("transaction_id", BankTransaction),
            ("original_transaction_id", BankTransaction),
        ):
            enqueue(target, sealed.get(field))
        for field, target in (
            ("journal", Journal),
            ("bank_posting", BankPosting),
            ("bank", BankTransaction),
        ):
            original = sealed.get(field) if isinstance(sealed.get(field), dict) else {}
            enqueue(target, original.get("id"))
            if field == "bank":
                enqueue(BankAccount, original.get("account_id"))
    graph = {
        "version": VERSION,
        "organization_id": org.pk,
        "refund_id": identity,
        "sources": sources,
        **({"selected_bank_id": bank_id} if bank_id is not None else {}),
    }
    return {**graph, "fingerprint": _hash(graph)}


def opening_refund_source_graph(*, organization, refund):
    org = _org(organization)
    pk = getattr(refund, "pk", refund)
    if (
        isinstance(pk, bool)
        or not isinstance(pk, (int, str))
        or (isinstance(pk, str) and not pk.isdecimal())
        or int(pk) <= 0
    ):
        raise ValidationError("Pilih identitas pengembalian yang valid.")
    return _refund_raw_graph(org, int(pk))


def _unknown_refund_sources(org):
    try:
        opening_cash_refund_facts(organization=org)
    except ValidationError:
        return {"refund_repairs": _refund_raw_graph(org)["fingerprint"]}
    return {}


def _cancellation_rows(org):
    return _opening_event_rows(
        OpeningBalanceCancellation,
        org,
        (
            "item__organization",
            "settlement__organization",
            "settlement__bank_posting__organization",
            "settlement__bank_posting__transaction__organization",
            "journal__organization",
            "journal__lines__organization",
        ),
    )


@_proof_errors
def opening_cash_cancellation_facts(*, organization, transaction=None):
    """Validated signed claim releases shared by every cash-capacity consumer.

    This boundary deliberately does not call inventory, bank state or any
    property derived from bank capacity. A surviving typed cancellation
    journal without its decision holds capacity; it never frees cash silently.
    """
    org = _org(organization)
    opening_cash_refund_facts(organization=org)
    bank = (
        _select(BankTransaction, transaction, org) if transaction is not None else None
    )
    rows = list(
        _cancellation_rows(org)
        .select_related(
            "item__party",
            "item__journal",
            "journal",
            "settlement__item__party",
            "settlement__item__journal",
            "settlement__journal",
            "settlement__bank_posting__transaction__account",
        )
        .order_by("pk")
    )
    journals = set(
        Journal.objects.filter(
            Q(organization=org) | Q(lines__organization=org),
            source_key__startswith="opening:opening-cancel:",
        )
        .distinct()
        .values_list("pk", flat=True)
    )
    if journals != {row.journal_id for row in rows}:
        raise ValidationError(
            "Ada jurnal pembatalan pencocokan tanpa keputusan asli yang lengkap."
        )
    releases, seen, released = [], set(), ZERO
    for row in rows:
        proof = _cancellation_proof(org, row)
        if row.settlement_id in seen:
            raise ValidationError("Pencocokan asli dibatalkan lebih dari sekali.")
        seen.add(row.settlement_id)
        signed = row.amount if row.item.kind == "receivable" else -row.amount
        entry = {
            "id": row.pk,
            "settlement_id": row.settlement_id,
            "transaction_id": row.settlement.bank_posting.transaction_id,
            "bank_posting_id": row.settlement.bank_posting_id,
            "signed_amount": str(signed),
            "proof": proof,
        }
        if bank is None or bank.pk == entry["transaction_id"]:
            releases.append(entry)
            released += signed
    graph = {
        "version": VERSION,
        "organization_id": org.pk,
        "transaction_id": bank.pk if bank else None,
        "releases": releases,
    }
    return {**graph, "released": released, "fingerprint": _hash(graph)}


def _opaque_correction(org, identity):
    """Hash original and current anchors without displaying foreign contents."""
    sources, item_ids, journal_ids, party_ids = [], set(), set(), set()

    def add(model, pk):
        row = model.objects.filter(pk=pk).values().first()
        sources.append(
            {
                "model": model._meta.label_lower,
                "id": pk,
                "hash": _hash(row) if row is not None else None,
                "missing": row is None,
            }
        )
        return row

    def anchors(row):
        if not row:
            return
        item_ids.add(row["item_id"])
        journal_ids.add(row["journal_id"])
        sealed = (
            row["source_snapshot"] if isinstance(row["source_snapshot"], dict) else {}
        )
        if type(sealed.get("item_id")) is int and sealed["item_id"] > 0:
            item_ids.add(sealed["item_id"])
        journal = (
            sealed.get("journal") if isinstance(sealed.get("journal"), dict) else {}
        )
        if type(journal.get("id")) is int and journal["id"] > 0:
            journal_ids.add(journal["id"])

    row = add(OpeningBalanceCorrection, identity)
    anchors(row)
    sibling_rows = list(
        OpeningBalanceCorrection.objects.filter(
            Q(item_id__in=item_ids) | Q(source_snapshot__item_id__in=list(item_ids))
        )
        .order_by("pk")
        .values()
    )
    sources.append({"kind": "correction_inventory", "hash": _hash(sibling_rows)})
    for sibling in sibling_rows:
        anchors(sibling)
    for pk in sorted(item_ids):
        item = add(OpeningBalanceItem, pk)
        if item:
            party_ids.add(item["party_id"])
            journal_ids.add(item["journal_id"])
            sealed = (
                item["source_snapshot"]
                if isinstance(item["source_snapshot"], dict)
                else {}
            )
            if type(sealed.get("party_id")) is int and sealed["party_id"] > 0:
                party_ids.add(sealed["party_id"])
            journal = (
                sealed.get("journal") if isinstance(sealed.get("journal"), dict) else {}
            )
            if type(journal.get("id")) is int and journal["id"] > 0:
                journal_ids.add(journal["id"])
    for pk in sorted(party_ids):
        add(Party, pk)
    for pk in sorted(journal_ids):
        add(Journal, pk)
        lines = list(JournalLine.objects.filter(journal_id=pk).order_by("pk").values())
        sources.append(
            {"journal_id": pk, "line_count": len(lines), "lines_hash": _hash(lines)}
        )
    graph = {
        "version": VERSION,
        "organization_id": org.pk,
        "correction_id": identity,
        "sources": sources,
    }
    return {**graph, "fingerprint": _hash(graph)}, item_ids


def _correction_rows(org):
    return OpeningBalanceCorrection.objects.filter(
        Q(organization=org)
        | Q(item__organization=org)
        | Q(item__source_snapshot__organization_id=org.pk)
        | Q(journal__organization=org)
        | Q(journal__lines__organization=org)
        | Q(source_snapshot__organization_id=org.pk)
    ).distinct()


def _invalid_correction_graphs(org):
    try:
        _inventory(org)
        return []
    except (
        ValidationError,
        ObjectDoesNotExist,
        ValueError,
        TypeError,
        ArithmeticError,
        KeyError,
    ):
        repairs = [
            {"id": pk, "hash": _opaque_correction(org, pk)[0]["fingerprint"]}
            for pk in _correction_rows(org).order_by("pk").values_list("pk", flat=True)
        ]
        # A missing decision must not hide its surviving typed journal. The
        # line scope also retains the source if its journal was moved away.
        journals = (
            Journal.objects.filter(
                Q(organization=org) | Q(lines__organization=org),
                source_key__startswith="opening:opening-correct:",
            )
            .distinct()
            .order_by("pk")
        )
        for row in journals.values():
            lines = list(
                JournalLine.objects.filter(journal_id=row["id"]).order_by("pk").values()
            )
            repairs.append(
                {
                    "kind": "correction_journal",
                    "journal_id": row["id"],
                    "hash": _hash(row),
                    "lines_hash": _hash(lines),
                    "line_count": len(lines),
                }
            )
        return repairs


def _unknown_correction_sources(org):
    repairs = _invalid_correction_graphs(org)
    return {"correction_repairs": repairs} if repairs else {}


def _cancellation_raw_graph(org, identity=None):
    """Opaque current/sealed anchors, including an orphan typed journal."""
    sources, visited, queue = [], set(), []
    rows = list(_cancellation_rows(org).order_by("pk").values())
    if identity is not None and all(row["id"] != identity for row in rows):
        row = OpeningBalanceCancellation.objects.filter(pk=identity).values().first()
        if row:
            rows.append(row)
        else:
            sources.append(
                {
                    "model": OpeningBalanceCancellation._meta.label_lower,
                    "id": identity,
                    "missing": True,
                }
            )
    sources.append({"kind": "cancellation_inventory", "hash": _hash(rows)})

    def enqueue(model, pk):
        if type(pk) is int and pk > 0:
            queue.append((model, pk))

    for row in rows:
        enqueue(OpeningBalanceCancellation, row["id"])
    typed_journals = list(
        Journal.objects.filter(
            Q(organization=org) | Q(lines__organization=org),
            source_key__startswith="opening:opening-cancel:",
        )
        .distinct()
        .order_by("pk")
        .values()
    )
    sources.append(
        {"kind": "cancellation_journal_inventory", "hash": _hash(typed_journals)}
    )
    for row in typed_journals:
        enqueue(Journal, row["id"])
    # A lost typed row must not lose the original source links recorded at
    # the same atomic write. These are repair anchors only: a dangling audit
    # ID does not establish a cancellation or release any bank capacity.
    cancellation_ids = {row["id"] for row in rows}
    if identity is not None:
        cancellation_ids.add(identity)
    journal_ids = {row["id"] for row in typed_journals}
    request_keys = {
        row["source_key"].removeprefix("opening:opening-cancel:")
        for row in typed_journals
    }
    audit_rows = list(
        AuditEvent.objects.filter(
            action="finance.opening_balance.cancel",
            object_type="OpeningBalanceCancellation",
        )
        .filter(
            Q(organization=org)
            | Q(object_id__in=[str(pk) for pk in cancellation_ids])
            | Q(detail__journal_id__in=list(journal_ids))
            | Q(detail__request_key__in=list(request_keys))
        )
        .order_by("pk")
        .values()
    )
    event_rows = list(
        DomainEvent.objects.filter(
            topic="opening_balance.cancel",
            aggregate_type="finance.openingbalancecancellation",
        )
        .filter(
            Q(organization=org)
            | Q(aggregate_id__in=[str(pk) for pk in cancellation_ids])
            | Q(payload__journal_id__in=list(journal_ids))
            | Q(payload__request_key__in=list(request_keys))
        )
        .order_by("pk")
        .values()
    )
    sources.append({"kind": "cancellation_audit_inventory", "hash": _hash(audit_rows)})
    sources.append({"kind": "cancellation_event_inventory", "hash": _hash(event_rows)})
    for row in audit_rows:
        enqueue(AuditEvent, row["id"])
    for row in event_rows:
        enqueue(DomainEvent, row["id"])
    while queue:
        model, pk = queue.pop(0)
        if (model, pk) in visited:
            continue
        visited.add((model, pk))
        row = model.objects.filter(pk=pk).values().first()
        sources.append(
            {
                "model": model._meta.label_lower,
                "id": pk,
                "hash": _hash(row) if row else None,
                "missing": row is None,
            }
        )
        if not row:
            continue
        if model in {AuditEvent, DomainEvent}:
            detail = row["detail"] if model == AuditEvent else row["payload"]
            detail = detail if isinstance(detail, dict) else {}
            for field, linked in (
                ("item_id", OpeningBalanceItem),
                ("settlement_id", OpeningBalanceSettlement),
                ("bank_posting_id", BankPosting),
                ("journal_id", Journal),
                ("transaction_id", BankTransaction),
            ):
                enqueue(linked, detail.get(field))
            original_id = (
                row["object_id"] if model == AuditEvent else row["aggregate_id"]
            )
            if isinstance(original_id, str) and original_id.isdecimal():
                enqueue(OpeningBalanceCancellation, int(original_id))
            key = detail.get("request_key")
            if isinstance(key, str):
                for journal_id in Journal.objects.filter(
                    source_key=f"opening:opening-cancel:{key}"
                ).values_list("pk", flat=True):
                    enqueue(Journal, journal_id)
        if model == Journal:
            lines = list(
                JournalLine.objects.filter(journal_id=pk).order_by("pk").values()
            )
            sources.append(
                {"journal_id": pk, "lines_hash": _hash(lines), "line_count": len(lines)}
            )
        if model in {
            OpeningBalanceCancellation,
            OpeningBalanceSettlement,
            OpeningBalanceItem,
        }:
            enqueue(Journal, row["journal_id"])
            enqueue(OpeningBalanceItem, row.get("item_id"))
            enqueue(OpeningBalanceSettlement, row.get("settlement_id"))
            enqueue(BankPosting, row.get("bank_posting_id"))
            enqueue(Party, row.get("party_id"))
            memberships = list(
                Membership.all_objects.filter(user_id=row["actor_id"])
                .order_by("pk")
                .values()
            )
            sources.append(
                {
                    "actor_id": row["actor_id"],
                    "membership_history_hash": _hash(memberships),
                }
            )
            sealed = (
                row["source_snapshot"]
                if isinstance(row["source_snapshot"], dict)
                else {}
            )
            enqueue(OpeningBalanceItem, sealed.get("item_id"))
            enqueue(OpeningBalanceSettlement, sealed.get("settlement_id"))
            enqueue(BankPosting, sealed.get("bank_posting_id"))
            enqueue(BankTransaction, sealed.get("transaction_id"))
            enqueue(Party, sealed.get("party_id"))
            for key, linked in (
                ("journal", Journal),
                ("bank_posting", BankPosting),
                ("bank", BankTransaction),
            ):
                source = sealed.get(key) if isinstance(sealed.get(key), dict) else {}
                enqueue(linked, source.get("id"))
                if key == "bank":
                    enqueue(BankAccount, source.get("account_id"))
        if model == BankPosting:
            enqueue(Journal, row["journal_id"])
            enqueue(BankTransaction, row["transaction_id"])
        if model == BankTransaction:
            enqueue(BankAccount, row["account_id"])
            for linked in (Allocation, BillPayment, BankPosting):
                claims = list(
                    linked.objects.filter(transaction_id=pk).order_by("pk").values()
                )
                sources.append(
                    {
                        "bank_id": pk,
                        "model": linked._meta.label_lower,
                        "claims_hash": _hash(claims),
                    }
                )
                for claim in claims:
                    enqueue(linked, claim["id"])
        if model == Allocation:
            enqueue(Invoice, row["invoice_id"])
            for journal_id in Journal.objects.filter(
                source_key=f"allocation:{pk}"
            ).values_list("pk", flat=True):
                enqueue(Journal, journal_id)
        if model == BillPayment:
            enqueue(Bill, row["bill_id"])
            for journal_id in Journal.objects.filter(
                source_key=f"bill-payment:{pk}"
            ).values_list("pk", flat=True):
                enqueue(Journal, journal_id)
        if model in {Invoice, Bill}:
            enqueue(Party, row.get("party_id", row.get("supplier_id")))
    graph = {
        "version": VERSION,
        "organization_id": org.pk,
        "cancellation_id": identity,
        "sources": sources,
    }
    return {**graph, "fingerprint": _hash(graph)}


def _unknown_recovery_sources(org):
    try:
        opening_cash_cancellation_facts(organization=org)
        return {}
    except (
        ValidationError,
        ObjectDoesNotExist,
        ValueError,
        TypeError,
        ArithmeticError,
        KeyError,
    ):
        return {"cancellation_repairs": _cancellation_raw_graph(org)["fingerprint"]}


def opening_cancellation_source_graph(*, organization, cancellation):
    """Repair identity without exposing another company's source contents."""
    org = _org(organization)
    pk = getattr(cancellation, "pk", cancellation)
    if (
        isinstance(pk, bool)
        or not isinstance(pk, (int, str))
        or (isinstance(pk, str) and not pk.isdecimal())
        or int(pk) <= 0
    ):
        raise ValidationError("Pilih identitas pembatalan pencocokan yang valid.")
    return _cancellation_raw_graph(org, int(pk))


def opening_correction_source_graph(*, organization, correction):
    """Opaque repair identity for this correction and its original sources."""
    org = _org(organization)
    pk = getattr(correction, "pk", correction)
    if (
        isinstance(pk, bool)
        or not isinstance(pk, (int, str))
        or (isinstance(pk, str) and not pk.isdecimal())
        or int(pk) <= 0
    ):
        raise ValidationError("Pilih identitas koreksi saldo awal yang valid.")
    graph, item_ids = _opaque_correction(org, int(pk))
    graph.pop("fingerprint")
    graph["original_sources"] = [
        {
            "item_id": identity,
            "hash": opening_balance_source_graph(organization=org, item=identity)[
                "fingerprint"
            ],
        }
        for identity in sorted(item_ids)
    ]
    graph.update(_unknown_correction_sources(org))
    graph.update(_unknown_recovery_sources(org))
    return {**graph, "fingerprint": _hash(graph)}


def opening_settlement_source_graph(*, organization, settlement):
    """Opaque repair identity, including damaged/foreign links, not a receipt.

    Valid financial facts come only from the validated snapshot interface.
    These hashes let fiscal review retain a changed-source token even when a
    link or journal is broken, without returning another company's contents.
    """
    org = _org(organization)
    pk = getattr(settlement, "pk", settlement)
    if (
        isinstance(pk, bool)
        or not isinstance(pk, (int, str))
        or (isinstance(pk, str) and not pk.isdecimal())
        or int(pk) <= 0
    ):
        raise ValidationError("Pilih identitas pembayaran yang valid.")
    found, entries = {}, {}
    visited_items, visited_banks, visited_postings, visited_decisions = (
        set(),
        set(),
        set(),
        set(),
    )

    def sealed_id(value):
        return value if type(value) is int and value > 0 else None

    def add(model, identity):
        if identity is None:
            return None
        marker = f"{model._meta.label_lower}:{identity}"
        if marker not in entries:
            values = model.objects.filter(pk=identity).values().first()
            found[marker] = values
            entries[marker] = {
                "id": identity,
                "model": model._meta.label_lower,
                "hash": _hash(values) if values is not None else None,
                "missing": values is None,
            }
        return found[marker]

    def journal(identity):
        add(Journal, identity)
        if identity is not None:
            rows = list(
                JournalLine.objects.filter(journal_id=identity).order_by("pk").values()
            )
            entries[f"journal-lines:{identity}"] = {
                "journal_id": identity,
                "hash": _hash(rows),
                "count": len(rows),
            }

    def item(identity):
        if identity is None or identity in visited_items:
            return
        visited_items.add(identity)
        row = add(OpeningBalanceItem, identity)
        if row:
            add(Party, row["party_id"])
            journal(row["journal_id"])
            sealed = (
                row["source_snapshot"]
                if isinstance(row["source_snapshot"], dict)
                else {}
            )
            add(Party, sealed_id(sealed.get("party_id")))
            original_journal = (
                sealed.get("journal") if isinstance(sealed.get("journal"), dict) else {}
            )
            journal(sealed_id(original_journal.get("id")))

    def bank(identity):
        if identity is None or identity in visited_banks:
            return
        visited_banks.add(identity)
        row = add(BankTransaction, identity)
        if not row:
            return
        add(BankAccount, row["account_id"])
        for allocation in Allocation.objects.filter(transaction_id=identity).order_by(
            "pk"
        ):
            add(Allocation, allocation.pk)
            invoice = add(Invoice, allocation.invoice_id)
            if invoice:
                add(Party, invoice["party_id"])
            expected = (
                Journal.objects.filter(
                    organization_id=allocation.organization_id,
                    source_key=f"allocation:{allocation.pk}",
                )
                .values_list("pk", flat=True)
                .first()
            )
            journal(expected)
            entries[f"allocation-journal:{allocation.pk}"] = {
                "allocation_id": allocation.pk,
                "journal_id": expected,
            }
        for payment in BillPayment.objects.filter(transaction_id=identity).order_by(
            "pk"
        ):
            add(BillPayment, payment.pk)
            bill = add(Bill, payment.bill_id)
            if bill:
                add(Party, bill["supplier_id"])
            expected = (
                Journal.objects.filter(
                    organization_id=payment.organization_id,
                    source_key=f"bill-payment:{payment.pk}",
                )
                .values_list("pk", flat=True)
                .first()
            )
            journal(expected)
            entries[f"payment-journal:{payment.pk}"] = {
                "payment_id": payment.pk,
                "journal_id": expected,
            }
        for claim in BankPosting.objects.filter(transaction_id=identity).order_by("pk"):
            posting(claim.pk)
            for linked_id in (
                OpeningBalanceSettlement.objects.filter(bank_posting_id=claim.pk)
                .order_by("pk")
                .values_list("pk", flat=True)
            ):
                decision(linked_id)
        recoveries = OpeningBalanceCancellation.objects.filter(
            Q(settlement__bank_posting__transaction_id=identity)
            | Q(source_snapshot__transaction_id=identity)
        )
        for cancellation_id in recoveries.order_by("pk").values_list("pk", flat=True):
            entries[f"opening-cancellation:{cancellation_id}"] = {
                "kind": "opening_cancellation",
                "id": cancellation_id,
                "hash": _cancellation_raw_graph(org, cancellation_id)["fingerprint"],
            }
        # Later opposite cash has its own dated proof. Original source identity
        # binds only actual claims on this bank; invalid refund history is bound
        # separately by the global repair graph below.
        refunds = OpeningBalanceRefund.objects.filter(
            Q(bank_posting__transaction_id=identity)
            | Q(source_snapshot__transaction_id=identity)
        )
        for refund_id in refunds.order_by("pk").values_list("pk", flat=True):
            entries[f"opening-refund:{refund_id}"] = {
                "kind": "opening_refund",
                "id": refund_id,
                "hash": _refund_raw_graph(org, refund_id)["fingerprint"],
            }

    def posting(identity):
        if identity is None or identity in visited_postings:
            return
        visited_postings.add(identity)
        row = add(BankPosting, identity)
        if row:
            journal(row["journal_id"])
            bank(row["transaction_id"])

    def decision(identity):
        if identity is None or identity in visited_decisions:
            return
        visited_decisions.add(identity)
        row = add(OpeningBalanceSettlement, identity)
        if not row:
            return
        item(row["item_id"])
        journal(row["journal_id"])
        posting(row["bank_posting_id"])
        # A damaged current FK must not detach the stored original anchors.
        sealed = (
            row["source_snapshot"] if isinstance(row["source_snapshot"], dict) else {}
        )
        item(sealed_id(sealed.get("item_id")))
        original_journal = (
            sealed.get("journal") if isinstance(sealed.get("journal"), dict) else {}
        )
        journal(sealed_id(original_journal.get("id")))
        original_posting = (
            sealed.get("bank_posting")
            if isinstance(sealed.get("bank_posting"), dict)
            else {}
        )
        posting(sealed_id(original_posting.get("id")))
        original_bank = (
            sealed.get("bank") if isinstance(sealed.get("bank"), dict) else {}
        )
        bank(sealed_id(original_bank.get("id")))
        add(BankAccount, sealed_id(original_bank.get("account_id")))

    decision(int(pk))
    graph = {
        "version": VERSION,
        "organization_id": org.pk,
        "settlement_id": int(pk),
        "sources": [entries[key] for key in sorted(entries)],
    }
    correction_repairs = _invalid_correction_graphs(org)
    if correction_repairs:
        graph["correction_repairs"] = correction_repairs
    graph.update(_unknown_recovery_sources(org))
    graph.update(_unknown_refund_sources(org))
    return {**graph, "fingerprint": _hash(graph)}


def opening_balance_source_graph(*, organization, item):
    """Opaque current and sealed original anchors for a damaged opening item."""
    org = _org(organization)
    pk = getattr(item, "pk", item)
    if (
        isinstance(pk, bool)
        or not isinstance(pk, (int, str))
        or (isinstance(pk, str) and not pk.isdecimal())
        or int(pk) <= 0
    ):
        raise ValidationError("Pilih identitas saldo awal yang valid.")
    row = OpeningBalanceItem.objects.filter(pk=int(pk)).values().first()
    sources = [
        {
            "model": OpeningBalanceItem._meta.label_lower,
            "id": int(pk),
            "hash": _hash(row) if row is not None else None,
            "missing": row is None,
        }
    ]
    if row:
        sealed = (
            row["source_snapshot"] if isinstance(row["source_snapshot"], dict) else {}
        )
        party_ids = {row["party_id"]}
        journal_ids = {row["journal_id"]}
        if type(sealed.get("party_id")) is int:
            party_ids.add(sealed["party_id"])
        if (
            isinstance(sealed.get("journal"), dict)
            and type(sealed["journal"].get("id")) is int
        ):
            journal_ids.add(sealed["journal"]["id"])
        for identity in sorted(party_ids):
            party = Party.objects.filter(pk=identity).values().first()
            sources.append(
                {
                    "model": Party._meta.label_lower,
                    "id": identity,
                    "hash": _hash(party) if party is not None else None,
                    "missing": party is None,
                }
            )
        for identity in sorted(journal_ids):
            journal = Journal.objects.filter(pk=identity).values().first()
            lines = list(
                JournalLine.objects.filter(journal_id=identity).order_by("pk").values()
            )
            sources.append(
                {
                    "model": Journal._meta.label_lower,
                    "id": identity,
                    "hash": _hash(journal) if journal is not None else None,
                    "missing": journal is None,
                    "lines_hash": _hash(lines),
                    "line_count": len(lines),
                }
            )
        # Matching documents and sibling decisions remain opaque even if one
        # has been damaged or moved to another company by an external write.
        sources.append(
            {
                "kind": "party_opening_inventory",
                "hash": _hash(
                    list(
                        OpeningBalanceItem.objects.filter(party_id__in=party_ids)
                        .order_by("pk")
                        .values()
                    )
                ),
            }
        )
        sources.append(
            {
                "kind": "party_invoice_inventory",
                "hash": _hash(
                    list(
                        Invoice.objects.filter(party_id__in=party_ids)
                        .order_by("pk")
                        .values()
                    )
                ),
            }
        )
        sources.append(
            {
                "kind": "party_bill_inventory",
                "hash": _hash(
                    list(
                        Bill.objects.filter(supplier_id__in=party_ids)
                        .order_by("pk")
                        .values()
                    )
                ),
            }
        )
        for identity in (
            OpeningBalanceSettlement.objects.filter(
                Q(item_id=int(pk)) | Q(organization=org)
            )
            .order_by("pk")
            .values_list("pk", flat=True)
        ):
            sources.append(
                {
                    "kind": "opening_cash_graph",
                    "id": identity,
                    "hash": opening_settlement_source_graph(
                        organization=org, settlement=identity
                    )["fingerprint"],
                }
            )
    graph = {
        "version": VERSION,
        "organization_id": org.pk,
        "item_id": int(pk),
        "sources": sources,
    }
    correction_repairs = _invalid_correction_graphs(org)
    if correction_repairs:
        graph["correction_repairs"] = correction_repairs
    graph.update(_unknown_recovery_sources(org))
    graph.update(_unknown_refund_sources(org))
    return {**graph, "fingerprint": _hash(graph)}


def _inventory(org):
    """Read transitive sources unfiltered by date to expose damaged old anchors."""
    items = list(
        OpeningBalanceItem.objects.filter(
            Q(organization=org)
            | Q(journal__organization=org)
            | Q(source_snapshot__organization_id=org.pk)
        )
        .select_related("party", "journal")
        .order_by("pk")
    )
    settlements = list(
        OpeningBalanceSettlement.objects.filter(
            Q(organization=org)
            | Q(item__organization=org)
            | Q(bank_posting__organization=org)
            | Q(journal__organization=org)
            | Q(source_snapshot__organization_id=org.pk)
        )
        .select_related(
            "item__party",
            "item__journal",
            "bank_posting__transaction__account",
            "journal",
        )
        .order_by("date", "pk")
    )
    corrections = list(
        _correction_rows(org)
        .select_related("item__party", "item__journal", "journal")
        .order_by("date", "pk")
    )
    cancellations = list(
        _cancellation_rows(org)
        .select_related(
            "item__party",
            "item__journal",
            "journal",
            "settlement__item__party",
            "settlement__item__journal",
            "settlement__journal",
            "settlement__bank_posting__transaction__account",
        )
        .order_by("date", "pk")
    )
    recovery = opening_cash_cancellation_facts(organization=org)
    refund_facts = opening_cash_refund_facts(organization=org)
    refunds = list(
        _refund_rows(org)
        .select_related(
            "item__party",
            "item__journal",
            "journal",
            "settlement__item__party",
            "settlement__item__journal",
            "settlement__journal",
            "settlement__bank_posting__transaction__account",
            "bank_posting__transaction__account",
        )
        .order_by("date", "pk")
    )
    proofs, cash, correction_proofs, cancellation_proofs, dates, identities = (
        [],
        [],
        [],
        [],
        set(),
        set(),
    )
    for row in items:
        proof = _item_proof(org, row)
        identity = (
            row.kind,
            row.party_id,
            _document_identity(org, row.party, row.kind, row.source_reference),
        )
        if identity in identities:
            raise ValidationError(
                "Dokumen saldo awal tercatat lebih dari sekali. Periksa sumber bersama Owner."
            )
        identities.add(identity)
        dates.add(row.date)
        proofs.append({"id": row.pk, "proof": proof})
    if len(dates) > 1:
        raise ValidationError(
            "Saldo awal perusahaan menggunakan tanggal awal yang berbeda. Periksa bukti asli."
        )
    item_ids = {row.pk for row in items}
    posting_ids, journal_ids, bank_ids = set(), {row.journal_id for row in items}, set()
    for row in corrections:
        proof = _correction_proof(org, row)
        if row.item_id not in item_ids or row.journal_id in journal_ids:
            raise ValidationError(
                "Koreksi saldo awal tidak mempunyai sumber atau jurnal tersendiri yang lengkap."
            )
        journal_ids.add(row.journal_id)
        correction_proofs.append({"id": row.pk, "proof": proof})
    for row in settlements:
        proof = _settlement_proof(org, row)
        if (
            row.item_id not in item_ids
            or row.bank_posting_id in posting_ids
            or row.journal_id in journal_ids
        ):
            raise ValidationError(
                "Sumber pembayaran saldo awal terhubung lebih dari sekali atau tidak tersedia."
            )
        posting_ids.add(row.bank_posting_id)
        journal_ids.add(row.journal_id)
        bank_ids.add(row.bank_posting.transaction_id)
        cash.append({"id": row.pk, "proof": proof})
    settlement_ids = {row.pk for row in settlements}
    for row in cancellations:
        if (
            row.item_id not in item_ids
            or row.settlement_id not in settlement_ids
            or row.journal_id in journal_ids
        ):
            raise ValidationError(
                "Pembatalan pencocokan tidak mempunyai sumber dan jurnal tersendiri yang lengkap."
            )
        journal_ids.add(row.journal_id)
        cancellation_proofs.append(
            {"id": row.pk, "proof": _cancellation_proof(org, row)}
        )
    refund_proofs = []
    for row in refunds:
        if (
            row.item_id not in item_ids
            or row.settlement_id not in settlement_ids
            or row.journal_id in journal_ids
            or row.bank_posting_id in posting_ids
        ):
            raise ValidationError(
                "Pengembalian tidak mempunyai sumber, klaim dan jurnal tersendiri yang lengkap."
            )
        journal_ids.add(row.journal_id)
        bank_ids.add(row.bank_posting.transaction_id)
        refund_proofs.append({"id": row.pk, "proof": _refund_proof(org, row)})
    for row in items:
        _conserve(row, settlements, corrections, cancellations, refunds)
    existing_postings = set(
        BankPosting.objects.filter(kind__in=["opening_receipt", "opening_payment"])
        .filter(
            Q(organization=org)
            | Q(transaction__organization=org)
            | Q(journal__organization=org)
        )
        .values_list("pk", flat=True)
    )
    if posting_ids != existing_postings:
        raise ValidationError(
            "Ada klaim bank saldo awal yang tidak mempunyai keputusan pembayaran lengkap."
        )
    opening_journals = set(
        Journal.objects.filter(
            organization=org, source_key__startswith="opening:opening-record:"
        ).values_list("pk", flat=True)
    )
    if opening_journals != {row.journal_id for row in items}:
        raise ValidationError(
            "Ada jurnal saldo awal tanpa dokumen sumber yang lengkap."
        )
    correction_journals = set(
        Journal.objects.filter(
            Q(organization=org) | Q(lines__organization=org),
            source_key__startswith="opening:opening-correct:",
        )
        .distinct()
        .values_list("pk", flat=True)
    )
    if correction_journals != {row.journal_id for row in corrections}:
        raise ValidationError(
            "Ada jurnal koreksi saldo awal tanpa keputusan koreksi yang lengkap."
        )
    cash_journals = set(
        Journal.objects.filter(
            organization=org, source_key__startswith="bank-event:opening-settle:"
        ).values_list("pk", flat=True)
    )
    if cash_journals != {row.journal_id for row in settlements}:
        raise ValidationError(
            "Ada jurnal pembayaran saldo awal tanpa keputusan pembayaran yang lengkap."
        )
    banks = [
        _bank_freshness(org, BankTransaction.objects.get(pk=pk))
        for pk in sorted(bank_ids)
    ]
    return {
        "items": items,
        "settlements": settlements,
        "corrections": corrections,
        "cancellations": cancellations,
        "refunds": refunds,
        "proofs": proofs,
        "cash": cash,
        "correction_proofs": correction_proofs,
        "cancellation_proofs": cancellation_proofs,
        "recovery": recovery,
        "refund_facts": refund_facts,
        "refund_proofs": refund_proofs,
        "banks": banks,
        "date": next(iter(dates)) if dates else None,
    }


def _conserve(
    row, settlements, corrections, cancellations=(), refunds=(), *, candidate=None
):
    """No dated cash allocation may borrow principal from a later correction."""
    changes = {}
    for event in corrections:
        if event.item_id == row.pk:
            principal, cash = changes.setdefault(event.date, [ZERO, ZERO])
            changes[event.date] = [
                principal
                + (event.amount if event.direction == "increase" else -event.amount),
                cash,
            ]
    for event in settlements:
        if event.item_id == row.pk:
            principal, cash = changes.setdefault(event.date, [ZERO, ZERO])
            changes[event.date] = [principal, cash + event.amount]
    for event in cancellations:
        if event.item_id == row.pk:
            principal, cash = changes.setdefault(event.date, [ZERO, ZERO])
            changes[event.date] = [principal, cash - event.amount]
    for event in refunds:
        if event.item_id == row.pk:
            principal, cash = changes.setdefault(event.date, [ZERO, ZERO])
            changes[event.date] = [principal, cash - event.amount]
    if candidate is not None:
        day, principal_delta, cash_delta = candidate
        principal, cash = changes.setdefault(day, [ZERO, ZERO])
        changes[day] = [principal + principal_delta, cash + cash_delta]
    principal, cash = row.amount, ZERO
    for day in sorted(changes):
        if day < row.date:
            raise ValidationError(
                "Riwayat koreksi atau pembayaran mendahului tanggal saldo awal."
            )
        delta, paid = changes[day]
        principal += delta
        cash += paid
        if principal < ZERO or cash < ZERO or principal - cash < ZERO:
            raise ValidationError(
                "Koreksi atau pembayaran melebihi pokok yang tersedia pada riwayat tanggalnya, termasuk pembayaran berikutnya."
            )


def _graph_sources(graph):
    sources = {
        "proofs": graph["proofs"],
        "cash": graph["cash"],
        "banks": graph["banks"],
    }
    # Existing no-correction receipt/freshness fingerprints remain unchanged.
    if graph["correction_proofs"]:
        sources["corrections"] = graph["correction_proofs"]
    if graph["cancellation_proofs"]:
        sources["cancellations"] = graph["cancellation_proofs"]
    if graph["refund_proofs"]:
        sources["refunds"] = graph["refund_proofs"]
    return sources


def _bank_freshness(org, bank):
    state = _bank_state(org, bank)
    # Identity edits and cross-claim repairs must invalidate the viewed preview.
    # Mutable names belong here, not inside immutable original cash receipts.
    state["account"] = {
        name: getattr(bank.account, name)
        for name in (
            "pk",
            "organization_id",
            "name",
            "account_number",
            "opening_balance",
            "opening_date",
        )
    }
    state["allocation_sources"] = [
        {
            "allocation": row.pk,
            "invoice": {
                name: getattr(row.invoice, name)
                for name in (
                    "pk",
                    "organization_id",
                    "party_id",
                    "number",
                    "date",
                    "status",
                )
            },
            "party": {
                name: getattr(row.invoice.party, name)
                for name in ("pk", "organization_id", "kind", "name", "tax_id")
            },
        }
        for row in bank.allocations.select_related("invoice__party").order_by("pk")
    ]
    state["bill_sources"] = [
        {
            "payment": row.pk,
            "bill": {
                name: getattr(row.bill, name)
                for name in (
                    "pk",
                    "organization_id",
                    "supplier_id",
                    "number",
                    "date",
                    "status",
                )
            },
            "party": {
                name: getattr(row.bill.supplier, name)
                for name in ("pk", "organization_id", "kind", "name", "tax_id")
            },
        }
        for row in bank.bill_payments.select_related("bill__supplier").order_by("pk")
    ]
    return state


def _position(row, day, graph):
    payments = [
        event
        for event in graph["settlements"]
        if event.item_id == row.pk and event.date <= day
    ]
    corrections = [
        event
        for event in graph["corrections"]
        if event.item_id == row.pk and event.date <= day
    ]
    cancellations = [
        event
        for event in graph["cancellations"]
        if event.item_id == row.pk and event.date <= day
    ]
    refunds = [
        event
        for event in graph["refunds"]
        if event.item_id == row.pk and event.date <= day
    ]
    original = row.amount if row.date <= day else ZERO
    delta = sum(
        (
            event.amount if event.direction == "increase" else -event.amount
            for event in corrections
        ),
        ZERO,
    )
    opening = original + delta
    settled = (
        sum((event.amount for event in payments), ZERO)
        - sum((event.amount for event in cancellations), ZERO)
        - sum((event.amount for event in refunds), ZERO)
    )
    remaining = opening - settled
    if opening < ZERO or settled < ZERO or remaining < ZERO:
        raise ValidationError(
            "Riwayat pembayaran lebih besar daripada pokok saldo awal."
        )
    history = sorted(
        [*corrections, *payments, *cancellations, *refunds],
        key=lambda event: (
            event.date,
            {"correct": 0, "settle": 1, "cancel": 2, "refund": 3}[event.action],
            event.pk,
        ),
    )
    return {
        "integrity": True,
        "current": True,
        "issues": [],
        "blockers": [],
        "opening": opening,
        "original_opening": original,
        "correction_delta": delta,
        "corrections": corrections,
        "cancellations": cancellations,
        "settled": settled,
        "remaining": remaining,
        "history": history,
        "date": day,
        **(
            {
                "refunds": refunds,
                "refunded": sum((event.amount for event in refunds), ZERO),
            }
            if graph["refund_proofs"]
            else {}
        ),
        "cutoff": row.date,
        "kind": row.kind,
        "party": row.party,
        "item": row,
        "fingerprint": _hash(
            {
                "version": VERSION,
                "item_id": row.pk,
                "as_of": day,
                "sources": graph["proofs"],
                "cash": graph["cash"],
                "banks": graph["banks"],
                **(
                    {"corrections": graph["correction_proofs"]}
                    if graph["correction_proofs"]
                    else {}
                ),
                **(
                    {"cancellations": graph["cancellation_proofs"]}
                    if graph["cancellation_proofs"]
                    else {}
                ),
                **(
                    {"refunds": graph["refund_proofs"]}
                    if graph["refund_proofs"]
                    else {}
                ),
            }
        ),
    }


@transaction.atomic
def opening_balance_position(*, organization, item, as_of=None):
    org = ledger._lock_org(organization)
    row = _select(OpeningBalanceItem, item, org)
    try:
        day = ledger._date(as_of) if as_of is not None else timezone.localdate()
        return _position(row, day, _inventory(org))
    except (
        ValidationError,
        ObjectDoesNotExist,
        ValueError,
        TypeError,
        ArithmeticError,
        KeyError,
    ) as exc:
        issues = exc.messages if isinstance(exc, ValidationError) else [CORRUPT]
        return {
            "integrity": False,
            "current": False,
            "issues": issues,
            "blockers": issues,
            "opening": None,
            "original_opening": None,
            "correction_delta": None,
            "corrections": [],
            "cancellations": [],
            "settled": None,
            "remaining": None,
            "history": [],
            "item": row,
            "fingerprint": _hash(
                {
                    "version": VERSION,
                    "item_id": row.pk,
                    "invalid": issues,
                    **_unknown_correction_sources(org),
                    **_unknown_recovery_sources(org),
                    **_unknown_refund_sources(org),
                }
            ),
        }


def _record_boundary(org, graph, day, source_date, party, kind, reference):
    if source_date > day:
        raise ValidationError(
            "Tanggal dokumen asal tidak boleh setelah tanggal saldo awal."
        )
    if graph["date"] is not None and graph["date"] != day:
        raise ValidationError(
            f"Tanggal saldo awal perusahaan sudah ditetapkan {graph['date'].isoformat()}. Gunakan tanggal yang sama."
        )
    first = (
        Journal.objects.filter(organization=org, status="posted")
        .order_by("date")
        .values_list("date", flat=True)
        .first()
    )
    if first is not None and day > first:
        raise ValidationError(
            "Tanggal saldo awal harus pada atau sebelum jurnal pertama perusahaan agar riwayat tidak dihitung ulang."
        )
    identity = _document_identity(org, party, kind, reference)
    if OpeningBalanceItem.objects.filter(
        organization=org, kind=kind, party=party, source_identity=identity
    ).exists():
        raise ValidationError(
            "Dokumen ini sudah tercatat sebagai saldo awal. Buka catatan asal untuk mencocokkan pembayaran."
        )
    return identity


def assert_opening_document_available(*, organization, kind, party, references):
    """Ordinary document creation/approval must not duplicate opening principal."""
    org = _org(organization)
    party = _party(org, _select(Party, party, org), kind)
    if not isinstance(references, (list, tuple)):
        raise ValidationError(
            "Referensi dokumen harus berasal dari nomor dokumen yang dipilih."
        )
    identities = {normalize_supplier_reference(value) for value in references if value}
    graph = _inventory(org)
    for item in graph["items"]:
        if (
            item.party_id == party.pk
            and item.kind == kind
            and item.source_identity in identities
        ):
            raise ValidationError(
                "Dokumen ini sudah dibawa sebagai saldo awal. Cocokkan pembayaran pada saldo awal agar tidak dihitung ulang.",
                code="duplicate_opening_document",
            )


def _candidate(row, bank, value, graph, bank_state):
    if bank.date < row.date:
        raise ValidationError("Tanggal mutasi bank mendahului tanggal saldo awal.")
    if (row.kind == "receivable") != (bank.amount > ZERO):
        raise ValidationError(
            "Piutang memakai uang masuk; utang memakai uang keluar bank."
        )
    _conserve(
        row,
        graph["settlements"],
        graph["corrections"],
        graph["cancellations"],
        graph["refunds"],
        candidate=(bank.date, ZERO, value),
    )
    if value > abs(Decimal(bank_state["remaining"])):
        raise ValidationError("Jumlah melebihi sisa uang pada mutasi bank asli.")


def _correction_boundary(org, row, day, direction, value, graph):
    if day < row.date:
        raise ValidationError(
            "Tanggal koreksi tidak boleh mendahului tanggal saldo awal."
        )
    _conserve(
        row,
        graph["settlements"],
        graph["corrections"],
        graph["cancellations"],
        graph["refunds"],
        candidate=(day, value if direction == "increase" else -value, ZERO),
    )
    # Fiscal packages are a separate protected dependency, not guessed tax.
    from taxes.opening_balance_review_dependencies import (
        opening_principal_correction_blockers,
    )

    blockers = opening_principal_correction_blockers(
        organization=org, item=row, date=day
    )
    if not isinstance(blockers, list) or any(
        not isinstance(message, str) or not message for message in blockers
    ):
        raise ValidationError(
            "Status laporan pajak terkait belum dapat diperiksa. Minta Owner meninjau sumber."
        )
    if blockers:
        raise ValidationError(blockers)


def _cancellation_boundary(org, row, graph):
    if any(event.settlement_id == row.pk for event in graph["cancellations"]):
        raise ValidationError(
            "Pencocokan ini sudah dibatalkan. Gunakan mutasi asli untuk pencocokan baru."
        )
    if any(event.settlement_id == row.pk for event in graph["refunds"]):
        raise ValidationError(
            "Pembayaran asal sudah mempunyai pengembalian uang. Pembatalan pencocokan ini ditahan."
        )
    _conserve(
        row.item,
        graph["settlements"],
        graph["corrections"],
        graph["cancellations"],
        graph["refunds"],
        candidate=(row.date, ZERO, -row.amount),
    )
    from taxes.opening_balance_review_dependencies import opening_cash_recovery_blockers

    blockers = opening_cash_recovery_blockers(
        organization=org, settlement=row, date=row.date, action="cancel"
    )
    if not isinstance(blockers, list) or any(
        not isinstance(message, str) or not message for message in blockers
    ):
        raise ValidationError(
            "Status laporan pajak terkait belum dapat diperiksa. Minta Owner meninjau sumber."
        )
    if blockers:
        raise ValidationError(blockers)


def _refund_boundary(org, cash, bank, value, graph, state):
    _settlement_proof(org, cash)
    value = ledger._amount(value)
    if any(event.settlement_id == cash.pk for event in graph["cancellations"]):
        raise ValidationError(
            "Pembayaran asal sudah dibatalkan. Pengembalian dari pencocokan ini tidak dapat dicatat."
        )
    if (
        bank.pk == cash.bank_posting.transaction_id
        or bank.date < cash.date
        or (cash.item.kind == "receivable") != (bank.amount < ZERO)
    ):
        raise ValidationError(
            "Pilih mutasi bank lain dengan arah berlawanan, pada atau sesudah pembayaran asal."
        )
    refunded = sum(
        (event.amount for event in graph["refunds"] if event.settlement_id == cash.pk),
        ZERO,
    )
    if value > cash.amount - refunded or value > abs(Decimal(state["remaining"])):
        raise ValidationError(
            "Jumlah melebihi sisa pembayaran asal yang belum dikembalikan atau sisa bank pengembalian."
        )
    _conserve(
        cash.item,
        graph["settlements"],
        graph["corrections"],
        graph["cancellations"],
        graph["refunds"],
        candidate=(bank.date, ZERO, -value),
    )
    from taxes.opening_refund_review_dependencies import opening_refund_record_blockers

    blockers = opening_refund_record_blockers(
        organization=org,
        settlement=cash,
        transaction=bank,
        date=bank.date,
        amount=value,
    )
    if not isinstance(blockers, list) or any(
        not isinstance(message, str) or not message for message in blockers
    ):
        raise ValidationError(
            "Status laporan pajak terkait pengembalian belum dapat diperiksa. Minta Owner meninjau sumber."
        )
    if blockers:
        raise ValidationError(blockers)


def _refund_selection_sources(org, settlement, transaction):
    """A failed refund preview still binds both actual sources, without PII."""
    sources = {"refund_original": _cancel_selection_sources(org, settlement)}
    pk = getattr(transaction, "pk", transaction)
    if (
        isinstance(pk, bool)
        or not isinstance(pk, (int, str))
        or (isinstance(pk, str) and not pk.isdecimal())
        or int(pk) <= 0
    ):
        value = (
            pk if isinstance(pk, (str, int, bool)) or pk is None else type(pk).__name__
        )
        sources["refund_bank"] = {"invalid_identity_hash": _hash(value)}
    else:
        sources["refund_bank"] = {
            "id": int(pk),
            "source_hash": _refund_raw_graph(org, bank_id=int(pk))["fingerprint"],
        }
    return sources


def _cancel_selection_sources(org, settlement):
    """A failed preview still binds the selected original and sealed links."""
    pk = getattr(settlement, "pk", settlement)
    if (
        isinstance(pk, bool)
        or not isinstance(pk, (int, str))
        or (isinstance(pk, str) and not pk.isdecimal())
        or int(pk) <= 0
    ):
        value = (
            pk if isinstance(pk, (str, int, bool)) or pk is None else type(pk).__name__
        )
        return {"cancel_selection": {"invalid_identity_hash": _hash(value)}}
    graph = opening_settlement_source_graph(organization=org, settlement=int(pk))
    return {
        "cancel_selection": {
            "settlement_id": int(pk),
            "original_cash_graph": graph["fingerprint"],
        }
    }


@transaction.atomic
def opening_balance_action_context(
    *,
    organization,
    action,
    kind=None,
    party=None,
    date=None,
    source_date=None,
    source_reference="",
    amount=None,
    item=None,
    transaction=None,
    direction=None,
    settlement=None,
):
    org = (
        ledger._lock_org(organization)
        if action in {"cancel", "refund"}
        else _org(organization)
    )
    result = {
        "opening": None,
        "settled": None,
        "remaining": None,
        "amount": None,
        "before": None,
        "after": None,
        "bank_remaining": None,
        "bank_after": None,
        "original_opening": None,
        "correction_delta": None,
        "corrections": [],
        "cancellations": [],
    }
    try:
        if action not in MODELS:
            raise ValidationError("Pilih pencatatan saldo awal atau pembayaran asli.")
        graph = _inventory(org)
        sources = _graph_sources(graph)
        sources["periods"] = list(
            AccountingPeriod.objects.filter(organization=org, closed=True)
            .order_by("year", "month")
            .values("pk", "year", "month", "closed")
        )
        value = ledger._amount(amount) if amount is not None else None
        if action == "record":
            party = _party(org, _select(Party, party, org), kind)
            day, source_day = (
                ledger._actual_date(date),
                ledger._actual_date(source_date),
            )
            reference = ledger._review_text(
                source_reference, "Nomor dokumen asal", limit=160
            )
            identity = normalize_supplier_reference(reference)
            sources["selection"] = {
                "kind": kind,
                "party_id": party.pk,
                "date": day,
                "source_date": source_day,
                "source_identity": identity,
            }
            # The earliest existing journal fixes the historical boundary.
            sources["first_journal"] = Journal.objects.filter(
                organization=org, status="posted"
            ).order_by("date", "pk")
            sources["first_journal"] = list(
                sources["first_journal"].values("pk", "date", "source_key")[:1]
            )
            result.update(
                opening=value, settled=ZERO, remaining=value, before=ZERO, after=value
            )
            blockers = []
            try:
                _record_boundary(org, graph, day, source_day, party, kind, reference)
            except ValidationError as exc:
                blockers.extend(exc.messages)
        elif action == "settle":
            item = _select(OpeningBalanceItem, item, org)
            bank = _select(BankTransaction, transaction, org)
            party, kind, day = item.party, item.kind, ledger._actual_date(bank.date)
            source_day, reference = item.source_date, item.source_reference
            state = _bank_freshness(org, bank)
            sources["selection"] = {"item_id": item.pk, "transaction_id": bank.pk}
            sources["bank"] = state
            position = _position(item, timezone.localdate(), graph)
            result.update(
                opening=position["opening"],
                settled=position["settled"],
                remaining=position["remaining"],
                original_opening=position["original_opening"],
                correction_delta=position["correction_delta"],
                corrections=position["corrections"],
                before=position["remaining"],
                after=position["remaining"] - value if value is not None else None,
                bank_remaining=abs(Decimal(state["remaining"])),
                bank_after=abs(Decimal(state["remaining"])) - value
                if value is not None
                else None,
                item=item,
                transaction=bank,
                position=position,
            )
            blockers = []
            try:
                _candidate(item, bank, value or ZERO, graph, state)
            except ValidationError as exc:
                blockers.extend(exc.messages)
        elif action == "correct":
            item = _select(OpeningBalanceItem, item, org)
            party, kind, day = item.party, item.kind, ledger._actual_date(date)
            direction = _direction(direction)
            source_day, reference = item.source_date, item.source_reference
            position = _position(item, day, graph)
            signed_value = (
                value
                if direction == "increase"
                else -value
                if value is not None
                else None
            )
            sources["selection"] = {
                "item_id": item.pk,
                "date": day,
                "direction": direction,
                "amount": value,
            }
            result.update(
                opening=position["opening"],
                settled=position["settled"],
                remaining=position["remaining"],
                original_opening=position["original_opening"],
                correction_delta=position["correction_delta"],
                corrections=position["corrections"],
                before=position["remaining"],
                after=position["remaining"] + signed_value
                if signed_value is not None
                else None,
                item=item,
                direction=direction,
                position=position,
                opening_before=position["opening"],
                opening_after=position["opening"] + signed_value
                if signed_value is not None
                else None,
            )
            blockers = []
            try:
                _correction_boundary(org, item, day, direction, value or ZERO, graph)
            except ValidationError as exc:
                blockers.extend(exc.messages)
        elif action == "refund":
            if (
                any(
                    value is not None
                    for value in (kind, party, date, source_date, item, direction)
                )
                or source_reference
            ):
                raise ValidationError(
                    "Pengembalian memakai pembayaran asal, mutasi bank baru dan jumlah positif."
                )
            cash = _select(OpeningBalanceSettlement, settlement, org)
            item = cash.item
            bank = _select(BankTransaction, transaction, org)
            party, kind, day = item.party, item.kind, ledger._actual_date(bank.date)
            source_day, reference = item.source_date, item.source_reference
            position = _position(item, timezone.localdate(), graph)
            state = _bank_freshness(org, bank)
            sources["selection"] = {
                "settlement_id": cash.pk,
                "item_id": item.pk,
                "transaction_id": bank.pk,
            }
            sources["bank"] = state
            refunded = sum(
                (
                    event.amount
                    for event in graph["refunds"]
                    if event.settlement_id == cash.pk
                ),
                ZERO,
            )
            result.update(
                opening=position["opening"],
                settled=position["settled"],
                remaining=position["remaining"],
                original_opening=position["original_opening"],
                correction_delta=position["correction_delta"],
                corrections=position["corrections"],
                before=position["remaining"],
                after=position["remaining"] + value if value is not None else None,
                bank_remaining=abs(Decimal(state["remaining"])),
                bank_after=abs(Decimal(state["remaining"])) - value
                if value is not None
                else None,
                refundable_remaining=cash.amount - refunded,
                item=item,
                transaction=bank,
                settlement=cash,
                position=position,
            )
            blockers = []
            try:
                _refund_boundary(org, cash, bank, value or ZERO, graph, state)
            except ValidationError as exc:
                blockers.extend(exc.messages)
        else:
            if (
                any(
                    value is not None
                    for value in (
                        kind,
                        party,
                        date,
                        source_date,
                        amount,
                        item,
                        transaction,
                        direction,
                    )
                )
                or source_reference
            ):
                raise ValidationError(
                    "Pembatalan memakai seluruh jumlah, tanggal bank dan dokumen pencocokan asli."
                )
            cash = _select(OpeningBalanceSettlement, settlement, org)
            item, bank, value = cash.item, cash.bank_posting.transaction, cash.amount
            party, kind, day = item.party, item.kind, ledger._actual_date(cash.date)
            source_day, reference = item.source_date, item.source_reference
            position = _position(item, timezone.localdate(), graph)
            state = _bank_freshness(org, bank)
            sources["selection"] = {
                "settlement_id": cash.pk,
                "item_id": item.pk,
                "transaction_id": bank.pk,
            }
            sources["bank"] = state
            result.update(
                opening=position["opening"],
                settled=position["settled"],
                remaining=position["remaining"],
                original_opening=position["original_opening"],
                correction_delta=position["correction_delta"],
                corrections=position["corrections"],
                cancellations=position["cancellations"],
                before=position["remaining"],
                after=position["remaining"] + value,
                bank_remaining=abs(Decimal(state["remaining"])),
                bank_after=abs(Decimal(state["remaining"])) + value,
                item=item,
                transaction=bank,
                settlement=cash,
                position=position,
            )
            blockers = []
            try:
                _cancellation_boundary(org, cash, graph)
            except ValidationError as exc:
                blockers.extend(exc.messages)
        sources["party"] = {
            name: getattr(party, name)
            for name in ("pk", "organization_id", "kind", "name", "tax_id")
        }
        try:
            assert_open_period(org.pk, day)
        except ValidationError as exc:
            blockers.extend(exc.messages)
        result.update(
            current=True,
            integrity=True,
            issues=[],
            blockers=blockers,
            amount=value,
            date=day,
            cutoff=item.date
            if action in {"settle", "correct", "cancel", "refund"}
            else day,
            kind=kind,
            party=party,
            source_date=source_day,
            source_reference=reference,
            fingerprint=_hash(
                {"version": VERSION, "action": action, "sources": sources}
            ),
        )
        return result
    except (
        ValidationError,
        ObjectDoesNotExist,
        ValueError,
        TypeError,
        ArithmeticError,
        KeyError,
    ) as exc:
        issues = exc.messages if isinstance(exc, ValidationError) else [CORRUPT]
        result.update(
            current=False,
            integrity=False,
            issues=issues,
            blockers=issues,
            fingerprint=_hash(
                {
                    "version": VERSION,
                    "action": action,
                    "invalid": issues,
                    **_unknown_correction_sources(org),
                    **_unknown_recovery_sources(org),
                    **_unknown_refund_sources(org),
                    **(
                        _cancel_selection_sources(org, settlement)
                        if action == "cancel"
                        else {}
                    ),
                    **(
                        _refund_selection_sources(org, settlement, transaction)
                        if action == "refund"
                        else {}
                    ),
                }
            ),
        )
        return result


def _replay(org, action, key, payload):
    old = MODELS[action].objects.filter(organization=org, request_key=key).first()
    if old:
        _inventory(org)
        _proof_for(action)(org, old)
        if payload != old.request_payload:
            raise ValidationError(
                "Kunci permintaan sudah dipakai untuk fakta yang berbeda. Periksa ulang sumber."
            )
        return old
    return None


def _fresh(org, action, expected, **values):
    context = opening_balance_action_context(organization=org, action=action, **values)
    if not context["current"] or context["blockers"]:
        raise ValidationError(context["blockers"] or context["issues"])
    if context["fingerprint"] != expected:
        raise ValidationError(
            "Sumber berubah sejak diperiksa. Isian dipertahankan; periksa ulang lalu konfirmasi kembali."
        )


def _save(org, action, actor, key, payload, journal, **values):
    row = MODELS[action](
        organization=org,
        actor=actor,
        amount=Decimal(payload["amount"]),
        date=Date.fromisoformat(payload["date"]),
        reference=f"opening-{action}:{key}",
        reason=payload["reason"],
        evidence=payload["evidence"],
        request_key=key,
        request_payload=payload,
        journal=journal,
        **values,
    )
    row.source_snapshot = _proof_for(action)(org, row, capture=True)
    row.source_hash = _hash(row.source_snapshot)
    row._service_transition = True
    try:
        row.save()
    finally:
        row.__dict__.pop("_service_transition", None)
    detail = {
        "action": action,
        "amount": str(row.amount),
        "journal_id": row.journal_id,
        "request_key": str(key),
        "source_hash": row.source_hash,
        "cash": action in {"settle", "refund"},
        **{
            name: getattr(row, name, None)
            for name in ("party_id", "item_id", "bank_posting_id")
        },
    }
    if action == "correct":
        detail["direction"] = row.direction
    if action == "cancel":
        detail.update(settlement_id=row.settlement_id, physical_cash=False)
    if action == "refund":
        detail.update(
            settlement_id=row.settlement_id,
            transaction_id=row.bank_posting.transaction_id,
            original_transaction_id=row.settlement.bank_posting.transaction_id,
            physical_cash=True,
        )
    record(org, actor, f"finance.opening_balance.{action}", obj=row, detail=detail)
    ledger._event(org, f"opening_balance.{action}", row, detail)
    return row


def _confirmed(value):
    if value is not True:
        raise ValidationError("Periksa sumber dan konfirmasi sebelum menyimpan.")


@transaction.atomic
def record_opening_balance(
    *,
    organization,
    actor,
    kind,
    party,
    date,
    source_date,
    source_reference,
    amount,
    reason,
    evidence,
    request_key,
    expected_fingerprint,
    confirmed,
):
    org = ledger._lock_org(organization)
    ledger._owner(org, actor)
    party = _party(org, _select(Party, party, org, lock=True), kind)
    day, source_day = ledger._actual_date(date), ledger._actual_date(source_date)
    value, key = ledger._amount(amount), _key(request_key)
    payload = _payload(
        action="record",
        actor=actor,
        amount=value,
        date=day,
        source_date=source_day,
        source_reference=source_reference,
        kind=kind,
        party=party,
        reason=reason,
        evidence=evidence,
        expected_fingerprint=expected_fingerprint,
    )
    _confirmed(confirmed)
    old = _replay(org, "record", key, payload)
    if old:
        return old
    _fresh(
        org,
        "record",
        expected_fingerprint,
        kind=kind,
        party=party,
        date=day,
        source_date=source_day,
        source_reference=payload["source_reference"],
        amount=value,
    )
    entries = (
        [("AR", value, ZERO), ("EQUITY", ZERO, value)]
        if kind == "receivable"
        else [("EQUITY", value, ZERO), ("AP", ZERO, value)]
    )
    journal = ledger._post(
        org,
        day,
        f"opening:opening-record:{key}",
        "Saldo awal piutang / utang yang ditinjau Owner",
        entries,
    )
    return _save(
        org,
        "record",
        actor,
        key,
        payload,
        journal,
        party=party,
        kind=kind,
        source_date=source_day,
        source_reference=payload["source_reference"],
        source_identity=normalize_supplier_reference(payload["source_reference"]),
    )


@transaction.atomic
def settle_opening_balance(
    *,
    organization,
    actor,
    item,
    transaction,
    amount,
    reason,
    evidence,
    request_key,
    expected_fingerprint,
    confirmed,
):
    org = ledger._lock_org(organization)
    ledger._write_actor(org, actor)
    row = _select(OpeningBalanceItem, item, org, lock=True)
    bank = _select(BankTransaction, transaction, org, lock=True)
    _party(org, row.party, row.kind)
    day, value, key = (
        ledger._actual_date(bank.date),
        ledger._amount(amount),
        _key(request_key),
    )
    payload = _payload(
        action="settle",
        actor=actor,
        amount=value,
        date=day,
        source_date=row.source_date,
        source_reference=row.source_reference,
        kind=row.kind,
        party=row.party,
        item=row,
        bank=bank,
        reason=reason,
        evidence=evidence,
        expected_fingerprint=expected_fingerprint,
    )
    _confirmed(confirmed)
    old = _replay(org, "settle", key, payload)
    if old:
        return old
    _fresh(
        org, "settle", expected_fingerprint, item=row, transaction=bank, amount=value
    )
    incoming = row.kind == "receivable"
    entries = (
        [("BANK", value, ZERO), ("AR", ZERO, value)]
        if incoming
        else [("AP", value, ZERO), ("BANK", ZERO, value)]
    )
    posting = ledger._bank_post(
        org,
        bank,
        value if incoming else -value,
        "opening_receipt" if incoming else "opening_payment",
        f"opening-settle:{key}",
        payload["evidence"],
        entries,
        actor,
    )
    return _save(
        org,
        "settle",
        actor,
        key,
        payload,
        posting.journal,
        item=row,
        bank_posting=posting,
    )


@transaction.atomic
def correct_opening_balance(
    *,
    organization,
    actor,
    item,
    date,
    direction,
    amount,
    reason,
    evidence,
    request_key,
    expected_fingerprint,
    confirmed,
):
    """Append an evidenced principal change effective on its own open date."""
    org = ledger._lock_org(organization)
    ledger._owner(org, actor)
    row = _select(OpeningBalanceItem, item, org, lock=True)
    _party(org, row.party, row.kind)
    day, value, key = (
        ledger._actual_date(date),
        ledger._amount(amount),
        _key(request_key),
    )
    direction = _direction(direction)
    payload = _payload(
        action="correct",
        actor=actor,
        amount=value,
        date=day,
        source_date=row.source_date,
        source_reference=row.source_reference,
        kind=row.kind,
        party=row.party,
        item=row,
        reason=reason,
        evidence=evidence,
        expected_fingerprint=expected_fingerprint,
        direction=direction,
    )
    _confirmed(confirmed)
    old = _replay(org, "correct", key, payload)
    if old:
        return old
    _fresh(
        org,
        "correct",
        expected_fingerprint,
        item=row,
        date=day,
        direction=direction,
        amount=value,
    )
    journal = ledger._post(
        org,
        day,
        f"opening:opening-correct:{key}",
        "Koreksi pokok saldo lama berdasarkan bukti Owner",
        _correction_entries(row.kind, direction, value),
    )
    return _save(
        org, "correct", actor, key, payload, journal, item=row, direction=direction
    )


@transaction.atomic
def cancel_opening_settlement(
    *,
    organization,
    actor,
    settlement,
    reason,
    evidence,
    request_key,
    expected_fingerprint,
    confirmed,
):
    """Undo a full wrong assignment on its original, still-open bank date."""
    org = ledger._lock_org(organization)
    ledger._owner(org, actor)
    cash = _select(OpeningBalanceSettlement, settlement, org, lock=True)
    row = _select(OpeningBalanceItem, cash.item_id, org, lock=True)
    bank = _select(BankTransaction, cash.bank_posting.transaction_id, org, lock=True)
    _party(org, row.party, row.kind)
    day, value, key = (
        ledger._actual_date(cash.date),
        ledger._amount(cash.amount),
        _key(request_key),
    )
    payload = _payload(
        action="cancel",
        actor=actor,
        amount=value,
        date=day,
        source_date=row.source_date,
        source_reference=row.source_reference,
        kind=row.kind,
        party=row.party,
        item=row,
        bank=bank,
        settlement=cash,
        reason=reason,
        evidence=evidence,
        expected_fingerprint=expected_fingerprint,
    )
    _confirmed(confirmed)
    old = _replay(org, "cancel", key, payload)
    if old:
        return old
    _fresh(org, "cancel", expected_fingerprint, settlement=cash)
    entries = (
        [("AR", value, ZERO), ("BANK", ZERO, value)]
        if row.kind == "receivable"
        else [("BANK", value, ZERO), ("AP", ZERO, value)]
    )
    journal = ledger._post(
        org,
        day,
        f"opening:opening-cancel:{key}",
        "Pembatalan pencocokan saldo lama; mutasi bank asli tetap disimpan",
        entries,
    )
    return _save(org, "cancel", actor, key, payload, journal, item=row, settlement=cash)


@transaction.atomic
def refund_opening_settlement(
    *,
    organization,
    actor,
    settlement,
    transaction,
    amount,
    reason,
    evidence,
    request_key,
    expected_fingerprint,
    confirmed,
):
    """Match genuine opposite cash to the original payment, on the new bank date."""
    org = ledger._lock_org(organization)
    ledger._write_actor(org, actor)
    cash = _select(OpeningBalanceSettlement, settlement, org, lock=True)
    row = _select(OpeningBalanceItem, cash.item_id, org, lock=True)
    bank = _select(BankTransaction, transaction, org, lock=True)
    _party(org, row.party, row.kind)
    day, value, key = (
        ledger._actual_date(bank.date),
        ledger._amount(amount),
        _key(request_key),
    )
    payload = _payload(
        action="refund",
        actor=actor,
        amount=value,
        date=day,
        source_date=row.source_date,
        source_reference=row.source_reference,
        kind=row.kind,
        party=row.party,
        item=row,
        bank=bank,
        settlement=cash,
        reason=reason,
        evidence=evidence,
        expected_fingerprint=expected_fingerprint,
    )
    _confirmed(confirmed)
    old = _replay(org, "refund", key, payload)
    if old:
        return old
    _fresh(
        org,
        "refund",
        expected_fingerprint,
        settlement=cash,
        transaction=bank,
        amount=value,
    )
    incoming_original = row.kind == "receivable"
    signed = -value if incoming_original else value
    entries = (
        [("AR", value, ZERO), ("BANK", ZERO, value)]
        if incoming_original
        else [("BANK", value, ZERO), ("AP", ZERO, value)]
    )
    posting = ledger._bank_post(
        org,
        bank,
        signed,
        "opening_receipt_refund" if incoming_original else "opening_payment_refund",
        f"opening-refund:{key}",
        payload["evidence"],
        entries,
        actor,
    )
    return _save(
        org,
        "refund",
        actor,
        key,
        payload,
        posting.journal,
        item=row,
        settlement=cash,
        bank_posting=posting,
    )


@transaction.atomic
def opening_balance_close_facts(*, organization, cutoff):
    """Dated residuals for AR/AP conservation, never an apparent zero on damage."""
    org = ledger._lock_org(organization)
    try:
        day = ledger._date(cutoff)
        graph = _inventory(org)
        receivable = payable = ZERO
        for row in graph["items"]:
            amount = _position(row, day, graph)["remaining"]
            if row.kind == "receivable":
                receivable += amount
            else:
                payable += amount
        return {
            "receivable": receivable,
            "payable": payable,
            "blockers": [],
            "integrity": True,
            "current": True,
            "cutoff": day,
            "fingerprint": _hash(
                {"version": VERSION, "cutoff": day, **_graph_sources(graph)}
            ),
        }
    except (
        ValidationError,
        ObjectDoesNotExist,
        ValueError,
        TypeError,
        ArithmeticError,
        KeyError,
    ) as exc:
        issues = exc.messages if isinstance(exc, ValidationError) else [CORRUPT]
        return {
            "receivable": None,
            "payable": None,
            "blockers": issues,
            "integrity": False,
            "current": False,
            "fingerprint": _hash(
                {
                    "version": VERSION,
                    "invalid": issues,
                    **_unknown_correction_sources(org),
                    **_unknown_recovery_sources(org),
                    **_unknown_refund_sources(org),
                }
            ),
        }
