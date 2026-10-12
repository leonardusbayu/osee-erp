"""Supplier advance staff screens; public Finance services own every decision."""

from datetime import date
from decimal import Decimal
from uuid import uuid4

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path, reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods

from core.access import organization_required, require_role
from finance import supplier_advances as workflow
from finance.models import (
    BankPosting,
    Bill,
    Party,
    SupplierAdvance,
    SupplierAdvanceApplication,
    SupplierAdvanceApplicationReversal,
)

from .supplier_advance_forms import (
    SupplierAdvanceApplyForm,
    SupplierAdvanceRecordForm,
    SupplierAdvanceRefundForm,
    SupplierAdvanceReverseForm,
    selected_id,
)

READ = ("owner", "finance", "reviewer", "auditor", "director")
WRITE = ("owner", "finance")
TAX_LABELS = {
    "pending": "Pemeriksaan pajak masih diperlukan",
    "reviewed": "Pemeriksaan pajak sudah tercatat",
    "held": "Pemeriksaan pajak perlu ditindaklanjuti",
    "stale": "Pemeriksaan pajak perlu diperiksa kembali",
}
ACTION_LABELS = {
    "record": "Uang muka dicatat",
    "apply": "Dipakai untuk tagihan",
    "refund": "Dikembalikan pemasok",
    "reverse": "Penerapan dibalik",
}
SPECS = {
    "record": (
        SupplierAdvanceRecordForm,
        "Catat uang muka pemasok",
        "Pilih uang keluar yang sudah terjadi sebelum tagihan pemasok tersedia. Uang ini disimpan sebagai uang muka sampai dipakai untuk tagihan atau dikembalikan.",
        "record_supplier_advance",
        "Uang muka pemasok tersimpan.",
    ),
    "apply": (
        SupplierAdvanceApplyForm,
        "Terapkan ke tagihan",
        "Gunakan uang muka yang sudah dibayar untuk mengurangi sisa tagihan pemasok ini. Tidak ada uang bank baru.",
        "apply_supplier_advance",
        "Uang muka sudah digunakan untuk tagihan.",
    ),
    "refund": (
        SupplierAdvanceRefundForm,
        "Cocokkan pengembalian pemasok",
        "Pilih uang masuk yang benar-benar dikembalikan pemasok. Hanya sisa uang muka yang belum digunakan dapat dikembalikan.",
        "refund_supplier_advance",
        "Pengembalian pemasok sudah dicocokkan.",
    ),
    "reverse": (
        SupplierAdvanceReverseForm,
        "Balik penerapan",
        "Kembalikan seluruh jumlah penerapan ini ke uang muka. Sisa tagihan akan bertambah kembali. Tidak ada perubahan uang bank.",
        "reverse_supplier_advance_application",
        "Penerapan sudah dibalik. Catatan asli tetap ada di riwayat.",
    ),
}


def _advance(request, pk):
    return get_object_or_404(SupplierAdvance, organization=request.organization, pk=pk)


def _date(value):
    try:
        return date.fromisoformat(value) if isinstance(value, str) else value
    except ValueError:
        return None


def _position(request, advance):
    position = dict(
        workflow.supplier_advance_position(
            organization=request.organization,
            advance=advance,
            as_of=timezone.localdate(),
        )
    )
    position["tax_label"] = TAX_LABELS.get(
        position["tax_review_status"], "Pemeriksaan pajak perlu ditindaklanjuti"
    )
    position["has_remaining"] = bool(
        position["current"]
        and position["remaining"] is not None
        and Decimal(position["remaining"]) > 0
    )
    position["exhausted"] = bool(
        position["current"]
        and position["remaining"] is not None
        and Decimal(position["remaining"]) == 0
    )
    position["history"] = [
        dict(
            item,
            date=_date(item["date"]),
            label=ACTION_LABELS.get(item["kind"], "Catatan uang muka"),
        )
        for item in position["history"]
    ]
    return position


def _source(request, advance, position):
    if not position["current"]:
        return None, None
    supplier = Party.objects.filter(
        organization=request.organization, kind="supplier", pk=advance.supplier_id
    ).first()
    posting = (
        BankPosting.objects.filter(
            organization=request.organization,
            pk=advance.bank_posting_id,
            transaction__organization=request.organization,
            transaction__account__organization=request.organization,
        )
        .select_related("transaction__account")
        .first()
    )
    return supplier, posting.transaction if posting else None


def _common(request, **context):
    return {
        "active_nav": "bank",
        "can_write": request.membership.role in WRITE,
        "can_reverse": request.membership.role == "owner",
        **context,
    }


@require_GET
@organization_required
def supplier_advances(request):
    require_role(request, *READ)
    rows = SupplierAdvance.objects.filter(organization=request.organization).order_by(
        "-date", "-pk"
    )
    supplier_id = selected_id(request.GET, "supplier")
    supplier = (
        Party.objects.filter(
            organization=request.organization, kind="supplier", pk=supplier_id
        ).first()
        if supplier_id
        else None
    )
    if supplier:
        rows = rows.filter(supplier=supplier)
    page = Paginator(rows, 50).get_page(request.GET.get("page"))
    items = []
    for advance in page.object_list:
        position = _position(request, advance)
        source_supplier, _ = _source(request, advance, position)
        items.append(
            {"advance": advance, "position": position, "supplier": source_supplier}
        )
    return render(
        request,
        "app/supplier_advance_list.html",
        _common(request, items=items, page_obj=page, selected_supplier=supplier),
    )


