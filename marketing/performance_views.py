"""Marketing KPI and work accountability pages using audited domain commands."""

from calendar import monthrange
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods

from core.access import organization_required, require_role

from . import activities as activity_services
from . import kpi
from .activity_models import WorkActivity
from .kpi_models import KpiTarget
from .models import TeamMember
from .performance_forms import (
    ActivityDecisionForm,
    ActivityFilterForm,
    ActivityForm,
    ActivitySubmitForm,
    KpiFilterForm,
    KpiReviewForm,
    KpiTargetForm,
    ReasonForm,
)
from .views import (
    READ,
    WRITE,
    _bad,
    _context,
    _form_error,
    _form_page,
    _page,
    _pagination_query,
    _version,
)

REVIEWERS = ("owner", "director")


def _value(value, unit):
    """Keep unavailable values distinct from zero in every card and review."""
    if value is None or value == "":
        return "Belum tersedia"
    try:
        number = Decimal(str(value))
        if not number.is_finite():
            return "Belum tersedia"
        numeric = f"{number:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
        if numeric.endswith(",00"):
            numeric = numeric.removesuffix(",00")
    except (InvalidOperation, TypeError, ValueError):
        return "Belum tersedia"
    if unit in {"idr", "IDR", "rupiah", "Rp", "currency"}:
        return "Rp " + numeric
    suffix = {
        "percent": "%",
        "%": "%",
        "percentage": "%",
        "minutes": " menit",
        "minute": " menit",
        "menit": " menit",
        "ratio": "×",
        "count": "",
        "jumlah": "",
        "number": "",
    }.get(unit, " " + str(unit or ""))
    return numeric + suffix


def _present(evaluation):
    value = dict(evaluation)
    value["actual_display"] = _value(value.get("actual"), value.get("unit"))
    value["target_display"] = _value(value.get("target"), value.get("unit"))
    value["attainment_display"] = _value(value.get("attainment"), "%")
    value["direction_label"] = (
        "Maksimal"
        if value.get("direction") in {"lower", "maximum", "max", "lte"}
        else "Minimal"
    )
    return value


@require_GET
@organization_required
def kpis(request):
    require_role(request, *READ)
    form = KpiFilterForm(request.GET, organization=request.organization)
    rows = KpiTarget.objects.filter(organization=request.organization).select_related(
        "member", "campaign"
    )
    valid = form.is_valid()
    if valid:
        values = form.cleaned_data
        for field in ("status", "member", "channel"):
            if values.get(field):
                rows = rows.filter(**{field: values[field]})
        if values.get("start"):
            rows = rows.filter(period_end__gte=values["start"])
        if values.get("end"):
            rows = rows.filter(period_start__lte=values["end"])
    else:
        rows = rows.none()
    try:
        page = _page(request, rows.order_by("-period_start", "-pk"))
    except ValidationError as exc:
        return _bad(exc)
    for target in page:
        target.live = _present(kpi.evaluate_target(target))
    return render(
        request,
        "marketing/kpis.html",
        _context(
            request,
            "kpis",
            page=page,
            filter_form=form,
            pagination_query=_pagination_query(request),
            marketing_reviewer=request.membership.role in REVIEWERS,
        ),
        status=200 if valid else 400,
    )


@require_http_methods(["GET", "POST"])
@organization_required
def kpi_create(request):
    require_role(request, *WRITE)
    today = timezone.localdate()
    form = KpiTargetForm(
        request.POST if request.method == "POST" else None,
        organization=request.organization,
        initial={
            "period_start": today.replace(day=1),
            "period_end": today.replace(day=monthrange(today.year, today.month)[1]),
        },
    )
    if request.method == "POST" and form.is_valid():
        try:
            target = kpi.create_target(
                organization=request.organization,
                actor=request.user,
                **form.cleaned_data,
            )
            messages.success(
                request,
                "Draf KPI disimpan. Direktur dapat meninjau dan mengaktifkan target.",
            )
            return redirect("marketing:kpi_detail", pk=target.pk)
        except ValidationError as exc:
            _form_error(form, exc)
    return _form_page(
        request,
        form,
        "Susun target marketing",
        "Pilih hasil yang diukur, periode, dan staf atau kanal yang dinilai.",
        "kpis",
        "marketing:kpis",
        note_title="Target disepakati, hasil dihitung dari data",
        note="Seimbangkan penjualan, biaya, dan tindak lanjut. Angka aktual berasal dari pencatatan ERP; data kosong tidak dianggap nol. Aplikasi menyimpan keputusan direktur dan hasil evaluasi agar dapat diperiksa kembali.",
    )


