"""KPI calculations share Marketing/Finance facts; approval never posts a journal."""

import hashlib
import json
import re
from datetime import datetime, time, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, localcontext
from statistics import median

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from core.models import Organization
from finance.fulfillment_models import InvoiceCredit
from finance.models import (
    AdvanceApplication,
    Allocation,
    CustomerTaxSettlement,
    Invoice,
    InvoiceCancellation,
)

from .ad_sources import active_ad_observations, recovery_manifest
from .analytics import JAKARTA, marketing_snapshot
from .kpi_models import KPI_METRICS, KpiReview, KpiTarget
from .models import Campaign, Lead, SourceCoverage, TeamMember
from .services import (
    _audit,
    _decimal,
    _fields,
    _org,
    _save,
    _scoped,
    _snapshot,
    _version,
)

TARGET_FIELDS = {
    "title",
    "metric_key",
    "target",
    "period_start",
    "period_end",
    "member",
    "channel",
    "campaign",
    "rationale",
}
APPROVERS = {"owner", "director"}
TRANSITION_FIELDS = {"status", "version", "approved_by", "approved_at"}
STATES = {
    "draft": "Draf, belum menjadi target resmi",
    "retired": "Dihentikan, tidak dihitung sebagai target aktif",
    "upcoming": "Periode belum dimulai",
    "in_progress": "Periode berjalan; hasil sementara",
    "no_data": "Belum ada data untuk dinilai",
    "incomplete": "Bukti belum lengkap untuk penilaian",
    "sources_incomplete": "Sementara; kelengkapan sumber belum disahkan",
    "met": "Target tercapai",
    "not_met": "Target belum tercapai",
}


def _text(value, *, label, maximum=500):
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or "\x00" in value
    ):
        raise ValidationError(f"{label} wajib diisi, maksimal {maximum} karakter.")
    return value.strip()


@transaction.atomic
def create_target(*, organization, actor, **fields):
    org = _org(organization, actor)
    if set(fields) - TARGET_FIELDS:
        raise ValidationError(
            "Ada kolom target yang tidak boleh ditetapkan melalui tindakan ini."
        )
    raw = dict(fields)
    rationale = _text(raw.pop("rationale", None), label="Dasar target", maximum=4000)
    number = _decimal(raw.pop("target", None))
    if number is None:
        raise ValidationError({"target": "Isi nilai target."})
    member = raw.pop("member", None)
    member = None if member in (None, "") else _scoped(TeamMember, member, org)
    if member and not member.active:
        raise ValidationError({"member": "Pilih anggota tim aktif."})
    values = _fields(raw, TARGET_FIELDS - {"target", "member"}, org)
    if values.get("campaign") and not values.get("channel"):
        values["channel"] = values["campaign"].channel
    obj = _save(
        KpiTarget(
            organization=org,
            created_by=actor,
            target=number,
            member=member,
            rationale=rationale,
            **values,
        )
    )
    _audit(
        org,
        actor,
        "kpi.created",
        obj,
        values=_snapshot(obj, TARGET_FIELDS),
        version=obj.version,
    )
    return obj


@transaction.atomic
def activate_target(*, organization, actor, target, expected_version, reason):
    org = _org(organization, actor, APPROVERS)
    obj = _scoped(KpiTarget, target, org)
    _version(obj, expected_version)
    reason = _text(reason, label="Alasan persetujuan")
    if obj.status != "draft":
        raise ValidationError("Hanya draf target yang dapat disetujui.")
    before = _snapshot(obj, TRANSITION_FIELDS)
    obj.status, obj.approved_by, obj.approved_at = "active", actor, timezone.now()
    obj.version += 1
    obj._kpi_transition = True
    _save(obj)
    _audit(
        org,
        actor,
        "kpi.activated",
        obj,
        before=before,
        after=_snapshot(obj, TRANSITION_FIELDS),
        reason=reason,
        version=obj.version,
        retroactive=timezone.localdate(obj.approved_at, JAKARTA) > obj.period_start,
    )
    return obj


