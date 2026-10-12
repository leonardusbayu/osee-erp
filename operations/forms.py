from django import forms

from director.models import CHANNEL_CHOICES
from finance.models import Invoice, Party, Product
from marketing.identity_review_forms import LegacyIdentityReviewForm
from marketing.models import Lead

from .intake import FIELDS
from .models import IntakeRow, Order, TestSession

DATE = {"type": "date"}


class ScopedForm(forms.Form):
    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if isinstance(field, forms.ModelChoiceField):
                field.queryset = field.queryset.filter(organization=organization)


class OrderForm(ScopedForm):
    reference = forms.CharField(label="Nomor pesanan", max_length=100)
    buyer = forms.ModelChoiceField(
        label="Pemesan / mitra yang ditagih",
        queryset=Party.objects.exclude(kind="supplier"),
    )
    date = forms.DateField(label="Tanggal pesanan", widget=forms.DateInput(attrs=DATE))
    channel = forms.ChoiceField(
        label="Kanal asal yang sudah dikonfirmasi",
        required=False,
        choices=[("", "Belum diketahui"), *CHANNEL_CHOICES],
    )


class LineForm(ScopedForm):
    product = forms.ModelChoiceField(label="Produk", queryset=Product.objects.all())
    reference = forms.CharField(
        label="Nama rincian / kelompok", max_length=100, required=False
    )
    quantity = forms.IntegerField(
        label="Jumlah unit layanan / peserta", min_value=1, max_value=100000
    )


class RegistrationForm(forms.Form):
    name = forms.CharField(label="Nama peserta", max_length=200)
    source = forms.CharField(
        label="Sumber pendaftaran",
        max_length=100,
        help_text="Misalnya form website atau daftar mitra. Gunakan nama sumber yang konsisten.",
    )
    external_id = forms.CharField(
        label="ID pendaftaran dari sumber",
        max_length=160,
        help_text="Gunakan ID respons yang tetap; bukan nomor baris sheet yang dapat berubah.",
    )
    email = forms.EmailField(label="Email peserta", required=False)
    phone = forms.CharField(label="Nomor kontak", max_length=40, required=False)
    planned_date = forms.DateField(
        label="Tanggal tes yang diminta",
        required=False,
        widget=forms.DateInput(attrs=DATE),
    )


class InvoiceLineForm(forms.Form):
    date = forms.DateField(label="Tanggal invoice", widget=forms.DateInput(attrs=DATE))
    due_date = forms.DateField(label="Jatuh tempo", widget=forms.DateInput(attrs=DATE))
    confirm = forms.BooleanField(
        label="Pemesan, produk, jumlah, dan harga pada pratinjau telah saya periksa",
        required=False,
    )
    quote_token = forms.CharField(required=False, widget=forms.HiddenInput)


class OrderLeadForm(ScopedForm):
    lead = forms.ModelChoiceField(
        label="Prospek sumber pesanan (kosongkan untuk melepas)",
        queryset=Lead.objects.all(),
        required=False,
    )
    expected_version = forms.IntegerField(widget=forms.HiddenInput)
    reason = forms.CharField(
        label="Alasan dan bukti kecocokan", max_length=2000, widget=forms.Textarea
    )
    confirm_identity = forms.BooleanField(
        label="Identitas pemesan sudah diperiksa terhadap sumber prospek",
        required=False,
    )


class ExistingInvoiceForm(ScopedForm):
    invoice = forms.ModelChoiceField(
        label="Invoice yang sudah terbit",
        queryset=Invoice.objects.filter(status="issued"),
    )
    reason = forms.CharField(
        label="Alasan dan bukti kecocokan", max_length=2000, widget=forms.Textarea
    )


class AttributionCorrectionForm(ScopedForm, LegacyIdentityReviewForm):
    invoice = forms.ModelChoiceField(
        label="Invoice pengganti (kosongkan untuk melepas)",
        queryset=Invoice.objects.filter(status__in=("issued", "delivered")),
        required=False,
    )
    expected_version = forms.IntegerField(widget=forms.HiddenInput)
    reason = forms.CharField(
        label="Alasan koreksi dan bukti", max_length=2000, widget=forms.Textarea
    )


