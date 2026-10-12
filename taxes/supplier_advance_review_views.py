from uuid import uuid4

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from core.access import organization_required
from core.models import Membership
from finance.models import SupplierAdvance

from . import supplier_advance_review as service
from .supplier_advance_review_forms import SupplierAdvanceReviewForm


def _initial(context):
    return {
        "source_fingerprint": context["source_fingerprint"],
        "expected_version": context["version"],
        "expected_fingerprint": context["fingerprint"],
        "request_key": str(uuid4()),
    }


@organization_required
@require_http_methods(["GET", "POST"])
def review(request, pk):
    roles = ("owner", "finance", "reviewer", "auditor", "director")
    if not request.user.is_active or request.membership.role not in roles:
        raise PermissionDenied(
            "Akun ini tidak memiliki akses pemeriksaan uang muka pemasok."
        )
    row = get_object_or_404(SupplierAdvance, organization=request.organization, pk=pk)
    context = service.supplier_advance_tax_context(
        organization=request.organization, advance=row
    )
    can_prepare = request.membership.role in ("owner", "finance")
    can_approve = Membership.objects.filter(
        organization=request.organization,
        user=request.user,
        user__is_active=True,
        role="owner",
    ).exists()
    form, status = None, 200
    if request.method == "POST":
        if not can_prepare:
            raise PermissionDenied(
                "Finance dapat menyiapkan laporan; Owner aktif mencatat kesimpulan profesional."
            )
        # A duplicate hidden/decision value cannot change meaning according to
        # which consumer reads the first or last value in a QueryDict.
        allowed = set(SupplierAdvanceReviewForm.base_fields) | {
            "operation",
            "csrfmiddlewaretoken",
        }
        duplicate = any(len(request.POST.getlist(key)) != 1 for key in request.POST)
        unknown = set(request.POST) - allowed
        operation = request.POST.get("operation", "save")
        if duplicate or unknown or operation not in ("save", "recheck"):
            form = SupplierAdvanceReviewForm(
                request.POST, organization=request.organization, context=context
            )
            form.is_valid()
            form.add_error(
                None,
                "Permintaan berisi nilai ganda atau isian yang tidak didukung. Periksa kembali isian Anda.",
            )
            status = 400
        elif operation == "recheck":
            retained = request.POST.copy()
            for key, value in _initial(context).items():
                retained[key] = value
            retained.pop("report_confirmed", None)
            form = SupplierAdvanceReviewForm(
                retained, organization=request.organization, context=context
            )
            form.fields["report_confirmed"].required = False
        else:
            if not can_approve:
                raise PermissionDenied(
                    "Owner aktif perusahaan ini diperlukan untuk mencatat keputusan laporan profesional."
                )
            form = SupplierAdvanceReviewForm(
                request.POST, organization=request.organization, context=context
            )
            status = 400
            if form.is_valid():
                data = form.cleaned_data
                try:
                    service.record_supplier_advance_review(
                        organization=request.organization,
                        actor=request.user,
                        advance=context["advance"],
                        decision=form.decision(),
                        **{
                            key: data[key]
                            for key in (
                                "evidence",
                                "professional_name",
                                "qualification_reference",
                                "reviewed_on",
                                "statement",
                                "report_confirmed",
                                "expected_version",
                                "expected_fingerprint",
                                "request_key",
                            )
                        },
                    )
                except ValidationError as exc:
                    form.add_error(None, exc.messages)
                else:
                    messages.success(
                        request,
                        "Kesimpulan laporan profesional tersimpan. Periksa status dan riwayat peristiwa kas ini.",
                    )
                    return redirect(
                        "supplier_advance_tax:review", pk=context["advance"].pk
                    )
    elif can_prepare:
        form = SupplierAdvanceReviewForm(
            organization=request.organization,
            context=context,
            initial=_initial(context),
        )
    return render(
        request,
        "taxes/supplier_advance_review.html",
        {
            "form": form,
            "review_context": context,
            "advance": context["advance"],
            "can_prepare": can_prepare,
            "can_approve": can_approve,
            "back_url": reverse(
                "supplier_advance_detail", args=[context["advance"].pk]
            ),
            "active_nav": "tax",
        },
        status=status,
    )
