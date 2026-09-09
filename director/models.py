"""Persisted planning records; these tables never post to the financial ledger."""

from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Sum

from finance.models import ScopedModel


ZERO = Decimal("0.00")
CHANNEL_CHOICES = [("mitra", "Mitra"), ("meta_ads", "Meta Ads"), ("google_ads", "Google Ads"),
                   ("whatsapp", "WhatsApp"), ("seo", "SEO"), ("sales", "Sales")]


class PlanningRecord(ScopedModel):
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not getattr(self, "_service_write", False):
            raise ValidationError("Gunakan layanan Direktur untuk menyimpan perubahan.")
        return super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        self._unchanged(self._old(), ("created_by_id",))

    def delete(self, *args, **kwargs):
        raise ValidationError("Catatan perencanaan disimpan sebagai riwayat; gunakan pembatalan atau revisi.")


class Objective(PlanningRecord):
    title = models.CharField(max_length=200)
    metric_key = models.CharField(max_length=100)
    target = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    period_start = models.DateField()
    period_end = models.DateField()
    owner_name = models.CharField(max_length=150)
    status = models.CharField(max_length=20, choices=[("draft", "Draf"), ("active", "Aktif"), ("paused", "Ditunda"), ("completed", "Selesai")], default="draft")
    progress_notes = models.TextField(max_length=4000, blank=True)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["-period_start", "-pk"]

    def clean(self):
        super().clean()
        if self.period_start and self.period_end and self.period_end < self.period_start:
            raise ValidationError("Akhir periode sasaran tidak boleh sebelum awal periode.")
        if self.status == "completed" and not self.progress_notes.strip():
            raise ValidationError("Catat hasil sasaran sebelum menandai selesai.")


class Decision(PlanningRecord):
    title = models.CharField(max_length=200)
    problem = models.TextField(max_length=4000, blank=True)
    evidence = models.TextField(max_length=4000, blank=True)
    option = models.TextField(max_length=4000, blank=True)
    risks = models.TextField(max_length=4000, blank=True)
    owner_name = models.CharField(max_length=150, blank=True)
    due_date = models.DateField(null=True, blank=True)
    review_date = models.DateField(null=True, blank=True)
    amount = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(ZERO)])
    status = models.CharField(max_length=20, choices=[("draft", "Draf"), ("submitted", "Diajukan"), ("reviewed", "Ditinjau Finance"), ("approved", "Disetujui"), ("rejected", "Ditolak"), ("active", "Aktif"), ("completed", "Selesai")], default="draft")
    version = models.PositiveIntegerField(default=1)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    review_notes = models.TextField(max_length=4000, blank=True)
    approval_notes = models.TextField(max_length=4000, blank=True)
    execution_notes = models.TextField(max_length=4000, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]

    def clean(self):
        super().clean()
        old = self._old()
        if old and old.status != "draft":
            self._unchanged(old, ("title", "problem", "evidence", "option", "risks", "owner_name", "due_date", "review_date", "amount"))
        if self.review_date and self.due_date and self.review_date < self.due_date:
            raise ValidationError("Tanggal evaluasi hasil tidak boleh sebelum tenggat pelaksanaan.")


