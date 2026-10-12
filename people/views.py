"""Permission-checked, finance-first pages for employee and tutor payroll."""

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from core.access import organization_required, require_role

from . import payroll
from .forms import (
    PayrollCorrectionForm,
    PayrollLineForm,
    PayrollPaymentForm,
    PayrollRunForm,
    PersonForm,
)
from .models import PayrollPerson, PayrollRun


def _access(request):
    require_role(request, "owner", "director", "finance")


def _write(request):
    require_role(request, "owner", "finance")


def _error(exc):
    return (
        " ".join(exc.messages)
        if hasattr(exc, "messages")
        else "Perubahan tidak dapat disimpan. Periksa kembali isian Anda."
    )


@organization_required
@require_GET
def index(request):
    _access(request)
    context = payroll.payroll_summary(
        organization=request.organization, actor=request.user
    )
    context.update(
        {
            "active_nav": "people",
            "can_prepare": request.membership.role in {"owner", "finance"},
            "can_approve": request.membership.role in {"owner", "director"},
        }
    )
    return render(request, "people/index.html", context)


@organization_required
@require_http_methods(["GET", "POST"])
def person_create(request):
    _write(request)
    form = PersonForm(request.POST or None)
    if form.is_valid():
        try:
            payroll.add_person(
                organization=request.organization,
                actor=request.user,
                **form.cleaned_data,
            )
            messages.success(request, "Nama berhasil ditambahkan ke daftar SDM.")
            return redirect("people:index")
        except ValidationError as exc:
            form.add_error(None, _error(exc))
    return render(
        request, "people/person_form.html", {"form": form, "active_nav": "people"}
    )


@organization_required
@require_POST
def person_status(request, pk):
    _write(request)
    person = get_object_or_404(PayrollPerson, organization=request.organization, pk=pk)
    try:
        payroll.set_person_active(
            organization=request.organization,
            actor=request.user,
            person=person,
            active=request.POST.get("active") == "1",
        )
        messages.success(request, "Status SDM diperbarui.")
    except ValidationError as exc:
        messages.error(request, _error(exc))
    return redirect("people:index")


@organization_required
@require_http_methods(["GET", "POST"])
def run_create(request):
    _write(request)
    initial = {
        key: request.GET[key] for key in ("year", "month") if request.GET.get(key)
    }
    form = PayrollRunForm(request.POST or None, initial=initial)
    if form.is_valid():
        try:
            run = payroll.create_run(
                organization=request.organization,
                actor=request.user,
                **form.cleaned_data,
            )
            messages.success(request, f"Payroll {run.period_label} siap diisi.")
            return redirect("people:run_detail", pk=run.pk)
        except ValidationError as exc:
            form.add_error(None, _error(exc))
    return render(
        request, "people/run_form.html", {"form": form, "active_nav": "people"}
    )


@organization_required
@require_GET
def run_detail(request, pk):
    _access(request)
    run = get_object_or_404(
        PayrollRun.objects.select_related(
            "created_by", "submitted_by", "approved_by", "journal", "reversal_journal"
        ).prefetch_related(
            "lines__person", "payments__transaction", "payments__created_by"
        ),
        organization=request.organization,
        pk=pk,
    )
    position = payroll.payment_position(
        organization=request.organization, actor=request.user, run=run
    )
    context = {
        "run": run,
        "lines": run.lines.all(),
        "totals": payroll._totals(run),
        "payments": run.payments.all(),
        "payment_position": position,
        "audit_events": payroll.audit_timeline(
            organization=request.organization, actor=request.user, run=run
        ),
        "active_nav": "people",
        "can_prepare": request.membership.role in {"owner", "finance"},
        "can_approve": request.membership.role in {"owner", "director"},
        "line_form": PayrollLineForm(organization=request.organization)
        if run.status == PayrollRun.Status.DRAFT
        else None,
        "payment_form": PayrollPaymentForm(
            organization=request.organization, position=position
        )
        if run.status == PayrollRun.Status.POSTED
        and request.membership.role in {"owner", "finance"}
        else None,
        "correction_form": PayrollCorrectionForm(
            initial={"reversal_date": timezone.localdate()}
        )
        if run.status == PayrollRun.Status.POSTED
        and request.membership.role in {"owner", "director"}
        else None,
    }
    return render(request, "people/run_detail.html", context)


@organization_required
@require_POST
def line_add(request, pk):
    _write(request)
    run = get_object_or_404(PayrollRun, organization=request.organization, pk=pk)
    form = PayrollLineForm(request.POST, organization=request.organization)
    if form.is_valid():
        try:
            data = dict(form.cleaned_data)
            person = data.pop("person")
            payroll.add_line(
                organization=request.organization,
                actor=request.user,
                run=run,
                person=person,
                **data,
            )
            messages.success(request, "Rincian penggajian ditambahkan.")
            return redirect("people:run_detail", pk=run.pk)
        except ValidationError as exc:
            form.add_error(None, _error(exc))
    return render(
        request,
        "people/run_detail.html",
        {
            "run": run,
            "lines": run.lines.select_related("person"),
            "totals": payroll._totals(run),
            "line_form": form,
            "can_prepare": True,
            "can_approve": False,
            "active_nav": "people",
            "payments": run.payments.all(),
            "payment_position": payroll.payment_position(
                organization=request.organization, actor=request.user, run=run
            ),
        },
    )


