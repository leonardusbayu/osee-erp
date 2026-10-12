"""Append-only unit delivery evidence; financial posting lives in fulfillment."""

from django.core.validators import MinValueValidator
from django.db import models

from .models import FinancialEvent


class InvoiceDelivery(FinancialEvent):
    invoice = models.ForeignKey(
        "finance.Invoice", on_delete=models.PROTECT, related_name="unit_deliveries"
    )
    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    amount = models.DecimalField(max_digits=20, decimal_places=2)
    scoped_references = ("invoice", "journal")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "reference"], name="finance_delivery_reference"
            )
        ]


class InvoiceCredit(FinancialEvent):
    invoice = models.ForeignKey(
        "finance.Invoice", on_delete=models.PROTECT, related_name="unit_credits"
    )
    kind = models.CharField(
        max_length=20,
        choices=[
            ("unfulfilled", "Pembatalan sebelum layanan"),
            ("service", "Koreksi layanan selesai"),
        ],
    )
    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    amount = models.DecimalField(max_digits=20, decimal_places=2)
    unpaid_amount = models.DecimalField(max_digits=20, decimal_places=2)
    refund_amount = models.DecimalField(max_digits=20, decimal_places=2)
    scoped_references = ("invoice", "journal")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "reference"], name="finance_credit_reference"
            )
        ]

    @property
    def refund_remaining(self):
        from .models import ZERO

        return self.refund_amount - (
            self.refunds.aggregate(value=models.Sum("amount"))["value"] or ZERO
        )


class CreditRefund(FinancialEvent):
    credit = models.ForeignKey(
        InvoiceCredit, on_delete=models.PROTECT, related_name="refunds"
    )
    bank_posting = models.OneToOneField("finance.BankPosting", on_delete=models.PROTECT)
    amount = models.DecimalField(max_digits=20, decimal_places=2)
    scoped_references = ("credit", "bank_posting", "journal")
