"""Shared, source-linked management facts; operational progress is not sales success."""

import hashlib
import json
from datetime import date

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from core.models import Organization

from .activity_models import WorkActivity
from .ad_sources import ad_source_state
from .kpi import evaluate_target
from .kpi_models import KpiTarget

MANAGEMENT_FACTS = {
    "kpi_active": "Target aktif dalam periode",
    "kpi_sources_incomplete": "Target menunggu kelengkapan sumber",
    "kpi_met": "Target tercapai dengan sumber diperiksa",
    "kpi_not_met": "Target belum tercapai dengan sumber diperiksa",
    "kpi_stale_reviews": "Target dengan evaluasi yang dasarnya berubah",
    "work_waiting_review": "Pekerjaan menunggu pemeriksaan",
    "work_review_oldest_days": "Umur pengajuan tertua dalam hari",
    "work_overdue": "Pekerjaan melewati tenggat pelaksanaan",
    "work_accepted": "Pekerjaan diterima tanpa pengganti",
    "work_corrections_pending": "Pekerjaan koreksi belum diterima",
}
COMMERCIAL_FACTS = {
    "cohort_orders": ("Pesanan tercatat dalam periode", "catatan"),
    "cohort_fully_paid_orders": ("Pesanan dengan seluruh tagihan lunas", "catatan"),
    "cohort_direct_paid_orders": ("Pesanan langsung lunas", "catatan"),
    "cohort_partner_paid_orders": ("Pesanan mitra lunas", "catatan"),
    "recorded_participants": ("Peserta pada daftar tercatat", "catatan"),
    "completed_participants": ("Peserta dengan bukti layanan selesai", "catatan"),
    "active_paid_partners": ("Mitra dengan pesanan lunas periode ini", "catatan"),
    "repeat_paid_partners": ("Mitra membayar berulang sampai pengamatan", "catatan"),
    "partners_without_earlier_paid_order": (
        "Mitra tanpa pesanan bertanggal lebih awal yang kini lunas",
        "catatan",
    ),
    "cohort_net_recognized_revenue": (
        "Pendapatan layanan neto kelompok pesanan",
        "IDR",
    ),
    "cohort_actual_cost": ("Biaya provider aktual untuk unit selesai", "IDR"),
    "cohort_actual_margin": (
        "Margin setelah biaya provider, sebelum marketing dan overhead",
        "IDR",
    ),
    "cost_pending_invoices": ("Invoice layanan menunggu kelengkapan biaya", "catatan"),
    "period_linked_refunds": ("Refund tertaut pada periode mutasi", "IDR"),
}


