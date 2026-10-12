"""Scoped spend pages; all financial positions and writes use domain services."""

from datetime import date
from decimal import Decimal

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods

from core.access import organization_required, require_role
from finance.models import Bill

from .models import Budget, BudgetEntry
from .spend_forms import (
    BillSpendSelectionForm,
    SpendAdoptForm,
    SpendBillForm,
    SpendConvertForm,
    SpendMatchForm,
    SpendMovementCorrectionForm,
    SpendPaymentForm,
    SpendReleaseForm,
    SpendRequestForm,
)

READ = ("owner", "director", "finance", "auditor")
WRITE = ("owner", "director", "finance")
CORRECT = ("owner", "director")


def _services():
    from . import spend_services

    return spend_services


def _spend(request, pk):
    from .spend_models import BudgetSpend

    return get_object_or_404(
        BudgetSpend.objects.select_related("budget", "origin_entry"),
        organization=request.organization,
        budget__organization=request.organization,
        pk=pk,
    )


def _position(request, spend):
    result = _services().spend_position(
        organization=request.organization, spend=spend, as_of=timezone.localdate()
    )
    labels = {
        "convert": "Perubahan sebagian",
        "release": "Sisa dilepaskan",
        "payment": "Pembayaran dilaporkan",
        "adopt": "Catatan lama mulai dikelola",
    }
    buckets = {
        "reservation": "Dana dicadangkan",
        "open_commitment": "Janji belanja",
        "incurred": "Biaya terjadi",
    }
    result["movements"] = [
        dict(
            row,
            label=labels.get(row["kind"], "Perubahan belanja"),
            from_label=buckets.get(row.get("from_kind"), ""),
            to_label=buckets.get(row.get("to_kind"), ""),
        )
        for row in result.get("movements", [])
    ]
    manual = {
        row["key"]: row["label"] for row in result.get("manual_payment_sources", [])
    }
    finance = {
        row["event_id"]: row["label"]
        for row in result.get("finance_payment_sources", [])
    }
    result["payment_matches"] = [
        dict(
            row,
            manual_label=manual.get(
                row["manual_source_key"], "Laporan pembayaran lama"
            ),
            finance_label=finance.get(
                row["finance_event_id"], "Pembayaran Finance lama"
            ),
        )
        for row in result.get("payment_matches", [])
    ]
    result["as_of"] = _display_date(result.get("as_of"))
    result["has_overrun"] = (
        result.get("overrun") is not None and Decimal(result["overrun"]) > 0
    )
    for collection in ("movements", "bill_links", "payment_matches"):
        result[collection] = [
            dict(row, date=_display_date(row.get("date")))
            for row in result.get(collection, [])
        ]
    return result


def _display_date(value):
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return value


def _context(request, **values):
    return {
        "active_nav": "director_budgets",
        "can_spend_write": request.membership.role in WRITE,
        "can_spend_correct": request.membership.role in CORRECT,
        **values,
    }


def _errors(form, exc):
    if hasattr(exc, "message_dict"):
        for key, items in exc.message_dict.items():
            for item in items:
                form.add_error(key if key in form.fields else None, item)
    else:
        for item in exc.messages:
            form.add_error(None, item)


def _data(request, position):
    if request.method != "POST":
        return None
    data = request.POST.copy()
    if data.get("intent") == "refresh":
        data["expected_version"] = str(position["version"])
        data["expected_fingerprint"] = position["fingerprint"]
    return data


def _call(request, form, callback, *, back_url):
    if request.method == "POST" and request.POST.get("intent") == "preview":
        return None
    if request.method == "POST" and request.POST.get("intent") == "refresh":
        messages.info(
            request,
            "Posisi terbaru dimuat. Isian Anda dipertahankan; periksa kembali sebelum menyimpan.",
        )
        return None
    if request.method == "POST" and form.is_valid():
        values = dict(form.cleaned_data)
        values.pop("confirmed")
        try:
            callback(organization=request.organization, actor=request.user, **values)
        except ValidationError as exc:
            _errors(form, exc)
        else:
            messages.success(
                request,
                "Catatan belanja tersimpan. Pembukuan dan pembayaran Finance tetap mengikuti dokumen aslinya.",
            )
            return redirect(back_url)
    return None


