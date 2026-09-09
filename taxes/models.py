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

    def delete(self):
        raise ValidationError("Catatan audit pajak tidak boleh dihapus massal.")


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
    taxpayer_reference = models.CharField(max_length=100, blank=True)
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
    external_review = models.ForeignKey("ExternalTaxReview", null=True, blank=True, on_delete=models.PROTECT, related_name="profiles")

    def clean(self):
        super().clean()
        old = type(self).objects.filter(pk=self.pk).first() if self.pk else None
        if old and old.reviewed_at and not getattr(self, "_tax_write", False):
            if any(getattr(old, field.attname) != getattr(self, field.attname) for field in self._meta.fields if field.name != "updated_at"):
                raise ValidationError("Profil yang ditinjau hanya dapat direvisi melalui alur pemeriksaan terdokumentasi.")
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
            if not Membership.objects.filter(user_id=self.reviewed_by_id, organization_id=self.organization_id, user__is_active=True, role__in=["owner", "reviewer"]).exists():
                errors["reviewed_by"] = "Persetujuan membutuhkan pemeriksa pajak organisasi ini."
        if not getattr(self, "_tax_write", False):
            errors["regime"] = "Gunakan alur pencatatan pemeriksaan profesional sebelum mengaktifkan aturan."
        if not self.external_review_id:
            errors["external_review"] = "Laporan pemeriksaan profesional wajib ditautkan."
        if self.regime == self.Regime.FINAL:
            from .rules import validate_final_profile
            try:
                validate_final_profile(self)
            except ValidationError as exc:
                errors["regime"] = exc.messages
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
    source_bill = models.ForeignKey("finance.Bill", null=True, blank=True, on_delete=models.PROTECT, related_name="tax_obligations")
    external_review = models.ForeignKey("ExternalTaxReview", null=True, blank=True, on_delete=models.PROTECT, related_name="obligations")
    rule_code = models.CharField(max_length=50, blank=True)
    calculation_version = models.CharField(max_length=60, blank=True)
    object_code = models.CharField(max_length=40, blank=True)
    source_evidence = models.ForeignKey("TaxEvidence", null=True, blank=True, on_delete=models.PROTECT, related_name="obligations")
    prepared_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="prepared_tax_obligations")
    reporting_method = models.CharField(max_length=24, choices=[("return_receipt", "Bukti penerimaan SPT"), ("validated_self_payment", "Pelaporan melalui pembayaran final tervalidasi")], default="return_receipt")

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
        return "Bukti pembayaran diverifikasi" if self.pk and self.verifications.filter(kind="payment").exists() else "Pembayaran belum diverifikasi"

    @property
    def filing_status(self):
        if self.pk and self.reporting_method == "validated_self_payment" and self.verifications.filter(kind="payment").exists():
            return "Pelaporan melalui pembayaran tervalidasi — PMK 81 Pasal 171(4)"
        return "Bukti pelaporan diverifikasi" if self.pk and self.verifications.filter(kind="filing").exists() else "Pelaporan belum diverifikasi"

    def clean(self):
        super().clean()
        errors = {}
        if self.pk:
            old = type(self).objects.filter(pk=self.pk).first()
            if old and old.status == self.Status.APPROVED and any(getattr(old, field.attname) != getattr(self, field.attname) for field in self._meta.fields if field.name not in ("updated_at",)):
                raise ValidationError("Kertas kerja disetujui bersifat tetap; bukti penyelesaian dicatat terpisah.")
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
        if self.source_bill_id and self.source_bill.organization_id != self.organization_id:
            errors["source_bill"] = "Tagihan tidak sesuai perusahaan."
        if self.source_evidence_id and self.source_evidence.organization_id != self.organization_id:
            errors["source_evidence"] = "Dokumen tidak sesuai perusahaan."
        if self.payment_reference or self.filing_reference or self.payment_verified_at or self.filing_verified_at:
            errors["status"] = "Catat verifikasi melalui layanan bukti resmi; kolom referensi lama tidak mengesahkan pembayaran/pelaporan."
        for prefix in ("payment", "filing"):
            if bool(getattr(self, f"{prefix}_reference").strip()) != bool(getattr(self, f"{prefix}_verified_at")):
                errors[f"{prefix}_reference"] = "Referensi resmi dan waktu verifikasi harus dilengkapi bersama."
        if self.direction == self.Direction.RECEIVABLE and self.payment_reference:
            errors["payment_reference"] = "Potongan oleh pelanggan bukan pembayaran pajak keluar OSEE."
        if self.status == self.Status.APPROVED:
            if not self.source_evidence_id or not self.object_code or not self.rule_code:
                errors["status"] = "Dokumen sumber, kode objek yang diperiksa, dan versi aturan wajib tersedia."
            if any(value is None for value in (self.base, self.rate, self.amount)) or not self.source_reference.strip() or not self.rule_reference.strip() or not self.reviewed_by_id or not self.reviewed_at:
                errors["status"] = "Dasar, tarif, nominal, bukti, aturan, dan pemeriksa wajib tersedia."
            elif self.organization_id:
                from core.models import Membership
                if not Membership.objects.filter(user_id=self.reviewed_by_id, organization_id=self.organization_id, role__in=["owner", "reviewer"], user__is_active=True).exists():
                    errors["reviewed_by"] = "Pemeriksa harus memiliki akses pemeriksa pada organisasi ini."
            if not getattr(self, "_tax_write", False) or not self.external_review_id:
                errors["status"] = "Persetujuan memerlukan layanan dan laporan pemeriksaan profesional."
            from .rules import calculate_tax, validate_monthly_period
            try:
                validate_monthly_period(self.period)
                result = calculate_tax(self.rule_code, self.base)
                if self.base != result["base"] or self.amount != result["amount"] or self.rate != result["rate"] or self.calculation_version != result["version"]:
                    errors["amount"] = "Nominal/tarif tidak sama dengan hasil perhitungan tervalidasi."
                expected_type = "final_turnover" if self.rule_code == "final_turnover_005" else "pph23"
                expected_method = "validated_self_payment" if expected_type == "final_turnover" else "return_receipt"
                valid_direction = self.direction == "own_tax" if expected_type == "final_turnover" else self.direction in ("payable", "receivable")
                if self.rule_code == "no_withholding" or self.tax_type != expected_type or self.reporting_method != expected_method or not valid_direction:
                    errors["status"] = "Jenis, arah, dan metode pelaporan harus sesuai klasifikasi perhitungan yang didukung."
            except ValidationError as exc:
                errors["amount"] = exc.messages
            if self.tax_type == self.TaxType.FINAL and self.organization_id:
                from .services import profile_gates
                profile, gates = profile_gates(self.organization, self.period)
                if gates or not profile or profile.regime != TaxProfile.Regime.FINAL or not profile.reviewed_at or not profile.effective_from or self.period < profile.effective_from or not profile.final_regime_start_date or self.period < profile.final_regime_start_date or not profile.final_regime_end_date or self.period > profile.final_regime_end_date:
                    errors["status"] = "PPh final tidak dapat disetujui sebelum kelayakan masa ini ditinjau."
        if (self.payment_reference or self.filing_reference) and self.status != self.Status.APPROVED:
            errors["status"] = "Kertas kerja harus disetujui sebelum bukti penyelesaian dicatat."
        if errors:
            raise ValidationError(errors)

    def delete(self, *args, **kwargs):
        raise ValidationError("Kertas kerja pajak tidak dihapus; simpan riwayat pemeriksaannya.")


