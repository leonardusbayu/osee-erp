from io import BytesIO

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods

from core.access import organization_required, require_role

from .activity_models import ActivityEvidence, WorkActivity
from .kpi import evaluate_target
from .kpi_completeness import (
    approve_source_period,
    create_source_period,
    withdraw_source_period,
)
from .kpi_models import KpiSourcePeriod, KpiTarget
from .performance_workflow_forms import (
    EvidenceUploadForm,
    SourceCheckFormSet,
    SourceDecisionForm,
    SourcePeriodForm,
    WorkCorrectionForm,
    WorkHandoffForm,
)
from .views import READ, WRITE, _context, _form_error


@require_GET
@organization_required
def management(request):
    from .management import management_snapshot

    require_role(request, *READ)
    today = timezone.localdate()
    try:
        snapshot = management_snapshot(
            organization=request.organization,
            start=request.GET.get("start", today.replace(month=1, day=1).isoformat()),
            end=request.GET.get("end", today.isoformat()),
        )
    except ValidationError as exc:
        return HttpResponse(" ".join(exc.messages), status=400)
    return render(
        request,
        "marketing/management.html",
        _context(request, "kpis", management=snapshot),
    )


@require_http_methods(["GET", "POST"])
@organization_required
def source_period_create(request, pk):
    require_role(request, *WRITE)
    target = get_object_or_404(KpiTarget, organization=request.organization, pk=pk)
    data = request.POST if request.method == "POST" else None
    form, formset = SourcePeriodForm(data), SourceCheckFormSet(data, prefix="sources")
    evaluation = evaluate_target(target)
    if request.method == "POST" and form.is_valid() and formset.is_valid():
        try:
            period = create_source_period(
                organization=request.organization,
                actor=request.user,
                target=target,
                sources=[row for row in formset.cleaned_data if row],
                **form.cleaned_data,
            )
            return redirect("marketing:source_period_detail", pk=period.pk)
        except ValidationError as exc:
            _form_error(form, exc)
    return render(
        request,
        "marketing/source_period_form.html",
        _context(
            request,
            "kpis",
            target=target,
            form=form,
            formset=formset,
            evaluation=evaluation,
        ),
    )


@require_http_methods(["GET", "POST"])
@organization_required
def source_period_detail(request, pk):
    require_role(request, *READ)
    period = get_object_or_404(
        KpiSourcePeriod.objects.select_related("target", "created_by", "approved_by"),
        organization=request.organization,
        pk=pk,
    )
    evaluation = evaluate_target(period.target)
    form = SourceDecisionForm(
        request.POST if request.method == "POST" else None,
        initial={
            "expected_version": period.version,
            "expected_fingerprint": evaluation["basis"]["provenance"]["digest"],
        },
    )
    if request.method == "POST":
        require_role(request, "owner", "director")
        if form.is_valid():
            try:
                values = form.cleaned_data.copy()
                if request.POST.get("operation") == "approve":
                    approve_source_period(
                        organization=request.organization,
                        actor=request.user,
                        period=period,
                        **values,
                    )
                elif request.POST.get("operation") == "withdraw":
                    values.pop("expected_fingerprint")
                    withdraw_source_period(
                        organization=request.organization,
                        actor=request.user,
                        period=period,
                        **values,
                    )
                else:
                    raise ValidationError("Tindakan pemeriksaan tidak dikenal.")
                messages.success(request, "Keputusan atas kelengkapan sumber disimpan.")
                return redirect("marketing:source_period_detail", pk=period.pk)
            except ValidationError as exc:
                _form_error(form, exc)
    return render(
        request,
        "marketing/source_period_detail.html",
        _context(
            request,
            "kpis",
            period=period,
            form=form,
            evaluation=evaluation,
            reviewer=request.membership.role in {"owner", "director"},
            is_stale=period.data_fingerprint
            != evaluation["basis"]["provenance"]["digest"],
        ),
    )


