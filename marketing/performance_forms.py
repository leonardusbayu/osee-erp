"""Forms for accountable marketing work; persistence belongs to services."""

from decimal import Decimal

from django import forms
from django.core.exceptions import ValidationError

from director.models import CHANNEL_CHOICES, Budget, Decision

from .kpi_models import KpiTarget
from .models import Campaign, Lead, TeamMember

KPI_STATUSES = [("draft", "Draf"), ("active", "Aktif"), ("retired", "Dihentikan")]
ACTIVITY_STATUSES = [
    ("planned", "Direncanakan"),
    ("in_progress", "Dikerjakan"),
    ("submitted", "Menunggu pemeriksaan"),
    ("accepted", "Diterima"),
    ("needs_revision", "Perlu perbaikan"),
    ("cancelled", "Dibatalkan"),
]
ACTIVITY_KINDS = [
    ("follow_up", "Follow-up prospek"),
    ("content", "Konten"),
    ("seo", "SEO"),
    ("ads", "Iklan"),
    ("partner", "Kemitraan"),
    ("sales", "Sales"),
]


class ScopedPerformanceForm(forms.Form):
    def __init__(self, *args, organization, active_members=False, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if isinstance(field, forms.ModelChoiceField):
                field.queryset = field.queryset.filter(organization=organization)
                if active_members and name in {"owner", "member"}:
                    field.queryset = field.queryset.filter(active=True)
            if isinstance(field, forms.DateField):
                field.widget = forms.DateInput(
                    format="%Y-%m-%d", attrs={"type": "date"}
                )
            if isinstance(field.widget, forms.Textarea):
                field.widget.attrs["rows"] = 3


class KpiTargetForm(ScopedPerformanceForm):
    title = forms.CharField(
        label="Nama target",
        max_length=200,
        help_text="Contoh: Pembayaran prospek Meta Ads bulan Oktober.",
    )
    metric_key = forms.ChoiceField(label="Ukuran hasil (KPI)")
    target = forms.DecimalField(
        label="Nilai target",
        min_value=Decimal(0),
        max_digits=20,
        decimal_places=2,
        help_text="Gunakan rupiah penuh untuk kas atau biaya; persentase 0–100 untuk konversi/cakupan; menit untuk respons.",
    )
    period_start = forms.DateField(label="Awal periode")
    period_end = forms.DateField(label="Akhir periode")
    member = forms.ModelChoiceField(
        label="PIC saat prospek dicatat",
        queryset=TeamMember.objects.all(),
        required=False,
        help_text="Kosong = seluruh tim. Belanja iklan tidak dapat dibagi ke anggota tanpa dasar biaya.",
    )
    channel = forms.ChoiceField(
        label="Lingkup channel",
        choices=[("", "Semua channel"), *CHANNEL_CHOICES],
        required=False,
    )
    campaign = forms.ModelChoiceField(
        label="Lingkup kampanye", queryset=Campaign.objects.all(), required=False
    )
    rationale = forms.CharField(
        label="Alasan target dan rencana mencapainya",
        max_length=4000,
        widget=forms.Textarea,
        help_text="Jelaskan dasar angka, pembagian tanggung jawab, dan konteks evaluasinya. Direktur meninjau sebelum mengaktifkan.",
    )

    def __init__(self, *args, **kwargs):
        from .kpi import KPI_METRICS

        kwargs["active_members"] = True
        super().__init__(*args, **kwargs)
        self.fields["metric_key"].choices = [
            (key, value["label"]) for key, value in KPI_METRICS.items()
        ]
        self.fields["target"].widget.attrs.update(
            {"step": "0.01", "inputmode": "decimal"}
        )

    def clean(self):
        data = super().clean()
        if (
            data.get("period_start")
            and data.get("period_end")
            and data["period_end"] < data["period_start"]
        ):
            self.add_error(
                "period_end", "Akhir periode tidak boleh mendahului awalnya."
            )
        return data


class KpiFilterForm(ScopedPerformanceForm):
    status = forms.ChoiceField(
        label="Status target",
        choices=[("", "Semua status"), *KPI_STATUSES],
        required=False,
    )
    member = forms.ModelChoiceField(
        label="Anggota", queryset=TeamMember.objects.all(), required=False
    )
    channel = forms.ChoiceField(
        label="Channel",
        choices=[("", "Semua channel"), *CHANNEL_CHOICES],
        required=False,
    )
    start = forms.DateField(label="Bersinggungan sejak", required=False)
    end = forms.DateField(label="Sampai", required=False)

    def clean(self):
        data = super().clean()
        if data.get("start") and data.get("end") and data["end"] < data["start"]:
            raise ValidationError(
                "Tanggal akhir filter tidak boleh mendahului awalnya."
            )
        return data


class ReasonForm(forms.Form):
    reason = forms.CharField(
        label="Catatan keputusan",
        max_length=500,
        widget=forms.Textarea(attrs={"rows": 3}),
    )


class KpiReviewForm(forms.Form):
    expected_fingerprint = forms.CharField(
        min_length=64, max_length=64, widget=forms.HiddenInput
    )
    conclusion = forms.CharField(
        label="Kesimpulan evaluasi dan langkah berikutnya",
        max_length=4000,
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text="Hasil angka disimpan otomatis bersama dasar perhitungannya. Catat konteks dan tindak lanjut yang disepakati.",
    )

    def clean(self):
        data = super().clean()
        if "expected_fingerprint" not in data:
            raise ValidationError(
                "Dasar angka evaluasi tidak lengkap. Muat ulang halaman sebelum menyimpan evaluasi."
            )
        return data


class ActivityForm(ScopedPerformanceForm):
    title = forms.CharField(label="Pekerjaan yang harus diselesaikan", max_length=200)
    owner = forms.ModelChoiceField(
        label="Penanggung jawab", queryset=TeamMember.objects.all()
    )
    kind = forms.ChoiceField(label="Jenis aktivitas", choices=ACTIVITY_KINDS)
    channel = forms.ChoiceField(label="Channel", choices=CHANNEL_CHOICES)
    due_date = forms.DateField(label="Tenggat")
    campaign = forms.ModelChoiceField(
        label="Kampanye terkait", queryset=Campaign.objects.all(), required=False
    )
    lead = forms.ModelChoiceField(
        label="Prospek terkait", queryset=Lead.objects.all(), required=False
    )
    description = forms.CharField(
        label="Hasil yang diharapkan dan petunjuk",
        required=False,
        max_length=4000,
        widget=forms.Textarea,
        help_text="Jelaskan pekerjaan dan bukti yang perlu disiapkan. Jangan masukkan password atau data pribadi yang tidak diperlukan.",
    )
    kpi_target = forms.ModelChoiceField(
        label="KPI yang didukung",
        queryset=KpiTarget.objects.exclude(status="retired"),
        required=False,
    )
    decision = forms.ModelChoiceField(
        label="Keputusan direktur terkait",
        queryset=Decision.objects.filter(status__in=["approved", "active"]),
        required=False,
    )
    budget = forms.ModelChoiceField(
        label="Budget terkait (tidak membukukan biaya)",
        queryset=Budget.objects.filter(status="approved"),
        required=False,
    )

    def __init__(self, *args, **kwargs):
        kwargs["active_members"] = True
        super().__init__(*args, **kwargs)


class ActivityFilterForm(ScopedPerformanceForm):
    q = forms.CharField(label="Cari pekerjaan", max_length=100, required=False)
    owner = forms.ModelChoiceField(
        label="Penanggung jawab", queryset=TeamMember.objects.all(), required=False
    )
    status = forms.ChoiceField(
        label="Status",
        choices=[("", "Semua status"), *ACTIVITY_STATUSES],
        required=False,
    )
    kind = forms.ChoiceField(
        label="Jenis", choices=[("", "Semua jenis"), *ACTIVITY_KINDS], required=False
    )
    overdue = forms.ChoiceField(
        label="Tenggat",
        choices=[("", "Semua jadwal"), ("1", "Lewat tenggat")],
        required=False,
    )


class ActivitySubmitForm(forms.Form):
    responsibility_reason = forms.CharField(
        label="Alasan bertindak sebagai pengawas (bila bukan PIC/delegasi)",
        required=False,
        max_length=4000,
        widget=forms.Textarea(attrs={"rows": 2}),
    )
    occurred_on = forms.DateField(
        label="Tanggal pekerjaan selesai",
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
    )
    evidence = forms.CharField(
        label="Bukti pelaksanaan",
        max_length=4000,
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text="Cantumkan lokasi arsip, referensi laporan, atau tautan hasil kerja yang dapat diperiksa. Catatan ini tidak mengunggah dokumen otomatis.",
    )
    outcome = forms.CharField(
        label="Hasil yang benar-benar terjadi",
        max_length=4000,
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text="Tuliskan hasil, hambatan, dan langkah berikutnya. Penyelesaian aktivitas tidak otomatis membuktikan pembayaran atau pertumbuhan penjualan.",
    )


class ActivityDecisionForm(forms.Form):
    review_notes = forms.CharField(
        label="Catatan pemeriksaan / alasan keputusan",
        max_length=4000,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    responsibility_reason = forms.CharField(
        label="Alasan bertindak sebagai pengawas (untuk pembatalan)",
        required=False,
        max_length=4000,
        widget=forms.Textarea(attrs={"rows": 2}),
    )
