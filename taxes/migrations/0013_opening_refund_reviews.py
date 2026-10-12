import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("taxes", "0012_opening_balance_reviews"),
        ("finance", "0017_opening_balance_refunds"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="OpeningRefundTaxReview",
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
                ("version", models.PositiveIntegerField()),
                ("decision", models.JSONField()),
                ("source_snapshot", models.JSONField()),
                ("source_fingerprint", models.CharField(max_length=64)),
                ("report_snapshot", models.JSONField()),
                ("snapshot_hash", models.CharField(max_length=64)),
                ("request_key", models.UUIDField()),
                ("request_payload", models.JSONField()),
                (
                    "created_at",
                    models.DateTimeField(
                        default=django.utils.timezone.now, editable=False
                    ),
                ),
                (
                    "actor",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
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
                    "refund",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="tax_reviews",
                        to="finance.openingbalancerefund",
                    ),
                ),
                (
                    "previous",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="next_review",
                        to="taxes.openingrefundtaxreview",
                    ),
                ),
                (
                    "external_review",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="opening_refund_decision",
                        to="taxes.externaltaxreview",
                    ),
                ),
            ],
            options={
                "ordering": ["version", "pk"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("organization", "request_key"),
                        name="tax_refund_review_request",
                    ),
                    models.UniqueConstraint(
                        fields=("refund", "version"), name="tax_refund_review_version"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(version__gte=1),
                        name="tax_refund_review_positive",
                    ),
                ],
            },
        ),
    ]