@require_http_methods(["GET", "POST"])
@organization_required
def kpi_detail(request, pk):
    require_role(request, *READ)
    target = get_object_or_404(
        KpiTarget.objects.select_related(
            "member", "campaign", "approved_by", "created_by"
        ),
        organization=request.organization,
        pk=pk,
    )
    operation = request.POST.get("operation", "") if request.method == "POST" else ""
    reason_form = ReasonForm(
        request.POST
        if request.method == "POST" and operation in {"activate", "retire"}
        else None
    )
    review_form = KpiReviewForm(
        request.POST if request.method == "POST" and operation == "review" else None
    )
    command_error = ""
    if request.method == "POST":
        require_role(request, *REVIEWERS)
        if operation not in {"activate", "retire", "review"}:
            return _bad(
                ValidationError(
                    "Tindakan KPI tidak dikenali. Pilih tombol yang tersedia."
                )
            )
        form = review_form if operation == "review" else reason_form
        if form.is_valid():
            try:
                service = {
                    "activate": kpi.activate_target,
                    "retire": kpi.retire_target,
                    "review": kpi.review_target,
                }[operation]
                service(
                    organization=request.organization,
                    actor=request.user,
                    target=target,
                    expected_version=_version(request),
                    **form.cleaned_data,
                )
                messages.success(
                    request,
                    {
                        "activate": "Target KPI diaktifkan dan keputusan disimpan.",
                        "retire": "Target dihentikan. Riwayat target dan evaluasi tetap tersedia.",
                        "review": "Evaluasi tersimpan bersama angka dan dasar perhitungannya.",
                    }[operation],
                )
                return redirect("marketing:kpi_detail", pk=target.pk)
            except ValidationError as exc:
                _form_error(form, exc)
                command_error = " ".join(exc.messages)
    evaluation = _present(kpi.evaluate_target(target))
    if not review_form.is_bound:
        review_form.initial["expected_fingerprint"] = evaluation["fingerprint"]
    try:
        page = _page(
            request,
            target.reviews.select_related("created_by").order_by("-created_at", "-pk"),
        )
    except ValidationError as exc:
        return _bad(exc)
    reviews = list(page)
    for review in reviews:
        review.result = _present(review.snapshot)
        review.is_stale = review.fingerprint != evaluation.get("fingerprint")
    can_review = request.membership.role in REVIEWERS
    analysis_query = {
        "start": target.period_start.isoformat(),
        "end": target.period_end.isoformat(),
    }
    analysis_query.update(
        {
            key: value
            for key, value in {
                "member": target.member_id,
                "channel": target.channel,
                "campaign": target.campaign_id,
            }.items()
            if value
        }
    )
    return render(
        request,
        "marketing/kpi_detail.html",
        _context(
            request,
            "kpis",
            target=target,
            evaluation=evaluation,
            source_periods=target.source_periods.select_related("approved_by").order_by(
                "-pk"
            )[:10],
            reviews=reviews,
            page=page,
            pagination_query=_pagination_query(request),
            analysis_query=urlencode(analysis_query),
            reason_form=reason_form,
            review_form=review_form,
            marketing_reviewer=can_review,
            command_error=command_error,
            can_record_review=can_review
            and target.status == "active"
            and target.period_end < timezone.localdate()
            and evaluation.get("official", False),
            operation=operation,
        ),
    )


