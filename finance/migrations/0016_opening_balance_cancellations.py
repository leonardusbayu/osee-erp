"""Retained wrong-match decisions; no physical bank or business-data seeding."""

from decimal import Decimal

import django.db.models.deletion
from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("finance", "0015_opening_balance_corrections"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
        migrations.CreateModel(
            name="OpeningBalanceCancellation",
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
                ("date", models.DateField()),
                ("reference", models.CharField(max_length=120)),
                ("evidence", models.TextField(max_length=4000)),
                (
                    "amount",
                    models.DecimalField(
                        max_digits=20,
                        decimal_places=2,
                        validators=[MinValueValidator(Decimal("0.01"))],
                    ),
                ),
                ("reason", models.TextField(max_length=4000)),
                ("request_key", models.UUIDField()),
                ("request_payload", models.JSONField()),
                ("source_snapshot", models.JSONField()),
                ("source_hash", models.CharField(max_length=64)),
                (
                    "workflow_version",
                    models.CharField(default="opening-balance-v1", max_length=40),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="core.organization",
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
                    "journal",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="finance.journal",
                    ),
                ),
                (
                    "item",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="cancellations",
                        to="finance.openingbalanceitem",
                    ),
                ),
                (
                    "settlement",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="cancellation",
                        to="finance.openingbalancesettlement",
                    ),
                ),
            ],
            options={
                "ordering": ["date", "pk"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("organization", "request_key"),
                        name="fin_opening_cancel_request",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(amount__gt=0),
                        name="fin_opening_cancel_positive",
                    ),
                ],
            },
        ),
    ]
