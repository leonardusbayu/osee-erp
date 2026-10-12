"""Append-only responsibility and contact facts for the marketing workflow."""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from director.models import PlanningRecord


class WorkflowFact(PlanningRecord):
    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not getattr(self, "_workflow_write", False):
            raise ValidationError("Gunakan alur marketing untuk menyimpan riwayat.")
        try:
            return super().save(*args, **kwargs)
        finally:
            self.__dict__.pop("_workflow_write", None)
            self.__dict__.pop("_service_write", None)

    def clean(self):
        super().clean()
        old = self._old()
        self._unchanged(
            old,
            tuple(
                field.attname
                for field in self._meta.fields
                if field.name not in {"id", "created_at"}
            ),
        )


class LeadAssignment(WorkflowFact):
    lead = models.ForeignKey(
        "marketing.Lead", on_delete=models.PROTECT, related_name="assignment_events"
    )
    previous_member = models.ForeignKey(
        "marketing.TeamMember",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    member = models.ForeignKey(
        "marketing.TeamMember",
        on_delete=models.PROTECT,
        related_name="lead_assignments",
    )
    reason = models.TextField(max_length=1000)
    effective_at = models.DateTimeField(default=timezone.now)
    scoped_references = ("lead", "previous_member", "member")

    class Meta:
        ordering = ["-effective_at", "-pk"]


class MemberDelegation(PlanningRecord):
    member = models.ForeignKey(
        "marketing.TeamMember", on_delete=models.PROTECT, related_name="delegations"
    )
    delegate = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="marketing_delegations",
    )
    starts_on = models.DateField()
    ends_on = models.DateField()
    reason = models.TextField(max_length=1000)
    version = models.PositiveIntegerField(default=1)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revocation_reason = models.TextField(max_length=1000, blank=True)
    scoped_references = ("member",)

    class Meta:
        ordering = ["-starts_on", "-pk"]

    def save(self, *args, **kwargs):
        if not getattr(self, "_workflow_write", False):
            raise ValidationError(
                "Gunakan alur delegasi tim untuk menyimpan perubahan."
            )
        try:
            return super().save(*args, **kwargs)
        finally:
            self.__dict__.pop("_workflow_write", None)
            self.__dict__.pop("_service_write", None)

    def clean(self):
        super().clean()
        old = self._old()
        self._unchanged(
            old, ("member_id", "delegate_id", "starts_on", "ends_on", "reason")
        )
        if old and old.revoked_at:
            self._unchanged(old, ("revoked_at", "revocation_reason", "version"))
        if (
            self.starts_on
            and self.ends_on
            and not 0 <= (self.ends_on - self.starts_on).days <= 365
        ):
            raise ValidationError("Delegasi harus berurutan dan maksimal 366 hari.")


CONTACT_METHODS = [
    ("whatsapp", "WhatsApp"),
    ("phone", "Telepon"),
    ("email", "Email"),
    ("meeting", "Pertemuan"),
    ("other", "Lainnya / koreksi"),
]
CONTACT_OUTCOMES = [
    ("responded", "Respons diberikan"),
    ("no_answer", "Belum mendapat jawaban"),
    ("qualified", "Kebutuhan sesuai"),
    ("proposal", "Penawaran disampaikan"),
    ("won", "Sepakat membeli"),
    ("lost", "Tidak lanjut"),
    ("correction", "Koreksi catatan"),
]
LOSS_REASONS = [
    ("price", "Harga / anggaran"),
    ("schedule", "Jadwal"),
    ("product", "Produk tidak sesuai"),
    ("competitor", "Memilih penyedia lain"),
    ("unreachable", "Tidak dapat dihubungi"),
    ("other", "Alasan lain"),
]


class LeadContact(WorkflowFact):
    lead = models.ForeignKey(
        "marketing.Lead", on_delete=models.PROTECT, related_name="contact_events"
    )
    occurred_at = models.DateTimeField()
    method = models.CharField(max_length=20, choices=CONTACT_METHODS)
    outcome = models.CharField(max_length=20, choices=CONTACT_OUTCOMES)
    summary = models.TextField(max_length=4000)
    evidence = models.TextField(max_length=4000)
    loss_reason = models.CharField(max_length=20, choices=LOSS_REASONS, blank=True)
    next_due_at = models.DateTimeField(null=True, blank=True)
    followup_action = models.CharField(
        max_length=10,
        choices=[("keep", "Tetap"), ("set", "Jadwalkan"), ("clear", "Tutup jadwal")],
        default="keep",
    )
    corrects = models.OneToOneField(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="correction",
    )
    correction_reason = models.TextField(max_length=1000, blank=True)
    completes = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="completion_events",
    )
    responsibility_reason = models.TextField(max_length=1000, blank=True)
    scoped_references = ("lead", "corrects", "completes")

    class Meta:
        ordering = ["-occurred_at", "-pk"]

    def clean(self):
        super().clean()
        if self.occurred_at and (
            timezone.is_naive(self.occurred_at) or self.occurred_at > timezone.now()
        ):
            raise ValidationError(
                "Waktu kontak tidak boleh di masa depan dan wajib memakai zona waktu."
            )
        if (
            self.lead_id
            and self.occurred_at
            and self.occurred_at < self.lead.received_at
        ):
            raise ValidationError("Kontak tidak boleh mendahului penerimaan prospek.")
        if self.next_due_at and (
            timezone.is_naive(self.next_due_at) or self.next_due_at < self.occurred_at
        ):
            raise ValidationError("Janji berikutnya tidak boleh mendahului kontak.")
        if self.outcome == "lost" and not self.loss_reason:
            raise ValidationError({"loss_reason": "Pilih alasan prospek tidak lanjut."})
        if self.corrects_id and (
            self.corrects.lead_id != self.lead_id or not self.correction_reason.strip()
        ):
            raise ValidationError(
                "Koreksi harus merujuk kontak prospek yang sama dan mempunyai alasan."
            )
        if self.completes_id and self.completes.lead_id != self.lead_id:
            raise ValidationError(
                "Janji yang dipenuhi harus berasal dari prospek yang sama."
            )


class CampaignEvent(WorkflowFact):
    campaign = models.ForeignKey(
        "marketing.Campaign", on_delete=models.PROTECT, related_name="lifecycle_events"
    )
    operation = models.CharField(
        max_length=20,
        choices=[
            ("pause", "Jeda"),
            ("resume", "Lanjutkan"),
            ("complete", "Selesai"),
            ("handover", "Alih pengelola"),
        ],
    )
    effective_on = models.DateField()
    reason = models.TextField(max_length=1000)
    previous_status = models.CharField(max_length=20)
    resulting_status = models.CharField(max_length=20)
    previous_member = models.ForeignKey(
        "marketing.TeamMember",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    member = models.ForeignKey(
        "marketing.TeamMember",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    scoped_references = ("campaign", "previous_member", "member")

    class Meta:
        ordering = ["-effective_on", "-pk"]


class WorkPolicy(WorkflowFact):
    version = models.PositiveIntegerField()
    response_limit_minutes = models.PositiveIntegerField()
    review_limit_minutes = models.PositiveIntegerField()
    workdays = models.JSONField()
    work_start = models.TimeField()
    work_end = models.TimeField()
    holidays = models.JSONField(default=list, blank=True)
    reason = models.TextField(max_length=1000)

    class Meta:
        ordering = ["-version", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "version"], name="marketing_work_policy_version"
            )
        ]
