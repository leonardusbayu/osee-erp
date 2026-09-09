"""Document-led tax readiness; these workpapers are not official filing outputs."""
from datetime import date
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Sum
from django.db.models.functions import TruncMonth
from django.utils import timezone

from core.audit import record
from core.models import Membership
from .models import TaxObligation, TaxProfile, TaxSource


SOURCE_REVIEW_DATE = date(2026, 9, 9)
SOURCE_CATALOG = [
    {
        "slug": "per11-2025", "title": "PER-11/PJ/2025 — pelaporan dan pembulatan", "article": "Pasal 129; lampiran sesuai jenis SPT",
        "url": "https://www.pajak.go.id/id/peraturan/ketentuan-pelaporan-pajak-penghasilan-pajak-pertambahan-nilai-pajak-penjualan-atas-0",
        "summary": "Pengisian DPP dan PPh masa terkait menggunakan rupiah penuh: kurang dari setengah dibulatkan ke bawah, setengah atau lebih ke atas. Paket internal tetap perlu validasi format resmi.",
        "topics": ["workflow", "documents", "pph23"], "effective_from": date(2025, 5, 22),
    },
    {
        "slug": "pp20-2026", "title": "PP 20 Tahun 2026 — perubahan PP 55", "article": "Pasal II ayat (1) huruf e",
        "url": "https://pajak.go.id/id/peraturan/perubahan-atas-peraturan-pemerintah-nomor-55-tahun-2022-tentang-penyesuaian-pengaturan-di",
        "summary": "PT biasa memerlukan pemeriksaan hak transisi atas masa fasilitas lama yang belum berakhir; omzet kecil saja tidak membuktikan kelayakan.",
        "topics": ["final", "difference", "documents"], "effective_from": date(2026, 4, 22),
    },
    {
        "slug": "pp55-2022", "title": "PP 55 Tahun 2022 — Pajak Penghasilan", "article": "Ketentuan omzet tertentu; baca bersama PP 20/2026",
        "url": "https://www.pajak.go.id/index.php/id/peraturan/penyesuaian-pengaturan-di-bidang-pajak-penghasilan",
        "summary": "PP 55 menggantikan PP 23/2018. Riwayat fasilitas dan perubahan PP 20/2026 harus dibaca bersama untuk menentukan masa yang berlaku.",
        "topics": ["final", "difference", "documents"],
    },
    {
        "slug": "pph23-law", "title": "UU 36 Tahun 2008 — Pajak Penghasilan", "article": "Pasal 23",
        "url": "https://pajak.go.id/id/undang-undang-nomor-36-tahun-2008",
        "summary": "Pemotongan PPh 23 bergantung pada jenis penghasilan dan pihak transaksi. Potongan kepada pemasok berbeda dari kredit atas potongan oleh pelanggan.",
        "topics": ["pph23", "difference", "nonpkp"],
    },
    {
        "slug": "ppn-law", "title": "UU PPN — susunan setelah UU HPP", "article": "Pasal 14; Pasal 3A dan 4 untuk transaksi luar negeri",
        "url": "https://www.pajak.go.id/sites/default/files/2021-12/SDSN%20UU%20PPN%20Indo-%20dengan%20tanda%20perubahan_UU%20HPP.pdf",
        "summary": "Non-PKP tidak membuat Faktur Pajak untuk penjualan domestik biasa. Transaksi luar negeri memerlukan pemeriksaan tersendiri.",
        "topics": ["vat", "nonpkp", "invoice"],
    },
    {
        "slug": "pp94-2010", "title": "PP 94 Tahun 2010 — penghitungan penghasilan kena pajak", "article": "Pasal 10",
        "url": "https://stats.pajak.go.id/id/peraturan/penghitungan-penghasilan-kena-pajak-dan-pelunasan-pajak-penghasilan-dalam-tahun-1",
        "summary": "PPN yang tidak dapat dikreditkan dapat mengikuti biaya atau aset dengan syarat fiskal terkait; bukan otomatis kredit pajak masukan.",
        "topics": ["vat", "nonpkp", "invoice"],
    },
    {
        "slug": "pmk81-2024", "title": "PMK 81 Tahun 2024 — administrasi Coretax", "article": "Pasal 94, 169, 171; perhatikan perubahan dan pengecualian masa",
        "url": "https://www.pajak.go.id/id/peraturan/ketentuan-perpajakan-dalam-rangka-pelaksanaan-sistem-inti-administrasi-perpajakan",
        "summary": "PPh masa terkait umumnya dibayar tanggal 15 dan dilaporkan tanggal 20 bulan berikutnya; SPT badan empat bulan setelah tahun buku. Pembayaran final omzet setor sendiri yang tervalidasi memenuhi pelaporan masa Pasal 171(4); hari libur dan pengecualian harus diperiksa.",
        "topics": ["deadline", "annual", "workflow", "payment"],
    },
    {
        "slug": "coretax-corporate", "title": "DJP — Lapor SPT Tahunan Badan", "article": "Panduan resmi Coretax",
        "url": "https://www.pajak.go.id/coretaxpedia/lapor-spt-tahunan-badan",
        "summary": "SPT tahunan badan memerlukan laporan dan rekonsiliasi yang lengkap. Bukti penerimaan resmi berbeda dari draf yang disiapkan ERP.",
        "topics": ["annual", "workflow", "documents", "payment"],
    },
    {
        "slug": "pmk168-2023", "title": "PMK 168 Tahun 2023 — pemotongan PPh 21", "article": "Penghasilan orang pribadi dari pekerjaan, jasa, atau kegiatan",
        "url": "https://pajak.go.id/id/peraturan/petunjuk-pelaksanaan-pemotongan-pajak-atas-penghasilan-sehubungan-dengan-pekerjaan-jasa-1",
        "summary": "Pembayaran tutor orang pribadi memerlukan klasifikasi penerima dan hubungan kerja/jasa; tidak otomatis PPh 23.",
        "topics": ["tutor", "pph23"],
    },
]


