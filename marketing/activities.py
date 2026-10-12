"""Audited activity commands; no lead-stage or financial postings."""

import re
from datetime import date, datetime

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from director.models import Budget, Decision

from .activity_models import WorkActivity
from .kpi_models import KpiTarget
from .models import Lead
from .services import _audit, _fields, _org, _save, _scoped, _snapshot, _version

ACTIVITY_FIELDS = {
    "title",
    "owner",
    "channel",
    "campaign",
    "lead",
    "kind",
    "due_date",
    "description",
    "kpi_target",
    "decision",
    "budget",
}
ACTIVITY_SNAPSHOT_FIELDS = ACTIVITY_FIELDS | {
    "current_owner",
    "status",
    "occurred_on",
    "evidence",
    "outcome",
    "version",
    "created_by",
    "submitted_by",
    "submitted_at",
    "reviewed_by",
    "reviewed_at",
    "review_notes",
    "evidence_manifest",
}
ACTIVITY_REVIEWERS = {"owner", "director"}
OPERATIONS = {
    "start": ({"planned", "needs_revision"}, "in_progress", set(), "started"),
    "submit": (
        {"planned", "in_progress", "needs_revision"},
        "submitted",
        {"evidence", "outcome", "occurred_on"},
        "submitted",
    ),
    "accept": ({"submitted"}, "accepted", {"review_notes"}, "accepted"),
    "return": ({"submitted"}, "needs_revision", {"review_notes"}, "returned"),
    "withdraw": ({"submitted"}, "in_progress", {"review_notes"}, "withdrawn"),
    "cancel": (
        {"planned", "in_progress", "needs_revision"},
        "cancelled",
        {"review_notes"},
        "cancelled",
    ),
}


def _save_activity(obj):
    if obj.pk is None and obj.current_owner_id is None and obj.owner_id is not None:
        obj.current_owner = obj.owner
    obj._activity_service_write = True
    return _save(obj)


def _required_text(value, field):
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 4000
        or "\x00" in value
    ):
        raise ValidationError(
            {field: "Wajib diisi dengan teks, maksimal 4.000 karakter."}
        )
    return value.strip()


def require_work_responsibility(*, organization, actor, work, reason=""):
    from core.models import Membership

    from .workflow import can_act_for_member

    if can_act_for_member(
        organization=organization, actor=actor, member=work.assigned_owner
    ):
        return ""
    if Membership.objects.filter(
        organization=organization,
        user=actor,
        user__is_active=True,
        role__in=ACTIVITY_REVIEWERS,
    ).exists():
        return _required_text(reason, "responsibility_reason")
    raise PermissionDenied(
        "Pekerjaan hanya dapat dijalankan PIC yang terhubung ke akun atau penerima delegasi aktif."
    )


def _work_date(value):
    try:
        parsed = (
            date.fromisoformat(value)
            if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)
            else value
        )
        if (
            not isinstance(parsed, date)
            or isinstance(parsed, datetime)
            or not 2000 <= parsed.year <= 2200
            or parsed > timezone.localdate()
        ):
            raise ValueError
        return parsed
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            {
                "occurred_on": "Tanggal pelaksanaan wajib valid (YYYY-MM-DD), dari tahun 2000 hingga hari ini."
            }
        ) from exc


@transaction.atomic
def create_activity(*, organization, actor, **fields):
    org = _org(organization, actor)
    if set(fields) - ACTIVITY_FIELDS:
        raise ValidationError(
            "Ada kolom yang tidak boleh diisi melalui pembuatan aktivitas."
        )
    values = dict(fields)
    lead = values.pop("lead", None)
    links = {}
    for key, model in (
        ("kpi_target", KpiTarget),
        ("decision", Decision),
        ("budget", Budget),
    ):
        item = values.pop(key, None)
        links[key] = None if item in (None, "") else _scoped(model, item, org)
    if links["decision"] and links["decision"].status not in {"approved", "active"}:
        raise ValidationError(
            "Pekerjaan keputusan harus mengacu keputusan yang disetujui atau berjalan."
        )
    if links["budget"] and (
        links["budget"].status != "approved"
        or links["budget"].channel != fields.get("channel")
    ):
        raise ValidationError("Pilih budget yang disetujui pada kanal pekerjaan.")
    values = _fields(
        values, ACTIVITY_FIELDS - {"lead", "kpi_target", "decision", "budget"}, org
    )
    values["lead"] = None if lead in (None, "") else _scoped(Lead, lead, org)
    obj = _save_activity(
        WorkActivity(organization=org, created_by=actor, **values, **links)
    )
    _audit(
        org,
        actor,
        "activity.created",
        obj,
        before={},
        after=_snapshot(obj, ACTIVITY_SNAPSHOT_FIELDS),
        version=obj.version,
    )
    return obj


@transaction.atomic
def correct_activity(
    *, organization, actor, activity, expected_version, reason, due_date
):
    org = _org(organization, actor, ACTIVITY_REVIEWERS)
    original = _scoped(WorkActivity, activity, org)
    _version(original, expected_version)
    reason = _required_text(reason, "reason")
    if (
        original.status != "accepted"
        or WorkActivity.objects.filter(organization=org, replaces=original).exists()
    ):
        raise ValidationError(
            "Koreksi hanya untuk hasil diterima yang belum mempunyai pengganti."
        )
    values = {key: getattr(original, key) for key in ACTIVITY_FIELDS}
    values.update(_fields({"due_date": due_date}, {"due_date"}, org))
    replacement = _save_activity(
        WorkActivity(
            organization=org,
            created_by=actor,
            replaces=original,
            current_owner=original.assigned_owner,
            correction_reason=reason,
            **values,
        )
    )
    _audit(
        org,
        actor,
        "activity.correction_created",
        replacement,
        replaces_id=original.pk,
        reason=reason,
        source_status=original.status,
        after=_snapshot(replacement, ACTIVITY_SNAPSHOT_FIELDS),
    )
    return replacement


