"""Tenant-scoped marketing facts. Receipt dates and lead cohorts stay separate."""
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from statistics import median
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from core.models import Organization
from finance.models import Allocation
from director.models import CHANNEL_CHOICES
from .models import AdDailyObservation, Lead


JAKARTA = ZoneInfo("Asia/Jakarta")
ZERO = Decimal("0")
GROUP_BY_CHOICES = (
    ("channel", "Kanal"), ("campaign", "Kampanye"), ("member", "Penanggung jawab lead"),
    ("product", "Produk"), ("segment", "Segmen"), ("region", "Wilayah lead"), ("ad", "Iklan"),
)
DIMENSIONS = GROUP_BY_CHOICES
FILTER_KEYS = {"channel", "campaign", "member", "product", "segment", "region", "ad"}
SPEND_NOTE = "Belanja iklan tidak dapat dipetakan ke penanggung jawab lead, segmen, atau wilayah lead dengan data yang tersedia."
CASH_NOTE = "Lead belum memiliki tautan ke iklan individual; kas per iklan belum tersedia."
PRODUCT_SPEND_NOTE = "Belanja masih subtotal yang sudah dipetakan ke produk. Sebagian penerimaan berasal dari kampanye dengan produk yang belum diketahui atau berbeda; rasio kas/belanja produk belum tersedia."
COST_COVERAGE_NOTE = "Belanja masih subtotal yang diketahui. Ada penerimaan dari kampanye tanpa bukti belanja periode ini atau tanpa pengaitan kampanye. Lengkapi sumber biaya, termasuk nol yang terverifikasi, sebelum menghitung rasio kas/belanja."


