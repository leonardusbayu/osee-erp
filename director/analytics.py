"""Read-only Director evidence snapshot, kept separate from posting and planning.

Source recaps are observations, never invoices, cash, or recognized revenue.
All financial amounts are strings (or null when unobserved), suitable for JSON.
"""

from collections import defaultdict
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import CharField, Sum, Value
from django.db.models.functions import Cast, Concat
from django.urls import reverse
from django.utils import timezone

from core.models import Organization
from finance.models import (
    AccountingPeriod,
    Allocation,
    BankAccount,
    BankOpening,
    BankPosting,
    BankTransaction,
    BillPayment,
    Journal,
    JournalLine,
)
from imports.services import import_summary

MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "Mei",
    "Jun",
    "Jul",
    "Agu",
    "Sep",
    "Okt",
    "Nov",
    "Des",
)
ZERO = Decimal("0.00")
CENT = Decimal("0.01")
JAKARTA = ZoneInfo("Asia/Jakarta")
BOOK_PROFIT_NOTE = "Selisih pendapatan dan beban yang tercatat sebelum pajak penghasilan, bukan laba lengkap perusahaan atau dasar pajak."


def _money(value):
    return (
        format(Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP), "f")
        if value is not None
        else None
    )


def _observed_sum(values, *, money=False):
    values = [value for value in values if value is not None]
    if not values:
        return None
    return (
        _money(sum((Decimal(value) for value in values), ZERO))
        if money
        else sum(values)
    )


def _bank_cutoff(year):
    today = timezone.localdate(timezone=JAKARTA)
    if type(year) is not int or not 2000 <= year <= 2200:
        return None
    if year < today.year:
        return date(year, 12, 31)
    if year == today.year and today.month > 1:
        return today.replace(day=1) - timedelta(days=1)
    return None


def _unassigned_bank_before(organization, account, start):
    """Prior journals must belong to another evidenced bank account.

    BANK is an organization-wide ledger account. Its other-account balance is
    not the opening of a new account. Generic journals still make that opening
    uncertain, even if their signed amounts cancel one another.
    """
    history = JournalLine.objects.filter(
        organization=organization,
        account="BANK",
        journal__organization=organization,
        journal__status="posted",
        journal__date__lt=start,
    )
    openings = (
        BankOpening.objects.filter(
            organization=organization,
            account__organization=organization,
            date__lt=start,
        )
        .exclude(account=account)
        .values("journal_id")
    )
    bank_events = (
        BankPosting.objects.filter(
            organization=organization,
            transaction__organization=organization,
            transaction__account__organization=organization,
            date__lt=start,
            transaction__date__lt=start,
        )
        .exclude(transaction__account=account)
        .values("journal_id")
    )
    transaction_scope = {
        "organization": organization,
        "transaction__organization": organization,
        "transaction__account__organization": organization,
        "transaction__date__lt": start,
    }
    receipt_keys = (
        Allocation.objects.filter(**transaction_scope)
        .exclude(transaction__account=account)
        .annotate(
            bank_source=Concat(
                Value("allocation:"), Cast("pk", output_field=CharField())
            )
        )
        .values("bank_source")
    )
    payment_keys = (
        BillPayment.objects.filter(**transaction_scope)
        .exclude(transaction__account=account)
        .annotate(
            bank_source=Concat(
                Value("bill-payment:"), Cast("pk", output_field=CharField())
            )
        )
        .values("bank_source")
    )
    return (
        history.exclude(journal_id__in=openings)
        .exclude(journal_id__in=bank_events)
        .exclude(journal__source_key__in=receipt_keys)
        .exclude(journal__source_key__in=payment_keys)
        .exists()
    )


