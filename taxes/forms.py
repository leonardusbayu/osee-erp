from django import forms
from django.utils import timezone
from .models import TaxEvidence
from .rules import RULES

DATE = {"type": "date"}


class EvidenceForm(forms.Form):
    file = forms.FileField(label="Dokumen PDF", help_text="Maksimum 2 MB. Disimpan privat; bukan publik.")


class EvidenceChoiceForm(forms.Form):
    evidence = forms.ModelChoiceField(label="Dokumen pendukung yang sudah diunggah", queryset=TaxEvidence.objects.none())

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["evidence"].queryset = TaxEvidence.objects.filter(organization=organization).defer("content").order_by("-pk")
        self.fields["evidence"].label_from_instance = lambda obj: f"#{obj.pk} — {obj.original_filename}"


class ProfessionalReviewForm(EvidenceChoiceForm):
    professional_name = forms.CharField(label="Nama profesional yang memeriksa", max_length=200)
    qualification_reference = forms.CharField(label="Referensi kompetensi / surat penugasan", max_length=300,
        help_text="Salin dari dokumen profesional. Aplikasi mencatat referensi, bukan memverifikasi izin profesi secara otomatis.")
    reviewed_on = forms.DateField(label="Tanggal laporan pemeriksaan", widget=forms.DateInput(attrs=DATE))
    statement = forms.CharField(label="Kesimpulan laporan profesional", max_length=4000, widget=forms.Textarea(attrs={"rows": 4}))
    report_confirmed = forms.BooleanField(label="Saya telah menerima dan mencocokkan laporan profesional dengan keputusan ini. Ini bukan penetapan hukum oleh pemilik sendiri.")


class ProfileReviewForm(ProfessionalReviewForm):
    taxpayer_reference = forms.RegexField(regex=r"^[0-9]{15,16}$", label="NPWP perusahaan (15/16 digit tanpa tanda baca)", max_length=16)
    regime = forms.ChoiceField(label="Keputusan dalam laporan", choices=[("normal", "PPh badan umum"), ("final", "PPh final berdasarkan hak transisi yang masih berlaku")])
    effective_from = forms.DateField(label="Mulai berlaku (tanggal pertama masa)", widget=forms.DateInput(attrs=DATE))
    final_regime_start_date = forms.DateField(label="Awal tahun fasilitas lama (jika final)", required=False, widget=forms.DateInput(attrs=DATE))
    final_regime_end_date = forms.DateField(label="Akhir fasilitas lama (jika final)", required=False, widget=forms.DateInput(attrs=DATE))
    transition_evidence = forms.CharField(label="Kesimpulan riwayat/transisi dan tidak memilih tarif umum (jika final)", required=False, max_length=300)
    qualifying_turnover = forms.DecimalField(label="Omzet untuk pengujian kelayakan pada laporan (Rp, jika final)", required=False, min_value=0, max_digits=18, decimal_places=2)
    legacy_eligibility_confirmed = forms.BooleanField(label="Laporan memastikan seluruh syarat fasilitas lama masih dipenuhi (jika final)", required=False)
    no_normal_election_confirmed = forms.BooleanField(label="Laporan memastikan tidak ada pilihan tarif umum atau pengecualian yang menggugurkan fasilitas (jika final)", required=False)


class ObligationForm(EvidenceChoiceForm):
    period = forms.RegexField(label="Masa pajak", regex=r"^\d{4}-\d{2}$", widget=forms.TextInput(attrs={"type": "month"}))
    rule_code = forms.ChoiceField(label="Klasifikasi sesuai dokumen pemeriksa", choices=[(key, value[0]) for key, value in RULES.items() if key != "no_withholding"])
    base = forms.DecimalField(label="Dasar pajak sebelum pembulatan (Rp)", max_digits=18, decimal_places=2, min_value=0)
    direction = forms.ChoiceField(label="Arah pajak", choices=[("payable", "OSEE memotong pemasok"), ("receivable", "Pelanggan memotong OSEE"), ("own_tax", "Pajak omzet OSEE sendiri")])
    source_reference = forms.CharField(label="Referensi transaksi/rekonsiliasi sumber", max_length=300)
    rule_reference = forms.CharField(label="Dasar hukum dan perlakuan dalam dokumen", max_length=300)
    object_code = forms.CharField(label="Kode objek menurut dokumen pemeriksa", max_length=40, help_text="Kode belum divalidasi sebagai skema impor DJP. Draf tetap perlu pencocokan ke referensi resmi.")
    note = forms.CharField(label="Catatan dasar, penerima, waktu terutang, dan pengecualian", max_length=4000, widget=forms.Textarea(attrs={"rows": 4}))


