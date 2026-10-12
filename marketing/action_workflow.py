"""Keep legacy recommendations as origins; execute through reviewed WorkActivity."""

from django.core.exceptions import ValidationError
from django.db import transaction

from .activities import _save_activity
from .activity_models import WorkActivity
from .models import MarketingAction
from .services import _audit, _fields, _org, _save, _scoped, _version


@transaction.atomic
def create_action_work(*, organization, actor, channel, kind, **fields):
    from .services import create_action

    action = create_action(organization=organization, actor=actor, **fields)
    return adopt_action(
        organization=organization,
        actor=actor,
        action=action,
        expected_version=action.version,
        channel=channel,
        kind=kind,
    )


@transaction.atomic
def adopt_action(*, organization, actor, action, expected_version, channel, kind):
    org = _org(organization, actor)
    action = _scoped(MarketingAction, action, org)
    _version(action, expected_version)
    existing = WorkActivity.objects.filter(
        organization=org, legacy_action=action
    ).first()
    if existing:
        return current_work(existing)
    if not action.owner_id or not action.owner.active or not action.due_date:
        raise ValidationError(
            "Tindakan memerlukan PIC aktif dan tenggat sebelum menjadi pekerjaan."
        )
    data = _fields({"channel": channel, "kind": kind}, {"channel", "kind"}, org)
    work = _save_activity(
        WorkActivity(
            organization=org,
            created_by=actor,
            title=action.title,
            owner=action.owner,
            due_date=action.due_date,
            description=action.description,
            legacy_action=action,
            **data,
        )
    )
    _audit(
        org,
        actor,
        "action.linked_to_work",
        action,
        work_id=work.pk,
        original_status=action.status,
        basis="Status tindakan lama dipertahankan sebagai riwayat; bukti dan penerimaan mengikuti pekerjaan terkait.",
    )
    return work


def current_work(work):
    while True:
        replacement = WorkActivity.objects.filter(
            organization=work.organization, replaces=work
        ).first()
        if replacement is None:
            return work
        work = replacement


def close_linked_action(*, organization, actor, work):
    """Only called inside the already-authorized work acceptance transaction."""
    origin = work
    while origin.replaces_id:
        origin = origin.replaces
    if not origin.legacy_action_id:
        return
    action = _scoped(MarketingAction, origin.legacy_action, organization)
    before = {"status": action.status, "version": action.version}
    action.status, action.outcome = "done", work.outcome
    action.version += 1
    _save(action)
    _audit(
        organization,
        actor,
        "action.completed_from_review",
        action,
        before=before,
        work_id=work.pk,
        after={"status": action.status, "version": action.version},
        reviewer_id=actor.pk,
    )
