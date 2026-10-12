"""Audited team responsibility and daily workflow commands."""

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from core.models import Membership

from .ad_sources import active_ad_observations
from .models import Campaign, Lead, TeamMember
from .services import _audit, _org, _save, _scoped, _version
from .workflow_models import (
    CampaignEvent,
    LeadAssignment,
    LeadContact,
    MemberDelegation,
    WorkPolicy,
)

MANAGERS = {"owner", "director"}


def _reason(value, maximum=1000):
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or "\x00" in value
    ):
        raise ValidationError(f"Alasan wajib diisi, maksimal {maximum} karakter.")
    return value.strip()


def _workflow_save(obj):
    obj._workflow_write = True
    return _save(obj)


def _account(org, account):
    value = getattr(account, "pk", account)
    if value is None:
        return None
    if type(value) is not int:
        raise ValidationError("Pilih akun anggota perusahaan yang sah.")
    membership = (
        Membership.objects.filter(
            organization=org,
            user_id=value,
            user__is_active=True,
            role__in={"owner", "director", "marketing", "finance"},
        )
        .select_related("user")
        .first()
    )
    if membership is None:
        raise ValidationError(
            "Akun harus aktif dan memiliki akses operasional pada perusahaan ini."
        )
    return membership.user


def can_act_for_member(*, organization, actor, member, at=None):
    """No implicit supervisor bypass: caller must own or hold an explicit delegation."""
    if not getattr(actor, "is_authenticated", False) or not actor.is_active:
        return False
    organization_id = getattr(organization, "pk", organization)
    member_id = getattr(member, "pk", member)
    current = TeamMember.objects.filter(
        organization_id=organization_id, pk=member_id, active=True
    ).first()
    if current is None or current.account_id is None:
        return False
    if not Membership.objects.filter(
        organization_id=organization_id,
        user=actor,
        user__is_active=True,
        role__in={"owner", "director", "marketing", "finance"},
    ).exists():
        return False
    if current.account_id == actor.pk:
        return True
    point = at if at is not None else timezone.now()
    if isinstance(point, datetime):
        if timezone.is_naive(point):
            return False
        point = timezone.localdate(point)
    if not isinstance(point, date):
        return False
    return MemberDelegation.objects.filter(
        organization_id=organization_id,
        member=current,
        delegate=actor,
        starts_on__lte=point,
        ends_on__gte=point,
        revoked_at__isnull=True,
    ).exists()


@transaction.atomic
def configure_member(
    *, organization, actor, member, expected_version, account, active, reason
):
    org = _org(organization, actor, {"owner"})
    obj = _scoped(TeamMember, member, org)
    _version(obj, expected_version)
    reason = _reason(reason)
    if type(active) is not bool:
        raise ValidationError("Status anggota tidak valid.")
    user = _account(org, account)
    if (
        user
        and TeamMember.objects.filter(organization=org, account=user)
        .exclude(pk=obj.pk)
        .exists()
    ):
        raise ValidationError(
            "Akun sudah terhubung ke anggota tim lain. Lepaskan hubungan lama dengan alasan dahulu."
        )
    before = {
        "account_id": obj.account_id,
        "active": obj.active,
        "version": obj.version,
    }
    obj.account, obj.active = user, active
    obj.version += 1
    _workflow_save(obj)
    # Re-linking an identity must never revive an old person's delegated access.
    if before["account_id"] != obj.account_id or not active:
        for delegation in obj.delegations.filter(revoked_at__isnull=True):
            revoke_delegation(
                organization=org,
                actor=actor,
                delegation=delegation,
                expected_version=delegation.version,
                reason="Pengaturan akun/PIC berubah: " + reason[:950],
            )
    _audit(
        org,
        actor,
        "member.configured",
        obj,
        before=before,
        after={
            "account_id": obj.account_id,
            "active": obj.active,
            "version": obj.version,
        },
        reason=reason,
    )
    return obj


