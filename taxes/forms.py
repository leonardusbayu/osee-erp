from django import forms

from .models import TaxEvidence
from .rules import ANNUAL_WORKPAPER_YEARS, RULES

DATE = {"type": "date"}


class EvidenceForm(forms.Form):
    file = forms.FileField(
        label="Dokumen PDF", help_text="Maksimum 2 MB. Disimpan privat; bukan publik."
    )


class EvidenceChoiceForm(forms.Form):
    evidence = forms.ModelChoiceField(
        label="Dokumen pendukung yang sudah diunggah",
        queryset=TaxEvidence.objects.none(),
    )

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["evidence"].queryset = (
            TaxEvidence.objects.filter(organization=organization)
            .defer("content")
            .order_by("-pk")
        )
        self.fields["evidence"].label_from_instance = lambda obj: (
            f"#{obj.pk} — {obj.original_filename}"
        )


class ProfessionalReviewForm(EvidenceChoiceForm):
    professional_name = forms.CharField(
        label="Nama profesional yang memeriksa", max_length=200
    )
    qualification_reference = forms.CharField(
        label="Referensi kompetensi / surat penugasan",
        max_length=300,
        help_text="Salin dari dokumen profesional. Aplikasi mencatat referensi, bukan memverifikasi izin profesi secara otomatis.",
    )
    reviewed_on = forms.DateField(
        label="Tanggal laporan pemeriksaan", widget=forms.DateInput(attrs=DATE)
    )
    statement = forms.CharField(
        label="Kesimpulan laporan profesional",
        max_length=4000,
        widget=forms.Textarea(attrs={"rows": 4}),
    )
    report_confirmed = forms.BooleanField(
        label="Saya telah menerima dan mencocokkan laporan profesional dengan keputusan ini. Ini bukan penetapan hukum oleh pemilik sendiri."
    )


class ProfileReviewForm(ProfessionalReviewForm):
    taxpayer_reference = forms.RegexField(
        regex=r"^[0-9]{15,16}$",
        label="NPWP perusahaan (15/16 digit tanpa tanda baca)",
        max_length=16,
    )
    regime = forms.ChoiceField(
        label="Keputusan dalam laporan",
        choices=[
            ("normal", "PPh badan umum"),
            ("final", "PPh final berdasarkan hak transisi yang masih berlaku"),
        ],
    )
    effective_from = forms.DateField(
        label="Mulai berlaku (tanggal pertama masa)", widget=forms.DateInput(attrs=DATE)
    )
    final_regime_start_date = forms.DateField(
        label="Awal tahun fasilitas lama (jika final)",
        required=False,
        widget=forms.DateInput(attrs=DATE),
    )
    final_regime_end_date = forms.DateField(
        label="Akhir fasilitas lama (jika final)",
        required=False,
        widget=forms.DateInput(attrs=DATE),
    )
    transition_evidence = forms.CharField(
        label="Kesimpulan riwayat/transisi dan tidak memilih tarif umum (jika final)",
        required=False,
        max_length=300,
    )
    qualifying_turnover = forms.DecimalField(
        label="Omzet untuk pengujian kelayakan pada laporan (Rp, jika final)",
        required=False,
        min_value=0,
        max_digits=18,
        decimal_places=2,
    )
    legacy_eligibility_confirmed = forms.BooleanField(
        label="Laporan memastikan seluruh syarat fasilitas lama masih dipenuhi (jika final)",
        required=False,
    )
    no_normal_election_confirmed = forms.BooleanField(
        label="Laporan memastikan tidak ada pilihan tarif umum atau pengecualian yang menggugurkan fasilitas (jika final)",
        required=False,
    )


