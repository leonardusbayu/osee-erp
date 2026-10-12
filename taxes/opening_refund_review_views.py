from uuid import uuid4

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from core.access import organization_required
from core.models import Membership
from finance.models import OpeningBalanceRefund

from . import opening_refund_review as service
from .opening_refund_review_forms import OpeningRefundReviewForm


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
            "Akun ini tidak memiliki akses pemeriksaan pengembalian uang saldo awal."
        )
    row = get_object_or_404(
        OpeningBalanceRefund, organization=request.organization, pk=pk
    )
    context = service.opening_refund_review_context(
        organization=request.organization, refund=row
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
        allowed = set(OpeningRefundReviewForm.base_fields) | {
            "operation",
            "csrfmiddlewaretoken",
        }
        duplicate = any(len(request.POST.getlist(key)) != 1 for key in request.POST)
        unknown = set(request.POST) - allowed
        operation = request.POST.get("operation", "save")
        if duplicate or unknown or operation not in ("save", "recheck"):
            form = OpeningRefundReviewForm(
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
            form = OpeningRefundReviewForm(
                retained, organization=request.organization, context=context
            )
            form.fields["report_confirmed"].required = False
        else:
            if not can_approve:
                raise PermissionDenied(
                    "Owner aktif perusahaan ini diperlukan untuk mencatat keputusan laporan profesional."
                )
            form = OpeningRefundReviewForm(
                request.POST, organization=request.organization, context=context
            )
            status = 400
            if form.is_valid():
                data = form.cleaned_data
                try:
                    service.record_opening_refund_review(
                        organization=request.organization,
                        actor=request.user,
                        refund=context["refund"],
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
                        "opening_refund_tax:review", pk=context["refund"].pk
                    )
    elif can_prepare:
        form = OpeningRefundReviewForm(
            organization=request.organization,
            context=context,
            initial=_initial(context),
        )
    return render(
        request,
        "taxes/opening_refund_review.html",
        {
            "form": form,
            "review_context": context,
            "refund": context["refund"],
            "can_prepare": can_prepare,
            "can_approve": can_approve,
            "back_url": reverse(
                "opening_balance_detail", args=[context["refund"].item_id]
            ),
            "active_nav": "tax",
        },
        status=status,
    )
