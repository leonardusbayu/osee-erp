"""Audited employee and payroll line editing workflows."""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction

from core.audit import record

from .models import ZERO, PayrollLine, PayrollPerson, PayrollRun
from .payroll_common import (
    WRITERS,
    _authorize,
    _money,
    _org,
    _period_values,
    _reason,
    _save,
    _scoped,
)


@transaction.atomic
def add_person(*, organization, actor, name, kind):
    org = _org(organization)
    _authorize(org, actor, WRITERS)
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 150:
        raise ValidationError(
            "Nama karyawan atau tutor wajib diisi, maksimal 150 karakter."
        )
    if kind not in PayrollPerson.Kind.values:
        raise ValidationError("Pilih jenis orang yang tersedia.")
    person = _save(
        PayrollPerson(organization=org, name=name.strip(), kind=kind, created_by=actor)
    )
    record(
        org, actor, "people.person.created", obj=person, detail={"kind": person.kind}
    )
    return person


@transaction.atomic
def set_person_active(*, organization, actor, person, active):
    org = _org(organization)
    _authorize(org, actor, WRITERS)
    if type(active) is not bool:
        raise ValidationError("Status orang tidak valid.")
    person = _scoped(PayrollPerson, person, org)
    if person.active != active:
        person.active = active
        _save(person)
        record(
            org,
            actor,
            "people.person.status_changed",
            obj=person,
            detail={"active": active},
        )
    return person


@transaction.atomic
def create_run(*, organization, actor, year, month):
    org = _org(organization)
    _authorize(org, actor, WRITERS)
    year, month = _period_values(year, month)
    previous = (
        PayrollRun.objects.select_for_update()
        .filter(organization=org, year=year, month=month)
        .order_by("-revision")
        .first()
    )
    if previous and previous.status not in {
        PayrollRun.Status.REVERSED,
        PayrollRun.Status.REJECTED,
    }:
        raise ValidationError(
            "Payroll periode ini sudah ada. Buka periode tersebut untuk melanjutkan."
        )
    revision = (previous.revision + 1) if previous else 1
    run = _save(
        PayrollRun(
            organization=org,
            year=year,
            month=month,
            revision=revision,
            created_by=actor,
        )
    )
    record(
        org,
        actor,
        "people.payroll.created",
        obj=run,
        detail={"year": year, "month": month, "revision": revision},
    )
    return run


@transaction.atomic
def add_line(
    *,
    organization,
    actor,
    run,
    person,
    gross_amount,
    pph21_amount=ZERO,
    bpjs_employee_amount=ZERO,
    bpjs_employer_amount=ZERO,
    basis_reference="",
    note="",
):
    org = _org(organization)
    _authorize(org, actor, WRITERS)
    run = _scoped(PayrollRun, run, org)
    person = _scoped(PayrollPerson, person, org)
    if run.status != PayrollRun.Status.DRAFT:
        raise ValidationError(
            "Rincian hanya dapat diubah selama payroll masih berstatus draf."
        )
    if not person.active:
        raise ValidationError(
            "Orang yang sudah dinonaktifkan tidak dapat ditambahkan ke payroll baru."
        )
    basis_reference = (
        basis_reference.strip() if isinstance(basis_reference, str) else ""
    )
    note = note.strip() if isinstance(note, str) else ""
    if len(basis_reference) > 240 or len(note) > 500:
        raise ValidationError(
            "Rujukan maksimal 240 karakter dan catatan maksimal 500 karakter."
        )
    line = PayrollLine(
        organization=org,
        run=run,
        person=person,
        gross_amount=_money(gross_amount, positive=True),
        pph21_amount=_money(pph21_amount),
        bpjs_employee_amount=_money(bpjs_employee_amount),
        bpjs_employer_amount=_money(bpjs_employer_amount),
        basis_reference=basis_reference,
        note=note,
    )
    _save(line)
    run.version += 1
    _save(run)
    record(
        org,
        actor,
        "people.payroll.line_added",
        obj=run,
        detail={
            "person_id": person.pk,
            "line_id": line.pk,
            "period": f"{run.year:04d}-{run.month:02d}",
        },
    )
    return line


@transaction.atomic
def update_line(
    *,
    organization,
    actor,
    run,
    line,
    person,
    gross_amount,
    pph21_amount=ZERO,
    bpjs_employee_amount=ZERO,
    bpjs_employer_amount=ZERO,
    basis_reference="",
    note="",
):
    org = _org(organization)
    _authorize(org, actor, WRITERS)
    run = _scoped(PayrollRun, run, org)
    line = _scoped(PayrollLine, line, org)
    person = _scoped(PayrollPerson, person, org)
    if line.run_id != run.pk or run.status != PayrollRun.Status.DRAFT:
        raise ValidationError(
            "Rincian hanya dapat diubah pada payroll draf yang sesuai."
        )
    if not person.active:
        raise ValidationError("Pilih karyawan/tutor yang masih aktif.")
    basis_reference = (
        basis_reference.strip() if isinstance(basis_reference, str) else ""
    )
    note = note.strip() if isinstance(note, str) else ""
    if len(basis_reference) > 240 or len(note) > 500:
        raise ValidationError(
            "Rujukan maksimal 240 karakter dan catatan maksimal 500 karakter."
        )
    new_values = {
        "person_id": person.pk,
        "gross_amount": _money(gross_amount, positive=True),
        "pph21_amount": _money(pph21_amount),
        "bpjs_employee_amount": _money(bpjs_employee_amount),
        "bpjs_employer_amount": _money(bpjs_employer_amount),
        "basis_reference": basis_reference,
        "note": note,
    }
    old_values = {
        key: str(getattr(line, key)) if key.endswith("_amount") else getattr(line, key)
        for key in new_values
        if key != "person_id"
    }
    old_values["person_id"] = line.person_id
    for key, value in new_values.items():
        setattr(line, key, value)
    _save(line)
    run.version += 1
    _save(run)
    record(
        org,
        actor,
        "people.payroll.line_corrected",
        obj=run,
        detail={
            "line_id": line.pk,
            "previous": old_values,
            "current": {
                key: str(value) if isinstance(value, Decimal) else value
                for key, value in new_values.items()
            },
        },
    )
    return line


@transaction.atomic
def remove_line(*, organization, actor, run, line, reason):
    org = _org(organization)
    _authorize(org, actor, WRITERS)
    run = _scoped(PayrollRun, run, org)
    line = _scoped(PayrollLine, line, org)
    reason = _reason(reason, "Alasan penghapusan rincian")
    if line.run_id != run.pk or run.status != PayrollRun.Status.DRAFT:
        raise ValidationError(
            "Rincian hanya dapat dihapus dari payroll draf yang sesuai."
        )
    record(
        org,
        actor,
        "people.payroll.line_removed",
        obj=run,
        detail={
            "line_id": line.pk,
            "person_id": line.person_id,
            "gross": str(line.gross_amount),
            "pph21": str(line.pph21_amount),
            "bpjs_employee": str(line.bpjs_employee_amount),
            "bpjs_employer": str(line.bpjs_employer_amount),
            "reason": reason,
        },
    )
    line._service_transition = True
    line.delete()
    run.version += 1
    _save(run)
    return run
