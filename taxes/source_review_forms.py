from django import forms

from .knowledge import pending_source_revisions


class SourcePublicationForm(forms.Form):
    revision = forms.ModelChoiceField(
        queryset=pending_source_revisions(),
        empty_label="Pilih versi sumber yang sudah diperiksa",
        label="Versi sumber",
        help_text="Pilih versi yang cocok dengan dokumen resmi yang Anda periksa.",
    )
    reviewer_name = forms.CharField(
        max_length=200,
        label="Nama pemeriksa",
        help_text="Catat orang yang membandingkan ringkasan dengan sumber resmi.",
    )
    review_reference = forms.CharField(
        min_length=3,
        max_length=500,
        label="Catatan pemeriksaan",
        help_text="Misalnya nomor dokumen, catatan rapat, atau tautan sumber resmi.",
    )
    confirm_reviewed = forms.BooleanField(
        label="Saya sudah memeriksa versi ini terhadap sumber resmi.",
    )

    def __init__(self, *args, revisions=None, **kwargs):
        super().__init__(*args, **kwargs)
        if revisions is not None:
            self.fields["revision"].queryset = revisions

    def clean_reviewer_name(self):
        name = self.cleaned_data["reviewer_name"].strip()
        if len(name) < 3:
            raise forms.ValidationError("Masukkan nama pemeriksa yang jelas.")
        return name

    def clean_review_reference(self):
        reference = self.cleaned_data["review_reference"].strip()
        if len(reference) < 3:
            raise forms.ValidationError("Masukkan catatan pemeriksaan yang jelas.")
        return reference
