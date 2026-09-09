"""Operational marketing records; none of these models posts financial entries."""
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone

from director.models import CHANNEL_CHOICES, PlanningRecord
from finance.models import ScopedModel


ZERO = Decimal("0.00")
ALL_ADS = "Semua iklan"
ALL_AUDIENCES = "Semua audiens"
ALL_REGIONS = "Semua wilayah"
STAGES = [("new", "Baru"), ("contacted", "Dihubungi"), ("qualified", "Sesuai kebutuhan"),
          ("proposal", "Penawaran"), ("won", "Sepakat membeli"), ("lost", "Tidak lanjut")]
METRIC_CHOICES = [("cash_in", "Cash-in bruto terhubung"), ("leads", "Prospek masuk"),
    ("unattributed_receipts", "Penerimaan belum terkait prospek"), ("overdue_followups", "Follow-up terlambat"),
    ("spend_known_rows", "Kelengkapan laporan biaya iklan"), ("response_coverage_percent", "Kelengkapan pencatatan respons"),
    ("median_response_minutes", "Waktu respons pertama"), ("cohort_paid_conversion", "Konversi prospek menjadi pembayaran"),
    ("cash_to_spend", "Perbandingan kas dan biaya tercatat"), ("unpaid_won_amount", "Tagihan belum lunas dari prospek sepakat"),
    ("unpaid_won_leads", "Prospek sepakat yang belum lunas")]


class TeamMember(PlanningRecord):
    name = models.CharField(max_length=150)
    discipline = models.CharField(max_length=20, choices=[("manager", "Manajer"), ("media_buyer", "Pengelola iklan"),
        ("content", "Konten"), ("sales", "Sales / follow-up"), ("partnership", "Kemitraan")])
    team = models.CharField(max_length=100, default="Marketing")
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name", "pk"]

    def __str__(self):
        return self.name


class Campaign(PlanningRecord):
    name = models.CharField(max_length=180)
    channel = models.CharField(max_length=20, choices=CHANNEL_CHOICES)
    product = models.ForeignKey("finance.Product", on_delete=models.PROTECT, null=True, blank=True)
    owner = models.ForeignKey(TeamMember, on_delete=models.PROTECT, null=True, blank=True)
    objective = models.CharField(max_length=250, blank=True)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=[("active", "Aktif"), ("paused", "Ditunda"), ("completed", "Selesai")], default="active")
    scoped_references = ("product", "owner")

    class Meta:
        ordering = ["-created_at", "-pk"]

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        self._unchanged(self._old(), ("name", "channel", "product_id", "owner_id", "objective", "start_date", "end_date"))
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValidationError({"end_date": "Akhir kampanye tidak boleh sebelum awalnya."})


class AdDailyObservation(PlanningRecord):
    campaign = models.ForeignKey(Campaign, on_delete=models.PROTECT, related_name="observations")
    date = models.DateField()
    ad_name = models.CharField(max_length=180, default=ALL_ADS)
    audience = models.CharField(max_length=160, default=ALL_AUDIENCES)
    region = models.CharField(max_length=120, default=ALL_REGIONS)
    spend = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(ZERO)])
    impressions = models.PositiveIntegerField(null=True, blank=True)
    clicks = models.PositiveIntegerField(null=True, blank=True)
    source = models.CharField(max_length=180)
    reference = models.CharField(max_length=180)
    version = models.PositiveIntegerField(default=1)
    scoped_references = ("campaign",)

    class Meta:
        ordering = ["-date", "-pk"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "reference"], name="marketing_ad_reference"),
            models.UniqueConstraint(fields=["organization", "campaign", "date", "ad_name", "audience", "region"], name="marketing_ad_grain"),
        ]

    def clean(self):
        super().clean()
        self._unchanged(self._old(), ("campaign_id", "date", "ad_name", "audience", "region", "reference"))
        if all(value is None for value in (self.spend, self.impressions, self.clicks)):
            raise ValidationError("Isi minimal satu angka laporan. Kosong berarti belum diketahui.")
        if self.campaign_id and self.date:
            if self.campaign.start_date and self.date < self.campaign.start_date or self.campaign.end_date and self.date > self.campaign.end_date:
                raise ValidationError({"date": "Tanggal laporan harus berada dalam periode kampanye."})
            candidates = type(self).objects.filter(organization_id=self.organization_id, campaign_id=self.campaign_id, date=self.date).exclude(pk=self.pk)
            # A total and its subdivisions overlap even when their natural keys differ.
            dimensions = (("ad_name", ALL_ADS), ("audience", ALL_AUDIENCES), ("region", ALL_REGIONS))
            for other in candidates:
                if all(getattr(self, name) == getattr(other, name) or wildcard in (getattr(self, name), getattr(other, name)) for name, wildcard in dimensions):
                    raise ValidationError("Laporan tanggal ini tumpang tindih dengan total atau rincian yang sudah dicatat. Gunakan satu tingkat rincian yang konsisten.")


