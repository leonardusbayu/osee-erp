"""Staff forms for the canonical spend workflow."""

from datetime import date
from decimal import Decimal
from uuid import uuid4

from django import forms
from django.core.exceptions import ValidationError

from finance.models import Bill
from webapp.forms import BusinessDateField
from webapp.templatetags.finance_format import iddate, money

BUCKETS = [
    ("reservation", "Dana dicadangkan"),
    ("open_commitment", "Janji belanja tersisa"),
    ("incurred", "Biaya terjadi"),
]
TRANSFERS = [
    ("reservation:open_commitment", "Dana cadangan menjadi janji belanja"),
    ("reservation:incurred", "Dana cadangan menjadi biaya terjadi"),
    ("open_commitment:incurred", "Janji belanja menjadi biaya terjadi"),
]


def source_date(value):
    if isinstance(value, str):
        try:
            value = date.fromisoformat(value)
        except ValueError:
            return "Belum dapat diperiksa"
    return iddate(value)


class SpendRequestForm(forms.Form):
    expected_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    expected_fingerprint = forms.CharField(max_length=64, widget=forms.HiddenInput)
    reference = forms.CharField(
        max_length=100, initial=lambda: "UI-" + uuid4().hex, widget=forms.HiddenInput
    )
    date = BusinessDateField(
        label="Tanggal catatan",
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
        help_text="Gunakan tanggal kejadian yang didukung dokumen. Tanggal tagihan dan pembayaran Finance tetap mengikuti sumber asli.",
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
        help_text="Tulis nomor/nama dokumen dan hasil pemeriksaan yang mendukung catatan ini.",
    )
    confirmed = forms.BooleanField(
        label="Saya telah memeriksa tanggal, jumlah, dan dokumen yang dipilih."
    )

    def __init__(self, *args, position, **kwargs):
        initial = {
            "expected_version": position["version"],
            "expected_fingerprint": position["fingerprint"],
        }
        initial.update(kwargs.pop("initial", {}))
        super().__init__(*args, initial=initial, **kwargs)

    def clean(self):
        values = super().clean()
        if hasattr(self.data, "getlist") and any(
            len(self.data.getlist(key)) > 1 for key in self.fields
        ):
            raise ValidationError(
                "Ada isian ganda dalam permintaan ini. Periksa kembali dan kirim satu pilihan untuk setiap kolom."
            )
        return values


class SpendAdoptForm(SpendRequestForm):
    pass


class SpendConvertForm(SpendRequestForm):
    transfer = forms.ChoiceField(label="Perubahan yang terjadi", choices=TRANSFERS)
    amount = forms.DecimalField(
        label="Jumlah yang berubah (Rp)",
        min_value=Decimal("0.01"),
        max_digits=20,
        decimal_places=2,
    )

    def clean(self):
        values = super().clean()
        if values.get("transfer"):
            values["from_kind"], values["to_kind"] = values.pop("transfer").split(":")
        return values


class SpendReleaseForm(SpendRequestForm):
    bucket = forms.ChoiceField(
        label="Sisa yang tidak diperlukan lagi", choices=BUCKETS[:2]
    )
    amount = forms.DecimalField(
        label="Jumlah yang dilepaskan (Rp)",
        min_value=Decimal("0.01"),
        max_digits=20,
        decimal_places=2,
    )


class SpendPaymentForm(SpendRequestForm):
    amount = forms.DecimalField(
        label="Pembayaran yang dilaporkan (Rp)",
        min_value=Decimal("0.01"),
        max_digits=20,
        decimal_places=2,
        help_text="Catatan laporan pembayaran. Uang bank terverifikasi tetap berasal dari pencocokan Finance.",
    )


