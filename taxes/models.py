import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class TaxValidatedQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("Gunakan layanan pemeriksaan pajak untuk mengubah catatan.")

    def bulk_create(self, *args, **kwargs):
        raise ValidationError("Setiap catatan pajak harus diperiksa satu per satu.")

    def bulk_update(self, *args, **kwargs):
        raise ValidationError("Gunakan layanan pemeriksaan pajak untuk mengubah catatan.")


class ValidatedModel(models.Model):
    """Validate normal application writes; bulk writes are not public interfaces."""

    class Meta:
        abstract = True

    objects = TaxValidatedQuerySet.as_manager()

    def save(self, *args, **kwargs):
        if self.pk and self.organization_id:
            original = type(self).objects.filter(pk=self.pk).values_list("organization_id", flat=True).first()
            if original is not None and original != self.organization_id:
                raise ValidationError("Organisasi catatan pajak tidak dapat diganti.")
        self.full_clean()
        return super().save(*args, **kwargs)


class TaxProfile(ValidatedModel):
    class Regime(models.TextChoices):
        UNDECIDED = "undecided", "Menunggu pemeriksaan dokumen"
        FINAL = "final", "PPh final omzet — telah ditinjau"
        NORMAL = "normal", "PPh badan umum — telah ditinjau"

    organization = models.OneToOneField("core.Organization", on_delete=models.CASCADE, related_name="tax_profile")
    registration_date = models.DateField(null=True, blank=True)
    registration_evidence = models.CharField(max_length=300, blank=True)
    vat_status_evidence = models.CharField(max_length=300, blank=True)
    vat_status_effective_from = models.DateField(null=True, blank=True)
    regime = models.CharField(max_length=12, choices=Regime.choices, default=Regime.UNDECIDED)
    effective_from = models.DateField(null=True, blank=True)
    final_regime_start_date = models.DateField(null=True, blank=True)
    final_regime_end_date = models.DateField(null=True, blank=True)
    transition_evidence = models.CharField(max_length=300, blank=True)
    review_note = models.TextField(blank=True, max_length=4000)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="reviewed_tax_profiles")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        super().clean()
        if self.regime == self.Regime.UNDECIDED:
            return
        if self.pk:
            old = type(self).objects.filter(pk=self.pk).first()
            approval_inputs = ("registration_date", "registration_evidence", "regime", "effective_from", "final_regime_start_date", "final_regime_end_date", "transition_evidence", "vat_status_effective_from", "vat_status_evidence")
            if old and old.regime != self.Regime.UNDECIDED and old.reviewed_at == self.reviewed_at and any(getattr(old, field) != getattr(self, field) for field in approval_inputs):
                raise ValidationError("Perubahan bukti atau aturan memerlukan pemeriksaan ulang. Kembalikan profil ke status menunggu pemeriksaan terlebih dahulu.")
        required = {
            "registration_date": self.registration_date,
            "registration_evidence": self.registration_evidence.strip(),
            "effective_from": self.effective_from,
            "review_note": self.review_note.strip(),
            "reviewed_by": self.reviewed_by_id,
            "reviewed_at": self.reviewed_at,
        }
        if self.regime == self.Regime.FINAL:
            required.update({
                "final_regime_start_date": self.final_regime_start_date,
                "final_regime_end_date": self.final_regime_end_date,
                "transition_evidence": self.transition_evidence.strip(),
            })
        errors = {name: "Wajib dilengkapi pemeriksa pajak sebelum aturan diaktifkan." for name, value in required.items() if not value}
        if self.final_regime_start_date and self.final_regime_end_date and self.final_regime_end_date < self.final_regime_start_date:
            errors["final_regime_end_date"] = "Akhir masa fasilitas harus setelah awal masa fasilitas."
        if self.regime == self.Regime.FINAL and self.effective_from and self.final_regime_start_date and self.final_regime_end_date and not self.final_regime_start_date <= self.effective_from <= self.final_regime_end_date:
            errors["effective_from"] = "Tanggal aktivasi harus berada dalam masa fasilitas yang telah diperiksa."
        if self.reviewed_by_id and self.organization_id:
            from core.models import Membership
            if not Membership.objects.filter(user_id=self.reviewed_by_id, organization_id=self.organization_id, role="reviewer").exists():
                errors["reviewed_by"] = "Persetujuan membutuhkan pemeriksa pajak organisasi ini."
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"Profil pajak {self.organization_id}"


