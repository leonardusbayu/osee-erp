"""Organization-scoped finance records. Posting is owned by finance.services."""

from decimal import Decimal

from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models, transaction
from django.db.models import Q, Sum
from django.utils import timezone


ZERO = Decimal("0.00")


def assert_open_period(organization_id, posting_date):
    """A closed month also freezes every earlier posting feeding its balances."""
    if AccountingPeriod.objects.filter(organization_id=organization_id, closed=True).filter(
        Q(year__gt=posting_date.year) | Q(year=posting_date.year, month__gte=posting_date.month)
    ).exists():
        raise ValidationError("Tanggal berada dalam atau sebelum periode akuntansi yang sudah dikunci.")


class ValidatedQuerySet(models.QuerySet):
    """Do not let routine ORM bulk writes bypass financial validation."""

    def update(self, **kwargs):
        raise ValidationError("Gunakan layanan keuangan untuk mengubah catatan.")

    def bulk_create(self, *args, **kwargs):
        raise ValidationError("Impor keuangan harus memvalidasi setiap catatan.")

    def bulk_update(self, *args, **kwargs):
        raise ValidationError("Gunakan layanan keuangan untuk mengubah catatan.")

    def delete(self):
        raise ValidationError("Penghapusan massal catatan keuangan tidak diizinkan.")


class ScopedModel(models.Model):
    organization = models.ForeignKey("core.Organization", on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    objects = ValidatedQuerySet.as_manager()
    scoped_references = ()

    class Meta:
        abstract = True

    def clean(self):
        super().clean()
        for field in self.scoped_references:
            if getattr(self, f"{field}_id", None):
                try:
                    linked = getattr(self, field)
                except ObjectDoesNotExist as exc:
                    raise ValidationError({field: "Catatan terkait tidak ditemukan."}) from exc
                if linked.organization_id != self.organization_id:
                    raise ValidationError({field: "Catatan berasal dari organisasi lain."})
        if self.pk:
            old_org = type(self).objects.filter(pk=self.pk).values_list(
                "organization_id", flat=True
            ).first()
            if old_org is not None and old_org != self.organization_id:
                raise ValidationError("Organisasi catatan tidak boleh diganti.")

    def save(self, *args, **kwargs):
        # A common organization lock serializes posting, allocations and closing
        # in PostgreSQL, including callers saving validated model instances.
        from core.models import Organization

        with transaction.atomic():
            if self.organization_id:
                Organization.objects.select_for_update().get(pk=self.organization_id)
            try:
                self.full_clean()
                return super().save(*args, **kwargs)
            finally:
                # Privileges belong to one controlled save, never to returned objects.
                for flag in ("_service_transition", "_allow_price_end_change", "_tax_service_transition", "_opening_service_transition"):
                    self.__dict__.pop(flag, None)

    def _old(self):
        return type(self).objects.filter(pk=self.pk).first() if self.pk else None

    def _unchanged(self, old, fields):
        if old and any(getattr(old, field) != getattr(self, field) for field in fields):
            raise ValidationError("Catatan yang telah disahkan tidak dapat diubah.")

    def _delete_current_draft(self, *args, **kwargs):
        from core.models import Organization
        with transaction.atomic():
            Organization.objects.select_for_update().get(pk=self.organization_id)
            current = type(self).objects.select_for_update().get(pk=self.pk)
            if current.organization_id != self.organization_id or current.status != "draft":
                raise ValidationError("Dokumen terposting tidak boleh dihapus.")
            assert_open_period(self.organization_id, current.date)
            return super().delete(*args, **kwargs)


class Party(ScopedModel):
    class Kind(models.TextChoices):
        CUSTOMER = "customer", "Pelanggan"
        RESELLER = "reseller", "Mitra reseller"
        SUPPLIER = "supplier", "Pemasok"

    name = models.CharField(max_length=200)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    tax_id = models.CharField(max_length=32, blank=True)

    class Meta:
        ordering = ["name", "pk"]

    def __str__(self):
        return self.name


class Product(ScopedModel):
    class Kind(models.TextChoices):
        ITP = "itp", "TOEFL ITP"
        IBT = "ibt", "TOEFL iBT"
        COURSE = "course", "Kursus"

    code = models.CharField(max_length=60)
    name = models.CharField(max_length=200)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    default_price = models.DecimalField(max_digits=20, decimal_places=2, validators=[MinValueValidator(ZERO)])

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "code"], name="finance_product_org_code")]
        ordering = ["code"]

    def __str__(self):
        return self.name


