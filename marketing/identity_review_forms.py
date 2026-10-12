"""Optional explicit source review on existing link/attribution forms."""

from django import forms


class LegacyIdentityReviewForm(forms.Form):
    expected_fingerprint = forms.RegexField(
        r"^[0-9a-f]{64}$", required=False, widget=forms.HiddenInput
    )
    reason = forms.CharField(
        label="Alasan pemeriksaan dan pengaitan",
        required=False,
        max_length=2000,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    evidence = forms.CharField(
        label="Bukti sumber yang diperiksa",
        required=False,
        max_length=4000,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Referensi pendaftaran, invoice, atau lokasi arsip yang sudah dibandingkan dengan riwayat lama.",
    )
    confirm_legacy_source = forms.BooleanField(
        required=False,
        label="Saya sudah membandingkan riwayat lama dengan dokumen sumber dan menyetujui hasil pemeriksaan ini",
    )

    def __init__(self, *args, recovery=None, **kwargs):
        self.identity_recovery = recovery or {}
        super().__init__(*args, **kwargs)
        required = self.identity_recovery.get("legacy_review_required", False)
        for name in ("expected_fingerprint", "evidence", "confirm_legacy_source"):
            self.fields[name].required = required
            if not required:
                self.fields[name].widget = forms.HiddenInput()
        self.fields["reason"].required = required or self.fields["reason"].required
        if not required and not self.fields["reason"].required:
            self.fields["reason"].widget = forms.HiddenInput()

    def clean(self):
        values = super().clean()
        if hasattr(self.data, "getlist") and any(
            len(self.data.getlist(name)) > 1
            for name in (*self.fields, "expected_version", "operation", "review_action")
        ):
            raise forms.ValidationError(
                "Ada isian yang terkirim lebih dari sekali. Periksa formulir sebelum menyimpan."
            )
        return values