@transaction.atomic
def assign_lead(*, organization, actor, lead, expected_version, member, reason):
    org = _org(organization, actor, MANAGERS)
    obj = _scoped(Lead, lead, org)
    _version(obj, expected_version)
    assignee = _scoped(TeamMember, member, org)
    reason = _reason(reason)
    if not assignee.active:
        raise ValidationError("Pilih anggota tim aktif untuk penugasan.")
    if obj.current_owner_id == assignee.pk:
        raise ValidationError("Prospek sudah ditugaskan kepada anggota ini.")
    old = obj.current_owner
    obj.current_owner = assignee
    obj.version += 1
    _workflow_save(obj)
    event = _workflow_save(
        LeadAssignment(
            organization=org,
            created_by=actor,
            lead=obj,
            previous_member=old,
            member=assignee,
            reason=reason,
        )
    )
    _audit(
        org,
        actor,
        "lead.assigned",
        obj,
        before={"current_owner_id": getattr(old, "pk", None)},
        after={"current_owner_id": assignee.pk},
        reason=reason,
        version=obj.version,
        effective_at=event.effective_at.isoformat(),
        attribution_owner_id=obj.owner_id,
        attribution_basis="owner_at_lead_creation_unchanged",
    )
    return obj


def _date(value):
    try:
        result = date.fromisoformat(value) if isinstance(value, str) else value
        if (
            not isinstance(result, date)
            or isinstance(result, datetime)
            or not 2000 <= result.year <= 2200
        ):
            raise ValueError
        return result
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            "Tanggal tidak valid; gunakan YYYY-MM-DD pada tahun 2000–2200."
        ) from exc


@transaction.atomic
def grant_delegation(
    *,
    organization,
    actor,
    member,
    expected_version,
    delegate,
    starts_on,
    ends_on,
    reason,
):
    org = _org(organization, actor, {"owner"})
    obj = _scoped(TeamMember, member, org)
    _version(obj, expected_version)
    user = _account(org, delegate)
    reason = _reason(reason)
    start, end = _date(starts_on), _date(ends_on)
    if user is None or not obj.active or obj.account_id is None:
        raise ValidationError(
            "Delegasi memerlukan PIC aktif yang sudah terhubung ke akun dan akun pengganti aktif."
        )
    if user.pk == obj.account_id:
        raise ValidationError("Akun pengganti harus berbeda dari akun PIC.")
    if end < timezone.localdate():
        raise ValidationError(
            "Delegasi baru tidak dapat seluruhnya berada di masa lalu."
        )
    if MemberDelegation.objects.filter(
        organization=org,
        member=obj,
        delegate=user,
        revoked_at__isnull=True,
        starts_on__lte=end,
        ends_on__gte=start,
    ).exists():
        raise ValidationError(
            "Sudah ada delegasi kepada akun ini pada periode yang bertumpang tindih."
        )
    delegation = _workflow_save(
        MemberDelegation(
            organization=org,
            created_by=actor,
            member=obj,
            delegate=user,
            starts_on=start,
            ends_on=end,
            reason=reason,
        )
    )
    obj.version += 1
    _workflow_save(obj)
    _audit(
        org,
        actor,
        "member.delegated",
        delegation,
        member_id=obj.pk,
        delegate_id=user.pk,
        starts_on=str(start),
        ends_on=str(end),
        reason=reason,
        member_version=obj.version,
    )
    return delegation


@transaction.atomic
def revoke_delegation(*, organization, actor, delegation, expected_version, reason):
    org = _org(organization, actor, {"owner"})
    obj = _scoped(MemberDelegation, delegation, org)
    _version(obj, expected_version)
    if obj.revoked_at:
        raise ValidationError("Delegasi sudah dicabut.")
    obj.revoked_at, obj.revocation_reason = timezone.now(), _reason(reason)
    obj.version += 1
    _workflow_save(obj)
    _audit(
        org,
        actor,
        "member.delegation_revoked",
        obj,
        member_id=obj.member_id,
        reason=obj.revocation_reason,
        version=obj.version,
    )
    return obj


def _time(value):
    try:
        result = datetime.fromisoformat(value) if isinstance(value, str) else value
        if (
            not isinstance(result, datetime)
            or timezone.is_naive(result)
            or not 2000 <= result.year <= 2200
        ):
            raise ValueError
        return result
    except (ValueError, TypeError) as exc:
        raise ValidationError(
            "Waktu tidak valid; sertakan tanggal, jam, dan zona waktu."
        ) from exc


