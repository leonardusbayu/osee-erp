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


SOURCE_REVIEW_DATE = date(2026, 9, 7)
SOURCE_CATALOG = [
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
        "summary": "Aturan umum pembayaran PPh masa terkait tanggal 15, pelaporan tanggal 20 bulan berikutnya, dan SPT badan empat bulan setelah akhir tahun buku; pengecualian perlu kalender terverifikasi.",
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


def seed_tax_sources():
    """Seed reviewed general guidance; never activate a company's tax policy."""
    for source in SOURCE_CATALOG:
        values = dict(source)
        slug = values.pop("slug")
        TaxSource.objects.get_or_create(slug=slug, defaults={**values, "reviewed_on": SOURCE_REVIEW_DATE, "approved": True})


def source_cards(sources):
    return [{"id": item.slug, "title": item.title, "url": item.url, "article": item.article, "reviewed_on": item.reviewed_on.isoformat()} for item in sources]


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
    profile = TaxProfile.objects.filter(organization=organization).first()
    gates = []
    def add(code, title, detail):
        gates.append({"code": code, "title": title, "detail": detail, "severity": "review"})
    if not profile or not profile.registration_date or not profile.registration_evidence:
        add("registration", "Lengkapi dokumen pendaftaran pajak", "Unggah atau catat referensi SKT/NPWP agar pemeriksa dapat memeriksa riwayat fasilitas.")
    if not profile or profile.regime == TaxProfile.Regime.UNDECIDED or not profile.reviewed_at:
        add("regime", "Pemeriksa pajak belum mengaktifkan aturan", "Omzet di bawah Rp4,8 miliar tidak otomatis membuat PT biasa berhak memakai PPh final. ERP belum menghitungnya.")
    elif not profile.effective_from or period < profile.effective_from:
        add("historical_profile", "Profil belum berlaku untuk masa ini", "Status saat ini tidak membuktikan perlakuan pajak pada masa lampau.")
    elif profile.regime == TaxProfile.Regime.FINAL and (not profile.final_regime_end_date or period > profile.final_regime_end_date):
        add("final_expired", "Masa fasilitas perlu ditinjau", "Pemeriksa harus memastikan aturan setelah fasilitas berakhir sebelum nominal dihitung.")
    if not profile or not profile.vat_status_effective_from or not profile.vat_status_evidence:
        add("vat_history", "Lengkapi tanggal berlaku status PPN", "Status non-PKP saat ini belum menjadi bukti status untuk seluruh transaksi lampau.")
    elif period < profile.vat_status_effective_from:
        add("vat_historical_period", "Bukti status PPN belum mencakup masa ini", "Pemeriksa memerlukan bukti yang berlaku pada masa transaksi tersebut.")
    return profile, gates


def monthly_tax_context(organization, period=None):
    from finance.models import Bill
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
        elif bill.status == "approved" and bill.tax_amount > 0:
            reason = "Pemotongan positif tercatat; alur jurnal pemotongan dan penyetorannya belum didukung dalam rilis ini."
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
        "sources": source_cards(TaxSource.objects.filter(approved=True, slug__in=["pp20-2026", "pph23-law", "pmk81-2024"])),
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
    return {"year": year, "fiscal_start": start, "fiscal_end": end, "profile": profile, "gates": gates, "checks": checks, "ready": False, "readiness_label": "Persiapan — memerlukan pemeriksaan", "obligations": rows, "ledger_months": ledger_months, "book_revenue": book_revenue, "book_expenses": book_expenses, "book_profit": book_revenue - book_expenses, "closed_month_count": closed_count, "ledger_basis": "Akuntansi komersial dari jurnal terposting; bukan peredaran bruto pajak, penghasilan kena pajak, atau bukti kas lengkap. Nol berarti belum ada nilai terposting pada akun tersebut, bukan bukti aktivitas nihil.", "deadline_note": "Batas umum SPT badan: empat bulan setelah akhir tahun buku. Kalender resmi dan kebijakan khusus harus diverifikasi.", "draft_notice": "Ringkasan persiapan ini bukan SPT Tahunan dan tidak berarti telah dilaporkan.", "sources": source_cards(TaxSource.objects.filter(approved=True, slug__in=["pmk81-2024", "coretax-corporate"]))}


@transaction.atomic
def approve_tax_profile(profile, actor, *, regime, effective_from, review_note, final_regime_start_date=None, final_regime_end_date=None, transition_evidence=""):
    if not Membership.objects.filter(user=actor, organization=profile.organization, role="reviewer").exists():
        raise PermissionDenied("Persetujuan aturan membutuhkan pemeriksa pajak yang ditunjuk.")
    profile = TaxProfile.objects.select_for_update().get(pk=profile.pk)
    if regime not in (TaxProfile.Regime.FINAL, TaxProfile.Regime.NORMAL):
        raise ValidationError("Pilih aturan yang telah diperiksa.")
    profile.regime = regime
    profile.effective_from = effective_from
    profile.review_note = review_note
    profile.final_regime_start_date = final_regime_start_date
    profile.final_regime_end_date = final_regime_end_date
    profile.transition_evidence = transition_evidence
    profile.reviewed_by = actor
    profile.reviewed_at = timezone.now()
    profile.save()
    record(profile.organization, actor, "tax.profile.approved", profile, {"regime": regime, "effective_from": effective_from.isoformat()})
    return profile


@transaction.atomic
def approve_tax_obligation(obligation, actor):
    if not Membership.objects.filter(user=actor, organization=obligation.organization, role="reviewer").exists():
        raise PermissionDenied("Kertas kerja memerlukan pemeriksa pajak yang ditunjuk.")
    obligation = TaxObligation.objects.select_for_update().get(pk=obligation.pk)
    obligation.status = TaxObligation.Status.APPROVED
    obligation.reviewed_by = actor
    obligation.reviewed_at = timezone.now()
    obligation.save()
    record(obligation.organization, actor, "tax.obligation.approved", obligation, {"period": obligation.period.isoformat(), "tax_type": obligation.tax_type, "direction": obligation.direction})
    return obligation