class BillReviewForm(ProfessionalReviewForm):
    rule_code = forms.ChoiceField(label="Keputusan pemotongan pada laporan", choices=[(key, value[0]) for key, value in RULES.items() if key != "final_turnover_005"])
    base = forms.DecimalField(label="Dasar pemotongan yang diperiksa (Rp)", min_value=0, max_digits=18, decimal_places=2)
    period = forms.RegexField(label="Masa pajak menurut waktu terutang", regex=r"^\d{4}-\d{2}$", widget=forms.TextInput(attrs={"type": "month"}))
    object_code = forms.CharField(label="Kode objek (wajib untuk pemotongan positif)", max_length=40, required=False)
    reason = forms.CharField(label="Dasar klasifikasi / alasan tidak dipotong", max_length=2000, widget=forms.Textarea(attrs={"rows": 4}))


class VerificationForm(EvidenceChoiceForm):
    kind = forms.ChoiceField(label="Jenis bukti", choices=[("payment", "BPN/NTPN atau alokasi pembayaran resmi"), ("filing", "Bukti penerimaan SPT"), ("credit", "Bukti potong pelanggan")])
    reference = forms.CharField(label="Nomor referensi resmi", max_length=200)
    taxpayer_reference = forms.CharField(label="NPWP perusahaan pada dokumen", max_length=100)
    period = forms.RegexField(label="Masa pada dokumen", regex=r"^\d{4}-\d{2}$", widget=forms.TextInput(attrs={"type": "month"}))
    amount = forms.DecimalField(label="Nominal pajak yang cocok (Rp)", min_value=0, max_digits=18, decimal_places=2)
    note = forms.CharField(label="Catatan pencocokan dokumen, nomor, masa, dan nominal", max_length=4000, widget=forms.Textarea(attrs={"rows": 4}))
    matches_confirmed = forms.BooleanField(label="Saya mencocokkan dokumen asli dan versi kewajiban ini, termasuk identitas, masa, nominal, dan penerimaan resmi.")


class AnnualWorkpaperForm(EvidenceChoiceForm):
    year = forms.IntegerField(label="Tahun awal buku", min_value=2025, max_value=2100, initial=2026)
    return_version = forms.IntegerField(label="Versi SPT (0 = normal; 1 dst. = pembetulan)", min_value=0, max_value=999, initial=0)
    turnover = forms.DecimalField(label="Peredaran bruto fiskal dari rekonsiliasi (Rp)", min_value=0, max_digits=18, decimal_places=2)
    positive_adjustments = forms.DecimalField(label="Total koreksi fiskal positif (Rp)", min_value=0, max_digits=18, decimal_places=2)
    negative_adjustments = forms.DecimalField(label="Total koreksi fiskal negatif / pengecualian (Rp)", min_value=0, max_digits=18, decimal_places=2)
    loss_compensation = forms.DecimalField(label="Kompensasi rugi yang masih sah (Rp)", min_value=0, max_digits=18, decimal_places=2)
    instalments = forms.DecimalField(label="Angsuran/ pembayaran PPh badan yang didukung bukti (Rp)", min_value=0, max_digits=18, decimal_places=2)
    regime_code = forms.ChoiceField(label="Perlakuan dalam rekonsiliasi profesional", choices=[("corporate_22", "Normal 22%"), ("corporate_31e_small_11", "Normal dengan fasilitas 31E penuh — perlu kelayakan terverifikasi"), ("final_only", "Hanya final; penghasilan normal nihil terverifikasi")])
    reconciliation_note = forms.CharField(label="Rincian rekonsiliasi dan lampiran pendukung", max_length=4000, widget=forms.Textarea(attrs={"rows": 5}), help_text="Jelaskan setiap koreksi, pemisahan penghasilan final/bukan objek beserta biayanya, rugi, angsuran, aset/penyusutan dan lampiran. Kredit pelanggan dihitung hanya dari bukti yang diverifikasi.")


class AnnualFilingForm(EvidenceChoiceForm):
    reference = forms.CharField(label="Nomor BPE resmi", max_length=200)
    taxpayer_reference = forms.CharField(label="NPWP perusahaan pada BPE", max_length=100)
    year = forms.IntegerField(label="Tahun SPT yang diterima", min_value=2025, max_value=2100)
    return_version = forms.IntegerField(label="Versi SPT yang diterima (0 = normal)", min_value=0, max_value=999)
    reported_tax = forms.DecimalField(label="PPh normal pada SPT yang diterima (Rp)", min_value=0, max_digits=18, decimal_places=2)
    reported_balance_due = forms.DecimalField(label="Kurang / (lebih) bayar pada SPT diterima (Rp; lebih bayar negatif)", max_digits=18, decimal_places=2)
    matches_confirmed = forms.BooleanField(label="BPE ini cocok dengan perusahaan, tahun, status pembetulan, dan seluruh versi SPT/kertas kerja yang disetujui.")
