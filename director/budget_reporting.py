"""Dated management aggregates from the same budget position used by Finance."""

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.utils import timezone

from core.models import Organization

from .models import Budget

SCHEMA_VERSION = "director-budget-summary-v1"
MONEY_KEYS = (
    "amount",
    "incurred",
    "open_commitments",
    "reservations",
    "consumed",
    "available",
    "overrun",
    "payments",
    "reported_payments",
    "unmatched_reported_payments",
    "verified_cash",
    "unpaid_bills",
    "withholding_payable",
)
BUDGET_FACTS = {
    "planning.approved_budget": ("amount", "Budget yang disetujui", "approved_plan"),
    "planning.budget_incurred": (
        "incurred",
        "Biaya terjadi dalam budget",
        "budget_source_linked",
    ),
    "planning.budget_commitments": (
        "open_commitments",
        "Komitmen budget tersisa",
        "budget_source_linked",
    ),
    "planning.budget_reservations": (
        "reservations",
        "Dana budget yang dicadangkan",
        "budget_source_linked",
    ),
    "planning.budget_consumed": (
        "consumed",
        "Konsumsi otorisasi budget",
        "budget_source_linked",
    ),
    "planning.budget_available": (
        "available",
        "Sisa otorisasi budget",
        "budget_source_linked",
    ),
    "planning.budget_overrun": (
        "overrun",
        "Kelebihan budget yang perlu ditinjau",
        "budget_source_linked",
    ),
    "planning.budget_verified_cash": (
        "verified_cash",
        "Uang bank terkait budget",
        "budget_verified_cash",
    ),
    "planning.budget_reported_payments": (
        "reported_payments",
        "Pembayaran budget yang dilaporkan",
        "budget_reported",
    ),
    "planning.budget_unmatched_payments": (
        "unmatched_reported_payments",
        "Laporan pembayaran belum dicocokkan",
        "budget_reported",
    ),
    "planning.budget_unpaid_bills": (
        "unpaid_bills",
        "Sisa tagihan pemasok terkait budget",
        "budget_source_linked",
    ),
    "planning.budget_withholding_payable": (
        "withholding_payable",
        "Potongan terkait budget belum disetor",
        "budget_source_linked",
    ),
}


def _year(value):
    if type(value) is not int or not 2000 <= value <= 2200:
        raise ValidationError("Tahun budget harus antara 2000 dan 2200.")
    return value


def _cutoff(year, value):
    if value is None:
        value = min(timezone.localdate(), date(year, 12, 31))
    if (
        not isinstance(value, date)
        or isinstance(value, datetime)
        or not 2000 <= value.year <= 2200
    ):
        raise ValidationError("Tanggal posisi budget harus berupa tanggal yang valid.")
    return value


def _sum(positions, key):
    values = [row.get(key) for row in positions]
    if not positions or any(value is None for value in values):
        return None
    try:
        amounts = [Decimal(value) for value in values]
    except (ValueError, TypeError, ArithmeticError) as exc:
        raise ValidationError("Nominal posisi budget belum dapat diperiksa.") from exc
    if any(not amount.is_finite() for amount in amounts):
        raise ValidationError("Nominal posisi budget belum dapat diperiksa.")
    return format(sum(amounts, Decimal("0.00")), ".2f")


def budget_summary(*, organization, year, as_of=None):
    """Aggregate approved envelopes; this never asserts whole-company coverage."""
    from .spend_services import budget_position

    year = _year(year)
    cutoff = _cutoff(year, as_of)
    organization_id = getattr(organization, "pk", organization)
    if type(organization_id) is not int or organization_id < 1:
        raise ValidationError("Pilih perusahaan yang valid untuk melihat budget.")
    if not Organization.objects.filter(pk=organization_id).exists():
        raise ValidationError("Perusahaan tidak tersedia untuk melihat budget.")
    budgets = list(
        Budget.objects.filter(
            organization_id=organization_id, year=year, status="approved"
        ).order_by("month", "channel", "pk")
    )
    positions = []
    for budget in budgets:
        row = budget_position(organization=organization, budget=budget, as_of=cutoff)
        positions.append(
            {
                **row,
                "title": budget.title,
                "month": budget.month,
                "channel": budget.channel,
                "channel_label": budget.get_channel_display(),
            }
        )
    current = all(row.get("current") is True for row in positions)
    status = (
        "missing" if not positions else "available" if current else "review_required"
    )
    totals = {key: _sum(positions, key) for key in MONEY_KEYS}
    if not current:
        # An unverified source must never be presented as a complete aggregate.
        totals.update({key: None for key in MONEY_KEYS if key != "amount"})
    issues = [
        {"budget_id": row["budget_id"], **issue}
        for row in positions
        for issue in row.get("issues", [])
    ]
    tasks = [
        {"budget_id": row["budget_id"], **task}
        for row in positions
        for task in row.get("tasks", [])
    ]
    coverage = (
        "Belum ada budget yang disetujui pada tahun ini."
        if not positions
        else "Ada sumber budget yang perlu diperiksa; total belum dapat dinilai."
        if not current
        else "Batas dari versi budget yang saat ini disetujui; kegiatan belanja hingga "
        + str(cutoff)
        + ". "
        "Sumber dihubungkan secara eksplisit. "
        "Catatan manual tetap dilaporkan; kas bank hanya dari bukti Finance. "
        "Sisa budget bukan saldo kas atau izin pembayaran."
    )
    references = [
        {
            "budget_id": row["budget_id"],
            "version": row["version"],
            "fingerprint": row["fingerprint"],
            "as_of": row["as_of"],
        }
        for row in positions
    ]
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "organization_id": organization_id,
        "year": year,
        "as_of": str(cutoff),
        "source_references": references,
        "authorization_basis": "current_approved_envelopes",
        "status": status,
        "totals": totals,
    }
    fingerprint = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        **manifest,
        **totals,
        "approved_count": len(budgets),
        "current": current,
        "positions": positions,
        "issues": issues,
        "tasks": tasks,
        "coverage": coverage,
        "fingerprint": fingerprint,
    }


def budget_facts(summary):
    """Only aggregate values and code-owned labels may enter Director AI facts."""
    return [
        {
            "fact_id": key,
            "value": summary[field],
            "label": label,
            "unit": "IDR",
            "basis": basis,
            "coverage": summary["coverage"],
            "source_url": "/director/budgets/",
            "as_of": summary["as_of"],
            "status": summary["status"],
        }
        for key, (field, label, basis) in BUDGET_FACTS.items()
    ]
