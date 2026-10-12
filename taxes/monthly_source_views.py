"""Scoped monthly preparation screens for Finance and document-led Owner review."""

import csv
import io
import json
from datetime import date
from decimal import Decimal

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods

from core.access import organization_required
from core.audit import record
from core.models import Membership

from . import monthly_source_workflow as service
from .models import MonthlyTaxSourcePackage
from .monthly_source_forms import (
    AdjustmentFormSet,
    GenerateForm,
    MonthForm,
    ReviewCorrectionForm,
    SourceReviewForm,
)

SOURCE_LABELS = {
    "book_revenue": "Pendapatan dalam buku",
    "invoice_delivery": "Layanan selesai",
    "delivery": "Layanan selesai",
    "invoice_credit": "Koreksi layanan",
    "credit": "Koreksi layanan",
    "invoice_cancellation": "Pembatalan",
    "cancellation": "Pembatalan",
    "allocation": "Pembayaran pelanggan",
    "receipt": "Pembayaran pelanggan",
    "customer_tax_settlement": "Pembayaran dan bukti potong pelanggan",
    "customer_refund": "Pengembalian pembayaran",
    "credit_refund": "Pengembalian koreksi",
    "advance_application": "Uang muka yang dipakai",
    "invoice": "Invoice",
    "invoice_issue": "Invoice diterbitkan",
    "legacy_delivery": "Layanan selesai",
    "invoice-credit": "Koreksi layanan",
    "invoice-cancellation": "Pembatalan",
    "customer-advance": "Uang muka diterima",
    "advance-application": "Uang muka yang dipakai",
    "customer-tax": "Pelunasan dengan bukti potong",
    "refund": "Pengembalian pembayaran",
    "credit-refund": "Pengembalian koreksi",
    "source-observation": "Dokumen sumber impor",
    "opening-receipt": "Penerimaan piutang lama",
    "opening-refund": "Pengembalian uang saldo lama",
}
GROSS_LABELS = {
    "invoice_issue": "Nilai invoice",
    "delivery": "Nilai layanan",
    "legacy_delivery": "Nilai layanan",
    "invoice-credit": "Nilai koreksi",
    "invoice-cancellation": "Nilai pembatalan",
    "refund": "Nilai pengembalian",
    "credit-refund": "Nilai pengembalian",
    "receipt": "Nilai pelunasan invoice",
    "advance-application": "Nilai uang muka yang dialokasikan",
    "customer-tax": "Pelunasan invoice termasuk bukti potong",
}
BANK_SOURCE_KINDS = {
    "receipt",
    "customer-advance",
    "advance-application",
    "customer-tax",
    "refund",
    "credit-refund",
    "opening-receipt",
    "opening-refund",
}


def source_cards(organization, package):
    from finance.models import Invoice, OpeningBalanceItem

    rows = package.snapshot.get("rows", [])
    invoice_ids = {row.get("invoice_id") for row in rows if row.get("invoice_id")}
    invoices = {
        row.pk: row
        for row in Invoice.objects.filter(
            organization=organization, pk__in=invoice_ids
        ).only("pk", "number")
    }
    item_ids = {
        row.get("source_item_id")
        for row in rows
        if row.get("source_kind") in ("opening-receipt", "opening-refund")
    }
    opening_items = {
        row.pk: row
        for row in OpeningBalanceItem.objects.filter(
            organization=organization, pk__in=item_ids
        ).only("pk", "source_reference")
    }
    cards = []
    for row in rows:
        try:
            display_date = date.fromisoformat(row.get("date", ""))
        except (ValueError, TypeError):
            display_date = None
        kind = row.get("source_kind")
        cards.append(
            {
                **row,
                "display_date": display_date,
                "label": SOURCE_LABELS.get(kind, "Catatan pembukuan"),
                "opening_item": opening_items.get(row.get("source_item_id")),
                "invoice": invoices.get(row.get("invoice_id")),
                "gross_label": GROSS_LABELS.get(kind, "Nilai sumber"),
                "show_cash": kind in BANK_SOURCE_KINDS,
            }
        )
    return cards


