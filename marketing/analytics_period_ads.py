from marketing.ad_sources import active_ad_observations

from .analytics_metrics import _ad_group, _bucket
from .analytics_period_types import PeriodBuckets, PeriodScope


def _add_ad_facts(scope: PeriodScope, buckets: PeriodBuckets):
    ads = (
        active_ad_observations(organization=scope.organization)
        .filter(date__range=(scope.start, scope.observation_end))
        .select_related("campaign__product")
    )
    filter_lookups = {
        "campaign": "campaign_id",
        "channel": "campaign__channel",
        "product": "campaign__product_id",
        "ad": "ad_name",
    }
    for key, value in scope.filters.items():
        lookup = filter_lookups.get(key)
        if lookup:
            ads = ads.filter(**{lookup: value})
    if not scope.spend_available:
        return
    for observation in ads:
        key, label = _ad_group(observation, scope.group_by)
        item = buckets.rows.setdefault(key, _bucket(label))
        if scope.group_by == "product" and not observation.campaign.product_id:
            item["product_mapping_complete"] = False
        for bucket in (buckets.summary, item):
            bucket["ad_rows"] += 1
            if observation.spend is not None:
                bucket["spend_values"].append(observation.spend)
                bucket["spend_campaign_ids"].add(observation.campaign_id)
            if observation.impressions is not None:
                bucket["impressions_values"].append(observation.impressions)
            if observation.clicks is not None:
                bucket["clicks_values"].append(observation.clicks)