class SpendBillForm(SpendRequestForm):
    expected_bill_fingerprint = forms.CharField(
        max_length=64,
        widget=forms.HiddenInput,
        error_messages={
            "required": "Periksa tagihan terlebih dahulu, lalu simpan hubungan yang sudah diperiksa."
        },
    )
    bill = forms.ModelChoiceField(
        label="Tagihan yang sudah tercatat", queryset=Bill.objects.none()
    )
    # Preserve existing single-source requests; staff use the source amount rows below.
    coverage_source = forms.ChoiceField(required=False, widget=forms.HiddenInput)
    coverage_amount = forms.DecimalField(
        required=False,
        min_value=Decimal(0),
        max_digits=20,
        decimal_places=2,
        widget=forms.HiddenInput,
    )
    no_previous_coverage = forms.BooleanField(
        required=False,
        label="Tagihan ini tidak menggantikan nilai catatan lama.",
        help_text="Centang hanya jika seluruh tagihan merupakan pengeluaran tambahan yang belum diwakili catatan di atas.",
    )

    def __init__(
        self, *args, organization, position, preview=False, repair=False, **kwargs
    ):
        super().__init__(*args, position=position, **kwargs)
        self.repair = repair
        self.fields["expected_bill_fingerprint"].required = not preview
        self.fields["bill"].queryset = Bill.objects.filter(
            organization=organization, supplier__organization=organization
        ).order_by("-date", "-pk")
        self.fields["bill"].label_from_instance = lambda row: (
            f"{row.number} · {row.supplier.name} · {source_date(row.date)} · {money(row.amount)} · {row.get_status_display()}"
        )
        source_rows = (
            position.get("repair_sources", [])
            if repair and not position.get("current")
            else position.get("coverable_sources", [])
        )
        self.sources = {row["key"]: row for row in source_rows}
        self.fields["coverage_source"].choices = [
            ("", "Pilih catatan yang digantikan"),
            ("none", "Tidak menggantikan nilai lama"),
        ] + [
            (
                key,
                f"{dict(BUCKETS).get(row['bucket'], 'Catatan belanja')} · {row['label']} · "
                + (
                    "nilai akan diperiksa saat koreksi"
                    if row.get("requires_check")
                    else f"tersedia {money(row['available'])}"
                ),
            )
            for key, row in self.sources.items()
        ]
        self.coverage_fields = {}
        for key, row in self.sources.items():
            name = f"coverage_{row['kind']}_{row['id']}"
            self.coverage_fields[name] = key
            self.fields[name] = forms.DecimalField(
                required=False,
                min_value=Decimal(0),
                max_digits=20,
                decimal_places=2,
                label=f"{dict(BUCKETS).get(row['bucket'], 'Catatan belanja')} · {row['label']}",
                help_text="Nilai yang digantikan (Rp). "
                + (
                    f"Nilai akan diperiksa saat koreksi; catatan manual {money(row['amount'])}. "
                    if row.get("requires_check")
                    else f"Sisa saat ini {money(row['available'])}. "
                )
                + "Isi 0 atau kosong bila tidak digantikan.",
            )
        self.order_fields(
            [
                "expected_version",
                "expected_fingerprint",
                "reference",
                "expected_bill_fingerprint",
                "coverage_source",
                "coverage_amount",
                "bill",
                *self.coverage_fields,
                "no_previous_coverage",
                "date",
                "reason",
                "evidence",
                "confirmed",
            ]
        )

    def clean(self):
        values = super().clean()
        key, amount = (
            values.pop("coverage_source", None),
            values.pop("coverage_amount", None),
        )
        no_overlap = values.pop("no_previous_coverage", False)
        coverage = [
            {"source_key": source, "amount": values.pop(name)}
            for name, source in self.coverage_fields.items()
            if values.get(name) is not None and values[name] > 0
        ]
        for name in self.coverage_fields:
            values.pop(name, None)
        if any(
            name.startswith("coverage_") and name not in self.fields
            for name in self.data
        ):
            raise ValidationError(
                "Salah satu catatan yang dipilih tidak tersedia pada belanja ini. Muat posisi terbaru dan periksa kembali."
            )
        if key:
            if coverage or no_overlap:
                raise ValidationError(
                    "Pilih satu cara pemeriksaan cakupan. Periksa kembali jumlah pada setiap catatan."
                )
            if amount is None:
                self.add_error(None, "Isi nilai catatan lama yang digantikan.")
                return values
            if key == "none":
                if amount != 0:
                    self.add_error(
                        None, "Isi 0 bila tagihan tidak menggantikan catatan lama."
                    )
            else:
                if amount <= 0:
                    self.add_error(None, "Isi nilai catatan lama yang digantikan.")
                coverage = [{"source_key": key, "amount": amount}]
        elif coverage and no_overlap:
            self.add_error(
                "no_previous_coverage",
                "Hapus centang ini karena ada nilai catatan lama yang digantikan.",
            )
        elif not coverage and not no_overlap and not self.errors:
            self.add_error(
                "no_previous_coverage",
                "Isi jumlah pada catatan yang digantikan, atau konfirmasikan bahwa tidak ada nilai lama yang digantikan.",
            )
        values["coverage"] = coverage
        return values