def _lead_responsibility(org, actor, lead, reason):
    if lead.current_owner_id and can_act_for_member(
        organization=org, actor=actor, member=lead.current_owner
    ):
        return ""
    if Membership.objects.filter(
        organization=org, user=actor, role__in=MANAGERS
    ).exists():
        return _reason(reason)
    raise PermissionDenied(
        "Kontak dicatat oleh akun PIC atau pengganti dengan delegasi aktif. Hubungi Owner untuk mengatur penugasan."
    )


@transaction.atomic
def record_contact(
    *,
    organization,
    actor,
    lead,
    expected_version,
    occurred_at,
    method,
    outcome,
    summary,
    evidence,
    first_response=False,
    followup_action="keep",
    next_due_at=None,
    loss_reason="",
    completes=None,
    corrects=None,
    correction_reason="",
    responsibility_reason="",
):
    from .services import update_lead

    org = _org(organization, actor)
    obj = _scoped(Lead, lead, org)
    _version(obj, expected_version)
    responsibility = _lead_responsibility(org, actor, obj, responsibility_reason)
    occurred = _time(occurred_at)
    due = _time(next_due_at) if next_due_at is not None else None
    if type(first_response) is not bool:
        raise ValidationError("Penanda respons pertama tidak valid.")
    if followup_action not in {"keep", "set", "clear"} or (
        followup_action == "set"
    ) != (due is not None):
        raise ValidationError(
            "Isi tanggal/jam hanya ketika memilih jadwalkan tindak lanjut."
        )
    correction = None if corrects is None else _scoped(LeadContact, corrects, org)
    completion = None if completes is None else _scoped(LeadContact, completes, org)
    if correction:
        correction_reason = _reason(correction_reason)
        if LeadContact.objects.filter(corrects=correction).exists():
            raise ValidationError(
                "Catatan sudah memiliki koreksi. Pilih versi koreksi terakhir."
            )
        if first_response or followup_action != "keep" or completes is not None:
            raise ValidationError(
                "Koreksi kontak mempertahankan jadwal dan respons saat ini. Gunakan alur respons atau kontak baru untuk mengubahnya."
            )
    if completion and (
        not completion.next_due_at
        or completion.lead_id != obj.pk
        or LeadContact.objects.filter(completes=completion).exists()
    ):
        raise ValidationError("Pilih janji prospek ini yang belum ditandai dipenuhi.")
    if first_response and obj.first_response_at:
        raise ValidationError(
            "Respons pertama sudah dicatat. Gunakan koreksi dengan alasan bila waktunya salah."
        )
    event = _workflow_save(
        LeadContact(
            organization=org,
            created_by=actor,
            lead=obj,
            occurred_at=occurred,
            method=method,
            outcome=outcome,
            summary=_reason(summary, 4000),
            evidence=_reason(evidence, 4000),
            next_due_at=due,
            followup_action=followup_action,
            loss_reason=loss_reason,
            corrects=correction,
            correction_reason=correction_reason,
            completes=completion,
            responsibility_reason=responsibility,
        )
    )
    changes = {}
    if not correction and outcome in {"qualified", "proposal", "won", "lost"}:
        changes["stage"] = outcome
    elif not correction and outcome == "responded" and obj.stage == "new":
        changes["stage"] = "contacted"
    if first_response:
        changes["first_response_at"] = occurred
    if followup_action != "keep":
        changes["next_follow_up"] = timezone.localdate(due) if due else None
    obj = update_lead(
        organization=org,
        actor=actor,
        lead=obj,
        expected_version=obj.version,
        responsibility_reason=responsibility,
        **changes,
    )
    if followup_action != "keep":
        obj.next_follow_up_at = due
        _workflow_save(obj)
    _audit(
        org,
        actor,
        "lead.contact_recorded" if not correction else "lead.contact_corrected",
        event,
        lead_id=obj.pk,
        lead_version=obj.version,
        current_owner_id=obj.current_owner_id,
        occurred_at=occurred.isoformat(),
        outcome=outcome,
        next_due_at=due.isoformat() if due else None,
        corrects_id=getattr(correction, "pk", None),
        correction_reason=correction_reason,
        responsibility_reason=responsibility,
    )
    return event