class PriceVersion(ScopedModel):
    class Kind(models.TextChoices):
        SELLING = "selling", "Harga jual"
        SUPPLIER = "supplier", "Harga pemasok"

    party = models.ForeignKey(Party, null=True, blank=True, on_delete=models.PROTECT)
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    amount = models.DecimalField(max_digits=20, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    scoped_references = ("party", "product")

    class Meta:
        ordering = ["-effective_from", "-pk"]
        constraints = [models.CheckConstraint(condition=Q(effective_to__isnull=True) | Q(effective_to__gte=models.F("effective_from")), name="finance_price_dates")]

    def clean(self):
        super().clean()
        old = self._old()
        self._unchanged(old, ("party_id", "product_id", "kind", "amount", "effective_from"))
        if not getattr(self, "_allow_price_end_change", False):
            self._unchanged(old, ("effective_to",))
        elif old and (not self.effective_to or self.effective_to < self.effective_from or (old.effective_to and self.effective_to > old.effective_to)):
            raise ValidationError("Penggantian harga hanya boleh memperpendek masa berlaku lama.")
        if self.party_id:
            supplier = self.party.kind == Party.Kind.SUPPLIER
            if (self.kind == self.Kind.SUPPLIER) != supplier:
                raise ValidationError("Jenis mitra tidak sesuai dengan jenis harga.")
        if self.effective_from and self.product_id:
            overlaps = type(self).objects.filter(
                organization_id=self.organization_id, product_id=self.product_id,
                party_id=self.party_id, kind=self.kind,
            ).exclude(pk=self.pk)
            if self.effective_to:
                overlaps = overlaps.filter(effective_from__lte=self.effective_to)
            overlaps = overlaps.filter(Q(effective_to__isnull=True) | Q(effective_to__gte=self.effective_from))
            if overlaps.exists():
                raise ValidationError("Periode harga bertumpang tindih. Tanggal akhir bersifat inklusif.")

    def delete(self, *args, **kwargs):
        raise ValidationError("Versi harga disimpan sebagai riwayat dan tidak boleh dihapus.")


class Invoice(ScopedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draf"
        ISSUED = "issued", "Diterbitkan"
        DELIVERED = "delivered", "Layanan selesai"
        CANCELLED = "cancelled", "Dibatalkan"

    number = models.CharField(max_length=80)
    party = models.ForeignKey(Party, on_delete=models.PROTECT, related_name="invoices")
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    unit_price = models.DecimalField(max_digits=20, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    price_version = models.ForeignKey(PriceVersion, null=True, blank=True, on_delete=models.PROTECT)
    date = models.DateField()
    due_date = models.DateField()
    service_date = models.DateField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    cost_estimate = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(ZERO)])
    delivered_at = models.DateField(null=True, blank=True)
    scoped_references = ("party", "product", "price_version")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "number"], name="finance_invoice_org_number")]
        ordering = ["-date", "-pk"]

    def clean(self):
        super().clean()
        if self.party_id and self.party.kind == Party.Kind.SUPPLIER:
            raise ValidationError({"party": "Tagihan penjualan memerlukan pelanggan atau reseller."})
        if self.due_date and self.date and self.due_date < self.date:
            raise ValidationError({"due_date": "Jatuh tempo tidak boleh sebelum tanggal tagihan."})
        if self.price_version_id and (self.price_version.product_id != self.product_id or self.price_version.kind != PriceVersion.Kind.SELLING or self.price_version.party_id not in (None, self.party_id)):
            raise ValidationError({"price_version": "Versi harga tidak sesuai dengan produk/pelanggan."})
        old = self._old()
        if old and old.status != self.Status.DRAFT:
            self._unchanged(old, ("number", "party_id", "product_id", "quantity", "unit_price", "price_version_id", "date", "due_date", "service_date", "cost_estimate"))
        if old and old.delivered_at != self.delivered_at and (old.status == self.Status.DELIVERED or not getattr(self, "_service_transition", False)):
            raise ValidationError("Tanggal penyelesaian aktual tidak boleh diubah setelah dicatat.")
        if ((not old and self.status != self.Status.DRAFT) or (old and old.status != self.status)) and not getattr(self, "_service_transition", False):
            raise ValidationError("Gunakan layanan keuangan untuk menerbitkan/menyelesaikan tagihan.")
        if old and old.status != self.status and (old.status, self.status) not in {("draft", "issued"), ("issued", "delivered"), ("draft", "cancelled"), ("issued", "cancelled")}:
            raise ValidationError("Perubahan status invoice tidak diizinkan.")
        if self.status == "delivered" and not self.delivered_at:
            raise ValidationError("Tanggal penyelesaian wajib untuk layanan selesai.")

    @property
    def total(self):
        return (self.unit_price * self.quantity).quantize(Decimal("0.01"))

    @property
    def paid_amount(self):
        receipts = self.allocations.aggregate(amount=Sum("amount"))["amount"] or ZERO
        advances = self.advance_applications.aggregate(amount=Sum("amount"))["amount"] or ZERO
        return receipts + advances

    @property
    def outstanding_amount(self):
        if self.status == "cancelled":
            return ZERO
        return self.total - self.paid_amount

    def delete(self, *args, **kwargs):
        return self._delete_current_draft(*args, **kwargs)

    def __str__(self):
        return self.number