class Budget(PlanningRecord):
    title = models.CharField(max_length=200)
    year = models.PositiveIntegerField(validators=[MinValueValidator(2000), MaxValueValidator(2200)])
    month = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(12)])
    channel = models.CharField(max_length=20, choices=CHANNEL_CHOICES)
    amount = models.DecimalField(max_digits=20, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    status = models.CharField(max_length=20, choices=[("draft", "Draf"), ("submitted", "Diajukan"), ("approved", "Disetujui")], default="draft")
    version = models.PositiveIntegerField(default=1)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    approval_notes = models.TextField(max_length=4000, blank=True)

    class Meta:
        ordering = ["-year", "-month", "-pk"]

    def clean(self):
        super().clean()
        old = self._old()
        if old and old.status != "draft":
            self._unchanged(old, ("title", "year", "month", "channel", "amount"))

    def total_for(self, kind):
        return self.entries.filter(kind=kind, voided_at__isnull=True).aggregate(total=Sum("amount"))["total"] or ZERO

    @property
    def incurred(self):
        return self.total_for("incurred")

    @property
    def open_commitments(self):
        return self.total_for("open_commitment")

    @property
    def reservations(self):
        return self.total_for("reservation")

    @property
    def payments(self):
        return self.total_for("payment")

    @property
    def consumed(self):
        return self.incurred + self.open_commitments + self.reservations

    @property
    def available(self):
        return self.amount - self.consumed


class BudgetEntry(PlanningRecord):
    budget = models.ForeignKey(Budget, on_delete=models.PROTECT, related_name="entries")
    kind = models.CharField(max_length=24, choices=[("incurred", "Biaya terjadi"), ("open_commitment", "Komitmen tersisa"), ("reservation", "Reservasi"), ("payment", "Pembayaran tercatat")])
    amount = models.DecimalField(max_digits=20, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    date = models.DateField()
    description = models.CharField(max_length=500)
    reference = models.CharField(max_length=160, blank=True)
    idempotency_key = models.CharField(max_length=100)
    replaces = models.ForeignKey("self", on_delete=models.PROTECT, null=True, blank=True, related_name="replacements")
    voided_at = models.DateTimeField(null=True, blank=True)
    void_reason = models.CharField(max_length=500, blank=True)
    scoped_references = ("budget", "replaces")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "idempotency_key"], name="director_budget_entry_key")]
        ordering = ["-date", "-pk"]

    def clean(self):
        super().clean()
        old = self._old()
        self._unchanged(old, ("budget_id", "kind", "amount", "date", "description", "reference", "idempotency_key", "replaces_id"))
        if old and old.voided_at:
            self._unchanged(old, ("voided_at", "void_reason"))
        if self.replaces_id and self.replaces.budget_id != self.budget_id:
            raise ValidationError("Entri pengganti harus berada pada budget yang sama.")


class CashPlan(PlanningRecord):
    title = models.CharField(max_length=200)
    as_of = models.DateField()
    opening_balance = models.DecimalField(max_digits=20, decimal_places=2)
    reserve = models.DecimalField(max_digits=20, decimal_places=2, validators=[MinValueValidator(ZERO)], default=ZERO)
    unknown_obligations = models.TextField(max_length=4000, blank=True)
    basis = models.CharField(max_length=20, choices=[("assumed", "Simulasi dengan saldo asumsi")], default="assumed")
    version = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["-as_of", "-pk"]


class CashPlanLine(PlanningRecord):
    plan = models.ForeignKey(CashPlan, on_delete=models.PROTECT, related_name="lines")
    direction = models.CharField(max_length=10, choices=[("inflow", "Penerimaan rencana"), ("outflow", "Pengeluaran rencana")])
    amount = models.DecimalField(max_digits=20, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    date = models.DateField()
    category = models.CharField(max_length=80)
    description = models.CharField(max_length=500)
    source_reference = models.CharField(max_length=200, blank=True)
    evidence = models.TextField(max_length=4000, blank=True)
    status = models.CharField(max_length=10, choices=[("planned", "Rencana"), ("void", "Dibatalkan")], default="planned")
    replaces = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="replacements")
    void_reason = models.CharField(max_length=500, blank=True)
    scoped_references = ("plan", "replaces")

    class Meta:
        ordering = ["date", "pk"]

    def clean(self):
        super().clean()
        old = self._old()
        self._unchanged(old, ("plan_id", "direction", "amount", "date", "category", "description", "source_reference", "evidence", "replaces_id"))
        if old and old.status == "void":
            self._unchanged(old, ("status", "void_reason"))
        if self.replaces_id and self.replaces.plan_id != self.plan_id:
            raise ValidationError("Baris pengganti harus berada pada rencana kas yang sama.")


