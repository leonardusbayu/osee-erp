import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("marketing", "0009_ad_observation_recovery"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="LeadIdentityChange",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("schema_version", models.PositiveSmallIntegerField(default=1)),
                (
                    "kind",
                    models.CharField(
                        max_length=20,
                        choices=[
                            ("invoice_link", "Invoice dikonfirmasi"),
                            ("attribution", "Hubungan invoice dikoreksi"),
                            ("legacy_adoption", "Sumber lama diperiksa"),
                            ("replace", "Identitas diperbaiki"),
                            ("reset", "Invoice benar belum diketahui"),
                        ],
                    ),
                ),
                ("before_version", models.PositiveIntegerField()),
                ("after_version", models.PositiveIntegerField()),
                ("identity_confirmed", models.BooleanField(default=False)),
                ("origin_snapshot", models.JSONField(default=dict, blank=True)),
                ("source_snapshot", models.JSONField(default=dict, blank=True)),
                ("snapshot_digest", models.CharField(max_length=64)),
                (
                    "request_key",
                    models.CharField(max_length=160, null=True, blank=True),
                ),
                ("request_payload", models.JSONField(default=dict, blank=True)),
                ("viewed_fingerprint", models.CharField(max_length=64, blank=True)),
                ("reason", models.CharField(max_length=2000, blank=True)),
                ("evidence", models.TextField(max_length=4000, blank=True)),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="core.organization",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "lead",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="identity_changes",
                        to="marketing.lead",
                    ),
                ),
                (
                    "previous",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="following",
                        to="marketing.leadidentitychange",
                        null=True,
                        blank=True,
                    ),
                ),
                *[
                    (
                        name,
                        models.ForeignKey(
                            on_delete=django.db.models.deletion.PROTECT,
                            related_name="+",
                            to=target,
                            null=True,
                            blank=True,
                        ),
                    )
                    for name, target in (
                        ("before_party", "finance.party"),
                        ("after_party", "finance.party"),
                        ("before_product", "finance.product"),
                        ("after_product", "finance.product"),
                        ("before_invoice", "finance.invoice"),
                        ("after_invoice", "finance.invoice"),
                    )
                ],
            ],
            options={
                "ordering": ["created_at", "pk"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("organization", "lead"),
                        condition=models.Q(previous__isnull=True),
                        name="marketing_identity_one_root",
                    ),
                    models.UniqueConstraint(
                        fields=("organization", "request_key"),
                        condition=models.Q(request_key__isnull=False),
                        name="marketing_identity_request",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(before_version__gte=1, after_version__gte=1),
                        name="marketing_identity_versions_positive",
                    ),
                ],
            },
        )
    ]