class Bill(ScopedModel):
    number = models.CharField(max_length=80)
    supplier = models.ForeignKey(Party, on_delete=models.PROTECT, related_name="bills")
    amount = models.DecimalField(max_digits=20, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    supplier_vat = models.DecimalField(max_digits=20, decimal_places=2, default=ZERO, validators=[MinValueValidator(ZERO)])
    date = models.DateField()
    service_date = models.DateField()
    category = models.CharField(max_length=20, choices=[("expense", "Beban"), ("prepayment", "Biaya dibayar di muka"), ("provider", "Biaya penyedia tes")], default="expense")
    status = models.CharField(max_length=20, choices=[("draft", "Draf"), ("approved", "Disahkan")], default="draft")
    tax_status = models.CharField(max_length=20, choices=[("review", "Perlu tinjauan"), ("pending", "Menunggu bukti"), ("reviewed", "Ditinjau")], default="review")
    tax_amount = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(ZERO)])
    scoped_references = ("supplier",)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "supplier", "number"], name="finance_bill_supplier_number")]
        ordering = ["-date", "-pk"]

    def clean(self):
        super().clean()
        if self.supplier_id and self.supplier.kind != Party.Kind.SUPPLIER:
            raise ValidationError({"supplier": "Pilih pemasok untuk tagihan pembelian."})
        if self.amount is not None and self.supplier_vat is not None and self.supplier_vat > self.amount:
            raise ValidationError({"supplier_vat": "PPN pemasok adalah bagian dari jumlah bruto, bukan tambahan."})
        old = self._old()
        if old and old.status == "approved":
            self._unchanged(old, ("number", "supplier_id", "amount", "supplier_vat", "date", "service_date", "category"))
        if ((not old and self.status != "draft") or (old and old.status != self.status)) and not getattr(self, "_service_transition", False):
            raise ValidationError("Gunakan layanan pengesahan tagihan pembelian.")
        if old and old.status != self.status and (old.status, self.status) != ("draft", "approved"):
            raise ValidationError("Perubahan status tagihan pembelian tidak diizinkan.")
        tax_changed = (old and (old.tax_status, old.tax_amount) != (self.tax_status, self.tax_amount)) or (not old and (self.tax_status != "review" or self.tax_amount is not None))
        if tax_changed and not getattr(self, "_tax_service_transition", False):
            raise ValidationError("Keputusan potongan pajak hanya dapat dicatat melalui layanan tinjauan pajak.")
        if self.tax_amount is not None and self.amount is not None and self.tax_amount >= self.amount:
            raise ValidationError("Potongan pajak harus lebih kecil dari jumlah bruto tagihan.")

    def delete(self, *args, **kwargs):
        return self._delete_current_draft(*args, **kwargs)

    def __str__(self):
        return self.number

    @property
    def paid_amount(self):
        paid = self.payments.aggregate(amount=Sum("amount"))["amount"] or ZERO
        withheld = WithholdingSettlement.objects.filter(bill=self).aggregate(amount=Sum("gross_amount"))["amount"] or ZERO
        return paid + withheld

    @property
    def outstanding_amount(self):
        return self.amount - self.paid_amount

    @property
    def prepaid_remaining(self):
        if self.status != "approved" or not (self.category == "prepayment" or self.service_date > self.date):
            return ZERO
        return self.amount - (self.prepayment_releases.aggregate(amount=Sum("amount"))["amount"] or ZERO)


