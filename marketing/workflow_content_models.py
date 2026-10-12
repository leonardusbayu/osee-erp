"""Publication and source measurements remain attached to the approved work."""

from django.core.exceptions import ValidationError
from django.db import models

from .workflow_models import WorkflowFact


class ContentPublication(WorkflowFact):
    activity = models.ForeignKey(
        "marketing.WorkActivity", on_delete=models.PROTECT, related_name="publications"
    )
    version = models.PositiveIntegerField()
    published_at = models.DateTimeField()
    url = models.URLField(max_length=600)
    topic = models.CharField(max_length=250)
    evidence = models.TextField(max_length=4000)
    approval = models.JSONField()
    supersedes = models.OneToOneField(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="replacement",
    )
    correction_reason = models.TextField(max_length=1000, blank=True)
    responsibility_reason = models.TextField(max_length=1000, blank=True)
    scoped_references = ("activity", "supersedes")

    class Meta:
        ordering = ["-version", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "activity", "version"],
                name="marketing_publication_version",
            )
        ]

    def clean(self):
        super().clean()
        if self.supersedes_id and self.supersedes.activity_id != self.activity_id:
            raise ValidationError(
                "Koreksi publikasi harus merujuk pekerjaan yang sama."
            )


class ContentMeasurement(WorkflowFact):
    publication = models.ForeignKey(
        ContentPublication, on_delete=models.PROTECT, related_name="measurements"
    )
    period_start = models.DateField()
    period_end = models.DateField()
    source = models.CharField(max_length=180)
    reference = models.CharField(max_length=180)
    evidence = models.TextField(max_length=4000)
    impressions = models.PositiveIntegerField(null=True, blank=True)
    clicks = models.PositiveIntegerField(null=True, blank=True)
    visits = models.PositiveIntegerField(null=True, blank=True)
    supersedes = models.OneToOneField(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="replacement",
    )
    correction_reason = models.TextField(max_length=1000, blank=True)
    responsibility_reason = models.TextField(max_length=1000, blank=True)
    scoped_references = ("publication", "supersedes")

    class Meta:
        ordering = ["-period_end", "-created_at", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "reference"],
                name="marketing_content_measure_ref",
            )
        ]

    def clean(self):
        super().clean()
        if self.supersedes_id and (
            self.supersedes.publication_id != self.publication_id
            or self.period_start != self.supersedes.period_start
            or self.period_end != self.supersedes.period_end
        ):
            raise ValidationError(
                "Koreksi pengukuran harus untuk publikasi dan periode yang sama."
            )