@transaction.atomic
def correct_first_response(
    *,
    organization,
    actor,
    lead,
    expected_version,
    first_response_at,
    reason,
    evidence,
    responsibility_reason="",
):
    from .services import update_lead

    org = _org(organization, actor)
    obj = _scoped(Lead, lead, org)
    _version(obj, expected_version)
    _lead_responsibility(org, actor, obj, responsibility_reason)
    if obj.first_response_at is None:
        raise ValidationError(
            "Belum ada waktu respons untuk dikoreksi. Gunakan Catat kontak untuk mencatat respons pertama beserta bukti."
        )
    return update_lead(
        organization=org,
        actor=actor,
        lead=obj,
        expected_version=obj.version,
        first_response_at=first_response_at,
        correction_reason=_reason(reason),
        correction_evidence=_reason(evidence, 4000),
        responsibility_reason=responsibility_reason,
    )


@transaction.atomic
def transition_campaign(
    *,
    organization,
    actor,
    campaign,
    expected_version,
    operation,
    effective_on,
    reason,
    member=None,
):
    org = _org(organization, actor, MANAGERS)
    obj = _scoped(Campaign, campaign, org)
    _version(obj, expected_version)
    reason, effective = _reason(reason), _date(effective_on)
    transitions = {
        "pause": ({"active"}, "paused"),
        "resume": ({"paused"}, "active"),
        "complete": ({"active", "paused"}, "completed"),
        "handover": ({"active", "paused"}, obj.status),
    }
    if operation not in transitions or obj.status not in transitions[operation][0]:
        raise ValidationError(
            "Tindakan tidak sesuai status kampanye. Kampanye selesai tetap menjadi arsip; buat kampanye baru untuk melanjutkan."
        )
    last = obj.lifecycle_events.first()
    if (
        effective > timezone.localdate()
        or obj.start_date
        and effective < obj.start_date
        or last
        and effective < last.effective_on
    ):
        raise ValidationError(
            "Tanggal efektif tidak boleh di masa depan, sebelum awal kampanye, atau sebelum keputusan terakhir."
        )
    new_member = obj.current_owner
    if operation == "handover":
        new_member = _scoped(TeamMember, member, org)
        if not new_member.active or new_member.pk == obj.current_owner_id:
            raise ValidationError(
                "Pilih anggota aktif yang berbeda dari pengelola saat ini."
            )
    elif member is not None:
        raise ValidationError(
            "Pengelola baru hanya diisi untuk tindakan alih pengelola."
        )
    if (
        operation == "complete"
        and active_ad_observations(organization=org)
        .filter(campaign=obj, date__gt=effective)
        .exists()
    ):
        raise ValidationError(
            "Masih ada catatan biaya setelah tanggal akhir yang dipilih. Periksa sumber atau gunakan akhir aktual yang sesuai."
        )
    previous_status, previous_member = obj.status, obj.current_owner
    obj.status, obj.current_owner = transitions[operation][1], new_member
    if operation == "complete":
        obj.actual_end_date = effective
    obj.version += 1
    _workflow_save(obj)
    event = _workflow_save(
        CampaignEvent(
            organization=org,
            created_by=actor,
            campaign=obj,
            operation=operation,
            effective_on=effective,
            reason=reason,
            previous_status=previous_status,
            resulting_status=obj.status,
            previous_member=previous_member,
            member=new_member,
        )
    )
    _audit(
        org,
        actor,
        "campaign." + operation,
        obj,
        version=obj.version,
        event_id=event.pk,
        reason=reason,
        before={
            "status": previous_status,
            "current_owner_id": getattr(previous_member, "pk", None),
        },
        after={
            "status": obj.status,
            "current_owner_id": obj.current_owner_id,
            "actual_end_date": str(obj.actual_end_date)
            if obj.actual_end_date
            else None,
        },
        effective_on=str(effective),
    )
    return obj


DAILY_QUEUES = [
    ("today", "Janji hari ini"),
    ("overdue", "Janji yang terlambat"),
    ("unanswered", "Belum dihubungi"),
    ("unassigned", "Belum dibagikan ke staf"),
    ("work", "Tugas yang sedang dikerjakan"),
    ("revision", "Hasil yang perlu diperbaiki"),
    ("review", "Menunggu pemeriksaan"),
    ("response_sla", "Terlambat memberi respons"),
    ("review_sla", "Terlambat diperiksa"),
]


