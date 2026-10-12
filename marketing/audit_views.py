"""Read-only marketing history and a scoped, spreadsheet-safe evidence export."""

import csv
import json
from datetime import timedelta

from django import forms
from django.contrib.auth import get_user_model
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from core.access import organization_required, require_role
from core.audit import record
from core.models import AuditEvent

from .views import READ, _context, _page, _pagination_query

OBJECTS = {
    "KpiTarget": ("Target KPI", "marketing:kpi_detail"),
    "KpiReview": ("Evaluasi KPI", None),
    "WorkActivity": ("Bukti pekerjaan", "marketing:activity_detail"),
    "Lead": ("Prospek", "marketing:lead_detail"),
    "Campaign": ("Kampanye", "marketing:campaign_detail"),
    "AdDailyObservation": ("Laporan iklan", "marketing:ad_detail"),
    "AdObservationRecovery": ("Pemeriksaan laporan iklan", None),
    "SourceCoverage": ("Cakupan sumber", None),
    "TeamMember": ("Anggota tim", "marketing:member_detail"),
    "LeadAssignment": ("Riwayat penugasan", None),
    "LeadContact": ("Kontak prospek", None),
    "MemberDelegation": ("Delegasi PIC", None),
    "CampaignEvent": ("Keputusan kampanye", None),
    "WorkPolicy": ("Aturan jam kerja & SLA", None),
    "KpiSourcePeriod": ("Pemeriksaan sumber KPI", "marketing:source_period_detail"),
    "ActivityEvidence": ("Lampiran bukti pekerjaan", None),
    "ContentPublication": ("Publikasi Konten/SEO", None),
    "ContentMeasurement": ("Pengukuran Konten/SEO", None),
    "MarketingAction": ("Tindakan manager", "marketing:action_detail"),
    "MarketingAIPolicy": ("Izin AI", None),
}
ACTION_LABELS = {
    "created": "Dibuat",
    "updated": "Diperbarui",
    "corrected": "Dikoreksi",
    "activated": "Target disahkan",
    "approved": "Disetujui",
    "retired": "Target dihentikan",
    "reviewed": "Evaluasi disimpan",
    "started": "Mulai dikerjakan",
    "handed_off": "Pekerjaan dialihkan",
    "submitted": "Bukti diajukan",
    "accepted": "Bukti diterima",
    "returned": "Perlu perbaikan",
    "cancelled": "Dibatalkan",
    "confirmed": "Dikonfirmasi",
    "withdrawn": "Ditarik",
    "invoice_linked": "Invoice dihubungkan",
    "policy_changed": "Izin AI diubah",
    "exported": "Riwayat diunduh",
    "replaced": "Diganti",
    "assigned": "PIC ditugaskan",
    "configured": "Akun/PIC diatur",
    "delegated": "Pengganti ditunjuk",
    "delegation_revoked": "Delegasi dicabut",
    "contact_recorded": "Kontak dicatat",
    "contact_corrected": "Kontak dikoreksi",
    "pause": "Kampanye dijeda",
    "resume": "Kampanye dilanjutkan",
    "complete": "Kampanye diselesaikan",
    "handover": "Pengelola dialihkan",
    "published": "Publikasi dicatat",
    "publication_corrected": "Publikasi dikoreksi",
    "measured": "Pengukuran dicatat",
    "measurement_corrected": "Pengukuran dikoreksi",
}
EXPORT_LIMIT = 5000