def seed_tax_sources(*, reviewed_by=None, review_reference=None):
    """Explicitly publish reviewed source versions; never activate company policy."""
    from .knowledge import publish_catalog
    publish_catalog(SOURCE_CATALOG, SOURCE_REVIEW_DATE, reviewed_by=reviewed_by, review_reference=review_reference)


def source_cards(sources):
    from .knowledge import source_cards as cards
    return cards(sources)


def parse_period(value=None):
    if not value:
        return timezone.localdate().replace(day=1)
    try:
        if len(value) != 7 or value[4] != "-":
            raise ValueError
        result = date(int(value[:4]), int(value[5:]), 1)
        if not 2000 <= result.year <= 2100:
            raise ValueError
        return result
    except (TypeError, ValueError):
        raise ValidationError("Pilih masa dengan format YYYY-MM.") from None


def profile_gates(organization, period):
    from .workflow import profile_for_period
    profile = profile_for_period(organization, period)
    gates = []
    def add(code, title, detail):
        gates.append({"code": code, "title": title, "detail": detail, "severity": "review"})
    if not profile or not profile.registration_date or not profile.registration_evidence:
        add("registration", "Lengkapi dokumen pendaftaran pajak", "Unggah atau catat referensi SKT/NPWP agar pemeriksa dapat memeriksa riwayat fasilitas.")
    if not profile or profile.regime == TaxProfile.Regime.UNDECIDED or not profile.reviewed_at or not getattr(profile, "external_review_id", None):
        add("regime", "Pemeriksa pajak belum mengaktifkan aturan", "Omzet di bawah Rp4,8 miliar tidak otomatis membuat PT biasa berhak memakai PPh final. ERP belum menghitungnya.")
    elif not profile.effective_from or period < profile.effective_from:
        add("historical_profile", "Profil belum berlaku untuk masa ini", "Status saat ini tidak membuktikan perlakuan pajak pada masa lampau.")
    elif profile.regime == TaxProfile.Regime.FINAL and (not profile.final_regime_end_date or period > profile.final_regime_end_date):
        add("final_expired", "Masa fasilitas perlu ditinjau", "Pemeriksa harus memastikan aturan setelah fasilitas berakhir sebelum nominal dihitung.")
    elif profile.regime == TaxProfile.Regime.FINAL and profile.effective_from.year != period.year:
        add("final_year_review", "Kelayakan final tahun ini perlu ditinjau", "Dokumen omzet dan syarat tahun sebelumnya harus diperiksa untuk setiap tahun pajak; periode fasilitas bukan persetujuan kelayakan seluruh tahun secara otomatis.")
    if not profile or not profile.vat_status_effective_from or not profile.vat_status_evidence:
        add("vat_history", "Lengkapi tanggal berlaku status PPN", "Status non-PKP saat ini belum menjadi bukti status untuk seluruh transaksi lampau.")
    elif period < profile.vat_status_effective_from:
        add("vat_historical_period", "Bukti status PPN belum mencakup masa ini", "Pemeriksa memerlukan bukti yang berlaku pada masa transaksi tersebut.")
    return profile, gates