def current_work_policy(organization):
    return (
        WorkPolicy.objects.filter(
            organization_id=getattr(organization, "pk", organization)
        )
        .order_by("-version")
        .first()
    )


@transaction.atomic
def configure_work_policy(
    *,
    organization,
    actor,
    expected_version,
    response_limit_minutes,
    review_limit_minutes,
    workdays,
    work_start,
    work_end,
    holidays,
    reason,
):
    org = _org(organization, actor, {"owner"})
    current = current_work_policy(org)
    if type(expected_version) is not int or expected_version != (
        current.version if current else 0
    ):
        raise ValidationError(
            "Kebijakan sudah berubah. Muat ulang sebelum mengesahkan aturan baru."
        )
    if any(
        type(value) is not int or not 1 <= value <= 10080
        for value in (response_limit_minutes, review_limit_minutes)
    ):
        raise ValidationError("Batas respons/pemeriksaan wajib 1–10.080 menit kerja.")
    if (
        not isinstance(workdays, list)
        or not workdays
        or any(type(day) is not int or day not in range(7) for day in workdays)
        or len(set(workdays)) != len(workdays)
    ):
        raise ValidationError("Pilih hari kerja yang valid, tanpa pengulangan.")
    try:
        start = (
            time.fromisoformat(work_start)
            if isinstance(work_start, str)
            else work_start
        )
        end = time.fromisoformat(work_end) if isinstance(work_end, str) else work_end
        if (
            not isinstance(start, time)
            or not isinstance(end, time)
            or start.tzinfo
            or end.tzinfo
            or start >= end
        ):
            raise ValueError
    except (ValueError, TypeError) as exc:
        raise ValidationError(
            "Gunakan rentang jam kerja WIB yang berurutan pada hari yang sama."
        ) from exc
    if not isinstance(holidays, list) or len(holidays) > 366:
        raise ValidationError("Daftar hari libur maksimal 366 tanggal.")
    holiday_dates = sorted({_date(value).isoformat() for value in holidays})
    obj = _workflow_save(
        WorkPolicy(
            organization=org,
            created_by=actor,
            version=expected_version + 1,
            response_limit_minutes=response_limit_minutes,
            review_limit_minutes=review_limit_minutes,
            workdays=sorted(workdays),
            work_start=start,
            work_end=end,
            holidays=holiday_dates,
            reason=_reason(reason),
        )
    )
    _audit(
        org,
        actor,
        "work_policy.approved",
        obj,
        version=obj.version,
        response_limit_minutes=response_limit_minutes,
        review_limit_minutes=review_limit_minutes,
        workdays=obj.workdays,
        work_start=start.isoformat(),
        work_end=end.isoformat(),
        holidays=holiday_dates,
        reason=obj.reason,
        timezone="Asia/Jakarta",
    )
    return obj


def _business_minutes(policy, start, end, stop_after):
    zone = ZoneInfo("Asia/Jakarta")
    start, end = timezone.localtime(start, zone), timezone.localtime(end, zone)
    cursor, elapsed = start.date(), Decimal(0)
    holidays = set(policy.holidays)
    while cursor <= end.date():
        if cursor.weekday() in policy.workdays and cursor.isoformat() not in holidays:
            begin = max(start, datetime.combine(cursor, policy.work_start, zone))
            finish = min(end, datetime.combine(cursor, policy.work_end, zone))
            if finish > begin:
                duration = finish - begin
                elapsed += Decimal(
                    duration.days * 86400000000
                    + duration.seconds * 1000000
                    + duration.microseconds
                ) / Decimal(60000000)
                if elapsed > stop_after:
                    return elapsed
        cursor += timedelta(days=1)
    return elapsed


