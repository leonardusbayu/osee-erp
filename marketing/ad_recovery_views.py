"""Scoped source recovery pages; all persistence remains in audited commands."""

from uuid import uuid4

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods

from core.access import organization_required, require_role
from core.models import AuditEvent

from .ad_recovery_forms import AdRecoveryForm, AdReplacementForm
from .models import AdDailyObservation, AdObservationRecovery
from .views import READ, WRITE, _context, _form_error


def _observation(request, pk):
    return get_object_or_404(
        AdDailyObservation.objects.select_related(
            "campaign", "created_by", "source_root"
        ),
        organization=request.organization,
        pk=pk,
    )


def _detail_context(request, observation):
    root_id = observation.source_root_id or observation.pk
    from django.db.models import Q

    revisions = list(
        AdDailyObservation.objects.filter(organization=request.organization)
        .filter(Q(pk=root_id) | Q(source_root_id=root_id))
        .select_related("campaign", "created_by")
        .order_by("created_at", "pk")
    )
    ids = [row.pk for row in revisions]
    decisions = list(
        AdObservationRecovery.objects.filter(
            organization=request.organization, predecessor_id__in=ids
        )
        .select_related("created_by", "predecessor__campaign", "successor__campaign")
        .order_by("created_at", "pk")
    )
    recovery = next(
        (row for row in decisions if row.predecessor_id == observation.pk), None
    )
    successor = recovery.successor if recovery and recovery.successor_id else None
    status = (
        "Diganti" if successor else "Ditarik" if observation.withdrawn_at else "Aktif"
    )
    numeric_history = (
        AuditEvent.objects.filter(
            organization=request.organization,
            object_type="AdDailyObservation",
            object_id__in=[str(pk) for pk in ids],
            action="marketing.ad.corrected",
        )
        .select_related("actor")
        .order_by("created_at", "pk")
    )
    return _context(
        request,
        "campaigns",
        observation=observation,
        revisions=revisions,
        decisions=decisions,
        recovery=recovery,
        successor=successor,
        source_status=status,
        numeric_history=numeric_history,
        can_recover=request.membership.role in WRITE
        and observation.withdrawn_at is None,
    )


@require_GET
@organization_required
def ad_detail(request, pk):
    require_role(request, *READ)
    return render(
        request,
        "marketing/ad_detail.html",
        _detail_context(request, _observation(request, pk)),
    )


def _recover(request, pk, action):
    require_role(request, *WRITE)
    from .ad_recovery import (
        observation_fingerprint,
        replace_ad_observation,
        withdraw_ad_observation,
    )

    observation = _observation(request, pk)
    fingerprint = observation_fingerprint(observation)
    controls = {
        "expected_version": observation.version,
        "expected_fingerprint": fingerprint,
        "request_key": str(uuid4()),
    }
    form_class = AdReplacementForm if action == "replace" else AdRecoveryForm
    data = request.POST if request.method == "POST" else None
    rechecked = False
    if (
        data is not None
        and data.get("action") == "recheck"
        and observation.withdrawn_at is None
    ):
        # Only an explicit staff action refreshes the viewed source. Their proposed
        # replacement/reason/evidence stay visible and require confirmation again.
        data = data.copy()
        if not any(
            len(data.getlist(name)) > 1 for name in (*form_class.base_fields, "action")
        ):
            for name in ("expected_version", "expected_fingerprint"):
                data[name] = controls[name]
            data.pop("confirmed", None)
            rechecked = True
    form = form_class(
        data,
        organization=request.organization,
        observation=observation,
        initial=controls,
    )
    if request.method == "POST" and not rechecked and form.is_valid():
        values = form.cleaned_data.copy()
        values.pop("confirmed")
        values.pop("reference", None)
        service = (
            replace_ad_observation if action == "replace" else withdraw_ad_observation
        )
        try:
            decision = service(
                organization=request.organization,
                actor=request.user,
                observation=observation,
                **values,
            )
            messages.success(
                request,
                "Laporan pengganti disimpan. Periksa kembali cakupan sumber yang terdampak."
                if decision.successor_id
                else "Laporan ditarik dari perhitungan. Riwayat tetap tersedia; biaya sebenarnya belum dinyatakan nol.",
            )
            return redirect(
                "marketing:ad_detail", pk=decision.successor_id or observation.pk
            )
        except ValidationError as exc:
            _form_error(form, exc)
            observation.refresh_from_db()
            fingerprint = observation_fingerprint(observation)
    context = _detail_context(request, observation)
    stale = bool(
        data is not None
        and observation.withdrawn_at is None
        and (
            str(data.get("expected_version", "")) != str(observation.version)
            or data.get("expected_fingerprint") != fingerprint
        )
    )
    context.update(
        form=form,
        recovery_action=action,
        rechecked=rechecked,
        stale=stale,
        back_url=reverse("marketing:ad_detail", args=[observation.pk]),
        page_title="Perbaiki kampanye, tanggal, atau rincian"
        if action == "replace"
        else "Tarik laporan yang keliru",
        submit_label="Simpan pengganti"
        if action == "replace"
        else "Tarik dari perhitungan",
    )
    return render(request, "marketing/ad_recovery_form.html", context)


@require_http_methods(["GET", "POST"])
@organization_required
def ad_withdraw(request, pk):
    return _recover(request, pk, "withdraw")


@require_http_methods(["GET", "POST"])
@organization_required
def ad_replace(request, pk):
    return _recover(request, pk, "replace")