class SessionForm(ScopedForm):
    reference = forms.CharField(label="Nama / nomor sesi", max_length=100)
    product = forms.ModelChoiceField(label="Produk", queryset=Product.objects.all())
    starts_at = forms.DateTimeField(
        label="Mulai sesi (WIB)",
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
    )
    capacity = forms.IntegerField(
        label="Kapasitas yang disetujui", min_value=1, max_value=100000
    )


class CoursePlanForm(forms.Form):
    total_sessions = forms.IntegerField(
        label="Jumlah pertemuan dalam paket",
        min_value=1,
        max_value=200,
    )
    reason = forms.CharField(
        label="Bukti jumlah pertemuan",
        max_length=2000,
        help_text="Contoh: jumlah sesi pada penawaran atau kesepakatan kursus.",
        widget=forms.Textarea(attrs={"rows": 3}),
    )


class CoursePlanRevisionForm(CoursePlanForm):
    expected_version = forms.IntegerField(widget=forms.HiddenInput)


class CourseMeetingForm(forms.Form):
    expected_version = forms.IntegerField(widget=forms.HiddenInput)
    starts_at = forms.DateTimeField(
        label="Tanggal dan waktu pertemuan (WIB)",
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
    )
    reason = forms.CharField(
        label="Konfirmasi jadwal",
        max_length=2000,
        widget=forms.Textarea(attrs={"rows": 2}),
    )


class ScheduleForm(ScopedForm):
    expected_version = forms.IntegerField(widget=forms.HiddenInput)
    session = forms.ModelChoiceField(
        label="Sesi tujuan", queryset=TestSession.objects.all()
    )
    reason = forms.CharField(
        label="Alasan / konfirmasi peserta", max_length=2000, widget=forms.Textarea
    )


class AttendanceForm(forms.Form):
    expected_version = forms.IntegerField(widget=forms.HiddenInput)
    attended = forms.TypedChoiceField(
        label="Hasil pemeriksaan kehadiran",
        choices=[("yes", "Hadir"), ("no", "Tidak hadir")],
        coerce=lambda x: x == "yes",
    )
    evidence = forms.CharField(
        label="Referensi bukti kehadiran", max_length=2000, widget=forms.Textarea
    )


class CourseAttendanceForm(forms.Form):
    expected_version = forms.IntegerField(widget=forms.HiddenInput)
    attended = forms.TypedChoiceField(
        label="Kehadiran peserta",
        choices=[("yes", "Hadir"), ("no", "Tidak hadir")],
        coerce=lambda value: value == "yes",
    )
    evidence = forms.CharField(
        label="Bukti / referensi daftar hadir",
        max_length=2000,
        help_text="Simpan referensi daftar kelas atau bukti kehadiran yang sudah diperiksa.",
        widget=forms.Textarea(attrs={"rows": 3}),
    )


