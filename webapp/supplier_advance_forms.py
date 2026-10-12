"""Scoped staff inputs for the supplier advance public workflow."""

from decimal import Decimal
from uuid import uuid4

from django import forms
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils import timezone

from finance import services
from finance.models import BankTransaction, Bill, Party

from .forms import BusinessDateField
from .templatetags.finance_format import iddate, money


def selected_id(data, field):
    value = data.get(field, "") if data else ""
    return (
        int(value)
        if isinstance(value, str)
        and value.isascii()
        and value.isdigit()
        and len(value) <= 18
        else None
    )


class BankSourceField(forms.ModelChoiceField):
    def label_from_instance(self, source):
        return f"{iddate(source.date)} · {source.account.name} · {source.reference} · {money(source.amount)}"


def bank_sources(organization, data, *, positive):
    pending = services.pending_bank_transactions(organization=organization)
    selected = selected_id(data, "transaction")
    sources = BankTransaction.objects.filter(
        organization=organization, account__organization=organization
    )
    sources = (
        sources.filter(Q(pk__in=pending.values("pk")) | Q(pk=selected))
        if selected
        else sources.filter(pk__in=pending.values("pk"))
    )
    return sources.filter(amount__gt=0) if positive else sources.filter(amount__lt=0)


class SupplierAdvanceRequestForm(forms.Form):
    request_key = forms.CharField(
        max_length=120,
        widget=forms.HiddenInput,
        error_messages={
            "required": "Periksa sumber terlebih dahulu sebelum menyimpan."
        },
    )
    expected_fingerprint = forms.RegexField(
        regex=r"^[0-9a-f]{64}$",
        max_length=64,
        widget=forms.HiddenInput,
        error_messages={
            "required": "Periksa sumber terlebih dahulu sebelum menyimpan.",
            "invalid": "Pemeriksaan sumber belum tersedia. Periksa sumber kembali.",
        },
    )
    reason = forms.CharField(
        label="Alasan pencatatan",
        min_length=3,
        max_length=500,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    evidence = forms.CharField(
        label="Dokumen dan hasil pemeriksaan",
        min_length=3,
        max_length=4000,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Tulis nomor atau nama dokumen dan hasil pemeriksaan yang mendukung catatan ini.",
    )
    confirmed = forms.BooleanField(
        label="Saya telah memeriksa pihak, tanggal, jumlah, dan dokumen yang dipilih."
    )

    def __init__(self, data=None, *, organization, checking=False, **kwargs):
        self.organization = organization
        initial = {"request_key": str(uuid4())}
        initial.update(kwargs.pop("initial", {}))
        super().__init__(data, initial=initial, **kwargs)
        for name in ("request_key", "expected_fingerprint", "confirmed"):
            self.fields[name].required = not checking
        if checking:
            for name in ("amount", "source_reference", "reason", "evidence"):
                if name in self.fields:
                    self.fields[name].required = False

    def clean(self):
        values = super().clean()
        if hasattr(self.data, "getlist") and any(
            len(self.data.getlist(name)) > 1 for name in (*self.fields, "intent")
        ):
            raise ValidationError(
                "Ada isian ganda. Kirim satu pilihan untuk setiap kolom."
            )
        if set(self.data) - set(self.fields) - {"intent", "csrfmiddlewaretoken"}:
            raise ValidationError(
                "Ada isian yang tidak sesuai formulir ini. Periksa sumber kembali."
            )
        return values


class SupplierAdvanceRecordForm(SupplierAdvanceRequestForm):
    supplier = forms.ModelChoiceField(label="Pemasok", queryset=Party.objects.none())
    transaction = BankSourceField(
        label="Uang keluar dari bank", queryset=BankTransaction.objects.none()
    )
    amount = forms.DecimalField(
        label="Jumlah uang muka (Rp)",
        min_value=Decimal("0.01"),
        max_digits=20,
        decimal_places=2,
        help_text="Isi jumlah uang bank yang sudah keluar untuk pesanan ini.",
    )
    source_reference = forms.CharField(
        label="Nomor pesanan / dokumen uang muka",
        max_length=160,
        help_text="Salin nomor pesanan, proforma, atau dokumen uang muka pemasok.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["supplier"].queryset = Party.objects.filter(
            organization=self.organization, kind="supplier"
        ).order_by("name")
        self.fields["transaction"].queryset = (
            bank_sources(self.organization, self.data, positive=False)
            .select_related("account")
            .order_by("-date", "-pk")
        )
        self.fields[
            "confirmed"
        ].label = "Saya telah memeriksa pemasok, bukti uang keluar, tanggal, jumlah, dan dokumen uang muka."
        self.order_fields(
            [
                "request_key",
                "expected_fingerprint",
                "supplier",
                "transaction",
                "source_reference",
                "amount",
                "reason",
                "evidence",
                "confirmed",
            ]
        )


class SupplierAdvanceApplyForm(SupplierAdvanceRequestForm):
    bill = forms.ModelChoiceField(
        label="Tagihan pemasok ini", queryset=Bill.objects.none()
    )
    date = BusinessDateField(
        label="Tanggal uang muka digunakan",
        initial=timezone.localdate,
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
        help_text="Gunakan tanggal aktual pada periode buku yang masih terbuka.",
    )
    amount = forms.DecimalField(
        label="Jumlah yang digunakan (Rp)",
        min_value=Decimal("0.01"),
        max_digits=20,
        decimal_places=2,
    )

    def __init__(self, *args, advance, **kwargs):
        super().__init__(*args, **kwargs)
        sources = Bill.objects.filter(
            organization=self.organization,
            supplier__organization=self.organization,
            supplier_id=advance.supplier_id,
        )
        eligible = sources.filter(
            status="approved", tax_status="reviewed", tax_amount=0
        )
        selected = selected_id(self.data, "bill")
        if selected:
            eligible = sources.filter(Q(pk__in=eligible.values("pk")) | Q(pk=selected))
        self.fields["bill"].queryset = eligible.order_by("-date", "-pk")
        self.fields["bill"].label_from_instance = lambda source: (
            f"{source.number} · {source.supplier_reference or 'Dokumen lama'} · {iddate(source.date)}"
        )
        self.fields[
            "bill"
        ].help_text = "Pilih tagihan yang sudah disetujui dan pajaknya sudah diperiksa. Sisa tagihan ditampilkan setelah Periksa sumber."
        self.fields[
            "confirmed"
        ].label = "Saya telah memeriksa tagihan, pemasok, tanggal, jumlah, dan bukti. Uang bank baru Rp0."
        self.order_fields(
            [
                "request_key",
                "expected_fingerprint",
                "bill",
                "date",
                "amount",
                "reason",
                "evidence",
                "confirmed",
            ]
        )


class SupplierAdvanceRefundForm(SupplierAdvanceRequestForm):
    transaction = BankSourceField(
        label="Uang masuk dari pemasok", queryset=BankTransaction.objects.none()
    )
    amount = forms.DecimalField(
        label="Jumlah yang dikembalikan (Rp)",
        min_value=Decimal("0.01"),
        max_digits=20,
        decimal_places=2,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["transaction"].queryset = (
            bank_sources(self.organization, self.data, positive=True)
            .select_related("account")
            .order_by("-date", "-pk")
        )
        self.fields[
            "confirmed"
        ].label = "Saya telah memeriksa pemasok, bukti uang masuk, tanggal, jumlah pengembalian, dan dokumen pendukung."
        self.order_fields(
            [
                "request_key",
                "expected_fingerprint",
                "transaction",
                "amount",
                "reason",
                "evidence",
                "confirmed",
            ]
        )


class SupplierAdvanceReverseForm(SupplierAdvanceRequestForm):
    date = BusinessDateField(
        label="Tanggal pembalikan",
        initial=timezone.localdate,
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
        help_text="Gunakan tanggal aktual pada periode buku yang masih terbuka.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["reason"].label = "Alasan pembalikan"
        self.fields[
            "confirmed"
        ].label = "Saya telah memeriksa pembalikan seluruh penerapan ini, tanggal, alasan, dan bukti. Uang bank tidak berubah."
        self.order_fields(
            [
                "request_key",
                "expected_fingerprint",
                "date",
                "reason",
                "evidence",
                "confirmed",
            ]
        )
