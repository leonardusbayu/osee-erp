"""Read-only PDF renderers. Callers authorize and scope all supplied records."""

from decimal import Decimal
from io import BytesIO
from xml.sax.saxutils import escape

from django.conf import settings
from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from finance.models import JournalLine


INK = colors.HexColor("#222121")
BRAND_RED = colors.HexColor("#B51010")
DEEP_RED = colors.HexColor("#730808")
MUTED = colors.HexColor("#726966")
LINE = colors.HexColor("#EAECF0")
PALE = colors.HexColor("#F8F8F8")
AMBER = colors.HexColor("#895a12")
PAGE_WIDTH, PAGE_HEIGHT = A4
MARGIN = 42
WIDTH = PAGE_WIDTH - 2 * MARGIN
MONTHS = ("", "Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli", "Agustus", "September", "Oktober", "November", "Desember")

STYLES = {
    "body": ParagraphStyle("body", fontName="Helvetica", fontSize=9, leading=13, textColor=INK, spaceAfter=6),
    "small": ParagraphStyle("small", fontName="Helvetica", fontSize=8, leading=11, textColor=MUTED),
    "heading": ParagraphStyle("heading", fontName="Helvetica-Bold", fontSize=12, leading=16, textColor=INK, spaceBefore=18, spaceAfter=9),
    "label": ParagraphStyle("label", fontName="Helvetica-Bold", fontSize=8, leading=11, textColor=MUTED),
    "value": ParagraphStyle("value", fontName="Helvetica-Bold", fontSize=15, leading=20, textColor=INK),
    "cell": ParagraphStyle("cell", fontName="Helvetica", fontSize=8.5, leading=12, textColor=INK),
    "headcell": ParagraphStyle("headcell", fontName="Helvetica-Bold", fontSize=8, leading=11, textColor=BRAND_RED),
    "legal": ParagraphStyle("legal", fontName="Helvetica", fontSize=8, leading=11, textColor=MUTED, alignment=TA_RIGHT),
    "right": ParagraphStyle("right", fontName="Helvetica", fontSize=8.5, leading=12, textColor=INK, alignment=TA_RIGHT),
    "notice": ParagraphStyle("notice", fontName="Helvetica", fontSize=8.5, leading=12, textColor=AMBER, spaceAfter=7),
}


def _plain(value):
    # Names may contain valid newlines. Let the layout wrap text instead of
    # turning user whitespace into forced lines in unsplittable table cells.
    text = str(value).translate(str.maketrans({"\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-", "\u2212": "-", "\u00a0": " "}))
    return " ".join(text.split())


def _p(value, style="body"):
    return Paragraph(escape(_plain(value)), STYLES[style])


def _amount(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("Nilai keuangan harus berupa angka terbatas.")
    return result.quantize(Decimal("0.01"))


def _money(value):
    amount = _amount(value)
    number = f"{abs(amount):,.2f}".translate(str.maketrans({",": ".", ".": ","}))
    return f"{'-' if amount < 0 else ''}Rp{number}"


def _date(value):
    return f"{value.day} {MONTHS[value.month]} {value.year}" if value else "Belum ditentukan"


def _table(rows, widths, *, header=True):
    table = Table(rows, colWidths=widths, repeatRows=1 if header else 0, hAlign="LEFT")
    commands = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ("LINEBELOW", (0, 0), (-1, -1), 0.5, LINE),
    ]
    if header:
        commands.append(("BACKGROUND", (0, 0), (-1, 0), PALE))
    table.setStyle(TableStyle(commands))
    return table


def _metrics(items):
    rows = [[[_p(label, "label"), Spacer(1, 5), _p(_money(value), "value")] for label, value in items]]
    table = _table(rows, [WIDTH / len(items)] * len(items), header=False)
    table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), PALE), ("BOX", (0, 0), (-1, -1), 0.5, LINE)]))
    return table


