"""Immutable Finance attestations of bank statement period completeness."""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from .models import ScopedModel


class StatementPeriodReview(ScopedModel):
    account = models.ForeignKey(
        "finance.BankAccount",
        on_delete=models.PROTECT,
        related_name="statement_reviews",
    )
    statement = models.ForeignKey(
        "webapp.StatementImport",
        on_delete=models.PROTECT,
        related_name="period_reviews",
    )
    period_start = models.DateField()
    period_end = models.DateField()
    opening_balance = models.DecimalField(max_digits=20, decimal_places=2)
    closing_balance = models.DecimalField(max_digits=20, decimal_places=2)
    total_in = models.DecimalField(max_digits=20, decimal_places=2)
    total_out = models.DecimalField(max_digits=20, decimal_places=2)
    transaction_count = models.PositiveIntegerField()
    source_order = models.CharField(
        max_length=12,
        choices=[("ascending", "Lama ke baru"), ("descending", "Baru ke lama")],
        default="ascending",
    )
    evidence = models.TextField(max_length=4000)
    fingerprint = models.CharField(max_length=64)
    snapshot = models.JSONField()
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    supersedes = models.OneToOneField(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="replacement",
    )
    scoped_references = ("account", "statement", "supersedes")

    class Meta:
        ordering = ["-period_end", "-pk"]
        indexes = [
            models.Index(
                fields=["organization", "account", "period_start", "period_end"],
                name="finance_stmt_review_period",
            )
        ]

    def save(self, *args, **kwargs):
        if not getattr(self, "_statement_review_write", False):
            raise ValidationError(
                "Gunakan pemeriksaan cakupan e-statement untuk menyimpan."
            )
        try:
            return super().save(*args, **kwargs)
        finally:
            self.__dict__.pop("_statement_review_write", None)

    def clean(self):
        super().clean()
        if self._old():
            raise ValidationError(
                "Pemeriksaan e-statement tidak ditimpa. Buat pemeriksaan pengganti."
            )
        if self.statement_id and self.statement.account_id != self.account_id:
            raise ValidationError(
                "File e-statement harus berasal dari rekening yang diperiksa."
            )
        if self.supersedes_id and (
            self.supersedes.account_id != self.account_id
            or self.supersedes.period_start != self.period_start
            or self.supersedes.period_end != self.period_end
        ):
            raise ValidationError(
                "Pemeriksaan pengganti harus menunjuk rekening dan periode yang sama."
            )

    def delete(self, *args, **kwargs):
        raise ValidationError("Pemeriksaan e-statement disimpan sebagai jejak audit.")

    def __str__(self):
        return f"{self.account.name} · {self.period_start:%d/%m/%Y}–{self.period_end:%d/%m/%Y} · #{self.pk}"