class TaxObligation(ValidatedModel):
    class TaxType(models.TextChoices):
        PPH23 = "pph23", "PPh 23"
        FINAL = "final_turnover", "PPh final omzet"
        CORPORATE = "corporate_income", "PPh badan"
        PPH21 = "pph21", "PPh 21"
        PPH26 = "pph26", "PPh 26"
        VAT = "vat", "PPN — kasus yang perlu ditinjau"

    class Direction(models.TextChoices):
        PAYABLE = "payable", "Potongan kepada pemasok — kewajiban setor"
        RECEIVABLE = "receivable", "Potongan oleh pelanggan — calon kredit pajak"
        OWN = "own_tax", "Pajak perusahaan sendiri"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draf"
        REVIEW = "review", "Perlu pemeriksaan"
        APPROVED = "approved", "Disetujui pemeriksa"

    organization = models.ForeignKey("core.Organization", on_delete=models.CASCADE, related_name="tax_obligations")
    period = models.DateField(help_text="Hari pertama masa pajak")
    tax_type = models.CharField(max_length=24, choices=TaxType.choices)
    direction = models.CharField(max_length=12, choices=Direction.choices)
    base = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    rate = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True, help_text="Pecahan desimal, misalnya 0.02; kosong jika belum diputuskan")
    amount = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    source_reference = models.CharField(max_length=300, blank=True)
    rule_reference = models.CharField(max_length=300, blank=True)
    note = models.TextField(blank=True, max_length=4000)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="reviewed_tax_obligations")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    payment_reference = models.CharField(max_length=200, blank=True, help_text="BPN/NTPN atau bukti alokasi deposit yang diverifikasi")
    payment_verified_at = models.DateTimeField(null=True, blank=True)
    filing_reference = models.CharField(max_length=200, blank=True, help_text="BPE atau bukti penerimaan resmi")
    filing_verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["period", "tax_type", "direction", "pk"]
        indexes = [models.Index(fields=["organization", "period"])]
        constraints = [
            models.CheckConstraint(condition=models.Q(base__gte=0) | models.Q(base__isnull=True), name="tax_base_nonnegative"),
            models.CheckConstraint(condition=models.Q(amount__gte=0) | models.Q(amount__isnull=True), name="tax_amount_nonnegative"),
            models.CheckConstraint(condition=models.Q(rate__gte=0, rate__lte=1) | models.Q(rate__isnull=True), name="tax_rate_valid"),
        ]

    @property
    def payment_status(self):
        if self.direction == self.Direction.RECEIVABLE:
            return "Tidak disetor oleh OSEE"
        return "Bukti pembayaran diverifikasi" if self.payment_reference and self.payment_verified_at else "Pembayaran belum diverifikasi"

    @property
    def filing_status(self):
        return "Bukti pelaporan diverifikasi" if self.filing_reference and self.filing_verified_at else "Pelaporan belum diverifikasi"

    def clean(self):
        super().clean()
        errors = {}
        if self.pk:
            old = type(self).objects.filter(pk=self.pk).first()
            inputs = ("period", "tax_type", "direction", "base", "rate", "amount", "source_reference", "rule_reference")
            if old and old.status == self.Status.APPROVED and self.status == self.Status.APPROVED and old.reviewed_at == self.reviewed_at and any(getattr(old, field) != getattr(self, field) for field in inputs):
                errors["status"] = "Perubahan kertas kerja yang disetujui memerlukan pemeriksaan ulang."
        if self.period and self.period.day != 1:
            errors["period"] = "Masa pajak harus menggunakan tanggal pertama bulan."
        for field in ("base", "amount"):
            value = getattr(self, field)
            if value is not None and value < 0:
                errors[field] = "Nilai tidak boleh negatif."
        if self.rate is not None and not 0 <= self.rate <= 1:
            errors["rate"] = "Tarif harus berupa pecahan antara 0 dan 1."
        for prefix in ("payment", "filing"):
            if bool(getattr(self, f"{prefix}_reference").strip()) != bool(getattr(self, f"{prefix}_verified_at")):
                errors[f"{prefix}_reference"] = "Referensi resmi dan waktu verifikasi harus dilengkapi bersama."
        if self.direction == self.Direction.RECEIVABLE and self.payment_reference:
            errors["payment_reference"] = "Potongan oleh pelanggan bukan pembayaran pajak keluar OSEE."
        if self.status == self.Status.APPROVED:
            if any(value is None for value in (self.base, self.rate, self.amount)) or not self.source_reference.strip() or not self.rule_reference.strip() or not self.reviewed_by_id or not self.reviewed_at:
                errors["status"] = "Dasar, tarif, nominal, bukti, aturan, dan pemeriksa wajib tersedia."
            elif self.organization_id:
                from core.models import Membership
                if not Membership.objects.filter(user_id=self.reviewed_by_id, organization_id=self.organization_id, role="reviewer").exists():
                    errors["reviewed_by"] = "Pemeriksa harus memiliki akses pemeriksa pada organisasi ini."
            if self.tax_type == self.TaxType.FINAL and self.organization_id:
                profile = TaxProfile.objects.filter(organization_id=self.organization_id).first()
                if not profile or profile.regime != TaxProfile.Regime.FINAL or not profile.reviewed_at or not profile.effective_from or self.period < profile.effective_from or not profile.final_regime_start_date or self.period < profile.final_regime_start_date or not profile.final_regime_end_date or self.period > profile.final_regime_end_date:
                    errors["status"] = "PPh final tidak dapat disetujui sebelum kelayakan masa ini ditinjau."
        if (self.payment_reference or self.filing_reference) and self.status != self.Status.APPROVED:
            errors["status"] = "Kertas kerja harus disetujui sebelum bukti penyelesaian dicatat."
        if errors:
            raise ValidationError(errors)


