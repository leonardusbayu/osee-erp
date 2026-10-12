from django import forms

from .forms import ProfessionalReviewForm
from .opening_refund_review import SCHEMA


class OpeningRefundReviewForm(ProfessionalReviewForm):
    resolution = forms.ChoiceField(
        label="Kesimpulan untuk pengembalian uang",
        choices=[
            ("", "Pilih sesuai laporan profesional"),
            ("principal_only", "Laporan menyimpulkan hanya pengembalian pokok"),
            ("more_evidence", "Laporan meminta bukti tambahan"),
            ("unsupported_treatment", "Perlakuan memerlukan pemeriksaan lanjutan"),
        ],
    )
    gross_amount = forms.DecimalField(
        label="Nilai bruto yang diperiksa (Rp)",
        required=False,
        min_value=0,
        max_digits=20,
        decimal_places=2,
        help_text="Kosongkan jika laporan belum menentukan nilai bruto. Nominal bank tidak mengisi nilai ini secara otomatis.",
    )
    withholding_amount = forms.DecimalField(
        label="Nominal pemotongan baru untuk pengembalian ini (Rp)",
        required=False,
        min_value=0,
        max_digits=20,
        decimal_places=2,
        help_text="Isi 0 hanya jika laporan profesional secara jelas menyimpulkan nol. Kolom kosong tetap belum ditentukan.",
    )
    legal_basis_reference = forms.CharField(
        label="Referensi dasar kesimpulan pada laporan",
        max_length=4000,
        widget=forms.Textarea(attrs={"rows": 2}),
    )
    professional_instruction = forms.CharField(
        label="Instruksi profesional untuk pengembalian ini",
        max_length=4000,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    withholding_recovery_requested = forms.ChoiceField(
        label="Apakah laporan meminta pemulihan pemotongan pajak?",
        required=False,
        choices=[
            ("", "Belum dijelaskan dalam laporan"),
            ("no", "Laporan menyatakan tidak diminta"),
            ("yes", "Laporan meminta pemulihan"),
        ],
    )
    tax_credit_restitution_requested = forms.ChoiceField(
        label="Apakah laporan meminta pengembalian kredit pajak?",
        required=False,
        choices=[
            ("", "Belum dijelaskan dalam laporan"),
            ("no", "Laporan menyatakan tidak diminta"),
            ("yes", "Laporan meminta pengembalian"),
        ],
    )
    prior_period_income_correction_requested = forms.ChoiceField(
        label="Apakah laporan meminta pembetulan penghasilan bulan sebelumnya?",
        required=False,
        choices=[
            ("", "Belum dijelaskan dalam laporan"),
            ("no", "Laporan menyatakan tidak diminta"),
            ("yes", "Laporan meminta pembetulan"),
        ],
    )
    source_fingerprint = forms.RegexField(
        regex=r"^[0-9a-f]{64}$", widget=forms.HiddenInput
    )
    expected_version = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    expected_fingerprint = forms.RegexField(
        regex=r"^[0-9a-f]{64}$", widget=forms.HiddenInput
    )
    request_key = forms.UUIDField(widget=forms.HiddenInput)

    def __init__(self, *args, organization, context, **kwargs):
        self.review_context = context
        super().__init__(*args, organization=organization, **kwargs)

    def decision(self):
        data = self.cleaned_data
        refund = self.review_context["refund"]
        return {
            "schema": SCHEMA,
            "version": 1,
            "scope": "refund_cash_event",
            "source_fingerprint": data["source_fingerprint"],
            "resolution": data["resolution"],
            "reviewed_facts": {
                "cash_amount": format(refund.amount, ".2f"),
                "payment_date": refund.date.isoformat(),
                "contract_reference": refund.item.source_reference,
                "gross_amount": format(data["gross_amount"], ".2f")
                if data.get("gross_amount") is not None
                else None,
                "withholding_amount": format(data["withholding_amount"], ".2f")
                if data.get("withholding_amount") is not None
                else None,
                "withholding_recovery_requested": {"no": False, "yes": True}.get(
                    data["withholding_recovery_requested"]
                ),
                "tax_credit_restitution_requested": {"no": False, "yes": True}.get(
                    data["tax_credit_restitution_requested"]
                ),
                "prior_period_income_correction_requested": {
                    "no": False,
                    "yes": True,
                }.get(data["prior_period_income_correction_requested"]),
                "legal_basis_reference": data["legal_basis_reference"],
                "professional_instruction": data["professional_instruction"],
            },
        }