class SpendMatchForm(SpendRequestForm):
    manual_source_key = forms.ChoiceField(label="Laporan pembayaran yang dicocokkan")
    finance_event_id = forms.ChoiceField(
        label="Pembayaran Finance untuk kejadian yang sama"
    )
    amount = forms.DecimalField(
        label="Jumlah yang merupakan pembayaran sama (Rp)",
        min_value=Decimal("0.01"),
        max_digits=20,
        decimal_places=2,
    )

    def __init__(self, *args, position, **kwargs):
        super().__init__(*args, position=position, **kwargs)
        self.fields["manual_source_key"].choices = [
            ("", "Pilih laporan pembayaran")
        ] + [
            (
                row["key"],
                f"{row['label']} · {source_date(row['date'])} · belum cocok {money(row['unmatched'])}",
            )
            for row in position.get("manual_payment_sources", [])
        ]
        self.fields["finance_event_id"].choices = [
            ("", "Pilih pembayaran yang sudah dicocokkan Finance")
        ] + [
            (
                row["event_id"],
                f"{row['label']} · {source_date(row['date'])} · tersedia {money(row['unmatched'])}",
            )
            for row in position.get("finance_payment_sources", [])
        ]


class SpendMovementCorrectionForm(SpendRequestForm):
    correction = forms.ChoiceField(
        label="Perbaikan yang diperlukan",
        choices=[
            ("reverse", "Tarik catatan yang keliru"),
            ("replace", "Ganti dengan catatan yang benar"),
        ],
    )
    transfer = forms.ChoiceField(
        label="Perubahan yang benar (jika diganti)",
        choices=[("", "Pilih perubahan")] + TRANSFERS,
        required=False,
    )
    bucket = forms.ChoiceField(
        label="Sisa yang dilepaskan (jika diganti)",
        choices=[("", "Pilih sisa")] + BUCKETS[:2],
        required=False,
    )
    amount = forms.DecimalField(
        label="Jumlah yang benar (Rp, jika diganti)",
        min_value=Decimal("0.01"),
        max_digits=20,
        decimal_places=2,
        required=False,
    )

    def __init__(self, *args, movement, **kwargs):
        super().__init__(*args, **kwargs)
        self.movement = movement
        if movement["kind"] != "convert":
            self.fields.pop("transfer")
        if movement["kind"] != "release":
            self.fields.pop("bucket")

    def clean(self):
        values = super().clean()
        mode = values.pop("correction", None)
        amount, transfer, bucket = (
            values.pop("amount", None),
            values.pop("transfer", None),
            values.pop("bucket", None),
        )
        values["replacement"] = None
        if mode == "replace":
            if amount is None:
                self.add_error("amount", "Isi jumlah untuk catatan pengganti.")
            replacement = {"amount": amount}
            if self.movement["kind"] == "convert":
                if not transfer:
                    self.add_error("transfer", "Pilih perubahan yang benar.")
                else:
                    replacement["from_kind"], replacement["to_kind"] = transfer.split(
                        ":"
                    )
            elif self.movement["kind"] == "release":
                if not bucket:
                    self.add_error("bucket", "Pilih sisa yang dilepaskan.")
                replacement["bucket"] = bucket
            values["replacement"] = replacement
        return values


class BillSpendSelectionForm(forms.Form):
    spend = forms.ChoiceField(label="Belanja yang terkait dengan tagihan ini")

    def __init__(self, *args, choices, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["spend"].choices = [
            ("", "Pilih belanja pada anggaran disetujui")
        ] + choices
