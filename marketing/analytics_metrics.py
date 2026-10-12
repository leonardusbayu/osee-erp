"""Shared calculations for audited marketing performance snapshots."""

from decimal import ROUND_HALF_UP, Decimal
from statistics import median
from zoneinfo import ZoneInfo

from director.models import CHANNEL_CHOICES

JAKARTA = ZoneInfo("Asia/Jakarta")
ZERO = Decimal(0)
GROUP_BY_CHOICES = (
    ("channel", "Kanal"),
    ("campaign", "Kampanye"),
    ("member", "Penanggung jawab lead"),
    ("product", "Produk"),
    ("segment", "Segmen"),
    ("region", "Wilayah lead"),
    ("ad", "Iklan"),
)
DIMENSIONS = GROUP_BY_CHOICES
FILTER_KEYS = {"channel", "campaign", "member", "product", "segment", "region", "ad"}
SEGMENT_LABELS = {
    "direct": "Langsung",
    "reseller": "Reseller",
    "institution": "Institusi",
}
CHANNEL_LABELS = dict(CHANNEL_CHOICES)
SPEND_NOTE = "Belanja iklan tidak dapat dipetakan ke penanggung jawab lead, segmen, atau wilayah lead dengan data yang tersedia."
CASH_NOTE = (
    "Lead belum memiliki tautan ke iklan individual; kas per iklan belum tersedia."
)
PRODUCT_SPEND_NOTE = "Belanja masih subtotal yang sudah dipetakan ke produk. Sebagian penerimaan berasal dari kampanye dengan produk yang belum diketahui atau berbeda; rasio kas/belanja produk belum tersedia."
COST_COVERAGE_NOTE = "Belanja masih subtotal yang diketahui. Ada penerimaan dari kampanye tanpa bukti belanja periode ini atau tanpa pengaitan kampanye. Lengkapi sumber biaya, termasuk nol yang terverifikasi, sebelum menghitung rasio kas/belanja."


