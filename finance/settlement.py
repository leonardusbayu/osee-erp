"""Scoped invoice settlement facts, including advances and dated cancellations.

These read-only facts are not bank cash flow: applied payments retain their gross
history after cancellation/refund. Consumers must not infer payment from a zero
collectible balance. Historical cutoffs use document, bank and application dates.
"""
from datetime import date, datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models import Sum
from django.utils import timezone

from .models import Allocation, AdvanceApplication, Invoice, InvoiceCancellation

ZERO = Decimal("0.00")


def _facts(organization, invoice_ids, as_of):
    organization_id = getattr(organization, "pk", organization)
    if isinstance(organization_id, bool) or not isinstance(organization_id, int) or organization_id <= 0:
        raise ValidationError("Organisasi tidak valid.")
    if as_of is None:
        as_of = timezone.localdate()
    if isinstance(as_of, str):
        try:
            as_of = date.fromisoformat(as_of)
        except ValueError as exc:
            raise ValidationError("Tanggal batas tidak valid.") from exc
    if not isinstance(as_of, date) or isinstance(as_of, datetime):
        raise ValidationError("Tanggal batas harus berupa tanggal kalender.")
    try:
        ids = list(invoice_ids)
    except TypeError as exc:
        raise ValidationError("Daftar invoice tidak valid.") from exc
    if any(isinstance(pk, bool) or not isinstance(pk, int) or pk <= 0 for pk in ids):
        raise ValidationError("Daftar invoice tidak valid.")
    ids = list(dict.fromkeys(ids))
    # Bound SQL parameters, not company size. Input is an internal scoped list.
    if len(ids) > 500:
        paid, collectible = {}, {}
        for start in range(0, len(ids), 500):
            chunk_paid, chunk_collectible = _facts(organization_id, ids[start:start + 500], as_of)
            paid.update(chunk_paid)
            collectible.update(chunk_collectible)
        return paid, collectible
    invoices = list(Invoice.objects.filter(organization_id=organization_id, pk__in=ids))
    scoped_ids = [invoice.pk for invoice in invoices]
    receipts = dict(Allocation.objects.filter(organization_id=organization_id,
        invoice__organization_id=organization_id, invoice_id__in=scoped_ids,
        transaction__organization_id=organization_id, transaction__date__lte=as_of)
        .values("invoice_id").annotate(value=Sum("amount")).values_list("invoice_id", "value"))
    advances = dict(AdvanceApplication.objects.filter(organization_id=organization_id,
        invoice__organization_id=organization_id, invoice_id__in=scoped_ids,
        advance__organization_id=organization_id, date__lte=as_of)
        .values("invoice_id").annotate(value=Sum("amount")).values_list("invoice_id", "value"))
    cancellations = dict(InvoiceCancellation.objects.filter(organization_id=organization_id,
        invoice_id__in=scoped_ids).values_list("invoice_id", "date"))
    paid, collectible = {}, {}
    for invoice in invoices:
        active_at_cutoff = invoice.date <= as_of and invoice.status in ("issued", "delivered", "cancelled")
        # A cancelled draft has never created AR and has no posted cancellation.
        if invoice.status == "cancelled" and invoice.pk not in cancellations:
            active_at_cutoff = False
        applied = receipts.get(invoice.pk, ZERO) + advances.get(invoice.pk, ZERO) if active_at_cutoff else ZERO
        paid[invoice.pk] = applied
        cancelled_at_cutoff = cancellations.get(invoice.pk) is not None and cancellations[invoice.pk] <= as_of
        collectible[invoice.pk] = max(ZERO, invoice.total - applied) if active_at_cutoff and not cancelled_at_cutoff else ZERO
    return paid, collectible


def collectible_amounts(*, organization, invoice_ids, as_of=None):
    """Amounts still collectible at a calendar cutoff; foreign IDs are omitted.

    None means today's local date. Issued/delivered invoices are reduced by dated
    receipts and applied advances. Cancellation is effective on its recorded
    date; historical balances before cancellation are preserved. Drafts are zero.
    """
    return _facts(organization, invoice_ids, as_of)[1]


def applied_payment_amounts(*, organization, invoice_ids, as_of=None):
    """Gross receipts plus applied advances through cutoff, not refund-net cash."""
    return _facts(organization, invoice_ids, as_of)[0]