def _bank_position(organization, year):
    """Monthly bank position, never live cash or available spending capacity.

    Source coverage must remain current from an evidenced opening through the
    exact cutoff on every known account. A matching aggregate journal balance
    alone cannot conceal an unmatched receipt and an unmatched outgoing payment.
    """
    from finance.services import pending_bank_transactions, report_balances
    from finance.statement_models import StatementPeriodReview
    from finance.statement_services import statement_coverage

    today = timezone.localdate(timezone=JAKARTA)
    cutoff = _bank_cutoff(year)
    accounts = list(
        BankAccount.objects.filter(organization=organization).order_by("pk")
    )
    position = {
        "cutoff": str(cutoff) if cutoff else None,
        "age_days": (today - cutoff).days if cutoff else None,
        "status": "missing",
        "balance": None,
        "statement_balance": None,
        "book_balance": None,
        "difference": None,
        "account_count": len(accounts),
        "issues": [],
        "source_references": [],
        "basis": "reviewed_statements_and_posted_bank_ledger",
        "limit": "Saldo pada akhir bulan, bukan saldo saat ini atau dana yang bebas dibelanjakan. Kewajiban layanan, refund, pajak, dan pembayaran berikutnya tetap diperiksa.",
    }
    if cutoff is None:
        position["issues"].append(
            "Tahun pilihan belum memiliki akhir bulan yang sudah lewat."
        )
        return position
    if not accounts:
        position["issues"].append("Belum ada rekening perusahaan yang dicatat.")
        return position
    book_balance = report_balances(organization=organization, as_of=cutoff)["BANK"]
    position["book_balance"] = _money(book_balance)
    source_balances = []
    for account in accounts:
        account_issues = []
        first = (
            StatementPeriodReview.objects.filter(
                organization=organization,
                account=account,
                replacement__isnull=True,
                period_start__lte=cutoff,
            )
            .order_by("period_start", "pk")
            .first()
        )
        opening = (
            BankOpening.objects.filter(organization=organization, account=account)
            .select_related("journal")
            .first()
        )
        start = opening.date if opening else first.period_start if first else None
        if not start or start > cutoff:
            account_issues.append(
                "Saldo awal dan sumber sampai tanggal laporan belum diperiksa."
            )
        elif opening:
            opening_lines = opening.journal.lines.filter(
                organization=organization, account="BANK"
            ).aggregate(debit=Sum("debit"), credit=Sum("credit"))
            opening_book = (opening_lines["debit"] or ZERO) - (
                opening_lines["credit"] or ZERO
            )
            if (
                opening.journal.status != "posted"
                or opening.journal.organization_id != organization.pk
                or opening.journal.date != opening.date
                or opening.amount != account.opening_balance
                or opening.date != account.opening_date
                or opening_book != opening.amount
            ):
                account_issues.append("Saldo awal belum sesuai jurnal yang disahkan.")
            if (
                not first
                or first.period_start != start
                or first.opening_balance != opening.amount
            ):
                account_issues.append(
                    "Saldo awal pada e-statement belum sesuai saldo awal yang disahkan."
                )
        else:
            # The explicit source review establishes zero. A default model value
            # or missing ledger is insufficient evidence on its own.
            if (
                first.opening_balance != ZERO
                or account.opening_balance != ZERO
                or account.opening_date
                or BankTransaction.objects.filter(
                    organization=organization, account=account, date__lt=start
                ).exists()
                or _unassigned_bank_before(organization, account, start)
            ):
                account_issues.append(
                    "Saldo awal perlu disahkan. Saldo nol perlu pemeriksaan sumber nol, tanpa mutasi rekening sebelumnya atau jurnal bank lama yang belum tertaut rekening."
                )
        if start and start <= cutoff:
            coverage = statement_coverage(
                organization=organization, account=account, start=start, end=cutoff
            )
            row = coverage["accounts"][0]
            position["source_references"].append(
                {
                    "account_id": account.pk,
                    "start": str(start),
                    "cutoff": str(cutoff),
                    "review_ids": row["review_ids"],
                    "fingerprint": coverage["fingerprint"],
                }
            )
            if not row["verified"]:
                account_issues.extend(row["issues"])
            elif row["closing_balance"] is None:
                account_issues.append(
                    "Perlu saldo e-statement tepat pada tanggal akhir bulan laporan."
                )
            else:
                source_balances.append(Decimal(row["closing_balance"]))
            # Same-day allocations cannot be silently offset across accounts or
            # between unclassified income and outgoing payments.
            unmatched = (
                pending_bank_transactions(organization=organization)
                .filter(account=account, date__range=(start, cutoff))
                .count()
            )
            if unmatched:
                account_issues.append(
                    f"Masih ada {unmatched} mutasi yang belum dicocokkan seluruhnya."
                )
        for issue in dict.fromkeys(account_issues):
            position["issues"].append(f"Rekening {account.name}: {issue}")
    if len(source_balances) == len(accounts):
        total = sum(source_balances, ZERO)
        position["statement_balance"] = _money(total)
        position["difference"] = _money(total - book_balance)
        if total != book_balance:
            position["issues"].append(
                "Jumlah saldo e-statement berbeda dari saldo BANK buku besar. Minta Finance memeriksa saldo awal dan jurnal."
            )
    if not position["issues"] and position["statement_balance"] is not None:
        position["status"], position["balance"] = (
            "available",
            position["statement_balance"],
        )
    else:
        position["status"] = "review_required"
    return position


