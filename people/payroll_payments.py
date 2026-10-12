"""Bank matching for approved payroll liabilities."""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum

from core.audit import record
from core.models import Organization
from finance import services as finance_services
from finance.models import BankTransaction

from .models import ZERO, PayrollPayment, PayrollRun
from .payroll_common import (
    READERS,
    WRITERS,
    _authorize,
    _money,
    _org,
    _save,
    _scoped,
    _totals,
)


def payment_position(*, organization, actor, run):
    org = Organization.objects.get(pk=getattr(organization, "pk", organization))
    _authorize(org, actor, READERS)
    try:
        run = PayrollRun.objects.filter(organization=org).get(
            pk=getattr(run, "pk", run)
        )
    except (PayrollRun.DoesNotExist, ValueError, TypeError) as exc:
        raise ValidationError("Payroll tidak tersedia untuk perusahaan ini.") from exc
    totals = _totals(run)
    paid = {
        row["kind"]: row["total"]
        for row in run.payments.values("kind").annotate(total=Sum("amount"))
    }
    expected = {
        PayrollPayment.Kind.NET_PAY: totals["net_pay"],
        PayrollPayment.Kind.PPH21: totals["pph21"],
        PayrollPayment.Kind.BPJS: totals["bpjs_total"],
    }
    return {
        kind: {
            "expected": value,
            "matched": paid.get(kind, ZERO),
            "remaining": value - paid.get(kind, ZERO),
        }
        for kind, value in expected.items()
    }


@transaction.atomic
def match_payment(*, organization, actor, run, transaction, kind, amount):
    org = _org(organization)
    _authorize(org, actor, WRITERS)
    run = _scoped(PayrollRun, run, org)
    bank_line = _scoped(BankTransaction, transaction, org)
    if run.status != PayrollRun.Status.POSTED:
        raise ValidationError(
            "Pembayaran hanya dapat dicocokkan setelah payroll disetujui."
        )
    if kind not in PayrollPayment.Kind.values:
        raise ValidationError("Pilih jenis pembayaran payroll.")
    value = _money(amount, positive=True)
    existing = PayrollPayment.objects.filter(
        organization=org, run=run, transaction=bank_line, kind=kind
    ).first()
    if existing:
        if existing.amount != value:
            raise ValidationError("Mutasi ini sudah dicocokkan dengan nominal berbeda.")
        return existing
    if bank_line.amount >= ZERO:
        raise ValidationError("Pilih mutasi keluar dari rekening BNI.")
    position = payment_position(organization=org, actor=actor, run=run)[kind]
    if value > position["remaining"]:
        raise ValidationError(
            "Nominal melebihi sisa payroll yang belum dibayar untuk jenis ini."
        )
    reference = f"PAYROLL-{run.pk}-{kind}-{bank_line.pk}"
    posting = finance_services.reconcile_payroll_payment(
        organization=org,
        actor=actor,
        transaction=bank_line,
        kind=kind,
        amount=value,
        reference=reference,
        evidence=f"Mutasi bank {bank_line.reference}; payroll {run.period_label} revisi {run.revision}.",
    )
    payment = _save(
        PayrollPayment(
            organization=org,
            run=run,
            transaction=bank_line,
            bank_posting=posting,
            kind=kind,
            amount=value,
            created_by=actor,
        )
    )
    record(
        org,
        actor,
        "people.payroll.payment_matched",
        obj=payment,
        detail={
            "run_id": run.pk,
            "kind": kind,
            "amount": str(value),
            "transaction_id": bank_line.pk,
        },
    )
    return payment