def _number(value):
    return (
        format(Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")
        if value is not None
        else None
    )


def _percent(numerator, denominator):
    return (
        _number(Decimal(numerator) * 100 / Decimal(denominator))
        if denominator
        else None
    )


def _lead_value(lead, key):
    if key == "campaign":
        return lead.campaign_id
    if key == "member":
        return lead.owner_id
    if key == "product":
        if hasattr(lead, "_attributed_product"):
            return lead._attributed_product.pk if lead._attributed_product else None
        return lead.product_id or (lead.invoice.product_id if lead.invoice_id else None)
    return getattr(lead, key, "")


_LEAD_GROUP_LABELERS = {
    "campaign": lambda lead, value: (
        lead.campaign.name if lead.campaign_id else "Tanpa kampanye"
    ),
    "member": lambda lead, value: (
        lead.owner.name if lead.owner_id else "Belum ditugaskan"
    ),
    "segment": lambda lead, value: SEGMENT_LABELS.get(value, value),
    "channel": lambda lead, value: CHANNEL_LABELS.get(
        value, value or "Belum diketahui"
    ),
}


def _lead_group(lead, group_by):
    value = _lead_value(lead, group_by)
    if group_by == "product":
        if getattr(lead, "_multiple_products", False):
            return "multiple", "Pesanan multiproduk"
        product = getattr(
            lead,
            "_attributed_product",
            lead.product or (lead.invoice.product if lead.invoice_id else None),
        )
        label = product.name if product else "Produk belum diketahui"
    else:
        labeler = _LEAD_GROUP_LABELERS.get(group_by)
        label = labeler(lead, value) if labeler else value or "Belum diketahui"
    return str(value or "unknown"), label


def _campaign_matches_group(campaign, group_by, key):
    return {
        "campaign": str(campaign.pk) == key,
        "product": str(campaign.product_id or "unknown") == key,
        "ad": key.startswith(f"{campaign.pk}:"),
        "channel": campaign.channel == key,
    }.get(group_by, False)


def _ad_group(observation, group_by):
    campaign = observation.campaign
    if group_by == "campaign":
        return str(campaign.pk), campaign.name
    if group_by == "product":
        return str(
            campaign.product_id or "unknown"
        ), campaign.product.name if campaign.product_id else "Produk belum diketahui"
    if group_by == "ad":
        return (
            f"{campaign.pk}:{observation.ad_name}",
            f"{campaign.name} · {observation.ad_name or 'Iklan tanpa nama'}",
        )
    return campaign.channel or "unknown", CHANNEL_LABELS.get(
        campaign.channel, campaign.channel or "Belum diketahui"
    )


def _bucket(label):
    return {
        "label": label,
        "cash": ZERO,
        "refunds": ZERO,
        "cohort": [],
        "spend_values": [],
        "impressions_values": [],
        "clicks_values": [],
        "ad_rows": 0,
        "overdue": 0,
        "unpaid_won": 0,
        "unpaid_won_amount": ZERO,
        "followups_due": 0,
        "followups_on_time": 0,
        "contexts": set(),
        "cash_campaign_ids": set(),
        "spend_campaign_ids": set(),
        "product_mapping_complete": True,
    }


def _invoice_ids(lead):
    return getattr(
        lead,
        "_attributed_invoice_ids",
        [lead.invoice_id] if lead.invoice_id else [],
    )


def _lead_performance(
    bucket,
    paid_by_invoice,
    cutoff,
    *,
    cash_available,
    applied_by_invoice=None,
):
    leads = bucket["cohort"]
    applied = paid_by_invoice if applied_by_invoice is None else applied_by_invoice
    paid = [
        lead
        for lead in leads
        if any(applied.get(pk, ZERO) > 0 for pk in _invoice_ids(lead))
    ]
    fully_paid = [lead for lead in leads if getattr(lead, "_cohort_fully_paid", False)]
    responses = [
        Decimal(str((lead.first_response_at - lead.received_at).total_seconds())) / 60
        for lead in leads
        if lead.first_response_at
        and lead.received_at <= lead.first_response_at < cutoff
    ]
    return {
        "leads": len(leads) if cash_available else None,
        "cohort_paid_leads": len(paid) if cash_available else None,
        "cohort_paid_conversion": _percent(len(paid), len(leads))
        if cash_available
        else None,
        "cohort_fully_paid_leads": len(fully_paid) if cash_available else None,
        "cohort_fully_paid_conversion": _percent(len(fully_paid), len(leads))
        if cash_available
        else None,
        "cohort_cash_in": _number(
            sum(
                (
                    paid_by_invoice.get(pk, ZERO)
                    for lead in paid
                    for pk in _invoice_ids(lead)
                ),
                ZERO,
            )
        )
        if cash_available
        else None,
        "cohort_applied_payments": _number(
            sum(
                (applied.get(pk, ZERO) for lead in paid for pk in _invoice_ids(lead)),
                ZERO,
            )
        )
        if cash_available
        else None,
        "median_response_minutes": _number(median(responses)) if responses else None,
        "followed_up_leads": len(responses) if cash_available else None,
        "response_coverage_percent": _percent(len(responses), len(leads))
        if cash_available
        else None,
        "overdue_followups": bucket["overdue"] if cash_available else None,
        "followups_due": bucket["followups_due"] if cash_available else None,
        "followups_on_time": bucket["followups_on_time"] if cash_available else None,
        "followup_on_time_percent": _percent(
            bucket["followups_on_time"], bucket["followups_due"]
        )
        if cash_available
        else None,
        "unpaid_won_leads": bucket["unpaid_won"] if cash_available else None,
        "unpaid_won_amount": _number(bucket["unpaid_won_amount"])
        if cash_available
        else None,
    }


def _ad_delivery_metrics(bucket, *, spend_available):
    known_spend = bucket["spend_values"]
    spend = sum(known_spend, ZERO) if spend_available and known_spend else None
    cost_coverage_complete = not (
        bucket["cash_campaign_ids"] - bucket["spend_campaign_ids"]
    )
    complete_spend = (
        spend_available
        and bucket["ad_rows"] > 0
        and len(known_spend) == bucket["ad_rows"]
        and cost_coverage_complete
        and bucket["product_mapping_complete"]
    )
    impressions = (
        sum(bucket["impressions_values"])
        if spend_available and bucket["impressions_values"]
        else None
    )
    clicks = (
        sum(bucket["clicks_values"])
        if spend_available and bucket["clicks_values"]
        else None
    )
    complete_impressions = (
        spend_available
        and bucket["ad_rows"] > 0
        and len(bucket["impressions_values"]) == bucket["ad_rows"]
    )
    complete_clicks = (
        spend_available
        and bucket["ad_rows"] > 0
        and len(bucket["clicks_values"]) == bucket["ad_rows"]
    )
    return {
        "known_spend": known_spend,
        "spend": spend,
        "cost_coverage_complete": cost_coverage_complete,
        "complete_spend": complete_spend,
        "impressions": impressions,
        "clicks": clicks,
        "complete_impressions": complete_impressions,
        "complete_clicks": complete_clicks,
    }


def _spend_note(bucket, *, spend_available, delivery):
    if not spend_available:
        return SPEND_NOTE
    if not bucket["product_mapping_complete"]:
        return PRODUCT_SPEND_NOTE
    if not delivery["cost_coverage_complete"]:
        return COST_COVERAGE_NOTE
    if bucket["ad_rows"] > len(delivery["known_spend"]):
        return "Belanja sebagian belum diketahui."
    if not delivery["known_spend"]:
        return "Belum ada belanja tercatat."
    return "Belanja dilaporkan dari sumber iklan."


def _paid_media_performance(bucket, *, spend_available, cash_available, delivery):
    spend = delivery["spend"]
    cash = bucket["cash"] if cash_available else None
    refunds = bucket["refunds"] if cash_available else None
    complete_spend = delivery["complete_spend"]
    complete_clicks = delivery["complete_clicks"]
    complete_impressions = delivery["complete_impressions"]
    known_spend = delivery["known_spend"]
    impressions = delivery["impressions"]
    clicks = delivery["clicks"]
    return {
        "cash_in": _number(cash),
        "actual_refunds": _number(refunds),
        "net_cash_in": _number(cash - refunds)
        if cash is not None and refunds is not None
        else None,
        "spend": _number(spend),
        "cash_to_spend": _number(cash / spend)
        if cash is not None and complete_spend and spend > 0
        else None,
        "spend_known_rows": len(known_spend),
        "spend_rows": bucket["ad_rows"],
        "spend_complete": complete_spend,
        "recorded_spend_complete": complete_spend,
        "source_period_complete": False,
        "source_coverage_note": "Cakupan sumber untuk periode ini belum dikonfirmasi. Rasio hanya memakai biaya yang tercatat.",
        "cash_cost_coverage_complete": delivery["cost_coverage_complete"],
        "product_mapping_complete": bucket["product_mapping_complete"],
        "impressions": impressions,
        "clicks": clicks,
        "impressions_known_rows": len(bucket["impressions_values"]),
        "clicks_known_rows": len(bucket["clicks_values"]),
        "impressions_complete": complete_impressions,
        "clicks_complete": complete_clicks,
        "ctr": _percent(clicks, impressions)
        if complete_clicks and complete_impressions
        else None,
        "cpc": _number(spend / clicks)
        if complete_spend and complete_clicks and clicks
        else None,
        "cpm": _number(spend * 1000 / impressions)
        if complete_spend and complete_impressions and impressions
        else None,
        "spend_note": _spend_note(
            bucket, spend_available=spend_available, delivery=delivery
        ),
        "cash_note": CASH_NOTE
        if not cash_available
        else "Uang masuk bruto memakai tanggal mutasi. Kas setelah refund mengurangkan pengembalian invoice yang sudah dibayar dari bank; kewajiban refund yang belum dibayar tidak mengurangi kas. Keduanya bukan laba.",
        "contexts": sorted(bucket["contexts"]),
    }


def _metrics(
    bucket,
    paid_by_invoice,
    cutoff,
    *,
    spend_available,
    cash_available,
    applied_by_invoice=None,
):
    delivery = _ad_delivery_metrics(bucket, spend_available=spend_available)
    return {
        **_lead_performance(
            bucket,
            paid_by_invoice,
            cutoff,
            cash_available=cash_available,
            applied_by_invoice=applied_by_invoice,
        ),
        **_paid_media_performance(
            bucket,
            spend_available=spend_available,
            cash_available=cash_available,
            delivery=delivery,
        ),
    }
