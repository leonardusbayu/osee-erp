"""Private, immutable source documents; downloads always require membership."""

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.storage import FileSystemStorage
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from django.db.models import Q

from finance.models import ScopedModel


MAX_UPLOAD_BYTES = 10 * 1024 * 1024


class PrivateEvidenceStorage(FileSystemStorage):
    def url(self, name):
        raise NotImplementedError("Dokumen privat hanya tersedia melalui unduhan berizin.")


def evidence_path(instance, filename):
    suffix = {"application/pdf": ".pdf", "image/png": ".png", "image/jpeg": ".jpg"}[instance.content_type]
    return f"organizations/{instance.organization_id}/evidence/{uuid.uuid4().hex}{suffix}"


class Attachment(ScopedModel):
    invoice = models.ForeignKey("finance.Invoice", null=True, blank=True, on_delete=models.PROTECT, related_name="attachments")
    bill = models.ForeignKey("finance.Bill", null=True, blank=True, on_delete=models.PROTECT, related_name="attachments")
    file = models.FileField(storage=PrivateEvidenceStorage(file_permissions_mode=0o600, directory_permissions_mode=0o700), upload_to=evidence_path, max_length=180)
    original_name = models.CharField(max_length=240)
    content_type = models.CharField(max_length=32, choices=[("application/pdf", "PDF"), ("image/png", "PNG"), ("image/jpeg", "JPEG")])
    size = models.PositiveIntegerField(validators=[MinValueValidator(1), MaxValueValidator(MAX_UPLOAD_BYTES)])
    sha256 = models.CharField(max_length=64, validators=[RegexValidator(r"\A[a-f0-9]{64}\Z", "Hash SHA-256 tidak valid.")])
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="uploaded_evidence")
    scoped_references = ("invoice", "bill")

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [
            models.CheckConstraint(condition=(Q(invoice__isnull=False, bill__isnull=True) | Q(invoice__isnull=True, bill__isnull=False)), name="evidence_exactly_one_source"),
            models.UniqueConstraint(fields=["organization", "invoice", "sha256"], name="evidence_invoice_hash"),
            models.UniqueConstraint(fields=["organization", "bill", "sha256"], name="evidence_bill_hash"),
        ]

    def clean(self):
        super().clean()
        if bool(self.invoice_id) == bool(self.bill_id):
            raise ValidationError("Dokumen harus terhubung tepat ke satu invoice atau tagihan pemasok.")
        if not self.pk and not getattr(self, "_service_creation", False):
            raise ValidationError("Gunakan layanan lampiran untuk memvalidasi berkas sumber.")
        self._unchanged(self._old(), ("invoice_id", "bill_id", "file", "original_name", "content_type", "size", "sha256", "uploaded_by_id"))

    def delete(self, *args, **kwargs):
        raise ValidationError("Dokumen sumber disimpan sebagai riwayat dan tidak boleh dihapus.")

    def __str__(self):
        return self.original_name
