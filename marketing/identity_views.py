"""Read-only source context and explicit Finance/Owner recovery commands."""

from uuid import uuid4

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from core.access import organization_required, require_role

from .identity_forms import InvoiceIdentityRecoveryForm
from .identity_recovery import identity_recovery_context, recover_invoice_identity
from .models import Lead
from .views import READ, _context, _form_error

IDENTITY_WRITERS = ("owner", "finance")


@require_http_methods(["GET", "POST"])
@organization_required
def invoice_identity_correction(request, pk):
    require_role(request, *READ)
    writable = request.membership.role in IDENTITY_WRITERS
    if request.method == "POST":
        require_role(request, *IDENTITY_WRITERS)
    lead = get_object_or_404(
        Lead.objects.select_related("party", "product", "invoice", "campaign"),
        organization=request.organization,
        pk=pk,
    )
    recovery = identity_recovery_context(organization=request.organization, lead=lead)
    lead = recovery["lead"]
    controls = {
        "expected_version": recovery["version"],
        "expected_fingerprint": recovery["fingerprint"],
        "request_key": str(uuid4()),
        "action": "replace",
    }
    data = request.POST if request.method == "POST" else None
    rechecked = False
    if data is not None and data.get("operation") == "recheck":
        data = data.copy()
        if not any(
            len(data.getlist(name)) > 1
            for name in (*InvoiceIdentityRecoveryForm.base_fields, "operation")
        ):
            for name in ("expected_version", "expected_fingerprint"):
                data[name] = controls[name]
            data["request_key"] = str(uuid4())
            for name in ("confirm_identity", "confirm_legacy_source"):
                data.pop(name, None)
            rechecked = True
    form = (
        InvoiceIdentityRecoveryForm(
            data, organization=request.organization, recovery=recovery, initial=controls
        )
        if writable
        else None
    )
    if request.method == "POST" and not rechecked and form.is_valid():
        try:
            decision = recover_invoice_identity(
                organization=request.organization,
                actor=request.user,
                lead=lead,
                **form.cleaned_data,
            )
        except ValidationError as exc:
            _form_error(form, exc)
            lead.refresh_from_db()
            recovery = identity_recovery_context(
                organization=request.organization, lead=lead
            )
            lead = recovery["lead"]
        else:
            messages.success(
                request,
                "Perbaikan tersimpan. Pelanggan dan produk kini mengikuti invoice yang diperiksa."
                if decision.kind == "replace"
                else "Perbaikan tersimpan. Data dari invoice yang keliru kembali belum diketahui; riwayat tetap tersedia.",
            )
            return redirect("marketing:invoice_identity_correction", pk=lead.pk)
    stale = bool(
        data is not None
        and (
            str(data.get("expected_version", "")) != str(recovery["version"])
            or data.get("expected_fingerprint") != recovery["fingerprint"]
        )
    )
    origins = []
    source_origins = recovery.get("origins")
    if not isinstance(source_origins, dict):
        source_origins = {}
    for field, label in (("party", "Pelanggan"), ("product", "Produk")):
        source = source_origins.get(field)
        if not isinstance(source, dict):
            source = {}
        origins.append(
            {
                "label": label,
                "value": getattr(lead, field),
                "source_invoice_id": source.get("source_invoice_id"),
                "eligible": field in recovery["eligible_fields"],
                "kind": source.get("kind", "unknown"),
            }
        )
    response = render(
        request,
        "marketing/identity_correction.html",
        _context(
            request,
            "leads",
            lead=lead,
            recovery=recovery,
            identity_origins=origins,
            form=form,
            can_write_identity=writable,
            legacy_history=legacy_review_rows(
                organization=request.organization, recovery=recovery
            ),
            stale=stale,
            rechecked=rechecked,
        ),
    )
    if request.method == "POST" and not rechecked:
        response.status_code = 400
    return response


def legacy_review_rows(*, organization, recovery):
    """Human-readable, scoped before/after facts for explicit historical review."""
    from finance.models import Invoice, Party, Product

    # Invalid/ambiguous evidence is a visible blocker, never a fabricated null
    # mapping. The source parser remains the authority for displayable facts.
    events = recovery.get("legacy_events", []) if not recovery.get("blockers") else []
    parties = (
        {row.pk: row for row in Party.objects.filter(organization=organization)}
        if events
        else {}
    )
    products = (
        {row.pk: row for row in Product.objects.filter(organization=organization)}
        if events
        else {}
    )
    invoices = (
        {row.pk: row for row in Invoice.objects.filter(organization=organization)}
        if events
        else {}
    )
    rows = []
    for event in events:
        detail = event.detail
        if event.action == "marketing.lead.created":
            rows.append(
                {
                    "title": "Data saat prospek dibuat",
                    "event": event,
                    "after_party": parties.get(detail["values"]["party"]),
                    "after_product": products.get(detail["values"]["product"]),
                    "creation": True,
                }
            )
        elif event.action == "marketing.lead.invoice_linked":
            rows.append(
                {
                    "title": "Invoice dikonfirmasi",
                    "event": event,
                    "before_party": parties.get(detail["before_mapping"]["party_id"]),
                    "before_product": products.get(
                        detail["before_mapping"]["product_id"]
                    ),
                    "after_party": parties.get(detail["after_mapping"]["party_id"]),
                    "after_product": products.get(
                        detail["after_mapping"]["product_id"]
                    ),
                    "invoice": invoices.get(detail["invoice_id"]),
                    "confirmed": detail["identity_confirmed"],
                }
            )
        else:
            rows.append(
                {
                    "title": "Hubungan invoice dikoreksi",
                    "event": event,
                    "before_invoice": invoices.get(detail["before_invoice_id"]),
                    "after_invoice": invoices.get(detail["after_invoice_id"]),
                    "attribution": True,
                }
            )
    return rows