def visible_issues(status):
    if status["approval"] and not status["stale"]:
        return [
            issue
            for issue in status["issues"]
            if issue.get("hard_blocker") is not False
        ]
    return status["issues"]


def approved_bridge(organization, package, approval, cards):
    if not approval:
        return None
    from .models import TaxEvidence

    source = {
        card["row_id"]: {**card, "number": number}
        for number, card in enumerate(cards, 1)
    }
    adjustments = approval.decision.get("adjustments", [])
    evidence = {
        row.pk: row
        for row in TaxEvidence.objects.filter(
            organization=organization,
            pk__in=[item["evidence_id"] for item in adjustments],
        ).defer("content")
    }
    labels = {
        "timing": "Perbedaan waktu pengakuan",
        "gross_basis": "Perbedaan nilai kontrak / bruto",
        "correction": "Koreksi sesuai laporan",
    }
    choices = {
        "include": "Masuk dasar yang diperiksa",
        "exclude": "Tidak dihitung pada bulan ini",
        "adjustment": "Dipakai pada penyesuaian berdokumen",
    }
    return {
        "book_amount": sum(
            (
                Decimal(row["amount"])
                for row in approval.result["allocations"]
                if row["kind"] == "book"
            ),
            Decimal("0.00"),
        ),
        "opening_cash_amount": sum(
            (
                Decimal(row["amount"])
                for row in approval.result["allocations"]
                if row["kind"] == "opening_cash"
            ),
            Decimal("0.00"),
        ),
        "amount": approval.result["reviewed_bridge_amount"],
        "adjustments": [
            {
                **item,
                "label": labels[item["kind"]],
                "evidence": evidence.get(item["evidence_id"]),
                "sources": [
                    source[ref] for ref in item["source_row_ids"] if ref in source
                ],
            }
            for item in adjustments
        ],
        "dispositions": [
            {
                **item,
                "source": source.get(item["row_id"]),
                "label": choices[item["decision"]],
            }
            for item in approval.decision["rows"]
        ],
    }


def history_cards(organization, package):
    from finance.models import Invoice

    history = package.profile_snapshot.get("fiscal_history", [])
    invoice_ids = {
        row.get("invoice_id")
        for item in history
        for row in item["allocations"] + item.get("invoice_allocations", [])
        if row.get("invoice_id")
    }
    invoices = {
        row.pk: row
        for row in Invoice.objects.filter(
            organization=organization, pk__in=invoice_ids
        ).only("pk", "number")
    }
    visible_packages = set(
        MonthlyTaxSourcePackage.objects.filter(
            organization=organization, pk__in=[item["package_id"] for item in history]
        ).values_list("pk", flat=True)
    )
    labels = {
        "book": "Pendapatan buku yang sudah diperiksa",
        "timing": "Penghasilan yang sudah dialokasikan waktunya",
        "opening_cash": "Penerimaan piutang lama yang sudah diperiksa",
        "gross_basis": "Selisih bruto yang sudah diperiksa",
        "correction": "Koreksi yang sudah diperiksa",
    }
    return [
        {
            **item,
            "allocations": [
                {
                    **row,
                    "label": labels.get(row["kind"], "Alokasi yang sudah diperiksa"),
                    "invoice": invoices.get(row.get("invoice_id")),
                }
                for row in item["allocations"]
            ],
            "projections": [
                {**row, "invoice": invoices.get(row.get("invoice_id"))}
                for row in item.get("invoice_allocations", [])
            ],
        }
        for item in history
        if item["package_id"] in visible_packages
    ]


