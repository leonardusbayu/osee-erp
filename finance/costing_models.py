"""Audited allocation evidence; supplier expense is posted only by bill approval."""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from .models import ScopedModel


class CostingRecord(ScopedModel):
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    reason = models.TextField(max_length=4000)
    evidence = models.TextField(max_length=4000)

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not getattr(self, "_costing_write", False):
            raise ValidationError(
                "Gunakan alur biaya dan persetujuan harga untuk menyimpan."
            )
        try:
            return super().save(*args, **kwargs)
        finally:
            self.__dict__.pop("_costing_write", None)

    def clean(self):
        super().clean()
        if self._old():
            raise ValidationError(
                "Catatan biaya/harga tidak ditimpa. Gunakan pembalikan atau permintaan baru."
            )

    def delete(self, *args, **kwargs):
        raise ValidationError(
            "Catatan biaya dan persetujuan harga tidak boleh dihapus."
        )


class CostAllocation(CostingRecord):
    invoice = models.ForeignKey(
        "finance.Invoice", on_delete=models.PROTECT, related_name="cost_allocations"
    )
    bill = models.ForeignKey(
        "finance.Bill", on_delete=models.PROTECT, related_name="cost_allocations"
    )
    date = models.DateField()
    quantity = models.PositiveIntegerField()
    amount = models.DecimalField(max_digits=20, decimal_places=2)
    reference = models.CharField(max_length=120)
    scoped_references = ("invoice", "bill")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "reference"], name="finance_cost_reference"
            )
        ]
        ordering = ["date", "pk"]


class CostReversal(CostingRecord):
    allocation = models.OneToOneField(
        CostAllocation, on_delete=models.PROTECT, related_name="reversal"
    )
    date = models.DateField()
    reference = models.CharField(max_length=120)
    scoped_references = ("allocation",)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "reference"],
                name="finance_cost_reversal_reference",
            )
        ]
        ordering = ["date", "pk"]


class PriceOverrideRequest(CostingRecord):
    party = models.ForeignKey(
        "finance.Party",
        on_delete=models.PROTECT,
        related_name="price_override_requests",
    )
    product = models.ForeignKey("finance.Product", on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()
    date = models.DateField()
    unit_price = models.DecimalField(max_digits=20, decimal_places=2)
    baseline_price = models.DecimalField(max_digits=20, decimal_places=2)
    price_version = models.ForeignKey(
        "finance.PriceVersion", null=True, blank=True, on_delete=models.PROTECT
    )
    fingerprint = models.CharField(max_length=64)
    scoped_references = ("party", "product", "price_version")

    class Meta:
        ordering = ["-pk"]

    def __str__(self):
        return f"#{self.pk} · {self.party.name} · {self.product.name} · {self.quantity} unit · Rp{self.unit_price}"


class PriceOverrideDecision(CostingRecord):
    request = models.OneToOneField(
        PriceOverrideRequest, on_delete=models.PROTECT, related_name="decision_record"
    )
    decision = models.CharField(
        max_length=10,
        choices=[("approved", "Disetujui Direktur"), ("rejected", "Ditolak Direktur")],
    )
    scoped_references = ("request",)


class PriceOverrideUse(CostingRecord):
    request = models.OneToOneField(
        PriceOverrideRequest, on_delete=models.PROTECT, related_name="use"
    )
    invoice = models.OneToOneField(
        "finance.Invoice", on_delete=models.PROTECT, related_name="price_override_use"
    )
    scoped_references = ("request", "invoice")
