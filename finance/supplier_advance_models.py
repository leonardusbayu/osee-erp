"""Immutable supplier cash decisions; balances are derived, never cached."""

import hashlib
import json
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models

from .models import FinancialEvent

VERSION = "supplier-advance-v1"


class SupplierAdvanceEvent(FinancialEvent):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    amount = models.DecimalField(
        max_digits=20, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))]
    )
    source_reference = models.CharField(max_length=160, blank=True)
    reason = models.TextField(max_length=4000)
    request_key = models.UUIDField()
    request_payload = models.JSONField()
    source_snapshot = models.JSONField()
    source_hash = models.CharField(max_length=64)
    workflow_version = models.CharField(max_length=40, default=VERSION)
    action = ""

    class Meta:
        abstract = True

    def clean(self):
        super().clean()
        self._unchanged(
            self._old(),
            tuple(
                field.attname
                for field in self._meta.concrete_fields
                if field.name != "id"
            ),
        )
        if (
            self.workflow_version != VERSION
            or not isinstance(self.request_payload, dict)
            or not isinstance(self.source_snapshot, dict)
        ):
            raise ValidationError("Bukti keputusan uang muka pemasok tidak lengkap.")
        if (
            self.request_payload.get("action") != self.action
            or self.request_payload.get("actor_id") != self.actor_id
        ):
            raise ValidationError(
                "Pelaku atau jenis permintaan tidak sesuai keputusan asli."
            )
        if self.action == "record" and not self.source_reference.strip():
            raise ValidationError("Nomor pesanan atau dokumen uang muka wajib dicatat.")
        digest = hashlib.sha256(
            json.dumps(
                self.source_snapshot,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        if self.source_hash != digest:
            raise ValidationError("Hash sumber uang muka pemasok tidak cocok.")


class SupplierAdvance(SupplierAdvanceEvent):
    supplier = models.ForeignKey(
        "finance.Party", on_delete=models.PROTECT, related_name="supplier_advances"
    )
    bank_posting = models.OneToOneField(
        "finance.BankPosting", on_delete=models.PROTECT, related_name="supplier_advance"
    )
    purpose = models.CharField(max_length=300, blank=True)
    scoped_references = ("journal", "supplier", "bank_posting")
    action = "record"

    class Meta:
        ordering = ["-date", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "request_key"],
                name="fin_supplier_advance_request",
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gt=0), name="fin_supplier_advance_positive"
            ),
        ]


class SupplierAdvanceApplication(SupplierAdvanceEvent):
    advance = models.ForeignKey(
        SupplierAdvance, on_delete=models.PROTECT, related_name="applications"
    )
    bill = models.ForeignKey(
        "finance.Bill",
        on_delete=models.PROTECT,
        related_name="supplier_advance_applications",
    )
    scoped_references = ("journal", "advance", "bill")
    action = "apply"

    class Meta:
        ordering = ["date", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "request_key"],
                name="fin_supplier_apply_request",
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gt=0), name="fin_supplier_apply_positive"
            ),
        ]


class SupplierAdvanceRefund(SupplierAdvanceEvent):
    advance = models.ForeignKey(
        SupplierAdvance, on_delete=models.PROTECT, related_name="refunds"
    )
    bank_posting = models.OneToOneField(
        "finance.BankPosting",
        on_delete=models.PROTECT,
        related_name="supplier_advance_refund",
    )
    scoped_references = ("journal", "advance", "bank_posting")
    action = "refund"

    class Meta:
        ordering = ["date", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "request_key"],
                name="fin_supplier_refund_request",
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gt=0), name="fin_supplier_refund_positive"
            ),
        ]


class SupplierAdvanceApplicationReversal(SupplierAdvanceEvent):
    application = models.OneToOneField(
        SupplierAdvanceApplication, on_delete=models.PROTECT, related_name="reversal"
    )
    scoped_references = ("journal", "application")
    action = "reverse"

    class Meta:
        ordering = ["date", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "request_key"],
                name="fin_supplier_reverse_request",
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gt=0), name="fin_supplier_reverse_positive"
            ),
        ]
