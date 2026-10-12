"""Private management PDF; figures are supplied by the same deterministic snapshot as UI."""

from reportlab.platypus import Spacer

from webapp.documents import WIDTH, _build, _money, _p, _table


def _value(value, currency=False):
    if value is None:
        return "Belum tersedia"
    return _money(value) if currency else str(value)


def management_report_pdf(organization, snapshot, objectives, decisions):
    source = snapshot["source"]
    story = [
        _p(
            "Laporan manajemen internal. Bukan laporan pajak resmi atau laporan keuangan diaudit.",
            "notice",
        ),
        _p("Data diambil: " + snapshot["as_of"], "small"),
        _p(source["coverage_label"]),
        _p("Angka utama dan basisnya", "heading"),
    ]
    rows = [
        [
            _p("Ukuran", "headcell"),
            _p("Nilai", "headcell"),
            _p("Basis / keterbatasan", "headcell"),
        ],
        [
            _p("Nilai rekap ITP", "cell"),
            _p(_value(source["reported_amount"], True), "cell"),
            _p("Rekap sumber; bukan pendapatan terverifikasi", "cell"),
        ],
        [
            _p("Peserta tercetak / detail", "cell"),
            _p(
                f"{_value(source['printed_count'])} / {_value(source['detail_count'])}",
                "cell",
            ),
            _p("Kemunculan peserta, bukan pembeli unik", "cell"),
        ],
        [
            _p("Pendapatan telah diposting", "cell"),
            _p(_value(snapshot["finance"]["revenue"], True), "cell"),
            _p(snapshot["finance"]["coverage_label"], "cell"),
        ],
        [
            _p("Saldo bank akhir bulan", "cell"),
            _p(_value(snapshot["finance"]["bank_balance"], True), "cell"),
            _p(snapshot["finance"]["bank_note"], "cell"),
        ],
    ]
    story.append(_table(rows, [WIDTH * 0.29, WIDTH * 0.29, WIDTH * 0.42]))
    budget = snapshot.get("budget")
    if budget:
        story.extend(
            [
                _p("Budget dan pembayaran terkait", "heading"),
                _p("Posisi per " + budget["as_of"], "small"),
                _p(budget["coverage"]),
                _p("Referensi posisi budget: " + budget["fingerprint"], "small"),
            ]
        )
        rows = [[_p(x, "headcell") for x in ("Ukuran", "Nilai", "Arti")]]
        for key, label, meaning in (
            ("amount", "Otorisasi disetujui", "Batas budget; bukan saldo bank"),
            (
                "incurred",
                "Biaya terjadi",
                "Sumber Finance terhubung dan catatan manual yang ditinjau",
            ),
            (
                "open_commitments",
                "Komitmen tersisa",
                "Janji belanja yang belum menjadi biaya",
            ),
            (
                "reservations",
                "Dana dicadangkan",
                "Nilai yang masih disiapkan untuk belanja",
            ),
            ("consumed", "Konsumsi budget", "Biaya + komitmen + dana dicadangkan"),
            (
                "available",
                "Sisa otorisasi",
                "Dapat negatif ketika terjadi kelebihan budget",
            ),
            (
                "verified_cash",
                "Uang bank terkait",
                "Pembayaran dengan sumber bank Finance",
            ),
            (
                "unmatched_reported_payments",
                "Pembayaran belum cocok",
                "Laporan manual; belum menjadi kas terverifikasi",
            ),
            (
                "unpaid_bills",
                "Tagihan belum lunas",
                "Sisa kewajiban kepada pemasok terkait",
            ),
            (
                "withholding_payable",
                "Potongan belum disetor",
                "Kewajiban potongan terpisah dari tagihan pemasok",
            ),
        ):
            rows.append(
                [
                    _p(label, "cell"),
                    _p(_value(budget[key], True), "cell"),
                    _p(meaning, "cell"),
                ]
            )
        story.append(_table(rows, [WIDTH * 0.30, WIDTH * 0.25, WIDTH * 0.45]))
        for task in budget.get("tasks", []):
            story.append(_p(task["message"], "notice"))
    story.append(_p("Rekap bulanan", "heading"))
    rows = [
        [
            _p(x, "headcell")
            for x in ("Periode", "Nilai sumber", "Tercetak / detail", "Cakupan")
        ]
    ]
    for month in snapshot["monthly_series"]:
        coverage = (
            "Belum tersedia"
            if not month["available"]
            else "Parsial"
            if month["is_partial"]
            else "Halaman tersedia"
        )
        rows.append(
            [
                _p(month["label"], "cell"),
                _p(_value(month["reported_amount"], True), "cell"),
                _p(
                    f"{_value(month['printed_count'])} / {_value(month['detail_count'])}",
                    "cell",
                ),
                _p(coverage, "cell"),
            ]
        )
    story.append(_table(rows, [WIDTH * 0.20, WIDTH * 0.31, WIDTH * 0.25, WIDTH * 0.24]))
    story.append(_p("Kesiapan dan catatan data", "heading"))
    for item in snapshot["readiness"]:
        story.extend([_p(item["label"], "label"), _p(item["detail"]), Spacer(1, 5)])
        for issue in item.get("issues", []):
            story.append(_p(issue, "small"))
    story.append(
        _p(
            f"{len(snapshot['exceptions'])} catatan pemeriksaan sumber tercatat. Detail bukti tersedia melalui Kesiapan Data di aplikasi.",
            "notice",
        )
    )
    story.append(_p("Sasaran perusahaan terbaru (lintas periode)", "heading"))
    if objectives:
        for item in objectives:
            story.extend(
                [
                    _p(item.title, "label"),
                    _p(
                        f"{item.metric_key}; target {_value(item.target)}; penanggung jawab {item.owner_name}; {item.get_status_display()}."
                    ),
                    _p(
                        f"Periode {item.period_start} sampai {item.period_end}", "small"
                    ),
                    Spacer(1, 7),
                ]
            )
    else:
        story.append(_p("Belum ada sasaran tercatat."))
    story.append(_p("Keputusan dan tindak lanjut terbaru (lintas periode)", "heading"))
    if decisions:
        for item in decisions:
            story.extend(
                [
                    _p(item.title, "label"),
                    _p(
                        f"{item.get_status_display()}; versi {item.version}; penanggung jawab {item.owner_name}; evaluasi {item.review_date or 'belum ditentukan'}."
                    ),
                    Spacer(1, 7),
                ]
            )
    else:
        story.append(_p("Belum ada keputusan tercatat."))
    story.append(
        _p(
            "Daftar sasaran dan keputusan dibatasi 30 catatan terbaru. Unduhan mempertahankan angka saat dibuat; pengunduhan ulang dapat mencerminkan perubahan data.",
            "small",
        )
    )
    return _build(
        organization,
        f"Laporan Direktur {snapshot['year']}",
        "Pertumbuhan, kesiapan data, dan tindak lanjut",
        story,
    )
