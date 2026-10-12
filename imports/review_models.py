"""Append-only interpretations of imported exceptions; source facts stay immutable."""

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from evidence.models import PrivateEvidenceStorage
from finance.models import ScopedModel


def review_evidence_path(instance, filename):
    suffix = {"application/pdf": ".pdf", "image/png": ".png", "image/jpeg": ".jpg"}[
        instance.content_type
    ]
    return f"organizations/{instance.organization_id}/source-reviews/{uuid.uuid4().hex}{suffix}"


class ImportExceptionReview(ScopedModel):
    exception = models.ForeignKey(
        "imports.ImportException", on_delete=models.PROTECT, related_name="reviews"
    )
    decision = models.CharField(
        max_length=12,
        choices=[
            ("open", "Penugasan / pemeriksaan"),
            ("resolved", "Diselesaikan dengan bukti"),
            ("reopened", "Dibuka kembali"),
        ],
    )
    responsible_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    reason = models.TextField(max_length=4000)
    evidence = models.TextField(max_length=4000)
    correction = models.TextField(max_length=4000, blank=True)
    source_document = models.ForeignKey(
        "imports.SourceDocument",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="exception_reviews",
    )
    file = models.FileField(
        storage=PrivateEvidenceStorage(
            file_permissions_mode=0o600, directory_permissions_mode=0o700
        ),
        upload_to=review_evidence_path,
        max_length=180,
        blank=True,
    )
    original_name = models.CharField(max_length=240, blank=True)
    content_type = models.CharField(max_length=32, blank=True)
    size = models.PositiveIntegerField(default=0)
    sha256 = models.CharField(max_length=64, blank=True)
    snapshot = models.JSONField()
    fingerprint = models.CharField(max_length=64)
    supersedes = models.OneToOneField(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="replacement",
    )
    scoped_references = ("exception", "source_document", "supersedes")

    class Meta:
        ordering = ["-pk"]

    def save(self, *args, **kwargs):
        if not getattr(self, "_exception_review_write", False):
            raise ValidationError(
                "Gunakan pemeriksaan catatan sumber untuk menyimpan keputusan."
            )
        try:
            return super().save(*args, **kwargs)
        finally:
            self.__dict__.pop("_exception_review_write", None)

    def clean(self):
        super().clean()
        if self._old():
            raise ValidationError(
                "Keputusan lama tetap tersimpan; tambahkan keputusan pengganti."
            )
        if self.supersedes_id and self.supersedes.exception_id != self.exception_id:
            raise ValidationError("Pengganti harus menunjuk catatan sumber yang sama.")
        if (
            self.source_document_id
            and self.source_document.batch_id != self.exception.batch_id
        ):
            raise ValidationError(
                "Dokumen sumber harus berasal dari impor yang diperiksa."
            )

    def delete(self, *args, **kwargs):
        raise ValidationError("Riwayat pemeriksaan sumber tidak boleh dihapus.")