@transaction.atomic
def handoff_activity(
    *, organization, actor, activity, expected_version, member, reason
):
    """Change today's executor while retaining the original plan and previous evidence."""
    from .activity_models import ActivityAssignment
    from .models import TeamMember
    from .workflow import _account

    org = _org(organization, actor, ACTIVITY_REVIEWERS)
    work = _scoped(WorkActivity, activity, org)
    _version(work, expected_version)
    reason = _required_text(reason, "reason")
    if work.status not in {"planned", "in_progress", "needs_revision"}:
        raise ValidationError(
            "Alih tugas hanya untuk pekerjaan yang belum diajukan atau perlu perbaikan. Hasil yang diajukan harus diperiksa atau ditarik dahulu."
        )
    recipient = _scoped(TeamMember, member, org)
    if not recipient.active or recipient.account_id is None:
        raise ValidationError(
            {"member": "Pilih staf aktif yang sudah terhubung ke akun perusahaan."}
        )
    _account(org, recipient.account_id)
    previous = work.assigned_owner
    if previous.pk == recipient.pk:
        raise ValidationError({"member": "Pekerjaan sudah ditugaskan kepada staf ini."})
    before = _snapshot(work, ACTIVITY_SNAPSHOT_FIELDS)
    work.current_owner, work.version = recipient, work.version + 1
    work._activity_handoff = True
    _save_activity(work)
    event = ActivityAssignment(
        organization=org,
        created_by=actor,
        activity=work,
        previous_member=previous,
        member=recipient,
        reason=reason,
        activity_version=work.version,
    )
    event._assignment_service_write = True
    _save(event)
    _audit(
        org,
        actor,
        "activity.handed_off",
        work,
        before=before,
        after=_snapshot(work, ACTIVITY_SNAPSHOT_FIELDS),
        reason=reason,
        assignment_id=event.pk,
        effective_at=event.effective_at.isoformat(),
        origin_owner_id=work.owner_id,
        version=work.version,
    )
    return work


@transaction.atomic
def update_activity(
    *,
    organization,
    actor,
    activity,
    expected_version,
    operation,
    responsibility_reason="",
    **fields,
):
    if not isinstance(operation, str) or operation not in OPERATIONS:
        raise ValidationError("Tindakan aktivitas tidak dikenal.")
    org = (
        _org(organization, actor, roles=ACTIVITY_REVIEWERS)
        if operation in {"accept", "return"}
        else _org(organization, actor)
    )
    obj = _scoped(WorkActivity, activity, org)
    _version(obj, expected_version)
    allowed_statuses, next_status, allowed_fields, event = OPERATIONS[operation]
    if obj.status not in allowed_statuses:
        raise ValidationError(
            "Tindakan tidak sesuai status aktivitas saat ini. Muat ulang halaman."
        )
    if set(fields) - allowed_fields:
        raise ValidationError("Ada kolom yang tidak boleh diubah melalui tindakan ini.")
    if operation in {"start", "submit"} and not obj.assigned_owner.active:
        raise ValidationError(
            "Penanggung jawab harus anggota tim aktif untuk menjalankan atau mengajukan aktivitas."
        )
    if operation in {"start", "submit", "cancel"}:
        responsibility_reason = require_work_responsibility(
            organization=org, actor=actor, work=obj, reason=responsibility_reason
        )
    if operation in {"accept", "return"} and actor.pk == obj.submitted_by_id:
        raise PermissionDenied("Pemeriksa harus berbeda dari pengaju bukti aktivitas.")
    if operation == "withdraw" and actor.pk != obj.submitted_by_id:
        raise PermissionDenied(
            "Hanya pengaju bukti ini yang dapat menarik pengajuan untuk diperbaiki."
        )
    before = _snapshot(obj, ACTIVITY_SNAPSHOT_FIELDS)
    if operation == "submit":
        from .work_evidence import active_evidence_manifest

        obj.evidence_manifest = active_evidence_manifest(obj)
        obj.evidence = _required_text(fields.get("evidence"), "evidence")
        obj.outcome = _required_text(fields.get("outcome"), "outcome")
        obj.occurred_on = _work_date(fields.get("occurred_on"))
        obj.submitted_by = actor
        obj.submitted_at = timezone.now()
        obj.reviewed_by = None
        obj.reviewed_at = None
        obj.review_notes = ""
    elif operation in {"accept", "return", "cancel", "withdraw"}:
        obj.review_notes = _required_text(fields.get("review_notes"), "review_notes")
        if operation == "accept":
            from .work_evidence import active_evidence_manifest

            if active_evidence_manifest(obj) != obj.evidence_manifest:
                raise ValidationError(
                    "Bukti berubah atau tidak tersedia; tarik pengajuan dan periksa ulang."
                )
        if operation in {"accept", "return"}:
            obj.reviewed_by = actor
            obj.reviewed_at = timezone.now()
        else:
            # Withdrawal/cancellation reasons belong to their audit actor, not a prior reviewer.
            obj.reviewed_by = None
            obj.reviewed_at = None
    obj.status = next_status
    obj.version += 1
    _save_activity(obj)
    if operation == "accept":
        from .action_workflow import close_linked_action

        close_linked_action(organization=org, actor=actor, work=obj)
    _audit(
        org,
        actor,
        "activity." + event,
        obj,
        before=before,
        after=_snapshot(obj, ACTIVITY_SNAPSHOT_FIELDS),
        version=obj.version,
        responsibility_reason=responsibility_reason,
    )
    return obj