def _number(value):
    return format(Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f") if value is not None else None


def _percent(numerator, denominator):
    return _number(Decimal(numerator) * 100 / Decimal(denominator)) if denominator else None


def _date(value):
    if isinstance(value, datetime):
        raise ValidationError("Gunakan tanggal tanpa waktu untuk periode analisis.")
    try:
        return value if isinstance(value, date) else date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValidationError("Tanggal analisis tidak valid.") from None


def _validate(start, end, group_by, filters):
    start, end = _date(start), _date(end)
    if end < start or (end - start).days > 730 or start.year < 2000 or end.year > 2200:
        raise ValidationError("Periode analisis harus berurutan, maksimal 731 hari, dalam tahun 2000–2200.")
    if group_by not in dict(GROUP_BY_CHOICES):
        raise ValidationError("Dimensi analisis tidak dikenal.")
    if filters is not None and not isinstance(filters, dict):
        raise ValidationError("Filter analisis tidak valid.")
    clean = {}
    for key, value in (filters or {}).items():
        if key not in FILTER_KEYS:
            raise ValidationError("Filter analisis tidak dikenal.")
        if value in (None, ""):
            continue
        if key in {"campaign", "member", "product"}:
            try:
                if isinstance(value, bool) or str(int(value)) != str(value) or int(value) <= 0:
                    raise ValueError
                value = int(value)
            except (TypeError, ValueError):
                raise ValidationError("Identitas filter tidak valid.") from None
        elif not isinstance(value, str) or len(value) > 200:
            raise ValidationError("Nilai filter tidak valid.")
        clean[key] = value
    return start, end, clean


def _lead_value(lead, key):
    if key == "campaign":
        return lead.campaign_id
    if key == "member":
        return lead.owner_id
    if key == "product":
        return lead.product_id or (lead.invoice.product_id if lead.invoice_id else None)
    return getattr(lead, key, "")


def _lead_group(lead, group_by):
    value = _lead_value(lead, group_by)
    if group_by == "campaign":
        label = lead.campaign.name if lead.campaign_id else "Tanpa kampanye"
    elif group_by == "member":
        label = lead.owner.name if lead.owner_id else "Belum ditugaskan"
    elif group_by == "product":
        product = lead.product or (lead.invoice.product if lead.invoice_id else None)
        label = product.name if product else "Produk belum diketahui"
    elif group_by == "segment":
        label = {"direct": "Langsung", "reseller": "Reseller", "institution": "Institusi"}.get(value, value)
    elif group_by == "channel":
        label = dict(CHANNEL_CHOICES).get(value, value or "Belum diketahui")
    else:
        label = value or "Belum diketahui"
    return str(value or "unknown"), label


def _ad_group(observation, group_by):
    campaign = observation.campaign
    if group_by == "campaign":
        return str(campaign.pk), campaign.name
    if group_by == "product":
        return str(campaign.product_id or "unknown"), campaign.product.name if campaign.product_id else "Produk belum diketahui"
    if group_by == "ad":
        return f"{campaign.pk}:{observation.ad_name}", f"{campaign.name} · {observation.ad_name or 'Iklan tanpa nama'}"
    return campaign.channel or "unknown", dict(CHANNEL_CHOICES).get(campaign.channel, campaign.channel or "Belum diketahui")


def _bucket(label):
    return {"label": label, "cash": ZERO, "cohort": [], "spend_values": [], "impressions_values": [], "clicks_values": [], "ad_rows": 0,
            "overdue": 0, "unpaid_won": 0, "unpaid_won_amount": ZERO, "contexts": set(),
            "cash_campaign_ids": set(), "spend_campaign_ids": set(), "product_mapping_complete": True}


def _metrics(bucket, paid_by_invoice, cutoff, *, spend_available, cash_available):
    leads = bucket["cohort"]
    paid = [lead for lead in leads if lead.invoice_id and paid_by_invoice.get(lead.invoice_id, ZERO) > 0]
    responses = [Decimal(str((lead.first_response_at - lead.received_at).total_seconds())) / 60
                 for lead in leads if lead.first_response_at and lead.received_at <= lead.first_response_at < cutoff]
    known_spend = bucket["spend_values"]
    spend = sum(known_spend, ZERO) if spend_available and known_spend else None
    cost_coverage_complete = not (bucket["cash_campaign_ids"] - bucket["spend_campaign_ids"])
    complete_spend = spend_available and bucket["ad_rows"] > 0 and len(known_spend) == bucket["ad_rows"] and cost_coverage_complete and bucket["product_mapping_complete"]
    impressions = sum(bucket["impressions_values"]) if spend_available and bucket["impressions_values"] else None
    clicks = sum(bucket["clicks_values"]) if spend_available and bucket["clicks_values"] else None
    complete_impressions = spend_available and bucket["ad_rows"] > 0 and len(bucket["impressions_values"]) == bucket["ad_rows"]
    complete_clicks = spend_available and bucket["ad_rows"] > 0 and len(bucket["clicks_values"]) == bucket["ad_rows"]
    cash = bucket["cash"] if cash_available else None
    return {
        "cash_in": _number(cash), "net_cash_in": None, "spend": _number(spend), "leads": len(leads) if cash_available else None,
        "cohort_paid_leads": len(paid) if cash_available else None,
        "cohort_paid_conversion": _percent(len(paid), len(leads)) if cash_available else None,
        "cohort_cash_in": _number(sum((paid_by_invoice[lead.invoice_id] for lead in paid), ZERO)) if cash_available else None,
        "median_response_minutes": _number(median(responses)) if responses else None,
        "followed_up_leads": len(responses) if cash_available else None,
        "response_coverage_percent": _percent(len(responses), len(leads)) if cash_available else None,
        "overdue_followups": bucket["overdue"] if cash_available else None,
        "unpaid_won_leads": bucket["unpaid_won"] if cash_available else None,
        "unpaid_won_amount": _number(bucket["unpaid_won_amount"]) if cash_available else None,
        "cash_to_spend": _number(cash / spend) if cash is not None and complete_spend and spend > 0 else None,
        "spend_known_rows": len(known_spend), "spend_rows": bucket["ad_rows"],
        "spend_complete": complete_spend,
        "cash_cost_coverage_complete": cost_coverage_complete,
        "product_mapping_complete": bucket["product_mapping_complete"],
        "impressions": impressions, "clicks": clicks,
        "impressions_known_rows": len(bucket["impressions_values"]), "clicks_known_rows": len(bucket["clicks_values"]),
        "impressions_complete": complete_impressions, "clicks_complete": complete_clicks,
        "ctr": _percent(clicks, impressions) if complete_clicks and complete_impressions else None,
        "cpc": _number(spend / clicks) if complete_spend and complete_clicks and clicks else None,
        "cpm": _number(spend * 1000 / impressions) if complete_spend and complete_impressions and impressions else None,
        "spend_note": SPEND_NOTE if not spend_available else PRODUCT_SPEND_NOTE if not bucket["product_mapping_complete"] else COST_COVERAGE_NOTE if not cost_coverage_complete else ("Belanja sebagian belum diketahui." if bucket["ad_rows"] > len(known_spend) else "Belum ada belanja tercatat." if not known_spend else "Belanja dilaporkan dari sumber iklan."),
        "cash_note": CASH_NOTE if not cash_available else "Penerimaan bruto teralokasi, berdasarkan tanggal mutasi bank; bukan laba atau kas bersih.",
        "contexts": sorted(bucket["contexts"]),
    }


def _period(organization, start, end, group_by, filters):
    today = timezone.localdate(timezone=JAKARTA)
    observation_end = min(end, today)
    cutoff = datetime.combine(observation_end + timedelta(days=1), time.min, JAKARTA)
    beginning = datetime.combine(start, time.min, JAKARTA)
    cash_available = group_by != "ad" and "ad" not in filters
    spend_available = group_by in {"channel", "campaign", "product", "ad"} and not ({"member", "segment", "region"} & filters.keys())
    all_leads = list(Lead.objects.filter(organization=organization).select_related("campaign", "owner", "product", "invoice__product"))
    linked = {lead.invoice_id: lead for lead in all_leads if lead.invoice_id}
    selected = [lead for lead in all_leads if all(_lead_value(lead, key) == value for key, value in filters.items() if key != "ad")]
    selected_ids = {lead.pk for lead in selected}
    paid_by_invoice = dict(Allocation.objects.filter(
        organization=organization, invoice__organization=organization, transaction__organization=organization,
        invoice_id__in=[lead.invoice_id for lead in selected if lead.invoice_id], transaction__date__lte=observation_end,
    ).values("invoice_id").annotate(total=Sum("amount")).values_list("invoice_id", "total"))
    rows = {}
    summary = _bucket("Total")

    def row(lead):
        key, label = _lead_group(lead, group_by)
        return rows.setdefault(key, _bucket(label))

    if cash_available:
        for lead in selected:
            if beginning <= lead.received_at < cutoff:
                summary["cohort"].append(lead)
                item = row(lead)
                item["cohort"].append(lead)
                item["contexts"].add(f"{lead.channel or 'Kanal belum diketahui'} · {lead.segment}")
            # Outstanding work is a present-day operational measure, not historical status reconstructed from current stage.
            unpaid = not lead.invoice_id or paid_by_invoice.get(lead.invoice_id, ZERO) < lead.invoice.total
            if start <= today <= end and lead.received_at < cutoff and lead.next_follow_up and lead.next_follow_up < today and lead.stage != "lost" and unpaid:
                summary["overdue"] += 1
                row(lead)["overdue"] += 1
            if start <= today <= end and lead.received_at < cutoff and lead.stage == "won" and lead.invoice_id and unpaid:
                outstanding = lead.invoice.total - paid_by_invoice.get(lead.invoice_id, ZERO)
                for bucket in (summary, row(lead)):
                    bucket["unpaid_won"] += 1
                    bucket["unpaid_won_amount"] += outstanding

    receipts = Allocation.objects.filter(organization=organization, invoice__organization=organization,
                                        transaction__organization=organization, transaction__date__range=(start, observation_end))
    total_cash = attributed_cash = ZERO
    for allocation in receipts:
        total_cash += allocation.amount
        lead = linked.get(allocation.invoice_id)
        if lead is not None:
            attributed_cash += allocation.amount
        if cash_available and lead is not None and lead.pk in selected_ids:
            item = row(lead)
            for bucket in (summary, item):
                bucket["cash"] += allocation.amount
                # An absent report is not evidence that this campaign incurred zero spend.
                bucket["cash_campaign_ids"].add(lead.campaign_id)
            effective_product = _lead_value(lead, "product")
            product_mapping_complete = bool(effective_product and lead.campaign_id and lead.campaign.product_id == effective_product)
            if not product_mapping_complete:
                if "product" in filters:
                    summary["product_mapping_complete"] = False
                if group_by == "product" or "product" in filters:
                    item["product_mapping_complete"] = False

    ads = AdDailyObservation.objects.filter(organization=organization, campaign__organization=organization,
                                           date__range=(start, observation_end)).select_related("campaign__product")
    for key, value in filters.items():
        lookup = {"campaign": "campaign_id", "channel": "campaign__channel", "product": "campaign__product_id", "ad": "ad_name"}.get(key)
        if lookup:
            ads = ads.filter(**{lookup: value})
    if spend_available:
        for observation in ads:
            key, label = _ad_group(observation, group_by)
            item = rows.setdefault(key, _bucket(label))
            if group_by == "product" and not observation.campaign.product_id:
                item["product_mapping_complete"] = False
            for bucket in (summary, item):
                bucket["ad_rows"] += 1
                if observation.spend is not None:
                    bucket["spend_values"].append(observation.spend)
                    bucket["spend_campaign_ids"].add(observation.campaign_id)
                if observation.impressions is not None:
                    bucket["impressions_values"].append(observation.impressions)
                if observation.clicks is not None:
                    bucket["clicks_values"].append(observation.clicks)

    result = _metrics(summary, paid_by_invoice, cutoff, spend_available=spend_available, cash_available=cash_available)
    result_rows = [{"key": key, "label": bucket["label"], **_metrics(bucket, paid_by_invoice, cutoff, spend_available=spend_available, cash_available=cash_available)} for key, bucket in sorted(rows.items(), key=lambda item: item[1]["label"].casefold())]
    # Historical work queues require state history; do not show today's state as a historical zero.
    if not start <= today <= end:
        for item in [result, *result_rows]:
            for key in ("overdue_followups", "unpaid_won_leads", "unpaid_won_amount"):
                item[key] = None
    if start > today:
        for key in ("cash_in", "leads", "cohort_paid_leads", "cohort_cash_in", "followed_up_leads"):
            result[key] = None
    return result, result_rows, {"total_customer_receipts": _number(total_cash), "attributed_receipts": _number(attributed_cash),
                                "unattributed_receipts": _number(total_cash - attributed_cash),
                                "attribution_percent": _percent(attributed_cash, total_cash),
                                "spend_rows": summary["ad_rows"], "spend_known_rows": len(summary["spend_values"]),
                                "scope": "Cakupan pengaitan kas seluruh organisasi pada periode ini; angka tabel mengikuti filter."}


@transaction.atomic
def marketing_snapshot(*, organization, start, end, group_by="channel", filters=None):
    # Marketing and finance writers share this short tenant lock. Release before AI/network work.
    Organization.objects.select_for_update().get(pk=organization.pk)
    start, end, filters = _validate(start, end, group_by, filters)
    today = timezone.localdate(timezone=JAKARTA)
    previous_end = start - timedelta(days=1)
    comparison_span = min(end, today) - start if start <= today else end - start
    previous_start = previous_end - comparison_span
    summary, rows, coverage = _period(organization, start, end, group_by, filters)
    previous, _, _ = _period(organization, previous_start, previous_end, group_by, filters)
    quality = [
        "Kas adalah penerimaan pelanggan bruto yang teralokasi; refund, chargeback, dan uang muka sebelum invoice belum didukung. Kas bersih belum tersedia.",
        "Konversi pembayaran memakai lead yang masuk dalam periode dan pembayaran sampai akhir periode. Kas periode dapat berasal dari lead periode sebelumnya.",
        "Rasio kas/belanja periode adalah indikator operasional, bukan ROAS kausal atau ROAS cohort. Jangan menjumlahkan rasio antar baris.",
        "Respons memakai lead dengan waktu respons tercatat; respons kosong tidak dianggap nol menit. Perbandingan anggota perlu mempertimbangkan kanal, segmen, beban, dan umur lead.",
        "Cakupan pengaitan tidak menjamin seluruh mutasi bank atau pengeluaran perusahaan telah diimpor.",
    ]
    if Decimal(coverage["unattributed_receipts"]) > 0:
        quality.append("Sebagian penerimaan belum terhubung dengan lead. Lengkapi pengaitan invoice sebelum menyimpulkan kontribusi marketing.")
    if not summary["spend_complete"]:
        quality.append(summary["spend_note"])
    if any(not item["product_mapping_complete"] for item in rows) and PRODUCT_SPEND_NOTE not in quality:
        quality.append(PRODUCT_SPEND_NOTE)
    if any(not item["cash_cost_coverage_complete"] for item in rows) and COST_COVERAGE_NOTE not in quality:
        quality.append(COST_COVERAGE_NOTE)
    if group_by == "ad" or "ad" in filters:
        quality.append(CASH_NOTE)
    if end < timezone.localdate(timezone=JAKARTA):
        quality.append("Follow-up terlambat hanya tersedia untuk kondisi hari ini; antrean historis belum direkonstruksi.")
    if end > today:
        quality.append(f"Data aktual hanya dihitung sampai {today.isoformat()}. Periode masa depan belum memiliki hasil; mutasi dan belanja bertanggal masa depan dikecualikan.")
        if start <= today:
            quality.append(f"Perbandingan menggunakan {comparison_span.days + 1} hari yang sudah teramati dan periode sebelumnya dengan panjang sama.")
    quality.append("Sepakat membeli belum berarti lunas. Follow-up tetap memuat prospek sepakat dengan invoice yang belum lunas; piutang ini bukan penerimaan kas.")
    legacy = {"reported_spend": None, "reported_value": None, "rows": 0,
              "basis": "Rekap lama seluruh organisasi pada periode ini, tanpa filter dimensi; tidak digabungkan dengan kas atau belanja iklan rinci."}
    from director.models import MarketingObservation
    old = MarketingObservation.objects.filter(organization=organization, status="active", date__range=(start, end))
    legacy.update(rows=old.count(), reported_spend=_number(old.aggregate(value=Sum("spend"))["value"]),
                  reported_value=_number(old.aggregate(value=Sum("reported_value"))["value"]))
    return {"organization_id": organization.pk, "as_of": timezone.now().isoformat(), "formula_version": "marketing-cash-v1",
            "attribution_model": "Satu lead–satu invoice; 100% kanal sumber lead, bukan bukti sebab akibat.",
            "period": {"start": start.isoformat(), "end": end.isoformat(), "previous_start": previous_start.isoformat(), "previous_end": previous_end.isoformat()},
            "observation_end": min(end, today).isoformat(), "observed_through": min(end, today).isoformat(),
            "group_by": group_by, "filters": filters, "dimension_choices": GROUP_BY_CHOICES,
            "summary": summary, "previous_summary": previous, "rows": rows, "coverage": coverage,
            "data_quality": quality, "legacy": legacy}
