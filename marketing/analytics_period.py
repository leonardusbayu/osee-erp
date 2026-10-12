from datetime import datetime, time, timedelta

from django.db.models import Q
from django.utils import timezone

from .analytics_metrics import (
    JAKARTA,
    _bucket,
    _campaign_matches_group,
    _metrics,
    _number,
    _percent,
)
from .analytics_period_ads import _add_ad_facts
from .analytics_period_data import (
    _add_followup_metrics,
    _add_invoice_refund_facts,
    _add_lead_work_metrics,
    _add_receipt_facts,
    _lead_facts,
    _payment_facts,
)
from .analytics_period_types import (
    CampaignSources,
    LeadFacts,
    PaymentFacts,
    PeriodBuckets,
    PeriodResults,
    PeriodScope,
)
from .coverage import confirmed_campaigns
from .models import Campaign


def _period_scope(organization, start, end, group_by, filters):
    today = timezone.localdate(timezone=JAKARTA)
    observation_end = min(end, today)
    return PeriodScope(
        organization=organization,
        start=start,
        end=end,
        observation_end=observation_end,
        beginning=datetime.combine(start, time.min, JAKARTA),
        cutoff=datetime.combine(observation_end + timedelta(days=1), time.min, JAKARTA),
        today=today,
        group_by=group_by,
        filters=filters,
        cash_available=group_by != "ad" and "ad" not in filters,
        spend_available=group_by in {"channel", "campaign", "product", "ad"}
        and not ({"member", "segment", "region"} & filters.keys()),
    )


def _collect_bucket_facts(scope: PeriodScope, leads: LeadFacts, payments: PaymentFacts):
    buckets = PeriodBuckets(summary=_bucket("Total"))
    _add_followup_metrics(scope, leads, buckets)
    _add_lead_work_metrics(scope, leads, payments.collectible, buckets)
    _add_receipt_facts(scope, leads, buckets)
    _add_invoice_refund_facts(scope, leads, buckets)
    _add_ad_facts(scope, buckets)
    return buckets


def _period_metric_rows(
    scope: PeriodScope, buckets: PeriodBuckets, payments: PaymentFacts
):
    summary = _metrics(
        buckets.summary,
        payments.paid_by_invoice,
        scope.cutoff,
        spend_available=scope.spend_available,
        cash_available=scope.cash_available,
        applied_by_invoice=payments.applied_by_invoice,
    )
    rows = [
        {
            "key": key,
            "label": bucket["label"],
            **_metrics(
                bucket,
                payments.paid_by_invoice,
                scope.cutoff,
                spend_available=scope.spend_available,
                cash_available=scope.cash_available,
                applied_by_invoice=payments.applied_by_invoice,
            ),
        }
        for key, bucket in sorted(
            buckets.rows.items(), key=lambda item: item[1]["label"].casefold()
        )
    ]
    coverage = {
        "total_customer_receipts": _number(buckets.total_cash),
        "attributed_receipts": _number(buckets.attributed_cash),
        "unattributed_receipts": _number(buckets.total_cash - buckets.attributed_cash),
        "attribution_percent": _percent(buckets.attributed_cash, buckets.total_cash),
        "total_invoice_refunds": _number(buckets.total_invoice_refunds),
        "attributed_invoice_refunds": _number(buckets.attributed_invoice_refunds),
        "unattributed_invoice_refunds": _number(
            buckets.total_invoice_refunds - buckets.attributed_invoice_refunds
        ),
        "spend_rows": buckets.summary["ad_rows"],
        "spend_known_rows": len(buckets.summary["spend_values"]),
        "scope": "Cakupan penerimaan dan refund seluruh organisasi pada tanggal mutasi; angka tabel mengikuti filter dan refund tanpa lead tidak dibebankan ke kelompok.",
    }
    return PeriodResults(summary=summary, rows=rows, coverage=coverage)


