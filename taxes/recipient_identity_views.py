"""Private, scoped identity register for document-led PPh 23 preparation."""

import hashlib
import uuid
from pathlib import PurePath

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_http_methods

from core.access import organization_required
from core.models import Membership, Organization

from .recipient_identity_forms import RecipientPreparationForm, RecipientReviewForm

STATUS_LABELS = {
    "draft": "Menunggu laporan pemeriksa",
    "approved": "Sudah diperiksa",
    "unresolved": "Belum dapat digunakan untuk PPh 23",
    "stale": "Perlu pemeriksaan baru",
    "superseded": "Versi sebelumnya",
}
TREATMENT_LABELS = {
    "standard_npwp": "NPWP terverifikasi — faktor standar",
    "standard_integrated_nik": "NIK terintegrasi — perlakuan standar diperiksa",
    "increased_no_npwp": "Tidak mempunyai NPWP — faktor kenaikan diperiksa",
    "unresolved": "Identitas belum dapat dipastikan",
}


def authorize(request, *, write=False, approve=False):
    roles = (
        ("owner",)
        if approve
        else ("owner", "finance")
        if write
        else ("owner", "finance", "reviewer", "auditor")
    )
    if not Membership.objects.filter(
        organization=request.organization,
        user=request.user,
        user__is_active=True,
        role__in=roles,
    ).exists():
        raise PermissionDenied(
            "Finance menyiapkan identitas; Owner/Direktur mencatat laporan pemeriksa. Akun pembaca hanya melihat riwayat."
        )


def status_context(organization, identity):
    from .recipient_identity import identity_status
    from .workspace_views import staff_tax_errors

    status = identity_status(organization=organization, identity=identity)
    status["issues"] = staff_tax_errors(status["issues"])
    return {
        **status,
        "identity": identity,
        "status_label": STATUS_LABELS.get(status["status"], "Perlu pemeriksaan"),
        "treatment_label": TREATMENT_LABELS.get(
            status.get("treatment"), "Belum diperiksa"
        ),
    }


@organization_required
@require_GET
def index(request):
    from .models import RecipientTaxIdentity

    authorize(request)
    query = (
        RecipientTaxIdentity.objects.filter(organization=request.organization)
        .select_related("recipient_party")
        .order_by("-created_at", "-pk")
    )
    page = Paginator(query, 20).get_page(request.GET.get("page"))
    cards = [status_context(request.organization, row) for row in page]
    return render(
        request,
        "taxes/recipient_identity_list.html",
        {
            "cards": cards,
            "page": page,
            "can_prepare": request.membership.role in ("owner", "finance"),
            "active_nav": "tax",
        },
    )


