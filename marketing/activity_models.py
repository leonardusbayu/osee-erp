"""Marketing work evidence with immutable planning facts and independent review."""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone

from director.models import CHANNEL_CHOICES, PlanningRecord
from evidence.models import PrivateEvidenceStorage, evidence_path

ACTIVITY_KINDS = [
    ("follow_up", "Follow-up prospek"),
    ("content", "Konten"),
    ("seo", "SEO"),
    ("ads", "Iklan"),
    ("partner", "Mitra"),
    ("sales", "Sales"),
]
ACTIVITY_STATUSES = [
    ("planned", "Direncanakan"),
    ("in_progress", "Dikerjakan"),
    ("submitted", "Menunggu pemeriksaan"),
    ("accepted", "Diterima"),
    ("needs_revision", "Perlu perbaikan"),
    ("cancelled", "Dibatalkan"),
]
ACTIVITY_ORIGIN_FIELDS = (
    "title",
    "owner_id",
    "channel",
    "campaign_id",
    "lead_id",
    "kind",
    "due_date",
    "description",
    "kpi_target_id",
    "decision_id",
    "budget_id",
    "legacy_action_id",
    "replaces_id",
    "correction_reason",
)


class WorkActivity(PlanningRecord):
    title = models.CharField(max_length=200)
    owner = models.ForeignKey(
        "marketing.TeamMember", on_delete=models.PROTECT, related_name="work_activities"
    )
    current_owner = models.ForeignKey(
        "marketing.TeamMember",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="assigned_work_activities",
    )
    channel = models.CharField(max_length=20, choices=CHANNEL_CHOICES)
    campaign = models.ForeignKey(
        "marketing.Campaign",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="work_activities",
    )
    lead = models.ForeignKey(
        "marketing.Lead",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="work_activities",
    )
    kind = models.CharField(max_length=20, choices=ACTIVITY_KINDS)
    due_date = models.DateField()
    description = models.TextField(max_length=4000, blank=True)
    kpi_target = models.ForeignKey(
        "marketing.KpiTarget",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="work_activities",
    )
    decision = models.ForeignKey(
        "director.Decision",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="marketing_work",
    )
    budget = models.ForeignKey(
        "director.Budget",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="marketing_work",
    )
    legacy_action = models.OneToOneField(
        "marketing.MarketingAction",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="work_activity",
    )
    replaces = models.OneToOneField(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="replacement",
    )
    correction_reason = models.TextField(max_length=4000, blank=True)
    status = models.CharField(
        max_length=20, choices=ACTIVITY_STATUSES, default="planned"
    )
    occurred_on = models.DateField(null=True, blank=True)
    evidence = models.TextField(max_length=4000, blank=True)
    evidence_manifest = models.JSONField(default=list, blank=True)
    outcome = models.TextField(max_length=4000, blank=True)
    version = models.PositiveIntegerField(default=1, validators=[MinValueValidator(1)])
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_notes = models.TextField(max_length=4000, blank=True)
    scoped_references = (
        "owner",
        "current_owner",
        "campaign",
        "lead",
        "kpi_target",
        "decision",
        "budget",
        "legacy_action",
        "replaces",
    )

    class Meta:
        ordering = ["due_date", "-created_at", "pk"]
        indexes = [
            models.Index(
                fields=["organization", "status", "due_date"],
                name="marketing_activity_queue",
            )
        ]

    def __str__(self):
        return self.title

    @property
    def assigned_owner(self):
        return self.current_owner or self.owner

    def save(self, *args, **kwargs):
        if not getattr(self, "_activity_service_write", False):
            raise ValidationError(
                "Gunakan alur aktivitas marketing untuk menyimpan perubahan."
            )
        try:
            return super().save(*args, **kwargs)
        finally:
            self.__dict__.pop("_activity_service_write", None)
            self.__dict__.pop("_activity_handoff", None)
            self.__dict__.pop("_service_write", None)

    def clean(self):
        super().clean()
        old = self._old()
        self._unchanged(old, ACTIVITY_ORIGIN_FIELDS)
        if not getattr(self, "_activity_handoff", False):
            self._unchanged(old, ("current_owner_id",))
        elif old and old.status not in {"planned", "in_progress", "needs_revision"}:
            raise ValidationError(
                "Alih tugas hanya untuk pekerjaan yang belum diajukan atau sudah diminta diperbaiki."
            )
        if old and old.status in {"accepted", "cancelled"}:
            raise ValidationError(
                "Aktivitas yang diterima atau dibatalkan disimpan sebagai riwayat dan tidak dapat diubah."
            )
        if self.campaign_id and self.channel != self.campaign.channel:
            raise ValidationError(
                {"campaign": "Kanal aktivitas harus sesuai dengan kampanye."}
            )
        if self.lead_id and (
            self.channel != self.lead.channel
            or self.campaign_id != self.lead.campaign_id
        ):
            raise ValidationError(
                {
                    "lead": "Kanal dan kampanye aktivitas harus sesuai dengan prospek terkait."
                }
            )
        if self.occurred_on and self.occurred_on > timezone.localdate():
            raise ValidationError(
                {"occurred_on": "Tanggal pelaksanaan tidak boleh berada di masa depan."}
            )
        if self.status in {"submitted", "accepted", "needs_revision"}:
            if (
                not self.evidence.strip()
                or not self.outcome.strip()
                or not self.occurred_on
                or not self.submitted_by_id
                or not self.submitted_at
            ):
                raise ValidationError(
                    "Tanggal pelaksanaan, bukti, hasil, dan identitas pengaju wajib lengkap."
                )
        if self.status in {"accepted", "needs_revision"}:
            if (
                not self.reviewed_by_id
                or not self.reviewed_at
                or not self.review_notes.strip()
            ):
                raise ValidationError(
                    "Pemeriksa dan catatan pemeriksaan wajib lengkap."
                )
            # The plan author may assign work and review somebody else's evidence.
            if self.reviewed_by_id == self.submitted_by_id:
                raise ValidationError(
                    "Pemeriksa harus berbeda dari pengaju bukti aktivitas."
                )
        if self.status == "cancelled" and not self.review_notes.strip():
            raise ValidationError({"review_notes": "Alasan pembatalan wajib diisi."})