class BankAccount(ScopedModel):
    name = models.CharField(max_length=120)
    account_number = models.CharField(max_length=80)
    opening_balance = models.DecimalField(max_digits=20, decimal_places=2, default=ZERO)
    opening_date = models.DateField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "account_number"], name="finance_bank_account_number")]
        ordering = ["name"]

    def clean(self):
        super().clean()
        old = self._old()
        if not getattr(self, "_opening_service_transition", False):
            self._unchanged(old, ("opening_balance", "opening_date"))
            if not old and (self.opening_balance != ZERO or self.opening_date is not None):
                raise ValidationError("Gunakan layanan saldo awal yang ditinjau pemilik.")

    def __str__(self):
        return self.name


class BankTransaction(ScopedModel):
    account = models.ForeignKey(BankAccount, on_delete=models.PROTECT, related_name="transactions")
    reference = models.CharField(max_length=160)
    date = models.DateField()
    description = models.CharField(max_length=500, blank=True)
    amount = models.DecimalField(max_digits=20, decimal_places=2)
    scoped_references = ("account",)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "account", "reference"], name="finance_bank_reference"),
            models.CheckConstraint(condition=~Q(amount=0), name="finance_bank_amount_nonzero"),
        ]
        ordering = ["-date", "-pk"]

    def clean(self):
        super().clean()
        self._unchanged(self._old(), ("account_id", "reference", "date", "description", "amount"))
        if not self.pk:
            if self.date and self.date > timezone.localdate():
                raise ValidationError({"date": "Mutasi bank aktual tidak boleh bertanggal di masa depan."})
            if self.date:
                assert_open_period(self.organization_id, self.date)
                if self.account_id and self.account.opening_date and self.date < self.account.opening_date:
                    raise ValidationError("Mutasi sebelum saldo awal memerlukan rekonstruksi migrasi, bukan impor tambahan.")

    @property
    def allocated_amount(self):
        receipts = self.allocations.aggregate(amount=Sum("amount"))["amount"] or ZERO
        payments = self.bill_payments.aggregate(amount=Sum("amount"))["amount"] or ZERO
        other = self.postings.aggregate(amount=Sum("signed_amount"))["amount"] or ZERO
        return receipts - payments + other

    @property
    def unallocated_amount(self):
        return self.amount - self.allocated_amount

    def delete(self, *args, **kwargs):
        raise ValidationError("Mutasi bank adalah bukti sumber dan tidak boleh dihapus.")