@organization_required
@require_http_methods(["GET", "POST"])
def prepare(request):
    from .models import RecipientTaxIdentity
    from .recipient_identity import prepare_recipient_identity
    from .workflow import upload_evidence

    authorize(request, write=True)
    initial = {"request_key": str(uuid.uuid4())}
    if request.GET.get("supplier"):
        from finance.models import Party

        supplier = get_object_or_404(
            Party,
            organization=request.organization,
            pk=request.GET["supplier"],
            kind="supplier",
        )
        initial.update(
            target="supplier", recipient_party=supplier, legal_name=supplier.name
        )
    elif request.GET.get("target") == "organization":
        initial.update(
            target="organization",
            legal_name=request.organization.name,
            recipient_type="body",
            population_status="na",
        )
    if request.GET.get("previous"):
        previous = get_object_or_404(
            RecipientTaxIdentity,
            organization=request.organization,
            pk=request.GET["previous"],
        )
        initial.update(
            previous=previous if hasattr(previous, "approval") else None,
            target="supplier" if previous.recipient_party_id else "organization",
            recipient_party=previous.recipient_party,
            legal_name=previous.legal_name,
            recipient_type=previous.recipient_type,
        )
        if not hasattr(previous, "approval") or previous.approval.factor is None:
            for key in (
                "name_variation_reason",
                "tax_residency",
                "population_status",
                "identity_type",
                "number",
                "registration_status",
                "nik_validated",
                "nik_integrated",
                "nik_activated",
                "effective_from",
                "effective_until",
            ):
                initial[key] = getattr(previous, key)
    form = RecipientPreparationForm(
        request.POST if request.method == "POST" else None,
        request.FILES or None,
        organization=request.organization,
        initial=initial,
    )
    if request.method == "POST" and form.is_valid():
        values = dict(form.cleaned_data)
        values.pop("target")
        file = values.pop("file")
        try:
            with transaction.atomic():
                Organization.objects.select_for_update().get(pk=request.organization.pk)
                if file:
                    old = (
                        RecipientTaxIdentity.objects.filter(
                            organization=request.organization,
                            request_key=values["request_key"],
                        )
                        .select_related("evidence")
                        .first()
                    )
                    if old:
                        content = file.read(2 * 1024 * 1024 + 1)
                        filename = PurePath(str(file.name).replace("\\", "/")).name[
                            :200
                        ]
                        if (
                            filename != old.evidence.original_filename
                            or hashlib.sha256(content).hexdigest()
                            != old.evidence.sha256
                            or hashlib.sha256(bytes(old.evidence.content)).hexdigest()
                            != old.evidence.sha256
                        ):
                            raise ValidationError(
                                "Berkas pada permintaan ini berubah. Siapkan pemeriksaan baru dengan dokumen yang tepat."
                            )
                        values["evidence"] = old.evidence
                    else:
                        values["evidence"] = upload_evidence(
                            organization=request.organization,
                            actor=request.user,
                            file=file,
                        )
                identity = prepare_recipient_identity(
                    organization=request.organization, actor=request.user, **values
                )
        except ValidationError as exc:
            from .workspace_views import staff_tax_errors

            form.add_error(None, staff_tax_errors(exc))
        else:
            messages.success(
                request,
                "Dokumen identitas tersimpan. Owner/Direktur mencatat laporan profesional sebelum identitas dipakai untuk PPh 23.",
            )
            return redirect("taxes:recipient_identity", pk=identity.pk)
    groups = [
        {
            "title": "1. Pilih penerima penghasilan",
            "keys": [
                "target",
                "recipient_party",
                "previous",
                "legal_name",
                "name_variation_reason",
                "recipient_type",
            ],
        },
        {
            "title": "2. Salin status yang didukung dokumen",
            "keys": [
                "tax_residency",
                "population_status",
                "identity_type",
                "number",
                "registration_status",
                "nik_validated",
                "nik_integrated",
                "nik_activated",
            ],
        },
        {
            "title": "3. Simpan tanggal dan bukti",
            "keys": [
                "effective_from",
                "effective_until",
                "reference",
                "reason",
                "evidence",
                "file",
            ],
        },
    ]
    for group in groups:
        group["fields"] = [form[key] for key in group["keys"]]
    return render(
        request,
        "taxes/recipient_identity_prepare.html",
        {"form": form, "groups": groups, "active_nav": "tax"},
    )


@organization_required
@require_GET
def detail(request, pk):
    from .models import RecipientTaxIdentity

    authorize(request)
    identity = get_object_or_404(
        RecipientTaxIdentity.objects.select_related(
            "recipient_party", "evidence", "prepared_by"
        ),
        organization=request.organization,
        pk=pk,
    )
    status = status_context(request.organization, identity)
    history = RecipientTaxIdentity.objects.filter(
        organization=request.organization,
        recipient_party_id=identity.recipient_party_id,
    ).order_by("-revision", "-created_at")
    return render(
        request,
        "taxes/recipient_identity_detail.html",
        {
            **status,
            "history": history,
            "can_prepare": request.membership.role in ("owner", "finance"),
            "can_review": request.membership.role == "owner"
            and status["status"] == "draft",
            "active_nav": "tax",
        },
    )


@organization_required
@require_http_methods(["GET", "POST"])
def review(request, pk):
    from .models import RecipientTaxIdentity
    from .recipient_identity import approve_recipient_identity, identity_decision
    from .workflow import record_external_review
    from .workspace_views import review_fields

    authorize(request, approve=True)
    identity = get_object_or_404(
        RecipientTaxIdentity.objects.select_related("recipient_party", "evidence"),
        organization=request.organization,
        pk=pk,
    )
    status = status_context(request.organization, identity)
    form = RecipientReviewForm(
        request.POST if request.method == "POST" else None,
        organization=request.organization,
        initial={
            "expected_fingerprint": status["fingerprint"],
            "expected_version": status["version"],
        },
    )
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            with transaction.atomic():
                decision = identity_decision(treatment=data["treatment"])
                report = record_external_review(
                    organization=request.organization,
                    actor=request.user,
                    subject=identity,
                    decision=decision,
                    **review_fields(data),
                )
                approve_recipient_identity(
                    organization=request.organization,
                    actor=request.user,
                    identity=identity,
                    treatment=data["treatment"],
                    external_review=report,
                    expected_fingerprint=data["expected_fingerprint"],
                    expected_version=data["expected_version"],
                )
        except ValidationError as exc:
            from .workspace_views import staff_tax_errors

            form.add_error(None, staff_tax_errors(exc))
            status = status_context(request.organization, identity)
        else:
            messages.success(
                request,
                "Laporan dan keputusan identitas tercatat untuk versi ini. PPh 23 tetap memerlukan pemeriksaan objek dan tanggal terutang.",
            )
            return redirect("taxes:recipient_identity", pk=identity.pk)
    return render(
        request,
        "taxes/recipient_identity_review.html",
        {**status, "form": form, "active_nav": "tax"},
    )