@require_GET
@organization_required
def activities(request):
    require_role(request, *READ)
    form = ActivityFilterForm(request.GET, organization=request.organization)
    rows = WorkActivity.objects.filter(
        organization=request.organization
    ).select_related("owner", "current_owner", "campaign")
    valid = form.is_valid()
    if valid:
        values = form.cleaned_data
        if values.get("owner"):
            rows = rows.filter(
                Q(current_owner=values["owner"])
                | Q(current_owner__isnull=True, owner=values["owner"])
            )
        for field in ("status", "kind"):
            if values.get(field):
                rows = rows.filter(**{field: values[field]})
        if values.get("q"):
            rows = rows.filter(
                Q(title__icontains=values["q"]) | Q(description__icontains=values["q"])
            )
        if values.get("overdue"):
            rows = rows.filter(
                due_date__lt=timezone.localdate(),
                status__in=("planned", "in_progress", "needs_revision"),
            )
    else:
        rows = rows.none()
    try:
        page = _page(request, rows.order_by("due_date", "-pk"))
    except ValidationError as exc:
        return _bad(exc)
    for activity in page:
        activity.is_overdue = (
            activity.due_date < timezone.localdate()
            and activity.status in {"planned", "in_progress", "needs_revision"}
        )
    return render(
        request,
        "marketing/activities.html",
        _context(
            request,
            "activities",
            page=page,
            filter_form=form,
            pagination_query=_pagination_query(request),
            has_active_members=TeamMember.objects.filter(
                organization=request.organization, active=True
            ).exists(),
        ),
        status=200 if valid else 400,
    )


@require_http_methods(["GET", "POST"])
@organization_required
def activity_create(request):
    require_role(request, *WRITE)
    form = ActivityForm(
        request.POST if request.method == "POST" else None,
        organization=request.organization,
        initial={"due_date": timezone.localdate()},
    )
    if request.method == "POST" and form.is_valid():
        try:
            activity = activity_services.create_activity(
                organization=request.organization,
                actor=request.user,
                **form.cleaned_data,
            )
            messages.success(
                request, "Pekerjaan ditugaskan. Catat bukti saat mengajukan hasilnya."
            )
            return redirect("marketing:activity_detail", pk=activity.pk)
        except ValidationError as exc:
            _form_error(form, exc)
    return _form_page(
        request,
        form,
        "Tugaskan pekerjaan marketing",
        "Setiap pekerjaan memiliki penanggung jawab, tenggat, dan hasil yang bisa diperiksa.",
        "activities",
        "marketing:activities",
        note_title="Dikerjakan, dibuktikan, diperiksa",
        note="Setelah pekerjaan selesai, staf mengisi tanggal pelaksanaan, bukti, dan hasil. Direktur yang berbeda dari pengaju hasil memeriksa sebelum pekerjaan dinyatakan diterima. Direktur boleh memeriksa pekerjaan yang ditugaskannya kepada staf. Riwayat tetap disimpan.",
    )