@organization_required
@require_http_methods(["GET", "POST"])
def line_edit(request, pk, line_pk):
    _write(request)
    run = get_object_or_404(PayrollRun, organization=request.organization, pk=pk)
    if run.status != PayrollRun.Status.DRAFT:
        messages.error(
            request,
            "Payroll yang sudah diajukan tidak dapat diubah. Minta perbaikan jika ada angka yang perlu diganti.",
        )
        return redirect("people:run_detail", pk=run.pk)
    line = get_object_or_404(run.lines.select_related("person"), pk=line_pk)
    initial = {
        "person": line.person_id,
        "gross_amount": line.gross_amount,
        "pph21_amount": line.pph21_amount,
        "bpjs_employee_amount": line.bpjs_employee_amount,
        "bpjs_employer_amount": line.bpjs_employer_amount,
        "basis_reference": line.basis_reference,
        "note": line.note,
    }
    form = PayrollLineForm(
        request.POST or None, organization=request.organization, initial=initial
    )
    if request.method == "POST" and form.is_valid():
        try:
            data = dict(form.cleaned_data)
            person = data.pop("person")
            payroll.update_line(
                organization=request.organization,
                actor=request.user,
                run=run,
                line=line,
                person=person,
                **data,
            )
            messages.success(
                request, "Rincian payroll diperbarui dan dicatat dalam riwayat."
            )
            return redirect("people:run_detail", pk=run.pk)
        except ValidationError as exc:
            form.add_error(None, _error(exc))
    return render(
        request,
        "people/line_form.html",
        {"form": form, "run": run, "line": line, "active_nav": "people"},
    )


@organization_required
@require_POST
def line_remove(request, pk, line_pk):
    _write(request)
    run = get_object_or_404(PayrollRun, organization=request.organization, pk=pk)
    line = get_object_or_404(run.lines, pk=line_pk)
    try:
        payroll.remove_line(
            organization=request.organization,
            actor=request.user,
            run=run,
            line=line,
            reason=request.POST.get("reason", ""),
        )
        messages.success(
            request, "Rincian dihapus dari draf dan alasan disimpan di riwayat."
        )
    except ValidationError as exc:
        messages.error(request, _error(exc))
    return redirect("people:run_detail", pk=run.pk)


@organization_required
@require_POST
def run_action(request, pk, action):
    _access(request)
    run = get_object_or_404(PayrollRun, organization=request.organization, pk=pk)
    actions = {
        "submit": (
            payroll.submit_run,
            {},
            "Payroll diajukan untuk diperiksa Owner/Direktur.",
        ),
        "approve": (
            payroll.approve_run,
            {"approval_note": "approval_note"},
            "Payroll disetujui dan tercatat di pembukuan. Cocokkan pembayaran setelah mutasi bank tersedia.",
        ),
        "reject": (
            payroll.reject_run,
            {"rejection_note": "rejection_note"},
            "Payroll perlu diperbaiki. Finance dapat menyiapkan versi baru untuk periode ini.",
        ),
        "reverse": (
            payroll.reverse_run,
            {"reversal_date": "reversal_date", "reason": "reason"},
            "Koreksi payroll tercatat. Finance dapat menyiapkan perbaikan untuk periode ini.",
        ),
    }
    try:
        try:
            expected_version = int(request.POST.get("version", ""))
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                "Halaman ini sudah berubah. Muat ulang untuk melihat angka terbaru sebelum melanjutkan."
            ) from exc
        try:
            service, post_fields, success_message = actions[action]
        except KeyError:
            raise PermissionDenied("Tindakan payroll tidak dikenal.")
        updated = service(
            organization=request.organization,
            actor=request.user,
            run=run,
            expected_version=expected_version,
            **{key: request.POST.get(field, "") for key, field in post_fields.items()},
        )
        messages.success(request, success_message)
        return redirect("people:run_detail", pk=updated.pk)
    except ValidationError as exc:
        messages.error(request, _error(exc))
        return redirect("people:run_detail", pk=run.pk)


@organization_required
@require_POST
def payment_match(request, pk):
    _write(request)
    run = get_object_or_404(PayrollRun, organization=request.organization, pk=pk)
    position = payroll.payment_position(
        organization=request.organization, actor=request.user, run=run
    )
    form = PayrollPaymentForm(
        request.POST, organization=request.organization, position=position
    )
    if form.is_valid():
        try:
            payroll.match_payment(
                organization=request.organization,
                actor=request.user,
                run=run,
                transaction=form.cleaned_data["transaction"],
                kind=form.cleaned_data["kind"],
                amount=form.cleaned_data["amount"],
            )
            messages.success(
                request,
                "Pembayaran dicocokkan dengan mutasi bank dan masuk ke pembukuan.",
            )
        except ValidationError as exc:
            messages.error(request, _error(exc))
    else:
        messages.error(
            request, "Pilih jenis pembayaran, mutasi bank, dan nominal yang valid."
        )
    return redirect("people:run_detail", pk=run.pk)