def _form_page(
    request,
    form,
    *,
    title,
    explanation,
    position,
    back_url,
    spend=None,
    entry=None,
    source_record=None,
    submit_label="Simpan catatan",
    available=True,
    bill_preview=None,
    repair_allowed=False,
):
    bill_form = isinstance(form, SpendBillForm)
    if bill_form and form.is_bound and form.errors:
        submit_label = "Periksa tagihan terbaru"
    return render(
        request,
        "director/spend_form.html",
        _context(
            request,
            form=form,
            page_title=title,
            explanation=explanation,
            position=position,
            spend=spend,
            entry=entry,
            source_record=source_record,
            back_url=back_url,
            submit_label=submit_label,
            save_available=bool(
                (position.get("current") or repair_allowed) and available
            ),
            bill_form=bill_form,
            bill_preview=bill_preview,
            hidden_errors=[
                error for field in form.hidden_fields() for error in field.errors
            ],
            missing_bill_source=bill_form and not form.fields["bill"].queryset.exists(),
            missing_payment_report=isinstance(form, SpendMatchForm)
            and not position.get("manual_payment_sources"),
            missing_finance_payment=isinstance(form, SpendMatchForm)
            and not position.get("finance_payment_sources"),
        ),
    )


def _preview_bill(request, form, *, organization, position, initial=None):
    if (
        request.method != "POST"
        or request.POST.get("intent") != "preview"
        or not form.is_valid()
    ):
        return form, None
    from finance.budget_adapter import bill_budget_facts

    try:
        facts = bill_budget_facts(
            organization=organization,
            bill=form.cleaned_data["bill"],
            as_of=timezone.localdate(),
        )
    except ValidationError as exc:
        _errors(form, exc)
        return form, None
    data = form.data.copy()
    data["expected_bill_fingerprint"] = facts["fingerprint"]
    form = SpendBillForm(
        data,
        organization=organization,
        position=position,
        initial=initial or {},
        repair=form.repair,
    )
    facts = dict(
        facts,
        bill=dict(
            facts["bill"],
            date=_display_date(facts["bill"].get("date")),
            service_date=_display_date(facts["bill"].get("service_date")),
        ),
    )
    return form, facts


@require_GET
@organization_required
def spend_detail(request, pk):
    require_role(request, *READ)
    spend = _spend(request, pk)
    return render(
        request,
        "director/spend_detail.html",
        _context(request, spend=spend, position=_position(request, spend)),
    )


@require_http_methods(["GET", "POST"])
@organization_required
def spend_adopt(request, pk, entry_pk):
    require_role(request, *WRITE)
    budget = get_object_or_404(Budget, organization=request.organization, pk=pk)
    entry = get_object_or_404(
        BudgetEntry,
        organization=request.organization,
        budget=budget,
        pk=entry_pk,
        kind__in=("reservation", "open_commitment", "incurred"),
    )
    from .spend_models import BudgetSpend

    existing = BudgetSpend.objects.filter(
        organization=request.organization, origin_entry=entry
    ).first()
    if existing and request.method == "GET":
        return redirect("director:spend_detail", pk=existing.pk)
    position = _services().budget_position(
        organization=request.organization, budget=budget, as_of=timezone.localdate()
    )
    position["as_of"] = _display_date(position.get("as_of"))
    position["has_overrun"] = (
        position.get("overrun") is not None and Decimal(position["overrun"]) > 0
    )
    form = SpendAdoptForm(_data(request, position), position=position)
    result = _call(
        request,
        form,
        lambda **values: _services().adopt_budget_entry(entry=entry, **values),
        back_url=reverse("director:budget_detail", args=[budget.pk]),
    )
    if result:
        return result
    return _form_page(
        request,
        form,
        title="Kelola belanja ini",
        entry=entry,
        position=position,
        explanation="Mulai pantau catatan ini sebagai satu belanja. Nilai, tanggal, dan laporan pembayaran lama tetap tersimpan.",
        back_url=reverse("director:budget_detail", args=[budget.pk]),
        submit_label="Mulai kelola belanja",
        available=budget.status == "approved" and not entry.voided_at,
    )


