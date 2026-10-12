"""Plain staff inputs for withdrawing or replacing one real advertising source."""

from django import forms

from .models import Campaign


class AdRecoveryForm(forms.Form):
    expected_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    expected_fingerprint = forms.RegexField(r"^[0-9a-f]{64}$", widget=forms.HiddenInput)
    request_key = forms.CharField(max_length=160, widget=forms.HiddenInput)
    reason = forms.CharField(
        label="Mengapa laporan ini perlu diperbaiki?",
        max_length=500,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    evidence = forms.CharField(
        label="Bukti yang sudah diperiksa",
        max_length=4000,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Tulis lokasi arsip atau referensi laporan platform dan bagian yang membuktikan koreksi. Jangan masukkan kata sandi atau token akun.",
    )
    confirmed = forms.BooleanField(
        label="Saya sudah memeriksa sumber dan menyetujui perubahan ini"
    )

    def __init__(self, *args, organization, observation, **kwargs):
        self.organization = organization
        self.observation = observation
        super().__init__(*args, **kwargs)

    def clean(self):
        values = super().clean()
        if hasattr(self.data, "getlist") and any(
            len(self.data.getlist(name)) > 1 for name in (*self.fields, "action")
        ):
            raise forms.ValidationError(
                "Ada pilihan yang terkirim lebih dari sekali. Periksa kembali formulir sebelum menyimpan."
            )
        return values


class AdReplacementForm(AdRecoveryForm):
    campaign = forms.ModelChoiceField(
        label="Kampanye yang benar", queryset=Campaign.objects.none()
    )
    date = forms.DateField(
        label="Tanggal laporan yang benar",
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
    )
    ad_name = forms.CharField(
        label="Nama / ID iklan",
        max_length=180,
        help_text="Isi nama dari sumber. Gunakan ‘Semua iklan’ hanya untuk total seluruh iklan.",
    )
    audience = forms.CharField(label="Audiens", max_length=160)
    region = forms.CharField(label="Wilayah", max_length=120)
    spend = forms.DecimalField(
        label="Biaya iklan dilaporkan (Rp)",
        max_digits=20,
        decimal_places=2,
        min_value=0,
        required=False,
        widget=forms.NumberInput(attrs={"step": "0.01", "inputmode": "decimal"}),
        help_text="Kosong berarti belum diketahui. Nol hanya jika sumber menyatakan tidak ada biaya.",
    )
    impressions = forms.IntegerField(
        label="Tayangan", min_value=0, max_value=2147483647, required=False
    )
    clicks = forms.IntegerField(
        label="Klik", min_value=0, max_value=2147483647, required=False
    )
    source = forms.CharField(label="Sumber laporan", max_length=180)
    reference = forms.CharField(
        label="Referensi dokumen yang dipertahankan",
        disabled=True,
        help_text="Ini tetap laporan sumber yang sama. Koreksi tidak membutuhkan referensi dokumen baru.",
    )

    def __init__(self, *args, organization, observation, **kwargs):
        initial = {
            name: getattr(observation, name)
            for name in (
                "campaign",
                "date",
                "ad_name",
                "audience",
                "region",
                "spend",
                "impressions",
                "clicks",
                "source",
                "reference",
            )
        }
        initial.update(kwargs.pop("initial", {}))
        super().__init__(
            *args,
            organization=organization,
            observation=observation,
            initial=initial,
            **kwargs,
        )
        self.fields["campaign"].queryset = Campaign.objects.filter(
            organization=organization
        )
        self.order_fields(
            [
                "expected_version",
                "expected_fingerprint",
                "request_key",
                "campaign",
                "date",
                "ad_name",
                "audience",
                "region",
                "spend",
                "impressions",
                "clicks",
                "source",
                "reference",
                "reason",
                "evidence",
                "confirmed",
            ]
        )

    def clean(self):
        values = super().clean()
        if all(values.get(name) is None for name in ("spend", "impressions", "clicks")):
            raise forms.ValidationError(
                "Isi minimal satu angka sesuai laporan. Kolom yang belum diketahui boleh tetap kosong."
            )
        return values