def _build(organization, title, subtitle, story):
    output = BytesIO()
    generated = timezone.localtime()
    generated_label = generated.strftime("%d/%m/%Y %H:%M %Z")
    logo = ImageReader(str(settings.BASE_DIR / "static" / "brand" / "osee-logo.png"))
    source_width, source_height = logo.getSize()
    logo_scale = min(180 / source_width, 44 / source_height)
    logo_width, logo_height = source_width * logo_scale, source_height * logo_scale
    logo_top = 24
    legal_width = WIDTH - 204
    legal_name = _p(organization.name, "legal")
    _, legal_height = legal_name.wrap(legal_width, PAGE_HEIGHT)
    legal_top = 57 if organization.is_demo else 30
    header_bottom = max(logo_top + logo_height, legal_top + legal_height)
    title_baseline = header_bottom + 31
    subtitle_baseline = title_baseline + 17
    document = SimpleDocTemplate(output, pagesize=A4, rightMargin=MARGIN, leftMargin=MARGIN, topMargin=subtitle_baseline + 24, bottomMargin=59, title=_plain(title), author=_plain(organization.name), pageCompression=1)

    def page_decoration(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(BRAND_RED)
        canvas.rect(0, PAGE_HEIGHT - 5, PAGE_WIDTH, 5, stroke=0, fill=1)
        canvas.drawImage(logo, MARGIN, PAGE_HEIGHT - logo_top - logo_height, width=logo_width, height=logo_height, mask="auto")
        if organization.is_demo:
            canvas.setFillColor(colors.HexColor("#fff0cd"))
            canvas.roundRect(PAGE_WIDTH - MARGIN - 103, PAGE_HEIGHT - 44, 103, 22, 4, stroke=0, fill=1)
            canvas.setFillColor(AMBER)
            canvas.setFont("Helvetica-Bold", 9)
            canvas.drawCentredString(PAGE_WIDTH - MARGIN - 51.5, PAGE_HEIGHT - 36, "DEMO - DATA UJI")
        legal_name.drawOn(canvas, PAGE_WIDTH - MARGIN - legal_width, PAGE_HEIGHT - legal_top - legal_height)
        canvas.setFillColor(DEEP_RED)
        canvas.setFont("Helvetica-Bold", 22)
        canvas.drawString(MARGIN, PAGE_HEIGHT - title_baseline, title)
        canvas.setFillColor(MUTED)
        canvas.setFont("Helvetica", 9)
        canvas.drawString(MARGIN, PAGE_HEIGHT - subtitle_baseline, subtitle)
        canvas.setStrokeColor(LINE)
        canvas.line(MARGIN, 46, PAGE_WIDTH - MARGIN, 46)
        canvas.setFont("Helvetica", 7)
        canvas.drawString(MARGIN, 32, f"{title} | onestopenglisheducation.com")
        canvas.drawString(MARGIN, 21, f"Diekspor {generated_label}")
        canvas.drawRightString(PAGE_WIDTH - MARGIN, 32, f"Halaman {doc.page}")
        canvas.restoreState()

    document.build(story, onFirstPage=page_decoration, onLaterPages=page_decoration)
    return output.getvalue()


def invoice_pdf(invoice):
    """Return an invoice PDF as bytes; the caller must authorize this invoice."""
    organization = invoice.organization
    if invoice.party.organization_id != organization.pk or invoice.product.organization_id != organization.pk:
        raise ValueError("Data invoice harus berasal dari organisasi yang sama.")
    statuses = {"draft": "DRAF", "issued": "Diterbitkan", "delivered": "Layanan selesai", "cancelled": "Dibatalkan"}
    status = statuses.get(invoice.status, "Perlu ditinjau")
    total, paid = invoice.total, invoice.paid_amount
    outstanding = total - paid
    story = [_p(f"{invoice.number} | {status}", "heading")]
    if invoice.status in {"draft", "cancelled"}:
        story.append(_p("DRAF: invoice belum diterbitkan." if invoice.status == "draft" else "DIBATALKAN: dokumen ini tidak berlaku sebagai tagihan aktif.", "notice"))
    story.append(_table([
        [[_p("DITAGIHKAN KEPADA", "label"), _p(invoice.party.name)], [_p("TANGGAL INVOICE", "label"), _p(_date(invoice.date))]],
        [[_p("TANGGAL LAYANAN", "label"), _p(_date(invoice.service_date))], [_p("JATUH TEMPO", "label"), _p(_date(invoice.due_date))]],
    ], [WIDTH * 0.59, WIDTH * 0.41], header=False))
    story.extend([Spacer(1, 20), _table([
        [_p("Produk / layanan", "headcell"), _p("Jumlah", "headcell"), _p("Harga satuan", "headcell"), _p("Total", "headcell")],
        [_p(invoice.product.name, "cell"), _p(invoice.quantity, "right"), _p(_money(invoice.unit_price), "right"), _p(_money(total), "right")],
    ], [WIDTH * 0.37, WIDTH * 0.10, WIDTH * 0.265, WIDTH * 0.265]), Spacer(1, 18)])
    story.append(_table([
        [_p("Total invoice", "label"), _p(_money(total), "right")],
        [_p("Pembayaran dialokasikan saat ekspor", "cell"), _p(_money(paid), "right")],
        [_p("Sisa tagihan saat ekspor", "label"), _p(_money(outstanding), "right")],
    ], [WIDTH * 0.66, WIDTH * 0.34], header=False))
    story.extend([_p("Catatan dokumen", "heading"), _p("Invoice komersial. Dokumen ini bukan Faktur Pajak. Angka pembayaran mencerminkan alokasi yang tercatat saat ekspor, bukan konfirmasi saldo rekening bank.", "small")])
    if organization.vat_status == "non_pkp":
        story.extend([Spacer(1, 7), _p("Status PPN pada profil saat ekspor: Non-PKP. Keterangan ini bukan penetapan status historis untuk tanggal transaksi.", "small")])
    return _build(organization, "Invoice penjualan", "Rincian layanan dan pembayaran tercatat", story)


def monthly_report_pdf(organization, summary):
    """Render a caller-scoped monthly_summary; this function performs no queries."""
    if summary.get("organization_id", organization.pk) != organization.pk:
        raise ValueError("Ringkasan harus berasal dari organisasi yang sama.")
    month, year = int(summary["month"]), int(summary["year"])
    if not 1 <= month <= 12:
        raise ValueError("Bulan laporan tidak valid.")
    closed = bool(summary["closed"])
    label = "PERIODE DITUTUP" if closed else "DRAF - PERIODE BELUM DITUTUP"
    story = [_p(label, "heading"), _p(f"Periode {_date(summary['start'])} - {_date(summary['end'])}. Batas tanggal data buku besar: {_date(summary['end'])}.", "small"), Spacer(1, 16)]
    story.append(_metrics([("PENDAPATAN BULAN INI", summary["revenue"]), ("BEBAN BULAN INI", summary["expenses"]), ("LABA / RUGI BUKU", summary["profit"])]))
    story.extend([Spacer(1, 12), _p("Angka berasal dari jurnal yang sudah diposting. Laporan akuntansi komersial ini bukan perhitungan dasar pajak, SPT, laporan audit, atau bukti kelengkapan mutasi bank.", "small"), _p("Neraca saldo", "heading"), _p("Saldo kumulatif sampai batas tanggal data, termasuk periode sebelumnya. Saldo akun Bank merupakan nilai buku dari transaksi tercatat, bukan saldo yang telah dikonfirmasi oleh bank.", "small"), Spacer(1, 9)])
    rows = [[_p("Akun", "headcell"), _p("Debit", "headcell"), _p("Kredit", "headcell")]]
    account_labels = dict(JournalLine.Account.choices)
    balances = summary["balances"]
    debit_total = Decimal("0.00")
    credit_total = Decimal("0.00")
    for code in list(account_labels) + [code for code in balances if code not in account_labels]:
        amount = _amount(balances.get(code, 0))
        debit, credit = max(amount, Decimal(0)), max(-amount, Decimal(0))
        debit_total += debit
        credit_total += credit
        rows.append([_p(account_labels.get(code, code), "cell"), _p(_money(debit), "right"), _p(_money(credit), "right")])
    rows.append([_p("TOTAL", "label"), _p(_money(debit_total), "right"), _p(_money(credit_total), "right")])
    story.append(_table(rows, [WIDTH * 0.48, WIDTH * 0.26, WIDTH * 0.26]))
    balanced = debit_total == credit_total
    story.extend([Spacer(1, 9), _p("Total debit dan kredit seimbang. Keseimbangan ini tidak membuktikan kelengkapan data atau ketepatan pajak." if balanced else f"PERLU DITINJAU: neraca saldo belum seimbang. Selisih {_money(abs(debit_total - credit_total))}.", "small" if balanced else "notice")])
    story.append(_p("Hal yang perlu diselesaikan", "heading"))
    blockers = summary.get("blockers", [])
    if blockers:
        for number, blocker in enumerate(blockers, 1):
            story.append(_p(f"{number}. {blocker}", "notice"))
    else:
        story.append(_p("Tidak ada penghalang tutup buku yang terdeteksi oleh pemeriksaan aplikasi saat ekspor. Verifikasi cakupan rekening, periode mutasi, dan dokumen sumber tetap memerlukan bukti.", "small"))
    for gate in summary.get("tax_profile_gates", []):
        text = f"{gate.get('title', '')}: {gate.get('detail', '')}" if isinstance(gate, dict) else gate
        story.append(_p(text, "notice"))
    return _build(organization, "Laporan keuangan bulanan", f"{MONTHS[month]} {year} | Akuntansi komersial", story)
