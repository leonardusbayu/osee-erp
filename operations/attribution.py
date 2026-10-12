"""Single invoice attribution map shared by operational and KPI consumers."""

import hashlib
import json
from collections import defaultdict

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from core.audit import record
from finance import services as finance
from finance.models import Invoice
from marketing.models import Lead

from .models import OrderLine
from .services import _finance_org, _text, _version


def invoice_attribution(*, organization):
    assignments = defaultdict(set)
    basis = []
    for lead_id, version, invoice_id in (
        Lead.objects.filter(organization=organization)
        .exclude(invoice=None)
        .values_list("pk", "version", "invoice_id")
    ):
        assignments[invoice_id].add(lead_id)
        basis.append(["legacy", lead_id, version, invoice_id])
    for invoice_id, lead_id, order_id, version in (
        OrderLine.objects.filter(
            organization=organization,
            order__organization=organization,
            invoice__organization=organization,
            order__lead__organization=organization,
        )
        .exclude(invoice=None)
        .exclude(order__lead=None)
        .values_list("invoice_id", "order__lead_id", "order_id", "order__version")
    ):
        assignments[invoice_id].add(lead_id)
        basis.append(["order", lead_id, order_id, version, invoice_id])
    by_invoice = {
        pk: next(iter(leads)) for pk, leads in assignments.items() if len(leads) == 1
    }
    by_lead = defaultdict(list)
    for invoice_id, lead_id in sorted(by_invoice.items()):
        by_lead[lead_id].append(invoice_id)
    from marketing.identity_recovery import identity_lineage_fingerprint

    basis.append(
        ["identity_lineage", identity_lineage_fingerprint(organization=organization)]
    )
    return {
        "by_invoice": by_invoice,
        "by_lead": dict(by_lead),
        "ambiguous_invoice_ids": sorted(
            pk for pk, leads in assignments.items() if len(leads) != 1
        ),
        "fingerprint": hashlib.sha256(
            json.dumps(sorted(basis, key=str), separators=(",", ":")).encode()
        ).hexdigest(),
    }


@transaction.atomic
def correct_lead_invoice(
    *,
    organization,
    actor,
    lead,
    invoice,
    expected_version,
    reason,
    confirm_legacy_source=False,
    expected_fingerprint=None,
    evidence="",
):
    org = _finance_org(organization, actor)
    lead = finance._scoped(Lead, lead, org, lock=True)
    _version(lead, expected_version)
    reason = _text(reason, "Alasan koreksi atribusi", 2000)
    invoice = finance._scoped(Invoice, invoice, org) if invoice is not None else None
    if invoice:
        if invoice.status not in (
            "issued",
            "delivered",
        ) or invoice.date < timezone.localdate(lead.received_at):
            raise ValidationError("Invoice dan tanggal tidak sesuai peluang pembelian.")
        if lead.party_id != invoice.party_id or lead.product_id != invoice.product_id:
            raise ValidationError(
                "Koreksi tautan memerlukan identitas pembeli dan produk yang sama. Jangan mengubah asal prospek."
            )
        if (
            Lead.objects.filter(organization=org, invoice=invoice)
            .exclude(pk=lead.pk)
            .exists()
        ):
            raise ValidationError("Invoice sudah terhubung ke prospek lain.")
        attributed_order = (
            OrderLine.objects.filter(organization=org, invoice=invoice)
            .exclude(order__lead=None)
            .exclude(order__lead=lead)
            .exists()
        )
        if attributed_order:
            raise ValidationError("Invoice terhubung ke peluang lain melalui pesanan.")
    from marketing.identity_recovery import (
        prepare_identity_transition,
        record_identity_transition,
    )

    prepared = prepare_identity_transition(
        organization=org,
        actor=actor,
        lead=lead,
        confirm_legacy_source=confirm_legacy_source,
        expected_fingerprint=expected_fingerprint,
        reason=reason,
        evidence=evidence,
    )
    before = lead.invoice_id
    lead.invoice, lead.version = invoice, lead.version + 1
    lead._invoice_link = lead._service_write = True
    try:
        lead.save()
    finally:
        lead.__dict__.pop("_invoice_link", None)
        lead.__dict__.pop("_service_write", None)
    record_identity_transition(
        organization=org,
        actor=actor,
        lead=lead,
        prepared=prepared,
        kind="attribution",
        invoice=invoice,
        reason=reason,
        evidence=evidence,
    )
    record(
        org,
        actor,
        "marketing.lead.invoice_corrected",
        obj=lead,
        detail={
            "before_invoice_id": before,
            "after_invoice_id": lead.invoice_id,
            "reason": reason,
            "version": lead.version,
        },
    )
    return lead
