"""Tenant-scoped marketing facts. Receipt dates and lead cohorts stay separate."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from core.models import Organization
from director.models import MarketingObservation

from .ad_sources import ad_source_state
from .analytics_metrics import (
    CASH_NOTE,
    COST_COVERAGE_NOTE,
    FILTER_KEYS,
    GROUP_BY_CHOICES,
    JAKARTA,
    PRODUCT_SPEND_NOTE,
    _number,
)
from .analytics_period import _period


@dataclass(frozen=True)
class QualityNotesContext:
    summary: dict
    rows: list
    coverage: dict
    group_by: str
    filters: dict
    start: date
    end: date
    today: date
    comparison_span: timedelta


def _date(value):
    if isinstance(value, datetime):
        raise ValidationError("Gunakan tanggal tanpa waktu untuk periode analisis.")
    try:
        return value if isinstance(value, date) else date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValidationError("Tanggal analisis tidak valid.") from None


def _validate(start, end, group_by, filters):
    start, end = _date(start), _date(end)
    if end < start or (end - start).days > 730 or start.year < 2000 or end.year > 2200:
        raise ValidationError(
            "Periode analisis harus berurutan, maksimal 731 hari, dalam tahun 2000–2200."
        )
    if group_by not in dict(GROUP_BY_CHOICES):
        raise ValidationError("Dimensi analisis tidak dikenal.")
    if filters is not None and not isinstance(filters, dict):
        raise ValidationError("Filter analisis tidak valid.")
    clean = {}
    for key, value in (filters or {}).items():
        if key not in FILTER_KEYS:
            raise ValidationError("Filter analisis tidak dikenal.")
        if value in (None, ""):
            continue
        if key in {"campaign", "member", "product"}:
            try:
                if (
                    isinstance(value, bool)
                    or str(int(value)) != str(value)
                    or int(value) <= 0
                ):
                    raise ValueError
                value = int(value)
            except (TypeError, ValueError):
                raise ValidationError("Identitas filter tidak valid.") from None
        elif not isinstance(value, str) or len(value) > 200:
            raise ValidationError("Nilai filter tidak valid.")
        clean[key] = value
    return start, end, clean


def _quality_notes(context: QualityNotesContext):
    summary = context.summary
    rows = context.rows
    coverage = context.coverage
    group_by = context.group_by
    filters = context.filters
    start = context.start
    end = context.end
    today = context.today
    comparison_span = context.comparison_span
    notes = [
        "Invoice dari satu pesanan dapat terkait ke satu peluang pembelian; setiap invoice tetap dihitung sekali. Pesanan dengan beberapa produk masuk kelompok multiproduk dan tidak masuk filter satu produk; rincian produk tersedia pada pesanan.",
        "Dimensi anggota mengacu PIC saat prospek dicatat. Alih tugas mengubah PIC operasional, bukan atribusi historis.",
        "Uang masuk bruto memakai alokasi penerimaan bank ke invoice, termasuk penerimaan setelah potongan PPh23. Kas setelah refund mengurangi refund invoice aktual yang dibayar melalui bank pada tanggal mutasi; kewajiban refund yang belum dibayar, refund uang muka, chargeback, dan penerimaan/piutang lama tidak masuk hitungan. Kas setelah refund bukan laba.",
        "Konversi pembayaran memakai prospek dalam periode dan pelunasan yang diterapkan ke invoice sampai akhir pengamatan, termasuk uang muka dan kredit pajak terverifikasi. Penerapan uang muka dan kredit pajak bukan uang masuk kedua.",
        "Rasio kas/belanja periode adalah indikator operasional, bukan ROAS kausal atau ROAS cohort. Jangan menjumlahkan rasio antar baris.",
        "Respons memakai lead dengan waktu respons tercatat; respons kosong tidak dianggap nol menit. Perbandingan anggota perlu mempertimbangkan kanal, segmen, beban, dan umur lead.",
        "Janji masuk hitungan menurut tanggal tenggat. Tepat waktu memerlukan bukti penyelesaian yang tercatat dan terjadi selambatnya pada tenggat; koreksi yang dicatat sesudah tenggat tidak dapat mengubahnya menjadi tepat waktu.",
        "Prospek lunas penuh berarti semua invoice aktif yang masih bernilai neto untuk prospek itu telah dilunasi; pembayaran sebagian tetap terlihat sebagai pembayaran, tetapi tidak sebagai lunas.",
        "Cakupan pengaitan tidak menjamin seluruh mutasi bank atau pengeluaran perusahaan telah diimpor.",
    ]
    if Decimal(coverage["unattributed_receipts"]) > 0:
        notes.append(
            "Sebagian penerimaan belum terhubung dengan lead. Lengkapi pengaitan invoice sebelum menyimpulkan kontribusi marketing."
        )
    if Decimal(coverage["unattributed_invoice_refunds"]) > 0:
        notes.append(
            "Sebagian refund invoice aktual belum terhubung dengan lead. Cakupan ini terlihat terpisah dan refund tersebut tidak dibebankan ke kampanye atau tim tertentu."
        )
    if not summary["spend_complete"]:
        notes.append(summary["spend_note"])
    notes.append(summary["source_coverage_note"])
    if group_by == "member" or "member" in filters:
        notes.append(
            "Aktivitas follow-up masuk ke kelompok PIC prospek saat prospek dicatat; angka ini bukan pembagian berdasarkan akun yang mencatat kontak."
        )
    if (
        any(not item["product_mapping_complete"] for item in rows)
        and PRODUCT_SPEND_NOTE not in notes
    ):
        notes.append(PRODUCT_SPEND_NOTE)
    if (
        any(not item["cash_cost_coverage_complete"] for item in rows)
        and COST_COVERAGE_NOTE not in notes
    ):
        notes.append(COST_COVERAGE_NOTE)
    if group_by == "ad" or "ad" in filters:
        notes.append(CASH_NOTE)
    if end < timezone.localdate(timezone=JAKARTA):
        notes.append(
            "Antrean follow-up yang terlambat hanya menunjukkan kondisi hari ini; angka janji tepat waktu memakai riwayat tenggat dan penyelesaian."
        )
    if end > today:
        notes.append(
            f"Data aktual hanya dihitung sampai {today.isoformat()}. Periode masa depan belum memiliki hasil; mutasi dan belanja bertanggal masa depan dikecualikan."
        )
        if start <= today:
            notes.append(
                f"Perbandingan menggunakan {comparison_span.days + 1} hari yang sudah teramati dan periode sebelumnya dengan panjang sama."
            )
    notes.append(
        "Sepakat membeli belum berarti lunas. Follow-up tetap memuat prospek sepakat dengan invoice yang belum lunas; piutang ini bukan penerimaan kas."
    )
    return notes


def _legacy_summary(organization, start, end):
    old = MarketingObservation.objects.filter(
        organization=organization, status="active", date__range=(start, end)
    )
    return {
        "reported_spend": _number(old.aggregate(value=Sum("spend"))["value"]),
        "reported_value": _number(old.aggregate(value=Sum("reported_value"))["value"]),
        "rows": old.count(),
        "basis": "Rekap lama seluruh organisasi pada periode ini, tanpa filter dimensi; tidak digabungkan dengan kas atau belanja iklan rinci.",
    }


@transaction.atomic
def marketing_snapshot(*, organization, start, end, group_by="channel", filters=None):
    Organization.objects.select_for_update().get(pk=organization.pk)
    start, end, filters = _validate(start, end, group_by, filters)
    today = timezone.localdate(timezone=JAKARTA)
    previous_end = start - timedelta(days=1)
    comparison_span = min(end, today) - start if start <= today else end - start
    previous_start = previous_end - comparison_span
    summary, rows, coverage, fingerprint = _period(
        organization, start, end, group_by, filters
    )
    previous, _, _, _ = _period(
        organization, previous_start, previous_end, group_by, filters
    )
    quality = _quality_notes(
        QualityNotesContext(
            summary=summary,
            rows=rows,
            coverage=coverage,
            group_by=group_by,
            filters=filters,
            start=start,
            end=end,
            today=today,
            comparison_span=comparison_span,
        )
    )
    return {
        "organization_id": organization.pk,
        "as_of": timezone.now().isoformat(),
        "formula_version": "marketing-performance-v6",
        "attribution_model": "Kredit penjualan mengikuti sumber pertama yang dicatat pada prospek; kontak follow-up tidak memindahkan atribusi. Satu peluang–satu pesanan dengan beberapa invoice; tautan invoice lama tetap didukung. Setiap invoice dihitung sekali; kanal sumber bukan bukti sebab akibat. Kas bruto dan refund aktual ditampilkan terpisah sebelum kas setelah refund.",
        "period": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "previous_start": previous_start.isoformat(),
            "previous_end": previous_end.isoformat(),
        },
        "observation_end": min(end, today).isoformat(),
        "observed_through": min(end, today).isoformat(),
        "group_by": group_by,
        "filters": filters,
        "dimension_choices": GROUP_BY_CHOICES,
        "summary": summary,
        "previous_summary": previous,
        "rows": rows,
        "coverage": coverage,
        "data_quality": quality,
        "legacy": _legacy_summary(organization, start, end),
        "invoice_attribution_fingerprint": fingerprint,
        "ad_sources": ad_source_state(
            organization=organization, start=start, end=min(end, today), filters=filters
        ),
    }