class AuditFilterForm(forms.Form):
    start = forms.DateField(
        label="Dari tanggal",
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    end = forms.DateField(
        label="Sampai tanggal",
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    actor = forms.ModelChoiceField(
        label="Dilakukan oleh", required=False, queryset=get_user_model().objects.none()
    )
    object_type = forms.ChoiceField(
        label="Jenis catatan",
        required=False,
        choices=[
            ("", "Semua catatan marketing"),
            *[(key, value[0]) for key, value in OBJECTS.items()],
        ],
    )
    object_id = forms.IntegerField(label="Nomor catatan", min_value=1, required=False)
    action = forms.ChoiceField(label="Tindakan", required=False)

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        events = AuditEvent.objects.filter(
            organization=organization, action__startswith="marketing."
        )
        self.fields["actor"].queryset = (
            get_user_model()
            .objects.filter(pk__in=events.values("actor_id"))
            .order_by("username")
        )
        actions = events.order_by("action").values_list("action", flat=True).distinct()
        self.fields["action"].choices = [
            ("", "Semua tindakan"),
            *[(action, action) for action in actions],
        ]

    def clean(self):
        data = super().clean()
        if data.get("start") and data.get("end") and data["start"] > data["end"]:
            raise forms.ValidationError(
                "Tanggal akhir tidak boleh sebelum tanggal awal."
            )
        return data


def _filtered(request, values):
    # Object drilldowns intentionally include all history unless dates are supplied.
    values = values.copy()
    if not any(key in values for key in ("start", "end", "object_type", "object_id")):
        values["start"] = (timezone.localdate() - timedelta(days=30)).isoformat()
        values["end"] = timezone.localdate().isoformat()
    form = AuditFilterForm(values, organization=request.organization)
    events = AuditEvent.objects.filter(
        organization=request.organization, action__startswith="marketing."
    ).select_related("actor")
    if not form.is_valid():
        return form, events.none()
    data = form.cleaned_data
    if data.get("object_type") == "KpiTarget" and data.get("object_id"):
        events = events.filter(
            Q(object_type="KpiTarget", object_id=str(data["object_id"]))
            | Q(
                object_type__in={"KpiReview", "KpiSourcePeriod"},
                detail__target_id=data["object_id"],
            )
        )
        keys = ("actor", "action")
    elif data.get("object_type") == "WorkActivity" and data.get("object_id"):
        events = events.filter(
            Q(object_type="WorkActivity", object_id=str(data["object_id"]))
            | Q(object_type="ActivityEvidence", detail__work_id=data["object_id"])
            | Q(
                object_type__in={"ContentPublication", "ContentMeasurement"},
                detail__activity_id=data["object_id"],
            )
        )
        keys = ("actor", "action")
    elif data.get("object_type") == "Lead" and data.get("object_id"):
        events = events.filter(
            Q(object_type="Lead", object_id=str(data["object_id"]))
            | Q(
                object_type__in={"LeadContact", "LeadAssignment"},
                detail__lead_id=data["object_id"],
            )
        )
        keys = ("actor", "action")
    elif data.get("object_type") == "TeamMember" and data.get("object_id"):
        events = events.filter(
            Q(object_type="TeamMember", object_id=str(data["object_id"]))
            | Q(object_type="MemberDelegation", detail__member_id=data["object_id"])
        )
        keys = ("actor", "action")
    else:
        keys = ("actor", "object_type", "object_id", "action")
    for key in keys:
        if data.get(key) not in (None, ""):
            events = events.filter(
                **{key: str(data[key]) if key == "object_id" else data[key]}
            )
    if data.get("start"):
        events = events.filter(created_at__date__gte=data["start"])
    if data.get("end"):
        events = events.filter(created_at__date__lte=data["end"])
    return form, events


def _decorate(event, role):
    label, route = OBJECTS.get(event.object_type, ("Marketing", None))
    event.record_label = label
    event.action_label = ACTION_LABELS.get(
        event.action.rsplit(".", 1)[-1], event.action
    )
    event.record_url = None
    if route and event.object_id.isdigit():
        event.record_url = reverse(route, args=[int(event.object_id)])
    if event.object_type == "KpiReview" and type(event.detail.get("target_id")) is int:
        event.record_url = reverse(
            "marketing:kpi_detail", args=[event.detail["target_id"]]
        )
    parents = {
        "AdObservationRecovery": ("predecessor_id", "marketing:ad_detail"),
        "LeadContact": ("lead_id", "marketing:lead_history"),
        "LeadAssignment": ("lead_id", "marketing:lead_history"),
        "MemberDelegation": ("member_id", "marketing:member_detail"),
        "ActivityEvidence": ("work_id", "marketing:activity_detail"),
        "ContentPublication": ("activity_id", "marketing:content_delivery"),
        "ContentMeasurement": ("activity_id", "marketing:content_delivery"),
    }
    if event.object_type in parents:
        key, target = parents[event.object_type]
        if type(event.detail.get(key)) is int:
            event.record_url = reverse(target, args=[event.detail[key]])
    event.details_text = json.dumps(
        event.detail, ensure_ascii=False, indent=2, sort_keys=True
    )
    event.reason = (
        event.detail.get("reason")
        or event.detail.get("conclusion")
        or event.detail.get("review_notes")
        or ""
    )
    return event


@require_GET
@organization_required
def audit(request):
    require_role(request, *READ)
    form, events = _filtered(request, request.GET)
    page = None
    if form.is_valid():
        from django.core.exceptions import ValidationError

        try:
            page = _page(request, events)
        except ValidationError as exc:
            form.add_error(None, exc)
    if page is not None:
        for event in page:
            _decorate(event, request.membership.role)
    return render(
        request,
        "marketing/audit.html",
        _context(
            request,
            "audit",
            filter_form=form,
            page=page,
            pagination_query=_pagination_query(request),
            export_limit=EXPORT_LIMIT,
        ),
        status=200 if not form.errors else 400,
    )


def csv_cell(value):
    value = "" if value is None else str(value)
    # Quoting a CSV field does not neutralize spreadsheet formulas.
    if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(
        ("\t", "\r", "\n")
    ):
        return "'" + value
    return value


@require_POST
@organization_required
def audit_export(request):
    require_role(request, *READ)
    form, events = _filtered(request, request.POST)
    if not form.is_valid():
        return HttpResponse(
            "Filter unduhan tidak valid. Periksa kembali filter riwayat.", status=400
        )
    rows = list(events[: EXPORT_LIMIT + 1])
    if len(rows) > EXPORT_LIMIT:
        return HttpResponse(
            f"Hasil melebihi {EXPORT_LIMIT} perubahan. Persempit tanggal atau jenis catatan sebelum mengunduh.",
            status=400,
        )
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = (
        'attachment; filename="osee-riwayat-marketing.csv"'
    )
    response["Cache-Control"] = "private, no-store"
    response.write("\ufeff")
    writer = csv.writer(response)
    writer.writerow(
        [
            "ID perubahan",
            "Waktu WIB",
            "Pelaku",
            "Tindakan",
            "Jenis catatan",
            "Nomor catatan",
            "Rincian perubahan (JSON)",
        ]
    )
    for event in rows:
        writer.writerow(
            [
                csv_cell(value)
                for value in (
                    event.pk,
                    timezone.localtime(event.created_at).isoformat(),
                    event.actor.get_username()
                    if event.actor
                    else "Akun tidak tersedia",
                    event.action,
                    event.object_type,
                    event.object_id,
                    json.dumps(event.detail, ensure_ascii=False, sort_keys=True),
                )
            ]
        )
    filters = {
        key: (value.pk if hasattr(value, "pk") else str(value))
        for key, value in form.cleaned_data.items()
        if value not in (None, "")
    }
    record(
        request.organization,
        request.user,
        "marketing.audit.exported",
        detail={"rows": len(rows), "filters": filters},
    )
    return response
