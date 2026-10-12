"""Current ad facts and their local recovery provenance share one scoped reader."""

import hashlib
import json

from django.core.exceptions import ValidationError
from django.db.models import Q

from .models import AdDailyObservation

AD_SOURCE_FIELDS = (
    "pk",
    "version",
    "campaign_id",
    "date",
    "ad_name",
    "audience",
    "region",
    "spend",
    "impressions",
    "clicks",
    "source",
    "reference",
)


def _organization_id(organization):
    value = getattr(organization, "pk", organization)
    if type(value) is not int or value < 1:
        raise ValidationError("Pilih perusahaan tersimpan untuk membaca sumber iklan.")
    return value


def active_ad_observations(*, organization):
    organization_id = _organization_id(organization)
    return AdDailyObservation.objects.filter(
        organization_id=organization_id,
        campaign__organization_id=organization_id,
        withdrawn_at__isnull=True,
    )


def observation_fingerprint(observation):
    values = {field: getattr(observation, field) for field in AD_SOURCE_FIELDS}
    values.update(
        organization_id=observation.organization_id,
        created_by_id=observation.created_by_id,
        source_root_id=observation.source_root_id,
        replaces_id=observation.replaces_id,
        withdrawn_at=observation.withdrawn_at,
    )
    return _digest(values)


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _scope(prefix, start, end, filters):
    lookup = {
        "campaign": "campaign_id",
        "channel": "campaign__channel",
        "product": "campaign__product_id",
        "ad": "ad_name",
    }
    return Q(
        **{
            prefix + "date__range": (start, end),
            **{
                prefix + lookup[key]: value
                for key, value in (filters or {}).items()
                if key in lookup
            },
        }
    )


def recovery_manifest(*, organization, start, end, filters=None):
    """Both affected scopes retain a tombstone even if the active set returns empty."""
    from .models import AdObservationRecovery

    organization_id = _organization_id(organization)
    rows = AdObservationRecovery.objects.filter(
        organization_id=organization_id,
        predecessor__organization_id=organization_id,
        predecessor__campaign__organization_id=organization_id,
    ).filter(
        _scope("predecessor__", start, end, filters)
        | (
            _scope("successor__", start, end, filters)
            & Q(
                successor__organization_id=organization_id,
                successor__campaign__organization_id=organization_id,
            )
        )
    )
    return list(
        rows.order_by("pk").values(
            "pk", "action", "predecessor_id", "successor_id", "created_at"
        )
    )


def ad_source_state(*, organization, start, end, filters=None):
    rows = list(
        active_ad_observations(organization=organization)
        .filter(_scope("", start, end, filters))
        .order_by("pk")
        .values(*AD_SOURCE_FIELDS)
    )
    recoveries = recovery_manifest(
        organization=organization, start=start, end=end, filters=filters
    )
    return {
        "count": len(rows),
        "recovery_count": len(recoveries),
        "fingerprint": _digest({"active": rows, "recoveries": recoveries}),
    }