@transaction.atomic
def daily_work(*, organization, actor, scope="mine", queue="today"):
    """One live row per outstanding object; queues never send or duplicate messages."""
    from .activity_models import WorkActivity

    org = _org(
        organization, actor, {"owner", "director", "finance", "marketing", "auditor"}
    )
    if scope not in {"mine", "all"} or queue not in dict(DAILY_QUEUES):
        raise ValidationError("Pilihan antrean harian tidak valid.")
    now, today = timezone.now(), timezone.localdate()
    delegated_ids = MemberDelegation.objects.filter(
        organization=org,
        delegate=actor,
        revoked_at__isnull=True,
        starts_on__lte=today,
        ends_on__gte=today,
    ).values("member_id")
    members = TeamMember.objects.filter(
        organization=org, active=True, account__isnull=False
    ).filter(Q(account=actor) | Q(pk__in=delegated_ids))
    mapped_ids = list(members.values_list("pk", flat=True))
    leads = (
        Lead.objects.filter(organization=org)
        .exclude(stage="lost")
        .select_related("current_owner", "campaign")
    )
    work = WorkActivity.objects.filter(organization=org).select_related(
        "owner", "current_owner"
    )
    if scope == "mine":
        leads = leads.filter(current_owner_id__in=mapped_ids)
        work = work.filter(
            Q(current_owner_id__in=mapped_ids)
            | Q(current_owner__isnull=True, owner_id__in=mapped_ids)
        )
    queries = {
        "today": leads.filter(
            Q(next_follow_up_at__date=today)
            | Q(next_follow_up_at__isnull=True, next_follow_up=today)
        ),
        "overdue": leads.filter(
            Q(next_follow_up_at__lt=now)
            | Q(next_follow_up_at__isnull=True, next_follow_up__lt=today)
        ),
        "unanswered": leads.filter(first_response_at__isnull=True),
        # Unassigned leads belong to the company triage queue, never secretly to a user.
        "unassigned": Lead.objects.filter(organization=org, current_owner__isnull=True)
        .exclude(stage="lost")
        .select_related("current_owner", "campaign")
        if scope == "all"
        else leads.none(),
        "work": work.filter(status__in={"planned", "in_progress"}),
        "revision": work.filter(status="needs_revision"),
        "review": work.filter(status="submitted"),
    }
    policy = current_work_policy(org)
    late_responses, late_reviews = [], []
    if policy:
        late_responses = [
            pk
            for pk, received in queries["unanswered"].values_list("pk", "received_at")
            if _business_minutes(policy, received, now, policy.response_limit_minutes)
            > policy.response_limit_minutes
        ]
        late_reviews = [
            pk
            for pk, submitted in queries["review"].values_list("pk", "submitted_at")
            if submitted
            and _business_minutes(policy, submitted, now, policy.review_limit_minutes)
            > policy.review_limit_minutes
        ]
    queries["response_sla"] = leads.filter(pk__in=late_responses)
    queries["review_sla"] = work.filter(pk__in=late_reviews)
    rows = queries[queue]
    kind = (
        "lead"
        if queue in {"today", "overdue", "unanswered", "unassigned", "response_sla"}
        else "work"
    )
    rows = (
        rows.order_by("next_follow_up", "received_at", "pk")
        if kind == "lead"
        else rows.order_by("submitted_at" if queue == "review" else "due_date", "pk")
    )
    return {
        "rows": rows,
        "counts": [
            {"key": key, "label": label, "count": queries[key].count()}
            for key, label in DAILY_QUEUES
        ],
        "scope": scope,
        "queue": queue,
        "queue_label": dict(DAILY_QUEUES)[queue],
        "row_kind": kind,
        "mapping_needed": scope == "mine" and not mapped_ids,
        "today": today,
        "member_names": list(members.values_list("name", flat=True)),
        "policy": policy,
    }


def daily_work_summary(*, organization, actor):
    """PII-free current-work facts for management; no historical KPI claims."""
    result = daily_work(organization=organization, actor=actor, scope="all")
    policy = result["policy"]
    return {
        "policy_id": policy.pk if policy else None,
        "policy_version": policy.version if policy else None,
        "response_limit_minutes": policy.response_limit_minutes if policy else None,
        "review_limit_minutes": policy.review_limit_minutes if policy else None,
        "time_basis": "approved_business_minutes_Asia/Jakarta"
        if policy
        else "not_configured",
        "counts": {item["key"]: item["count"] for item in result["counts"]},
    }
