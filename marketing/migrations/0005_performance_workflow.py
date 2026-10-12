import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import evidence.models


class Migration(migrations.Migration):
    dependencies = [
        ("marketing", "0004_team_lead_workflow"),
        ("director", "0001_initial"),
    ]
    operations = [
        migrations.AddField(
            model_name="workactivity",
            name="kpi_target",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="work_activities",
                to="marketing.kpitarget",
            ),
        ),
        migrations.AddField(
            model_name="workactivity",
            name="decision",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="marketing_work",
                to="director.decision",
            ),
        ),
        migrations.AddField(
            model_name="workactivity",
            name="budget",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="marketing_work",
                to="director.budget",
            ),
        ),
        migrations.AddField(
            model_name="workactivity",
            name="legacy_action",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="work_activity",
                to="marketing.marketingaction",
            ),
        ),
        migrations.AddField(
            model_name="workactivity",
            name="replaces",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="replacement",
                to="marketing.workactivity",
            ),
        ),
        migrations.AddField(
            model_name="workactivity",
            name="correction_reason",
            field=models.TextField(blank=True, max_length=4000),
        ),
        migrations.AddField(
            model_name="workactivity",
            name="evidence_manifest",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.CreateModel(
            name="ActivityEvidence",
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
                (
                    "file",
                    models.FileField(
                        max_length=180,
                        storage=evidence.models.PrivateEvidenceStorage(
                            directory_permissions_mode=448, file_permissions_mode=384
                        ),
                        upload_to=evidence.models.evidence_path,
                    ),
                ),
                ("original_name", models.CharField(max_length=240)),
                ("content_type", models.CharField(max_length=32)),
                ("size", models.PositiveIntegerField()),
                ("sha256", models.CharField(max_length=64)),
                ("version", models.PositiveIntegerField(default=1)),
                ("correction_reason", models.TextField(blank=True, max_length=4000)),
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
                    "activity",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="attachments",
                        to="marketing.workactivity",
                    ),
                ),
                (
                    "replaces",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="replacement",
                        to="marketing.activityevidence",
                    ),
                ),
            ],
            options={"ordering": ["version", "pk"]},
        ),
        migrations.AddConstraint(
            model_name="activityevidence",
            constraint=models.UniqueConstraint(
                fields=("organization", "activity", "version"),
                name="marketing_work_evidence_version",
            ),
        ),
        migrations.CreateModel(
            name="KpiSourcePeriod",
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
                ("reference", models.CharField(max_length=180)),
                ("sources", models.JSONField()),
                ("inventory_note", models.TextField(max_length=4000)),
                ("data_fingerprint", models.CharField(max_length=64)),
                ("bank_fingerprint", models.CharField(blank=True, max_length=64)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("draft", "Draf"),
                            ("approved", "Diperiksa"),
                            ("withdrawn", "Ditarik"),
                        ],
                        default="draft",
                        max_length=20,
                    ),
                ),
                ("version", models.PositiveIntegerField(default=1)),
                ("approved_at", models.DateTimeField(blank=True, null=True)),
                ("review_notes", models.TextField(blank=True, max_length=4000)),
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
                    "approved_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "target",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="source_periods",
                        to="marketing.kpitarget",
                    ),
                ),
            ],
            options={"ordering": ["-created_at", "-pk"]},
        ),
        migrations.AddConstraint(
            model_name="kpisourceperiod",
            constraint=models.UniqueConstraint(
                fields=("organization", "reference"),
                name="marketing_kpi_period_reference",
            ),
        ),
    ]
