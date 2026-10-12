"""Append-only receipt of an independent professional's actual refund conclusion."""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from .models import ImmutableTaxRecord


class OpeningRefundTaxReview(ImmutableTaxRecord):
    organization = models.ForeignKey("core.Organization", on_delete=models.PROTECT)
    refund = models.ForeignKey(
        "finance.OpeningBalanceRefund",
        on_delete=models.PROTECT,
        related_name="tax_reviews",
    )
    previous = models.OneToOneField(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="next_review",
    )
    version = models.PositiveIntegerField()
    external_review = models.OneToOneField(
        "taxes.ExternalTaxReview",
        on_delete=models.PROTECT,
        related_name="opening_refund_decision",
    )
    decision = models.JSONField()
    source_snapshot = models.JSONField()
    source_fingerprint = models.CharField(max_length=64)
    report_snapshot = models.JSONField()
    snapshot_hash = models.CharField(max_length=64)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    request_key = models.UUIDField()
    request_payload = models.JSONField()
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    scoped_references = ("refund", "previous", "external_review")

    class Meta:
        ordering = ["version", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "request_key"], name="tax_refund_review_request"
            ),
            models.UniqueConstraint(
                fields=["refund", "version"], name="tax_refund_review_version"
            ),
            models.CheckConstraint(
                condition=models.Q(version__gte=1), name="tax_refund_review_positive"
            ),
        ]

    def save(self, *args, **kwargs):
        try:
            if not getattr(self, "_opening_refund_review_write", False):
                raise ValidationError(
                    "Gunakan alur penerimaan laporan profesional untuk pengembalian uang saldo awal."
                )
            return super().save(*args, **kwargs)
        finally:
            self.__dict__.pop("_opening_refund_review_write", None)
            self.__dict__.pop("_tax_write", None)

    def clean(self):
        super().clean()
        from .opening_refund_review import (
            validate_new_review_record,
            validate_review_record,
        )

        validate_review_record(self)
        if self._state.adding:
            validate_new_review_record(self)
