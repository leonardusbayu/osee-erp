"""Controlled spend identities and immutable observations; never ledger entries."""

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models

from finance.models import ScopedModel


def _json_object(value, label):
    if not isinstance(value, dict):
        raise ValidationError(
            f"{label} harus berupa catatan terstruktur. Minta Owner memeriksa sumber."
        )
    return value


def _source_amounts(value, *, key_name, label):
    if not isinstance(value, list):
        raise ValidationError(f"{label} tidak lengkap. Minta Owner memeriksa sumber.")
    keys, total = set(), Decimal("0.00")
    for item in value:
        if not isinstance(item, dict) or set(item) != {key_name, "amount"}:
            raise ValidationError(f"{label} memiliki bentuk sumber yang tidak sesuai.")
        key, raw_amount = item[key_name], item["amount"]
        if (
            not isinstance(key, str)
            or not re.fullmatch(r"(?:entry|movement):[1-9][0-9]*", key)
            or key in keys
        ):
            raise ValidationError(
                f"{label} memiliki sumber yang tidak sesuai atau berulang."
            )
        if not isinstance(raw_amount, str):
            raise ValidationError(f"{label} memiliki nilai sumber yang tidak sesuai.")
        try:
            amount = Decimal(raw_amount)
        except InvalidOperation as exc:
            raise ValidationError(
                f"{label} memiliki nilai sumber yang tidak sesuai."
            ) from exc
        if not amount.is_finite() or amount <= 0 or amount.as_tuple().exponent < -2:
            raise ValidationError(f"{label} memiliki nilai sumber yang tidak sesuai.")
        keys.add(key)
        total += amount
    return total


def _movement_request(movement):
    payload = _json_object(movement.payload, "Bukti perubahan belanja")
    return _json_object(payload.get("request"), "Permintaan perubahan belanja")