class Scenario(PlanningRecord):
    title = models.CharField(max_length=200)
    kind = models.CharField(max_length=30, choices=[("price_cost", "Harga, biaya, dan volume")])
    inputs = models.JSONField(default=dict)
    results = models.JSONField(default=dict)
    formula_version = models.CharField(max_length=80)

    class Meta:
        ordering = ["-created_at", "-pk"]

    def clean(self):
        super().clean()
        self._unchanged(self._old(), ("title", "kind", "inputs", "results", "formula_version"))


class MarketingObservation(PlanningRecord):
    date = models.DateField()
    channel = models.CharField(max_length=20, choices=CHANNEL_CHOICES)
    spend = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(ZERO)])
    leads = models.PositiveIntegerField(null=True, blank=True)
    paid_orders = models.PositiveIntegerField(null=True, blank=True)
    reported_value = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(ZERO)])
    source = models.CharField(max_length=200)
    reference = models.CharField(max_length=200)
    basis = models.CharField(max_length=20, choices=[("reported", "Dilaporkan sumber"), ("provisional", "Sementara")], default="reported")
    status = models.CharField(max_length=10, choices=[("active", "Aktif"), ("void", "Dibatalkan")], default="active")
    replaces = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="replacements")
    void_reason = models.CharField(max_length=500, blank=True)
    scoped_references = ("replaces",)

    class Meta:
        ordering = ["-date", "-pk"]

    def clean(self):
        super().clean()
        old = self._old()
        self._unchanged(old, ("date", "channel", "spend", "leads", "paid_orders", "reported_value", "source", "reference", "basis", "replaces_id"))
        if old and old.status == "void":
            self._unchanged(old, ("status", "void_reason"))


class DirectorAIPolicy(ScopedModel):
    organization = models.OneToOneField("core.Organization", on_delete=models.PROTECT, related_name="director_ai_policy")
    enabled = models.BooleanField(default=False)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    approved_at = models.DateTimeField(null=True, blank=True)
    policy_version = models.CharField(max_length=20, default="1")
    monthly_budget_usd = models.DecimalField(max_digits=12, decimal_places=6, default=0, validators=[MinValueValidator(0)])
    request_cap_usd = models.DecimalField(max_digits=12, decimal_places=6, default=0, validators=[MinValueValidator(0)])
    disclosure_version = models.CharField(max_length=80, default="director-aggregate-v1")
    disclosure_text = models.TextField(blank=True)


class DirectorConversation(ScopedModel):
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    title = models.CharField(max_length=160, blank=True)


class DirectorMessage(ScopedModel):
    conversation = models.ForeignKey(DirectorConversation, on_delete=models.PROTECT, related_name="messages")
    role = models.CharField(max_length=12, choices=[("user", "Pengguna"), ("assistant", "AI")])
    content = models.TextField()
    response = models.JSONField(default=dict, blank=True)
    scoped_references = ("conversation",)

    class Meta:
        ordering = ["created_at", "pk"]


class AIUsageReservation(ScopedModel):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    request_id = models.CharField(max_length=80)
    amount_usd = models.DecimalField(max_digits=12, decimal_places=6, validators=[MinValueValidator(0)])
    status = models.CharField(max_length=20, choices=[("reserved", "Dipesan"), ("dispatching", "Dikirim"), ("completed", "Selesai"), ("outcome_unknown", "Hasil belum diketahui"), ("released", "Dilepas")], default="reserved")
    actual_cost = models.DecimalField(max_digits=12, decimal_places=6, null=True, blank=True, validators=[MinValueValidator(0)])

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "request_id"], name="director_ai_request_key")]


class AIUsageCounter(models.Model):
    key = models.CharField(max_length=200, unique=True)
    reserved_usd = models.DecimalField(max_digits=14, decimal_places=6, default=0, validators=[MinValueValidator(0)])
    created_at = models.DateTimeField(auto_now_add=True)
