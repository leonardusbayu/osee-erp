import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


def preserve_initial_responsibility(apps, schema_editor):
    Lead = apps.get_model("marketing", "Lead")
    Lead.objects.using(schema_editor.connection.alias).update(
        current_owner=models.F("owner")
    )
    Campaign = apps.get_model("marketing", "Campaign")
    Campaign.objects.using(schema_editor.connection.alias).update(
        current_owner=models.F("owner")
    )


class Migration(migrations.Migration):
    dependencies = [
        ("marketing", "0003_kpi_activity_accountability"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
        migrations.AddField(
            model_name="teammember",
            name="account",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="marketing_team_members",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="teammember",
            name="version",
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.AddConstraint(
            model_name="teammember",
            constraint=models.UniqueConstraint(
                condition=models.Q(account__isnull=False),
                fields=("organization", "account"),
                name="marketing_member_account",
            ),
        ),
        migrations.AddField(
            model_name="lead",
            name="current_owner",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="current_leads",
                to="marketing.teammember",
            ),
        ),
        migrations.AddField(
            model_name="lead",
            name="next_follow_up_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="campaign",
            name="current_owner",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="current_campaigns",
                to="marketing.teammember",
            ),
        ),
        migrations.AddField(
            model_name="campaign",
            name="actual_end_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="campaign",
            name="version",
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.RunPython(
            preserve_initial_responsibility, migrations.RunPython.noop
        ),
        migrations.CreateModel(
            name="LeadAssignment",
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
                ("reason", models.TextField(max_length=1000)),
                (
                    "effective_at",
                    models.DateTimeField(default=django.utils.timezone.now),
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
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="core.organization",
                    ),
                ),
                (
                    "lead",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="assignment_events",
                        to="marketing.lead",
                    ),
                ),
                (
                    "previous_member",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="marketing.teammember",
                    ),
                ),
                (
                    "member",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="lead_assignments",
                        to="marketing.teammember",
                    ),
                ),
            ],
            options={"ordering": ["-effective_at", "-pk"]},
        ),
        migrations.CreateModel(
            name="MemberDelegation",
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
                ("starts_on", models.DateField()),
                ("ends_on", models.DateField()),
                ("reason", models.TextField(max_length=1000)),
                ("version", models.PositiveIntegerField(default=1)),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                ("revocation_reason", models.TextField(blank=True, max_length=1000)),
                (
                    "created_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="core.organization",
                    ),
                ),
                (
                    "member",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="delegations",
                        to="marketing.teammember",
                    ),
                ),
                (
                    "delegate",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="marketing_delegations",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ["-starts_on", "-pk"]},
        ),
        migrations.CreateModel(
            name="LeadContact",
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
                ("occurred_at", models.DateTimeField()),
                (
                    "method",
                    models.CharField(
                        max_length=20,
                        choices=[
                            ("whatsapp", "WhatsApp"),
                            ("phone", "Telepon"),
                            ("email", "Email"),
                            ("meeting", "Pertemuan"),
                            ("other", "Lainnya / koreksi"),
                        ],
                    ),
                ),
                (
                    "outcome",
                    models.CharField(
                        max_length=20,
                        choices=[
                            ("responded", "Respons diberikan"),
                            ("no_answer", "Belum mendapat jawaban"),
                            ("qualified", "Kebutuhan sesuai"),
                            ("proposal", "Penawaran disampaikan"),
                            ("won", "Sepakat membeli"),
                            ("lost", "Tidak lanjut"),
                            ("correction", "Koreksi catatan"),
                        ],
                    ),
                ),
                ("summary", models.TextField(max_length=4000)),
                ("evidence", models.TextField(max_length=4000)),
                (
                    "loss_reason",
                    models.CharField(
                        blank=True,
                        max_length=20,
                        choices=[
                            ("price", "Harga / anggaran"),
                            ("schedule", "Jadwal"),
                            ("product", "Produk tidak sesuai"),
                            ("competitor", "Memilih penyedia lain"),
                            ("unreachable", "Tidak dapat dihubungi"),
                            ("other", "Alasan lain"),
                        ],
                    ),
                ),
                ("next_due_at", models.DateTimeField(blank=True, null=True)),
                (
                    "followup_action",
                    models.CharField(
                        max_length=10,
                        default="keep",
                        choices=[
                            ("keep", "Tetap"),
                            ("set", "Jadwalkan"),
                            ("clear", "Tutup jadwal"),
                        ],
                    ),
                ),
                ("correction_reason", models.TextField(blank=True, max_length=1000)),
                (
                    "responsibility_reason",
                    models.TextField(blank=True, max_length=1000),
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
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="core.organization",
                    ),
                ),
                (
                    "lead",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="contact_events",
                        to="marketing.lead",
                    ),
                ),
                (
                    "corrects",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="correction",
                        to="marketing.leadcontact",
                    ),
                ),
                (
                    "completes",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="completion_events",
                        to="marketing.leadcontact",
                    ),
                ),
            ],
            options={"ordering": ["-occurred_at", "-pk"]},
        ),
        migrations.CreateModel(
            name="CampaignEvent",
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
                    "operation",
                    models.CharField(
                        max_length=20,
                        choices=[
                            ("pause", "Jeda"),
                            ("resume", "Lanjutkan"),
                            ("complete", "Selesai"),
                            ("handover", "Alih pengelola"),
                        ],
                    ),
                ),
                ("effective_on", models.DateField()),
                ("reason", models.TextField(max_length=1000)),
                ("previous_status", models.CharField(max_length=20)),
                ("resulting_status", models.CharField(max_length=20)),
                (
                    "created_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="core.organization",
                    ),
                ),
                (
                    "campaign",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="lifecycle_events",
                        to="marketing.campaign",
                    ),
                ),
                (
                    "previous_member",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="marketing.teammember",
                    ),
                ),
                (
                    "member",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="marketing.teammember",
                    ),
                ),
            ],
            options={"ordering": ["-effective_on", "-pk"]},
        ),
        migrations.CreateModel(
            name="WorkPolicy",
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
                ("version", models.PositiveIntegerField()),
                ("response_limit_minutes", models.PositiveIntegerField()),
                ("review_limit_minutes", models.PositiveIntegerField()),
                ("workdays", models.JSONField()),
                ("work_start", models.TimeField()),
                ("work_end", models.TimeField()),
                ("holidays", models.JSONField(blank=True, default=list)),
                ("reason", models.TextField(max_length=1000)),
                (
                    "created_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="core.organization",
                    ),
                ),
            ],
            options={
                "ordering": ["-version", "-pk"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("organization", "version"),
                        name="marketing_work_policy_version",
                    )
                ],
            },
        ),
    ]
