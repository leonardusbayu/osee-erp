"""People and payroll records. Tax and benefit amounts are supplied manually."""

from decimal import Decimal
from typing import ClassVar

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import F, Q

from finance.models import BankPosting, BankTransaction, Journal, ScopedModel

ZERO = Decimal("0.00")


class PeopleRecord(ScopedModel):
    """Require application services for all writes to confidential people data."""

    class Meta:
        abstract = True

    def clean(self):
        super().clean()
        if not getattr(self, "_service_transition", False):
            from django.core.exceptions import ValidationError

            raise ValidationError("Gunakan alur SDM untuk menyimpan perubahan.")


class PayrollPerson(PeopleRecord):
    class Kind(models.TextChoices):
        EMPLOYEE = "employee", "Karyawan"
        CONTRACTOR = "contractor", "Tutor / kontraktor"

    name = models.CharField(max_length=150)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        ordering: ClassVar[list[str]] = ["name", "pk"]

    def clean(self):
        super().clean()
        if self.name and not self.name.strip():
            from django.core.exceptions import ValidationError

            raise ValidationError({"name": "Nama wajib diisi."})

    def delete(self, *args, **kwargs):
        from django.core.exceptions import ValidationError

        raise ValidationError("Riwayat SDM tidak dihapus. Nonaktifkan orang tersebut.")


class PayrollRun(PeopleRecord):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draf"
        SUBMITTED = "submitted", "Menunggu persetujuan"
        POSTED = "posted", "Disetujui dan tercatat"
        REVERSED = "reversed", "Dikoreksi"
        REJECTED = "rejected", "Perlu revisi"

    year = models.PositiveSmallIntegerField()
    month = models.PositiveSmallIntegerField()
    revision = models.PositiveSmallIntegerField(default=1)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.DRAFT
    )
    version = models.PositiveIntegerField(default=1)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_payroll_runs",
    )
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="submitted_payroll_runs",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="approved_payroll_runs",
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    approval_note = models.CharField(max_length=500, blank=True)
    rejected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="rejected_payroll_runs",
    )
    rejected_at = models.DateTimeField(null=True, blank=True)
    rejection_note = models.CharField(max_length=500, blank=True)
    journal = models.ForeignKey(
        Journal,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="payroll_runs",
    )
    reversal_journal = models.ForeignKey(
        Journal,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="reversed_payroll_runs",
    )
    reversed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="reversed_payroll_runs",
    )
    reversed_at = models.DateTimeField(null=True, blank=True)
    reversal_date = models.DateField(null=True, blank=True)
    reversal_reason = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-year", "-month", "-revision", "-pk"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["organization", "year", "month", "revision"],
                name="people_payroll_revision",
            ),
            models.CheckConstraint(
                condition=Q(month__gte=1, month__lte=12),
                name="people_payroll_valid_month",
            ),
            models.CheckConstraint(
                condition=Q(revision__gte=1), name="people_payroll_positive_revision"
            ),
        ]

    def clean(self):
        super().clean()
        if (
            not 2000 <= self.year <= 2200
            or not 1 <= self.month <= 12
            or self.revision < 1
        ):
            from django.core.exceptions import ValidationError

            raise ValidationError("Periode penggajian tidak valid.")
        old = self._old()
        if old:
            self._unchanged(old, ("year", "month", "revision", "created_by_id"))

    def delete(self, *args, **kwargs):
        from django.core.exceptions import ValidationError

        raise ValidationError(
            "Riwayat penggajian disimpan. Gunakan pembatalan draf atau koreksi tercatat."
        )

    @property
    def period_label(self):
        months = (
            "",
            "Januari",
            "Februari",
            "Maret",
            "April",
            "Mei",
            "Juni",
            "Juli",
            "Agustus",
            "September",
            "Oktober",
            "November",
            "Desember",
        )
        return f"{months[self.month]} {self.year}"

    @property
    def status_label(self):
        labels = {
            self.Status.DRAFT: "Belum diajukan",
            self.Status.SUBMITTED: "Menunggu persetujuan",
            self.Status.POSTED: "Disetujui",
            self.Status.REVERSED: "Sudah dikoreksi",
            self.Status.REJECTED: "Perlu perbaikan",
        }
        return labels.get(self.status, self.get_status_display())


