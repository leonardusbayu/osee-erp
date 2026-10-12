"""Month selection and document-led review; no retyped monthly tax total."""

from django import forms
from django.forms import BaseFormSet, formset_factory

from .forms import ProfessionalReviewForm
from .models import TaxEvidence
from .monthly_source_workflow import MAX_MONTHLY_ADJUSTMENTS
from .services import parse_period


class MonthForm(forms.Form):
    period = forms.RegexField(
        label="Bulan yang ingin disiapkan",
        regex=r"^\d{4}-\d{2}$",
        widget=forms.TextInput(attrs={"type": "month"}),
    )

    def clean_period(self):
        return parse_period(self.cleaned_data["period"])


class SourceReviewForm(ProfessionalReviewForm):
    expected_fingerprint = forms.CharField(max_length=64, widget=forms.HiddenInput())
    expected_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput())
    positive_reason = forms.CharField(
        label="Dasar perlakuan transaksi positif pada laporan",
        required=False,
        max_length=1000,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text="Dapat dipakai bersama untuk transaksi positif yang memang mendapat perlakuan sama. Pengecualian tetap dijelaskan pada transaksinya.",
    )
    proposed_confirmed = forms.BooleanField(
        required=False,
        label="Saya mencocokkan seluruh transaksi positif yang diajukan masuk dengan laporan pemeriksa.",
    )
    completeness_confirmed = forms.BooleanField(
        label="Laporan memastikan sumber bulan ini lengkap, termasuk penjualan yang sebelumnya belum masuk ERP."
    )
    inventory_note = forms.CharField(
        label="Sumber yang telah dicocokkan",
        min_length=10,
        max_length=4000,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Sebutkan produk, mitra/pelanggan langsung, kanal, cabang dan dokumen pembanding yang diperiksa.",
    )
    nil_confirmed = forms.BooleanField(
        required=False,
        label="Laporan memastikan dasar pajak benar-benar nihil (hanya jika hasil pemeriksaan nol).",
    )
    self_payment_only_confirmed = forms.BooleanField(
        required=False,
        label="Laporan memastikan seluruh pajak final bulan ini melalui setor sendiri; tidak ada pemotongan final oleh pelanggan.",
    )
    customer_final_withholding = forms.BooleanField(
        required=False,
        label="Ada atau belum pasti ada pajak final yang dipotong pelanggan.",
        help_text="PPh 23 pelanggan berbeda. Jangan kurangi penjualan dengan kredit PPh 23. Pemotongan final oleh pelanggan memerlukan alur terpisah.",
    )
    prior_history_confirmed = forms.BooleanField(
        required=False,
        label="Laporan telah mencocokkan perlakuan pajak bulan sebelumnya agar invoice yang sama tidak dihitung dua kali.",
    )
    object_code = forms.CharField(
        label="Kode objek sesuai laporan (jika pajak final)",
        required=False,
        max_length=40,
    )
    rule_reference = forms.CharField(
        label="Dasar hukum dan perlakuan sesuai laporan", min_length=3, max_length=300
    )

    def __init__(self, *args, organization, package, **kwargs):
        super().__init__(*args, organization=organization, **kwargs)
        self.package = package
        self.rows = package.snapshot.get("rows", [])
        self.fields["evidence"].label_from_instance = lambda row: row.original_filename
        for index, row in enumerate(self.rows):
            prefix = f"source_{index}"
            candidate = row.get("role") == "candidate"
            choices = [("", "Pilih sesuai laporan pemeriksa")]
            choices += (
                [
                    ("include", "Masuk dasar pajak"),
                    ("exclude", "Tidak masuk dasar pajak"),
                ]
                if candidate
                else [
                    ("exclude", "Informasi pendukung; tidak dihitung lagi"),
                    ("adjustment", "Ada penyesuaian sesuai laporan"),
                ]
            )
            if row.get("source_kind") == "opening-refund":
                choices = [
                    ("", "Pilih sesuai laporan pemeriksa"),
                    ("exclude", "Laporan mengecualikan pengembalian pokok ini"),
                ]
            self.fields[prefix + "_decision"] = forms.ChoiceField(
                label="Perlakuan pada bulan ini",
                choices=choices,
                initial="include" if row.get("proposed_inclusion") else "",
                widget=forms.Select(
                    attrs={"data-source-choice": "decision", "data-source-index": index}
                ),
            )
            self.fields[prefix + "_reason"] = forms.CharField(
                label="Alasan sesuai laporan",
                max_length=1000,
                required=False,
                widget=forms.Textarea(
                    attrs={
                        "rows": 2,
                        "data-source-choice": "reason",
                        "data-source-index": index,
                    }
                ),
                help_text="Transaksi positif yang masuk boleh memakai dasar bersama di atas. Selain itu, jelaskan alasan transaksi ini.",
            )

    def source_fields(self):
        return [
            {
                "row": row,
                "decision": self[f"source_{index}_decision"],
                "reason": self[f"source_{index}_reason"],
            }
            for index, row in enumerate(self.rows)
        ]

    def clean(self):
        data = super().clean()
        decisions = []
        for index, row in enumerate(self.rows):
            key = f"source_{index}"
            choice = data.get(key + "_decision")
            reason = data.get(key + "_reason", "").strip()
            shared = (
                row.get("proposed_inclusion") and choice == "include" and not reason
            )
            if shared:
                reason = data.get("positive_reason", "").strip()
                if not data.get("proposed_confirmed"):
                    self.add_error(
                        "proposed_confirmed",
                        "Cocokkan transaksi yang diajukan masuk dengan laporan sebelum menyimpan.",
                    )
            if choice and len(reason) < 3:
                self.add_error(
                    key + "_reason",
                    "Jelaskan alasan sesuai laporan; minimal 3 karakter.",
                )
            if choice:
                decisions.append(
                    {"row_id": row["row_id"], "decision": choice, "reason": reason}
                )
        data["rows"] = decisions
        return data