@require_http_methods(["GET", "POST"])
@organization_required
def evidence_upload(request, pk):
    from .work_evidence import attach_work_evidence

    require_role(request, *WRITE)
    work = get_object_or_404(WorkActivity, organization=request.organization, pk=pk)
    form = EvidenceUploadForm(
        request.POST if request.method == "POST" else None,
        request.FILES or None,
        initial={"expected_version": work.version},
    )
    if request.method == "POST" and form.is_valid():
        try:
            attach_work_evidence(
                organization=request.organization,
                actor=request.user,
                activity=work,
                **form.cleaned_data,
            )
            messages.success(
                request, "Bukti privat tersimpan dengan sidik berkas dan nomor versi."
            )
            return redirect("marketing:activity_detail", pk=work.pk)
        except ValidationError as exc:
            _form_error(form, exc)
    return render(
        request,
        "marketing/work_attachment_form.html",
        _context(
            request,
            "activities",
            work=work,
            form=form,
            page_title="Unggah versi bukti",
            multipart=True,
        ),
    )


@require_GET
@organization_required
def evidence_download(request, pk):
    from .services import _audit
    from .work_evidence import checked_evidence_bytes

    require_role(request, *READ)
    proof = get_object_or_404(
        ActivityEvidence,
        organization=request.organization,
        activity__organization=request.organization,
        pk=pk,
    )
    try:
        data = checked_evidence_bytes(proof)
    except ValidationError as exc:
        raise Http404(
            "Bukti tidak tersedia atau integritas berkas perlu diperiksa."
        ) from exc
    _audit(
        request.organization,
        request.user,
        "activity.evidence_downloaded",
        proof,
        work_id=proof.activity_id,
    )
    response = FileResponse(
        BytesIO(data),
        as_attachment=True,
        filename=proof.original_name,
        content_type=proof.content_type,
    )
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store"
    return response


@require_http_methods(["GET", "POST"])
@organization_required
def work_correction(request, pk):
    from .activities import correct_activity

    require_role(request, "owner", "director")
    work = get_object_or_404(WorkActivity, organization=request.organization, pk=pk)
    form = WorkCorrectionForm(
        request.POST if request.method == "POST" else None,
        initial={"expected_version": work.version, "due_date": work.due_date},
    )
    if request.method == "POST" and form.is_valid():
        try:
            replacement = correct_activity(
                organization=request.organization,
                actor=request.user,
                activity=work,
                **form.cleaned_data,
            )
            return redirect("marketing:activity_detail", pk=replacement.pk)
        except ValidationError as exc:
            _form_error(form, exc)
    return render(
        request,
        "marketing/work_attachment_form.html",
        _context(
            request,
            "activities",
            work=work,
            form=form,
            page_title="Koreksi dengan pekerjaan pengganti",
            multipart=False,
        ),
    )


@require_http_methods(["GET", "POST"])
@organization_required
def work_handoff(request, pk):
    from .activities import handoff_activity

    require_role(request, "owner", "director")
    work = get_object_or_404(
        WorkActivity.objects.select_related("owner", "current_owner"),
        organization=request.organization,
        pk=pk,
    )
    eligible = work.status in {"planned", "in_progress", "needs_revision"}
    form = WorkHandoffForm(
        request.POST if request.method == "POST" else None,
        organization=request.organization,
        activity=work,
        initial={"expected_version": work.version},
    )
    if request.method == "POST" and form.is_valid():
        try:
            handoff_activity(
                organization=request.organization,
                actor=request.user,
                activity=work,
                **form.cleaned_data,
            )
            messages.success(
                request,
                "Pekerjaan dipindahkan ke staf pengganti. Bukti dan riwayat sebelumnya tetap tersimpan.",
            )
            return redirect("marketing:activity_detail", pk=work.pk)
        except ValidationError as exc:
            _form_error(form, exc)
    return render(
        request,
        "marketing/work_handoff.html",
        _context(
            request,
            "activities",
            work=work,
            form=form,
            eligible=eligible,
            has_recipients=form.fields["member"].queryset.exists(),
        ),
    )
