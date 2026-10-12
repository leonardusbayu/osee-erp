"""Private Finance inbox for resolving imported-source discrepancies."""

from django import forms
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path
from django.views.decorators.http import require_GET, require_http_methods

from core.access import organization_required, require_role
from core.audit import record
from core.models import Membership

from .models import ImportException, SourceDocument
from .review_models import ImportExceptionReview
from .review_services import exception_state, exception_states, review_exception


class ExceptionReviewForm(forms.Form):
    expected_previous_id = forms.IntegerField(
        required=False, widget=forms.HiddenInput(), min_value=1
    )
    responsible_user = forms.ModelChoiceField(
        label="Penanggung jawab pemeriksaan", queryset=get_user_model().objects.none()
    )
    decision = forms.ChoiceField(
        label="Keputusan",
        choices=ImportExceptionReview._meta.get_field("decision").choices,
    )
    reason = forms.CharField(
        label="Alasan keputusan / pembaruan",
        max_length=4000,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    evidence = forms.CharField(
        label="Referensi bukti dan lokasi yang diperiksa",
        max_length=4000,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Cantumkan halaman, baris, atau referensi pendukung yang dapat diperiksa ulang.",
    )
    correction = forms.CharField(
        label="Interpretasi atau koreksi yang disetujui",
        max_length=4000,
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Wajib jika diselesaikan. Jelaskan nilai sumber dan nilai yang disetujui. Nilai rekap asli tetap tersimpan.",
    )
    source_document = forms.ModelChoiceField(
        label="Dokumen asli yang mendukung keputusan",
        required=False,
        queryset=SourceDocument.objects.none(),
    )
    upload = forms.FileField(
        label="Lampiran bukti pendukung baru (opsional)",
        required=False,
        help_text="PDF, PNG, JPEG maksimal 10 MB, disimpan privat. Penyelesaian wajib memilih dokumen asli atau mengunggah lampiran.",
    )

    def __init__(self, *args, exception, **kwargs):
        super().__init__(*args, **kwargs)
        ids = Membership.objects.filter(
            organization=exception.organization,
            user__is_active=True,
            role__in=["owner", "finance"],
        ).values("user_id")
        self.fields["responsible_user"].queryset = (
            get_user_model().objects.filter(pk__in=ids).order_by("username")
        )
        self.fields["source_document"].queryset = SourceDocument.objects.filter(
            organization=exception.organization, batch=exception.batch
        )
        self.fields["source_document"].label_from_instance = lambda item: (
            item.original_name
        )


@require_GET
@organization_required
def inbox(request):
    require_role(request, "owner", "finance", "auditor")
    selected = request.GET.get("status", "open")
    if selected not in {"open", "resolved", "all"}:
        selected = "open"
    rows = []
    exceptions = list(
        ImportException.objects.filter(organization=request.organization)
        .select_related("batch")
        .order_by("pk")
    )
    states = exception_states(organization=request.organization, exceptions=exceptions)
    for exception in exceptions:
        state = states[exception.pk]
        if selected == "all" or state["status"] == selected:
            rows.append({"exception": exception, **state})
    page = Paginator(rows, 50).get_page(request.GET.get("page"))
    return render(
        request,
        "imports/review_inbox.html",
        {
            "rows": page.object_list,
            "page_obj": page,
            "selected": selected,
            "active_nav": "imports",
        },
    )


@require_http_methods(["GET", "POST"])
@organization_required
def detail(request, pk):
    require_role(request, "owner", "finance", "auditor")
    exception = get_object_or_404(
        ImportException.objects.select_related("batch"),
        organization=request.organization,
        pk=pk,
    )
    state = exception_state(organization=request.organization, exception=exception)
    can_write = request.membership.role in {"owner", "finance"}
    if request.method == "POST":
        require_role(request, "owner", "finance")
    initial = {
        "expected_previous_id": state["previous_id"],
        "decision": "open",
        "responsible_user": state["review"].responsible_user_id
        if state["review"]
        else request.user.pk,
    }
    form = ExceptionReviewForm(
        request.POST if request.method == "POST" else None,
        request.FILES if request.method == "POST" else None,
        exception=exception,
        initial=initial,
    )
    if request.method == "POST" and form.is_valid():
        try:
            review_exception(
                organization=request.organization,
                actor=request.user,
                exception=exception,
                **form.cleaned_data,
            )
            messages.success(
                request,
                "Keputusan tersimpan. Sumber asli tetap utuh; koreksi memiliki riwayat sendiri dan tidak membukukan transaksi.",
            )
            return redirect("import_exception_review", pk=exception.pk)
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(
        request,
        "imports/review_detail.html",
        {
            "exception": exception,
            "state": state,
            "form": form,
            "can_write": can_write,
            "reviews": exception.reviews.select_related(
                "created_by", "responsible_user", "source_document"
            ),
            "active_nav": "imports",
        },
    )


@require_GET
@organization_required
def evidence_download(request, pk):
    require_role(request, "owner", "finance", "auditor")
    review = get_object_or_404(
        ImportExceptionReview, organization=request.organization, pk=pk
    )
    if not review.file:
        raise Http404
    try:
        handle = review.file.open("rb")
    except OSError as exc:
        raise Http404 from exc
    try:
        record(
            request.organization,
            request.user,
            "imports.review.evidence_downloaded",
            obj=review,
            detail={"exception_id": review.exception_id, "sha256": review.sha256},
        )
    except Exception:
        handle.close()
        raise
    response = FileResponse(
        handle,
        as_attachment=True,
        filename=review.original_name,
        content_type="application/octet-stream",
    )
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


urlpatterns = [
    path("", inbox, name="import_exception_inbox"),
    path("<int:pk>/", detail, name="import_exception_review"),
    path("evidence/<int:pk>/", evidence_download, name="import_exception_evidence"),
]
