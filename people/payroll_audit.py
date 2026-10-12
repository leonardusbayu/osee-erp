"""Private audit summaries for payroll changes and approvals."""

from django.core.exceptions import ValidationError
from django.db.models import CharField, Q
from django.db.models.functions import Cast

from core.models import AuditEvent, Organization

from .models import PayrollRun
from .payroll_common import READERS, _authorize, _totals


def _audit_detail(run, *, action):
    totals = _totals(run)
    return {
        "action": action,
        "year": run.year,
        "month": run.month,
        "revision": run.revision,
        "people_count": totals["people_count"],
        "gross": str(totals["gross"]),
        "pph21": str(totals["pph21"]),
        "bpjs": str(totals["bpjs_total"]),
        "employer_cost": str(totals["employer_cost"]),
    }


def _summary_added(detail):
    return f"Rincian orang #{detail.get('person_id')} ditambahkan."


def _summary_corrected(detail):
    before, after = detail.get("previous", {}), detail.get("current", {})
    return (
        f"Rincian #{detail.get('line_id')} diubah: jumlah sebelum potongan {before.get('gross_amount')} → {after.get('gross_amount')}; "
        f"PPh 21 {before.get('pph21_amount')} → {after.get('pph21_amount')}; BPJS karyawan {before.get('bpjs_employee_amount')} → {after.get('bpjs_employee_amount')}; "
        f"BPJS perusahaan {before.get('bpjs_employer_amount')} → {after.get('bpjs_employer_amount')}."
    )


def _summary_removed(detail):
    return f"Rincian orang #{detail.get('person_id')} dikeluarkan dari draf. Alasan: {detail.get('reason')}"


def _summary_payment(detail):
    return f"{detail.get('kind')} · Rp{detail.get('amount')} · mutasi #{detail.get('transaction_id')}"


def _summary_correction(detail):
    return detail.get("reason", detail.get("reversal_date", "Alasan koreksi tercatat."))


def _summary_totals(detail):
    return (
        f"{detail.get('people_count', 0)} orang · sebelum potongan Rp{detail.get('gross', '0')} · "
        f"PPh 21 Rp{detail.get('pph21', '0')} · BPJS Rp{detail.get('bpjs', '0')}"
    )


SUMMARY_HANDLERS = {
    "people.payroll.line_added": _summary_added,
    "people.payroll.line_corrected": _summary_corrected,
    "people.payroll.line_removed": _summary_removed,
    "people.payroll.payment_matched": _summary_payment,
    "people.payroll.rejected": _summary_correction,
    "people.payroll.reversed": _summary_correction,
}


def audit_timeline(*, organization, actor, run):
    org = Organization.objects.get(pk=getattr(organization, "pk", organization))
    _authorize(org, actor, READERS)
    run = (
        PayrollRun.objects.filter(organization=org)
        .filter(pk=getattr(run, "pk", run))
        .first()
    )
    if run is None:
        raise ValidationError("Payroll tidak tersedia untuk perusahaan ini.")
    payment_ids = run.payments.annotate(
        pk_text=Cast("pk", output_field=CharField())
    ).values("pk_text")
    rows = (
        AuditEvent.objects.filter(
            organization=org, action__startswith="people.payroll."
        )
        .filter(
            Q(object_type="PayrollRun", object_id=str(run.pk))
            | Q(object_type="PayrollPayment", object_id__in=payment_ids)
        )
        .select_related("actor")
        .order_by("-created_at", "-pk")[:100]
    )
    labels = {
        "people.payroll.created": "Payroll disiapkan",
        "people.payroll.line_added": "Rincian ditambahkan",
        "people.payroll.line_corrected": "Rincian dikoreksi",
        "people.payroll.line_removed": "Rincian dihapus dari draf",
        "people.payroll.submitted": "Diajukan untuk persetujuan",
        "people.payroll.approved": "Disetujui dan dicatat",
        "people.payroll.rejected": "Diminta untuk diperbaiki",
        "people.payroll.reversed": "Jurnal pembalik dicatat",
        "people.payroll.payment_matched": "Pembayaran dicocokkan ke bank",
    }
    timeline = []
    for event in rows:
        detail = event.detail
        summary = SUMMARY_HANDLERS.get(event.action, _summary_totals)(detail)
        timeline.append(
            {
                "label": labels.get(event.action, "Aktivitas payroll"),
                "summary": summary,
                "actor": event.actor.get_full_name() or event.actor.username
                if event.actor
                else "Akun dinonaktifkan",
                "created_at": event.created_at,
            }
        )
    return timeline