@transaction.atomic
def retire_target(*, organization, actor, target, expected_version, reason):
    org = _org(organization, actor, APPROVERS)
    obj = _scoped(KpiTarget, target, org)
    _version(obj, expected_version)
    reason = _text(reason, label="Alasan penghentian")
    if obj.status == "retired":
        raise ValidationError("Target ini sudah dihentikan.")
    before = _snapshot(obj, TRANSITION_FIELDS)
    obj.status = "retired"
    obj.version += 1
    obj._kpi_transition = True
    _save(obj)
    _audit(
        org,
        actor,
        "kpi.retired",
        obj,
        before=before,
        after=_snapshot(obj, TRANSITION_FIELDS),
        reason=reason,
        version=obj.version,
    )
    return obj


def _number(value, rounding=ROUND_HALF_UP):
    with localcontext() as context:
        context.prec = 60
        return (
            format(Decimal(value).quantize(Decimal("0.01"), rounding=rounding), "f")
            if value is not None
            else None
        )


def _source_manifest(queryset, fields):
    """Hash every scoped fact, but bound the snapshot's reference list and omit PII."""
    digest, count, references = hashlib.sha256(), 0, []
    for row in queryset.order_by("pk").values(*fields).iterator(chunk_size=500):
        digest.update(
            json.dumps(row, sort_keys=True, default=str, separators=(",", ":")).encode()
        )
        digest.update(b"\n")
        count += 1
        if len(references) < 100:
            reference = {"id": row["pk"]}
            if "version" in row:
                reference["version"] = row["version"]
            references.append(reference)
    return {
        "count": count,
        "digest": digest.hexdigest(),
        "records": references,
        "references_complete": count <= 100,
    }