def _financial_snapshot(organization, year):
    journals = Journal.objects.filter(
        organization=organization, status="posted", date__year=year
    )
    posting_count = journals.count()
    rows = (
        JournalLine.objects.filter(
            organization=organization,
            journal__organization=organization,
            journal__status="posted",
            journal__date__year=year,
        )
        .values("account")
        .annotate(debit=Sum("debit"), credit=Sum("credit"))
    )
    movements = {row["account"]: row["debit"] - row["credit"] for row in rows}
    revenue = -movements["REVENUE"] if "REVENUE" in movements else None
    expenses = movements.get("EXPENSE")
    profit = (
        revenue - expenses if revenue is not None and expenses is not None else None
    )
    covered = sorted(
        {item.strftime("%Y-%m") for item in journals.values_list("date", flat=True)}
    )
    closed = list(
        AccountingPeriod.objects.filter(
            organization=organization, year=year, closed=True
        )
        .order_by("month")
        .values_list("month", flat=True)
    )
    bank = _bank_position(organization, year)
    bank_note = (
        f"Saldo {bank['account_count']} rekening pada {bank['cutoff']} telah dicocokkan dengan sumber dan buku. "
        if bank["status"] == "available"
        else f"Saldo bank pada {bank['cutoff']} belum dapat ditampilkan. "
        if bank["cutoff"]
        else "Saldo bank belum dapat ditampilkan. "
    )
    bank_note += bank["limit"]
    return {
        "posted_available": posting_count > 0,
        "posting_count": posting_count,
        "revenue": _money(revenue),
        "expenses": _money(expenses),
        "profit": _money(profit),
        "bank_balance": bank["balance"],
        "bank_position": bank,
        "covered_months": covered,
        "closed_months": closed,
        "coverage_label": (
            "Jurnal terposting yang tersedia dalam tahun kalender; kelengkapan pendapatan dan beban belum dinyatakan."
            if posting_count
            else "Belum ada jurnal terposting untuk tahun ini."
        ),
        "profit_note": BOOK_PROFIT_NOTE,
        "bank_note": bank_note,
        "source_url": reverse("reports")
        + f"?period={covered[-1] if covered else str(year) + '-12'}",
        "source_links": [
            {"period": period, "url": reverse("reports") + f"?period={period}"}
            for period in covered
        ],
    }


