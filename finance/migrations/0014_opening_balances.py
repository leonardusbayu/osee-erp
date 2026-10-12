"""Typed opening principal/cash only; no adopted amounts or business seeding."""

from decimal import Decimal

import django.db.models.deletion
from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import migrations, models


def event_fields():
    return [
        (
            "id",
            models.BigAutoField(
                auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
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
                on_delete=django.db.models.deletion.PROTECT, to="core.organization"
            ),
        ),
        (
            "actor",
            models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL
            ),
        ),
        (
            "journal",
            models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, to="finance.journal"
            ),
        ),
    ]


class Migration(migrations.Migration):
    dependencies = [
        ("finance", "0013_supplier_advances"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
        migrations.AlterField(
            model_name="bankposting",
            name="kind",
            field=models.CharField(
                max_length=30,
                choices=[
                    ("bank_fee", "Biaya bank"),
                    ("owner_funding", "Setoran modal pemilik"),
                    ("transfer_out", "Transfer keluar"),
                    ("transfer_in", "Transfer masuk"),
                    ("customer_advance", "Uang muka pelanggan"),
                    ("customer_refund", "Pengembalian pelanggan"),
                    ("withheld_supplier", "Pembayaran bersih pemasok"),
                    ("tax_remittance", "Penyetoran potongan pajak"),
                    (
                        "customer_tax_receipt",
                        "Penerimaan pelanggan setelah potongan pajak",
                    ),
                    ("own_tax_payment", "Pembayaran pajak perusahaan"),
                    ("supplier_advance", "Uang muka pemasok"),
                    ("supplier_advance_refund", "Pengembalian uang muka pemasok"),
                    ("opening_receipt", "Penerimaan piutang lama"),
                    ("opening_payment", "Pembayaran utang lama"),
                ],
            ),
        ),
        migrations.CreateModel(
            name="OpeningBalanceItem",
            fields=event_fields()
            + [
                (
                    "kind",
                    models.CharField(
                        max_length=20,
                        choices=[
                            ("receivable", "Piutang pelanggan lama"),
                            ("payable", "Utang pemasok lama"),
                        ],
                    ),
                ),
                ("source_date", models.DateField()),
                ("source_reference", models.CharField(max_length=160)),
                ("source_identity", models.CharField(max_length=480, editable=False)),
                (
                    "party",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="opening_balances",
                        to="finance.party",
                    ),
                ),
            ],
            options={
                "ordering": ["date", "pk"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("organization", "request_key"),
                        name="fin_opening_item_request",
                    ),
                    models.UniqueConstraint(
                        fields=("organization", "kind", "party", "source_identity"),
                        name="fin_opening_item_source",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(amount__gt=0),
                        name="fin_opening_item_positive",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(source_date__lte=models.F("date")),
                        name="fin_opening_source_date",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="OpeningBalanceSettlement",
            fields=event_fields()
            + [
                (
                    "item",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="settlements",
                        to="finance.openingbalanceitem",
                    ),
                ),
                (
                    "bank_posting",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="opening_settlement",
                        to="finance.bankposting",
                    ),
                ),
            ],
            options={
                "ordering": ["date", "pk"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("organization", "request_key"),
                        name="fin_opening_settle_request",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(amount__gt=0),
                        name="fin_opening_settle_positive",
                    ),
                ],
            },
        ),
    ]
