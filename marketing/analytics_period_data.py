from itertools import chain

from django.db.models import F, Sum

from finance.fulfillment_models import CreditRefund, InvoiceCredit
from finance.models import (
    Allocation,
    CustomerRefund,
    CustomerTaxSettlement,
    Invoice,
    InvoiceCancellation,
)
from finance.settlement import applied_payment_amounts, collectible_amounts
from operations.attribution import invoice_attribution

from .analytics_metrics import ZERO, _bucket, _lead_group, _lead_value
from .analytics_period_types import (
    LeadFacts,
    PaymentFacts,
)
from .models import Lead
from .workflow_models import LeadContact


def _row(buckets, lead, group_by):
    key, label = _lead_group(lead, group_by)
    return buckets.rows.setdefault(key, _bucket(label))


def _lead_facts(scope):
    all_leads = list(
        Lead.objects.filter(organization=scope.organization).select_related(
            "campaign", "owner", "product", "invoice__product"
        )
    )
    attribution = invoice_attribution(organization=scope.organization)
    lead_index = {lead.pk: lead for lead in all_leads}
    linked = {
        invoice_id: lead_index[lead_id]
        for invoice_id, lead_id in attribution["by_invoice"].items()
        if lead_id in lead_index
    }
    invoice_index = {
        invoice.pk: invoice
        for invoice in Invoice.objects.filter(
            organization=scope.organization, pk__in=linked
        ).select_related("product")
    }
    for lead in all_leads:
        lead._attributed_invoice_ids = attribution["by_lead"].get(lead.pk, [])
        products = {
            invoice_index[pk].product_id: invoice_index[pk].product
            for pk in lead._attributed_invoice_ids
            if pk in invoice_index
        }
        lead._multiple_products = len(products) > 1
        if products:
            lead._attributed_product = (
                next(iter(products.values())) if len(products) == 1 else None
            )
    selected = [
        lead
        for lead in all_leads
        if all(
            _lead_value(lead, key) == value
            for key, value in scope.filters.items()
            if key != "ad"
        )
    ]
    selected_ids = {lead.pk for lead in selected}
    invoice_ids = [pk for lead in selected for pk in lead._attributed_invoice_ids]
    return LeadFacts(
        attribution_fingerprint=attribution["fingerprint"],
        linked=linked,
        invoice_index=invoice_index,
        selected=selected,
        selected_ids=selected_ids,
        invoice_ids=invoice_ids,
    )


def _net_cohort_invoices(scope, lead_facts):
    cohort_invoice_ids = sorted(
        {
            pk
            for lead in lead_facts.selected
            if scope.beginning <= lead.received_at < scope.cutoff
            for pk in lead._attributed_invoice_ids
            if pk in lead_facts.invoice_index
            and lead_facts.invoice_index[pk].date <= scope.observation_end
        }
    )
    cohort_invoices = {pk: lead_facts.invoice_index[pk] for pk in cohort_invoice_ids}
    cancellations = set(
        InvoiceCancellation.objects.filter(
            organization=scope.organization,
            invoice_id__in=cohort_invoice_ids,
            date__lte=scope.observation_end,
        ).values_list("invoice_id", flat=True)
    )
    future_cancellations = set(
        InvoiceCancellation.objects.filter(
            organization=scope.organization,
            invoice_id__in=cohort_invoice_ids,
            date__gt=scope.observation_end,
        ).values_list("invoice_id", flat=True)
    )
    credits = dict(
        InvoiceCredit.objects.filter(
            organization=scope.organization,
            invoice_id__in=cohort_invoice_ids,
            date__lte=scope.observation_end,
        )
        .values("invoice_id")
        .annotate(total=Sum("amount"))
        .values_list("invoice_id", "total")
    )
    net_invoices = {}
    for invoice_id, invoice in cohort_invoices.items():
        if (
            invoice.status not in {"issued", "delivered", "cancelled"}
            or invoice_id in cancellations
        ):
            continue
        if invoice.status == "cancelled" and invoice_id not in future_cancellations:
            continue
        net_total = invoice.total - credits.get(invoice_id, ZERO)
        if net_total > ZERO:
            net_invoices[invoice_id] = net_total
    return net_invoices


def _mark_fully_paid(scope, lead_facts, net_invoices, applied_by_invoice):
    for lead in lead_facts.selected:
        if not scope.beginning <= lead.received_at < scope.cutoff:
            lead._cohort_fully_paid = False
            continue
        retained = [pk for pk in lead._attributed_invoice_ids if pk in net_invoices]
        lead._cohort_fully_paid = bool(retained) and all(
            applied_by_invoice.get(pk, ZERO) >= net_invoices[pk] for pk in retained
        )