def _partner_rollups(data, month_lookup):
    grouped = defaultdict(list)
    for batch in data["batches"]:
        selected = {
            month.source_id for month in data["months"] if month.batch_id == batch.pk
        }
        counts = defaultdict(int)
        for detail in batch.payload.get("details", []):
            if detail["month_id"] in selected:
                counts[detail["channel_id"]] += detail["quantity"]
        for channel in batch.payload.get("channels", []):
            if channel["month_id"] in selected:
                grouped[channel["source_label"]].append(
                    {
                        "period": month_lookup[
                            (batch.pk, channel["month_id"])
                        ].source_period,
                        "printed_count": channel.get("printed_count"),
                        "printed_amount": channel.get("printed_amount"),
                        "detail_count": channel.get(
                            "detail_count", counts.get(channel["id"])
                        ),
                    }
                )
    partners = []
    for label, channels in grouped.items():
        periods = sorted({channel["period"] for channel in channels})
        partners.append(
            {
                "source_label": label,
                "reported_amount": _observed_sum(
                    [row["printed_amount"] for row in channels], money=True
                ),
                "printed_count": _observed_sum(
                    [row["printed_count"] for row in channels]
                ),
                "detail_count": _observed_sum(
                    [row["detail_count"] for row in channels]
                ),
                "month_count": len(periods),
                "months": periods,
                "unknown_amount_count": sum(
                    row["printed_amount"] is None for row in channels
                ),
                "source_url": reverse("imports_month", args=[periods[-1]]),
                "identity_status": "raw_source_label",
            }
        )
    return sorted(
        partners, key=lambda row: (-(row["printed_count"] or 0), row["source_label"])
    )


