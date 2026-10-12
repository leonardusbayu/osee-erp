"""Private, immutable dated facts; professional approval is a separate event."""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from .models import ImmutableTaxRecord


class RecipientTaxIdentity(ImmutableTaxRecord):
    organization = models.ForeignKey("core.Organization", on_delete=models.PROTECT)
    recipient_party = models.ForeignKey(
        "finance.Party", null=True, blank=True, on_delete=models.PROTECT
    )
    legal_name = models.CharField(max_length=200)
    name_variation_reason = models.TextField(max_length=2000, blank=True)
    recipient_type = models.CharField(
        max_length=10, choices=[("person", "Orang pribadi"), ("body", "Badan")]
    )
    tax_residency = models.CharField(
        max_length=10,
        choices=[
            ("spdn", "Subjek dalam negeri"),
            ("spln", "Subjek luar negeri"),
            ("but", "Bentuk usaha tetap"),
            ("unknown", "Belum diperiksa"),
        ],
        default="unknown",
    )
    population_status = models.CharField(
        max_length=12,
        choices=[
            ("resident", "Penduduk"),
            ("nonresident", "Bukan Penduduk"),
            ("na", "Tidak berlaku — badan"),
            ("unknown", "Belum diperiksa"),
        ],
        default="unknown",
    )
    identity_type = models.CharField(
        max_length=10,
        choices=[
            ("npwp16", "NPWP 16 digit"),
            ("nik", "NIK"),
            ("none", "Tidak memiliki NPWP — terdokumentasi"),
            ("unknown", "Belum diperiksa"),
        ],
        default="unknown",
    )
    number = models.CharField(max_length=16, blank=True)
    registration_status = models.CharField(
        max_length=12,
        choices=[
            ("registered", "Terdaftar"),
            ("no_npwp", "Tidak memiliki NPWP"),
            ("nonactive", "Nonaktif"),
            ("deleted", "Dihapus"),
            ("unknown", "Belum diperiksa"),
        ],
        default="unknown",
    )
    nik_validated = models.BooleanField(null=True, blank=True)
    nik_integrated = models.BooleanField(null=True, blank=True)
    nik_activated = models.BooleanField(null=True, blank=True)
    effective_from = models.DateField()
    effective_until = models.DateField(null=True, blank=True)
    evidence = models.ForeignKey("taxes.TaxEvidence", on_delete=models.PROTECT)
    reference = models.CharField(max_length=300)
    reason = models.TextField(max_length=2000)
    previous = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="proposed_successors",
    )
    revision = models.PositiveIntegerField(default=1)
    request_key = models.CharField(max_length=80)
    fingerprint = models.CharField(max_length=64)
    target_snapshot = models.JSONField()
    legal_sources = models.JSONField()
    prepared_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-effective_from", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "request_key"],
                name="recipient_identity_request_unique",
            ),
            models.CheckConstraint(
                condition=models.Q(effective_until__isnull=True)
                | models.Q(effective_until__gte=models.F("effective_from")),
                name="recipient_identity_dates_valid",
            ),
        ]

    @property
    def masked_number(self):
        return "•••• " + self.number[-4:] if self.number else "Belum ada nomor"

    def clean(self):
        super().clean()
        if self.previous_id:
            approval = RecipientIdentityApproval.objects.filter(
                identity_id=self.previous_id
            ).first()
            unresolved_same_date = (
                approval
                and approval.treatment == "unresolved"
                and approval.factor is None
                and self.effective_from == self.previous.effective_from
            )
            if (
                self.previous.recipient_party_id != self.recipient_party_id
                or not approval
                or (
                    self.effective_from <= self.previous.effective_from
                    and not unresolved_same_date
                )
            ):
                raise ValidationError(
                    "Pilih versi sebelumnya yang disetujui. Versi bertarif hanya diganti pada tanggal perubahan berikutnya; hasil belum ditentukan dapat diselesaikan pada tanggal sebenarnya yang sama."
                )


class RecipientIdentityApproval(ImmutableTaxRecord):
    organization = models.ForeignKey("core.Organization", on_delete=models.PROTECT)
    identity = models.OneToOneField(
        RecipientTaxIdentity, on_delete=models.PROTECT, related_name="approval"
    )
    previous_approval = models.OneToOneField(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="successor_approval",
    )
    external_review = models.OneToOneField(
        "taxes.ExternalTaxReview", on_delete=models.PROTECT
    )
    treatment = models.CharField(
        max_length=30,
        choices=[
            ("standard_npwp", "NPWP — faktor 1"),
            ("standard_integrated_nik", "NIK terintegrasi — faktor 1"),
            ("increased_no_npwp", "Tidak memiliki NPWP — faktor 2"),
            ("unresolved", "Belum dapat menentukan PPh 23"),
        ],
    )
    factor = models.PositiveSmallIntegerField(null=True, blank=True)
    identity_fingerprint = models.CharField(max_length=64)
    snapshot = models.JSONField()
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    def clean(self):
        super().clean()
        if (
            self.previous_approval_id
            and self.previous_approval.identity_id != self.identity.previous_id
        ):
            raise ValidationError(
                "Persetujuan sebelumnya tidak sesuai versi identitas."
            )