def authorize_read(request):
    if not Membership.objects.filter(
        organization=request.organization,
        user=request.user,
        user__is_active=True,
        role__in=("owner", "finance", "reviewer", "auditor"),
    ).exists():
        raise PermissionDenied(
            "Akun ini tidak memiliki akses ke data pajak perusahaan."
        )


def authorize_write(request, *, approve=False):
    roles = ("owner",) if approve else ("owner", "finance")
    if not Membership.objects.filter(
        organization=request.organization,
        user=request.user,
        user__is_active=True,
        role__in=roles,
    ).exists():
        raise PermissionDenied(
            "Persiapan dilakukan Finance; persetujuan laporan pemeriksa dicatat Owner/Direktur."
        )


@organization_required
@require_http_methods(["GET", "POST"])
def index(request):
    authorize_read(request)
    if request.method == "POST":
        authorize_write(request)
    form = MonthForm(
        request.POST if request.method == "POST" else request.GET or None,
        initial={"period": timezone.localdate().strftime("%Y-%m")},
    )
    if request.method == "POST" and form.is_valid():
        try:
            package = service.prepare_monthly_package(
                organization=request.organization,
                actor=request.user,
                period=form.cleaned_data["period"],
            )
        except ValidationError as exc:
            form.add_error(None, exc.messages)
        else:
            return redirect("taxes:monthly_source", pk=package.pk)
    versions = MonthlyTaxSourcePackage.objects.filter(
        organization=request.organization
    ).order_by("-period", "-revision")
    if form.is_bound and form.is_valid():
        versions = versions.filter(period=form.cleaned_data["period"])
    page = Paginator(versions, 20).get_page(request.GET.get("page"))
    return render(
        request,
        "taxes/monthly_sources/index.html",
        {
            "form": form,
            "versions": page,
            "can_prepare": request.membership.role in ("owner", "finance"),
            "active_nav": "tax",
        },
    )


@organization_required
@require_GET
def detail(request, pk):
    authorize_read(request)
    package = get_object_or_404(
        MonthlyTaxSourcePackage, organization=request.organization, pk=pk
    )
    status = service.package_status(organization=request.organization, package=package)
    cards = source_cards(request.organization, package)
    page = Paginator(cards, 25).get_page(request.GET.get("page"))
    return render(
        request,
        "taxes/monthly_sources/detail.html",
        {
            **status,
            "cards": page,
            "issues": visible_issues(status),
            "approved_bridge": approved_bridge(
                request.organization, package, status["approval"], cards
            ),
            "book_revenue": package.snapshot.get("book_revenue"),
            "pending_source_count": 0
            if status["approval"] and not status["stale"]
            else len(cards),
            "period_value": package.period.strftime("%Y-%m"),
            "can_prepare": request.membership.role in ("owner", "finance"),
            "can_review": request.membership.role == "owner"
            and not status["stale"]
            and not status.get("superseded")
            and not status["approval"],
            "can_correct": request.membership.role == "owner"
            and status.get("can_review_revision", False),
            "versions": MonthlyTaxSourcePackage.objects.filter(
                organization=request.organization, period=package.period
            ).order_by("-revision"),
            "active_nav": "tax",
        },
    )


@organization_required
@require_http_methods(["GET", "POST"])
def correction(request, pk):
    authorize_write(request, approve=True)
    package = get_object_or_404(
        MonthlyTaxSourcePackage, organization=request.organization, pk=pk
    )
    status = service.package_status(organization=request.organization, package=package)
    form = ReviewCorrectionForm(
        request.POST if request.method == "POST" else None,
        initial={
            "expected_fingerprint": status["fingerprint"],
            "expected_version": status["version"],
        },
    )
    if request.method == "POST" and form.is_valid():
        try:
            revised = service.revise_monthly_review(
                organization=request.organization,
                actor=request.user,
                package=package,
                reason=form.cleaned_data["reason"],
                expected_fingerprint=form.cleaned_data["expected_fingerprint"],
                expected_version=form.cleaned_data["expected_version"],
            )
        except ValidationError as exc:
            form.add_error(None, exc.messages)
            status = service.package_status(
                organization=request.organization, package=package
            )
        else:
            messages.success(
                request,
                "Versi baru siap diperiksa. Keputusan dan dokumen sebelumnya tetap tersimpan dalam riwayat.",
            )
            return redirect("taxes:monthly_source_review", pk=revised.pk)
    return render(
        request,
        "taxes/monthly_sources/correction.html",
        {
            **status,
            "form": form,
            "period_value": package.period.strftime("%Y-%m"),
            "active_nav": "tax",
        },
    )