class Allocation(ScopedModel):
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name="allocations")
    transaction = models.ForeignKey(BankTransaction, on_delete=models.PROTECT, related_name="allocations")
    amount = models.DecimalField(max_digits=20, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    scoped_references = ("invoice", "transaction")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["invoice", "transaction"], name="finance_receipt_invoice_pair")]

    def clean(self):
        super().clean()
        self._unchanged(self._old(), ("invoice_id", "transaction_id", "amount"))
        if not self.pk and not getattr(self, "_service_transition", False):
            raise ValidationError("Gunakan rekonsiliasi penerimaan untuk membuat alokasi.")
        if self.transaction_id and self.transaction.amount <= ZERO:
            raise ValidationError("Hanya mutasi masuk positif yang dapat dialokasikan sebagai penerimaan.")

    def delete(self, *args, **kwargs):
        raise ValidationError("Alokasi terposting memerlukan koreksi akuntansi, bukan penghapusan.")


class BillPayment(ScopedModel):
    bill = models.ForeignKey(Bill, on_delete=models.PROTECT, related_name="payments")
    transaction = models.ForeignKey(BankTransaction, on_delete=models.PROTECT, related_name="bill_payments")
    amount = models.DecimalField(max_digits=20, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    scoped_references = ("bill", "transaction")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["bill", "transaction"], name="finance_bill_payment_pair")]

    def clean(self):
        super().clean()
        self._unchanged(self._old(), ("bill_id", "transaction_id", "amount"))
        if not self.pk and not getattr(self, "_service_transition", False):
            raise ValidationError("Gunakan rekonsiliasi pembayaran untuk membuat alokasi.")
        if self.transaction_id and self.transaction.amount >= ZERO:
            raise ValidationError("Pembayaran pemasok memerlukan mutasi keluar negatif.")

    def delete(self, *args, **kwargs):
        raise ValidationError("Pembayaran terposting memerlukan koreksi, bukan penghapusan.")


class AccountingPeriod(ScopedModel):
    year = models.PositiveIntegerField(validators=[MinValueValidator(2000), MaxValueValidator(2200)])
    month = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(12)])
    closed = models.BooleanField(default=False)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "year", "month"], name="finance_org_period")]
        ordering = ["-year", "-month"]

    def clean(self):
        super().clean()
        old = self._old()
        self._unchanged(old, ("year", "month"))
        if old and old.closed:
            self._unchanged(old, ("closed", "closed_at"))

    def delete(self, *args, **kwargs):
        raise ValidationError("Periode akuntansi tidak boleh dihapus.")


class Journal(ScopedModel):
    date = models.DateField()
    description = models.CharField(max_length=250)
    source_key = models.CharField(max_length=160)
    status = models.CharField(max_length=10, choices=[("draft", "Draf"), ("posted", "Terposting")], default="draft")
    posted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "source_key"], name="finance_journal_source_key")]
        ordering = ["-date", "-pk"]

    def clean(self):
        super().clean()
        old = self._old()
        if old and old.status == "posted":
            raise ValidationError("Jurnal terposting tidak dapat diubah.")
        if self.status == "posted":
            if not self.pk or not getattr(self, "_service_transition", False):
                raise ValidationError("Jurnal harus diposting melalui layanan buku besar.")
            totals = self.lines.aggregate(debit=Sum("debit"), credit=Sum("credit"))
            if self.lines.count() < 2 or not totals["debit"] or totals["debit"] != totals["credit"]:
                raise ValidationError("Jurnal wajib seimbang dan memiliki setidaknya dua baris.")
            assert_open_period(self.organization_id, self.date)

    def delete(self, *args, **kwargs):
        raise ValidationError("Jurnal tidak boleh dihapus. Gunakan pembalikan yang disetujui.")