class PayrollLine(PeopleRecord):
    run = models.ForeignKey(PayrollRun, on_delete=models.PROTECT, related_name="lines")
    person = models.ForeignKey(
        PayrollPerson, on_delete=models.PROTECT, related_name="payroll_lines"
    )
    gross_amount = models.DecimalField(
        max_digits=20, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))]
    )
    pph21_amount = models.DecimalField(
        max_digits=20,
        decimal_places=2,
        default=ZERO,
        validators=[MinValueValidator(ZERO)],
    )
    bpjs_employee_amount = models.DecimalField(
        max_digits=20,
        decimal_places=2,
        default=ZERO,
        validators=[MinValueValidator(ZERO)],
    )
    bpjs_employer_amount = models.DecimalField(
        max_digits=20,
        decimal_places=2,
        default=ZERO,
        validators=[MinValueValidator(ZERO)],
    )
    basis_reference = models.CharField(max_length=240, blank=True)
    note = models.CharField(max_length=500, blank=True)
    scoped_references = ("run", "person")

    class Meta:
        ordering: ClassVar[list[str]] = ["person__name", "pk"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["run", "person"], name="people_payroll_person_once"
            ),
            models.CheckConstraint(
                condition=Q(gross_amount__gt=0), name="people_payroll_gross_positive"
            ),
            models.CheckConstraint(
                condition=Q(
                    pph21_amount__gte=0,
                    bpjs_employee_amount__gte=0,
                    bpjs_employer_amount__gte=0,
                ),
                name="people_payroll_deductions_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(
                    gross_amount__gte=F("pph21_amount") + F("bpjs_employee_amount")
                ),
                name="people_payroll_net_nonnegative",
            ),
        ]

    def clean(self):
        super().clean()
        from django.core.exceptions import ValidationError

        if (
            self.gross_amount
            and self.pph21_amount is not None
            and self.bpjs_employee_amount is not None
            and self.pph21_amount + self.bpjs_employee_amount > self.gross_amount
        ):
            raise ValidationError(
                {
                    "bpjs_employee_amount": "Total potongan karyawan tidak boleh melebihi jumlah sebelum potongan."
                }
            )
        if (
            any(
                value and value > ZERO
                for value in (
                    self.pph21_amount,
                    self.bpjs_employee_amount,
                    self.bpjs_employer_amount,
                )
            )
            and not self.basis_reference.strip()
        ):
            raise ValidationError(
                {
                    "basis_reference": "Cantumkan rujukan dokumen/keputusan yang menyetujui nominal PPh 21 atau BPJS."
                }
            )

    @property
    def net_amount(self):
        return self.gross_amount - self.pph21_amount - self.bpjs_employee_amount

    def delete(self, *args, **kwargs):
        from django.core.exceptions import ValidationError

        if self.run.status != PayrollRun.Status.DRAFT or not getattr(
            self, "_service_transition", False
        ):
            raise ValidationError(
                "Rincian hanya dapat dihapus melalui alur koreksi payroll draf."
            )
        return super().delete(*args, **kwargs)


class PayrollPayment(PeopleRecord):
    class Kind(models.TextChoices):
        NET_PAY = "net_pay", "Gaji / honor bersih"
        PPH21 = "pph21", "PPh 21"
        BPJS = "bpjs", "BPJS"

    run = models.ForeignKey(
        PayrollRun, on_delete=models.PROTECT, related_name="payments"
    )
    transaction = models.ForeignKey(
        BankTransaction, on_delete=models.PROTECT, related_name="payroll_payments"
    )
    bank_posting = models.OneToOneField(
        BankPosting, on_delete=models.PROTECT, related_name="payroll_payment"
    )
    kind = models.CharField(max_length=20, choices=Kind.choices)
    amount = models.DecimalField(
        max_digits=20, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))]
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="recorded_payroll_payments",
    )
    scoped_references = ("run", "transaction", "bank_posting")

    class Meta:
        ordering: ClassVar[list[str]] = ["-created_at", "-pk"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["run", "transaction", "kind"],
                name="people_payroll_payment_once",
            ),
            models.CheckConstraint(
                condition=Q(amount__gt=0), name="people_payroll_payment_positive"
            ),
        ]

    def clean(self):
        super().clean()
        self._unchanged(
            self._old(),
            (
                "run_id",
                "transaction_id",
                "bank_posting_id",
                "kind",
                "amount",
                "created_by_id",
            ),
        )

    def delete(self, *args, **kwargs):
        from django.core.exceptions import ValidationError

        raise ValidationError(
            "Pembayaran payroll yang sudah dicocokkan tidak dihapus. Gunakan koreksi bank."
        )
