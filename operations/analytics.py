"""Business units for management: orders, participants and retained commercial value.

Counts use the order-date cohort. Cash uses bank dates. They are deliberately
separate and must not be added together or described as causal advertising ROI.
"""

import hashlib
import json
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from core.models import Organization
from finance.costing import cost_facts
from finance.fulfillment import fulfillment_summary
from finance.fulfillment_models import CreditRefund, InvoiceCredit, InvoiceDelivery
from finance.models import (
    Allocation,
    CustomerRefund,
    CustomerTaxSettlement,
    InvoiceCancellation,
)
from finance.services import _date
from finance.settlement import applied_payment_amounts

from .models import Order, OrderLine, Registration

ZERO = Decimal("0.00")


@transaction.atomic
def commercial_snapshot(*, organization, start, end):
    Organization.objects.select_for_update().get(pk=organization.pk)
    start, end = _date(start), _date(end)
    if start > end or (end - start).days > 731:
        raise ValidationError(
            "Periode ringkasan tidak valid atau lebih dari dua tahun."
        )
    cutoff = min(end, timezone.localdate())
    orders = list(
        Order.objects.filter(
            organization=organization, date__lte=cutoff
        ).select_related("buyer")
    )
    lines = list(
        OrderLine.objects.filter(
            organization=organization,
            order__organization=organization,
            order__date__lte=cutoff,
        ).select_related("invoice", "product")
    )
    by_order = {}
    for line in lines:
        by_order.setdefault(line.order_id, []).append(line)
    invoices = {
        line.invoice_id: line.invoice
        for line in lines
        if line.invoice_id and line.invoice.date <= cutoff
    }
    paid = applied_payment_amounts(
        organization=organization, invoice_ids=list(invoices), as_of=cutoff
    )
    cancellations = dict(
        InvoiceCancellation.objects.filter(
            organization=organization, invoice_id__in=invoices, date__lte=cutoff
        ).values_list("invoice_id", "amount")
    )
    facts = {
        pk: fulfillment_summary(
            organization=organization, invoice=invoice, as_of=cutoff
        )
        for pk, invoice in invoices.items()
    }
    full_paid, cancelled = set(), set()
    for pk, invoice in invoices.items():
        future_cancellation = (
            invoice.status == "cancelled"
            and InvoiceCancellation.objects.filter(
                organization=organization, invoice=invoice, date__gt=cutoff
            ).exists()
        )
        if (
            pk in cancellations
            or invoice.status == "cancelled"
            and not future_cancellation
        ):
            cancelled.add(pk)
        elif (
            (invoice.status in ("issued", "delivered") or future_cancellation)
            and facts[pk]["net_total"] > 0
            and paid.get(pk, ZERO) >= facts[pk]["net_total"]
        ):
            full_paid.add(pk)
    paid_orders = []
    for order in orders:
        current = [line for line in by_order.get(order.pk, []) if line.quantity > 0]
        retained = [
            line
            for line in current
            if line.invoice_id not in facts or facts[line.invoice_id]["net_total"] > 0
        ]
        if retained and all(line.invoice_id in full_paid for line in retained):
            paid_orders.append(order)
    cohort = [order for order in orders if start <= order.date <= cutoff]
    cohort_ids = {order.pk for order in cohort}
    cohort_lines = [line for line in lines if line.order_id in cohort_ids]
    cohort_invoice_ids = {
        line.invoice_id for line in cohort_lines if line.invoice_id in invoices
    }
    cohort_paid = [order for order in paid_orders if order.pk in cohort_ids]
    paid_partner_ids = {
        order.buyer_id for order in cohort_paid if order.buyer.kind == "reseller"
    }
    repeats = {
        pk
        for pk in paid_partner_ids
        if sum(order.buyer_id == pk for order in paid_orders) >= 2
    }
    previously_paid = {order.buyer_id for order in paid_orders if order.date < start}
    registrations = Registration.objects.filter(
        organization=organization, line__order_id__in=cohort_ids
    )
    # A completion has its own dated FinancialEvent; current roster state alone
    # cannot prove historic fulfillment.
    completed = registrations.filter(delivery__date__lte=cutoff).count()
    costs = {
        pk: cost_facts(organization=organization, invoice=invoices[pk], as_of=cutoff)
        for pk in cohort_invoice_ids
    }
    recognized = sum(
        (
            facts[pk]["recognized_amount"]
            for pk in cohort_invoice_ids
            if pk not in cancelled
        ),
        ZERO,
    )
    delivered_ids = {
        pk for pk in cohort_invoice_ids if facts[pk]["delivered_quantity"] > 0
    }
    cost_complete = bool(delivered_ids) and all(
        costs[pk]["complete"] for pk in delivered_ids
    )
    actual_cost = (
        sum((costs[pk]["actual_cost"] for pk in delivered_ids), ZERO)
        if cost_complete
        else None
    )
    receipts = Allocation.objects.filter(
        organization=organization,
        invoice_id__in=invoices,
        transaction__organization=organization,
        transaction__date__range=(start, cutoff),
    )
    refunded = CreditRefund.objects.filter(
        organization=organization,
        credit__invoice_id__in=invoices,
        date__range=(start, cutoff),
    )
    cancelled_refunds = CustomerRefund.objects.filter(
        organization=organization,
        cancellation__invoice_id__in=invoices,
        date__range=(start, cutoff),
    )
    cash = receipts.aggregate(value=Sum("amount"))["value"] or ZERO
    tax_receipts = CustomerTaxSettlement.objects.filter(
        organization=organization,
        invoice_id__in=invoices,
        bank_posting__transaction__organization=organization,
        cash_amount__gt=0,
        date__range=(start, cutoff),
    )
    cash += tax_receipts.aggregate(value=Sum("cash_amount"))["value"] or ZERO
    refunds = (refunded.aggregate(value=Sum("amount"))["value"] or ZERO) + (
        cancelled_refunds.aggregate(value=Sum("amount"))["value"] or ZERO
    )
    products = []
    for product_id in sorted({line.product_id for line in cohort_lines}):
        subset = [line for line in cohort_lines if line.product_id == product_id]
        products.append(
            {
                "product_id": product_id,
                "kind": subset[0].product.kind,
                "current_order_units": sum(line.quantity for line in subset),
                "fully_paid_units": sum(
                    max(
                        0,
                        line.quantity
                        - facts[line.invoice_id]["cancelled_quantity"]
                        - facts[line.invoice_id]["service_credit_quantity"],
                    )
                    for line in subset
                    if line.invoice_id in full_paid
                ),
            }
        )
    result = {
        "period": {
            "start": str(start),
            "end": str(end),
            "observed_through": str(cutoff),
        },
        "cohort_orders": len(cohort),
        "cohort_fully_paid_orders": len(cohort_paid),
        "cohort_direct_paid_orders": sum(
            order.buyer.kind == "customer" for order in cohort_paid
        ),
        "cohort_partner_paid_orders": sum(
            order.buyer.kind == "reseller" for order in cohort_paid
        ),
        "recorded_participants": registrations.count(),
        "completed_participants": completed,
        "active_paid_partners": len(paid_partner_ids),
        "repeat_paid_partners": len(repeats),
        "partners_without_earlier_paid_order": len(paid_partner_ids - previously_paid),
        "cohort_net_recognized_revenue": str(recognized),
        "cost_complete": cost_complete,
        "cohort_actual_cost": str(actual_cost) if actual_cost is not None else None,
        "cohort_actual_margin": str(recognized - actual_cost)
        if actual_cost is not None
        else None,
        "cost_pending_invoices": sum(not costs[pk]["complete"] for pk in delivered_ids),
        "period_gross_bank_receipts": str(cash),
        "period_linked_refunds": str(refunds),
        "period_bank_receipts_less_linked_refunds": str(cash - refunds),
        "products": products,
        "limits": [
            "Pesanan mengikuti tanggal pesanan; pembayaran sebagian tidak berarti lunas.",
            "Peserta adalah roster yang tercatat saat laporan dibuka; penyelesaian memakai tanggal bukti layanan.",
            "Jumlah unit produk mengikuti jumlah pesanan saat ini; pembatalan sebelum penagihan dapat mengubah jumlah tersebut.",
            "Mitra tanpa pesanan lunas terdahulu dibandingkan berdasarkan tanggal pesanan dan keadaan lunas pada akhir pengamatan; bukan tanggal pertama kali membayar.",
            "Kas mencakup alokasi mutasi biasa dan uang masuk bersih setelah potongan pelanggan, dikurangi refund tertaut; aset kredit pajak dan uang muka belum teratribusi tidak termasuk.",
            "Margin merupakan pendapatan layanan neto kelompok pesanan dikurangi biaya provider unit selesai yang telah dialokasikan; belum dikurangi biaya marketing dan overhead. Bukan laba bersih perusahaan.",
            "Sumber eksternal dan kelengkapan periode tetap memerlukan pengesahan tersendiri.",
        ],
    }
    manifest = {
        "version": "operations-commercial-sources-v2",
        "orders": [(o.pk, o.version, o.lead_id, str(o.date)) for o in orders],
        "lines": [
            (line.pk, line.order_id, line.product_id, line.quantity, line.invoice_id)
            for line in lines
        ],
        "invoices": [
            (pk, inv.status, str(inv.total), str(paid.get(pk, ZERO)))
            for pk, inv in invoices.items()
        ],
        "costs": {pk: value["fingerprint"] for pk, value in costs.items()},
        "deliveries": list(
            InvoiceDelivery.objects.filter(
                organization=organization, invoice_id__in=invoices, date__lte=cutoff
            ).values_list("pk", "invoice_id", "quantity", "amount", "date")
        ),
        "credits": list(
            InvoiceCredit.objects.filter(
                organization=organization, invoice_id__in=invoices, date__lte=cutoff
            ).values_list("pk", "invoice_id", "quantity", "amount", "date")
        ),
        "tax_receipts": list(
            CustomerTaxSettlement.objects.filter(
                organization=organization, invoice_id__in=invoices, date__lte=cutoff
            ).values_list(
                "pk",
                "invoice_id",
                "cash_amount",
                "tax_amount",
                "gross_amount",
                "date",
                "verification_id",
                "bank_posting_id",
                "source_allocation_id",
                "source_advance_application_id",
            )
        ),
        "result": result,
    }
    result["fingerprint"] = hashlib.sha256(
        json.dumps(
            manifest, sort_keys=True, default=str, separators=(",", ":")
        ).encode()
    ).hexdigest()
    return result