OPERATIONS = {
    "convert": (
        SpendConvertForm,
        "Catat perubahan sebagian",
        "Pindahkan hanya jumlah yang sudah berubah. Konsumsi anggaran tidak bertambah karena perpindahan ini.",
        "convert_spend",
    ),
    "release": (
        SpendReleaseForm,
        "Lepaskan sisa yang tidak diperlukan",
        "Hanya sisa janji belanja atau dana cadangan yang dapat dilepaskan. Biaya terjadi dan utang tagihan tetap dipertahankan.",
        "release_spend_residual",
    ),
    "report_payment": (
        SpendPaymentForm,
        "Laporkan pembayaran",
        "Catat laporan pembayaran yang diterima. Uang bank terverifikasi berasal dari pencocokan Finance, bukan dari laporan ini.",
        "record_spend_payment",
    ),
    "link_bill": (
        SpendBillForm,
        "Hubungkan tagihan yang sudah ada",
        "Pilih tagihan dan nilai catatan lama yang mewakili pengeluaran sama. Hubungan ini tidak membuat tagihan, jurnal, atau transfer bank baru.",
        "link_spend_bill",
    ),
    "match_payment": (
        SpendMatchForm,
        "Cocokkan laporan dengan pembayaran Finance",
        "Pilih dua catatan untuk pembayaran yang sama. Jumlah yang dicocokkan dihitung sekali; bukti lama tetap tersedia.",
        "match_spend_payment",
    ),
}


@require_http_methods(["GET", "POST"])
@organization_required
def spend_operation(request, pk, operation):
    require_role(request, *(CORRECT if operation == "release" else WRITE))
    spend = _spend(request, pk)
    position = _position(request, spend)
    form_type, title, explanation, function = OPERATIONS[operation]
    kwargs = {"position": position}
    if operation == "link_bill":
        kwargs["organization"] = request.organization
        kwargs["preview"] = (
            request.method != "POST" or request.POST.get("intent") == "preview"
        )
        selected = request.GET.get("bill", "")
        if selected:
            bill = get_object_or_404(
                Bill, organization=request.organization, pk=selected
            )
            kwargs["initial"] = {"bill": bill.pk}
    form = form_type(_data(request, position), **kwargs)
    bill_preview = None
    if operation == "link_bill":
        form, bill_preview = _preview_bill(
            request,
            form,
            organization=request.organization,
            position=position,
            initial=kwargs.get("initial"),
        )
    back_url = reverse("director:spend_detail", args=[spend.pk])
    result = _call(
        request,
        form,
        lambda **values: getattr(_services(), function)(spend=spend, **values),
        back_url=back_url,
    )
    if result:
        return result
    available = operation in position.get("allowed_actions", [])
    if operation == "link_bill":
        available = available and form.fields["bill"].queryset.exists()
    if operation == "match_payment":
        available = available and bool(
            position.get("manual_payment_sources")
            and position.get("finance_payment_sources")
        )
    return _form_page(
        request,
        form,
        title=title,
        explanation=explanation,
        position=position,
        spend=spend,
        back_url=back_url,
        available=available,
        bill_preview=bill_preview,
    )