def _bank_payments_by_invoice(scope, invoice_ids):
    paid_by_invoice = dict(
        Allocation.objects.filter(
            organization=scope.organization,
            invoice__organization=scope.organization,
            transaction__organization=scope.organization,
            invoice_id__in=invoice_ids,
            transaction__date__lte=scope.observation_end,
        )
        .values("invoice_id")
        .annotate(total=Sum("amount"))
        .values_list("invoice_id", "total")
    )
    for invoice_id, cash in (
        CustomerTaxSettlement.objects.filter(
            organization=scope.organization,
            invoice__organization=scope.organization,
            bank_posting__transaction__organization=scope.organization,
            invoice_id__in=invoice_ids,
            cash_amount__gt=0,
            date__lte=scope.observation_end,
        )
        .values("invoice_id")
        .annotate(total=Sum("cash_amount"))
        .values_list("invoice_id", "total")
    ):
        paid_by_invoice[invoice_id] = paid_by_invoice.get(invoice_id, ZERO) + cash
    return paid_by_invoice


def _payment_facts(scope, lead_facts):
    applied_by_invoice = applied_payment_amounts(
        organization=scope.organization,
        invoice_ids=lead_facts.invoice_ids,
        as_of=scope.observation_end,
    )
    net_invoices = _net_cohort_invoices(scope, lead_facts)
    _mark_fully_paid(scope, lead_facts, net_invoices, applied_by_invoice)
    paid_by_invoice = _bank_payments_by_invoice(scope, lead_facts.invoice_ids)
    collectible = collectible_amounts(
        organization=scope.organization,
        invoice_ids=lead_facts.invoice_ids,
        as_of=scope.observation_end,
    )
    return PaymentFacts(
        applied_by_invoice=applied_by_invoice,
        paid_by_invoice=paid_by_invoice,
        collectible=collectible,
    )


def _followup_events(scope, lead_facts):
    promises = list(
        LeadContact.objects.filter(
            organization=scope.organization,
            lead_id__in=lead_facts.selected_ids,
            followup_action="set",
            next_due_at__gte=scope.beginning,
            next_due_at__lt=scope.cutoff,
            created_at__lt=scope.cutoff,
        ).select_related("lead")
    )
    promise_ids = [promise.pk for promise in promises]
    completion_events = (
        list(
            LeadContact.objects.filter(
                organization=scope.organization,
                lead_id__in=lead_facts.selected_ids,
                completes_id__in=promise_ids,
                created_at__lt=scope.cutoff,
            ).order_by("pk")
        )
        if promise_ids
        else []
    )
    completions_by_promise = {event.completes_id: event for event in completion_events}
    correction_children = {}
    frontier = [event.pk for event in completion_events]
    while frontier:
        children = list(
            LeadContact.objects.filter(
                organization=scope.organization,
                lead_id__in=lead_facts.selected_ids,
                corrects_id__in=frontier,
                created_at__lt=scope.cutoff,
            ).order_by("pk")
        )
        frontier = []
        for child in children:
            correction_children.setdefault(child.corrects_id, []).append(child)
            frontier.append(child.pk)
    return promises, completions_by_promise, correction_children


def _latest_completion(event, correction_children):
    latest = event
    visited = {latest.pk}
    while correction_children.get(latest.pk):
        latest = correction_children[latest.pk][0]
        if latest.pk in visited:
            break
        visited.add(latest.pk)
    return latest


def _apply_followup_metrics(
    scope, promises, completions_by_promise, correction_children, buckets
):
    for promise in promises:
        due = promise.next_due_at
        completion = completions_by_promise.get(promise.pk)
        timely = False
        if completion is not None:
            latest = _latest_completion(completion, correction_children)
            timely = (
                promise.created_at <= due
                and promise.occurred_at <= due
                and latest.created_at <= due
                and promise.occurred_at <= latest.occurred_at <= due
            )
        for bucket in (buckets.summary, _row(buckets, promise.lead, scope.group_by)):
            bucket["followups_due"] += 1
            if timely:
                bucket["followups_on_time"] += 1


def _add_followup_metrics(scope, lead_facts, buckets):
    if not scope.cash_available:
        return
    promises, completions, correction_children = _followup_events(scope, lead_facts)
    _apply_followup_metrics(scope, promises, completions, correction_children, buckets)


