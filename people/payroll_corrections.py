"""Owner-approved reversal of payroll accruals before bank settlement."""

import re
from datetime import date as Date
from datetime import datetime

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from core.audit import record
from core.models import DomainEvent
from finance import services as finance_services

from .models import PayrollRun
from .payroll_audit import _audit_detail
from .payroll_common import (
    APPROVERS,
    _authorize,
    _org,
    _reason,
    _save,
    _scoped,
    _version,
)


@transaction.atomic
def reverse_run(*, organization, actor, run, expected_version, reversal_date, reason):
    org = _org(organization)
    _authorize(org, actor, APPROVERS)
    run = _scoped(PayrollRun, run, org)
    _version(run, expected_version)
    reason = _reason(reason, "Alasan koreksi")
    if (
        run.status == PayrollRun.Status.REVERSED
        and run.reversal_date == reversal_date
        and run.reversal_reason == reason
    ):
        return run
    if run.status != PayrollRun.Status.POSTED or not run.journal_id:
        raise ValidationError("Hanya payroll terposting yang dapat dikoreksi.")
    if run.payments.exists():
        raise ValidationError(
            "Payroll sudah memiliki pembayaran bank. Koreksi harus didahului koreksi setiap pembayaran bank."
        )
    try:
        if isinstance(reversal_date, str) and re.fullmatch(
            r"\d{4}-\d{2}-\d{2}", reversal_date
        ):
            reversal_date = Date.fromisoformat(reversal_date)
        elif isinstance(reversal_date, datetime):
            raise ValueError
        if (
            not isinstance(reversal_date, Date)
            or reversal_date > timezone.localdate()
            or reversal_date.year > 2200
        ):
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            "Tanggal koreksi harus tanggal aktual yang valid."
        ) from exc
    journal = finance_services.reverse_payroll_accrual(
        organization=org,
        actor=actor,
        original=run.journal,
        posting_date=reversal_date,
        source_key=f"people.payroll.reverse:{run.pk}",
        description=f"Koreksi payroll {run.period_label} r{run.revision}",
    )
    run.status = PayrollRun.Status.REVERSED
    run.reversal_journal = journal
    run.reversed_by = actor
    run.reversed_at = timezone.now()
    run.reversal_date = reversal_date
    run.reversal_reason = reason
    run.version += 1
    _save(run)
    record(
        org,
        actor,
        "people.payroll.reversed",
        obj=run,
        detail={
            **_audit_detail(run, action="reversed"),
            "reason": reason,
            "reversal_date": reversal_date.isoformat(),
        },
    )
    DomainEvent.objects.get_or_create(
        organization=org,
        idempotency_key=f"people.payroll.reversed:{run.pk}",
        defaults={
            "topic": "people.payroll.reversed",
            "version": 1,
            "aggregate_type": "people.payrollrun",
            "aggregate_id": str(run.pk),
            "payload": {
                "year": run.year,
                "month": run.month,
                "revision": run.revision,
                "reversal_date": reversal_date.isoformat(),
                "reversal_journal_id": journal.pk,
            },
        },
    )
    return run
