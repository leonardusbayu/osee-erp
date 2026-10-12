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
STAGES = [
    ("new", "Baru"),
    ("contacted", "Dihubungi"),
    ("qualified", "Sesuai kebutuhan"),
    ("proposal", "Penawaran"),
    ("won", "Sepakat membeli"),
    ("lost", "Tidak lanjut"),
]
METRIC_CHOICES = [
    ("cash_in", "Cash-in bruto terhubung"),
    ("leads", "Prospek masuk"),
    ("unattributed_receipts", "Penerimaan belum terkait prospek"),
    ("overdue_followups", "Follow-up terlambat"),
    ("spend_known_rows", "Kelengkapan laporan biaya iklan"),
    ("response_coverage_percent", "Kelengkapan pencatatan respons"),
    ("median_response_minutes", "Waktu respons pertama"),
    ("cohort_paid_conversion", "Konversi prospek menjadi pembayaran"),
    ("cohort_fully_paid_conversion", "Konversi prospek menjadi pesanan lunas penuh"),
    ("followup_on_time_percent", "Janji follow-up tepat waktu"),
    ("cash_to_spend", "Perbandingan kas dan biaya tercatat"),
    ("unpaid_won_amount", "Tagihan belum lunas dari prospek sepakat"),
    ("unpaid_won_leads", "Prospek sepakat yang belum lunas"),
]


class TeamMember(PlanningRecord):
    name = models.CharField(max_length=150)
    discipline = models.CharField(
        max_length=20,
        choices=[
            ("manager", "Manajer"),
            ("media_buyer", "Pengelola iklan"),
            ("content", "Konten"),
            ("sales", "Sales / follow-up"),
            ("partnership", "Kemitraan"),
        ],
    )
    team = models.CharField(max_length=100, default="Marketing")
    active = models.BooleanField(default=True)
    account = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="marketing_team_members",
    )
    version = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["name", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "account"],
                condition=models.Q(account__isnull=False),
                name="marketing_member_account",
            )
        ]

    def clean(self):
        super().clean()
        old = self._old()
        if not getattr(self, "_workflow_write", False):
            self._unchanged(old, ("account_id", "active", "version"))
            if not old and self.account_id:
                raise ValidationError(
                    "Hubungkan akun melalui pengaturan tim oleh Owner."
                )

    def save(self, *args, **kwargs):
        try:
            return super().save(*args, **kwargs)
        finally:
            self.__dict__.pop("_workflow_write", None)
            self.__dict__.pop("_service_write", None)

    def __str__(self):
        return self.name


class Campaign(PlanningRecord):
    name = models.CharField(max_length=180)
    channel = models.CharField(max_length=20, choices=CHANNEL_CHOICES)
    product = models.ForeignKey(
        "finance.Product", on_delete=models.PROTECT, null=True, blank=True
    )
    owner = models.ForeignKey(
        TeamMember, on_delete=models.PROTECT, null=True, blank=True
    )
    current_owner = models.ForeignKey(
        TeamMember,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="current_campaigns",
    )
    objective = models.CharField(max_length=250, blank=True)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    actual_end_date = models.DateField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1)
    status = models.CharField(
        max_length=20,
        choices=[("active", "Aktif"), ("paused", "Ditunda"), ("completed", "Selesai")],
        default="active",
    )
    scoped_references = ("product", "owner", "current_owner")

    class Meta:
        ordering = ["-created_at", "-pk"]

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        self._unchanged(
            self._old(),
            (
                "name",
                "channel",
                "product_id",
                "owner_id",
                "objective",
                "start_date",
                "end_date",
            ),
        )
        if not getattr(self, "_workflow_write", False):
            self._unchanged(
                self._old(),
                ("current_owner_id", "status", "version", "actual_end_date"),
            )
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValidationError(
                {"end_date": "Akhir kampanye tidak boleh sebelum awalnya."}
            )

    def save(self, *args, **kwargs):
        try:
            return super().save(*args, **kwargs)
        finally:
            self.__dict__.pop("_workflow_write", None)
            self.__dict__.pop("_service_write", None)


