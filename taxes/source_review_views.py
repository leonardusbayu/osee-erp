from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from core.access import organization_required

from .knowledge import (
    activate_source_revision,
    current_sources,
    pending_source_revisions,
)
from .models import TaxSource, TaxSourcePublication
from .source_review_forms import SourcePublicationForm


def _not_ready_reason(source, today):
    if not source.approved:
        return "Belum ada versi yang diterbitkan setelah pemeriksaan."
    if (today - source.reviewed_on).days > 180:
        return "Tanggal pemeriksaan sudah lebih dari 180 hari."
    if source.effective_from and source.effective_from > today:
        return f"Masa berlaku baru mulai {source.effective_from:%d %B %Y}."
    if source.effective_until and source.effective_until < today:
        return f"Masa berlaku berakhir {source.effective_until:%d %B %Y}."
    return "Versi yang diterbitkan tidak cocok dengan isi sumber saat ini."


def _publish_reviewed_revision(request, pending):
    form = SourcePublicationForm(request.POST, revisions=pending)
    if not form.is_valid():
        return form, None
    try:
        activate_source_revision(
            revision=form.cleaned_data["revision"],
            reviewer_name=form.cleaned_data["reviewer_name"],
            review_reference=form.cleaned_data["review_reference"],
            confirm_reviewed=form.cleaned_data["confirm_reviewed"],
            actor=request.user,
        )
    except ValidationError as exc:
        form.add_error(None, exc)
        return form, None
    messages.success(
        request,
        "Rujukan umum diterbitkan. Profil dan catatan pajak perusahaan tidak berubah.",
    )
    return None, redirect("taxes:source_reviews")


def _source_status_rows(pending):
    today = timezone.localdate()
    active_sources = {source.slug: source for source in current_sources()}
    active_version_ids = [source.version_id for source in active_sources.values()]
    publications = {
        publication.revision_id: publication
        for publication in TaxSourcePublication.objects.filter(
            revision_id__in=active_version_ids
        ).select_related("published_by", "revision")
    }
    pending_by_source = {revision.source_id: revision for revision in pending}
    rows = []
    for source in TaxSource.objects.order_by("title"):
        active = active_sources.get(source.slug)
        revision = pending_by_source.get(source.pk)
        publication = publications.get(active.version_id) if active else None
        audit_detail = _publication_audit_detail(publication)
        if active and revision:
            status = "Aktif · pembaruan menunggu tinjauan"
            detail = (
                "Versi aktif tetap dipakai sampai versi baru ditinjau dan diterbitkan. "
                + audit_detail
            )
        elif active:
            status = "Aktif"
            detail = f"Ditinjau {source.reviewed_on:%d %B %Y}. {audit_detail}"
        elif revision:
            status = "Menunggu pemeriksaan"
            detail = "Versi baru belum digunakan dalam panduan pajak."
        else:
            status = "Perlu pemeriksaan"
            detail = _not_ready_reason(source, today)
        rows.append(
            {
                "title": source.title,
                "url": source.url,
                "status": status,
                "detail": detail,
            }
        )
    return rows


def _publication_audit_detail(publication):
    if not publication:
        return ""
    publisher = publication.published_by
    publisher_label = (
        (publisher.get_full_name() or publisher.get_username())
        if publisher
        else "Akun penerbit tidak tercatat pada versi ini"
    )
    return (
        f"Pemeriksa: {publication.reviewer_name}. "
        f"Penerbit: {publisher_label}. "
        f"Catatan: {publication.review_reference}."
    )


@organization_required
@require_http_methods(["GET", "POST"])
def source_reviews(request):
    if request.membership.role not in ("owner", "finance"):
        raise PermissionDenied("Halaman ini hanya untuk Owner dan Finance.")
    if request.method == "POST" and not request.user.is_staff:
        raise PermissionDenied("Hanya administrator sistem dapat menerbitkan sumber.")

    pending = pending_source_revisions()
    if request.method == "POST":
        form, response = _publish_reviewed_revision(request, pending)
        if response:
            return response
    else:
        form = (
            SourcePublicationForm(revisions=pending) if request.user.is_staff else None
        )

    return render(
        request,
        "taxes/source_reviews.html",
        {
            "active_nav": "tax",
            "form": form,
            "pending_revisions": pending,
            "publication_history": TaxSourcePublication.objects.select_related(
                "revision__source", "published_by"
            ).order_by("-created_at", "-pk")[:50],
            "status_rows": _source_status_rows(pending),
        },
    )
