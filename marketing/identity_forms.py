"""Staff inputs for a separately reviewed correction of invoice-derived identity."""

from django import forms
from django.db.models import Q

from finance.models import Invoice


class IdentityInvoiceSelect(forms.Select):
    def create_option(
        self, name, value, label, selected, index, subindex=None, attrs=None
    ):
        option = super().create_option(
            name, value, label, selected, index, subindex, attrs
        )
        invoice = getattr(value, "instance", None)
        if invoice is not None:
            option["attrs"].update(
                {
                    "data-party": invoice.party.name,
                    "data-product": invoice.product.name,
                    "data-number": invoice.number,
                }
            )
        return option


class IdentityInvoiceChoice(forms.ModelChoiceField):
    def label_from_instance(self, invoice):
        return f"{invoice.number} · {invoice.party.name} · {invoice.product.name} · {invoice.date:%d/%m/%Y}"


class InvoiceIdentityRecoveryForm(forms.Form):
    expected_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    expected_fingerprint = forms.RegexField(r"^[0-9a-f]{64}$", widget=forms.HiddenInput)
    request_key = forms.CharField(max_length=160, widget=forms.HiddenInput)
    action = forms.ChoiceField(
        label="Apa yang ingin Anda lakukan?",
        choices=[
            ("replace", "Pilih invoice yang benar"),
            ("reset", "Belum tahu invoice yang benar"),
        ],
        widget=forms.RadioSelect,
    )
    target_invoice = IdentityInvoiceChoice(
        label="Invoice yang benar",
        queryset=Invoice.objects.none(),
        widget=IdentityInvoiceSelect,
        required=False,
        empty_label="Pilih invoice…",
        help_text="Periksa nomor, pelanggan, produk, dan tanggal. Pelanggan atau produk yang sudah diketahui sejak pendaftaran tetap dipertahankan.",
    )
    reason = forms.CharField(
        label="Mengapa perlu diperbaiki?",
        max_length=500,
        widget=forms.Textarea(
            attrs={
                "rows": 3,
                "placeholder": "Contoh: invoice pelanggan lain terpilih saat menghubungkan prospek.",
            }
        ),
    )
    evidence = forms.CharField(
        label="Bukti yang sudah Anda periksa",
        max_length=4000,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Tulis referensi pendaftaran, nomor dokumen, atau lokasi arsip yang membuktikan koreksi.",
    )
    confirm_legacy_source = forms.BooleanField(
        required=False,
        label="Saya sudah membandingkan riwayat lama dengan dokumen sumber dan menyetujui pencatatan hasil pemeriksaan ini",
    )
    confirm_identity = forms.BooleanField(
        label="Saya sudah memeriksa perbandingan dan menyetujui perubahan ini"
    )

    def __init__(self, *args, organization, recovery, **kwargs):
        self.recovery = recovery
        super().__init__(*args, **kwargs)
        candidates = recovery.get("candidate_invoices", [])
        candidate_ids = [getattr(row, "pk", row) for row in candidates]
        query = Q(pk__in=candidate_ids)
        # An accepted retry must reach the command even if the current mapping
        # no longer offers that invoice. Scope is retained here; the command
        # authorizes the exact replay or revalidates all fresh target invariants.
        if self.is_bound:
            submitted = self.data.get("target_invoice", "")
            if (
                isinstance(submitted, str)
                and submitted.isascii()
                and submitted.isdecimal()
                and len(submitted) <= 20
            ):
                query |= Q(pk=int(submitted))
        self.fields["target_invoice"].queryset = (
            Invoice.objects.filter(organization=organization)
            .filter(query)
            .select_related("party", "product")
            .order_by("-date", "number", "pk")
        )
        if not recovery.get("legacy_review_required"):
            self.fields["confirm_legacy_source"].widget = forms.HiddenInput()

    def clean(self):
        values = super().clean()
        if hasattr(self.data, "getlist") and any(
            len(self.data.getlist(name)) > 1 for name in (*self.fields, "operation")
        ):
            raise forms.ValidationError(
                "Ada isian yang terkirim lebih dari sekali. Periksa formulir sebelum menyimpan."
            )
        if values.get("action") == "replace" and not values.get("target_invoice"):
            self.add_error(
                "target_invoice",
                "Pilih invoice yang benar, atau pilih ‘Belum tahu invoice yang benar’.",
            )
        if values.get("action") == "reset" and values.get("target_invoice"):
            self.add_error(
                "target_invoice",
                "Kosongkan pilihan invoice untuk mengembalikan data yang belum diketahui.",
            )
        if self.recovery.get("legacy_review_required") and not values.get(
            "confirm_legacy_source"
        ):
            self.add_error(
                "confirm_legacy_source",
                "Periksa dokumen sumber dan riwayat lama terlebih dahulu.",
            )
        return values