class AdDailyObservation(PlanningRecord):
    campaign = models.ForeignKey(
        Campaign, on_delete=models.PROTECT, related_name="observations"
    )
    date = models.DateField()
    ad_name = models.CharField(max_length=180, default=ALL_ADS)
    audience = models.CharField(max_length=160, default=ALL_AUDIENCES)
    region = models.CharField(max_length=120, default=ALL_REGIONS)
    spend = models.DecimalField(
        max_digits=20,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(ZERO)],
    )
    impressions = models.PositiveIntegerField(null=True, blank=True)
    clicks = models.PositiveIntegerField(null=True, blank=True)
    source = models.CharField(max_length=180)
    reference = models.CharField(max_length=180)
    version = models.PositiveIntegerField(default=1)
    withdrawn_at = models.DateTimeField(null=True, blank=True)
    source_root = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="source_revisions",
    )
    replaces = models.OneToOneField(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="replacement",
    )
    scoped_references = ("campaign", "source_root", "replaces")

    class Meta:
        ordering = ["-date", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "reference"],
                condition=models.Q(source_root__isnull=True),
                name="marketing_ad_root_reference",
            ),
            models.UniqueConstraint(
                fields=[
                    "organization",
                    "campaign",
                    "date",
                    "ad_name",
                    "audience",
                    "region",
                ],
                condition=models.Q(withdrawn_at__isnull=True),
                name="marketing_ad_active_grain",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(source_root__isnull=True, replaces__isnull=True)
                    | models.Q(source_root__isnull=False, replaces__isnull=False)
                ),
                name="marketing_ad_chain_shape",
            ),
        ]

    def clean(self):
        super().clean()
        old = self._old()
        identity = (
            "campaign_id",
            "date",
            "ad_name",
            "audience",
            "region",
            "reference",
            "source_root_id",
            "replaces_id",
            "created_at",
        )
        self._unchanged(old, identity)
        recovery_write = getattr(self, "_recovery_write", False)
        if old and old.withdrawn_at:
            self._unchanged(
                old,
                ("spend", "impressions", "clicks", "source", "version", "withdrawn_at"),
            )
        elif old and recovery_write:
            self._unchanged(old, ("spend", "impressions", "clicks", "source"))
            if (
                not self.withdrawn_at
                or timezone.is_naive(self.withdrawn_at)
                or self.version != old.version + 1
            ):
                raise ValidationError(
                    "Penarikan harus mencatat waktu dan versi baru melalui alur koreksi."
                )
            # Archiving unchanged facts must not revalidate an obsolete date against
            # today's campaign plan. Any successor is fully validated separately.
            return
        elif old:
            self._unchanged(old, ("withdrawn_at",))
        elif self.withdrawn_at:
            raise ValidationError(
                "Laporan baru harus aktif; gunakan penarikan untuk membatalkannya."
            )
        if bool(self.source_root_id) != bool(self.replaces_id):
            raise ValidationError(
                "Pengganti harus mencatat dokumen asal dan laporan sebelumnya sekaligus."
            )
        if self.source_root_id:
            if not old and not recovery_write:
                raise ValidationError(
                    "Gunakan penggantian laporan untuk mencatat versi sumber baru."
                )
            root, previous = self.source_root, self.replaces
            if (
                not previous
                or root.source_root_id is not None
                or root.pk == self.pk
                or previous.pk == self.pk
                or (previous.source_root_id or previous.pk) != root.pk
                or not previous.withdrawn_at
                or self.reference != root.reference
                or previous.reference != root.reference
            ):
                raise ValidationError(
                    "Pengganti harus melanjutkan riwayat dokumen asli yang sudah ditarik."
                )
        if all(value is None for value in (self.spend, self.impressions, self.clicks)):
            raise ValidationError(
                "Isi minimal satu angka laporan. Kosong berarti belum diketahui."
            )
        if self.campaign_id and self.date:
            if (
                self.campaign.actual_end_date
                and self.date > self.campaign.actual_end_date
            ):
                raise ValidationError(
                    {
                        "date": "Tanggal biaya melewati akhir aktual kampanye. Buat kampanye lanjutan atau periksa dokumen sumber."
                    }
                )
            if (
                self.campaign.start_date
                and self.date < self.campaign.start_date
                or self.campaign.end_date
                and self.date > self.campaign.end_date
            ):
                raise ValidationError(
                    {"date": "Tanggal laporan harus berada dalam periode kampanye."}
                )
            candidates = (
                type(self)
                .objects.filter(
                    organization_id=self.organization_id,
                    campaign_id=self.campaign_id,
                    date=self.date,
                    withdrawn_at__isnull=True,
                )
                .exclude(pk=self.pk)
            )
            # A total and its subdivisions overlap even when their natural keys differ.
            dimensions = (
                ("ad_name", ALL_ADS),
                ("audience", ALL_AUDIENCES),
                ("region", ALL_REGIONS),
            )
            for other in candidates:
                if all(
                    getattr(self, name) == getattr(other, name)
                    or wildcard in (getattr(self, name), getattr(other, name))
                    for name, wildcard in dimensions
                ):
                    raise ValidationError(
                        "Laporan tanggal ini tumpang tindih dengan total atau rincian yang sudah dicatat. Gunakan satu tingkat rincian yang konsisten."
                    )

    def save(self, *args, **kwargs):
        try:
            return super().save(*args, **kwargs)
        finally:
            self.__dict__.pop("_recovery_write", None)
            self.__dict__.pop("_service_write", None)


