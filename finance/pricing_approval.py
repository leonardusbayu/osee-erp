"""Owner approvals bind one exceptional quote to one exact invoice."""

from django.core.exceptions import ValidationError
from django.db import transaction

from core.audit import record

from . import services as ledger
from .costing import _hash, _save
from .costing_models import (
    PriceOverrideDecision,
    PriceOverrideRequest,
    PriceOverrideUse,
)
from .fulfillment import _quantity
from .models import Invoice, Party, Product


@transaction.atomic
def request_price_override(
    *, organization, actor, party, product, quantity, date, unit_price, reason, evidence
):
    org = ledger._lock_org(organization)
    ledger._write_actor(org, actor)
    party, product = (
        ledger._scoped(Party, party, org),
        ledger._scoped(Product, product, org),
    )
    if party.kind == "supplier":
        raise ValidationError("Permintaan harga memerlukan pelanggan atau mitra.")
    quantity, date, amount = (
        _quantity(quantity),
        ledger._date(date),
        ledger._amount(unit_price),
    )
    ledger._period(org, date)
    reason, evidence = (
        ledger._review_text(reason, "Alasan harga khusus"),
        ledger._review_text(evidence, "Bukti penawaran"),
    )
    baseline, version = ledger._resolve_price(org, party, product, date)
    if baseline == amount:
        raise ValidationError(
            "Harga sama dengan daftar aktif; gunakan pembuatan invoice biasa."
        )
    snapshot = {
        "organization_id": org.pk,
        "party_id": party.pk,
        "product_id": product.pk,
        "quantity": quantity,
        "date": str(date),
        "unit_price": str(amount),
        "baseline_price": str(baseline),
        "price_version_id": version.pk if version else None,
        "reason": reason,
        "evidence": evidence,
    }
    row = _save(
        PriceOverrideRequest,
        organization=org,
        party=party,
        product=product,
        quantity=quantity,
        date=date,
        unit_price=amount,
        baseline_price=baseline,
        price_version=version,
        reason=reason,
        evidence=evidence,
        created_by=actor,
        fingerprint=_hash(snapshot),
    )
    record(org, actor, "finance.price_override.requested", obj=row, detail=snapshot)
    return row


def _current_baseline(org, request):
    party = ledger._scoped(Party, request.party_id, org)
    product = ledger._scoped(Product, request.product_id, org)
    baseline, version = ledger._resolve_price(org, party, product, request.date)
    if (baseline, version.pk if version else None) != (
        request.baseline_price,
        request.price_version_id,
    ):
        raise ValidationError(
            "Dasar harga berubah. Buat permintaan harga dan draf invoice baru untuk ditinjau Direktur."
        )


@transaction.atomic
def decide_price_override(
    *, organization, actor, request, expected_fingerprint, decision, reason, evidence
):
    org = ledger._lock_org(organization)
    ledger._owner(org, actor)
    request = ledger._scoped(PriceOverrideRequest, request, org, lock=True)
    if request.fingerprint != expected_fingerprint or hasattr(
        request, "decision_record"
    ):
        raise ValidationError(
            "Permintaan sudah diputuskan atau dasar pemeriksaan berubah. Muat ulang halaman."
        )
    if decision not in {"approved", "rejected"}:
        raise ValidationError("Pilih setuju atau tolak.")
    reason, evidence = (
        ledger._review_text(reason, "Alasan keputusan"),
        ledger._review_text(evidence, "Bukti pemeriksaan harga"),
    )
    if decision == "approved":
        _current_baseline(org, request)
        ledger._period(org, request.date)
    result = _save(
        PriceOverrideDecision,
        organization=org,
        request=request,
        decision=decision,
        reason=reason,
        evidence=evidence,
        created_by=actor,
    )
    record(
        org,
        actor,
        f"finance.price_override.{decision}",
        obj=result,
        detail={"request_id": request.pk, "fingerprint": request.fingerprint},
    )
    return result


def validate_price_override(
    *, organization, request, party, product, quantity, date, unit_price, invoice=None
):
    request = ledger._scoped(PriceOverrideRequest, request, organization, lock=True)
    if (
        not hasattr(request, "decision_record")
        or request.decision_record.decision != "approved"
    ):
        raise ValidationError(
            "Harga khusus memerlukan permintaan yang disetujui Owner/Direktur."
        )
    if (
        request.party_id,
        request.product_id,
        request.quantity,
        request.date,
        request.unit_price,
    ) != (party.pk, product.pk, int(quantity), date, unit_price):
        raise ValidationError(
            "Persetujuan harga tidak sesuai pelanggan, produk, jumlah, tanggal, atau harga invoice."
        )
    if hasattr(request, "use") and request.use.invoice_id != getattr(
        invoice, "pk", None
    ):
        raise ValidationError("Persetujuan harga sudah digunakan pada invoice lain.")
    _current_baseline(organization, request)
    return request


@transaction.atomic
def bind_price_override(*, organization, actor, invoice, request):
    org = ledger._lock_org(organization)
    ledger._write_actor(org, actor)
    invoice = ledger._scoped(Invoice, invoice, org, lock=True)
    if invoice.status != "draft":
        raise ValidationError(
            "Persetujuan harga hanya dapat dipakai pada draf invoice."
        )
    request = validate_price_override(
        organization=org,
        request=request,
        invoice=invoice,
        party=invoice.party,
        product=invoice.product,
        quantity=invoice.quantity,
        date=invoice.date,
        unit_price=invoice.unit_price,
    )
    if hasattr(invoice, "price_override_use"):
        if invoice.price_override_use.request_id != request.pk:
            raise ValidationError("Draf ini telah menggunakan persetujuan harga lain.")
        return invoice.price_override_use
    row = _save(
        PriceOverrideUse,
        organization=org,
        request=request,
        invoice=invoice,
        created_by=actor,
        reason=request.reason,
        evidence=request.evidence,
    )
    record(
        org,
        actor,
        "finance.price_override.used",
        obj=row,
        detail={
            "invoice_id": invoice.pk,
            "request_id": request.pk,
            "fingerprint": request.fingerprint,
        },
    )
    return row


def validate_invoice_price(*, organization, invoice):
    if hasattr(invoice, "price_override_use"):
        validate_price_override(
            organization=organization,
            request=invoice.price_override_use.request,
            invoice=invoice,
            party=invoice.party,
            product=invoice.product,
            quantity=invoice.quantity,
            date=invoice.date,
            unit_price=invoice.unit_price,
        )
        return
    baseline, version = ledger._resolve_price(
        organization, invoice.party, invoice.product, invoice.date
    )
    if invoice.unit_price != baseline:
        raise ValidationError(
            "Harga draf berbeda dari harga aktif. Hubungkan persetujuan Owner/Direktur sebelum menerbitkan."
        )