class AdjustmentForm(forms.Form):
    kind = forms.ChoiceField(
        label="Jenis penyesuaian",
        required=False,
        choices=[
            ("", "Pilih jika ada penyesuaian"),
            ("timing", "Perbedaan waktu pengakuan"),
            ("gross_basis", "Perbedaan nilai kontrak / bruto"),
            ("correction", "Koreksi sesuai laporan"),
        ],
        help_text="Untuk waktu pengakuan, isi tambahan penghasilan fiskal yang belum pernah dihitung. Untuk bruto/koreksi, isi selisih terhadap transaksi yang dimasukkan.",
    )
    amount = forms.DecimalField(
        label="Jumlah penyesuaian saja (Rp)",
        required=False,
        max_digits=18,
        decimal_places=2,
        help_text="Isi selisihnya, bukan total bulanan. Gunakan nilai negatif untuk pengurangan.",
    )
    reason = forms.CharField(
        label="Alasan penyesuaian",
        required=False,
        max_length=2000,
        widget=forms.Textarea(attrs={"rows": 2}),
    )
    evidence = forms.ModelChoiceField(
        label="PDF pendukung penyesuaian",
        required=False,
        queryset=TaxEvidence.objects.none(),
    )
    source_row_ids = forms.MultipleChoiceField(
        label="Transaksi yang mendasari penyesuaian",
        required=False,
        choices=[],
        widget=forms.CheckboxSelectMultiple(),
    )

    def __init__(self, *args, organization, rows, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["evidence"].queryset = (
            TaxEvidence.objects.filter(organization=organization)
            .defer("content")
            .order_by("-pk")
        )
        self.fields["evidence"].label_from_instance = lambda row: row.original_filename
        self.fields["source_row_ids"].choices = [
            (
                row["row_id"],
                f"Transaksi {index + 1} · {row.get('date', '')} · Rp{row.get('amount', '0')}",
            )
            for index, row in enumerate(rows)
            if row.get("source_kind") != "opening-refund"
        ]

    def clean(self):
        data = super().clean()
        if not any(data.get(name) not in (None, "", [], False) for name in self.fields):
            return data
        for name in ("kind", "amount", "reason", "evidence", "source_row_ids"):
            if data.get(name) in (None, "", [], False):
                self.add_error(
                    name, "Lengkapi bagian ini untuk penyesuaian yang dicatat."
                )
        if data.get("amount") == 0:
            self.add_error(
                "amount",
                "Jumlah penyesuaian tidak boleh nol. Kosongkan seluruh baris jika tidak ada penyesuaian.",
            )
        if data.get("reason") and len(data["reason"].strip()) < 3:
            self.add_error("reason", "Jelaskan alasan penyesuaian.")
        return data


class AdjustmentSet(BaseFormSet):
    def add_fields(self, form, index):
        super().add_fields(form, index)
        form.fields["DELETE"].label = "Abaikan penyesuaian ini"


AdjustmentFormSet = formset_factory(
    AdjustmentForm,
    formset=AdjustmentSet,
    extra=0,
    max_num=MAX_MONTHLY_ADJUSTMENTS,
    validate_max=True,
    absolute_max=MAX_MONTHLY_ADJUSTMENTS,
    can_delete=True,
)


class GenerateForm(forms.Form):
    expected_fingerprint = forms.CharField(max_length=64, widget=forms.HiddenInput())
    expected_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput())


class ReviewCorrectionForm(GenerateForm):
    reason = forms.CharField(
        label="Apa yang perlu diperbaiki pada hasil pemeriksaan?",
        min_length=10,
        max_length=2000,
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text="Jelaskan kesalahan dan bagian laporan yang perlu diperiksa kembali. Sumber transaksi tetap tersimpan.",
    )
    confirmed = forms.BooleanField(
        label="Saya akan mencatat laporan pemeriksaan yang benar pada versi baru. Versi ini tetap tersimpan dalam riwayat."
    )