class PPh23ContextForm(EvidenceChoiceForm):
    recipient_identity = forms.ModelChoiceField(
        label="Identitas penerima yang sudah diperiksa",
        required=False,
        queryset=None,
        help_text="Pilih versi yang berlaku pada tanggal terutang. Pemasok adalah penerima saat OSEE memotong; PT OSEE adalah penerima saat pelanggan memotong.",
    )
    recipient_party = forms.ModelChoiceField(
        label="Pemasok penerima penghasilan", required=False, queryset=None
    )
    withholder_party = forms.ModelChoiceField(
        label="Pelanggan pemotong pajak (jika tidak memakai invoice)",
        required=False,
        queryset=None,
    )
    tax_point_date = forms.DateField(
        label="Tanggal terutang sesuai laporan profesional",
        required=False,
        widget=forms.DateInput(attrs=DATE),
        help_text="Isi tanggal yang diperiksa. Aplikasi tidak memilih tanggal tagihan, bank, atau layanan sebagai waktu terutang secara otomatis.",
    )
    tax_point_basis = forms.ChoiceField(
        label="Dasar penentuan tanggal terutang pada laporan",
        required=False,
        choices=[
            ("", "Pilih sesuai laporan"),
            ("paid", "Penghasilan dibayarkan"),
            ("available", "Penghasilan disediakan untuk dibayarkan"),
            ("due", "Penghasilan jatuh tempo"),
        ],
    )
    withholder_reference = forms.CharField(
        label="NPWP 16 digit pihak pemotong pada dokumen", required=False, max_length=16
    )
    withholder_name = forms.CharField(
        label="Nama legal pihak pemotong pada dokumen", required=False, max_length=200
    )
    object_reason = forms.CharField(
        label="Kesimpulan objek PPh 23 dalam laporan",
        required=False,
        max_length=2000,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    individual_21_26_excluded = forms.BooleanField(
        label="Laporan memastikan jasa orang pribadi ini bukan rute PPh 21/26 (jika penerima orang pribadi).",
        required=False,
    )
    final_exempt_excluded = forms.BooleanField(
        label="Laporan memastikan objek ini tidak memakai PPh final atau pengecualian/fasilitas lain.",
        required=False,
    )

    def __init__(self, *args, organization, bill=None, **kwargs):
        from finance.models import Party

        from .models import RecipientTaxIdentity

        super().__init__(*args, organization=organization, **kwargs)
        self.bill = bill
        identities = (
            RecipientTaxIdentity.objects.filter(
                organization=organization, approval__isnull=False
            )
            .exclude(approval__factor__isnull=True)
            .select_related("recipient_party")
        )
        if bill:
            identities = identities.filter(recipient_party=bill.supplier)
        self.fields["recipient_identity"].queryset = identities.order_by(
            "legal_name", "-effective_from"
        )
        self.fields["recipient_identity"].label_from_instance = lambda row: (
            f"{row.legal_name} · {row.masked_number} · berlaku {row.effective_from:%d-%m-%Y} · versi {row.revision}"
        )
        self.fields["recipient_party"].queryset = Party.objects.filter(
            organization=organization, kind="supplier"
        ).order_by("name")
        self.fields["withholder_party"].queryset = (
            Party.objects.filter(organization=organization)
            .exclude(kind="supplier")
            .order_by("name")
        )
        for name in ("recipient_party", "withholder_party"):
            self.fields[name].label_from_instance = lambda row: row.name
        if bill:
            self.fields["recipient_party"].disabled = True
            self.fields["recipient_party"].initial = bill.supplier
            self.fields.pop("withholder_party")
            self.fields.pop("object_reason")
        if "rule_code" in self.fields:
            labels = {
                "pph23_service_2": "PPh 23 — kelompok objek dengan tarif dasar 2%",
                "pph23_other_15": "PPh 23 — kelompok objek dengan tarif dasar 15%",
            }
            self.fields["rule_code"].choices = [
                (value, labels.get(value, label))
                for value, label in self.fields["rule_code"].choices
            ]

    def clean(self):
        data = super().clean()
        if not data.get("rule_code", "").startswith("pph23_"):
            return data
        if self.bill:
            data["object_reason"] = data.get("reason", "")
        for key in (
            "recipient_identity",
            "tax_point_date",
            "tax_point_basis",
            "withholder_reference",
            "withholder_name",
            "object_reason",
        ):
            if not data.get(key):
                self.add_error(
                    "reason" if key == "object_reason" and self.bill else key,
                    "Lengkapi sesuai laporan profesional untuk PPh 23.",
                )
        if not data.get("final_exempt_excluded"):
            self.add_error(
                "final_exempt_excluded",
                "Konfirmasikan hasil pemeriksaan rute pajak sebelum memakai PPh 23.",
            )
        if (
            not self.bill
            and data.get("direction") == "payable"
            and not data.get("recipient_party")
        ):
            self.add_error(
                "recipient_party", "Pilih pemasok yang menerima penghasilan."
            )
        if (
            not self.bill
            and data.get("direction") == "receivable"
            and not data.get("source_invoice")
        ):
            self.add_error(
                "source_invoice", "Pilih invoice pelanggan yang memotong OSEE."
            )
        return data


PPH23_FORM_FIELDS = (
    "recipient_identity",
    "recipient_party",
    "withholder_party",
    "tax_point_date",
    "tax_point_basis",
    "withholder_reference",
    "withholder_name",
    "object_reason",
    "individual_21_26_excluded",
    "final_exempt_excluded",
)


class ObligationForm(PPh23ContextForm):
    source_invoice = forms.ModelChoiceField(
        label="Invoice yang dipotong pelanggan",
        required=False,
        queryset=None,
        help_text="Pilih untuk PPh 23 yang dipotong pelanggan. Nomor ini menghubungkan bukti potong dan pelunasan invoice.",
    )
    period = forms.RegexField(
        label="Masa pajak",
        regex=r"^\d{4}-\d{2}$",
        widget=forms.TextInput(attrs={"type": "month"}),
    )
    rule_code = forms.ChoiceField(
        label="Klasifikasi sesuai dokumen pemeriksa",
        choices=[
            (key, value[0]) for key, value in RULES.items() if key != "no_withholding"
        ],
    )
    base = forms.DecimalField(
        label="Dasar pajak sebelum pembulatan (Rp)",
        max_digits=18,
        decimal_places=2,
        min_value=0,
    )
    direction = forms.ChoiceField(
        label="Arah pajak",
        choices=[
            ("payable", "OSEE memotong pemasok"),
            ("receivable", "Pelanggan memotong OSEE"),
            ("own_tax", "Pajak omzet OSEE sendiri"),
        ],
    )
    source_reference = forms.CharField(
        label="Referensi transaksi/rekonsiliasi sumber", max_length=300
    )
    rule_reference = forms.CharField(
        label="Dasar hukum dan perlakuan dalam dokumen", max_length=300
    )
    object_code = forms.CharField(
        label="Kode objek menurut dokumen pemeriksa",
        max_length=40,
        help_text="Kode belum divalidasi sebagai skema impor DJP. Draf tetap perlu pencocokan ke referensi resmi.",
    )
    note = forms.CharField(
        label="Catatan dasar, penerima, waktu terutang, dan pengecualian",
        max_length=4000,
        widget=forms.Textarea(attrs={"rows": 4}),
    )

    def __init__(self, *args, organization, **kwargs):
        from finance.models import Invoice

        super().__init__(*args, organization=organization, **kwargs)
        self.fields.pop("withholder_party")
        self.fields["source_invoice"].queryset = Invoice.objects.filter(
            organization=organization, status__in=["issued", "delivered"]
        ).select_related("party")


class WithdrawalForm(forms.Form):
    expected_version = forms.CharField(max_length=80, widget=forms.HiddenInput())
    reason = forms.CharField(
        label="Mengapa draf ini perlu ditarik?",
        min_length=10,
        max_length=2000,
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text="Contoh: dokumen yang sama dicatat dua kali. Jelaskan sumber dan kesalahannya agar riwayat dapat ditelusuri.",
    )
    confirmed = forms.BooleanField(
        label="Saya telah memeriksa draf ini. Penarikan menyimpan riwayat, tidak menghapus dokumen atau mengubah jurnal, dan memerlukan paket tahunan baru bila paket lama memasukkan draf ini."
    )


class BillReviewForm(ProfessionalReviewForm, PPh23ContextForm):
    rule_code = forms.ChoiceField(
        label="Keputusan pemotongan pada laporan",
        choices=[
            (key, value[0])
            for key, value in RULES.items()
            if key != "final_turnover_005"
        ],
    )
    base = forms.DecimalField(
        label="Dasar pemotongan yang diperiksa (Rp)",
        min_value=0,
        max_digits=18,
        decimal_places=2,
    )
    period = forms.RegexField(
        label="Masa pajak menurut waktu terutang",
        regex=r"^\d{4}-\d{2}$",
        widget=forms.TextInput(attrs={"type": "month"}),
    )
    object_code = forms.CharField(
        label="Kode objek (wajib untuk pemotongan positif)",
        max_length=40,
        required=False,
    )
    reason = forms.CharField(
        label="Dasar klasifikasi / alasan tidak dipotong",
        max_length=2000,
        widget=forms.Textarea(attrs={"rows": 4}),
    )


class VerificationForm(EvidenceChoiceForm):
    kind = forms.ChoiceField(
        label="Jenis bukti",
        choices=[
            ("payment", "BPN/NTPN atau alokasi pembayaran resmi"),
            ("filing", "Bukti penerimaan SPT"),
            ("credit", "Bukti potong pelanggan"),
        ],
    )
    reference = forms.CharField(label="Nomor referensi resmi", max_length=200)
    taxpayer_reference = forms.CharField(
        label="NPWP perusahaan pada dokumen", max_length=100
    )
    period = forms.RegexField(
        label="Masa pada dokumen",
        regex=r"^\d{4}-\d{2}$",
        widget=forms.TextInput(attrs={"type": "month"}),
    )
    amount = forms.DecimalField(
        label="Nominal pajak yang cocok (Rp)",
        min_value=0,
        max_digits=18,
        decimal_places=2,
    )
    note = forms.CharField(
        label="Catatan pencocokan dokumen, nomor, masa, dan nominal",
        max_length=4000,
        widget=forms.Textarea(attrs={"rows": 4}),
    )
    matches_confirmed = forms.BooleanField(
        label="Saya mencocokkan dokumen asli dan versi kewajiban ini, termasuk identitas, masa, nominal, dan penerimaan resmi."
    )
    recipient_reference = forms.CharField(
        label="Nomor identitas OSEE sebagai penerima pada bukti potong",
        max_length=100,
        required=False,
    )
    recipient_name = forms.CharField(
        label="Nama OSEE sebagai penerima pada bukti potong",
        max_length=200,
        required=False,
    )
    withholder_reference = forms.CharField(
        label="NPWP pihak pelanggan yang memotong pada bukti",
        max_length=100,
        required=False,
    )
    withholder_name = forms.CharField(
        label="Nama pihak pelanggan yang memotong pada bukti",
        max_length=200,
        required=False,
    )
    object_code = forms.CharField(
        label="Kode objek pada bukti potong", max_length=40, required=False
    )
    base = forms.DecimalField(
        label="Dasar pajak pada bukti potong (Rp)",
        required=False,
        min_value=0,
        max_digits=18,
        decimal_places=2,
    )
    tax_point_date = forms.DateField(
        label="Tanggal terutang pada bukti potong",
        required=False,
        widget=forms.DateInput(attrs=DATE),
    )
    certificate_status = forms.ChoiceField(
        label="Status bukti potong",
        required=False,
        choices=[
            ("", "Pilih status dokumen"),
            ("normal", "Normal — bukan pembetulan atau pembatalan"),
        ],
        help_text="Pembetulan/pembatalan memerlukan alur koreksi yang belum didukung.",
    )

    def __init__(self, *args, organization, obligation=None, **kwargs):
        super().__init__(*args, organization=organization, **kwargs)
        self.obligation = obligation
        if (
            obligation
            and obligation.tax_type == "pph23"
            and obligation.direction == "receivable"
        ):
            self.fields["kind"].choices = [("credit", "Bukti potong pelanggan")]
            self.fields["kind"].initial = "credit"

    def clean(self):
        data = super().clean()
        if (
            self.obligation
            and self.obligation.tax_type == "pph23"
            and data.get("kind") == "credit"
        ):
            for key in PPH23_CERTIFICATE_FIELDS:
                if data.get(key) in (None, ""):
                    self.add_error(key, "Cocokkan bagian ini dari bukti potong asli.")
        return data


PPH23_CERTIFICATE_FIELDS = (
    "recipient_reference",
    "recipient_name",
    "withholder_reference",
    "withholder_name",
    "object_code",
    "base",
    "tax_point_date",
    "certificate_status",
)


class AnnualWorkpaperForm(EvidenceChoiceForm):
    year = forms.IntegerField(
        label="Tahun awal buku",
        min_value=min(ANNUAL_WORKPAPER_YEARS),
        max_value=max(ANNUAL_WORKPAPER_YEARS),
        initial=max(ANNUAL_WORKPAPER_YEARS),
        help_text="Kalkulator saat ini mendukung tahun 2025–2026. Tahun berikutnya akan tersedia setelah aturan dan perhitungannya ditinjau.",
    )
    return_version = forms.TypedChoiceField(
        label="Jenis SPT",
        choices=[(0, "SPT normal (pertama)")],
        coerce=int,
        initial=0,
        help_text="Pembetulan memerlukan riwayat pembayaran dan pengembalian dari SPT sebelumnya. Perhitungan pembetulan belum tersedia di aplikasi.",
    )
    turnover = forms.DecimalField(
        label="Peredaran bruto fiskal dari rekonsiliasi (Rp)",
        min_value=0,
        max_digits=18,
        decimal_places=2,
    )
    positive_adjustments = forms.DecimalField(
        label="Total koreksi fiskal positif (Rp)",
        min_value=0,
        max_digits=18,
        decimal_places=2,
    )
    negative_adjustments = forms.DecimalField(
        label="Total koreksi fiskal negatif / pengecualian (Rp)",
        min_value=0,
        max_digits=18,
        decimal_places=2,
    )
    loss_compensation = forms.DecimalField(
        label="Kompensasi rugi yang masih sah (Rp)",
        min_value=0,
        max_digits=18,
        decimal_places=2,
    )
    instalments = forms.DecimalField(
        label="Angsuran/ pembayaran PPh badan yang didukung bukti (Rp)",
        min_value=0,
        max_digits=18,
        decimal_places=2,
    )
    regime_code = forms.ChoiceField(
        label="Perlakuan dalam rekonsiliasi profesional",
        choices=[
            ("corporate_22", "Normal 22%"),
            (
                "corporate_31e_small_11",
                "Normal dengan fasilitas 31E penuh — perlu kelayakan terverifikasi",
            ),
            ("final_only", "Hanya final; penghasilan normal nihil terverifikasi"),
        ],
    )
    reconciliation_note = forms.CharField(
        label="Rincian rekonsiliasi dan lampiran pendukung",
        max_length=4000,
        widget=forms.Textarea(attrs={"rows": 5}),
        help_text="Jelaskan setiap koreksi, pemisahan penghasilan final/bukan objek beserta biayanya, rugi, angsuran, aset/penyusutan dan lampiran. Kredit pelanggan dihitung hanya dari bukti yang diverifikasi.",
    )


class AnnualFilingForm(EvidenceChoiceForm):
    reference = forms.CharField(label="Nomor BPE resmi", max_length=200)
    taxpayer_reference = forms.CharField(
        label="NPWP perusahaan pada BPE", max_length=100
    )
    year = forms.IntegerField(
        label="Tahun SPT yang diterima", min_value=2025, max_value=2100
    )
    return_version = forms.IntegerField(
        label="Versi SPT yang diterima (0 = normal)", min_value=0, max_value=999
    )
    reported_tax = forms.DecimalField(
        label="PPh normal pada SPT yang diterima (Rp)",
        min_value=0,
        max_digits=18,
        decimal_places=2,
    )
    reported_balance_due = forms.DecimalField(
        label="Kurang / (lebih) bayar pada SPT diterima (Rp; lebih bayar negatif)",
        max_digits=18,
        decimal_places=2,
    )
    matches_confirmed = forms.BooleanField(
        label="BPE ini cocok dengan perusahaan, tahun, status pembetulan, dan seluruh versi SPT/kertas kerja yang disetujui."
    )