@require_GET
@organization_required
def supplier_advance_detail(request, pk):
    require_role(request, *READ)
    advance = _advance(request, pk)
    position = _position(request, advance)
    supplier, bank = _source(request, advance, position)
    applications = []
    if position["current"]:
        for item in (
            SupplierAdvanceApplication.objects.filter(
                organization=request.organization,
                advance=advance,
                bill__organization=request.organization,
                bill__supplier__organization=request.organization,
            )
            .select_related("bill")
            .order_by("date", "pk")
        ):
            applications.append(
                {
                    "item": item,
                    "reversed": SupplierAdvanceApplicationReversal.objects.filter(
                        organization=request.organization, application=item
                    ).exists(),
                }
            )
    return render(
        request,
        "app/supplier_advance_detail.html",
        _common(
            request,
            advance=advance,
            position=position,
            supplier=supplier,
            bank=bank,
            applications=applications,
        ),
    )


def _errors(form, error):
    if hasattr(error, "message_dict"):
        for name, items in error.message_dict.items():
            for message in items:
                form.add_error(name if name in form.fields else None, message)
    else:
        for message in error.messages:
            form.add_error(None, message)


def _action_context(request, action, *, advance, application, values):
    sources = {
        key: values[key]
        for key in ("supplier", "transaction", "bill", "date", "amount")
        if key in values
    }
    return workflow.supplier_advance_action_context(
        organization=request.organization,
        action=action,
        advance=advance,
        application=application,
        **sources,
    )


def _preview(action, context, values):
    """Display arithmetic only from the public, assessable source capacities."""
    if not context["current"]:
        return None
    before, position = context["before"], context["position"]
    amount = values.get("amount")
    preview = {"amount": amount, "suggested": None}
    if action == "record" and "transaction" in before:
        remaining = Decimal(before["transaction"]["remaining"])
        if remaining <= 0:
            preview.update(
                suggested=-remaining,
                bank_after=remaining + amount if amount is not None else None,
            )
    elif (
        action in ("apply", "refund") and position and position["remaining"] is not None
    ):
        remaining = Decimal(position["remaining"])
        if action == "apply" and "bill" in before:
            outstanding = Decimal(before["bill"]["outstanding"])
            preview.update(
                suggested=min(remaining, outstanding),
                bill_after=outstanding - amount if amount is not None else None,
            )
        elif action == "refund" and "transaction" in before:
            bank_remaining = Decimal(before["transaction"]["remaining"])
            if bank_remaining >= 0:
                preview.update(
                    suggested=min(remaining, bank_remaining),
                    bank_after=bank_remaining - amount if amount is not None else None,
                )
        preview["remaining_after"] = remaining - amount if amount is not None else None
    elif (
        action == "reverse"
        and position
        and "application" in before
        and "bill" in before
    ):
        amount = Decimal(before["application"]["amount"])
        preview.update(
            amount=amount,
            remaining_after=Decimal(position["remaining"]) + amount,
            bill_after=Decimal(before["bill"]["outstanding"]) + amount,
        )
    return preview


