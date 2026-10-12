"""Allocate reviewed supplier costs to completed units, with immutable corrections."""

import hashlib
import json

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from core.audit import record

from . import services as ledger
from .costing_models import CostAllocation, CostReversal
from .fulfillment import _quantity, fulfillment_summary
from .models import ZERO, Bill, Invoice


def _hash(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _save(model, **values):
    row = model(**values)
    row._costing_write = True
    row.save()
    return row


def _active(query, as_of=None):
    if as_of is None:
        return query.filter(reversal__isnull=True)
    return query.filter(date__lte=as_of).filter(
        Q(reversal__isnull=True) | Q(reversal__date__gt=as_of)
    )


def _chronological(organization, invoice, bill, date):
    related = CostAllocation.objects.filter(organization=organization).filter(
        Q(invoice=invoice) | Q(bill=bill)
    )
    if (
        related.filter(date__gt=date).exists()
        or related.filter(reversal__date__gt=date).exists()
    ):
        raise ValidationError(
            "Catat alokasi dan pembalikan biaya berurutan; tanggal tidak boleh mendahului catatan berikutnya."
        )


@transaction.atomic
def allocate_cost(
    *,
    organization,
    actor,
    invoice,
    bill,
    quantity,
    amount,
    date,
    reference,
    reason,
    evidence,
):
    org = ledger._lock_org(organization)
    ledger._write_actor(org, actor)
    invoice = ledger._scoped(Invoice, invoice, org, lock=True)
    bill = ledger._scoped(Bill, bill, org, lock=True)
    quantity, amount, date = (
        _quantity(quantity),
        ledger._amount(amount),
        ledger._actual_date(date),
    )
    reference = ledger._review_text(reference, "Referensi alokasi", limit=120)
    reason, evidence = (
        ledger._review_text(reason, "Alasan alokasi"),
        ledger._review_text(evidence, "Bukti biaya per unit"),
    )
    old = CostAllocation.objects.filter(organization=org, reference=reference).first()
    if old:
        if (
            old.invoice_id,
            old.bill_id,
            old.quantity,
            old.amount,
            old.date,
            old.reason,
            old.evidence,
        ) != (invoice.pk, bill.pk, quantity, amount, date, reason, evidence):
            raise ValidationError(
                "Referensi alokasi biaya sudah digunakan untuk fakta berbeda."
            )
        return old
    ledger._period(org, date)
    if bill.status != "approved" or bill.category != "provider" or bill.date > date:
        raise ValidationError(
            "Pilih tagihan penyedia tes yang telah disahkan pada atau sebelum tanggal alokasi."
        )
    if invoice.status not in {"issued", "delivered"}:
        raise ValidationError(
            "Alokasi biaya memerlukan invoice terbit dengan unit yang sudah dilaksanakan."
        )
    _chronological(org, invoice, bill, date)
    bill_allocated = (
        _active(bill.cost_allocations).aggregate(value=Sum("amount"))["value"] or ZERO
    )
    invoice_allocated = (
        _active(invoice.cost_allocations).aggregate(value=Sum("quantity"))["value"] or 0
    )
    delivered = fulfillment_summary(organization=org, invoice=invoice, as_of=date)[
        "delivered_quantity"
    ]
    if bill_allocated + amount > bill.amount:
        raise ValidationError(
            "Alokasi melampaui jumlah bruto tagihan penyedia yang tersedia."
        )
    if bill.service_date > bill.date:
        recognized_cost = (
            bill.prepayment_releases.filter(date__lte=date).aggregate(
                value=Sum("amount")
            )["value"]
            or ZERO
        )
        if bill_allocated + amount > recognized_cost:
            raise ValidationError(
                "Biaya dibayar di muka belum dilepas ke beban sebesar alokasi ini. Catat bukti layanan penyedia dan pelepasan biaya terlebih dahulu."
            )
    if invoice_allocated + quantity > delivered:
        raise ValidationError(
            "Alokasi melampaui unit invoice yang sudah dilaksanakan dan belum diberi biaya."
        )
    row = _save(
        CostAllocation,
        organization=org,
        invoice=invoice,
        bill=bill,
        quantity=quantity,
        amount=amount,
        date=date,
        reference=reference,
        reason=reason,
        evidence=evidence,
        created_by=actor,
    )
    record(
        org,
        actor,
        "finance.cost.allocated",
        obj=row,
        detail={
            "invoice_id": invoice.pk,
            "bill_id": bill.pk,
            "quantity": quantity,
            "amount": str(amount),
            "date": str(date),
            "reference": reference,
        },
    )
    return row


@transaction.atomic
def reverse_cost(*, organization, actor, allocation, date, reference, reason, evidence):
    org = ledger._lock_org(organization)
    ledger._write_actor(org, actor)
    allocation = ledger._scoped(CostAllocation, allocation, org, lock=True)
    date = ledger._actual_date(date)
    reference = ledger._review_text(reference, "Referensi pembalikan", limit=120)
    reason, evidence = (
        ledger._review_text(reason, "Alasan pembalikan"),
        ledger._review_text(evidence, "Bukti koreksi biaya"),
    )
    old = CostReversal.objects.filter(organization=org, reference=reference).first()
    if old:
        if (old.allocation_id, old.date, old.reason, old.evidence) != (
            allocation.pk,
            date,
            reason,
            evidence,
        ):
            raise ValidationError(
                "Referensi pembalikan sudah digunakan untuk fakta berbeda."
            )
        return old
    ledger._period(org, date)
    if date < allocation.date or hasattr(allocation, "reversal"):
        raise ValidationError(
            "Alokasi telah dibalik atau tanggal mendahului alokasi asli."
        )
    _chronological(org, allocation.invoice, allocation.bill, date)
    row = _save(
        CostReversal,
        organization=org,
        allocation=allocation,
        date=date,
        reference=reference,
        reason=reason,
        evidence=evidence,
        created_by=actor,
    )
    record(
        org,
        actor,
        "finance.cost.reversed",
        obj=row,
        detail={
            "allocation_id": allocation.pk,
            "invoice_id": allocation.invoice_id,
            "bill_id": allocation.bill_id,
            "quantity": allocation.quantity,
            "amount": str(allocation.amount),
            "date": str(date),
        },
    )
    return row


def cost_facts(*, organization, invoice, as_of=None):
    invoice = ledger._scoped(Invoice, invoice, organization)
    cutoff = ledger._actual_date(as_of) if as_of else timezone.localdate()
    fulfillment = fulfillment_summary(
        organization=organization, invoice=invoice, as_of=cutoff
    )
    rows = list(
        _active(
            invoice.cost_allocations.select_related("bill", "reversal"), cutoff
        ).order_by("pk")
    )
    amount = sum((row.amount for row in rows), ZERO)
    quantity = sum(row.quantity for row in rows)
    delivered = fulfillment["delivered_quantity"]
    complete = (
        delivered > 0
        and quantity == delivered
        and all(row.bill.status == "approved" for row in rows)
    )
    issues = (
        []
        if complete
        else [
            "Biaya penyedia belum dipetakan lengkap ke seluruh unit yang telah dilaksanakan."
        ]
    )
    snapshot = {
        "invoice_id": invoice.pk,
        "as_of": str(cutoff),
        "fulfillment": fulfillment,
        "allocations": [
            {
                "id": row.pk,
                "bill_id": row.bill_id,
                "quantity": row.quantity,
                "amount": str(row.amount),
                "date": str(row.date),
            }
            for row in rows
        ],
        "reversals": list(
            CostReversal.objects.filter(
                organization=organization, allocation__invoice=invoice, date__lte=cutoff
            ).values("pk", "allocation_id", "date")
        ),
    }
    return {
        "allocated_quantity": quantity,
        "delivered_quantity": delivered,
        "active_amount": amount,
        "complete": complete,
        "actual_cost": amount if complete else None,
        "estimated_cost": invoice.cost_estimate,
        "scope": "completed_invoice_units",
        "remaining_quantity": fulfillment["remaining_quantity"],
        "issues": issues,
        "fingerprint": _hash(snapshot),
    }


def request_price_override(**kwargs):
    from .pricing_approval import request_price_override as command

    return command(**kwargs)


def decide_price_override(**kwargs):
    from .pricing_approval import decide_price_override as command

    return command(**kwargs)


def bind_price_override(**kwargs):
    from .pricing_approval import bind_price_override as command

    return command(**kwargs)