def monthly_tax_context(organization, period=None):
    from finance.models import Bill
    from .knowledge import current_sources
    period = parse_period(period) if not isinstance(period, date) else period.replace(day=1)
    profile, gates = profile_gates(organization, period)
    obligations = list(TaxObligation.objects.filter(organization=organization, period=period))
    totals = []
    for direction, label in TaxObligation.Direction.choices:
        rows = [item for item in obligations if item.direction == direction]
        totals.append({"direction": direction, "label": label, "amount": sum((item.amount for item in rows if item.amount is not None), Decimal("0.00")), "count": len(rows), "unknown_count": sum(item.amount is None for item in rows)})
    next_month = date(period.year + (period.month == 12), period.month % 12 + 1, 1)
    candidates = []
    bills = Bill.objects.filter(organization=organization, supplier__organization=organization, date__gte=period, date__lt=next_month).select_related("supplier")
    for bill in bills:
        reason = ""
        if bill.tax_status != "reviewed":
            reason = "Jenis transaksi, penerima, dan bukti perlu diperiksa sebelum menentukan pemotongan."
        elif bill.tax_amount is None:
            reason = "Status ditinjau belum disertai nominal pajak yang diputuskan pemeriksa."
        elif bill.status == "approved" and bill.tax_amount > 0 and not hasattr(bill, "tax_decision"):
            reason = "Pemotongan lama belum ditautkan dengan keputusan pemeriksaan dan kewajiban pajak."
        if reason:
            candidates.append({"source_kind": "bill", "bill_id": bill.pk, "number": bill.number, "supplier_name": bill.supplier.name, "gross_amount": bill.amount, "reason": reason, "status": "needs_review"})
    return {
        "period": period, "period_value": period.strftime("%Y-%m"), "profile": profile,
        "gates": gates, "profile_ready": not gates, "obligations": obligations, "tax_totals": totals,
        "review_count": sum(item.status != "approved" for item in obligations),
        "tax_candidates": candidates, "tax_candidate_count": len(candidates),
        "unknown_amount_count": sum(item.amount is None for item in obligations),
        "draft_notice": "Kertas kerja DRAF. Bukan SPT, bukan XML DJP, dan belum membuktikan pajak dibayar atau dilaporkan.",
        "payment_due_standard": next_month.replace(day=15), "filing_due_standard": next_month.replace(day=20),
        "deadline_note": "Tanggal standar PPh masa terkait. Hari libur, jenis kewajiban, dan kebijakan khusus masa harus dikonfirmasi pemeriksa; belum kalender tenggat resmi.",
        "sources": source_cards(current_sources(slugs=["pp20-2026", "pph23-law", "pmk81-2024"])),
    }


