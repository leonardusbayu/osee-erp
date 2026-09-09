from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

class Organization(models.Model):
    name = models.CharField("Nama badan usaha", max_length=200)
    brand_name = models.CharField("Nama usaha", max_length=100, default="OSEE")
    legal_form = models.CharField(max_length=30, default="ordinary_pt", choices=[("ordinary_pt", "PT non-perorangan")])
    vat_status = models.CharField(max_length=20, default="non_pkp", choices=[("non_pkp", "Non-PKP"), ("pkp", "PKP")])
    vat_effective_from = models.DateField(null=True, blank=True)
    fiscal_year_start_month = models.PositiveSmallIntegerField(default=1, validators=[MinValueValidator(1), MaxValueValidator(12)])
    is_demo = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

class Membership(models.Model):
    TEAM_ROLES = [("owner", "Owner/Direktur"), ("marketing", "Marketing"), ("finance", "Finance")]
    # Keep stored legacy memberships readable without changing their permissions.
    ROLES = TEAM_ROLES + [("director", "Direktur (akses lama)"), ("reviewer", "Peninjau pajak (akses lama)"), ("auditor", "Pembaca laporan (akses lama)")]
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    role = models.CharField(max_length=20, choices=ROLES, default="finance")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "user"], name="unique_membership")]

class AuditEvent(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=120)
    object_type = models.CharField(max_length=80, default="")
    object_id = models.CharField(max_length=80, default="")
    detail = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-pk"]

class DomainEvent(models.Model):
    """Durable extension events; future modules consume by id with idempotency."""
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT)
    topic = models.CharField(max_length=100)
    version = models.PositiveSmallIntegerField(default=1)
    aggregate_type = models.CharField(max_length=80)
    aggregate_id = models.CharField(max_length=80)
    payload = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    published_at = models.DateTimeField(null=True, blank=True)
    idempotency_key = models.CharField(max_length=200)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "idempotency_key"], name="unique_domain_event")]

class DocumentCounter(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT)
    kind = models.CharField(max_length=20)
    year = models.PositiveSmallIntegerField()
    value = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "kind", "year"], name="unique_document_counter")]

class AccessThrottle(models.Model):
    key = models.CharField(max_length=64, unique=True)
    window_start = models.DateTimeField()
    attempts = models.PositiveIntegerField(default=0)
