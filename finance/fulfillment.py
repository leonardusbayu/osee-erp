"""Unit-based fulfillment using the existing accounting and authorization rules."""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from core.audit import record

from . import services as ledger
from .fulfillment_models import CreditRefund, InvoiceCredit, InvoiceDelivery
from .models import ZERO, Invoice


def _quantity(value):
    if (
        isinstance(value, bool)
        or not str(value).isdigit()
        or not 1 <= int(value) <= 100000
    ):
        raise ValidationError(
            "Jumlah unit harus bilangan bulat positif, maksimal 100.000."
        )
    return int(value)


def fulfillment_summary(*, organization, invoice, as_of=None):
    invoice = ledger._scoped(Invoice, invoice, organization)
    cutoff = ledger._date(as_of) if as_of else timezone.localdate()
    deliveries = invoice.unit_deliveries.filter(date__lte=cutoff)
    delivered = deliveries.aggregate(value=Sum("quantity"))["value"] or 0
    legacy = invoice.status == "delivered" and not invoice.unit_deliveries.exists()
    if legacy and invoice.delivered_at <= cutoff:
        delivered = invoice.quantity
    credits = invoice.unit_credits.filter(date__lte=cutoff)
    cancelled = (
        credits.filter(kind="unfulfilled").aggregate(value=Sum("quantity"))["value"]
        or 0
    )
    service_credits = (
        credits.filter(kind="service").aggregate(value=Sum("quantity"))["value"] or 0
    )
    credit_amount = credits.aggregate(value=Sum("amount"))["value"] or ZERO
    from .models import InvoiceCancellation

    whole_cancelled = InvoiceCancellation.objects.filter(
        organization=organization, invoice=invoice, date__lte=cutoff
    ).exists()
    if whole_cancelled:
        cancelled, credit_amount = invoice.quantity, invoice.total
    return {
        "quantity": invoice.quantity,
        "delivered_quantity": delivered,
        "remaining_quantity": invoice.quantity - delivered - cancelled,
        "cancelled_quantity": cancelled,
        "service_credit_quantity": service_credits,
        "credited_amount": credit_amount,
        "net_total": invoice.total - credit_amount,
        "recognized_amount": invoice.unit_price * (delivered - service_credits),
        "legacy_full_delivery": legacy,
    }