def csv_facts(value, prefix=""):
    if isinstance(value, dict):
        for key, child in sorted(value.items()):
            yield from csv_facts(child, f"{prefix}.{key}" if prefix else str(key))
    elif isinstance(value, list):
        for number, child in enumerate(value, 1):
            yield from csv_facts(child, f"{prefix}[{number}]")
    else:
        yield prefix, "Belum ditentukan" if value is None else value


def expanded_review_data(post, package):
    """Compact browser transport avoids Django's field-count ceiling for large months.

    Row identities always come from the immutable package, never the browser.
    Without JS ordinary small-month form submissions use the same validators.
    """
    data = post.copy()
    raw = post.get("row_choices")
    if raw is not None and len(raw.encode("utf-8")) > 1_000_000:
        raise ValidationError(
            "Pilihan transaksi terlalu besar. Ringkas alasan bersama sesuai laporan dan kirim kembali."
        )
    try:
        if raw is not None:
            rows = json.loads(raw)
            if not isinstance(rows, list) or len(rows) != len(
                package.snapshot.get("rows", [])
            ):
                raise ValueError
            for index, row in enumerate(rows):
                if (
                    not isinstance(row, dict)
                    or set(row) != {"decision", "reason"}
                    or not all(isinstance(value, str) for value in row.values())
                ):
                    raise ValueError
                if len(row["decision"]) > 20 or len(row["reason"]) > 1000:
                    raise ValueError
                data[f"source_{index}_decision"] = row["decision"]
                data[f"source_{index}_reason"] = row["reason"]
        raw_adjustments = post.get("adjustment_inputs")
        if raw_adjustments is not None:
            from .monthly_source_workflow import MAX_MONTHLY_ADJUSTMENTS

            if len(raw_adjustments.encode("utf-8")) > 1_000_000:
                raise ValueError
            adjustments = json.loads(raw_adjustments)
            if (
                not isinstance(adjustments, list)
                or len(adjustments) > MAX_MONTHLY_ADJUSTMENTS
            ):
                raise ValueError
            data["adjustments-TOTAL_FORMS"], data["adjustments-INITIAL_FORMS"] = (
                str(len(adjustments)),
                "0",
            )
            for index, item in enumerate(adjustments):
                if not isinstance(item, dict) or set(item) != {
                    "kind",
                    "amount",
                    "reason",
                    "evidence",
                    "source_row_ids",
                }:
                    raise ValueError
                for key in ("kind", "amount", "reason", "evidence"):
                    if not isinstance(item[key], str) or len(item[key]) > (
                        2000 if key == "reason" else 40
                    ):
                        raise ValueError
                    data[f"adjustments-{index}-{key}"] = item[key]
                refs = item["source_row_ids"]
                if (
                    not isinstance(refs, list)
                    or len(refs) > len(package.snapshot.get("rows", []))
                    or any(not isinstance(ref, str) or len(ref) > 150 for ref in refs)
                ):
                    raise ValueError
                data.setlist(f"adjustments-{index}-source_row_ids", refs)
                data.pop(f"adjustments-{index}-DELETE", None)
    except (ValueError, TypeError, RecursionError):
        raise ValidationError(
            "Pilihan transaksi tidak lengkap. Periksa kembali pilihan pada versi ini."
        ) from None
    return data