class JournalLine(ScopedModel):
    class Account(models.TextChoices):
        BANK = "BANK", "Bank"
        AR = "AR", "Piutang usaha"
        DEFERRED_REVENUE = "DEFERRED_REVENUE", "Pendapatan diterima di muka"
        REVENUE = "REVENUE", "Pendapatan"
        EXPENSE = "EXPENSE", "Beban"
        PREPAID = "PREPAID", "Biaya dibayar di muka"
        AP = "AP", "Utang usaha"
        EQUITY = "EQUITY", "Modal / saldo awal yang ditinjau"
        BANK_TRANSFER = "BANK_TRANSFER", "Transfer antarbank dalam perjalanan"
        CUSTOMER_ADVANCE = "CUSTOMER_ADVANCE", "Uang muka pelanggan belum diterapkan"
        CUSTOMER_REFUND = "CUSTOMER_REFUND", "Utang pengembalian pelanggan"
        TAX_PAYABLE = "TAX_PAYABLE", "Utang pajak potongan"

    journal = models.ForeignKey(Journal, on_delete=models.PROTECT, related_name="lines")
    account = models.CharField(max_length=30, choices=Account.choices)
    debit = models.DecimalField(max_digits=20, decimal_places=2, default=ZERO, validators=[MinValueValidator(ZERO)])
    credit = models.DecimalField(max_digits=20, decimal_places=2, default=ZERO, validators=[MinValueValidator(ZERO)])
    scoped_references = ("journal",)

    class Meta:
        constraints = [models.CheckConstraint(condition=(Q(debit__gt=0) & Q(credit=0)) | (Q(credit__gt=0) & Q(debit=0)), name="finance_journal_single_side")]

    def clean(self):
        super().clean()
        if self.journal_id and Journal.objects.filter(pk=self.journal_id, status="posted").exists():
            raise ValidationError("Baris jurnal terposting tidak dapat ditambahkan/diubah.")
        old = self._old()
        if old and old.journal.status == "posted":
            raise ValidationError("Baris jurnal terposting tidak dapat dipindahkan/diubah.")

    def delete(self, *args, **kwargs):
        if Journal.objects.filter(pk=self.journal_id, status="posted").exists():
            raise ValidationError("Baris jurnal terposting tidak dapat dihapus.")
        return super().delete(*args, **kwargs)


class FinancialEvent(ScopedModel):
    """One immutable, scoped accounting decision linked to its posted journal."""
    journal = models.ForeignKey(Journal, on_delete=models.PROTECT)
    date = models.DateField()
    reference = models.CharField(max_length=120)
    evidence = models.TextField(max_length=4000)
    scoped_references = ("journal",)

    class Meta:
        abstract = True

    def clean(self):
        super().clean()
        old = self._old()
        if old:
            self._unchanged(old, tuple(field.attname for field in self._meta.concrete_fields if field.name not in {"id", "created_at"}))
        elif not getattr(self, "_service_transition", False):
            raise ValidationError("Gunakan layanan akuntansi untuk mencatat peristiwa keuangan.")
        if self.journal_id and (self.journal.status != "posted" or self.journal.date != self.date):
            raise ValidationError("Peristiwa harus terhubung ke jurnal terposting pada tanggal yang sama.")

    def delete(self, *args, **kwargs):
        raise ValidationError("Riwayat akuntansi tidak boleh dihapus.")


class BankPosting(FinancialEvent):
    transaction = models.ForeignKey(BankTransaction, on_delete=models.PROTECT, related_name="postings")
    kind = models.CharField(max_length=30, choices=[(value, label) for value, label in [
        ("bank_fee", "Biaya bank"), ("owner_funding", "Setoran modal pemilik"),
        ("transfer_out", "Transfer keluar"), ("transfer_in", "Transfer masuk"),
        ("customer_advance", "Uang muka pelanggan"), ("customer_refund", "Pengembalian pelanggan"),
        ("withheld_supplier", "Pembayaran bersih pemasok"), ("tax_remittance", "Penyetoran potongan pajak")]])
    signed_amount = models.DecimalField(max_digits=20, decimal_places=2)
    scoped_references = ("journal", "transaction")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "reference"], name="finance_bank_posting_reference"),
                       models.CheckConstraint(condition=~Q(signed_amount=0), name="finance_bank_posting_nonzero")]

    def clean(self):
        super().clean()
        if self.transaction_id and (self.date != self.transaction.date or self.signed_amount * self.transaction.amount <= ZERO):
            raise ValidationError("Tanggal dan arah alokasi harus sesuai bukti bank.")