class TaxSource(models.Model):
    slug = models.SlugField(max_length=80, unique=True)
    title = models.CharField(max_length=250)
    url = models.URLField(max_length=600)
    issuer = models.CharField(max_length=100, default="Direktorat Jenderal Pajak / JDIH")
    article = models.CharField(max_length=200, blank=True)
    summary = models.TextField(max_length=2500)
    topics = models.JSONField(default=list)
    reviewed_on = models.DateField()
    effective_from = models.DateField(null=True, blank=True)
    effective_until = models.DateField(null=True, blank=True)
    approved = models.BooleanField(default=False)

    class Meta:
        ordering = ["slug"]


class ChatConversation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey("core.Organization", on_delete=models.CASCADE)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    title = models.CharField(max_length=100, default="Tanya pajak")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [models.Index(fields=["organization", "created_by"])]


class ChatMessage(models.Model):
    conversation = models.ForeignKey(ChatConversation, on_delete=models.CASCADE, related_name="messages")
    role = models.CharField(max_length=12, choices=[("user", "Pengguna"), ("assistant", "Asisten")])
    content = models.TextField(max_length=6000)
    mode = models.CharField(max_length=40, blank=True)
    source_ids = models.JSONField(default=list)
    metadata = models.JSONField(default=dict)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["id"]


class ChatQuota(models.Model):
    """Atomic reservations bound requests, including failed provider requests."""

    key = models.CharField(max_length=160, unique=True)
    window_start = models.DateTimeField()
    count = models.PositiveIntegerField(default=0)