class Lead(PlanningRecord):
    reference = models.CharField(max_length=120)
    name = models.CharField(max_length=150)
    received_at = models.DateTimeField()
    campaign = models.ForeignKey(Campaign, on_delete=models.PROTECT, null=True, blank=True, related_name="leads")
    channel = models.CharField(max_length=20, choices=CHANNEL_CHOICES)
    owner = models.ForeignKey(TeamMember, on_delete=models.PROTECT, null=True, blank=True, related_name="leads")
    product = models.ForeignKey("finance.Product", on_delete=models.PROTECT, null=True, blank=True)
    party = models.ForeignKey("finance.Party", on_delete=models.PROTECT, null=True, blank=True)
    segment = models.CharField(max_length=20, choices=[("direct", "Pembeli langsung"), ("reseller", "Mitra reseller"), ("institution", "Institusi")], default="direct")
    region = models.CharField(max_length=120, default=ALL_REGIONS)
    stage = models.CharField(max_length=20, choices=STAGES, default="new")
    first_response_at = models.DateTimeField(null=True, blank=True)
    next_follow_up = models.DateField(null=True, blank=True)
    invoice = models.ForeignKey("finance.Invoice", on_delete=models.PROTECT, null=True, blank=True, related_name="marketing_leads")
    notes = models.TextField(max_length=4000, blank=True)
    version = models.PositiveIntegerField(default=1)
    scoped_references = ("campaign", "owner", "product", "party", "invoice")

    class Meta:
        ordering = ["-received_at", "-pk"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "reference"], name="marketing_lead_reference"),
            models.UniqueConstraint(fields=["invoice"], condition=models.Q(invoice__isnull=False), name="marketing_lead_invoice"),
        ]

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        old = self._old()
        self._unchanged(old, ("reference", "name", "received_at", "campaign_id", "channel", "owner_id", "segment", "region"))
        for field in ("product_id", "party_id"):
            if old and (getattr(old, field) is not None or not getattr(self, "_invoice_link", False)):
                self._unchanged(old, (field,))
        if not getattr(self, "_invoice_link", False):
            self._unchanged(old, ("invoice_id",))
            if not old and self.invoice_id:
                raise ValidationError("Relasi invoice harus diperiksa melalui layanan Finance marketing.")
        for field in ("received_at", "first_response_at"):
            value = getattr(self, field)
            if value and (timezone.is_naive(value) or value > timezone.now()):
                raise ValidationError({field: "Gunakan waktu dengan zona waktu yang tidak berada di masa depan."})
        if self.first_response_at and self.received_at and self.first_response_at < self.received_at:
            raise ValidationError({"first_response_at": "Respons pertama tidak boleh sebelum prospek diterima."})
        if self.next_follow_up and self.received_at and self.next_follow_up < timezone.localdate(self.received_at):
            raise ValidationError({"next_follow_up": "Jadwal tindak lanjut tidak boleh sebelum prospek diterima."})
        if self.campaign_id and (self.channel != self.campaign.channel or self.campaign.product_id and self.product_id != self.campaign.product_id):
            raise ValidationError("Kanal dan produk prospek harus sesuai dengan kampanye asal.")
        if self.party_id and self.party.kind == "supplier":
            raise ValidationError({"party": "Pemasok bukan pembeli prospek marketing."})
        if self.party_id and ((self.segment == "reseller") != (self.party.kind == "reseller")):
            raise ValidationError({"party": "Jenis pembeli harus sesuai dengan segmen prospek."})


class LeadStageEvent(PlanningRecord):
    lead = models.ForeignKey(Lead, on_delete=models.PROTECT, related_name="stage_events")
    from_stage = models.CharField(max_length=20, blank=True, choices=STAGES)
    to_stage = models.CharField(max_length=20, choices=STAGES)
    occurred_at = models.DateTimeField()
    scoped_references = ("lead",)

    class Meta:
        ordering = ["occurred_at", "pk"]

    def clean(self):
        super().clean()
        self._unchanged(self._old(), ("lead_id", "from_stage", "to_stage", "occurred_at"))
        if self.occurred_at and timezone.is_naive(self.occurred_at):
            raise ValidationError("Waktu perubahan tahap harus menyertakan zona waktu.")


class MarketingAction(PlanningRecord):
    title = models.CharField(max_length=200)
    evidence = models.TextField(max_length=4000, blank=True)
    description = models.TextField(max_length=4000)
    owner = models.ForeignKey(TeamMember, on_delete=models.PROTECT, null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=[("open", "Belum mulai"), ("in_progress", "Dikerjakan"), ("done", "Selesai"), ("dismissed", "Tidak dilanjutkan")], default="open")
    outcome = models.TextField(max_length=4000, blank=True)
    metric_key = models.CharField(max_length=60, default="cash_in")
    version = models.PositiveIntegerField(default=1)
    scoped_references = ("owner",)

    class Meta:
        ordering = ["due_date", "-created_at", "pk"]

    @property
    def metric_label(self):
        return dict(METRIC_CHOICES).get(self.metric_key, self.metric_key)

    def clean(self):
        super().clean()
        self._unchanged(self._old(), ("title", "evidence", "description", "owner_id", "due_date", "metric_key"))
        if self.status in ("done", "dismissed") and not self.outcome.strip():
            raise ValidationError({"outcome": "Catat hasil atau alasan sebelum menutup tindakan."})


class MarketingAIPolicy(ScopedModel):
    organization = models.OneToOneField("core.Organization", on_delete=models.PROTECT, related_name="marketing_ai_policy")
    enabled = models.BooleanField(default=False)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    approved_at = models.DateTimeField(null=True, blank=True)
    policy_version = models.PositiveIntegerField(default=1)
    monthly_budget_usd = models.DecimalField(max_digits=12, decimal_places=6, default=0, validators=[MinValueValidator(0)])
    request_cap_usd = models.DecimalField(max_digits=12, decimal_places=6, default=0, validators=[MinValueValidator(0)])
    disclosure_version = models.CharField(max_length=80, default="marketing-aggregate-v1")
    disclosure_text = models.TextField(blank=True)