class AdObservationRecovery(PlanningRecord):
    """One immutable decision per archived source; the document reference stays owned."""

    predecessor = models.OneToOneField(
        AdDailyObservation, on_delete=models.PROTECT, related_name="recovery"
    )
    successor = models.OneToOneField(
        AdDailyObservation,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="origin_recovery",
    )
    request_key = models.CharField(max_length=160)
    action = models.CharField(
        max_length=10,
        choices=[("withdraw", "Tarik laporan"), ("replace", "Ganti laporan")],
    )
    request_payload = models.JSONField()
    reason = models.CharField(max_length=500)
    evidence = models.TextField(max_length=4000)
    scoped_references = ("predecessor", "successor")

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "request_key"],
                name="marketing_ad_recovery_request",
            )
        ]

    def clean(self):
        super().clean()
        self._unchanged(
            self._old(),
            tuple(field.attname for field in self._meta.fields if field.name != "id"),
        )
        if not self.predecessor_id or not self.predecessor.withdrawn_at:
            raise ValidationError(
                "Keputusan koreksi harus merujuk laporan yang sudah ditarik."
            )
        if (self.action == "replace") != bool(self.successor_id):
            raise ValidationError(
                "Penggantian memerlukan satu laporan pengganti; penarikan tidak mempunyai pengganti."
            )
        if self.successor_id and (
            self.successor.replaces_id != self.predecessor_id
            or self.successor.reference != self.predecessor.reference
            or self.successor.source_root_id
            != (self.predecessor.source_root_id or self.predecessor_id)
        ):
            raise ValidationError(
                "Pengganti tidak sesuai dengan riwayat laporan yang dikoreksi."
            )
        if (
            not isinstance(self.reason, str)
            or not self.reason.strip()
            or not isinstance(self.evidence, str)
            or not self.evidence.strip()
            or not isinstance(self.request_payload, dict)
        ):
            raise ValidationError(
                "Alasan, bukti, dan identitas permintaan koreksi wajib dicatat."
            )

    def save(self, *args, **kwargs):
        try:
            if not getattr(self, "_recovery_write", False):
                raise ValidationError(
                    "Gunakan alur koreksi laporan iklan untuk menyimpan riwayat."
                )
            return super().save(*args, **kwargs)
        finally:
            self.__dict__.pop("_recovery_write", None)
            self.__dict__.pop("_service_write", None)


class SourceCoverage(PlanningRecord):
    """Human-reviewed campaign/source interval; never a platform verification claim."""

    campaign = models.ForeignKey(
        Campaign, on_delete=models.PROTECT, related_name="source_coverage"
    )
    period_start = models.DateField()
    period_end = models.DateField()
    source = models.CharField(max_length=180)
    reference = models.CharField(max_length=180)
    evidence = models.TextField(max_length=4000)
    declared_spend = models.DecimalField(
        max_digits=20, decimal_places=2, validators=[MinValueValidator(ZERO)]
    )
    observation_fingerprint = models.CharField(max_length=64)
    withdrawn_at = models.DateTimeField(null=True, blank=True)
    withdrawal_reason = models.CharField(max_length=500, blank=True)
    scoped_references = ("campaign",)

    class Meta:
        ordering = ["-period_end", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "reference"],
                name="marketing_source_coverage_reference",
            )
        ]

    def clean(self):
        super().clean()
        old = self._old()
        self._unchanged(
            old,
            (
                "campaign_id",
                "period_start",
                "period_end",
                "source",
                "reference",
                "evidence",
                "declared_spend",
                "observation_fingerprint",
            ),
        )
        if old and old.withdrawn_at:
            self._unchanged(old, ("withdrawn_at", "withdrawal_reason"))
        if (
            self.period_start
            and self.period_end
            and self.period_end < self.period_start
        ):
            raise ValidationError("Akhir cakupan tidak boleh sebelum awalnya.")