@require_http_methods(["GET", "POST"])
@organization_required
def activity_detail(request, pk):
    require_role(request, *READ)
    activity = get_object_or_404(
        WorkActivity.objects.select_related(
            "owner",
            "current_owner",
            "campaign",
            "lead",
            "created_by",
            "submitted_by",
            "reviewed_by",
        ),
        organization=request.organization,
        pk=pk,
    )
    operation = request.POST.get("operation", "") if request.method == "POST" else ""
    submit_form = ActivitySubmitForm(
        request.POST if request.method == "POST" and operation == "submit" else None,
        initial={
            "occurred_on": activity.occurred_on or timezone.localdate(),
            "evidence": activity.evidence,
            "outcome": activity.outcome,
        },
    )
    decision_form = ActivityDecisionForm(
        request.POST
        if request.method == "POST"
        and operation in {"accept", "return", "cancel", "withdraw"}
        else None
    )
    action_error = ""
    if request.method == "POST":
        require_role(request, *WRITE)
        if operation not in {
            "start",
            "submit",
            "accept",
            "return",
            "cancel",
            "withdraw",
        }:
            return _bad(
                ValidationError(
                    "Tindakan pekerjaan tidak dikenali. Pilih tombol yang tersedia."
                )
            )
        if operation in {"accept", "return"}:
            require_role(request, *REVIEWERS)
        form = (
            submit_form
            if operation == "submit"
            else decision_form
            if operation != "start"
            else None
        )
        if form is None or form.is_valid():
            try:
                values = (
                    form.cleaned_data
                    if form
                    else {
                        "responsibility_reason": request.POST.get(
                            "responsibility_reason", ""
                        )
                    }
                )
                activity_services.update_activity(
                    organization=request.organization,
                    actor=request.user,
                    activity=activity,
                    expected_version=_version(request),
                    operation=operation,
                    **values,
                )
                messages.success(
                    request,
                    {
                        "start": "Pekerjaan ditandai sedang dikerjakan.",
                        "submit": "Hasil diajukan untuk diperiksa direktur.",
                        "accept": "Hasil pekerjaan diterima beserta catatan pemeriksaan.",
                        "return": "Pekerjaan dikembalikan untuk perbaikan.",
                        "cancel": "Pekerjaan dibatalkan; alasan dan riwayat tetap tersimpan.",
                        "withdraw": "Pengajuan ditarik untuk diperbaiki. Bukti dan alasan sebelumnya tetap tersimpan dalam riwayat.",
                    }[operation],
                )
                return redirect("marketing:activity_detail", pk=activity.pk)
            except ValidationError as exc:
                action_error = " ".join(exc.messages)
                if form is not None:
                    _form_error(form, exc)
    writer = request.membership.role in WRITE
    reviewer = request.membership.role in REVIEWERS
    from .workflow import can_act_for_member

    responsible = can_act_for_member(
        organization=request.organization,
        actor=request.user,
        member=activity.assigned_owner,
    )
    executor = writer and (responsible or reviewer)
    if not (reviewer and not responsible):
        submit_form.fields["responsibility_reason"].widget = forms.HiddenInput()
        decision_form.fields["responsibility_reason"].widget = forms.HiddenInput()
    independent = request.user.pk != activity.submitted_by_id
    return render(
        request,
        "marketing/activity_detail.html",
        _context(
            request,
            "activities",
            activity=activity,
            submit_form=submit_form,
            decision_form=decision_form,
            action_error=action_error,
            operation=operation,
            attachments=activity.attachments.select_related("created_by").order_by(
                "version"
            ),
            replacement=WorkActivity.objects.filter(
                organization=request.organization, replaces=activity
            ).first(),
            assignment_events=activity.assignment_events.select_related(
                "previous_member", "member", "created_by"
            ),
            can_handoff=reviewer
            and activity.status in {"planned", "in_progress", "needs_revision"},
            supervisor_exception=reviewer and not responsible,
            can_attach=executor
            and activity.status in {"planned", "in_progress", "needs_revision"},
            can_correct=reviewer
            and activity.status == "accepted"
            and not WorkActivity.objects.filter(replaces=activity).exists(),
            can_start=executor
            and activity.assigned_owner.active
            and activity.status in {"planned", "needs_revision"},
            can_submit=executor
            and activity.assigned_owner.active
            and activity.status in {"planned", "in_progress", "needs_revision"},
            can_decide=reviewer and independent and activity.status == "submitted",
            can_withdraw=writer
            and request.user.pk == activity.submitted_by_id
            and activity.status == "submitted",
            needs_independent_reviewer=reviewer
            and not independent
            and activity.status == "submitted",
            can_cancel=executor
            and activity.status in {"planned", "in_progress", "needs_revision"},
            is_overdue=activity.due_date < timezone.localdate()
            and activity.status in {"planned", "in_progress", "needs_revision"},
        ),
    )
