"""Private bank-statement intake before financial transaction confirmation."""
import uuid
from django.conf import settings
from django.db import models
from django.core.exceptions import ValidationError
from evidence.models import PrivateEvidenceStorage
from finance.models import ValidatedQuerySet


def statement_path(instance, filename):
    suffix = filename.rsplit(".", 1)[-1].lower()
    return f"organizations/{instance.organization_id}/statements/{uuid.uuid4().hex}.{suffix}"


class StatementImport(models.Model):
    objects = ValidatedQuerySet.as_manager()
    organization = models.ForeignKey("core.Organization", on_delete=models.PROTECT)
    account = models.ForeignKey("finance.BankAccount", on_delete=models.PROTECT)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    file = models.FileField(storage=PrivateEvidenceStorage(file_permissions_mode=0o600, directory_permissions_mode=0o700), upload_to=statement_path)
    original_name = models.CharField(max_length=240)
    sha256 = models.CharField(max_length=64)
    headers = models.JSONField(default=list)
    rows = models.JSONField(default=list)
    mapping = models.JSONField(default=dict)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="confirmed_bank_imports")
    result = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "account", "sha256"], name="unique_statement_file_account")]

    def save(self, *args, **kwargs):
        if self.account.organization_id != self.organization_id:
            raise ValidationError("Rekening berasal dari perusahaan lain.")
        if self.pk:
            old = type(self).objects.get(pk=self.pk)
            immutable = ["organization_id", "account_id", "created_by_id", "file", "original_name", "sha256", "headers", "rows"]
            if any(getattr(old, name) != getattr(self, name) for name in immutable) or old.confirmed_at:
                raise ValidationError("Sumber impor dan konfirmasi yang sudah disimpan tidak dapat diubah.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Sumber e-statement disimpan untuk jejak audit.")
