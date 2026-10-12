"""Append-only facts for the Lead's invoice identity and attribution lineage."""

from django.core.exceptions import ValidationError
from django.db import models

from director.models import PlanningRecord


class LeadIdentityChange(PlanningRecord):
    lead = models.ForeignKey(
        "marketing.Lead", on_delete=models.PROTECT, related_name="identity_changes"
    )
    previous = models.OneToOneField(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="following",
    )
    schema_version = models.PositiveSmallIntegerField(default=1)
    kind = models.CharField(
        max_length=20,
        choices=[
            ("invoice_link", "Invoice dikonfirmasi"),
            ("attribution", "Hubungan invoice dikoreksi"),
            ("legacy_adoption", "Sumber lama diperiksa"),
            ("replace", "Identitas diperbaiki"),
            ("reset", "Invoice benar belum diketahui"),
        ],
    )
    before_party = models.ForeignKey(
        "finance.Party",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    after_party = models.ForeignKey(
        "finance.Party",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    before_product = models.ForeignKey(
        "finance.Product",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    after_product = models.ForeignKey(
        "finance.Product",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    before_invoice = models.ForeignKey(
        "finance.Invoice",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    after_invoice = models.ForeignKey(
        "finance.Invoice",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    before_version = models.PositiveIntegerField()
    after_version = models.PositiveIntegerField()
    identity_confirmed = models.BooleanField(default=False)
    origin_snapshot = models.JSONField(default=dict, blank=True)
    source_snapshot = models.JSONField(default=dict, blank=True)
    snapshot_digest = models.CharField(max_length=64)
    request_key = models.CharField(max_length=160, null=True, blank=True)
    request_payload = models.JSONField(default=dict, blank=True)
    viewed_fingerprint = models.CharField(max_length=64, blank=True)
    reason = models.CharField(max_length=2000, blank=True)
    evidence = models.TextField(max_length=4000, blank=True)
    scoped_references = (
        "lead",
        "previous",
        "before_party",
        "after_party",
        "before_product",
        "after_product",
        "before_invoice",
        "after_invoice",
    )

    class Meta:
        ordering = ["created_at", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "lead"],
                condition=models.Q(previous__isnull=True),
                name="marketing_identity_one_root",
            ),
            models.UniqueConstraint(
                fields=["organization", "request_key"],
                condition=models.Q(request_key__isnull=False),
                name="marketing_identity_request",
            ),
            models.CheckConstraint(
                condition=models.Q(before_version__gte=1, after_version__gte=1),
                name="marketing_identity_versions_positive",
            ),
        ]

    def clean(self):
        super().clean()
        if self.pk or not getattr(self, "_identity_write", False):
            raise ValidationError(
                "Riwayat identitas hanya dapat ditambahkan melalui pemeriksaan invoice."
            )
        if self.schema_version != 1:
            raise ValidationError("Versi riwayat identitas tidak dikenali.")
        if type(self.before_version) is not int or type(self.after_version) is not int:
            raise ValidationError("Versi identitas harus bilangan bulat.")
        before = (self.before_party_id, self.before_product_id, self.before_invoice_id)
        after = (self.after_party_id, self.after_product_id, self.after_invoice_id)
        if self.kind == "legacy_adoption":
            if (
                self.previous_id
                or before != after
                or self.before_version != self.after_version
                or not self.origin_snapshot
            ):
                raise ValidationError(
                    "Pemeriksaan sumber lama harus menjadi akar yang mempertahankan nilai saat ini."
                )
        elif self.after_version != self.before_version + 1:
            raise ValidationError(
                "Perubahan identitas harus menambah tepat satu versi lead."
            )
        if self.previous_id:
            previous = self.previous
            if (
                previous.lead_id != self.lead_id
                or previous.organization_id != self.organization_id
                or previous.after_version > self.before_version
                or (
                    previous.after_party_id,
                    previous.after_product_id,
                    previous.after_invoice_id,
                )
                != before
            ):
                raise ValidationError(
                    "Perubahan harus melanjutkan nilai dan riwayat lead yang sama."
                )
        elif not self.origin_snapshot:
            raise ValidationError("Asal pertama wajib dicatat pada akar riwayat.")
        if (
            self.lead.party_id,
            self.lead.product_id,
            self.lead.invoice_id,
            self.lead.version,
        ) != (*after, self.after_version):
            raise ValidationError(
                "Riwayat tidak sesuai dengan perubahan lead yang tersimpan."
            )
        if self.kind in {"replace", "reset"} and (
            not self.request_key
            or not self.request_payload
            or not self.viewed_fingerprint
            or not self.reason.strip()
            or not self.evidence.strip()
            or self.identity_confirmed is not True
        ):
            raise ValidationError(
                "Konfirmasi, alasan, bukti dan identitas permintaan wajib disimpan."
            )
        from .identity_recovery import change_snapshot_digest

        if self.snapshot_digest != change_snapshot_digest(self):
            raise ValidationError(
                "Ringkasan riwayat identitas tidak sesuai dengan fakta sumber."
            )

    def save(self, *args, **kwargs):
        try:
            if self.pk or not getattr(self, "_identity_write", False):
                raise ValidationError(
                    "Riwayat identitas tersimpan tidak boleh ditulis ulang."
                )
            return super().save(*args, **kwargs)
        finally:
            self.__dict__.pop("_identity_write", None)
            self.__dict__.pop("_service_write", None)
