"""Coverage declared by a user with source evidence, bound to reviewed observations."""
import hashlib
import json
from datetime import timedelta
from decimal import Decimal

from .models import AdDailyObservation, SourceCoverage


def observation_state(*, organization, campaign, start, end):
    rows = list(AdDailyObservation.objects.filter(organization=organization, campaign=campaign,
        date__range=(start, end)).order_by("pk").values("pk", "date", "spend", "impressions", "clicks",
            "ad_name", "audience", "region", "source", "reference", "version"))
    total = None if any(row["spend"] is None for row in rows) else sum((row["spend"] for row in rows), Decimal("0"))
    fingerprint = hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()
    return fingerprint, total


def declaration_current(declaration):
    if declaration.withdrawn_at:
        return False
    fingerprint, total = observation_state(organization=declaration.organization_id,
        campaign=declaration.campaign_id, start=declaration.period_start, end=declaration.period_end)
    return fingerprint == declaration.observation_fingerprint and total == declaration.declared_spend


def confirmed_campaigns(*, organization, campaigns, start, end):
    """Return coverage of each requested campaign interval; no missing dates become zero rows."""
    intervals = {campaign.pk: [] for campaign in campaigns}
    if end < start:
        return {key: False for key in intervals}
    declarations = SourceCoverage.objects.filter(organization=organization, campaign_id__in=intervals,
        withdrawn_at__isnull=True, period_start__lte=end, period_end__gte=start)
    for declaration in declarations:
        if declaration_current(declaration):
            intervals[declaration.campaign_id].append((declaration.period_start, declaration.period_end))
    result = {}
    for campaign in campaigns:
        # Outside a known active campaign interval there is still no automatic proof of zero spend.
        required_start = max(start, campaign.start_date or start)
        required_end = min(end, campaign.end_date or end)
        if required_start > required_end:
            required_start, required_end = start, end
        cursor = required_start
        for lower, upper in sorted(intervals[campaign.pk]):
            if lower > cursor:
                break
            if upper >= cursor:
                cursor = upper + timedelta(days=1)
        result[campaign.pk] = cursor > required_end
    return result