class BankOpening(FinancialEvent):
    account = models.OneToOneField(BankAccount, on_delete=models.PROTECT, related_name="opening_record")
    amount = models.DecimalField(max_digits=20, decimal_places=2)
    scoped_references = ("journal", "account")


class PrepaymentRelease(FinancialEvent):
    bill = models.ForeignKey(Bill, on_delete=models.PROTECT, related_name="prepayment_releases")
    amount = models.DecimalField(max_digits=20, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    scoped_references = ("journal", "bill")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "reference"], name="finance_prepayment_reference")]


class CustomerAdvance(FinancialEvent):
    party = models.ForeignKey(Party, on_delete=models.PROTECT, related_name="customer_advances")
    bank_posting = models.OneToOneField(BankPosting, on_delete=models.PROTECT)
    amount = models.DecimalField(max_digits=20, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    scoped_references = ("journal", "party", "bank_posting")

    @property
    def remaining_amount(self):
        used = self.applications.aggregate(value=Sum("amount"))["value"] or ZERO
        refunded = self.refunds.aggregate(value=Sum("amount"))["value"] or ZERO
        return self.amount - used - refunded


class AdvanceApplication(FinancialEvent):
    advance = models.ForeignKey(CustomerAdvance, on_delete=models.PROTECT, related_name="applications")
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name="advance_applications")
    amount = models.DecimalField(max_digits=20, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    scoped_references = ("journal", "advance", "invoice")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "reference"], name="finance_advance_application_reference")]


class InvoiceCancellation(FinancialEvent):
    invoice = models.OneToOneField(Invoice, on_delete=models.PROTECT, related_name="cancellation")
    amount = models.DecimalField(max_digits=20, decimal_places=2)
    unpaid_amount = models.DecimalField(max_digits=20, decimal_places=2)
    refund_amount = models.DecimalField(max_digits=20, decimal_places=2)
    scoped_references = ("journal", "invoice")

    @property
    def refund_remaining(self):
        return self.refund_amount - (self.refunds.aggregate(value=Sum("amount"))["value"] or ZERO)


class CustomerRefund(FinancialEvent):
    advance = models.ForeignKey(CustomerAdvance, null=True, blank=True, on_delete=models.PROTECT, related_name="refunds")
    cancellation = models.ForeignKey(InvoiceCancellation, null=True, blank=True, on_delete=models.PROTECT, related_name="refunds")
    bank_posting = models.OneToOneField(BankPosting, on_delete=models.PROTECT)
    amount = models.DecimalField(max_digits=20, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    scoped_references = ("journal", "advance", "cancellation", "bank_posting")

    class Meta:
        constraints = [models.CheckConstraint(condition=(Q(advance__isnull=False, cancellation__isnull=True) | Q(advance__isnull=True, cancellation__isnull=False)), name="finance_refund_one_source")]


class WithholdingSettlement(FinancialEvent):
    bill = models.OneToOneField(Bill, on_delete=models.PROTECT, related_name="withholding_settlement")
    obligation = models.OneToOneField("taxes.TaxObligation", on_delete=models.PROTECT, related_name="withholding_settlement")
    bank_posting = models.OneToOneField(BankPosting, on_delete=models.PROTECT)
    gross_amount = models.DecimalField(max_digits=20, decimal_places=2)
    net_amount = models.DecimalField(max_digits=20, decimal_places=2)
    withholding_amount = models.DecimalField(max_digits=20, decimal_places=2)
    scoped_references = ("journal", "bill", "obligation", "bank_posting")


class TaxRemittance(FinancialEvent):
    obligation = models.ForeignKey("taxes.TaxObligation", on_delete=models.PROTECT, related_name="remittances")
    bank_posting = models.OneToOneField(BankPosting, on_delete=models.PROTECT)
    amount = models.DecimalField(max_digits=20, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    scoped_references = ("journal", "obligation", "bank_posting")