class CourseAttendanceCorrectionForm(CourseAttendanceForm):
    reason = forms.CharField(
        label="Alasan koreksi",
        max_length=2000,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    confirm = forms.BooleanField(
        label="Saya sudah memeriksa ulang bukti dan menyetujui koreksi"
    )


class AttendanceCorrectionForm(forms.Form):
    expected_version = forms.IntegerField(widget=forms.HiddenInput)
    attended = forms.TypedChoiceField(
        label="Hasil kehadiran yang sudah diperiksa ulang",
        choices=[("yes", "Hadir"), ("no", "Tidak hadir")],
        coerce=lambda x: x == "yes",
    )
    reason = forms.CharField(
        label="Alasan dan bukti pemeriksaan ulang",
        max_length=2000,
        widget=forms.Textarea,
    )
    confirm = forms.BooleanField(
        label="Saya sudah memeriksa bukti dan menyetujui koreksi kehadiran"
    )


class IdentityCorrectionForm(forms.Form):
    expected_version = forms.IntegerField(widget=forms.HiddenInput)
    name = forms.CharField(label="Nama peserta sesuai bukti", max_length=200)
    email = forms.EmailField(label="Email peserta", required=False)
    phone = forms.CharField(label="Nomor kontak peserta", max_length=40, required=False)
    reason = forms.CharField(
        label="Alasan dan referensi bukti", max_length=2000, widget=forms.Textarea
    )
    confirm = forms.BooleanField(
        label="Saya sudah mencocokkan data baru dengan bukti asli"
    )


class CompleteForm(forms.Form):
    expected_version = forms.IntegerField(widget=forms.HiddenInput)
    date = forms.DateField(
        label="Tanggal aktual layanan selesai", widget=forms.DateInput(attrs=DATE)
    )
    evidence = forms.CharField(
        label="Referensi bukti layanan selesai", max_length=2000, widget=forms.Textarea
    )


class CancelForm(forms.Form):
    expected_version = forms.IntegerField(widget=forms.HiddenInput)
    date = forms.DateField(
        label="Tanggal koreksi / pembatalan", widget=forms.DateInput(attrs=DATE)
    )
    reason = forms.CharField(
        label="Alasan dan bukti persetujuan", max_length=2000, widget=forms.Textarea
    )
    confirm = forms.BooleanField(
        label="Saya menyetujui pembatalan unit dan koreksi invoice terkait; pengembalian uang dicocokkan terpisah oleh Finance"
    )


class UploadForm(forms.Form):
    source = forms.CharField(
        label="Nama sumber",
        max_length=100,
        help_text="Gunakan nama yang sama untuk file dari formulir atau sistem yang sama.",
    )
    file = forms.FileField(
        label="File pendaftaran CSV / XLSX",
        widget=forms.FileInput(attrs={"accept": ".csv,.xlsx"}),
        help_text="Maksimal 5 MB dan 5.000 baris; judul kolom pada baris pertama. Tidak mengunduh tautan dokumen di dalam file.",
    )

    def clean_file(self):
        value = self.cleaned_data["file"]
        if value.size > 5 * 1024 * 1024 or not value.name.lower().endswith(
            (".csv", ".xlsx")
        ):
            raise forms.ValidationError("Pilih CSV/XLSX berukuran maksimal 5 MB.")
        return value


class MappingForm(forms.Form):
    expected_version = forms.IntegerField(widget=forms.HiddenInput)

    def __init__(self, *args, batch, **kwargs):
        super().__init__(*args, **kwargs)
        choices = [
            ("", "Tidak tersedia pada sumber"),
            *[(str(i), f"{i + 1}. {value}") for i, value in enumerate(batch.headers)],
        ]
        for key, label in FIELDS.items():
            self.fields[key] = forms.ChoiceField(
                label=label,
                choices=choices,
                required=key == "name",
                initial=batch.mapping.get(key, ""),
            )


class IntakeReviewForm(ScopedForm):
    expected_version = forms.IntegerField(widget=forms.HiddenInput)
    row_ids = forms.ModelMultipleChoiceField(
        label="Baris yang dipilih",
        queryset=IntakeRow.objects.none(),
        widget=forms.CheckboxSelectMultiple,
    )
    order = forms.ModelChoiceField(
        label="Pesanan / pemesan yang benar",
        queryset=Order.objects.all(),
        required=False,
    )
    product = forms.ModelChoiceField(
        label="Produk yang dikonfirmasi", queryset=Product.objects.all(), required=False
    )
    reason = forms.CharField(
        label="Hasil pemeriksaan / alasan pengecualian",
        max_length=2000,
        widget=forms.Textarea,
    )
    confirm_identity = forms.BooleanField(
        label="Saya sudah memeriksa pemesan, produk, identitas sumber, dan kemungkinan duplikat",
        required=False,
    )
    reviewed_duplicates = forms.BooleanField(
        label="Nama/tanggal yang sama sudah diperiksa dan ini adalah pendaftaran berbeda",
        required=False,
    )

    def __init__(self, *args, batch, page_rows, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["row_ids"].queryset = batch.rows.filter(
            pk__in=[row.pk for row in page_rows], status="pending"
        )
        self.fields["row_ids"].label_from_instance = lambda row: (
            f"Baris {row.row_number} · {row.normalized.get('name', 'Belum dipetakan')}"
        )
