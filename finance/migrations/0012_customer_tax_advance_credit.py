import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("finance", "0011_customer_tax_late_credit")]

    operations = [
        migrations.RemoveConstraint(
            model_name="customertaxsettlement", name="finance_customer_tax_one_proof"
        ),
        migrations.AddField(
            model_name="customertaxsettlement",
            name="source_advance_application",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="tax_credit_settlements",
                to="finance.advanceapplication",
            ),
        ),
        migrations.AddConstraint(
            model_name="customertaxsettlement",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        bank_posting__isnull=False,
                        source_allocation__isnull=True,
                        source_advance_application__isnull=True,
                        cash_amount__gt=0,
                    )
                    | models.Q(
                        bank_posting__isnull=True,
                        source_allocation__isnull=False,
                        source_advance_application__isnull=True,
                        cash_amount=0,
                    )
                    | models.Q(
                        bank_posting__isnull=True,
                        source_allocation__isnull=True,
                        source_advance_application__isnull=False,
                        cash_amount=0,
                    )
                ),
                name="finance_customer_tax_one_proof",
            ),
        ),
    ]