@organization_required
@require_http_methods(["GET", "POST"])
def review(request, pk):
    authorize_write(request, approve=True)
    from .workflow import record_external_review
    from .workspace_views import review_fields

    package = get_object_or_404(
        MonthlyTaxSourcePackage, organization=request.organization, pk=pk
    )
    status = service.package_status(organization=request.organization, package=package)
    data, transport_error = None, None
    if request.method == "POST":
        try:
            data = expanded_review_data(request.POST, package)
        except ValidationError as exc:
            data, transport_error = request.POST, exc.messages
    form = SourceReviewForm(
        data,
        organization=request.organization,
        package=package,
        initial={
            "expected_fingerprint": status["fingerprint"],
            "expected_version": status["version"],
        },
    )
    adjustments = AdjustmentFormSet(
        data,
        prefix="adjustments",
        form_kwargs={
            "organization": request.organization,
            "rows": package.snapshot.get("rows", []),
        },
    )
    if transport_error:
        form.add_error(None, transport_error)
    if request.method == "POST" and not transport_error:
        form_ok, adjustments_ok = form.is_valid(), adjustments.is_valid()
        if form_ok and adjustments_ok:
            cleaned = form.cleaned_data
            actual_adjustments = [
                {
                    "kind": row["kind"],
                    "amount": row["amount"],
                    "reason": row["reason"],
                    "evidence_id": row["evidence"].pk,
                    "source_row_ids": row["source_row_ids"],
                }
                for row in adjustments.cleaned_data
                if row and not row.get("DELETE") and row.get("kind")
            ]
            try:
                with transaction.atomic():
                    decision = service.monthly_decision(
                        package,
                        rows=cleaned["rows"],
                        adjustments=actual_adjustments,
                        **{
                            key: cleaned[key]
                            for key in (
                                "completeness_confirmed",
                                "inventory_note",
                                "nil_confirmed",
                                "self_payment_only_confirmed",
                                "customer_final_withholding",
                                "prior_history_confirmed",
                                "object_code",
                                "rule_reference",
                            )
                        },
                    )
                    report = record_external_review(
                        organization=request.organization,
                        actor=request.user,
                        subject=package,
                        decision=decision,
                        **review_fields(cleaned),
                    )
                    service.approve_monthly_package(
                        organization=request.organization,
                        actor=request.user,
                        package=package,
                        decision=decision,
                        external_review=report,
                        expected_fingerprint=cleaned["expected_fingerprint"],
                        expected_version=cleaned["expected_version"],
                    )
            except ValidationError as exc:
                form.add_error(None, exc.messages)
                status = service.package_status(
                    organization=request.organization, package=package
                )
            else:
                messages.success(
                    request,
                    "Laporan pemeriksa tercatat untuk versi ini. Angka draf diambil dari keputusan yang diperiksa.",
                )
                return redirect("taxes:monthly_source", pk=pk)
    cards = [
        {**card, **fields, "number": index + 1}
        for index, (card, fields) in enumerate(
            zip(source_cards(request.organization, package), form.source_fields())
        )
    ]
    return render(
        request,
        "taxes/monthly_sources/review.html",
        {
            **status,
            "form": form,
            "adjustments": adjustments,
            "review_cards": cards,
            "has_proposed": any(card.get("proposed_inclusion") for card in cards),
            "report_fields": [
                form[key]
                for key in (
                    "evidence",
                    "professional_name",
                    "qualification_reference",
                    "reviewed_on",
                    "statement",
                    "rule_reference",
                    "report_confirmed",
                )
            ],
            "inventory_fields": [
                form[key]
                for key in ("inventory_note", "completeness_confirmed", "nil_confirmed")
            ],
            "final_fields": [
                form[key]
                for key in (
                    "self_payment_only_confirmed",
                    "customer_final_withholding",
                    "object_code",
                )
            ],
            "prior_history": history_cards(request.organization, package),
            "book_revenue": package.snapshot.get("book_revenue"),
            "period_value": package.period.strftime("%Y-%m"),
            "active_nav": "tax",
        },
    )