class ImmutableTaxRecord(ValidatedModel):
    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError("Bukti/keputusan pajak bersifat tetap; catat versi baru.")
        if not getattr(self, "_tax_write", False):
            raise ValidationError("Gunakan layanan pajak yang berwenang.")
        try:
            return super().save(*args, **kwargs)
        finally:
            self.__dict__.pop("_tax_write", None)

    def delete(self, *args, **kwargs):
        raise ValidationError("Bukti audit pajak tidak boleh dihapus.")

    def clean(self):
        super().clean()
        for field in self._meta.fields:
            if isinstance(field, models.ForeignKey) and getattr(self, field.attname, None):
                related = getattr(self, field.name)
                if hasattr(related, "organization_id") and related.organization_id != self.organization_id:
                    raise ValidationError({field.name: "Referensi harus berada pada perusahaan yang sama."})


class TaxEvidence(ImmutableTaxRecord):
    organization = models.ForeignKey("core.Organization", on_delete=models.PROTECT)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    original_filename = models.CharField(max_length=200)
    sha256 = models.CharField(max_length=64)
    content = models.BinaryField()
    created_at = models.DateTimeField(auto_now_add=True)


class ExternalTaxReview(ImmutableTaxRecord):
    organization = models.ForeignKey("core.Organization", on_delete=models.PROTECT)
    evidence = models.ForeignKey(TaxEvidence, on_delete=models.PROTECT)
    professional_name = models.CharField(max_length=200)
    qualification_reference = models.CharField(max_length=300)
    reviewed_on = models.DateField()
    statement = models.TextField(max_length=4000)
    subject_type = models.CharField(max_length=30)
    subject_id = models.PositiveBigIntegerField()
    subject_digest = models.CharField(max_length=64)
    decision = models.JSONField()
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    recorded_at = models.DateTimeField(auto_now_add=True)

    def clean(self):
        if self.evidence_id and self.evidence.organization_id != self.organization_id:
            raise ValidationError("Dokumen pemeriksaan tidak sesuai perusahaan.")


class TaxProfileDecision(ImmutableTaxRecord):
    organization = models.ForeignKey("core.Organization", on_delete=models.PROTECT)
    profile = models.ForeignKey(TaxProfile, on_delete=models.PROTECT, related_name="decisions")
    external_review = models.OneToOneField(ExternalTaxReview, on_delete=models.PROTECT)
    effective_from = models.DateField()
    snapshot = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)


