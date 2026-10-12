"""Staff inputs for evidenced old receivables and payables."""

from decimal import Decimal
from uuid import uuid4

from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone

from finance.models import BankTransaction, OpeningBalanceItem, Party

from .forms import BusinessDateField
from .supplier_advance_forms import BankSourceField, bank_sources


class OpeningBalanceRequestForm(forms.Form):
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
            "invalid": "Pemeriksaan belum tersedia. Tekan Periksa sumber kembali.",
        },
    )
    reason = forms.CharField(
        label="Alasan pencatatan",
        min_length=3,
        max_length=4000,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    evidence = forms.CharField(
        label="Dokumen dan hasil pemeriksaan",
        min_length=3,
        max_length=4000,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Tulis nama atau nomor dokumen asli dan hasil pencocokan yang mendukung jumlah ini.",
    )
    confirmed = forms.BooleanField(
        label="Saya telah memeriksa pihak, tanggal, jumlah, dan dokumen asli."
    )

    def __init__(self, data=None, *, organization, checking=False, **kwargs):
        self.organization = organization
        initial = {"request_key": str(uuid4())}
        initial.update(kwargs.pop("initial", {}))
        super().__init__(data, initial=initial, **kwargs)
        for name in ("request_key", "expected_fingerprint", "confirmed"):
            self.fields[name].required = not checking
        if checking:
            for name in ("reason", "evidence"):
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


class OpeningBalanceRecordForm(OpeningBalanceRequestForm):
    kind = forms.ChoiceField(
        choices=OpeningBalanceItem.Kind.choices, widget=forms.HiddenInput
    )
    party = forms.ModelChoiceField(
        label="Pelanggan / mitra", queryset=Party.objects.none()
    )
    date = BusinessDateField(
        label="Tanggal mulai pembukuan",
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
        help_text="Semua piutang dan utang lama menggunakan satu tanggal mulai pembukuan.",
    )
    source_date = BusinessDateField(
        label="Tanggal dokumen asli",
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
        help_text="Tanggal pada invoice atau tagihan lama, paling lambat tanggal mulai pembukuan.",
    )
    source_reference = forms.CharField(
        label="Nomor invoice / tagihan lama",
        max_length=160,
        help_text="Salin nomor dokumen asli agar dokumen yang sama tidak dicatat dua kali.",
    )
    amount = forms.DecimalField(
        label="Sisa yang belum dibayar saat mulai pembukuan (Rp)",
        min_value=Decimal("0.01"),
        max_digits=20,
        decimal_places=2,
        help_text="Isi sisa yang benar-benar masih terutang, bukan nilai penuh dokumen yang sudah dibayar sebagian. "
        "Contoh: 500000 untuk Rp500.000.",
    )

    def __init__(self, *args, kind="receivable", common_date=None, **kwargs):
        initial = {"kind": kind}
        if common_date is not None:
            initial["date"] = common_date
        initial.update(kwargs.pop("initial", {}))
        super().__init__(*args, initial=initial, **kwargs)
        selected_kind = (
            self.data.get("kind") if self.is_bound else self.initial.get("kind")
        )
        parties = Party.objects.filter(organization=self.organization)
        if selected_kind == "payable":
            parties = parties.filter(kind="supplier")
            self.fields["party"].label = "Pemasok"
        elif selected_kind == "receivable":
            parties = parties.filter(kind__in=("customer", "reseller"))
        else:
            parties = parties.none()
        self.fields["party"].queryset = parties.order_by("name", "pk")
        if common_date is not None:
            self.fields["date"].widget.attrs["readonly"] = True
            self.fields[
                "date"
            ].help_text = (
                "Tanggal ini mengikuti saldo lama yang sudah dicatat untuk perusahaan."
            )
        self.order_fields(
            [
                "request_key",
                "expected_fingerprint",
                "kind",
                "party",
                "date",
                "source_date",
                "source_reference",
                "amount",
                "reason",
                "evidence",
                "confirmed",
            ]
        )

    def clean(self):
        values = super().clean()
        cutoff, original = values.get("date"), values.get("source_date")
        if cutoff and cutoff > timezone.localdate():
            self.add_error("date", "Tanggal mulai pembukuan tidak boleh di masa depan.")
        if original and cutoff and original > cutoff:
            self.add_error(
                "source_date",
                "Tanggal dokumen asli tidak boleh setelah tanggal mulai pembukuan.",
            )
        return values


class OpeningBalanceMatchForm(OpeningBalanceRequestForm):
    transaction = BankSourceField(
        label="Uang masuk dari pelanggan / mitra",
        queryset=BankTransaction.objects.none(),
    )
    amount = forms.DecimalField(
        label="Jumlah yang dicocokkan (Rp)",
        min_value=Decimal("0.01"),
        max_digits=20,
        decimal_places=2,
        help_text="Jumlah ini mengurangi sisa lama dan sisa mutasi bank yang dipilih. "
        "Contoh: 500000 untuk Rp500.000.",
    )

    def __init__(self, *args, item, **kwargs):
        super().__init__(*args, **kwargs)
        positive = item.kind == "receivable"
        self.fields["transaction"].queryset = (
            bank_sources(self.organization, self.data, positive=positive)
            .select_related("account")
            .order_by("-date", "-pk")
        )
        self.fields["transaction"].label = (
            "Uang masuk dari pelanggan / mitra"
            if positive
            else "Uang keluar untuk pemasok"
        )
        self.fields[
            "transaction"
        ].help_text = "Pilih mutasi yang sudah terjadi. Tanggal pencocokan mengikuti tanggal asli bank."
        if not self.fields["confirmed"].required:
            self.fields["amount"].required = False
        self.fields[
            "confirmed"
        ].label = (
            "Saya telah memeriksa pihak, bukti bank, tanggal, jumlah, dan dokumen lama."
        )
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


class OpeningBalanceCorrectionForm(OpeningBalanceRequestForm):
    direction = forms.ChoiceField(
        label="Apa yang perlu diperbaiki?",
        choices=(("increase", "Tambah"), ("decrease", "Kurangi")),
        widget=forms.RadioSelect,
        help_text="Pilih Tambah jika jumlah tercatat terlalu kecil, atau Kurangi jika terlalu besar.",
    )
    date = BusinessDateField(
        label="Tanggal perbaikan mulai berlaku",
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
        help_text="Pilih tanggal pembukuan yang masih terbuka, mulai tanggal saldo lama sampai hari ini.",
    )
    amount = forms.DecimalField(
        label="Jumlah yang ditambah / dikurangi (Rp)",
        min_value=Decimal("0.01"),
        max_digits=20,
        decimal_places=2,
        help_text="Tulis jumlah perbaikannya, bukan jumlah akhir. Contoh: 500000 untuk Rp500.000. "
        "Tidak perlu menulis tanda tambah atau kurang.",
    )

    def __init__(self, *args, item, **kwargs):
        self.item = item
        super().__init__(*args, **kwargs)
        self.fields["reason"].label = "Alasan perbaikan jumlah"
        self.fields[
            "evidence"
        ].help_text = "Tulis dokumen asli dan hasil pemeriksaan yang membuktikan jumlah perlu diperbaiki."
        self.fields[
            "confirmed"
        ].label = "Saya telah memeriksa dokumen asli, pilihan Tambah / Kurangi, tanggal, dan jumlah perbaikan."
        self.fields["date"].widget.attrs.update(
            {"min": item.date.isoformat(), "max": timezone.localdate().isoformat()}
        )
        self.order_fields(
            [
                "request_key",
                "expected_fingerprint",
                "direction",
                "date",
                "amount",
                "reason",
                "evidence",
                "confirmed",
            ]
        )

    def clean(self):
        values = super().clean()
        day = values.get("date")
        if day and day < self.item.date:
            self.add_error(
                "date",
                "Tanggal perbaikan tidak boleh sebelum tanggal mulai pembukuan saldo lama.",
            )
        if day and day > timezone.localdate():
            self.add_error("date", "Tanggal perbaikan tidak boleh di masa depan.")
        return values


class OpeningBalanceCancelForm(OpeningBalanceRequestForm):
    """Only the explanation is editable; full amount and date come from cash proof."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["reason"].label = "Alasan pembatalan pencocokan"
        self.fields[
            "reason"
        ].help_text = (
            "Jelaskan mengapa mutasi bank ini dicocokkan ke dokumen yang keliru."
        )
        self.fields[
            "evidence"
        ].help_text = "Tulis rekening koran dan dokumen yang sudah diperiksa untuk membuktikan pencocokan ini keliru."
        self.fields[
            "confirmed"
        ].label = "Saya telah memeriksa pembatalan seluruh pencocokan ini. Mutasi bank asli tetap ada dan perlu dicocokkan kembali."
        self.order_fields(
            ["request_key", "expected_fingerprint", "reason", "evidence", "confirmed"]
        )


class OpeningBalanceRefundForm(OpeningBalanceRequestForm):
    transaction = BankSourceField(
        label="Uang keluar untuk pengembalian pelanggan / mitra",
        queryset=BankTransaction.objects.none(),
    )
    amount = forms.DecimalField(
        label="Jumlah pengembalian yang dicocokkan (Rp)",
        min_value=Decimal("0.01"),
        max_digits=20,
        decimal_places=2,
        help_text="Isi jumlah uang yang benar-benar dikembalikan dari pembayaran asli. Contoh: 50000 untuk Rp50.000.",
    )

    def __init__(self, *args, item, **kwargs):
        super().__init__(*args, **kwargs)
        self.bank_source_errors = []
        positive = item.kind == "payable"
        try:
            self.fields["transaction"].queryset = (
                bank_sources(self.organization, self.data, positive=positive)
                .select_related("account")
                .order_by("-date", "-pk")
            )
        except ValidationError as error:
            self.bank_source_errors = error.messages
        self.fields["transaction"].label = (
            "Uang masuk dari pengembalian pemasok"
            if positive
            else "Uang keluar untuk pengembalian pelanggan / mitra"
        )
        self.fields[
            "transaction"
        ].help_text = "Pilih mutasi pengembalian yang sudah terjadi. Tanggal mengikuti mutasi bank ini dan tidak dapat diubah."
        self.fields["reason"].label = "Alasan pengembalian uang"
        self.fields[
            "evidence"
        ].help_text = "Tulis bukti pembayaran asli dan bukti pengembalian, beserta hasil pemeriksaan jumlahnya."
        self.fields[
            "confirmed"
        ].label = "Saya telah memeriksa pembayaran asli, mutasi pengembalian, tanggal bank, jumlah, dan bukti."
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

    def clean(self):
        values = super().clean()
        if self.bank_source_errors:
            raise ValidationError(
                "Mutasi bank belum dapat diperiksa. Minta Owner memeriksa sumber dan riwayat bank sebelum melanjutkan."
            )
        return values
