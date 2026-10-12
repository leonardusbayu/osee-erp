from decimal import Decimal

import django.db.models.deletion
from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import migrations, models


def base():
    return [
        (
            "id",
            models.BigAutoField(
                auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
            ),
        ),
        ("created_at", models.DateTimeField(auto_now_add=True)),
        ("content_hash", models.CharField(max_length=64)),
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
                on_delete=django.db.models.deletion.PROTECT, to="core.organization"
            ),
        ),
    ]


class Migration(migrations.Migration):
    dependencies = [
        ("director", "0002_linked_budget_payments"),
        ("finance", "0012_customer_tax_advance_credit"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="BudgetSpend",
            fields=base()
            + [
                ("date", models.DateField()),
                ("title", models.CharField(max_length=500)),
                ("origin_snapshot", models.JSONField()),
                ("version", models.PositiveIntegerField(default=1)),
                (
                    "budget",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="spends",
                        to="director.budget",
                    ),
                ),
                (
                    "origin_entry",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="budget_spend",
                        to="director.budgetentry",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="BudgetSpendMovement",
            fields=base()
            + [
                ("kind", models.CharField(max_length=24)),
                ("date", models.DateField()),
                (
                    "amount",
                    models.DecimalField(
                        decimal_places=2,
                        default=0,
                        max_digits=20,
                        validators=[MinValueValidator(Decimal(0))],
                    ),
                ),
                ("from_kind", models.CharField(blank=True, max_length=24)),
                ("to_kind", models.CharField(blank=True, max_length=24)),
                ("reference", models.CharField(max_length=120)),
                ("reason", models.CharField(max_length=500)),
                ("evidence", models.TextField(max_length=4000)),
                ("payload", models.JSONField()),
                ("before", models.JSONField(blank=True, default=dict)),
                ("after", models.JSONField(blank=True, default=dict)),
                (
                    "reverse_of",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="reversal",
                        to="director.budgetspendmovement",
                    ),
                ),
                (
                    "spend",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="movements",
                        to="director.budgetspend",
                    ),
                ),
            ],
            options={
                "ordering": ["date", "pk"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("organization", "reference"),
                        name="director_spend_request",
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="BudgetBillLink",
            fields=base()
            + [
                ("date", models.DateField()),
                ("coverage", models.JSONField(blank=True, default=list)),
                ("source_snapshot", models.JSONField()),
                ("source_hash", models.CharField(max_length=64)),
                ("voided_at", models.DateField(blank=True, null=True)),
                (
                    "bill",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="budget_links",
                        to="finance.bill",
                    ),
                ),
                (
                    "request",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="bill_link",
                        to="director.budgetspendmovement",
                    ),
                ),
                (
                    "spend",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="bill_links",
                        to="director.budgetspend",
                    ),
                ),
                (
                    "void_request",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="voided_link",
                        to="director.budgetspendmovement",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        condition=models.Q(voided_at__isnull=True),
                        fields=("bill",),
                        name="director_active_bill_link",
                    )
                ]
            },
        ),
        migrations.CreateModel(
            name="BudgetPaymentMatch",
            fields=base()
            + [
                ("date", models.DateField()),
                (
                    "amount",
                    models.DecimalField(
                        decimal_places=2,
                        max_digits=20,
                        validators=[MinValueValidator(Decimal("0.01"))],
                    ),
                ),
                ("source_snapshot", models.JSONField()),
                ("source_hash", models.CharField(max_length=64)),
                ("voided_at", models.DateField(blank=True, null=True)),
                (
                    "bill_payment",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="budget_matches",
                        to="finance.billpayment",
                    ),
                ),
                (
                    "manual_entry",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="payment_matches",
                        to="director.budgetentry",
                    ),
                ),
                (
                    "manual_movement",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="payment_matches",
                        to="director.budgetspendmovement",
                    ),
                ),
                (
                    "request",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="payment_match",
                        to="director.budgetspendmovement",
                    ),
                ),
                (
                    "spend",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="payment_matches",
                        to="director.budgetspend",
                    ),
                ),
                (
                    "tax_remittance",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="budget_matches",
                        to="finance.taxremittance",
                    ),
                ),
                (
                    "void_request",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="voided_match",
                        to="director.budgetspendmovement",
                    ),
                ),
                (
                    "withholding_settlement",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="budget_matches",
                        to="finance.withholdingsettlement",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.CheckConstraint(
                        condition=(
                            models.Q(
                                manual_entry__isnull=False, manual_movement__isnull=True
                            )
                            | models.Q(
                                manual_entry__isnull=True, manual_movement__isnull=False
                            )
                        ),
                        name="director_match_manual_source",
                    ),
                    models.CheckConstraint(
                        condition=(
                            models.Q(
                                bill_payment__isnull=False,
                                withholding_settlement__isnull=True,
                                tax_remittance__isnull=True,
                            )
                            | models.Q(
                                bill_payment__isnull=True,
                                withholding_settlement__isnull=False,
                                tax_remittance__isnull=True,
                            )
                            | models.Q(
                                bill_payment__isnull=True,
                                withholding_settlement__isnull=True,
                                tax_remittance__isnull=False,
                            )
                        ),
                        name="director_match_finance_source",
                    ),
                ]
            },
        ),
    ]
