"""Private management PDF; figures are supplied by the same deterministic snapshot as UI."""
from reportlab.platypus import Spacer
from webapp.documents import _build, _p, _table, _money, WIDTH


def _value(value, currency=False):
    if value is None:
        return "Belum tersedia"
    return _money(value) if currency else str(value)


def management_report_pdf(organization, snapshot, objectives, decisions):
    source = snapshot["source"]
    story = [_p("Laporan manajemen internal. Bukan laporan pajak resmi atau laporan keuangan diaudit.", "notice"),
             _p("Data diambil: " + snapshot["as_of"], "small"),
             _p(source["coverage_label"]),
             _p("Angka utama dan basisnya", "heading")]
    rows = [[_p("Ukuran", "headcell"), _p("Nilai", "headcell"), _p("Basis / keterbatasan", "headcell")],
            [_p("Nilai rekap ITP", "cell"), _p(_value(source["reported_amount"], True), "cell"), _p("Rekap sumber; bukan pendapatan terverifikasi", "cell")],
            [_p("Peserta tercetak / detail", "cell"), _p(f'{_value(source["printed_count"])} / {_value(source["detail_count"])}', "cell"), _p("Kemunculan peserta, bukan pembeli unik", "cell")],
            [_p("Pendapatan telah diposting", "cell"), _p(_value(snapshot["finance"]["revenue"], True), "cell"), _p(snapshot["finance"]["coverage_label"], "cell")],
            [_p("Saldo kas terverifikasi", "cell"), _p("Belum tersedia", "cell"), _p("Saldo awal dan rekonsiliasi lengkap belum disahkan", "cell")]]
    story.append(_table(rows, [WIDTH*.29, WIDTH*.29, WIDTH*.42]))
    story.append(_p("Rekap bulanan", "heading"))
    rows = [[_p(x, "headcell") for x in ("Periode", "Nilai sumber", "Tercetak / detail", "Cakupan")]]
    for month in snapshot["monthly_series"]:
        coverage = "Belum tersedia" if not month["available"] else "Parsial" if month["is_partial"] else "Halaman tersedia"
        rows.append([_p(month["label"], "cell"), _p(_value(month["reported_amount"], True), "cell"),
                     _p(f'{_value(month["printed_count"])} / {_value(month["detail_count"])}', "cell"), _p(coverage, "cell")])
    story.append(_table(rows, [WIDTH*.20, WIDTH*.31, WIDTH*.25, WIDTH*.24]))
    story.append(_p("Kesiapan dan catatan data", "heading"))
    for item in snapshot["readiness"]:
        story.extend([_p(item["label"], "label"), _p(item["detail"]), Spacer(1, 5)])
    story.append(_p(f'{len(snapshot["exceptions"])} catatan pemeriksaan sumber tercatat. Detail bukti tersedia melalui Kesiapan Data di aplikasi.', "notice"))
    story.append(_p("Sasaran perusahaan terbaru (lintas periode)", "heading"))
    if objectives:
        for item in objectives:
            story.extend([_p(item.title, "label"), _p(f'{item.metric_key}; target {_value(item.target)}; penanggung jawab {item.owner_name}; {item.get_status_display()}.'),
                          _p(f'Periode {item.period_start} sampai {item.period_end}', "small"), Spacer(1, 7)])
    else: story.append(_p("Belum ada sasaran tercatat."))
    story.append(_p("Keputusan dan tindak lanjut terbaru (lintas periode)", "heading"))
    if decisions:
        for item in decisions:
            story.extend([_p(item.title, "label"), _p(f'{item.get_status_display()}; versi {item.version}; penanggung jawab {item.owner_name}; evaluasi {item.review_date or "belum ditentukan"}.'), Spacer(1, 7)])
    else: story.append(_p("Belum ada keputusan tercatat."))
    story.append(_p("Daftar sasaran dan keputusan dibatasi 30 catatan terbaru. Unduhan mempertahankan angka saat dibuat; pengunduhan ulang dapat mencerminkan perubahan data.", "small"))
    return _build(organization, f'Laporan Direktur {snapshot["year"]}', "Pertumbuhan, kesiapan data, dan tindak lanjut", story)
