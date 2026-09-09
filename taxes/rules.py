"""Bounded IDR workpaper rules. Classification requires documented professional review.

These are application calculations, not a claim of DJP XML acceptance. PER-11/PJ/2025
Article 129 supplies current monthly whole-rupiah rounding; earlier periods are gated.
"""
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP
from django.core.exceptions import ValidationError
from django.utils import timezone

CALCULATION_VERSION = "osee-idr-tax-2026-09-v1"
MONTHLY_START = date(2025, 1, 1)
RULE_REVIEW_DATE = date(2026, 9, 9)
RULES = {
    "no_withholding": ("Tidak dipotong — memerlukan dasar pemeriksaan", Decimal("0")),
    "pph23_service_2": ("PPh 23 jasa/sewa yang memenuhi ketentuan — 2%", Decimal("0.02")),
    "pph23_other_15": ("PPh 23 penghasilan tertentu — 15%", Decimal("0.15")),
    "final_turnover_005": ("PPh final omzet yang memenuhi syarat — 0,5%", Decimal("0.005")),
}


def number(value, *, signed=False):
    try:
        if isinstance(value, (float, bool)):
            raise ValueError
        result = Decimal(str(value))
        if not result.is_finite() or abs(result) >= Decimal("1000000000000000") or (not signed and result < 0) or result.as_tuple().exponent < -2:
            raise ValueError
        return result
    except (InvalidOperation, TypeError, ValueError):
        raise ValidationError("Nominal harus berupa desimal rupiah yang sah, maksimal dua angka pecahan.") from None


def calculate_tax(rule_code, base):
    ensure_rule_current()
    if rule_code not in RULES:
        raise ValidationError("Aturan belum didukung; jangan menerapkan tarif tebakan.")
    raw_base = number(base)
    rounded_base = raw_base.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    rate = RULES[rule_code][1]
    return {"base": rounded_base, "raw_base": raw_base, "rate": rate,
            "amount": (rounded_base * rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP),
            "version": CALCULATION_VERSION, "rounding": "PER-11/PJ/2025 Pasal 129: rupiah penuh, half-up"}


def ensure_rule_current():
    if not 0 <= (timezone.localdate() - RULE_REVIEW_DATE).days <= 180:
        raise ValidationError("Versi kalkulator memerlukan pemeriksaan aturan terbaru sebelum perhitungan/persetujuan baru.")


def validate_monthly_period(period):
    if not isinstance(period, date) or period.day != 1 or period < MONTHLY_START or period.year > 2026 or period > timezone.localdate().replace(day=1):
        raise ValidationError("Kalkulator masa hanya mendukung Januari 2025 sampai masa berjalan; masa lainnya perlu versi aturan tersendiri.")


def validate_final_profile(profile):
    if profile.organization.legal_form != "ordinary_pt" or profile.organization.fiscal_year_start_month != 1:
        raise ValidationError("Kalkulator transisi rilis ini hanya mencakup PT biasa dengan tahun buku kalender; profil lain perlu aturan tersendiri.")
    registration = profile.registration_date
    if not registration or registration >= date(2026, 4, 22):
        raise ValidationError("PT biasa yang baru terdaftar sejak PP 20/2026 tidak mendapat periode final baru dari batas omzet saja.")
    first_year = max(registration.year, 2018)
    expected_start, expected_end = date(first_year, 1, 1), date(first_year + 2, 12, 31)
    if profile.final_regime_start_date != expected_start or profile.final_regime_end_date != expected_end:
        raise ValidationError(f"Periode awal PT biasa harus sesuai riwayat tiga tahun pajak: {expected_start} sampai {expected_end}; tidak dimulai ulang oleh PP 55/PP 20.")
    if not profile.effective_from or not expected_start <= profile.effective_from <= expected_end:
        raise ValidationError("Masa aktivasi berada di luar fasilitas lama.")
    if profile.effective_from >= date(2026, 4, 22) and expected_end < date(2026, 4, 22):
        raise ValidationError("Fasilitas telah berakhir sebelum transisi PP 20/2026.")
    decision = profile.external_review.decision if profile.external_review_id else {}
    turnover = decision.get("qualifying_turnover")
    if turnover is None or number(turnover) > Decimal("4800000000") or decision.get("legacy_eligibility_confirmed") is not True or decision.get("no_normal_election_confirmed") is not True:
        raise ValidationError("Laporan profesional harus menegaskan kelayakan fasilitas lama, omzet yang memenuhi syarat, dan tidak adanya pilihan tarif umum/pengecualian yang menggugurkan fasilitas.")


def calculate_annual(*, book_profit, positive_adjustments, negative_adjustments,
                     loss_compensation, turnover, regime_code, credits, instalments):
    ensure_rule_current()
    profit = number(book_profit, signed=True)
    positive, negative, losses = map(number, (positive_adjustments, negative_adjustments, loss_compensation))
    turnover, credits, instalments = map(number, (turnover, credits, instalments))
    if regime_code not in ("corporate_22", "corporate_31e_small_11", "final_only"):
        raise ValidationError("Pilih perlakuan tahunan yang tercantum dalam laporan pemeriksa.")
    if regime_code == "corporate_31e_small_11" and not 0 < turnover <= Decimal("4800000000"):
        raise ValidationError("Kalkulator 31E penuh hanya mendukung peredaran bruto terverifikasi hingga Rp4,8 miliar; bukan penetapan otomatis kelayakan.")
    net = profit + positive - negative
    if losses > max(net, Decimal("0")):
        raise ValidationError("Kompensasi kerugian tidak boleh melebihi penghasilan neto fiskal positif.")
    pkp = (max(net - losses, Decimal("0")) / 1000).quantize(Decimal("1"), rounding=ROUND_DOWN) * 1000
    rate = Decimal("0.22") if regime_code == "corporate_22" else Decimal("0.11")
    if regime_code == "final_only":
        if pkp or credits or instalments:
            raise ValidationError("Final saja memerlukan rekonsiliasi penghasilan normal nihil dan tidak mengkreditkan pemotongan/angsuran normal otomatis.")
        rate = Decimal("0")
    tax = (pkp * rate).quantize(Decimal("1"), rounding=ROUND_DOWN)
    return {"book_profit": str(profit), "positive_adjustments": str(positive), "negative_adjustments": str(negative),
            "fiscal_net": str(net), "loss_compensation": str(losses), "taxable_income": str(pkp),
            "rate": str(rate), "income_tax": str(tax), "credits": str(credits), "instalments": str(instalments),
            "balance_due": str(tax - credits - instalments), "turnover": str(turnover), "regime_code": regime_code,
            "version": CALCULATION_VERSION, "status": "draft_workpaper", "official_schema_validated": False}