def _add_lead_work_metrics(scope, lead_facts, collectible, buckets):
    if not scope.cash_available:
        return
    for lead in lead_facts.selected:
        if scope.beginning <= lead.received_at < scope.cutoff:
            buckets.summary["cohort"].append(lead)
            item = _row(buckets, lead, scope.group_by)
            item["cohort"].append(lead)
            item["contexts"].add(
                f"{lead.channel or 'Kanal belum diketahui'} · {lead.segment}"
            )
        unpaid = not lead._attributed_invoice_ids or any(
            collectible.get(pk, ZERO) > ZERO for pk in lead._attributed_invoice_ids
        )
        if (
            scope.start <= scope.today <= scope.end
            and lead.received_at < scope.cutoff
            and lead.next_follow_up
            and lead.next_follow_up < scope.today
            and lead.stage != "lost"
            and unpaid
        ):
            buckets.summary["overdue"] += 1
            _row(buckets, lead, scope.group_by)["overdue"] += 1
        if (
            scope.start <= scope.today <= scope.end
            and lead.received_at < scope.cutoff
            and lead.stage == "won"
            and lead._attributed_invoice_ids
            and unpaid
        ):
            outstanding = sum(
                (collectible.get(pk, ZERO) for pk in lead._attributed_invoice_ids),
                ZERO,
            )
            for bucket in (buckets.summary, _row(buckets, lead, scope.group_by)):
                bucket["unpaid_won"] += 1
                bucket["unpaid_won_amount"] += outstanding


def _add_receipt_facts(scope, lead_facts, buckets):
    receipts = Allocation.objects.filter(
        organization=scope.organization,
        invoice__organization=scope.organization,
        transaction__organization=scope.organization,
        transaction__date__range=(scope.start, scope.observation_end),
    )
    tax_receipts = CustomerTaxSettlement.objects.filter(
        organization=scope.organization,
        invoice__organization=scope.organization,
        bank_posting__transaction__organization=scope.organization,
        cash_amount__gt=0,
        date__range=(scope.start, scope.observation_end),
    )
    for allocation in chain(
        receipts.values("invoice_id", "amount"),
        tax_receipts.values("invoice_id", amount=F("cash_amount")),
    ):
        buckets.total_cash += allocation["amount"]
        lead = lead_facts.linked.get(allocation["invoice_id"])
        if lead is not None:
            buckets.attributed_cash += allocation["amount"]
        if (
            scope.cash_available
            and lead is not None
            and lead.pk in lead_facts.selected_ids
        ):
            item = _row(buckets, lead, scope.group_by)
            for bucket in (buckets.summary, item):
                bucket["cash"] += allocation["amount"]
                bucket["cash_campaign_ids"].add(lead.campaign_id)
            effective_product = _lead_value(lead, "product")
            mapping_complete = bool(
                effective_product
                and lead.campaign_id
                and lead.campaign.product_id == effective_product
            )
            if not mapping_complete:
                if "product" in scope.filters:
                    buckets.summary["product_mapping_complete"] = False
                if scope.group_by == "product" or "product" in scope.filters:
                    item["product_mapping_complete"] = False


def _add_invoice_refund_facts(scope, lead_facts, buckets):
    cancellation_refunds = CustomerRefund.objects.filter(
        organization=scope.organization,
        advance__isnull=True,
        cancellation__isnull=False,
        cancellation__invoice__organization=scope.organization,
        bank_posting__organization=scope.organization,
        bank_posting__kind="customer_refund",
        bank_posting__transaction__organization=scope.organization,
        bank_posting__transaction__date__range=(scope.start, scope.observation_end),
    ).values("amount", invoice_id=F("cancellation__invoice_id"))
    credit_refunds = CreditRefund.objects.filter(
        organization=scope.organization,
        credit__organization=scope.organization,
        credit__invoice__organization=scope.organization,
        bank_posting__organization=scope.organization,
        bank_posting__kind="customer_refund",
        bank_posting__transaction__organization=scope.organization,
        bank_posting__transaction__date__range=(scope.start, scope.observation_end),
    ).values("amount", invoice_id=F("credit__invoice_id"))
    for refund in chain(cancellation_refunds, credit_refunds):
        amount = refund["amount"]
        buckets.total_invoice_refunds += amount
        lead = lead_facts.linked.get(refund["invoice_id"])
        if lead is not None:
            buckets.attributed_invoice_refunds += amount
        if (
            scope.cash_available
            and lead is not None
            and lead.pk in lead_facts.selected_ids
        ):
            item = _row(buckets, lead, scope.group_by)
            for bucket in (buckets.summary, item):
                bucket["refunds"] += amount
                bucket["cash_campaign_ids"].add(lead.campaign_id)
