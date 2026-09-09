"""Read-only Director evidence snapshot, kept separate from posting and planning.

Source recaps are observations, never invoices, cash, or recognized revenue.
All financial amounts are strings (or null when unobserved), suitable for JSON.
"""

from collections import defaultdict
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db.models import Sum
from django.urls import reverse
from django.utils import timezone

from finance.models import AccountingPeriod, Journal, JournalLine
from imports.services import import_summary


MONTHS = ("Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des")
ZERO = Decimal("0.00")
CENT = Decimal("0.01")
JAKARTA = ZoneInfo("Asia/Jakarta")


def _money(value):
    return format(Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP), "f") if value is not None else None


def _observed_sum(values, *, money=False):
    values = [value for value in values if value is not None]
    if not values:
        return None
    return _money(sum((Decimal(value) for value in values), ZERO)) if money else sum(values)


def _financial_snapshot(organization, year):
    journals = Journal.objects.filter(organization=organization, status="posted", date__year=year)
    posting_count = journals.count()
    rows = JournalLine.objects.filter(organization=organization, journal__organization=organization,
                                      journal__status="posted", journal__date__year=year).values("account").annotate(
                                          debit=Sum("debit"), credit=Sum("credit"))
    movements = {row["account"]: row["debit"] - row["credit"] for row in rows}
    revenue = -movements["REVENUE"] if "REVENUE" in movements else None
    expenses = movements.get("EXPENSE")
    profit = revenue - expenses if revenue is not None and expenses is not None else None
    covered = sorted({item.strftime("%Y-%m") for item in journals.values_list("date", flat=True)})
    closed = list(AccountingPeriod.objects.filter(organization=organization, year=year, closed=True)
                  .order_by("month").values_list("month", flat=True))
    return {
        "posted_available": posting_count > 0, "posting_count": posting_count,
        "revenue": _money(revenue), "expenses": _money(expenses), "profit": _money(profit),
        "bank_balance": None, "covered_months": covered, "closed_months": closed,
        "coverage_label": ("Jurnal terposting yang tersedia dalam tahun kalender; kelengkapan pendapatan dan beban belum dinyatakan."
                           if posting_count else "Belum ada jurnal terposting untuk tahun ini."),
        "profit_note": "Selisih pendapatan dan beban yang tercatat, bukan laba lengkap perusahaan atau dasar pajak.",
        "bank_note": "Saldo awal dan saldo bank terverifikasi belum tersedia; mutasi tidak dianggap saldo bank.",
        "source_url": reverse("reports") + f"?period={covered[-1] if covered else str(year) + '-12'}",
        "source_links": [{"period": period, "url": reverse("reports") + f"?period={period}"} for period in covered],
    }


def _partner_rollups(data, month_lookup):
    grouped = defaultdict(list)
    for batch in data["batches"]:
        selected = {month.source_id for month in data["months"] if month.batch_id == batch.pk}
        counts = defaultdict(int)
        for detail in batch.payload.get("details", []):
            if detail["month_id"] in selected:
                counts[detail["channel_id"]] += detail["quantity"]
        for channel in batch.payload.get("channels", []):
            if channel["month_id"] in selected:
                grouped[channel["source_label"]].append({
                    "period": month_lookup[(batch.pk, channel["month_id"])].source_period,
                    "printed_count": channel.get("printed_count"),
                    "printed_amount": channel.get("printed_amount"),
                    "detail_count": channel.get("detail_count", counts.get(channel["id"])),
                })
    partners = []
    for label, channels in grouped.items():
        periods = sorted({channel["period"] for channel in channels})
        partners.append({"source_label": label,
                         "reported_amount": _observed_sum([row["printed_amount"] for row in channels], money=True),
                         "printed_count": _observed_sum([row["printed_count"] for row in channels]),
                         "detail_count": _observed_sum([row["detail_count"] for row in channels]),
                         "month_count": len(periods), "months": periods,
                         "unknown_amount_count": sum(row["printed_amount"] is None for row in channels),
                         "source_url": reverse("imports_month", args=[periods[-1]]),
                         "identity_status": "raw_source_label"})
    return sorted(partners, key=lambda row: (-(row["printed_count"] or 0), row["source_label"]))