def annual_readiness(organization, year=None):
    from finance.models import AccountingPeriod, JournalLine
    year = int(year or timezone.localdate().year)
    if not 2000 <= year <= 2100:
        raise ValidationError("Tahun tidak valid.")
    start = date(year, organization.fiscal_year_start_month, 1)
    end_next = date(year + 1, organization.fiscal_year_start_month, 1)
    from datetime import timedelta
    end = end_next - timedelta(days=1)
    profile, gates = profile_gates(organization, start)
    cursor = start
    year_gates = []
    for _ in range(12):
        _, monthly_gates = profile_gates(organization, cursor)
        for gate in monthly_gates:
            year_gates.append({**gate, "period": cursor.strftime("%Y-%m"), "title": f"{cursor:%Y-%m}: {gate['title']}"})
        cursor = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
    gates = year_gates
    rows = list(TaxObligation.objects.filter(organization=organization, period__gte=start, period__lt=end_next))
    lines = JournalLine.objects.filter(organization=organization, journal__organization=organization, journal__status="posted", journal__date__gte=start, journal__date__lt=end_next, account__in=["REVENUE", "EXPENSE"])
    grouped = lines.annotate(month=TruncMonth("journal__date")).values("month", "account").annotate(debit=Sum("debit"), credit=Sum("credit"))
    amounts = {(row["month"].year, row["month"].month, row["account"]): row["debit"] - row["credit"] for row in grouped}
    closed = set(AccountingPeriod.objects.filter(organization=organization, closed=True, year__gte=start.year, year__lte=end.year).values_list("year", "month"))
    ledger_months = []
    cursor = start
    for _ in range(12):
        revenue = -amounts.get((cursor.year, cursor.month, "REVENUE"), Decimal("0.00"))
        expenses = amounts.get((cursor.year, cursor.month, "EXPENSE"), Decimal("0.00"))
        ledger_months.append({"period": cursor, "year": cursor.year, "month": cursor.month, "revenue": revenue, "expenses": expenses, "book_profit": revenue - expenses, "closed": (cursor.year, cursor.month) in closed})
        cursor = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
    closed_count = sum(month["closed"] for month in ledger_months)
    book_revenue = sum((month["revenue"] for month in ledger_months), Decimal("0.00"))
    book_expenses = sum((month["expenses"] for month in ledger_months), Decimal("0.00"))
    checks = [
        {"title": "Profil dan aturan pajak", "status": "review" if gates else "recorded", "detail": "Dokumen dan aturan harus mencakup seluruh tahun buku; persetujuan satu masa tidak menggantikan pemeriksaan setahun."},
        {"title": "Penutupan buku dan rekonsiliasi bank", "status": "review", "detail": f"{closed_count} dari 12 bulan ditutup. Rekonsiliasi seluruh rekening, uang muka reseller, pendapatan setelah layanan, biaya, aset, dan utang."},
        {"title": "Kertas kerja pajak masa", "status": "review", "detail": f"{len(rows)} catatan tersedia. Kelengkapan semua kewajiban belum disahkan; tidak ada catatan bukan berarti nihil."},
        {"title": "Kredit pajak dan koreksi fiskal", "status": "review", "detail": "Periksa bukti potong pelanggan, biaya fiskal, penyusutan, dan perbedaan akuntansi/pajak bersama pemeriksa."},
        {"title": "Persetujuan dan bukti pelaporan", "status": "review", "detail": "Draf belum diajukan. Bukti penerimaan resmi disimpan setelah pelaporan di saluran DJP yang disetujui."},
    ]
    from .knowledge import current_sources
    return {"year": year, "fiscal_start": start, "fiscal_end": end, "profile": profile, "gates": gates, "checks": checks, "ready": False, "readiness_label": "Persiapan — memerlukan pemeriksaan", "obligations": rows, "ledger_months": ledger_months, "book_revenue": book_revenue, "book_expenses": book_expenses, "book_profit": book_revenue - book_expenses, "closed_month_count": closed_count, "ledger_basis": "Akuntansi komersial dari jurnal terposting; bukan peredaran bruto pajak, penghasilan kena pajak, atau bukti kas lengkap. Nol berarti belum ada nilai terposting pada akun tersebut, bukan bukti aktivitas nihil.", "deadline_note": "Batas umum SPT badan: empat bulan setelah akhir tahun buku. Kalender resmi dan kebijakan khusus harus diverifikasi.", "draft_notice": "Ringkasan persiapan ini bukan SPT Tahunan dan tidak berarti telah dilaporkan.", "sources": source_cards(current_sources(slugs=["pmk81-2024", "coretax-corporate"]))}


# Stable public domain-service seams used by Finance and the web layer.
from .workflow import (approve_tax_profile, approve_tax_obligation, prepare_obligation,
    review_bill_tax, record_external_review, upload_evidence, verify_obligation_evidence,
    prepare_annual_workpaper, approve_annual_workpaper, verify_annual_filing)