@transaction.atomic
def management_snapshot(*, organization, start, end):
    try:
        start, end = (
            date.fromisoformat(value) if isinstance(value, str) else value
            for value in (start, end)
        )
        if (
            not isinstance(start, date)
            or not isinstance(end, date)
            or end < start
            or (end - start).days > 366
        ):
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            "Periode ringkasan manajemen harus valid, maksimal 367 hari."
        ) from exc
    organization = Organization.objects.select_for_update().get(
        pk=getattr(organization, "pk", organization)
    )
    targets = KpiTarget.objects.filter(
        organization=organization,
        status="active",
        period_start__lte=end,
        period_end__gte=start,
    ).order_by("pk")
    target_count = targets.count()
    rows, references = [], []
    counts = {key: 0 for key in MANAGEMENT_FACTS}
    counts["kpi_active"] = target_count
    ads = ad_source_state(
        organization=organization, start=start, end=min(end, timezone.localdate())
    )
    references.append({"kind": "ad_sources", **ads})
    # Local source identity changes even when a correction leaves every total
    # unchanged and there are no KPI targets or operational orders yet.
    from operations.attribution import invoice_attribution

    references.append(
        {
            "kind": "invoice_identity",
            "fingerprint": invoice_attribution(organization=organization)[
                "fingerprint"
            ],
        }
    )
    for target in targets[:200]:
        result = evaluate_target(target)
        review = target.reviews.order_by("-created_at", "-pk").first()
        stale = bool(review and review.fingerprint != result["fingerprint"])
        counts["kpi_sources_incomplete"] += int(not result["completeness"]["complete"])
        counts["kpi_met"] += int(result["official"] and result["state"] == "met")
        counts["kpi_not_met"] += int(
            result["official"] and result["state"] == "not_met"
        )
        counts["kpi_stale_reviews"] += int(stale)
        rows.append(
            {
                "id": target.pk,
                "title": target.title,
                "metric": target.metric_label,
                "version": target.version,
                "url": f"/marketing/kpis/{target.pk}/",
                "evaluation": result,
                "review_id": getattr(review, "pk", None),
                "stale_review": stale,
            }
        )
        references.append(
            {
                "kind": "kpi",
                "id": target.pk,
                "version": target.version,
                "fingerprint": result["fingerprint"],
                "review_id": getattr(review, "pk", None),
            }
        )
    if target_count > 200:
        for key in (
            "kpi_sources_incomplete",
            "kpi_met",
            "kpi_not_met",
            "kpi_stale_reviews",
        ):
            counts[key] = None
    work = WorkActivity.objects.filter(
        organization=organization, due_date__range=(start, end)
    )
    now = timezone.now()
    waiting = work.filter(status="submitted")
    oldest = (
        waiting.order_by("submitted_at").values_list("submitted_at", flat=True).first()
    )
    counts.update(
        work_waiting_review=waiting.count(),
        work_review_oldest_days=max(0, (now - oldest).days) if oldest else 0,
        work_overdue=work.filter(
            status__in=["planned", "in_progress", "needs_revision"],
            due_date__lt=timezone.localdate(),
        ).count(),
        work_accepted=work.filter(status="accepted", replacement__isnull=True).count(),
        work_corrections_pending=work.filter(replaces__isnull=False)
        .exclude(status__in=["accepted", "cancelled"])
        .count(),
    )
    work_digest = hashlib.sha256()
    for row in work.order_by("pk").values(
        "pk",
        "version",
        "status",
        "evidence_manifest",
        "kpi_target_id",
        "decision_id",
        "budget_id",
        "replaces_id",
    ):
        work_digest.update(json.dumps(row, sort_keys=True, default=str).encode())
    references.append(
        {"kind": "work", "count": work.count(), "fingerprint": work_digest.hexdigest()}
    )
    from operations.analytics import commercial_snapshot

    commercial = commercial_snapshot(organization=organization, start=start, end=end)
    references.append(
        {
            "kind": "operations",
            "fingerprint": commercial["fingerprint"],
            "source_url": "/operations/",
            "order_count": commercial["cohort_orders"],
        }
    )
    fingerprint = hashlib.sha256(
        json.dumps(references, sort_keys=True).encode()
    ).hexdigest()
    facts = {key: value for key, value in counts.items() if value is not None}
    return {
        "organization_id": organization.pk,
        "period": {"start": str(start), "end": str(end)},
        "counts": counts,
        "targets": rows,
        "source_references": references,
        "fingerprint": fingerprint,
        "ai_facts": facts,
        "commercial": commercial,
        "commercial_facts": [
            {"key": key, "label": label, "unit": unit, "value": commercial[key]}
            for key, (label, unit) in COMMERCIAL_FACTS.items()
        ],
        "evaluated_targets": len(rows),
        "target_count": target_count,
        "complete_target_scan": target_count <= 200,
        "scope": "Seluruh organisasi; target yang bersinggungan dan pekerjaan dengan tenggat dalam periode. Filter analisis iklan tidak mengubah lingkup ringkasan ini.",
        "limitations": [
            "Pekerjaan diterima adalah pemeriksaan pelaksanaan, bukan pembayaran atau keberhasilan KPI.",
            "KPI memerlukan pengesahan cakupan sumber; target dengan sumber belum lengkap tidak dihitung tercapai.",
            "Jika lebih dari 200 target aktif, persempit periode: jumlah hasil KPI ditahan sampai seluruh target diperiksa.",
        ],
    }
