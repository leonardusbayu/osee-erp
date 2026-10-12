"""Reviewed content and SEO work: record publication, then sourced results."""

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from core.access import organization_required, require_role

from .activity_models import WorkActivity
from .views import (
    READ,
    WRITE,
    _bad,
    _context,
    _form_error,
    _page,
    _pagination_query,
    _version,
)
from .workflow import MANAGERS, can_act_for_member
from .workflow_content import record_measurement, record_publication
from .workflow_content_models import ContentMeasurement
from .workflow_forms import WorkflowForm


class PublicationForm(WorkflowForm):
    expected_latest_id = forms.IntegerField(
        required=False, min_value=1, widget=forms.HiddenInput
    )
    published_at = forms.DateTimeField(label="Waktu benar-benar dipublikasikan")
    url = forms.URLField(label="URL publikasi", max_length=600)
    topic = forms.CharField(label="Topik / query SEO utama", max_length=250)
    evidence = forms.CharField(
        label="Bukti publikasi yang dapat diperiksa",
        max_length=4000,
        widget=forms.Textarea(attrs={"rows": 2}),
    )
    correction_reason = forms.CharField(
        label="Alasan koreksi (untuk versi pengganti)", required=False, max_length=1000
    )
    responsibility_reason = forms.CharField(
        label="Alasan bantuan langsung oleh direktur", required=False, max_length=1000
    )


class MeasurementForm(WorkflowForm):
    publication_id = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    period_start = forms.DateField(label="Awal periode laporan")
    period_end = forms.DateField(label="Akhir periode laporan")
    source = forms.CharField(label="Sumber laporan / properti", max_length=180)
    reference = forms.CharField(label="ID unik laporan sumber", max_length=180)
    evidence = forms.CharField(
        label="Lokasi arsip bukti laporan",
        max_length=4000,
        widget=forms.Textarea(attrs={"rows": 2}),
    )
    impressions = forms.IntegerField(
        label="Tayangan", min_value=0, max_value=2147483647, required=False
    )
    clicks = forms.IntegerField(
        label="Klik", min_value=0, max_value=2147483647, required=False
    )
    visits = forms.IntegerField(
        label="Kunjungan", min_value=0, max_value=2147483647, required=False
    )
    supersedes = forms.ModelChoiceField(
        label="Pengukuran yang dikoreksi (opsional)",
        queryset=ContentMeasurement.objects.none(),
        required=False,
    )
    correction_reason = forms.CharField(
        label="Alasan koreksi", max_length=1000, required=False
    )
    responsibility_reason = forms.CharField(
        label="Alasan bantuan langsung oleh direktur", required=False, max_length=1000
    )

    def __init__(self, *args, publication, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["supersedes"].queryset = ContentMeasurement.objects.filter(
            organization=self.organization,
            publication=publication,
            replacement__isnull=True,
        )
        self.fields["supersedes"].label_from_instance = lambda obj: (
            f"{obj.reference} · {obj.period_start}–{obj.period_end}"
        )


@require_http_methods(["GET", "POST"])
@organization_required
def content_delivery(request, pk):
    require_role(request, *READ)
    work = get_object_or_404(
        WorkActivity.objects.select_related("owner", "current_owner", "reviewed_by"),
        organization=request.organization,
        pk=pk,
        kind__in={"content", "seo"},
    )
    publications = work.publications.select_related("created_by", "supersedes")
    latest = publications.first()
    operation = request.POST.get("operation", "") if request.method == "POST" else ""
    publish_initial = {
        "published_at": timezone.localtime().replace(microsecond=0),
        "expected_latest_id": latest.pk if latest else None,
    }
    if latest:
        publish_initial.update(
            {
                field: getattr(latest, field)
                for field in ("published_at", "url", "topic", "evidence")
            }
        )
    publish_form = PublicationForm(
        request.POST if operation == "publish" else None,
        organization=request.organization,
        initial=publish_initial,
    )
    measure_form = MeasurementForm(
        request.POST if operation == "measure" else None,
        organization=request.organization,
        publication=latest,
        initial={
            "publication_id": latest.pk if latest else None,
            "period_start": timezone.localdate(),
            "period_end": timezone.localdate(),
        },
    )
    if request.method == "POST":
        require_role(request, *WRITE)
        if operation not in {"publish", "measure"}:
            return _bad(ValidationError("Tindakan publikasi tidak valid."))
        form = publish_form if operation == "publish" else measure_form
        if form.is_valid():
            try:
                if operation == "publish":
                    record_publication(
                        organization=request.organization,
                        actor=request.user,
                        activity=work,
                        expected_version=_version(request),
                        **form.cleaned_data,
                    )
                else:
                    fields = form.cleaned_data.copy()
                    publication_id = fields.pop("publication_id")
                    if not latest or publication_id != latest.pk:
                        raise ValidationError(
                            "Publikasi berubah. Muat ulang untuk mencatat pada versi terbaru."
                        )
                    record_measurement(
                        organization=request.organization,
                        actor=request.user,
                        publication=latest,
                        **fields,
                    )
                messages.success(
                    request,
                    "Catatan disimpan beserta sumber dan riwayat. Tidak ada publikasi atau pengambilan data otomatis.",
                )
                return redirect("marketing:content_delivery", pk=pk)
            except ValidationError as exc:
                _form_error(form, exc)
    try:
        page = _page(
            request,
            ContentMeasurement.objects.filter(
                organization=request.organization, publication__activity=work
            ).select_related("publication", "created_by", "supersedes", "replacement"),
        )
    except ValidationError as exc:
        return _bad(exc)
    can_execute = request.membership.role in WRITE and (
        request.membership.role in MANAGERS
        or can_act_for_member(
            organization=request.organization,
            actor=request.user,
            member=work.assigned_owner,
        )
    )
    eligible = (
        work.status == "accepted"
        and not WorkActivity.objects.filter(replaces=work).exists()
    )
    return render(
        request,
        "marketing/content_delivery.html",
        _context(
            request,
            "activities",
            work=work,
            latest=latest,
            publications=publications,
            page=page,
            pagination_query=_pagination_query(request),
            publish_form=publish_form,
            measure_form=measure_form,
            can_execute=can_execute,
            eligible=eligible,
        ),
    )