class Lead(PlanningRecord):
    reference = models.CharField(max_length=120)
    name = models.CharField(max_length=150)
    received_at = models.DateTimeField()
    campaign = models.ForeignKey(
        Campaign, on_delete=models.PROTECT, null=True, blank=True, related_name="leads"
    )
    channel = models.CharField(max_length=20, choices=CHANNEL_CHOICES)
    owner = models.ForeignKey(
        TeamMember,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="leads",
    )
    current_owner = models.ForeignKey(
        TeamMember,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="current_leads",
    )
    product = models.ForeignKey(
        "finance.Product", on_delete=models.PROTECT, null=True, blank=True
    )
    party = models.ForeignKey(
        "finance.Party", on_delete=models.PROTECT, null=True, blank=True
    )
    segment = models.CharField(
        max_length=20,
        choices=[
            ("direct", "Pembeli langsung"),
            ("reseller", "Mitra reseller"),
            ("institution", "Institusi"),
        ],
        default="direct",
    )
    region = models.CharField(max_length=120, default=ALL_REGIONS)
    stage = models.CharField(max_length=20, choices=STAGES, default="new")
    first_response_at = models.DateTimeField(null=True, blank=True)
    next_follow_up = models.DateField(null=True, blank=True)
    next_follow_up_at = models.DateTimeField(null=True, blank=True)
    invoice = models.ForeignKey(
        "finance.Invoice",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="marketing_leads",
    )
    notes = models.TextField(max_length=4000, blank=True)
    version = models.PositiveIntegerField(default=1)
    scoped_references = (
        "campaign",
        "owner",
        "current_owner",
        "product",
        "party",
        "invoice",
    )

    class Meta:
        ordering = ["-received_at", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "reference"], name="marketing_lead_reference"
            ),
            models.UniqueConstraint(
                fields=["invoice"],
                condition=models.Q(invoice__isnull=False),
                name="marketing_lead_invoice",
            ),
        ]

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        old = self._old()
        if not getattr(self, "_workflow_write", False):
            self._unchanged(old, ("current_owner_id",))
        self._unchanged(
            old,
            (
                "reference",
                "name",
                "received_at",
                "campaign_id",
                "channel",
                "owner_id",
                "segment",
                "region",
            ),
        )
        recovery = getattr(self, "_identity_recovery_write", None)
        if recovery is not None:
            if (
                not old
                or not isinstance(recovery, dict)
                or set(recovery) != {"before", "after", "eligible_fields"}
                or recovery["before"]
                != (old.party_id, old.product_id, old.invoice_id, old.version)
                or recovery["after"]
                != (self.party_id, self.product_id, self.invoice_id, self.version)
                or self.version != old.version + 1
                or not isinstance(recovery["eligible_fields"], tuple)
                or not set(recovery["eligible_fields"]) <= {"party", "product"}
            ):
                raise ValidationError(
                    "Perubahan identitas tidak sesuai dengan sumber yang diperiksa."
                )
            from .identity_recovery import validate_lead_identity_write

            validate_lead_identity_write(before=old, after=self, proof=recovery)
        for field in ("product_id", "party_id"):
            if (
                recovery is not None
                and field.removesuffix("_id") in recovery["eligible_fields"]
            ):
                continue
            if (
                recovery is not None
                or old
                and (
                    getattr(old, field) is not None
                    or not getattr(self, "_invoice_link", False)
                )
            ):
                self._unchanged(old, (field,))
        if not getattr(self, "_invoice_link", False):
            self._unchanged(old, ("invoice_id",))
            if not old and self.invoice_id:
                raise ValidationError(
                    "Relasi invoice harus diperiksa melalui layanan Finance marketing."
                )
        for field in ("received_at", "first_response_at"):
            value = getattr(self, field)
            if value and (timezone.is_naive(value) or value > timezone.now()):
                raise ValidationError(
                    {
                        field: "Gunakan waktu dengan zona waktu yang tidak berada di masa depan."
                    }
                )
        if (
            self.first_response_at
            and self.received_at
            and self.first_response_at < self.received_at
        ):
            raise ValidationError(
                {
                    "first_response_at": "Respons pertama tidak boleh sebelum prospek diterima."
                }
            )
        if (
            self.next_follow_up
            and self.received_at
            and self.next_follow_up < timezone.localdate(self.received_at)
        ):
            raise ValidationError(
                {
                    "next_follow_up": "Jadwal tindak lanjut tidak boleh sebelum prospek diterima."
                }
            )
        if self.next_follow_up_at and (
            timezone.is_naive(self.next_follow_up_at)
            or self.next_follow_up_at < self.received_at
        ):
            raise ValidationError(
                {
                    "next_follow_up_at": "Jadwal kontak harus menyertakan zona waktu dan tidak mendahului prospek."
                }
            )
        if self.campaign_id and (
            self.channel != self.campaign.channel
            or self.campaign.product_id
            and self.product_id != self.campaign.product_id
        ):
            raise ValidationError(
                "Kanal dan produk prospek harus sesuai dengan kampanye asal."
            )
        if self.party_id and self.party.kind == "supplier":
            raise ValidationError({"party": "Pemasok bukan pembeli prospek marketing."})
        if self.party_id and (
            (self.segment == "reseller") != (self.party.kind == "reseller")
        ):
            raise ValidationError(
                {"party": "Jenis pembeli harus sesuai dengan segmen prospek."}
            )

    def save(self, *args, **kwargs):
        try:
            return super().save(*args, **kwargs)
        finally:
            self.__dict__.pop("_workflow_write", None)
            self.__dict__.pop("_service_write", None)
            self.__dict__.pop("_invoice_link", None)
            self.__dict__.pop("_identity_recovery_write", None)