class BillTaxDecision(ImmutableTaxRecord):
    organization = models.ForeignKey("core.Organization", on_delete=models.PROTECT)
    bill = models.OneToOneField("finance.Bill", on_delete=models.PROTECT, related_name="tax_decision")
    external_review = models.OneToOneField(ExternalTaxReview, on_delete=models.PROTECT)
    obligation = models.OneToOneField(TaxObligation, null=True, blank=True, on_delete=models.PROTECT, related_name="bill_decision")
    rule_code = models.CharField(max_length=50)
    base = models.DecimalField(max_digits=18, decimal_places=2)
    rate = models.DecimalField(max_digits=9, decimal_places=6)
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    period = models.DateField()
    reason = models.TextField(max_length=4000)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)


class TaxVerification(ImmutableTaxRecord):
    organization = models.ForeignKey("core.Organization", on_delete=models.PROTECT)
    obligation = models.ForeignKey(TaxObligation, on_delete=models.PROTECT, related_name="verifications")
    kind = models.CharField(max_length=12, choices=[("payment", "Pembayaran"), ("filing", "Pelaporan"), ("credit", "Bukti potong pelanggan")])
    evidence = models.ForeignKey(TaxEvidence, on_delete=models.PROTECT)
    reference = models.CharField(max_length=200)
    taxpayer_reference = models.CharField(max_length=100)
    period = models.DateField()
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    obligation_digest = models.CharField(max_length=64)
    verifier = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    matched_at = models.DateTimeField(auto_now_add=True)
    note = models.TextField(max_length=4000)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["obligation", "kind"], name="tax_one_verification_kind"), models.UniqueConstraint(fields=["organization", "kind", "reference"], name="tax_unique_verified_reference")]


class AnnualTaxWorkpaper(ImmutableTaxRecord):
    organization = models.ForeignKey("core.Organization", on_delete=models.PROTECT)
    year = models.PositiveSmallIntegerField()
    book_snapshot = models.JSONField()
    inputs = models.JSONField()
    result = models.JSONField()
    prepared_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    calculation_version = models.CharField(max_length=60)
    created_at = models.DateTimeField(auto_now_add=True)


class AnnualTaxApproval(ImmutableTaxRecord):
    organization = models.ForeignKey("core.Organization", on_delete=models.PROTECT)
    workpaper = models.OneToOneField(AnnualTaxWorkpaper, on_delete=models.PROTECT, related_name="approval")
    external_review = models.OneToOneField(ExternalTaxReview, on_delete=models.PROTECT)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)


class AnnualTaxFiling(ImmutableTaxRecord):
    organization = models.ForeignKey("core.Organization", on_delete=models.PROTECT)
    workpaper = models.OneToOneField(AnnualTaxWorkpaper, on_delete=models.PROTECT, related_name="filing")
    evidence = models.ForeignKey(TaxEvidence, on_delete=models.PROTECT)
    reference = models.CharField(max_length=200)
    taxpayer_reference = models.CharField(max_length=100)
    year = models.PositiveSmallIntegerField(null=True, blank=True)
    return_version = models.PositiveSmallIntegerField(default=0)
    reported_tax = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    reported_balance_due = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "year", "return_version"], name="tax_annual_receipt_version")]

    def clean(self):
        super().clean()
        if self.year is None or self.reported_tax is None or self.reported_balance_due is None:
            raise ValidationError("Bukti penerimaan memerlukan tahun dan nominal SPT yang dicocokkan; data lama kosong tidak dianggap terverifikasi.")


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


class TaxSourceRevision(models.Model):
    source = models.ForeignKey(TaxSource, on_delete=models.PROTECT, related_name="revisions")
    snapshot = models.JSONField()
    content_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    objects = TaxValidatedQuerySet.as_manager()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["source", "content_hash"], name="tax_source_unique_revision")]

    def save(self, *args, **kwargs):
        if not getattr(self, "_knowledge_write", False) or self.pk:
            raise ValidationError("Versi sumber bersifat tetap; gunakan publikasi yang telah ditinjau.")
        self.full_clean()
        try:
            return super().save(*args, **kwargs)
        finally:
            self.__dict__.pop("_knowledge_write", None)

    def delete(self, *args, **kwargs):
        raise ValidationError("Versi sumber tidak boleh dihapus.")


class TaxSourcePublication(models.Model):
    revision = models.ForeignKey(TaxSourceRevision, on_delete=models.PROTECT)
    reviewer_name = models.CharField(max_length=200)
    review_reference = models.CharField(max_length=500)
    created_at = models.DateTimeField(auto_now_add=True)
    objects = TaxValidatedQuerySet.as_manager()

    def save(self, *args, **kwargs):
        if self.pk or not getattr(self, "_knowledge_write", False):
            raise ValidationError("Publikasi sumber memerlukan pemeriksaan operator yang eksplisit.")
        self.full_clean()
        try:
            return super().save(*args, **kwargs)
        finally:
            self.__dict__.pop("_knowledge_write", None)

    def delete(self, *args, **kwargs):
        raise ValidationError("Riwayat publikasi sumber tidak dihapus.")


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