@organization_required
@require_http_methods(["POST"])
def generate(request, pk):
    authorize_write(request)
    package = get_object_or_404(
        MonthlyTaxSourcePackage, organization=request.organization, pk=pk
    )
    form = GenerateForm(request.POST)
    if form.is_valid():
        try:
            obligation = service.generate_monthly_obligation(
                organization=request.organization,
                actor=request.user,
                package=package,
                **form.cleaned_data,
            )
        except ValidationError as exc:
            form.add_error(None, exc.messages)
        else:
            return redirect("taxes:obligation", pk=obligation.pk)
    status = service.package_status(organization=request.organization, package=package)
    return render(
        request,
        "taxes/monthly_sources/generate_error.html",
        {
            **status,
            "form": form,
            "period_value": package.period.strftime("%Y-%m"),
            "active_nav": "tax",
        },
    )


@organization_required
@require_GET
def export(request, pk):
    authorize_read(request)
    from .views import csv_cell

    package = get_object_or_404(
        MonthlyTaxSourcePackage, organization=request.organization, pk=pk
    )
    status = service.package_status(organization=request.organization, package=package)
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(
        ["DRAF SUMBER PAJAK BULANAN — BUKAN SPT, XML DJP, BUKTI BAYAR ATAU BUKTI LAPOR"]
    )
    writer.writerow(
        [
            "Keadaan versi",
            "Versi sudah digantikan; gunakan versi terbaru"
            if status.get("superseded")
            else "Data berubah; gunakan versi terbaru"
            if status["stale"]
            else "Sumber versi tersimpan",
        ]
    )
    manifest = {
        "period": package.period.strftime("%Y-%m"),
        "revision": package.revision,
        "source_hash": package.source_hash,
        "package_hash": package.package_hash,
        "review_revision_reason": status.get("revision_reason", ""),
        "superseded": status.get("superseded", False),
        "snapshot": package.snapshot,
        "profile": package.profile_snapshot,
        "legal_sources": package.legal_sources,
        "approval": {
            "decision": status["approval"].decision,
            "decision_hash": status["approval"].decision_hash,
            "result": status["approval"].result,
            "actor_id": status["approval"].actor_id,
            "created_at": status["approval"].created_at.isoformat(),
            "professional_report": {
                "id": status["approval"].external_review_id,
                "subject_digest": status["approval"].external_review.subject_digest,
                "professional_name": status[
                    "approval"
                ].external_review.professional_name,
                "qualification_reference": status[
                    "approval"
                ].external_review.qualification_reference,
                "reviewed_on": status[
                    "approval"
                ].external_review.reviewed_on.isoformat(),
                "statement": status["approval"].external_review.statement,
                "evidence_id": status["approval"].external_review.evidence_id,
                "evidence_sha256": status["approval"].external_review.evidence.sha256,
            },
        }
        if status["approval"]
        else None,
    }
    writer.writerow(["Sumber / rincian", "Nilai pada versi ini"])
    for key, value in csv_facts(manifest):
        writer.writerow([csv_cell(key), csv_cell(value)])
    response = HttpResponse(
        "\ufeff" + output.getvalue(), content_type="text/csv; charset=utf-8"
    )
    response["Content-Disposition"] = (
        f'attachment; filename="DRAF-sumber-pajak-{package.period:%Y-%m}-versi-{package.revision}.csv"'
    )
    response["Cache-Control"], response["X-Content-Type-Options"] = (
        "no-store",
        "nosniff",
    )
    record(
        request.organization,
        request.user,
        "tax.monthly_package.exported",
        package,
        {
            "source_hash": package.source_hash,
            "stale": status["stale"],
            "official_schema_validated": False,
        },
    )
    return response