class LeadStageEvent(PlanningRecord):
    lead = models.ForeignKey(
        Lead, on_delete=models.PROTECT, related_name="stage_events"
    )
    from_stage = models.CharField(max_length=20, blank=True, choices=STAGES)
    to_stage = models.CharField(max_length=20, choices=STAGES)
    occurred_at = models.DateTimeField()
    scoped_references = ("lead",)

    class Meta:
        ordering = ["occurred_at", "pk"]

    def clean(self):
        super().clean()
        self._unchanged(
            self._old(), ("lead_id", "from_stage", "to_stage", "occurred_at")
        )
        if self.occurred_at and timezone.is_naive(self.occurred_at):
            raise ValidationError("Waktu perubahan tahap harus menyertakan zona waktu.")


class MarketingAction(PlanningRecord):
    title = models.CharField(max_length=200)
    evidence = models.TextField(max_length=4000, blank=True)
    description = models.TextField(max_length=4000)
    owner = models.ForeignKey(
        TeamMember, on_delete=models.PROTECT, null=True, blank=True
    )
    due_date = models.DateField(null=True, blank=True)
    status = models.CharField(
        max_length=20,
        choices=[
            ("open", "Belum mulai"),
            ("in_progress", "Dikerjakan"),
            ("done", "Selesai"),
            ("dismissed", "Tidak dilanjutkan"),
        ],
        default="open",
    )
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
        self._unchanged(
            self._old(),
            ("title", "evidence", "description", "owner_id", "due_date", "metric_key"),
        )
        if self.status in ("done", "dismissed") and not self.outcome.strip():
            raise ValidationError(
                {"outcome": "Catat hasil atau alasan sebelum menutup tindakan."}
            )


class MarketingAIPolicy(ScopedModel):
    organization = models.OneToOneField(
        "core.Organization",
        on_delete=models.PROTECT,
        related_name="marketing_ai_policy",
    )
    enabled = models.BooleanField(default=False)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    policy_version = models.PositiveIntegerField(default=1)
    monthly_budget_usd = models.DecimalField(
        max_digits=12, decimal_places=6, default=0, validators=[MinValueValidator(0)]
    )
    request_cap_usd = models.DecimalField(
        max_digits=12, decimal_places=6, default=0, validators=[MinValueValidator(0)]
    )
    disclosure_version = models.CharField(
        max_length=80, default="marketing-aggregate-v1"
    )
    disclosure_text = models.TextField(blank=True)


# Keep Django's model discovery centralized while workflow models remain focused.
from .activity_models import (
    ActivityAssignment as ActivityAssignment,
)
from .activity_models import (
    ActivityEvidence as ActivityEvidence,
)
from .activity_models import (
    WorkActivity as WorkActivity,
)
from .identity_models import LeadIdentityChange as LeadIdentityChange
from .kpi_models import (
    KpiReview as KpiReview,
)
from .kpi_models import (
    KpiSourcePeriod as KpiSourcePeriod,
)
from .kpi_models import (
    KpiTarget as KpiTarget,
)
from .workflow_content_models import (
    ContentMeasurement as ContentMeasurement,
)
from .workflow_content_models import (
    ContentPublication as ContentPublication,
)
from .workflow_models import (
    CampaignEvent as CampaignEvent,
)
from .workflow_models import (
    LeadAssignment as LeadAssignment,
)
from .workflow_models import (
    LeadContact as LeadContact,
)
from .workflow_models import (
    MemberDelegation as MemberDelegation,
)
from .workflow_models import (
    WorkPolicy as WorkPolicy,
)
