from django import forms

from .models import MAX_UPLOAD_BYTES
from .services import ALLOWED_EXTENSIONS


class AttachmentForm(forms.Form):
    file = forms.FileField(label="Dokumen pendukung", help_text="PDF, PNG, atau JPEG; maksimal 10 MB. Dokumen disimpan privat dan tidak dikirim ke layanan AI.", widget=forms.ClearableFileInput(attrs={"accept": ".pdf,.png,.jpg,.jpeg"}))

    def clean_file(self):
        upload = self.cleaned_data["file"]
        if upload.size > MAX_UPLOAD_BYTES:
            raise forms.ValidationError("Ukuran berkas maksimal 10 MB.")
        if not any(upload.name.lower().endswith(suffix) for suffix in ALLOWED_EXTENSIONS):
            raise forms.ValidationError("Gunakan dokumen PDF, PNG, atau JPEG.")
        return upload