class ActivityAssignment(PlanningRecord):
    activity = models.ForeignKey(
        WorkActivity, on_delete=models.PROTECT, related_name="assignment_events"
    )
    previous_member = models.ForeignKey(
        "marketing.TeamMember", on_delete=models.PROTECT, related_name="+"
    )
    member = models.ForeignKey(
        "marketing.TeamMember",
        on_delete=models.PROTECT,
        related_name="work_assignments",
    )
    reason = models.TextField(max_length=4000)
    effective_at = models.DateTimeField(default=timezone.now)
    activity_version = models.PositiveIntegerField()
    scoped_references = ("activity", "previous_member", "member")

    class Meta:
        ordering = ["-effective_at", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "activity", "activity_version"],
                name="marketing_work_assignment_version",
            )
        ]

    def save(self, *args, **kwargs):
        if not getattr(self, "_assignment_service_write", False):
            raise ValidationError(
                "Gunakan alih tugas untuk menyimpan riwayat penanggung jawab."
            )
        try:
            return super().save(*args, **kwargs)
        finally:
            self.__dict__.pop("_assignment_service_write", None)
            self.__dict__.pop("_service_write", None)

    def clean(self):
        super().clean()
        if self._old():
            raise ValidationError(
                "Riwayat alih tugas disimpan permanen dan tidak dapat diubah."
            )
        if not self.reason.strip():
            raise ValidationError({"reason": "Tuliskan alasan alih tugas."})
        if self.member_id == self.previous_member_id:
            raise ValidationError(
                "Pilih staf pengganti yang berbeda dari penanggung jawab saat ini."
            )


class ActivityEvidence(PlanningRecord):
    activity = models.ForeignKey(
        WorkActivity, on_delete=models.PROTECT, related_name="attachments"
    )
    file = models.FileField(
        storage=PrivateEvidenceStorage(
            file_permissions_mode=0o600, directory_permissions_mode=0o700
        ),
        upload_to=evidence_path,
        max_length=180,
    )
    original_name = models.CharField(max_length=240)
    content_type = models.CharField(max_length=32)
    size = models.PositiveIntegerField()
    sha256 = models.CharField(max_length=64)
    version = models.PositiveIntegerField(default=1)
    replaces = models.OneToOneField(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="replacement",
    )
    correction_reason = models.TextField(max_length=4000, blank=True)
    scoped_references = ("activity", "replaces")

    class Meta:
        ordering = ["version", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "activity", "version"],
                name="marketing_work_evidence_version",
            )
        ]

    def clean(self):
        super().clean()
        self._unchanged(
            self._old(),
            (
                "activity_id",
                "file",
                "original_name",
                "content_type",
                "size",
                "sha256",
                "version",
                "replaces_id",
                "correction_reason",
            ),
        )
        if not self.pk and not getattr(self, "_evidence_creation", False):
            raise ValidationError("Gunakan unggahan bukti pekerjaan yang tervalidasi.")
        if self.replaces_id and self.replaces.activity_id != self.activity_id:
            raise ValidationError("Versi bukti harus berasal dari pekerjaan yang sama.")

    def save(self, *args, **kwargs):
        try:
            return super().save(*args, **kwargs)
        finally:
            self.__dict__.pop("_service_write", None)
            self.__dict__.pop("_evidence_creation", None)