def _source_campaigns(scope: PeriodScope, buckets: PeriodBuckets):
    candidates = Campaign.objects.filter(organization=scope.organization).filter(
        (Q(start_date__isnull=True) | Q(start_date__lte=scope.observation_end))
        & (Q(end_date__isnull=True) | Q(end_date__gte=scope.start))
        & (Q(actual_end_date__isnull=True) | Q(actual_end_date__gte=scope.start))
        | Q(
            pk__in=[
                key for key in buckets.summary["cash_campaign_ids"] if key is not None
            ]
        )
    )
    filter_fields = {"campaign": "pk", "channel": "channel", "product": "product_id"}
    for key, value in scope.filters.items():
        field = filter_fields.get(key)
        if field:
            candidates = candidates.filter(**{field: value})
    campaigns = list(candidates)
    confirmed = confirmed_campaigns(
        organization=scope.organization,
        campaigns=campaigns,
        start=scope.start,
        end=scope.observation_end,
    )
    return CampaignSources(campaigns=campaigns, confirmed=confirmed)


def _attach_coverage(metrics, bucket, required, sources, spend_available):
    campaign_ids = set(required) | bucket["cash_campaign_ids"]
    complete = (
        spend_available
        and bool(campaign_ids)
        and all(sources.confirmed.get(key, False) for key in campaign_ids)
    )
    metrics["source_period_complete"] = complete
    metrics["source_coverage_basis"] = (
        "user_confirmed_source_evidence" if complete else "unconfirmed"
    )
    metrics["source_coverage_note"] = (
        "Cakupan sumber kampanye dalam lingkup ini dikonfirmasi pengguna; bukti dan total cocok dengan catatan yang diperiksa. Bukan verifikasi API atau bukti seluruh kegiatan perusahaan."
        if complete
        else "Cakupan sumber periode belum dikonfirmasi untuk seluruh kampanye dalam lingkup ini, atau catatan berubah setelah pemeriksaan. Rasio hanya memakai biaya tercatat."
    )


def _attach_result_coverage(
    scope: PeriodScope,
    buckets: PeriodBuckets,
    results: PeriodResults,
    sources: CampaignSources,
):
    _attach_coverage(
        results.summary,
        buckets.summary,
        [campaign.pk for campaign in sources.campaigns],
        sources,
        scope.spend_available,
    )
    for item in results.rows:
        matched = [
            campaign.pk
            for campaign in sources.campaigns
            if _campaign_matches_group(campaign, scope.group_by, item["key"])
        ]
        _attach_coverage(
            item,
            buckets.rows[item["key"]],
            matched,
            sources,
            scope.spend_available,
        )


def _apply_period_limits(scope: PeriodScope, results: PeriodResults):
    if not scope.start <= scope.today <= scope.end:
        for item in [results.summary, *results.rows]:
            for key in ("overdue_followups", "unpaid_won_leads", "unpaid_won_amount"):
                item[key] = None
    if scope.start > scope.today:
        for key in (
            "cash_in",
            "actual_refunds",
            "net_cash_in",
            "leads",
            "cohort_paid_leads",
            "cohort_fully_paid_leads",
            "cohort_fully_paid_conversion",
            "cohort_cash_in",
            "cohort_applied_payments",
            "followed_up_leads",
            "followups_due",
            "followups_on_time",
            "followup_on_time_percent",
        ):
            results.summary[key] = None


def _period(organization, start, end, group_by, filters):
    scope = _period_scope(organization, start, end, group_by, filters)
    leads = _lead_facts(scope)
    payments = _payment_facts(scope, leads)
    buckets = _collect_bucket_facts(scope, leads, payments)
    results = _period_metric_rows(scope, buckets, payments)
    sources = _source_campaigns(scope, buckets)
    _attach_result_coverage(scope, buckets, results, sources)
    _apply_period_limits(scope, results)
    return (
        results.summary,
        results.rows,
        results.coverage,
        leads.attribution_fingerprint,
    )
