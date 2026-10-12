"""Small, plain-language forms for finance staff and approvers."""

from django import forms
from django.utils import timezone

from finance.models import BankTransaction

from .models import PayrollPayment, PayrollPerson


class PersonForm(forms.Form):
    name = forms.CharField(label="Nama karyawan atau tutor", max_length=150)
    kind = forms.ChoiceField(label="Jenis orang", choices=PayrollPerson.Kind.choices)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["name"].widget.attrs.update(
            {"autocomplete": "name", "placeholder": "Contoh: Nama staf"}
        )
        self.fields[
            "kind"
        ].help_text = "Pilih kategori sesuai data perusahaan. Aplikasi tidak menentukan status pajaknya."


class PayrollRunForm(forms.Form):
    year = forms.IntegerField(label="Tahun", min_value=2000, max_value=2200)
    month = forms.ChoiceField(
        label="Bulan",
        choices=[
            ("1", "Januari"),
            ("2", "Februari"),
            ("3", "Maret"),
            ("4", "April"),
            ("5", "Mei"),
            ("6", "Juni"),
            ("7", "Juli"),
            ("8", "Agustus"),
            ("9", "September"),
            ("10", "Oktober"),
            ("11", "November"),
            ("12", "Desember"),
        ],
    )

    def __init__(self, *args, **kwargs):
        initial = kwargs.setdefault("initial", {})
        today = timezone.localdate()
        initial.setdefault("year", today.year)
        initial.setdefault("month", today.month)
        super().__init__(*args, **kwargs)

    def clean_month(self):
        return int(self.cleaned_data["month"])


class PayrollLineForm(forms.Form):
    person = forms.ModelChoiceField(queryset=PayrollPerson.objects.none(), label="Nama")
    gross_amount = forms.DecimalField(
        label="Gaji / honor sebelum potongan (Rp)",
        min_value=0.01,
        decimal_places=2,
        max_digits=20,
    )
    pph21_amount = forms.DecimalField(
        label="PPh 21 yang sudah disetujui (Rp)",
        min_value=0,
        decimal_places=2,
        max_digits=20,
        initial=0,
    )
    bpjs_employee_amount = forms.DecimalField(
        label="BPJS yang dibayar karyawan (Rp)",
        min_value=0,
        decimal_places=2,
        max_digits=20,
        initial=0,
    )
    bpjs_employer_amount = forms.DecimalField(
        label="BPJS yang dibayar perusahaan (Rp)",
        min_value=0,
        decimal_places=2,
        max_digits=20,
        initial=0,
    )
    basis_reference = forms.CharField(
        label="Bukti persetujuan angka", max_length=240, required=False
    )
    note = forms.CharField(label="Catatan (opsional)", max_length=500, required=False)

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["person"].queryset = PayrollPerson.objects.filter(
            organization=organization, active=True
        ).order_by("name")
        self.fields[
            "pph21_amount"
        ].help_text = "Masukkan angka yang sudah diperiksa dan disetujui. Aplikasi tidak menghitung pajak."
        self.fields[
            "bpjs_employee_amount"
        ].help_text = "Masukkan nominal yang sudah disetujui; jangan menebak atau memakai persentase otomatis."
        self.fields[
            "bpjs_employer_amount"
        ].help_text = "Masukkan nominal yang sudah disetujui; jangan menebak atau memakai persentase otomatis."
        self.fields[
            "basis_reference"
        ].help_text = "Wajib jika ada nominal PPh 21 atau BPJS. Contoh: nomor dokumen atau keputusan yang sudah diperiksa."
        self.fields[
            "note"
        ].help_text = "Jangan masukkan NIK, alamat, atau nomor rekening pribadi."
        for field in self.fields.values():
            if isinstance(field, forms.DecimalField):
                field.widget.attrs.update({"inputmode": "decimal", "step": "0.01"})


class PayrollPaymentForm(forms.Form):
    kind = forms.ChoiceField(
        label="Pembayaran untuk", choices=PayrollPayment.Kind.choices
    )
    transaction = forms.ModelChoiceField(
        queryset=BankTransaction.objects.none(), label="Mutasi keluar BNI"
    )
    amount = forms.DecimalField(
        label="Nominal yang dibayar (Rp)",
        min_value=0.01,
        decimal_places=2,
        max_digits=20,
    )

    def __init__(self, *args, organization, position, **kwargs):
        super().__init__(*args, **kwargs)
        outstanding = {key for key, row in position.items() if row["remaining"] > 0}
        self.fields["kind"].choices = [
            (value, label)
            for value, label in PayrollPayment.Kind.choices
            if value in outstanding
        ]
        candidates = (
            BankTransaction.objects.filter(
                organization=organization, amount__lt=0, date__lte=timezone.localdate()
            )
            .select_related("account")
            .order_by("-date", "-pk")[:200]
        )
        self.fields["transaction"].queryset = BankTransaction.objects.filter(
            pk__in=[row.pk for row in candidates if row.unallocated_amount < 0]
        ).order_by("-date", "-pk")
        self.fields["transaction"].label_from_instance = lambda row: (
            f"{row.date:%d/%m/%Y} · {row.account.name} · {row.reference} · Rp {abs(row.unallocated_amount):,.2f}"
        )
        self.fields[
            "transaction"
        ].help_text = "Pilih mutasi bank yang benar-benar sudah diimpor."


class PayrollCorrectionForm(forms.Form):
    reversal_date = forms.DateField(
        label="Tanggal koreksi", widget=forms.DateInput(attrs={"type": "date"})
    )
    reason = forms.CharField(
        label="Alasan koreksi", max_length=500, widget=forms.Textarea(attrs={"rows": 3})
    )