@require_http_methods(["GET", "POST"])
@organization_required
def spend_correction(request, pk, record_kind, record_pk, action):
    require_role(request, *CORRECT)
    spend = _spend(request, pk)
    position = _position(request, spend)
    config = {
        "movement": ("movements", "movement", "BudgetSpendMovement"),
        "bill_link": ("bill_links", "link", "BudgetBillLink"),
        "payment_match": ("payment_matches", "match", "BudgetPaymentMatch"),
    }
    if (
        record_kind not in config
        or action not in ("correct", "void")
        or (record_kind == "movement" and action != "correct")
    ):
        raise Http404
    collection, argument, model_name = config[record_kind]
    source_record = next(
        (row for row in position.get(collection, []) if row["id"] == record_pk), None
    )
    if not source_record:
        raise Http404
    from . import spend_models

    record = get_object_or_404(
        getattr(spend_models, model_name),
        organization=request.organization,
        spend=spend,
        pk=record_pk,
    )
    form_type = (
        SpendRequestForm
        if action == "void"
        else {
            "movement": SpendMovementCorrectionForm,
            "bill_link": SpendBillForm,
            "payment_match": SpendMatchForm,
        }[record_kind]
    )
    kwargs = {"position": position}
    if record_kind == "movement":
        kwargs["movement"] = source_record
    elif action == "correct" and record_kind == "bill_link":
        kwargs["organization"] = request.organization
        kwargs["preview"] = (
            request.method != "POST" or request.POST.get("intent") == "preview"
        )
        kwargs["repair"] = True
        kwargs["initial"] = {"bill": source_record["bill_id"]}
    form = form_type(_data(request, position), **kwargs)
    bill_preview = None
    if action == "correct" and record_kind == "bill_link":
        form, bill_preview = _preview_bill(
            request,
            form,
            organization=request.organization,
            position=position,
            initial=kwargs.get("initial"),
        )
    function = {
        ("movement", "correct"): "correct_spend_movement",
        ("bill_link", "correct"): "correct_spend_bill_link",
        ("bill_link", "void"): "void_spend_bill_link",
        ("payment_match", "correct"): "correct_spend_payment_match",
        ("payment_match", "void"): "void_spend_payment_match",
    }[(record_kind, action)]
    back_url = reverse("director:spend_detail", args=[spend.pk])
    result = _call(
        request,
        form,
        lambda **values: getattr(_services(), function)(**{argument: record}, **values),
        back_url=back_url,
    )
    if result:
        return result
    return _form_page(
        request,
        form,
        title="Perbaiki catatan belanja"
        if action == "correct"
        else "Tarik hubungan yang keliru",
        explanation="Catatan asal tetap dalam riwayat. Perbaikan ini tidak mengubah tagihan, jurnal, atau pembayaran Finance.",
        position=position,
        spend=spend,
        source_record=source_record,
        back_url=back_url,
        submit_label="Simpan perbaikan",
        available=source_record.get("can_correct", False),
        bill_preview=bill_preview,
        repair_allowed=record_kind in {"bill_link", "payment_match"}
        and ("correct_link" if record_kind == "bill_link" else "correct_match")
        in position.get("allowed_actions", []),
    )


@require_http_methods(["GET", "POST"])
@organization_required
def bill_spend_entry(request, pk):
    require_role(request, *WRITE)
    bill = get_object_or_404(
        Bill,
        organization=request.organization,
        supplier__organization=request.organization,
        pk=pk,
    )
    from .spend_models import BudgetSpend

    spends = list(
        BudgetSpend.objects.filter(
            organization=request.organization,
            budget__organization=request.organization,
            budget__status="approved",
        ).select_related("budget", "origin_entry")
    )
    choices = [
        (str(row.pk), f"{row.budget.title} · {row.origin_entry.description}")
        for row in spends
    ]
    form = BillSpendSelectionForm(
        request.POST if request.method == "POST" else None, choices=choices
    )
    if request.method == "POST" and form.is_valid():
        return redirect(
            reverse("director:spend_link_bill", args=[form.cleaned_data["spend"]])
            + f"?bill={bill.pk}"
        )
    return render(
        request,
        "director/spend_bill_entry.html",
        _context(
            request,
            form=form,
            bill=bill,
            has_spends=bool(spends),
            back_url=reverse("bill_detail", args=[bill.pk]),
        ),
    )