@require_http_methods(["GET", "POST"])
@organization_required
def supplier_advance_action(request, action, pk=None):
    require_role(request, "owner", *(() if action == "reverse" else ("finance",)))
    form_class, title, description, command_name, success = SPECS[action]
    advance = application = None
    if action == "reverse":
        application = get_object_or_404(
            SupplierAdvanceApplication, organization=request.organization, pk=pk
        )
        advance = _advance(request, application.advance_id)
    elif action != "record":
        advance = _advance(request, pk)
    intent = request.POST.get("intent", "") if request.method == "POST" else ""
    checking = intent == "check"
    data = request.POST.copy() if request.method == "POST" else None
    initial = {}
    if data is not None and checking:
        # A new check keeps staff inputs but discards the prior approval.
        for field in ("request_key", "expected_fingerprint", "confirmed"):
            if len(data.getlist(field)) <= 1:
                data.pop(field, None)
    if action == "apply" and request.method == "GET":
        bill_id = selected_id(request.GET, "bill")
        if Bill.objects.filter(
            organization=request.organization,
            supplier__organization=request.organization,
            supplier_id=advance.supplier_id,
            pk=bill_id,
            status="approved",
            tax_status="reviewed",
            tax_amount=0,
        ).exists():
            initial["bill"] = bill_id
    form_kwargs = {
        "organization": request.organization,
        "checking": checking,
        "initial": initial,
    }
    if action == "apply":
        form_kwargs["advance"] = advance
    form = form_class(data, **form_kwargs)
    valid = form.is_valid() if request.method == "POST" else False
    if request.method == "POST" and intent not in ("check", "save"):
        form.add_error(None, "Pilih Periksa sumber atau Konfirmasi dan simpan.")
        valid = False
    values = dict(form.cleaned_data) if form.is_bound else {}
    context = _action_context(
        request, action, advance=advance, application=application, values=values
    )
    if request.method == "POST" and intent == "save" and valid:
        command_values = dict(form.cleaned_data)
        if advance is not None and action != "reverse":
            command_values["advance"] = advance
        if application is not None:
            command_values["application"] = application
        try:
            result = getattr(workflow, command_name)(
                organization=request.organization, actor=request.user, **command_values
            )
        except ValidationError as error:
            _errors(form, error)
            form.add_error(
                None,
                "Isian Anda tetap tersimpan di formulir. Tekan Periksa sumber, lalu periksa dan konfirmasi lagi.",
            )
        else:
            messages.success(request, success)
            return redirect(
                "supplier_advance_detail",
                pk=result.pk if action == "record" else advance.pk,
            )
    preview = _preview(action, context, values)
    if checking and valid:
        fresh_data = form.data.copy()
        fresh_data["request_key"] = str(uuid4())
        fresh_data["expected_fingerprint"] = context["fingerprint"]
        fresh_data.pop("confirmed", None)
        if (
            preview
            and action != "reverse"
            and values.get("amount") is None
            and preview["suggested"] is not None
            and preview["suggested"] > 0
        ):
            fresh_data["amount"] = str(preview["suggested"])
            values["amount"] = preview["suggested"]
            context = _action_context(
                request, action, advance=advance, application=application, values=values
            )
            fresh_data["expected_fingerprint"] = context["fingerprint"]
            preview = _preview(action, context, values)
        form = form_class(fresh_data, **form_kwargs)
        # Validate before adding a capacity error, with confirmation optional for this check.
        form.is_valid()
        if (
            preview
            and action != "reverse"
            and preview["suggested"] is not None
            and values.get("amount") is not None
            and values["amount"] > preview["suggested"]
        ):
            form.add_error(
                "amount",
                "Jumlah melebihi sisa yang tersedia. Sesuaikan jumlah, lalu Periksa sumber kembali.",
            )
        if (
            preview
            and action != "reverse"
            and preview["suggested"] is not None
            and preview["suggested"] <= 0
        ):
            form.add_error(
                None,
                "Sisa yang tersedia sudah habis. Pilih mutasi atau tagihan lain, lalu Periksa sumber kembali.",
            )
        messages.info(
            request,
            "Sumber terbaru diperiksa. Isian Anda dipertahankan; periksa ringkasan lalu konfirmasi kembali.",
        )
    save_available = bool(
        checking
        and valid
        and not form.errors
        and context["current"]
        and not context["blockers"]
    )
    back_url = (
        reverse("supplier_advance_detail", args=[advance.pk])
        if advance
        else reverse("supplier_advances")
    )
    selected_bill = values.get("bill")
    bill_review_url = (
        reverse("taxes:bill_review", args=[selected_bill.pk]) if selected_bill else ""
    )
    bill_tax_blocker = (
        action == "apply"
        and selected_bill is not None
        and (
            selected_bill.tax_status != "reviewed"
            or selected_bill.tax_amount is None
            or selected_bill.tax_amount != 0
        )
    )
    request_error = request.method == "POST" and (
        any(len(request.POST.getlist(name)) > 1 for name in (*form.fields, "intent"))
        or bool(
            set(request.POST) - set(form.fields) - {"intent", "csrfmiddlewaretoken"}
        )
    )
    return render(
        request,
        "app/supplier_advance_form.html",
        _common(
            request,
            form=form,
            title=title,
            description=description,
            action=action,
            advance=advance,
            application=application,
            source_context=context,
            preview=preview,
            save_available=save_available,
            checked=checking and valid,
            bill_review_url=bill_review_url,
            bill_tax_blocker=bill_tax_blocker,
            missing_bills=action == "apply"
            and not form.fields["bill"].queryset.exists(),
            cancel_url=back_url,
            hidden_errors=[
                error for field in form.hidden_fields() for error in field.errors
            ],
        ),
        status=400
        if request_error or (request.method == "POST" and intent != "check")
        else 200,
    )


supplier_advance_urls = [
    path("finance/supplier-advances/", supplier_advances, name="supplier_advances"),
    path(
        "finance/supplier-advances/new/",
        supplier_advance_action,
        {"action": "record"},
        name="supplier_advance_new",
    ),
    path(
        "finance/supplier-advances/<int:pk>/",
        supplier_advance_detail,
        name="supplier_advance_detail",
    ),
    path(
        "finance/supplier-advances/<int:pk>/apply/",
        supplier_advance_action,
        {"action": "apply"},
        name="supplier_advance_apply",
    ),
    path(
        "finance/supplier-advances/<int:pk>/refund/",
        supplier_advance_action,
        {"action": "refund"},
        name="supplier_advance_refund",
    ),
    path(
        "finance/supplier-advance-applications/<int:pk>/reverse/",
        supplier_advance_action,
        {"action": "reverse"},
        name="supplier_advance_application_reverse",
    ),
]
