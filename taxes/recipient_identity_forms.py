"""Document-led recipient preparation and Owner review forms."""

from django import forms

from .forms import ProfessionalReviewForm
from .models import TaxEvidence


def nullable_boolean(value):
    return True if value == "yes" else False if value == "no" else None


class RecipientPreparationForm(forms.Form):
    request_key = forms.CharField(max_length=80, widget=forms.HiddenInput())
    target = forms.ChoiceField(
        label="Siapa yang menerima penghasilan?",
        choices=[
            ("", "Pilih penerima"),
            ("organization", "PT OSEE — pelanggan memotong pajak kami"),
            ("supplier", "Pemasok — OSEE memotong pajaknya"),
        ],
    )
    recipient_party = forms.ModelChoiceField(
        label="Pemasok penerima penghasilan", required=False, queryset=None
    )
    previous = forms.ModelChoiceField(
        label="Keputusan sebelumnya yang dilengkapi atau digantikan (jika ada)",
        required=False,
        queryset=None,
        help_text="Keputusan yang sebelumnya belum pasti dapat dilengkapi pada tanggal fakta yang sama, jika belum dipakai untuk pajak yang disetujui. Perubahan status yang sudah pasti memakai tanggal perubahan sebenarnya. Riwayat tetap tersimpan.",
    )
    legal_name = forms.CharField(label="Nama legal pada dokumen", max_length=200)
    name_variation_reason = forms.CharField(
        label="Alasan perbedaan nama, jika berbeda dari catatan perusahaan/pemasok",
        max_length=2000,
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
    )
    recipient_type = forms.ChoiceField(
        label="Jenis penerima menurut dokumen",
        choices=[
            ("", "Pilih sesuai dokumen"),
            ("body", "Badan usaha / badan hukum"),
            ("person", "Orang pribadi"),
        ],
    )
    tax_residency = forms.ChoiceField(
        label="Status subjek pajak menurut pemeriksa",
        initial="unknown",
        choices=[
            ("unknown", "Belum diperiksa"),
            ("spdn", "Subjek pajak dalam negeri"),
            ("spln", "Subjek pajak luar negeri"),
            ("but", "Bentuk usaha tetap"),
        ],
    )
    population_status = forms.ChoiceField(
        label="Status Penduduk untuk identitas NIK",
        initial="unknown",
        choices=[
            ("unknown", "Belum diperiksa"),
            ("resident", "Penduduk"),
            ("nonresident", "Bukan Penduduk"),
            ("na", "Tidak berlaku — penerima badan"),
        ],
    )
    identity_type = forms.ChoiceField(
        label="Jenis identitas pada dokumen",
        initial="unknown",
        choices=[
            ("unknown", "Belum diperiksa"),
            ("npwp16", "NPWP 16 digit"),
            ("nik", "NIK 16 digit"),
            ("none", "Tidak mempunyai NPWP menurut dokumen pemeriksa"),
        ],
    )
    number = forms.CharField(
        label="Nomor pada dokumen",
        max_length=16,
        required=False,
        help_text="Salin nomor lengkap sebagai teks. Nomor yang lolos format belum membuktikan status pajak.",
        widget=forms.TextInput(attrs={"autocomplete": "off"}),
    )
    registration_status = forms.ChoiceField(
        label="Status registrasi menurut dokumen",
        initial="unknown",
        choices=[
            ("unknown", "Belum diperiksa"),
            ("registered", "Terdaftar"),
            ("no_npwp", "Tidak mempunyai NPWP, dengan bukti"),
            ("nonactive", "Nonaktif"),
            ("deleted", "Dihapus"),
        ],
    )
    nik_validated = forms.TypedChoiceField(
        label="NIK sudah tervalidasi menurut dokumen?",
        required=False,
        coerce=nullable_boolean,
        empty_value=None,
        choices=[
            ("", "Belum diperiksa / tidak berlaku"),
            ("yes", "Ya"),
            ("no", "Tidak"),
        ],
    )
    nik_integrated = forms.TypedChoiceField(
        label="NIK sudah terintegrasi dengan sistem DJP menurut dokumen?",
        required=False,
        coerce=nullable_boolean,
        empty_value=None,
        choices=[
            ("", "Belum diperiksa / tidak berlaku"),
            ("yes", "Ya"),
            ("no", "Tidak"),
        ],
    )
    nik_activated = forms.TypedChoiceField(
        label="NIK sudah diaktivasi sebagai NPWP menurut dokumen?",
        required=False,
        coerce=nullable_boolean,
        empty_value=None,
        choices=[
            ("", "Belum diperiksa / tidak berlaku"),
            ("yes", "Ya"),
            ("no", "Tidak"),
        ],
    )
    effective_from = forms.DateField(
        label="Mulai berlaku menurut dokumen",
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
    )
    effective_until = forms.DateField(
        label="Berlaku sampai (jika disebutkan)",
        required=False,
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
    )
    reference = forms.CharField(label="Referensi dokumen / pemeriksaan", max_length=300)
    reason = forms.CharField(
        label="Penjelasan hasil pemeriksaan identitas",
        min_length=10,
        max_length=2000,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    evidence = forms.ModelChoiceField(
        label="Pilih PDF yang sudah diunggah",
        required=False,
        queryset=TaxEvidence.objects.none(),
    )
    file = forms.FileField(
        label="Atau unggah PDF dokumen identitas",
        required=False,
        help_text="PDF asli, maksimum 2 MB. Disimpan privat.",
    )

    def __init__(self, *args, organization, **kwargs):
        from finance.models import Party

        from .models import RecipientTaxIdentity

        super().__init__(*args, **kwargs)
        self.fields["recipient_party"].queryset = Party.objects.filter(
            organization=organization, kind="supplier"
        ).order_by("name")
        self.fields["recipient_party"].label_from_instance = lambda row: row.name
        self.fields["previous"].queryset = (
            RecipientTaxIdentity.objects.filter(
                organization=organization, approval__isnull=False
            )
            .select_related("recipient_party")
            .order_by("-created_at")
        )
        self.fields["previous"].label_from_instance = lambda row: (
            f"{row.legal_name} · versi {row.revision} · {row.masked_number}"
        )
        self.fields["evidence"].queryset = (
            TaxEvidence.objects.filter(organization=organization)
            .defer("content")
            .order_by("-pk")
        )
        self.fields["evidence"].label_from_instance = lambda row: row.original_filename

    def clean(self):
        data = super().clean()
        if data.get("target") == "supplier" and not data.get("recipient_party"):
            self.add_error(
                "recipient_party", "Pilih pemasok yang menerima penghasilan."
            )
        if data.get("target") == "organization" and data.get("recipient_party"):
            self.add_error(
                "recipient_party", "Untuk PT OSEE, kosongkan pilihan pemasok."
            )
        if not data.get("file") and not data.get("evidence"):
            self.add_error(
                "evidence", "Pilih PDF yang tersimpan atau unggah dokumen identitas."
            )
        if data.get("file") and data.get("evidence"):
            self.add_error(
                "file",
                "Gunakan satu dokumen: pilih PDF tersimpan atau unggah PDF baru.",
            )
        return data


class RecipientReviewForm(ProfessionalReviewForm):
    expected_fingerprint = forms.CharField(max_length=64, widget=forms.HiddenInput())
    expected_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput())
    treatment = forms.ChoiceField(
        label="Kesimpulan identitas dalam laporan profesional",
        choices=[
            ("", "Pilih kesimpulan pada laporan"),
            ("standard_npwp", "NPWP terverifikasi — faktor identitas standar"),
            (
                "standard_integrated_nik",
                "NIK terintegrasi terverifikasi — perlakuan standar dikonfirmasi",
            ),
            (
                "increased_no_npwp",
                "Tidak mempunyai NPWP dengan bukti — faktor kenaikan dikonfirmasi",
            ),
            ("unresolved", "Belum dapat dipastikan — tahan penggunaan untuk PPh 23"),
        ],
    )

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, organization=organization, **kwargs)
        self.fields["evidence"].label_from_instance = lambda row: row.original_filename