def record_digest(obj):
    excluded = {
        "id",
        "created_at",
        "content_hash",
        "version",
        "voided_at",
        "void_request",
    }
    values = {}
    for field in obj._meta.concrete_fields:
        if field.name not in excluded:
            value = getattr(obj, field.attname)
            # Preserve pre-extension receipts without rewriting their hashes.
            if field.name == "supplier_advance_application" and value is None:
                continue
            values[field.attname] = str(value) if isinstance(value, Decimal) else value
    return hashlib.sha256(
        json.dumps(values, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


class SpendRecord(ScopedModel):
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    content_hash = models.CharField(max_length=64)

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not getattr(self, "_spend_write", False):
            raise ValidationError("Gunakan layanan belanja untuk menyimpan perubahan.")
        try:
            return super().save(*args, **kwargs)
        finally:
            self.__dict__.pop("_spend_write", None)

    def clean(self):
        super().clean()
        if self.content_hash != record_digest(self):
            raise ValidationError(
                "Identitas dan bukti belanja tidak cocok dengan catatan yang disimpan."
            )
        old = self._old()
        mutable = {"version", "voided_at", "void_request_id"}
        self._unchanged(
            old,
            tuple(
                f.attname
                for f in self._meta.concrete_fields
                if f.name not in {"id", "created_at"} and f.attname not in mutable
            ),
        )
        if old and getattr(old, "voided_at", None):
            self._unchanged(old, ("voided_at", "void_request_id"))

    def delete(self, *args, **kwargs):
        raise ValidationError(
            "Riwayat belanja tidak dapat dihapus; gunakan koreksi dengan bukti."
        )


class BudgetSpend(SpendRecord):
    budget = models.ForeignKey(
        "director.Budget", on_delete=models.PROTECT, related_name="spends"
    )
    origin_entry = models.OneToOneField(
        "director.BudgetEntry", on_delete=models.PROTECT, related_name="budget_spend"
    )
    date = models.DateField()
    title = models.CharField(max_length=500)
    origin_snapshot = models.JSONField()
    version = models.PositiveIntegerField(default=1)
    scoped_references = ("budget", "origin_entry")

    def clean(self):
        super().clean()
        _json_object(self.origin_snapshot, "Bukti catatan asal belanja")
        if self.origin_entry_id and (
            self.origin_entry.budget_id != self.budget_id
            or self.origin_entry.kind == "payment"
        ):
            raise ValidationError(
                "Pilih biaya, komitmen atau reservasi dari anggaran yang sama."
            )


class BudgetSpendMovement(SpendRecord):
    spend = models.ForeignKey(
        BudgetSpend, on_delete=models.PROTECT, related_name="movements"
    )
    kind = models.CharField(max_length=24)
    date = models.DateField()
    amount = models.DecimalField(
        max_digits=20,
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(Decimal(0))],
    )
    from_kind = models.CharField(max_length=24, blank=True)
    to_kind = models.CharField(max_length=24, blank=True)
    reference = models.CharField(max_length=120)
    reason = models.CharField(max_length=500)
    evidence = models.TextField(max_length=4000)
    payload = models.JSONField()
    before = models.JSONField(default=dict, blank=True)
    after = models.JSONField(default=dict, blank=True)
    reverse_of = models.OneToOneField(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="reversal"
    )
    scoped_references = ("spend", "reverse_of")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "reference"], name="director_spend_request"
            )
        ]
        ordering = ["date", "pk"]

    def clean(self):
        super().clean()
        _movement_request(self)
        _json_object(self.before, "Posisi sebelum perubahan")
        _json_object(self.after, "Posisi setelah perubahan")
        if self.kind in {"convert", "release"}:
            total = _source_amounts(
                self.payload.get("allocations"),
                key_name="key",
                label="Sumber perpindahan belanja",
            )
            if not self.payload["allocations"] or total != self.amount:
                raise ValidationError(
                    "Sumber perpindahan belanja tidak cocok dengan nilai perubahan."
                )
        if self.reverse_of_id and self.reverse_of.spend_id != self.spend_id:
            raise ValidationError("Koreksi harus merujuk belanja yang sama.")


class BudgetBillLink(SpendRecord):
    spend = models.ForeignKey(
        BudgetSpend, on_delete=models.PROTECT, related_name="bill_links"
    )
    bill = models.ForeignKey(
        "finance.Bill", on_delete=models.PROTECT, related_name="budget_links"
    )
    request = models.OneToOneField(
        BudgetSpendMovement, on_delete=models.PROTECT, related_name="bill_link"
    )
    date = models.DateField()
    coverage = models.JSONField(default=list, blank=True)
    source_snapshot = models.JSONField()
    source_hash = models.CharField(max_length=64)
    voided_at = models.DateField(null=True, blank=True)
    void_request = models.OneToOneField(
        BudgetSpendMovement,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="voided_link",
    )
    scoped_references = ("spend", "bill", "request", "void_request")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["bill"],
                condition=models.Q(voided_at__isnull=True),
                name="director_active_bill_link",
            )
        ]

    def clean(self):
        super().clean()
        _source_amounts(
            self.coverage,
            key_name="source_key",
            label="Catatan yang digantikan tagihan",
        )
        _json_object(self.source_snapshot, "Bukti sumber tagihan")
        if self.request_id and self.request.spend_id != self.spend_id:
            raise ValidationError(
                "Permintaan hubungan harus berasal dari belanja yang sama."
            )
        if bool(self.voided_at) != bool(self.void_request_id):
            raise ValidationError(
                "Penarikan hubungan memerlukan tanggal dan permintaan koreksi."
            )
        if self.void_request_id and (
            self.void_request.spend_id != self.spend_id
            or self.void_request.date != self.voided_at
            or _movement_request(self.void_request).get("link_id") != self.pk
        ):
            raise ValidationError(
                "Bukti penarikan hubungan tidak cocok dengan catatan asal."
            )


