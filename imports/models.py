"""Immutable source observations, deliberately separate from posted accounts."""

import uuid

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from evidence.models import PrivateEvidenceStorage
from finance.models import ScopedModel


class ImmutableSourceRecord(ScopedModel):
    class Meta:
        abstract = True

    def clean(self):
        super().clean()
        if not self.pk and not getattr(self, "_import_creation", False):
            raise ValidationError("Gunakan layanan impor sumber untuk membuat arsip.")
        fields = tuple(field.attname for field in self._meta.concrete_fields if field.name not in {"id", "organization", "created_at"})
        self._unchanged(self._old(), fields)

    def delete(self, *args, **kwargs):
        raise ValidationError("Arsip sumber tidak boleh dihapus atau ditimpa.")


class ImportBatch(ImmutableSourceRecord):
    organization = models.ForeignKey("core.Organization", on_delete=models.PROTECT, related_name="import_batches")
    source_hash = models.CharField(max_length=64)
    bundle_hash = models.CharField(max_length=64)
    status = models.CharField(max_length=20, default="imported", choices=[("imported", "Diimpor sebagai arsip sumber")])
    payload = models.JSONField(default=dict)
    metadata = models.JSONField(default=dict, blank=True)
    supplier_bill = models.ForeignKey("finance.Bill", null=True, blank=True, on_delete=models.PROTECT, related_name="source_imports")
    scoped_references = ("supplier_bill",)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "source_hash"], name="imports_org_source_hash")]
        ordering = ["-created_at", "-pk"]


def source_document_path(instance, filename):
    return f"organizations/{instance.organization_id}/imports/{uuid.uuid4().hex}.pdf"


class SourceDocument(ImmutableSourceRecord):
    batch = models.ForeignKey(ImportBatch, on_delete=models.PROTECT, related_name="source_documents")
    source_id = models.CharField(max_length=160)
    kind = models.CharField(max_length=40)
    original_name = models.CharField(max_length=240)
    sha256 = models.CharField(max_length=64)
    size = models.PositiveIntegerField()
    page_count = models.PositiveIntegerField()
    file = models.FileField(storage=PrivateEvidenceStorage(file_permissions_mode=0o600, directory_permissions_mode=0o700), upload_to=source_document_path, max_length=180)
    scoped_references = ("batch",)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["batch", "source_id"], name="imports_batch_source_id")]
        ordering = ["pk"]


class MonthlySnapshot(ImmutableSourceRecord):
    batch = models.ForeignKey(ImportBatch, on_delete=models.PROTECT, related_name="months")
    source_id = models.CharField(max_length=160)
    source_period = models.CharField(max_length=7)
    year = models.PositiveIntegerField(validators=[MinValueValidator(2000), MaxValueValidator(2200)])
    month = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(12)])
    source_page = models.PositiveIntegerField()
    printed_count = models.PositiveIntegerField(null=True, blank=True)
    printed_amount = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    detail_count = models.PositiveIntegerField(null=True, blank=True)
    coverage = models.CharField(max_length=100)
    payload = models.JSONField(default=dict)
    scoped_references = ("batch",)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "year", "month"], name="imports_org_period")]
        ordering = ["year", "month"]

    @property
    def coverage_label(self):
        return "Data halaman sumber; kelengkapan periode belum terkonfirmasi"


class ImportException(ImmutableSourceRecord):
    batch = models.ForeignKey(ImportBatch, on_delete=models.PROTECT, related_name="exceptions")
    exception_id = models.CharField(max_length=200)
    code = models.CharField(max_length=100)
    severity = models.CharField(max_length=40)
    message = models.TextField()
    month_source_id = models.CharField(max_length=160, blank=True)
    source_locator = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, default="open", choices=[("open", "Perlu ditinjau")])
    payload = models.JSONField(default=dict)
    scoped_references = ("batch",)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["batch", "exception_id"], name="imports_batch_exception")]
        ordering = ["pk"]