@transaction.atomic
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
    organization = Organization.objects.select_for_update().get(pk=organization_id)
    data = import_summary(organization=organization, year=year)
    month_lookup = {
        (month.batch_id, month.source_id): month for month in data["months"]
    }
    by_period = {month.source_period: month for month in data["months"]}
    source_url = reverse("imports_overview") + f"?year={year}"
    coverage = f"{len(data['months'])} dari 12 halaman bulanan tersedia; kelengkapan operasional belum dikonfirmasi."
    monthly = []
    for month_number in range(1, 13):
        period = f"{year}-{month_number:02d}"
        item = by_period.get(period)
        partial = bool(item and item.coverage == "partial_source_page")
        row = {
            "period": period,
            "label": MONTHS[month_number - 1],
            "available": item is not None,
            "reported_amount": _money(item.printed_amount) if item else None,
            "printed_count": item.printed_count if item else None,
            "detail_count": item.detail_count if item else None,
            "is_partial": partial,
            "source_page": item.source_page if item else None,
            "source_url": reverse("imports_month", args=[period])
            if item
            else source_url,
            "change_pct": None,
            "change_reason": "Perbandingan bulan berurutan belum tersedia.",
            "coverage": item.coverage if item else "missing",
            "comparison_basis": "Jumlah peserta tercetak pada halaman sumber; bukan pertumbuhan pendapatan.",
        }
        previous = monthly[-1] if monthly else None
        if previous and item and previous["available"]:
            if partial or previous["is_partial"]:
                row["change_reason"] = (
                    "Perbandingan ditahan karena salah satu halaman hanya mencakup sebagian bulan."
                )
            elif row["printed_count"] is None or previous["printed_count"] is None:
                row["change_reason"] = (
                    "Jumlah tercetak belum tersedia pada kedua halaman."
                )
            elif previous["printed_count"] == 0:
                row["change_reason"] = (
                    "Persentase tidak dihitung karena jumlah bulan pembanding nol."
                )
            else:
                row["change_pct"] = _money(
                    Decimal(row["printed_count"] - previous["printed_count"])
                    / Decimal(previous["printed_count"])
                    * 100
                )
                row["change_reason"] = (
                    "Perubahan jumlah tercetak antarhalaman; kelengkapan bisnis dan sebab perubahan belum diketahui."
                )
        monthly.append(row)
    exceptions = []
    for item in data["exceptions"]:
        source_month = month_lookup.get((item.batch_id, item.month_source_id))
        if item.month_source_id and source_month is None:
            continue
        exceptions.append(
            {
                "id": item.exception_id,
                "code": item.code,
                "severity": item.severity,
                "message": item.message,
                "period": source_month.source_period if source_month else None,
                "source_url": reverse(
                    "imports_month", args=[source_month.source_period]
                )
                if source_month
                else source_url,
            }
        )
    annual_records = []
    for batch in data["batches"]:
        annual = batch.payload.get("annual_summary") or {}
        covered_periods = annual.get("covered_periods", [])
        if covered_periods and any(
            not period.startswith(f"{year}-") for period in covered_periods
        ):
            continue
        printed = annual.get("printed_total")
        if isinstance(printed, dict):
            printed = printed.get("count", printed.get("printed_count"))
        if type(printed) is int:
            annual_records.append(
                {
                    "printed_count": printed,
                    "covered_periods": covered_periods,
                    "missing_periods": annual.get("missing_periods", []),
                    "coverage": annual.get("coverage", "unconfirmed"),
                    "source_page": annual.get("page"),
                }
            )
    source = {
        "available": bool(data["months"]),
        "reported_amount": _money(data["reported_amount"]),
        "printed_count": data["printed_count"],
        "detail_count": data["detail_count"],
        "month_count": len(data["months"]),
        "raw_label_count": data["channel_count"],
        "detail_cell_count": data["detail_rows"],
        "covered_months": data["covered_months"],
        "missing_months": data["missing_months"],
        "partial_months": [row["period"] for row in monthly if row["is_partial"]],
        "annual_printed_count": annual_records[0]["printed_count"]
        if len(annual_records) == 1
        else None,
        "annual_summaries": annual_records,
        "exception_count": len(exceptions),
        "coverage_label": coverage,
        "amount_month_count": sum(
            month.printed_amount is not None for month in data["months"]
        ),
        "count_month_count": sum(
            month.printed_count is not None for month in data["months"]
        ),
        "source_url": source_url,
        "basis_note": "Nominal dan peserta dilaporkan sumber; bukan kas, pendapatan buku besar, atau omzet pajak. Label mentah belum dipetakan menjadi identitas mitra.",
        "review_fingerprint": data.get("review_fingerprint", ""),
        "unresolved_count": data.get("unresolved_count", len(exceptions)),
        "resolved_count": data.get("resolved_count", 0),
    }
    finance = _financial_snapshot(organization, year)
    readiness = [
        {
            "key": "source",
            "label": "Arsip operasional",
            "status": "partial" if source["available"] else "missing",
            "detail": coverage,
            "source_url": source_url,
        },
        {
            "key": "source_review",
            "label": "Pemeriksaan sumber",
            "status": "review_required"
            if source["unresolved_count"]
            else ("available" if source["available"] else "missing"),
            "detail": f"{source['unresolved_count']} pengecualian belum selesai dari {len(exceptions)} catatan; penyelesaian tidak menyatakan kelengkapan bisnis.",
            "source_url": source_url,
        },
        {
            "key": "ledger",
            "label": "Buku besar",
            "status": "partial" if finance["posted_available"] else "missing",
            "detail": finance["coverage_label"],
            "source_url": finance["source_url"],
        },
        {
            "key": "bank",
            "label": "Saldo bank akhir bulan",
            "status": finance["bank_position"]["status"],
            "detail": finance["bank_note"],
            "issues": finance["bank_position"]["issues"],
            "source_url": reverse("bank"),
        },
        {
            "key": "marketing_attribution",
            "label": "Atribusi dan biaya akuisisi",
            "status": "missing",
            "detail": "Belum ada data lintas kanal yang direkonsiliasi ke pelanggan dan pesanan; CAC dan ROAS belum dapat dinilai.",
            "source_url": None,
        },
    ]
    facts = []

    def fact(
        fact_id,
        value,
        label,
        unit,
        basis="reported",
        fact_coverage=coverage,
        url=source_url,
    ):
        facts.append(
            {
                "fact_id": fact_id,
                "value": value,
                "label": label,
                "unit": unit,
                "basis": basis,
                "coverage": fact_coverage,
                "source_url": url,
            }
        )

    for key, label, unit in (
        ("reported_amount", "Nominal tercetak pada rekap bulanan", "IDR"),
        ("printed_count", "Peserta tercetak pada rekap bulanan", "peserta"),
        ("detail_count", "Peserta pada sel rincian sumber", "peserta"),
        ("month_count", "Halaman bulanan tersedia", "bulan"),
        ("raw_label_count", "Label kanal mentah yang berbeda", "label"),
        ("detail_cell_count", "Sel rincian kuantitas sumber", "sel"),
        ("exception_count", "Catatan pemeriksaan sumber", "catatan"),
        ("unresolved_count", "Pengecualian sumber belum selesai", "catatan"),
    ):
        fact(f"source.{key}", source[key], label, unit)
    annual_coverage = (
        "Halaman ringkasan tahunan terpisah; "
        + ", ".join(annual_records[0]["covered_periods"])
        if len(annual_records) == 1
        else "Ringkasan tahunan tunggal belum tersedia."
    )
    fact(
        "source.annual_printed_count",
        source["annual_printed_count"],
        "Peserta pada ringkasan tahunan sumber",
        "peserta",
        fact_coverage=annual_coverage,
    )
    for key, label in (
        ("revenue", "Pendapatan terposting tersedia"),
        ("expenses", "Beban terposting tersedia"),
        ("profit", "Selisih pendapatan dan beban sebelum pajak penghasilan"),
    ):
        fact_coverage = finance["coverage_label"] + (
            " " + finance["profit_note"] if key == "profit" else ""
        )
        fact(
            f"finance.{key}",
            finance[key],
            label,
            "IDR",
            "posted",
            fact_coverage,
            finance["source_url"],
        )
    fact(
        "finance.bank_balance",
        finance["bank_balance"],
        "Saldo bank akhir bulan",
        "IDR",
        "bank_reconciled" if finance["bank_balance"] is not None else "unavailable",
        finance["bank_note"],
        reverse("bank"),
    )
    facts[-1]["as_of"] = finance["bank_position"]["cutoff"]
    facts[-1]["status"] = finance["bank_position"]["status"]
    for row in monthly:
        month_coverage = (
            "Sebagian halaman bulan; belum lengkap."
            if row["is_partial"]
            else "Halaman sumber tersedia; kelengkapan bisnis belum dikonfirmasi."
            if row["available"]
            else "Halaman sumber belum tersedia."
        )
        for key, label, unit in (
            ("reported_amount", "Nominal tercetak", "IDR"),
            ("printed_count", "Peserta tercetak", "peserta"),
            ("detail_count", "Peserta rincian", "peserta"),
        ):
            fact(
                f"source.month.{row['period']}.{key}",
                row[key],
                f"{label} {row['period']}",
                unit,
                fact_coverage=month_coverage,
                url=row["source_url"],
            )
    from marketing.management import (
        COMMERCIAL_FACTS,
        MANAGEMENT_FACTS,
        management_snapshot,
    )

    management = management_snapshot(
        organization=organization, start=date(year, 1, 1), end=date(year, 12, 31)
    )
    for key, value in management["counts"].items():
        fact(
            "marketing." + key,
            value,
            MANAGEMENT_FACTS[key],
            "hari" if key.endswith("days") else "catatan",
            "operational_reviewed"
            if key in {"kpi_met", "kpi_not_met"}
            else "operational_recorded",
            management["scope"],
            "/marketing/management/",
        )
    for key, (label, unit) in COMMERCIAL_FACTS.items():
        fact(
            "operations." + key,
            management["commercial"][key],
            label,
            unit,
            "recorded_order_cohort",
            "Pesanan tercatat; kelengkapan seluruh sumber belum otomatis disahkan.",
            "/marketing/management/",
        )
    from .budget_reporting import budget_facts, budget_summary

    budgets = budget_summary(organization=organization, year=year)
    facts.extend(budget_facts(budgets))
    readiness.append(
        {
            "key": "budget",
            "label": "Budget dan tagihan Finance",
            "status": budgets["status"],
            "detail": budgets["coverage"],
            "issues": [row["message"] for row in budgets["issues"]],
            "source_url": reverse("director:budgets"),
        }
    )
    return {
        "schema_version": "director-snapshot-v3",
        "organization_id": organization_id,
        "year": year,
        "as_of": timezone.localdate(timezone=JAKARTA).isoformat(),
        "source": source,
        "monthly_series": monthly,
        "partners": _partner_rollups(data, month_lookup),
        "exceptions": exceptions,
        "readiness": readiness,
        "finance": finance,
        "facts": facts,
        "marketing_management": management,
        "budget": budgets,
    }