def _source_provenance(target, filters, start, end):
    """Capture the identities behind metrics, including replaced equal-value evidence.

    Values are only hashed; saved references contain database IDs and versions, not
    customer names, lead notes, bank references, source documents or account numbers.
    The caller holds the same tenant lock used by all source writers.
    """
    beginning = datetime.combine(start, time.min, JAKARTA)
    cutoff = datetime.combine(end + timedelta(days=1), time.min, JAKARTA)
    all_leads = Lead.objects.filter(
        organization_id=target.organization_id, received_at__lt=cutoff
    )
    for key, value in filters.items():
        all_leads = all_leads.filter(
            **{
                {"member": "owner_id", "campaign": "campaign_id", "channel": "channel"}[
                    key
                ]: value
            }
        )
    cohort = all_leads.filter(received_at__gte=beginning, received_at__lt=cutoff)
    allocations = Allocation.objects.filter(
        organization_id=target.organization_id,
        invoice__organization_id=target.organization_id,
        transaction__organization_id=target.organization_id,
    )
    from operations.attribution import invoice_attribution

    mapping = invoice_attribution(organization=target.organization)
    scoped_ids = set(all_leads.values_list("pk", flat=True))
    mapped_invoices = {
        invoice_id: lead_id
        for invoice_id, lead_id in mapping["by_invoice"].items()
        if lead_id in scoped_ids
    }
    period_allocations = allocations.filter(
        invoice_id__in=mapped_invoices, transaction__date__range=(start, end)
    )
    tax_settlements = CustomerTaxSettlement.objects.filter(
        organization_id=target.organization_id,
        invoice__organization_id=target.organization_id,
    ).filter(
        Q(bank_posting__transaction__organization_id=target.organization_id)
        | Q(
            source_allocation__organization_id=target.organization_id,
            source_allocation__invoice__organization_id=target.organization_id,
            source_allocation__transaction__organization_id=target.organization_id,
        )
        | Q(
            source_advance_application__organization_id=target.organization_id,
            source_advance_application__invoice__organization_id=target.organization_id,
            source_advance_application__advance__organization_id=target.organization_id,
            source_advance_application__advance__bank_posting__organization_id=target.organization_id,
            source_advance_application__advance__bank_posting__transaction__organization_id=target.organization_id,
        )
    )
    period_tax_settlements = tax_settlements.filter(
        invoice_id__in=mapped_invoices, cash_amount__gt=0, date__range=(start, end)
    )
    cash_invoice_ids = set(
        period_allocations.values_list("invoice_id", flat=True)
    ) | set(period_tax_settlements.values_list("invoice_id", flat=True))
    cash_leads = all_leads.filter(
        pk__in={mapped_invoices[pk] for pk in cash_invoice_ids}
    )
    provenance = {
        "version": "marketing-kpi-sources-v7",
        "reference_limit": 100,
        "sources": {"identity_lineage": {"digest": mapping["fingerprint"]}},
    }
    sources = provenance["sources"]
    metric = target.metric_key
    paid_cohort_metrics = {
        "cohort_paid_leads",
        "cohort_paid_conversion",
        "cohort_fully_paid_leads",
        "cohort_fully_paid_conversion",
    }
    if metric in {
        "leads",
        "response_coverage_percent",
        "median_response_minutes",
        "followup_on_time_percent",
        *paid_cohort_metrics,
    }:
        sources["leads"] = _source_manifest(
            cohort,
            (
                "pk",
                "version",
                "campaign_id",
                "owner_id",
                "channel",
                "received_at",
                "first_response_at",
                "invoice_id",
            ),
        )
    if metric == "followup_on_time_percent":
        from .workflow_models import LeadContact

        promises = LeadContact.objects.filter(
            organization_id=target.organization_id,
            lead_id__in=scoped_ids,
            followup_action="set",
            next_due_at__gte=beginning,
            next_due_at__lt=cutoff,
            created_at__lt=cutoff,
        )
        event_ids = set(promises.values_list("pk", flat=True))
        frontier = set(event_ids)
        completion_ids = (
            set(
                LeadContact.objects.filter(
                    organization_id=target.organization_id,
                    lead_id__in=scoped_ids,
                    completes_id__in=event_ids,
                    created_at__lt=cutoff,
                ).values_list("pk", flat=True)
            )
            if event_ids
            else set()
        )
        event_ids.update(completion_ids)
        frontier.update(completion_ids)
        while frontier:
            children = set(
                LeadContact.objects.filter(
                    organization_id=target.organization_id,
                    lead_id__in=scoped_ids,
                    corrects_id__in=frontier,
                    created_at__lt=cutoff,
                ).values_list("pk", flat=True)
            )
            children.difference_update(event_ids)
            event_ids.update(children)
            frontier = children
        sources["followups"] = _source_manifest(
            promises,
            (
                "pk",
                "lead_id",
                "occurred_at",
                "created_at",
                "created_by_id",
                "followup_action",
                "next_due_at",
            ),
        )
        sources["followup_events"] = _source_manifest(
            LeadContact.objects.filter(
                organization_id=target.organization_id,
                pk__in=event_ids - set(promises.values_list("pk", flat=True)),
            ),
            (
                "pk",
                "lead_id",
                "occurred_at",
                "created_at",
                "created_by_id",
                "completes_id",
                "corrects_id",
            ),
        )
    if metric in {"cash_in", "cash_to_spend"}:
        sources["leads"] = _source_manifest(
            all_leads,
            ("pk", "version", "campaign_id", "owner_id", "channel", "invoice_id"),
        )
        sources["allocations"] = _source_manifest(
            period_allocations,
            ("pk", "invoice_id", "transaction_id", "amount", "transaction__date"),
        )
        sources["customer_tax_settlements"] = _source_manifest(
            period_tax_settlements,
            (
                "pk",
                "invoice_id",
                "date",
                "bank_posting_id",
                "source_allocation_id",
                "source_advance_application_id",
                "verification_id",
                "cash_amount",
                "tax_amount",
                "gross_amount",
            ),
        )
    if metric in paid_cohort_metrics:
        cohort_ids = set(cohort.values_list("pk", flat=True))
        invoice_ids = [
            invoice_id
            for invoice_id, lead_id in mapped_invoices.items()
            if lead_id in cohort_ids
        ]
        sources["invoices"] = _source_manifest(
            Invoice.objects.filter(
                organization_id=target.organization_id, pk__in=invoice_ids
            ),
            ("pk", "date", "status", "quantity", "unit_price"),
        )
        sources["allocations"] = _source_manifest(
            allocations.filter(invoice_id__in=invoice_ids, transaction__date__lte=end),
            ("pk", "invoice_id", "transaction_id", "amount", "transaction__date"),
        )
        sources["advance_applications"] = _source_manifest(
            AdvanceApplication.objects.filter(
                organization_id=target.organization_id,
                invoice__organization_id=target.organization_id,
                advance__organization_id=target.organization_id,
                invoice_id__in=invoice_ids,
                date__lte=end,
            ),
            ("pk", "invoice_id", "advance_id", "date", "amount"),
        )
        sources["customer_tax_settlements"] = _source_manifest(
            tax_settlements.filter(invoice_id__in=invoice_ids, date__lte=end),
            (
                "pk",
                "invoice_id",
                "date",
                "bank_posting_id",
                "source_allocation_id",
                "source_advance_application_id",
                "verification_id",
                "cash_amount",
                "tax_amount",
                "gross_amount",
            ),
        )
        sources["invoice_cancellations"] = _source_manifest(
            InvoiceCancellation.objects.filter(
                organization_id=target.organization_id, invoice_id__in=invoice_ids
            ),
            ("pk", "invoice_id", "date"),
        )
        if metric in {"cohort_fully_paid_leads", "cohort_fully_paid_conversion"}:
            sources["invoice_credits"] = _source_manifest(
                InvoiceCredit.objects.filter(
                    organization_id=target.organization_id,
                    invoice_id__in=invoice_ids,
                    date__lte=end,
                ),
                ("pk", "invoice_id", "kind", "quantity", "amount", "date"),
            )
    if metric in {"cash_in", "cash_to_spend", *paid_cohort_metrics}:
        from operations.models import OrderLine

        relevant_ids = (
            set(cohort.values_list("pk", flat=True))
            if metric.startswith("cohort_")
            else scoped_ids
        )
        relevant_mapping = sorted(
            (pk, lead_id)
            for pk, lead_id in mapped_invoices.items()
            if lead_id in relevant_ids
        )
        sources["invoice_attribution"] = {
            "count": len(relevant_mapping),
            "digest": hashlib.sha256(json.dumps(relevant_mapping).encode()).hexdigest(),
            "records": [
                {"invoice_id": pk, "lead_id": lead_id}
                for pk, lead_id in relevant_mapping[:100]
            ],
            "references_complete": len(relevant_mapping) <= 100,
        }
        sources["order_invoice_links"] = _source_manifest(
            OrderLine.objects.filter(
                organization=target.organization,
                order__organization=target.organization,
                order__lead_id__in=relevant_ids,
            ).exclude(invoice=None),
            ("pk", "order_id", "order__lead_id", "order__version", "invoice_id"),
        )
    if metric in {"spend", "cash_to_spend"}:
        campaigns = Campaign.objects.filter(
            organization_id=target.organization_id
        ).filter(
            (
                (Q(start_date__isnull=True) | Q(start_date__lte=end))
                & (Q(end_date__isnull=True) | Q(end_date__gte=start))
                & (Q(actual_end_date__isnull=True) | Q(actual_end_date__gte=start))
            )
            | Q(pk__in=cash_leads.exclude(campaign_id=None).values("campaign_id"))
        )
        observations = active_ad_observations(
            organization=target.organization_id
        ).filter(date__range=(start, end))
        for key in ("campaign", "channel"):
            if key in filters:
                campaigns = campaigns.filter(
                    **{{"campaign": "pk", "channel": "channel"}[key]: filters[key]}
                )
                observations = observations.filter(
                    **{
                        {"campaign": "campaign_id", "channel": "campaign__channel"}[
                            key
                        ]: filters[key]
                    }
                )
        sources["campaigns"] = _source_manifest(
            campaigns,
            (
                "pk",
                "channel",
                "start_date",
                "end_date",
                "actual_end_date",
                "status",
                "version",
            ),
        )
        sources["ad_observations"] = _source_manifest(
            observations,
            ("pk", "version", "campaign_id", "date", "spend", "impressions", "clicks"),
        )
        recoveries = recovery_manifest(
            organization=target.organization_id, start=start, end=end, filters=filters
        )
        if recoveries:
            sources["ad_recoveries"] = {
                "count": len(recoveries),
                "digest": hashlib.sha256(
                    json.dumps(recoveries, sort_keys=True, default=str).encode()
                ).hexdigest(),
                "records": [{"id": row["pk"]} for row in recoveries[:100]],
                "references_complete": len(recoveries) <= 100,
            }
        sources["coverage_declarations"] = _source_manifest(
            SourceCoverage.objects.filter(
                organization_id=target.organization_id,
                campaign_id__in=campaigns.values("pk"),
                withdrawn_at__isnull=True,
                period_start__lte=end,
                period_end__gte=start,
            ),
            (
                "pk",
                "campaign_id",
                "period_start",
                "period_end",
                "declared_spend",
                "observation_fingerprint",
                "created_by_id",
                "created_at",
            ),
        )
    provenance["digest"] = hashlib.sha256(
        json.dumps(sources, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return provenance


@transaction.atomic
def evaluate_target(target):
    if not isinstance(target, KpiTarget) or not target.pk:
        raise ValidationError("Pilih target tersimpan untuk dievaluasi.")
    Organization.objects.select_for_update().get(pk=target.organization_id)
    target = KpiTarget.objects.select_related("organization", "member", "campaign").get(
        pk=target.pk, organization_id=target.organization_id
    )
    spec = KPI_METRICS[target.metric_key]
    filters = {
        key: value
        for key, value in {
            "member": target.member_id,
            "campaign": target.campaign_id,
            "channel": target.channel,
        }.items()
        if value
    }
    report = marketing_snapshot(
        organization=target.organization,
        start=target.period_start,
        end=target.period_end,
        filters=filters,
    )
    summary, today = report["summary"], timezone.localdate(timezone=JAKARTA)
    notes = [
        spec["description"],
        "Hasil berasal dari catatan dalam ERP; kelengkapan impor dan pengaitan perlu diperiksa. Angka kosong bukan nol.",
    ]
    basis = {
        "formula_version": "marketing-kpi-v2",
        "analytics_formula_version": report["formula_version"],
        "scope": filters,
        "summary": {
            key: summary[key]
            for key in (
                "leads",
                "cash_in",
                "cohort_paid_leads",
                "cohort_paid_conversion",
                "cohort_fully_paid_leads",
                "cohort_fully_paid_conversion",
                "followups_due",
                "followups_on_time",
                "followup_on_time_percent",
                "followed_up_leads",
                "response_coverage_percent",
                "median_response_minutes",
                "spend",
                "cash_to_spend",
                "spend_rows",
                "spend_known_rows",
                "recorded_spend_complete",
                "source_period_complete",
                "source_coverage_basis",
            )
        },
        "attribution": report["attribution_model"],
        "coverage": report["coverage"],
    }
    actual = summary.get(target.metric_key)
    cutoff = datetime.combine(
        min(today, target.period_end) + timedelta(days=1), time.min, JAKARTA
    )
    scoped_leads = Lead.objects.filter(
        organization=target.organization, received_at__lt=cutoff
    )
    for key, value in filters.items():
        scoped_leads = scoped_leads.filter(
            **{
                {"member": "owner_id", "campaign": "campaign_id", "channel": "channel"}[
                    key
                ]: value
            }
        )
    # Ratios and elapsed times retain precision for threshold checks. Display rounding
    # must not convert an almost-reached minimum into a successful result.
    with localcontext() as context:
        context.prec = 60
        if (
            target.metric_key
            in {
                "cohort_paid_conversion",
                "cohort_fully_paid_conversion",
                "response_coverage_percent",
            }
            and summary["leads"]
        ):
            numerator = (
                summary["cohort_paid_leads"]
                if target.metric_key == "cohort_paid_conversion"
                else summary["cohort_fully_paid_leads"]
                if target.metric_key == "cohort_fully_paid_conversion"
                else summary["followed_up_leads"]
            )
            actual = Decimal(numerator) * 100 / Decimal(summary["leads"])
        elif (
            target.metric_key == "followup_on_time_percent" and summary["followups_due"]
        ):
            actual = (
                Decimal(summary["followups_on_time"])
                * 100
                / Decimal(summary["followups_due"])
            )
        elif target.metric_key == "median_response_minutes":
            beginning = datetime.combine(target.period_start, time.min, JAKARTA)
            durations = []
            for received, responded in scoped_leads.filter(
                received_at__gte=beginning, first_response_at__lt=cutoff
            ).values_list("received_at", "first_response_at"):
                delta = responded - received
                microseconds = (
                    delta.days * 86400 + delta.seconds
                ) * 1000000 + delta.microseconds
                durations.append(Decimal(microseconds) / Decimal(60000000))
            actual = median(durations) if durations else None
    incomplete = False
    no_data = actual is None
    if target.metric_key in {"spend", "cash_to_spend"}:
        complete = (
            summary["source_period_complete"]
            and summary["spend_known_rows"] == summary["spend_rows"]
        )
        if not complete:
            actual, incomplete = None, True
            notes.append(
                "Penilaian biaya menunggu bukti cakupan sumber lengkap dan cocok dengan seluruh kampanye pada lingkup target; subtotal belum menjadi hasil KPI."
            )
        else:
            spend = Decimal(summary["spend"] or "0")
            with localcontext() as context:
                context.prec = 60
                actual = (
                    spend
                    if target.metric_key == "spend"
                    else Decimal(summary["cash_in"]) / spend
                    if spend
                    else None
                )
            if summary["spend_rows"] == 0:
                notes.append(
                    "Nol belanja didasarkan pada konfirmasi sumber yang masih berlaku, bukan karena laporan tidak ada."
                )
        if target.metric_key == "cash_to_spend":
            notes.append(
                "Rasio tidak tersedia bila belanja nol; pembagian dengan nol tidak menghasilkan pencapaian."
            )
        notes.append(summary["source_coverage_note"])
        no_data = actual is None and not incomplete
    elif target.metric_key == "median_response_minutes":
        incomplete = (
            summary["leads"] not in (None, 0)
            and summary["followed_up_leads"] != summary["leads"]
        )
        if incomplete:
            notes.append(
                "Median hanya mencakup respons yang tercatat. Skor ditahan sampai seluruh prospek periode mempunyai waktu respons; mengabaikan prospek belum direspons akan membuat hasil terlalu baik."
            )
    elif target.metric_key in {
        "leads",
        "cohort_paid_leads",
        "cohort_paid_conversion",
        "cohort_fully_paid_leads",
        "cohort_fully_paid_conversion",
        "response_coverage_percent",
    }:
        no_data = summary["leads"] in (None, 0)
        if no_data:
            notes.append(
                "Belum ada prospek tercatat dalam periode dan lingkup ini. Jumlah nol baru dapat dinilai setelah seluruh sumber diperiksa; persentase dengan penyebut nol tetap tidak tersedia."
            )
    elif target.metric_key == "followup_on_time_percent":
        no_data = summary["followups_due"] in (None, 0)
        if no_data:
            notes.append(
                "Belum ada janji follow-up yang jatuh tempo dalam periode dan lingkup ini; persentase dengan penyebut nol tidak tersedia."
            )
    elif target.metric_key == "cash_in":
        no_data = not scoped_leads.exists() and Decimal(actual or "0") == 0
        notes.append(summary["cash_note"])
        if Decimal(report["coverage"]["unattributed_receipts"]) > 0:
            notes.append(
                "Ada penerimaan perusahaan belum terkait prospek; hasil kas marketing hanya bagian yang sudah terhubung."
            )
    retroactive = (
        timezone.localdate(target.approved_at or target.created_at, JAKARTA)
        > target.period_start
    )
    if retroactive:
        notes.append(
            "Target disusun atau disetujui setelah periode dimulai (retrospektif), bukan komitmen yang ditetapkan sebelum periode."
        )
    notes.append(
        "Pencapaian metrik minimum = aktual ÷ target × 100%. Metrik batas maksimum = minimum(100%, target ÷ aktual × 100%); aktual nol pada batas maksimum mendapat 100%."
    )
    notes.append(
        "Status memakai nilai sebelum pembulatan tampilan; persentase pencapaian dipotong pada dua desimal agar nilai di bawah 100% tidak terlihat sudah tercapai."
    )
    basis["provenance"] = _source_provenance(
        target, filters, target.period_start, min(today, target.period_end)
    )
    from .kpi_completeness import source_period_state

    completeness = source_period_state(
        target,
        basis["provenance"],
        spend_complete=summary["source_period_complete"]
        and summary["spend_known_rows"] == summary["spend_rows"],
    )
    if (
        completeness["complete"]
        and target.metric_key
        in {"leads", "cohort_paid_leads", "cohort_fully_paid_leads", "cash_in"}
        and actual is not None
    ):
        no_data = False
        if Decimal(actual) == 0:
            notes.append(
                "Nol telah diperiksa terhadap daftar seluruh sumber; hasil nol berbeda dari sumber yang belum tersedia."
            )
    score = None
    if (
        actual is not None
        and not incomplete
        and not no_data
        and target.status == "active"
        and today >= target.period_start
    ):
        actual_number = Decimal(actual)
        with localcontext() as context:
            context.prec = 60
            if spec["direction"] == "higher":
                score = actual_number * 100 / target.target
            else:
                score = (
                    min(Decimal(100), target.target * 100 / actual_number)
                    if actual_number
                    else Decimal(100)
                )
    if target.status != "active":
        state = target.status
    elif target.period_start > today:
        state, actual, score = "upcoming", None, None
    elif incomplete:
        state = "incomplete"
    elif no_data:
        state = "no_data"
    elif target.period_end >= today:
        state = "in_progress"
    else:
        state = "met" if score is not None and score >= 100 else "not_met"
    basis["unrounded_actual"] = str(actual) if actual is not None else None
    if state in {"met", "not_met"} and not completeness["complete"]:
        state = "sources_incomplete"
    notes.append(completeness["label"])
    result = {
        "actual": _number(actual),
        "target": _number(target.target),
        **spec,
        "attainment": _number(score, ROUND_DOWN),
        "state": state,
        "state_label": STATES[state],
        "notes": notes,
        "observed_through": report["observed_through"],
        "period_start": target.period_start.isoformat(),
        "period_end": target.period_end.isoformat(),
        "basis": basis,
        "retroactive": retroactive,
        "completeness": completeness,
        "official": target.status == "active"
        and target.period_end < today
        and score is not None
        and completeness["complete"],
    }
    result["fingerprint"] = hashlib.sha256(
        json.dumps(
            result, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()
    ).hexdigest()
    return result


@transaction.atomic
def review_target(
    *, organization, actor, target, expected_version, expected_fingerprint, conclusion
):
    org = _org(organization, actor, APPROVERS)
    obj = _scoped(KpiTarget, target, org)
    _version(obj, expected_version)
    conclusion = _text(conclusion, label="Kesimpulan evaluasi", maximum=4000)
    if obj.status != "active" or obj.period_end >= timezone.localdate(timezone=JAKARTA):
        raise ValidationError(
            "Evaluasi resmi memerlukan target aktif dengan periode yang sudah berakhir."
        )
    result = evaluate_target(obj)
    if (
        not isinstance(expected_fingerprint, str)
        or not re.fullmatch(r"[a-f0-9]{64}", expected_fingerprint)
        or result["fingerprint"] != expected_fingerprint
    ):
        raise ValidationError(
            "Data sumber atau hasil KPI berubah sejak ditampilkan. Muat ulang dan periksa kembali sebelum menyimpan evaluasi."
        )
    if not result["official"]:
        raise ValidationError(
            "Evaluasi resmi belum dapat disimpan: hasil atau bukti metrik belum lengkap."
        )
    review = _save(
        KpiReview(
            organization=org,
            created_by=actor,
            target=obj,
            actual=Decimal(result["actual"]),
            snapshot=result,
            fingerprint=result["fingerprint"],
            conclusion=conclusion,
        )
    )
    obj.version += 1
    obj._kpi_transition = True
    _save(obj)
    _audit(
        org,
        actor,
        "kpi.reviewed",
        review,
        target_id=obj.pk,
        fingerprint=review.fingerprint,
        actual=str(review.actual),
        conclusion=conclusion,
        version=obj.version,
        retroactive=result["retroactive"],
    )
    return review