@transaction.atomic
def deliver_units(
    *,
    organization,
    actor,
    invoice,
    quantity,
    date,
    reference,
    evidence,
    registration=None,
):
    organization = ledger._lock_org(organization)
    ledger._write_actor(organization, actor)
    invoice = ledger._scoped(Invoice, invoice, organization, lock=True)
    quantity, date = _quantity(quantity), ledger._actual_date(date)
    reference = ledger._review_text(reference, "Referensi pelaksanaan", limit=120)
    evidence = ledger._review_text(evidence, "Bukti pelaksanaan")
    from operations.models import Registration

    linked = Registration.objects.filter(
        organization=organization, line__invoice=invoice
    )
    if linked.exists():
        selected = (
            linked.filter(pk=getattr(registration, "pk", registration)).first()
            if registration is not None
            else None
        )
        if (
            not selected
            or selected.status not in ("attended", "completed")
            or quantity != 1
            or reference != f"registration:{selected.pk}"
        ):
            raise ValidationError(
                "Gunakan konfirmasi pada halaman peserta agar pelaksanaan tetap terhubung ke pendaftaran."
            )
    old = InvoiceDelivery.objects.filter(
        organization=organization, reference=reference
    ).first()
    if old:
        if (old.invoice_id, old.quantity, old.date, old.evidence) != (
            invoice.pk,
            quantity,
            date,
            evidence,
        ):
            raise ValidationError(
                "Referensi pelaksanaan sudah dipakai untuk data berbeda."
            )
        return old
    facts = fulfillment_summary(organization=organization, invoice=invoice)
    if (
        invoice.status != "issued"
        or date < invoice.date
        or quantity > facts["remaining_quantity"]
    ):
        raise ValidationError(
            "Pelaksanaan harus sesuai sisa unit pada invoice terbit dan tanggal invoice."
        )
    if invoice.unit_deliveries.filter(date__gt=date).exists():
        raise ValidationError(
            "Catat pelaksanaan berurutan; tanggal tidak boleh mendahului pelaksanaan yang sudah dicatat."
        )
    from .settlement import applied_payment_amounts

    paid = applied_payment_amounts(
        organization=organization, invoice_ids=[invoice.pk], as_of=date
    )[invoice.pk]
    dated_facts = fulfillment_summary(
        organization=organization, invoice=invoice, as_of=date
    )
    if invoice.unit_credits.filter(date__gt=date).exists():
        raise ValidationError(
            "Tanggal pelaksanaan tidak boleh mendahului koreksi yang sudah dicatat."
        )
    from operations.services import require_order_payment

    require_order_payment(organization=organization, invoice=invoice, as_of=date)
    if invoice.party.kind == "reseller" and paid < dated_facts["net_total"]:
        raise ValidationError(
            "Mitra harus melunasi pesanan sebelum tes. Selesaikan alokasi pembayaran."
        )
    amount = invoice.unit_price * quantity
    journal = ledger._post(
        organization,
        date,
        f"delivery-units:{reference}",
        f"Pelaksanaan {quantity} unit {invoice.number}",
        [("DEFERRED_REVENUE", amount, ZERO), ("REVENUE", ZERO, amount)],
    )
    result = ledger._event_row(
        InvoiceDelivery,
        organization=organization,
        invoice=invoice,
        quantity=quantity,
        amount=amount,
        date=date,
        reference=reference,
        evidence=evidence,
        journal=journal,
    )
    if facts["remaining_quantity"] == quantity:
        invoice.status, invoice.delivered_at, invoice.delivery_evidence = (
            "delivered",
            date,
            evidence,
        )
        invoice._service_transition = True
        invoice.save()
    record(
        organization,
        actor,
        "finance.invoice.units_delivered",
        obj=result,
        detail={
            "invoice_id": invoice.pk,
            "quantity": quantity,
            "amount": str(amount),
            "evidence": evidence,
        },
    )
    ledger._event(
        organization,
        "invoice.units_delivered",
        result,
        {
            "invoice_id": invoice.pk,
            "quantity": quantity,
            "amount": str(amount),
            "date": str(date),
        },
    )
    return result


