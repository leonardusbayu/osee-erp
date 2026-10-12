"""Controlled HR/payroll workflows; all tax and BPJS amounts stay manual."""

import calendar
from datetime import date as Date

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from core.audit import record
from core.models import DomainEvent, Organization
from finance import services as finance_services

from .models import PayrollPerson, PayrollRun
from .payroll_audit import _audit_detail, audit_timeline
from .payroll_common import (
    APPROVERS,
    READERS,
    WRITERS,
    _authorize,
    _org,
    _reason,
    _save,
    _scoped,
    _totals,
    _version,
)
from .payroll_corrections import reverse_run
from .payroll_entries import (
    add_line,
    add_person,
    create_run,
    remove_line,
    set_person_active,
    update_line,
)
from .payroll_payments import match_payment, payment_position

__all__ = [
    "add_line",
    "add_person",
    "approve_run",
    "audit_timeline",
    "create_run",
    "match_payment",
    "payment_position",
    "payroll_summary",
    "reject_run",
    "remove_line",
    "reverse_run",
    "set_person_active",
    "submit_run",
    "update_line",
]


@transaction.atomic
def submit_run(*, organization, actor, run, expected_version):
    org = _org(organization)
    _authorize(org, actor, WRITERS)
    run = _scoped(PayrollRun, run, org)
    _version(run, expected_version)
    if run.status != PayrollRun.Status.DRAFT:
        raise ValidationError("Hanya payroll draf yang dapat diajukan.")
    if not run.lines.exists():
        raise ValidationError(
            "Tambahkan setidaknya satu karyawan atau tutor sebelum mengajukan."
        )
    run.status = PayrollRun.Status.SUBMITTED
    run.submitted_by = actor
    run.submitted_at = timezone.now()
    run.version += 1
    _save(run)
    record(
        org,
        actor,
        "people.payroll.submitted",
        obj=run,
        detail=_audit_detail(run, action="submitted"),
    )
    return run


@transaction.atomic
def approve_run(*, organization, actor, run, expected_version, approval_note):
    org = _org(organization)
    _authorize(org, actor, APPROVERS)
    run = _scoped(PayrollRun, run, org)
    _version(run, expected_version)
    if run.status != PayrollRun.Status.SUBMITTED:
        raise ValidationError("Payroll harus diajukan Finance sebelum disetujui.")
    if run.submitted_by_id == actor.pk:
        raise PermissionDenied(
            "Persetujuan harus dilakukan oleh Owner/Direktur, terpisah dari pembuat pengajuan."
        )
    approval_note = _reason(approval_note, "Catatan pemeriksaan")
    totals = _totals(run)
    posting_date = Date(
        run.year, run.month, calendar.monthrange(run.year, run.month)[1]
    )
    if posting_date > timezone.localdate():
        raise ValidationError(
            "Payroll hanya dapat disetujui setelah hari terakhir periodenya."
        )
    journal = finance_services.post_payroll_accrual(
        organization=org,
        actor=actor,
        posting_date=posting_date,
        source_key=f"people.payroll:{run.pk}",
        description=f"Payroll {run.period_label} r{run.revision}",
        gross=totals["gross"],
        pph21=totals["pph21"],
        bpjs_employee=totals["bpjs_employee"],
        bpjs_employer=totals["bpjs_employer"],
    )
    run.status = PayrollRun.Status.POSTED
    run.approved_by = actor
    run.approved_at = timezone.now()
    run.approval_note = approval_note
    run.journal = journal
    run.version += 1
    _save(run)
    record(
        org,
        actor,
        "people.payroll.approved",
        obj=run,
        detail=_audit_detail(run, action="approved"),
    )
    DomainEvent.objects.get_or_create(
        organization=org,
        idempotency_key=f"people.payroll.posted:{run.pk}",
        defaults={
            "topic": "people.payroll.posted",
            "version": 1,
            "aggregate_type": "people.payrollrun",
            "aggregate_id": str(run.pk),
            "payload": _audit_detail(run, action="posted"),
        },
    )
    return run


@transaction.atomic
def reject_run(*, organization, actor, run, expected_version, rejection_note):
    org = _org(organization)
    _authorize(org, actor, APPROVERS)
    run = _scoped(PayrollRun, run, org)
    _version(run, expected_version)
    if run.status != PayrollRun.Status.SUBMITTED:
        raise ValidationError("Hanya payroll yang menunggu persetujuan dapat ditolak.")
    if run.submitted_by_id == actor.pk:
        raise PermissionDenied(
            "Pemeriksa harus berbeda dari pembuat pengajuan payroll."
        )
    run.rejection_note = _reason(rejection_note, "Alasan revisi")
    run.rejected_by = actor
    run.rejected_at = timezone.now()
    run.status = PayrollRun.Status.REJECTED
    run.version += 1
    _save(run)
    record(
        org,
        actor,
        "people.payroll.rejected",
        obj=run,
        detail={
            **_audit_detail(run, action="rejected"),
            "reason": run.rejection_note,
        },
    )
    DomainEvent.objects.get_or_create(
        organization=org,
        idempotency_key=f"people.payroll.rejected:{run.pk}",
        defaults={
            "topic": "people.payroll.rejected",
            "version": 1,
            "aggregate_type": "people.payrollrun",
            "aggregate_id": str(run.pk),
            "payload": {
                "year": run.year,
                "month": run.month,
                "revision": run.revision,
                "reason": run.rejection_note,
            },
        },
    )
    return run


def payroll_summary(*, organization, actor):
    org = Organization.objects.get(pk=getattr(organization, "pk", organization))
    _authorize(org, actor, READERS)
    return {
        "people": PayrollPerson.objects.filter(organization=org).order_by(
            "-active", "name"
        ),
        "runs": PayrollRun.objects.filter(organization=org)
        .select_related("created_by", "submitted_by", "approved_by")
        .prefetch_related("lines__person")
        .order_by("-year", "-month", "-revision")[:50],
        "pending_approval": PayrollRun.objects.filter(
            organization=org, status=PayrollRun.Status.SUBMITTED
        ).count(),
    }
