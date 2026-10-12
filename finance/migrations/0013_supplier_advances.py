"""Typed supplier cash history only; no inferred adoption or business data."""

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
        ("source_reference", models.CharField(max_length=160, blank=True)),
        ("reason", models.TextField(max_length=4000)),
        ("request_key", models.UUIDField()),
        ("request_payload", models.JSONField()),
        ("source_snapshot", models.JSONField()),
        ("source_hash", models.CharField(max_length=64)),
        (
            "workflow_version",
            models.CharField(default="supplier-advance-v1", max_length=40),
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
        ("finance", "0012_customer_tax_advance_credit"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
        migrations.AlterField(
            model_name="journalline",
            name="account",
            field=models.CharField(
                max_length=30,
                choices=[
                    ("BANK", "Bank"),
                    ("AR", "Piutang usaha"),
                    ("DEFERRED_REVENUE", "Pendapatan diterima di muka"),
                    ("REVENUE", "Pendapatan"),
                    ("EXPENSE", "Beban"),
                    ("PREPAID", "Biaya dibayar di muka"),
                    ("AP", "Utang usaha"),
                    ("EQUITY", "Modal / saldo awal yang ditinjau"),
                    ("BANK_TRANSFER", "Transfer antarbank dalam perjalanan"),
                    ("CUSTOMER_ADVANCE", "Uang muka pelanggan belum diterapkan"),
                    ("CUSTOMER_REFUND", "Utang pengembalian pelanggan"),
                    ("TAX_PAYABLE", "Utang pajak potongan"),
                    ("OWN_TAX_PAYABLE", "Utang pajak penghasilan perusahaan"),
                    ("TAX_RECEIVABLE", "Kredit pajak potongan pelanggan"),
                    ("TAX_EXPENSE", "Beban pajak penghasilan perusahaan"),
                    ("SUPPLIER_ADVANCE", "Uang muka pemasok"),
                ],
            ),
        ),
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
                ],
            ),
        ),
        migrations.CreateModel(
            name="SupplierAdvance",
            fields=event_fields()
            + [
                ("purpose", models.CharField(blank=True, max_length=300)),
                (
                    "supplier",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="supplier_advances",
                        to="finance.party",
                    ),
                ),
                (
                    "bank_posting",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="supplier_advance",
                        to="finance.bankposting",
                    ),
                ),
            ],
            options={
                "ordering": ["-date", "-pk"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("organization", "request_key"),
                        name="fin_supplier_advance_request",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(amount__gt=0),
                        name="fin_supplier_advance_positive",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="SupplierAdvanceApplication",
            fields=event_fields()
            + [
                (
                    "advance",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="applications",
                        to="finance.supplieradvance",
                    ),
                ),
                (
                    "bill",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="supplier_advance_applications",
                        to="finance.bill",
                    ),
                ),
            ],
            options={
                "ordering": ["date", "pk"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("organization", "request_key"),
                        name="fin_supplier_apply_request",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(amount__gt=0),
                        name="fin_supplier_apply_positive",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="SupplierAdvanceRefund",
            fields=event_fields()
            + [
                (
                    "advance",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="refunds",
                        to="finance.supplieradvance",
                    ),
                ),
                (
                    "bank_posting",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="supplier_advance_refund",
                        to="finance.bankposting",
                    ),
                ),
            ],
            options={
                "ordering": ["date", "pk"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("organization", "request_key"),
                        name="fin_supplier_refund_request",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(amount__gt=0),
                        name="fin_supplier_refund_positive",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="SupplierAdvanceApplicationReversal",
            fields=event_fields()
            + [
                (
                    "application",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="reversal",
                        to="finance.supplieradvanceapplication",
                    ),
                )
            ],
            options={
                "ordering": ["date", "pk"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("organization", "request_key"),
                        name="fin_supplier_reverse_request",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(amount__gt=0),
                        name="fin_supplier_reverse_positive",
                    ),
                ],
            },
        ),
    ]