def dashboard_snapshot(*, organization, year=2026):
    """One organization/calendar year, with reproducible facts and evidence links.

    HTTP authorization is the caller's responsibility; every query is scoped to
    the supplied organization. Raw labels are deliberately absent from facts so
    an AI adapter can independently allowlist aggregate fact IDs.
    """
    if type(year) is not int or not 2000 <= year <= 2200:
        raise ValidationError("Tahun harus berupa bilangan bulat antara 2000 dan 2200.")
    organization_id = getattr(organization, "pk", organization)
    if type(organization_id) is not int or organization_id <= 0:
        raise ValidationError("Organisasi tersimpan wajib dipilih.")
    data = import_summary(organization=organization, year=year)
    month_lookup = {(month.batch_id, month.source_id): month for month in data["months"]}
    by_period = {month.source_period: month for month in data["months"]}
    source_url = reverse("imports_overview") + f"?year={year}"
    coverage = f"{len(data['months'])} dari 12 halaman bulanan tersedia; kelengkapan operasional belum dikonfirmasi."
    monthly = []
    for month_number in range(1, 13):
        period = f"{year}-{month_number:02d}"
        item = by_period.get(period)
        partial = bool(item and item.coverage == "partial_source_page")
        row = {"period": period, "label": MONTHS[month_number - 1], "available": item is not None,
               "reported_amount": _money(item.printed_amount) if item else None,
               "printed_count": item.printed_count if item else None,
               "detail_count": item.detail_count if item else None,
               "is_partial": partial, "source_page": item.source_page if item else None,
               "source_url": reverse("imports_month", args=[period]) if item else source_url,
               "change_pct": None, "change_reason": "Perbandingan bulan berurutan belum tersedia.",
               "coverage": item.coverage if item else "missing",
               "comparison_basis": "Jumlah peserta tercetak pada halaman sumber; bukan pertumbuhan pendapatan."}
        previous = monthly[-1] if monthly else None
        if previous and item and previous["available"]:
            if partial or previous["is_partial"]:
                row["change_reason"] = "Perbandingan ditahan karena salah satu halaman hanya mencakup sebagian bulan."
            elif row["printed_count"] is None or previous["printed_count"] is None:
                row["change_reason"] = "Jumlah tercetak belum tersedia pada kedua halaman."
            elif previous["printed_count"] == 0:
                row["change_reason"] = "Persentase tidak dihitung karena jumlah bulan pembanding nol."
            else:
                row["change_pct"] = _money(Decimal(row["printed_count"] - previous["printed_count"]) / Decimal(previous["printed_count"]) * 100)
                row["change_reason"] = "Perubahan jumlah tercetak antarhalaman; kelengkapan bisnis dan sebab perubahan belum diketahui."
        monthly.append(row)
    exceptions = []
    for item in data["exceptions"]:
        source_month = month_lookup.get((item.batch_id, item.month_source_id))
        if item.month_source_id and source_month is None:
            continue
        exceptions.append({"id": item.exception_id, "code": item.code, "severity": item.severity,
                           "message": item.message, "period": source_month.source_period if source_month else None,
                           "source_url": reverse("imports_month", args=[source_month.source_period]) if source_month else source_url})
    annual_records = []
    for batch in data["batches"]:
        annual = batch.payload.get("annual_summary") or {}
        covered_periods = annual.get("covered_periods", [])
        if covered_periods and any(not period.startswith(f"{year}-") for period in covered_periods):
            continue
        printed = annual.get("printed_total")
        if isinstance(printed, dict):
            printed = printed.get("count", printed.get("printed_count"))
        if type(printed) is int:
            annual_records.append({"printed_count": printed, "covered_periods": covered_periods,
                                   "missing_periods": annual.get("missing_periods", []),
                                   "coverage": annual.get("coverage", "unconfirmed"), "source_page": annual.get("page")})
    source = {
        "available": bool(data["months"]), "reported_amount": _money(data["reported_amount"]),
        "printed_count": data["printed_count"], "detail_count": data["detail_count"],
        "month_count": len(data["months"]), "raw_label_count": data["channel_count"],
        "detail_cell_count": data["detail_rows"], "covered_months": data["covered_months"],
        "missing_months": data["missing_months"],
        "partial_months": [row["period"] for row in monthly if row["is_partial"]],
        "annual_printed_count": annual_records[0]["printed_count"] if len(annual_records) == 1 else None,
        "annual_summaries": annual_records, "exception_count": len(exceptions), "coverage_label": coverage,
        "amount_month_count": sum(month.printed_amount is not None for month in data["months"]),
        "count_month_count": sum(month.printed_count is not None for month in data["months"]),
        "source_url": source_url,
        "basis_note": "Nominal dan peserta dilaporkan sumber; bukan kas, pendapatan buku besar, atau omzet pajak. Label mentah belum dipetakan menjadi identitas mitra.",
    }
    finance = _financial_snapshot(organization, year)
    readiness = [
        {"key": "source", "label": "Arsip operasional", "status": "partial" if source["available"] else "missing",
         "detail": coverage, "source_url": source_url},
        {"key": "source_review", "label": "Pemeriksaan sumber", "status": "review_required" if exceptions else ("available" if source["available"] else "missing"),
         "detail": f"{len(exceptions)} catatan pemeriksaan tersimpan; tidak adanya catatan bukan bukti kelengkapan.", "source_url": source_url},
        {"key": "ledger", "label": "Buku besar", "status": "partial" if finance["posted_available"] else "missing",
         "detail": finance["coverage_label"], "source_url": finance["source_url"]},
        {"key": "bank", "label": "Saldo bank terverifikasi", "status": "missing", "detail": finance["bank_note"], "source_url": reverse("bank")},
        {"key": "marketing_attribution", "label": "Atribusi dan biaya akuisisi", "status": "missing",
         "detail": "Belum ada data lintas kanal yang direkonsiliasi ke pelanggan dan pesanan; CAC dan ROAS belum dapat dinilai.", "source_url": None},
    ]
    facts = []

    def fact(fact_id, value, label, unit, basis="reported", fact_coverage=coverage, url=source_url):
        facts.append({"fact_id": fact_id, "value": value, "label": label, "unit": unit,
                      "basis": basis, "coverage": fact_coverage, "source_url": url})

    for key, label, unit in (
        ("reported_amount", "Nominal tercetak pada rekap bulanan", "IDR"),
        ("printed_count", "Peserta tercetak pada rekap bulanan", "peserta"),
        ("detail_count", "Peserta pada sel rincian sumber", "peserta"),
        ("month_count", "Halaman bulanan tersedia", "bulan"),
        ("raw_label_count", "Label kanal mentah yang berbeda", "label"),
        ("detail_cell_count", "Sel rincian kuantitas sumber", "sel"),
        ("exception_count", "Catatan pemeriksaan sumber", "catatan"),
    ):
        fact(f"source.{key}", source[key], label, unit)
    annual_coverage = ("Halaman ringkasan tahunan terpisah; " + ", ".join(annual_records[0]["covered_periods"])
                       if len(annual_records) == 1 else "Ringkasan tahunan tunggal belum tersedia.")
    fact("source.annual_printed_count", source["annual_printed_count"], "Peserta pada ringkasan tahunan sumber", "peserta", fact_coverage=annual_coverage)
    for key, label in (("revenue", "Pendapatan terposting tersedia"), ("expenses", "Beban terposting tersedia"), ("profit", "Selisih pendapatan dan beban terposting")):
        fact(f"finance.{key}", finance[key], label, "IDR", "posted", finance["coverage_label"], finance["source_url"])
    fact("finance.bank_balance", None, "Saldo bank terverifikasi", "IDR", "unavailable", finance["bank_note"], reverse("bank"))
    for row in monthly:
        month_coverage = ("Sebagian halaman bulan; belum lengkap." if row["is_partial"] else
                          "Halaman sumber tersedia; kelengkapan bisnis belum dikonfirmasi." if row["available"] else "Halaman sumber belum tersedia.")
        for key, label, unit in (("reported_amount", "Nominal tercetak", "IDR"), ("printed_count", "Peserta tercetak", "peserta"), ("detail_count", "Peserta rincian", "peserta")):
            fact(f"source.month.{row['period']}.{key}", row[key], f"{label} {row['period']}", unit,
                 fact_coverage=month_coverage, url=row["source_url"])
    return {"schema_version": "director-snapshot-v1", "organization_id": organization_id, "year": year,
            "as_of": timezone.localdate(timezone=JAKARTA).isoformat(), "source": source,
            "monthly_series": monthly, "partners": _partner_rollups(data, month_lookup),
            "exceptions": exceptions, "readiness": readiness, "finance": finance, "facts": facts}
