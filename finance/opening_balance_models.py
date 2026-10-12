"""Evidenced legacy principal and actual cash; never fabricated sales or costs."""

import hashlib
import json
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models

from .models import FinancialEvent, normalize_supplier_reference

VERSION = "opening-balance-v1"


class OpeningBalanceEvent(FinancialEvent):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    amount = models.DecimalField(
        max_digits=20, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))]
    )
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
            raise ValidationError("Bukti keputusan saldo awal tidak lengkap.")
        if (
            self.request_payload.get("action") != self.action
            or self.request_payload.get("actor_id") != self.actor_id
        ):
            raise ValidationError(
                "Pelaku atau jenis keputusan tidak sesuai bukti saldo awal."
            )
        digest = hashlib.sha256(
            json.dumps(
                self.source_snapshot,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        if digest != self.source_hash:
            raise ValidationError("Bukti sumber saldo awal telah berubah.")


class OpeningBalanceItem(OpeningBalanceEvent):
    class Kind(models.TextChoices):
        RECEIVABLE = "receivable", "Piutang pelanggan lama"
        PAYABLE = "payable", "Utang pemasok lama"

    kind = models.CharField(max_length=20, choices=Kind.choices)
    party = models.ForeignKey(
        "finance.Party", on_delete=models.PROTECT, related_name="opening_balances"
    )
    source_date = models.DateField()
    source_reference = models.CharField(max_length=160)
    source_identity = models.CharField(max_length=480, editable=False)
    scoped_references = ("journal", "party")
    action = "record"

    class Meta:
        ordering = ["date", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "request_key"], name="fin_opening_item_request"
            ),
            models.UniqueConstraint(
                fields=["organization", "kind", "party", "source_identity"],
                name="fin_opening_item_source",
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gt=0), name="fin_opening_item_positive"
            ),
            models.CheckConstraint(
                condition=models.Q(source_date__lte=models.F("date")),
                name="fin_opening_source_date",
            ),
        ]

    def clean(self):
        super().clean()
        if (
            not self.source_reference.strip()
            or self.source_identity
            != normalize_supplier_reference(self.source_reference)
        ):
            raise ValidationError("Nomor dokumen asal saldo awal tidak sesuai bukti.")
        if self.party_id and (
            (self.kind == self.Kind.PAYABLE) != (self.party.kind == "supplier")
        ):
            raise ValidationError(
                "Piutang memerlukan pelanggan/mitra; utang memerlukan pemasok."
            )
        if self.source_date and self.date and self.source_date > self.date:
            raise ValidationError(
                "Dokumen asal tidak boleh lebih baru daripada tanggal saldo awal."
            )


class OpeningBalanceSettlement(OpeningBalanceEvent):
    item = models.ForeignKey(
        OpeningBalanceItem, on_delete=models.PROTECT, related_name="settlements"
    )
    bank_posting = models.OneToOneField(
        "finance.BankPosting",
        on_delete=models.PROTECT,
        related_name="opening_settlement",
    )
    scoped_references = ("journal", "item", "bank_posting")
    action = "settle"

    class Meta:
        ordering = ["date", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "request_key"],
                name="fin_opening_settle_request",
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gt=0), name="fin_opening_settle_positive"
            ),
        ]


class OpeningBalanceCorrection(OpeningBalanceEvent):
    """A dated principal correction; the original document stays immutable."""

    class Direction(models.TextChoices):
        INCREASE = "increase", "Tambah pokok"
        DECREASE = "decrease", "Kurangi pokok"

    item = models.ForeignKey(
        OpeningBalanceItem, on_delete=models.PROTECT, related_name="corrections"
    )
    direction = models.CharField(max_length=10, choices=Direction.choices)
    scoped_references = ("journal", "item")
    action = "correct"

    class Meta:
        ordering = ["date", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "request_key"],
                name="fin_opening_correct_request",
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gt=0), name="fin_opening_correct_positive"
            ),
            models.CheckConstraint(
                condition=models.Q(direction__in=["increase", "decrease"]),
                name="fin_opening_correct_direction",
            ),
        ]

    def clean(self):
        super().clean()
        if self.request_payload.get("direction") != self.direction:
            raise ValidationError("Arah koreksi tidak sesuai bukti keputusan asli.")
        if self.item_id and self.date and self.date < self.item.date:
            raise ValidationError(
                "Tanggal koreksi tidak boleh mendahului tanggal saldo awal."
            )


class OpeningBalanceCancellation(OpeningBalanceEvent):
    """Undo one whole wrong cash assignment; retain the physical bank source."""

    item = models.ForeignKey(
        OpeningBalanceItem, on_delete=models.PROTECT, related_name="cancellations"
    )
    settlement = models.OneToOneField(
        OpeningBalanceSettlement, on_delete=models.PROTECT, related_name="cancellation"
    )
    scoped_references = ("journal", "item", "settlement")
    action = "cancel"

    class Meta:
        ordering = ["date", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "request_key"],
                name="fin_opening_cancel_request",
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gt=0), name="fin_opening_cancel_positive"
            ),
        ]

    def clean(self):
        super().clean()
        if self.settlement_id and (
            self.item_id != self.settlement.item_id
            or self.date != self.settlement.date
            or self.amount != self.settlement.amount
        ):
            raise ValidationError(
                "Pembatalan harus memakai dokumen, tanggal dan seluruh jumlah pencocokan asli."
            )


class OpeningBalanceRefund(OpeningBalanceEvent):
    """Actual opposite cash linked to an original payment, not a new principal."""

    item = models.ForeignKey(
        OpeningBalanceItem, on_delete=models.PROTECT, related_name="refunds"
    )
    settlement = models.ForeignKey(
        OpeningBalanceSettlement, on_delete=models.PROTECT, related_name="refunds"
    )
    bank_posting = models.OneToOneField(
        "finance.BankPosting", on_delete=models.PROTECT, related_name="opening_refund"
    )
    scoped_references = ("journal", "item", "settlement", "bank_posting")
    action = "refund"

    class Meta:
        ordering = ["date", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "request_key"],
                name="fin_opening_refund_request",
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gt=0), name="fin_opening_refund_positive"
            ),
        ]

    def clean(self):
        super().clean()
        if self.settlement_id and (
            self.item_id != self.settlement.item_id or self.date < self.settlement.date
        ):
            raise ValidationError(
                "Pengembalian harus memakai pembayaran asal dan tanggal bank sesudah pembayaran tersebut."
            )