@transaction.atomic
def credit_units(
    *,
    organization,
    actor,
    invoice,
    quantity,
    kind,
    date,
    reference,
    evidence,
    registration=None,
):
    organization = ledger._lock_org(organization)
    ledger._owner(organization, actor)
    invoice = ledger._scoped(Invoice, invoice, organization, lock=True)
    if invoice.tax_settlements.exists():
        raise ValidationError(
            "Invoice dengan bukti potong pelanggan memerlukan koreksi pajak terlebih dahulu; koreksi/refund otomatis belum mendukung kasus ini."
        )
    quantity, date = _quantity(quantity), ledger._actual_date(date)
    reference = ledger._review_text(reference, "Nomor koreksi", limit=120)
    evidence = ledger._review_text(evidence, "Alasan dan bukti persetujuan koreksi")
    if kind == "unfulfilled":
        from operations.models import Registration

        linked = Registration.objects.filter(
            organization=organization, line__invoice=invoice
        )
        if linked.exists():
            selected = (
                linked.filter(pk=getattr(registration, "pk", registration)).first()
                if registration is not None
                else None
            )
            if (
                not selected
                or selected.status == "completed"
                or quantity != 1
                or reference != f"registration-cancel:{selected.pk}"
            ):
                raise ValidationError(
                    "Batalkan dari halaman peserta agar keadaan pendaftaran dan koreksi tetap sesuai."
                )
    old = InvoiceCredit.objects.filter(
        organization=organization, reference=reference
    ).first()
    if old:
        if (old.invoice_id, old.quantity, old.kind, old.date, old.evidence) != (
            invoice.pk,
            quantity,
            kind,
            date,
            evidence,
        ):
            raise ValidationError("Nomor koreksi sudah dipakai untuk rincian berbeda.")
        return old
    facts = fulfillment_summary(organization=organization, invoice=invoice)
    if (
        invoice.status not in ("issued", "delivered")
        or kind not in ("unfulfilled", "service")
        or date < invoice.date
    ):
        raise ValidationError(
            "Koreksi memerlukan invoice terbit, jenis koreksi, dan tanggal yang sesuai."
        )
    if (
        invoice.unit_deliveries.filter(date__gt=date).exists()
        or invoice.unit_credits.filter(date__gt=date).exists()
        or invoice.allocations.filter(transaction__date__gt=date).exists()
        or invoice.advance_applications.filter(date__gt=date).exists()
        or invoice.delivered_at
        and date < invoice.delivered_at
    ):
        raise ValidationError(
            "Tanggal koreksi tidak boleh mendahului pembayaran, pelaksanaan, atau koreksi sebelumnya."
        )
    available = (
        facts["remaining_quantity"]
        if kind == "unfulfilled"
        else facts["delivered_quantity"] - facts["service_credit_quantity"]
    )
    if quantity > available:
        raise ValidationError("Jumlah koreksi melebihi unit yang dapat dikoreksi.")
    amount = invoice.unit_price * quantity
    unpaid = min(amount, max(ZERO, facts["net_total"] - invoice.paid_amount))
    refund = amount - unpaid
    entries = [
        ("DEFERRED_REVENUE" if kind == "unfulfilled" else "REVENUE", amount, ZERO)
    ]
    if unpaid:
        entries.append(("AR", ZERO, unpaid))
    if refund:
        entries.append(("CUSTOMER_REFUND", ZERO, refund))
    journal = ledger._post(
        organization,
        date,
        f"invoice-credit:{reference}",
        f"Koreksi {invoice.number}",
        entries,
    )
    obj = ledger._event_row(
        InvoiceCredit,
        organization=organization,
        invoice=invoice,
        kind=kind,
        quantity=quantity,
        amount=amount,
        unpaid_amount=unpaid,
        refund_amount=refund,
        date=date,
        reference=reference,
        evidence=evidence,
        journal=journal,
    )
    record(
        organization,
        actor,
        "finance.invoice.credited",
        obj=obj,
        detail={
            "invoice_id": invoice.pk,
            "kind": kind,
            "quantity": quantity,
            "amount": str(amount),
            "refund_liability": str(refund),
        },
    )
    ledger._event(
        organization,
        "invoice.credited",
        obj,
        {"invoice_id": invoice.pk, "amount": str(amount), "date": str(date)},
    )
    return obj


@transaction.atomic
def refund_credit(
    *, organization, actor, credit, transaction, amount, reference, evidence
):
    organization = ledger._lock_org(organization)
    ledger._write_actor(organization, actor)
    credit = ledger._scoped(InvoiceCredit, credit, organization, lock=True)
    bank_line = ledger._scoped(
        ledger.BankTransaction, transaction, organization, lock=True
    )
    amount = ledger._amount(amount)
    reference = ledger._review_text(reference, "Referensi refund", limit=120)
    evidence = ledger._review_text(evidence, "Bukti refund")
    old = CreditRefund.objects.filter(
        organization=organization, reference=reference
    ).first()
    if old:
        if (
            old.credit_id,
            old.bank_posting.transaction_id,
            old.amount,
            old.evidence,
        ) != (credit.pk, bank_line.pk, amount, evidence):
            raise ValidationError(
                "Referensi refund sudah dipakai untuk rincian berbeda."
            )
        return old
    if bank_line.date < credit.date or amount > credit.refund_remaining:
        raise ValidationError(
            "Tanggal atau jumlah refund tidak sesuai sisa kewajiban koreksi."
        )
    posting = ledger._bank_post(
        organization,
        bank_line,
        -amount,
        "customer_refund",
        reference,
        evidence,
        [("CUSTOMER_REFUND", amount, ZERO), ("BANK", ZERO, amount)],
        actor,
    )
    obj = ledger._event_row(
        CreditRefund,
        organization=organization,
        credit=credit,
        bank_posting=posting,
        journal=posting.journal,
        amount=amount,
        date=bank_line.date,
        reference=reference,
        evidence=evidence,
    )
    record(
        organization,
        actor,
        "finance.credit.refunded",
        obj=obj,
        detail={"credit_id": credit.pk, "amount": str(amount)},
    )
    ledger._event(
        organization,
        "invoice.refunded",
        obj,
        {
            "invoice_id": credit.invoice_id,
            "amount": str(amount),
            "date": str(bank_line.date),
        },
    )
    return obj
