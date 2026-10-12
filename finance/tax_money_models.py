"""Immutable accounting events; bank matching does not certify tax payment."""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q, Sum

from .models import FinancialEvent


class CustomerTaxSettlement(FinancialEvent):
    invoice = models.ForeignKey(
        "finance.Invoice", on_delete=models.PROTECT, related_name="tax_settlements"
    )
    obligation = models.OneToOneField(
        "taxes.TaxObligation",
        on_delete=models.PROTECT,
        related_name="customer_settlement",
    )
    verification = models.OneToOneField(
        "taxes.TaxVerification",
        on_delete=models.PROTECT,
        related_name="customer_settlement",
    )
    bank_posting = models.OneToOneField(
        "finance.BankPosting", null=True, blank=True, on_delete=models.PROTECT
    )
    source_allocation = models.ForeignKey(
        "finance.Allocation",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="tax_credit_settlements",
    )
    source_advance_application = models.ForeignKey(
        "finance.AdvanceApplication",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="tax_credit_settlements",
    )
    cash_amount = models.DecimalField(max_digits=20, decimal_places=2)
    tax_amount = models.DecimalField(max_digits=20, decimal_places=2)
    gross_amount = models.DecimalField(max_digits=20, decimal_places=2)
    scoped_references = (
        "journal",
        "invoice",
        "obligation",
        "verification",
        "bank_posting",
        "source_allocation",
        "source_advance_application",
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(
                    cash_amount__gte=0,
                    tax_amount__gt=0,
                    gross_amount=models.F("cash_amount") + models.F("tax_amount"),
                ),
                name="finance_customer_tax_settlement_amounts",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        bank_posting__isnull=False,
                        source_allocation__isnull=True,
                        source_advance_application__isnull=True,
                        cash_amount__gt=0,
                    )
                    | Q(
                        bank_posting__isnull=True,
                        source_allocation__isnull=False,
                        source_advance_application__isnull=True,
                        cash_amount=0,
                    )
                    | Q(
                        bank_posting__isnull=True,
                        source_allocation__isnull=True,
                        source_advance_application__isnull=False,
                        cash_amount=0,
                    )
                ),
                name="finance_customer_tax_one_proof",
            ),
            models.UniqueConstraint(
                fields=["organization", "reference"],
                name="finance_customer_tax_reference",
            ),
        ]

    def clean(self):
        super().clean()
        if (
            self.source_allocation_id
            and self.source_allocation.invoice_id != self.invoice_id
        ):
            raise ValidationError("Pembayaran lama harus milik invoice yang sama.")
        if self.source_advance_application_id:
            application = self.source_advance_application
            advance = application.advance
            if (
                application.invoice_id != self.invoice_id
                or advance.party_id != self.invoice.party_id
            ):
                raise ValidationError(
                    "Penerapan uang muka harus milik invoice dan pelanggan yang sama."
                )
            if (
                advance.organization_id != self.organization_id
                or advance.party.organization_id != self.organization_id
            ):
                raise ValidationError(
                    "Uang muka pelanggan berasal dari organisasi lain."
                )
            if (
                application.amount <= 0
                or advance.amount <= 0
                or application.amount > advance.amount
            ):
                raise ValidationError(
                    "Penerapan uang muka harus bernilai positif dan tidak melebihi uang muka asal."
                )
        if self.obligation_id and self.obligation.source_invoice_id != self.invoice_id:
            raise ValidationError("Kewajiban pajak harus menunjuk invoice yang sama.")
        if (
            self.verification_id
            and self.verification.obligation_id != self.obligation_id
        ):
            raise ValidationError("Bukti potong harus menunjuk kewajiban yang sama.")
        if self.obligation_id and self.tax_amount != self.obligation.amount:
            raise ValidationError(
                "Kredit pajak harus sama dengan potongan yang telah disetujui."
            )


class OwnTaxRecognition(FinancialEvent):
    obligation = models.OneToOneField(
        "taxes.TaxObligation",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="own_recognition",
    )
    workpaper = models.OneToOneField(
        "taxes.AnnualTaxWorkpaper",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="own_recognition",
    )
    corporate_year = models.PositiveIntegerField(null=True, blank=True)
    final_period = models.DateField(null=True, blank=True)
    amount = models.DecimalField(max_digits=20, decimal_places=2)
    expense_amount = models.DecimalField(max_digits=20, decimal_places=2)
    credits_amount = models.DecimalField(
        max_digits=20, decimal_places=2, default=Decimal(0)
    )
    instruction_evidence = models.ForeignKey(
        "taxes.TaxEvidence", null=True, blank=True, on_delete=models.PROTECT
    )
    professional_instruction = models.TextField(max_length=2000, blank=True)
    scoped_references = ("journal", "obligation", "workpaper", "instruction_evidence")

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(
                        obligation__isnull=False,
                        workpaper__isnull=True,
                        corporate_year__isnull=True,
                        final_period__isnull=False,
                    )
                    | Q(
                        obligation__isnull=True,
                        workpaper__isnull=False,
                        corporate_year__isnull=False,
                        final_period__isnull=True,
                    )
                ),
                name="finance_own_tax_one_source",
            ),
            models.UniqueConstraint(
                fields=["organization", "reference"], name="finance_own_tax_reference"
            ),
            models.UniqueConstraint(
                fields=["organization", "corporate_year"],
                condition=Q(corporate_year__isnull=False),
                name="finance_own_tax_corporate_year",
            ),
            models.UniqueConstraint(
                fields=["organization", "final_period"],
                condition=Q(final_period__isnull=False),
                name="finance_own_tax_final_period",
            ),
            models.CheckConstraint(
                condition=Q(
                    amount__gt=0,
                    credits_amount__gte=0,
                    expense_amount=models.F("amount") + models.F("credits_amount"),
                ),
                name="finance_own_tax_amounts",
            ),
        ]

    @property
    def remaining_amount(self):
        paid = self.payments.aggregate(value=Sum("amount"))["value"] or Decimal(0)
        return self.amount - paid

    def __str__(self):
        source = (
            f"PPh badan {self.corporate_year}"
            if self.workpaper_id
            else f"Pajak final {self.obligation.period:%m/%Y}"
        )
        return f"{source} · sisa Rp {self.remaining_amount:,.2f}"


class OwnTaxPayment(FinancialEvent):
    recognition = models.ForeignKey(
        OwnTaxRecognition, on_delete=models.PROTECT, related_name="payments"
    )
    bank_posting = models.OneToOneField("finance.BankPosting", on_delete=models.PROTECT)
    amount = models.DecimalField(max_digits=20, decimal_places=2)
    scoped_references = ("journal", "recognition", "bank_posting")

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gt=0), name="finance_own_tax_payment_positive"
            )
        ]


class OwnTaxCreditApplication(FinancialEvent):
    recognition = models.ForeignKey(
        OwnTaxRecognition, on_delete=models.PROTECT, related_name="credit_applications"
    )
    settlement = models.OneToOneField(
        CustomerTaxSettlement, on_delete=models.PROTECT, related_name="tax_application"
    )
    amount = models.DecimalField(max_digits=20, decimal_places=2)
    scoped_references = ("journal", "recognition", "settlement")

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gt=0), name="finance_own_tax_credit_positive"
            )
        ]
