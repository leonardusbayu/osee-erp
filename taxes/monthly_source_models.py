"""Immutable monthly source snapshots and separately documented fiscal decisions."""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from .models import ImmutableTaxRecord


class MonthlyTaxSourcePackage(ImmutableTaxRecord):
    organization = models.ForeignKey("core.Organization", on_delete=models.PROTECT)
    period = models.DateField()
    cutoff = models.DateField()
    revision = models.PositiveIntegerField()
    previous = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT
    )
    snapshot = models.JSONField()
    source_hash = models.CharField(max_length=64)
    profile_snapshot = models.JSONField()
    legal_sources = models.JSONField()
    package_hash = models.CharField(max_length=64)
    extraction_version = models.CharField(max_length=80)
    prepared_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-period", "-revision"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "period", "revision"],
                name="tax_monthly_package_revision",
            )
        ]

    def clean(self):
        super().clean()
        if (
            not self.period
            or self.period.day != 1
            or not self.cutoff
            or self.cutoff < self.period
        ):
            raise ValidationError(
                "Periode paket harus dimulai pada tanggal pertama bulan."
            )
        if self.previous_id and (
            self.previous.organization_id != self.organization_id
            or self.previous.period != self.period
        ):
            raise ValidationError(
                "Versi sebelumnya harus berasal dari perusahaan dan masa yang sama."
            )
        if (
            not isinstance(self.snapshot, dict)
            or self.snapshot.get("source_hash") != self.source_hash
            or self.snapshot.get("organization_id") != self.organization_id
        ):
            raise ValidationError(
                "Sumber paket tidak cocok dengan perusahaan atau identitas sumber."
            )

    @property
    def fiscal_history(self):
        return self.profile_snapshot.get("fiscal_history", [])


class MonthlyFiscalApproval(ImmutableTaxRecord):
    organization = models.ForeignKey("core.Organization", on_delete=models.PROTECT)
    package = models.OneToOneField(
        MonthlyTaxSourcePackage, on_delete=models.PROTECT, related_name="approval"
    )
    external_review = models.OneToOneField(
        "taxes.ExternalTaxReview",
        on_delete=models.PROTECT,
        related_name="monthly_approval",
    )
    decision = models.JSONField()
    decision_hash = models.CharField(max_length=64)
    result = models.JSONField()
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    def clean(self):
        super().clean()
        from core.models import Membership

        if not Membership.objects.filter(
            organization_id=self.organization_id,
            user_id=self.actor_id,
            role="owner",
            user__is_active=True,
        ).exists():
            raise ValidationError(
                "Persetujuan paket harus dicatat oleh Owner aktif perusahaan ini."
            )
