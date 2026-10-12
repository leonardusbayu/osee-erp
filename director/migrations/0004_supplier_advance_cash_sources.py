import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("director", "0003_budget_spends"),
        ("finance", "0013_supplier_advances"),
    ]
    operations = [
        migrations.RemoveConstraint(
            model_name="budgetpaymentmatch", name="director_match_finance_source"
        ),
        migrations.AddField(
            model_name="budgetpaymentmatch",
            name="supplier_advance_application",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="budget_matches",
                to="finance.supplieradvanceapplication",
            ),
        ),
        migrations.AddConstraint(
            model_name="budgetpaymentmatch",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        bill_payment__isnull=False,
                        withholding_settlement__isnull=True,
                        tax_remittance__isnull=True,
                        supplier_advance_application__isnull=True,
                    )
                    | models.Q(
                        bill_payment__isnull=True,
                        withholding_settlement__isnull=False,
                        tax_remittance__isnull=True,
                        supplier_advance_application__isnull=True,
                    )
                    | models.Q(
                        bill_payment__isnull=True,
                        withholding_settlement__isnull=True,
                        tax_remittance__isnull=False,
                        supplier_advance_application__isnull=True,
                    )
                    | models.Q(
                        bill_payment__isnull=True,
                        withholding_settlement__isnull=True,
                        tax_remittance__isnull=True,
                        supplier_advance_application__isnull=False,
                    )
                ),
                name="director_match_finance_source",
            ),
        ),
    ]
