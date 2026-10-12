import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("marketing", "0008_alter_kpitarget_metric_key"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="addailyobservation", name="marketing_ad_reference"
        ),
        migrations.RemoveConstraint(
            model_name="addailyobservation", name="marketing_ad_grain"
        ),
        migrations.AddField(
            model_name="addailyobservation",
            name="withdrawn_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="addailyobservation",
            name="source_root",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="source_revisions",
                to="marketing.addailyobservation",
            ),
        ),
        migrations.AddField(
            model_name="addailyobservation",
            name="replaces",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="replacement",
                to="marketing.addailyobservation",
            ),
        ),
        migrations.AddConstraint(
            model_name="addailyobservation",
            constraint=models.UniqueConstraint(
                fields=("organization", "reference"),
                condition=models.Q(source_root__isnull=True),
                name="marketing_ad_root_reference",
            ),
        ),
        migrations.AddConstraint(
            model_name="addailyobservation",
            constraint=models.UniqueConstraint(
                fields=(
                    "organization",
                    "campaign",
                    "date",
                    "ad_name",
                    "audience",
                    "region",
                ),
                condition=models.Q(withdrawn_at__isnull=True),
                name="marketing_ad_active_grain",
            ),
        ),
        migrations.AddConstraint(
            model_name="addailyobservation",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(source_root__isnull=True, replaces__isnull=True)
                    | models.Q(source_root__isnull=False, replaces__isnull=False)
                ),
                name="marketing_ad_chain_shape",
            ),
        ),
        migrations.CreateModel(
            name="AdObservationRecovery",
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
                ("request_key", models.CharField(max_length=160)),
                (
                    "action",
                    models.CharField(
                        choices=[
                            ("withdraw", "Tarik laporan"),
                            ("replace", "Ganti laporan"),
                        ],
                        max_length=10,
                    ),
                ),
                ("request_payload", models.JSONField()),
                ("reason", models.CharField(max_length=500)),
                ("evidence", models.TextField(max_length=4000)),
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
                    "predecessor",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="recovery",
                        to="marketing.addailyobservation",
                    ),
                ),
                (
                    "successor",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="origin_recovery",
                        to="marketing.addailyobservation",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at", "-pk"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("organization", "request_key"),
                        name="marketing_ad_recovery_request",
                    )
                ],
            },
        ),
    ]