class BudgetPaymentMatch(SpendRecord):
    spend = models.ForeignKey(
        BudgetSpend, on_delete=models.PROTECT, related_name="payment_matches"
    )
    request = models.OneToOneField(
        BudgetSpendMovement, on_delete=models.PROTECT, related_name="payment_match"
    )
    date = models.DateField()
    amount = models.DecimalField(
        max_digits=20, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))]
    )
    manual_entry = models.ForeignKey(
        "director.BudgetEntry",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="payment_matches",
    )
    manual_movement = models.ForeignKey(
        BudgetSpendMovement,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="payment_matches",
    )
    bill_payment = models.ForeignKey(
        "finance.BillPayment",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="budget_matches",
    )
    withholding_settlement = models.ForeignKey(
        "finance.WithholdingSettlement",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="budget_matches",
    )
    tax_remittance = models.ForeignKey(
        "finance.TaxRemittance",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="budget_matches",
    )
    supplier_advance_application = models.ForeignKey(
        "finance.SupplierAdvanceApplication",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="budget_matches",
    )
    source_snapshot = models.JSONField()
    source_hash = models.CharField(max_length=64)
    voided_at = models.DateField(null=True, blank=True)
    void_request = models.OneToOneField(
        BudgetSpendMovement,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="voided_match",
    )
    scoped_references = (
        "spend",
        "request",
        "manual_entry",
        "manual_movement",
        "bill_payment",
        "withholding_settlement",
        "tax_remittance",
        "supplier_advance_application",
        "void_request",
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(manual_entry__isnull=False, manual_movement__isnull=True)
                    | models.Q(manual_entry__isnull=True, manual_movement__isnull=False)
                ),
                name="director_match_manual_source",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        bill_payment__isnull=False,
                        withholding_settlement__isnull=True,
                        tax_remittance__isnull=True,
                        supplier_advance_application__isnull=True,
                    )
                    | models.Q(
                        bill_payment__isnull=True,
                        withholding_settlement__isnull=False,
                        tax_remittance__isnull=True,
                        supplier_advance_application__isnull=True,
                    )
                    | models.Q(
                        bill_payment__isnull=True,
                        withholding_settlement__isnull=True,
                        tax_remittance__isnull=False,
                        supplier_advance_application__isnull=True,
                    )
                    | models.Q(
                        bill_payment__isnull=True,
                        withholding_settlement__isnull=True,
                        tax_remittance__isnull=True,
                        supplier_advance_application__isnull=False,
                    )
                ),
                name="director_match_finance_source",
            ),
        ]

    def clean(self):
        super().clean()
        _json_object(self.source_snapshot, "Bukti sumber pembayaran")
        if self.request_id and self.request.spend_id != self.spend_id:
            raise ValidationError(
                "Permintaan pencocokan harus berasal dari belanja yang sama."
            )
        if self.manual_entry_id and (
            self.manual_entry.kind != "payment"
            or self.manual_entry.budget_id != self.spend.budget_id
            or self.manual_entry.settles_id != self.spend.origin_entry_id
        ):
            raise ValidationError(
                "Pembayaran dilaporkan harus terkait dengan baris asal belanja."
            )
        if self.manual_movement_id and (
            self.manual_movement.kind != "payment"
            or self.manual_movement.spend_id != self.spend_id
        ):
            raise ValidationError("Pilih pembayaran dilaporkan dari belanja yang sama.")
        if bool(self.voided_at) != bool(self.void_request_id):
            raise ValidationError(
                "Penarikan pencocokan memerlukan tanggal dan permintaan koreksi."
            )
        if self.void_request_id and (
            self.void_request.spend_id != self.spend_id
            or self.void_request.date != self.voided_at
            or _movement_request(self.void_request).get("match_id") != self.pk
        ):
            raise ValidationError(
                "Bukti penarikan pencocokan tidak cocok dengan catatan asal."
            )
