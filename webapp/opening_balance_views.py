"""Simple opening-balance screens; Finance owns accounting and source proofs."""

from datetime import date
from decimal import Decimal
from uuid import uuid4

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path, reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods

from core.access import organization_required, require_role
from finance import opening_balances as workflow
from finance.models import OpeningBalanceItem, OpeningBalanceSettlement, Party
from taxes.opening_balance_review import opening_settlement_review_context
from taxes.opening_refund_review import opening_refund_review_context

from .opening_balance_forms import (
    OpeningBalanceCancelForm,
    OpeningBalanceCorrectionForm,
    OpeningBalanceMatchForm,
    OpeningBalanceRecordForm,
    OpeningBalanceRefundForm,
)

READ = ("owner", "finance", "reviewer", "auditor", "director")
WRITE = ("owner", "finance")
KIND_LABELS = {
    "receivable": "Piutang pelanggan / mitra lama",
    "payable": "Utang pemasok lama",
}
TAX_LABELS = {
    "pending": "Pemeriksaan pajak masih diperlukan",
    "reviewed": "Pemeriksaan pajak sudah tercatat",
    "stale": "Pemeriksaan pajak perlu diperiksa kembali",
    "held": "Pemeriksaan pajak perlu ditindaklanjuti",
}


def _item(request, pk):
    return get_object_or_404(
        OpeningBalanceItem, organization=request.organization, pk=pk
    )


def _date(value):
    try:
        return date.fromisoformat(value) if isinstance(value, str) else value
    except ValueError:
        return None


def _as_of(request):
    raw = request.GET.get("as_of", "")
    if len(request.GET.getlist("as_of")) > 1:
        raise ValidationError("Pilih satu tanggal laporan.")
    if not raw:
        return timezone.localdate()
    if not raw.isascii() or len(raw) != 10 or raw[4] != "-" or raw[7] != "-":
        raise ValidationError("Tanggal laporan harus ditulis YYYY-MM-DD.")
    try:
        selected = date.fromisoformat(raw)
    except ValueError:
        raise ValidationError("Tanggal laporan tidak sah.") from None
    if selected.year < 2000 or selected > timezone.localdate():
        raise ValidationError("Tanggal laporan harus antara tahun 2000 dan hari ini.")
    return selected


def _position(request, item, *, as_of):
    try:
        result = dict(
            workflow.opening_balance_position(
                organization=request.organization, item=item, as_of=as_of
            )
        )
    except ValidationError as error:
        result = {
            "current": False,
            "issues": error.messages,
            "blockers": error.messages,
            "opening": None,
            "settled": None,
            "remaining": None,
            "history": [],
        }
    result["has_remaining"] = bool(
        result.get("current")
        and result.get("remaining") is not None
        and Decimal(result["remaining"]) > 0
    )
    result["exhausted"] = bool(
        result.get("current")
        and result.get("remaining") is not None
        and Decimal(result["remaining"]) == 0
        and item.date <= as_of
    )
    result["not_started"] = bool(result.get("current") and as_of < item.date)
    history = []
    if result.get("current") and item.date <= as_of:
        history.append(
            {
                "date": item.date,
                "amount": item.amount,
                "label": "Saldo lama dicatat",
                "actor_name": item.actor.get_full_name() or item.actor.get_username(),
                "reference": item.source_reference,
                "reason": item.reason,
                "evidence": item.evidence,
            }
        )
        for event in result.get("history", []):
            correction = event.action == "correct"
            cancellation = event.action == "cancel"
            if correction:
                label, reference = (
                    (
                        "Jumlah ditambah"
                        if event.direction == "increase"
                        else "Jumlah dikurangi"
                    ),
                    item.source_reference,
                )
            elif cancellation:
                label, reference = (
                    "Pencocokan dibatalkan",
                    event.settlement.bank_posting.transaction.reference,
                )
            elif event.action == "refund":
                label, reference = (
                    "Pengembalian uang dicocokkan",
                    event.bank_posting.transaction.reference,
                )
            else:
                label, reference = (
                    "Pembayaran dicocokkan",
                    event.bank_posting.transaction.reference,
                )
            history.append(
                {
                    "date": _date(event.date),
                    "amount": event.amount,
                    "label": label,
                    "actor_name": event.actor.get_full_name()
                    or event.actor.get_username(),
                    "reference": reference,
                    "recorded_at": event.created_at
                    if cancellation or event.action == "refund"
                    else None,
                    "reason": event.reason,
                    "evidence": event.evidence,
                }
            )
    result["history"] = history
    return result


def _common(request, **values):
    return {
        "active_nav": "bank",
        "can_record": request.membership.role == "owner",
        "can_correct": request.membership.role == "owner",
        "can_cancel": request.membership.role == "owner",
        "can_refund": request.membership.role in WRITE,
        "can_match": request.membership.role in WRITE,
        **values,
    }


def _party(request, item, position):
    if not position.get("current"):
        return None
    return Party.objects.filter(
        organization=request.organization, pk=item.party_id
    ).first()


def _refund_facts(organization, settlement):
    bank = settlement.bank_posting.transaction
    returns = workflow.opening_cash_refund_facts(
        organization=organization, transaction=bank
    )
    cancellations = workflow.opening_cash_cancellation_facts(
        organization=organization, transaction=bank
    )
    refunded = sum(
        (
            Decimal(entry["amount"])
            for entry in returns["refunds"]
            if entry["settlement_id"] == settlement.pk
        ),
        Decimal(0),
    )
    cancelled = any(
        entry["settlement_id"] == settlement.pk for entry in cancellations["releases"]
    )
    return {
        "refunded": refunded,
        "refundable_remaining": settlement.amount - refunded,
        "cancelled": cancelled,
    }


def _original_payment(request, settlement):
    try:
        workflow.opening_settlement_source_snapshot(
            organization=request.organization, settlement=settlement
        )
        facts = _refund_facts(request.organization, settlement)
    except ValidationError:
        return None
    return {
        "settlement": settlement,
        "transaction": settlement.bank_posting.transaction,
        **facts,
    }


@require_GET
@organization_required
def opening_balances(request):
    require_role(request, *READ)
    try:
        as_of = _as_of(request)
    except ValidationError as error:
        return HttpResponseBadRequest(" ".join(error.messages))
    kind = request.GET.get("kind", "")
    rows = OpeningBalanceItem.objects.filter(
        organization=request.organization
    ).order_by("-date", "-pk")
    if kind in KIND_LABELS:
        rows = rows.filter(kind=kind)
    else:
        kind = ""
    page = Paginator(rows, 50).get_page(request.GET.get("page"))
    items = []
    for item in page.object_list:
        position = _position(request, item, as_of=as_of)
        items.append(
            {
                "item": item,
                "position": position,
                "party": _party(request, item, position),
                "kind_label": KIND_LABELS.get(item.kind, "Jenis perlu diperiksa"),
            }
        )
    return render(
        request,
        "app/opening_balance_list.html",
        _common(
            request,
            items=items,
            page_obj=page,
            selected_kind=kind,
            kind_label=KIND_LABELS.get(kind, ""),
            as_of=as_of,
            is_today=as_of == timezone.localdate(),
        ),
    )


@require_GET
@organization_required
def opening_balance_detail(request, pk):
    require_role(request, *READ)
    item = _item(request, pk)
    try:
        as_of = _as_of(request)
    except ValidationError as error:
        return HttpResponseBadRequest(" ".join(error.messages))
    position = _position(request, item, as_of=as_of)
    settlements = []
    sources = (
        OpeningBalanceSettlement.objects.filter(
            organization=request.organization,
            item=item,
            item__organization=request.organization,
            bank_posting__organization=request.organization,
            bank_posting__transaction__organization=request.organization,
            bank_posting__transaction__account__organization=request.organization,
            journal__organization=request.organization,
            date__lte=as_of,
        )
        .select_related("bank_posting__transaction__account", "actor")
        .order_by("date", "pk")
    )
    for settlement in sources:
        try:
            review = opening_settlement_review_context(
                organization=request.organization, settlement=settlement
            )
        except ValidationError as error:
            review = {"status": "held", "blockers": error.messages}
        cancellation = (
            next(
                (
                    event
                    for event in position.get("cancellations", [])
                    if event.settlement_id == settlement.pk
                ),
                None,
            )
            if position.get("current")
            else None
        )
        refundable, refunded, refundable_source = None, None, False
        if position.get("current"):
            try:
                facts = _refund_facts(request.organization, settlement)
            except ValidationError as error:
                position.update(
                    current=False,
                    integrity=False,
                    issues=error.messages,
                    blockers=error.messages,
                    opening=None,
                    settled=None,
                    remaining=None,
                    history=[],
                    has_remaining=False,
                    exhausted=False,
                    not_started=False,
                )
            else:
                refundable = facts["refundable_remaining"]
                refunded = sum(
                    (
                        event.amount
                        for event in position.get("refunds", [])
                        if event.settlement_id == settlement.pk
                    ),
                    Decimal(0),
                )
                refundable_source = not facts["cancelled"] and refundable > 0
        settlements.append(
            {
                "settlement": settlement,
                "bank": settlement.bank_posting.transaction,
                "cancellation": cancellation,
                "refunded": refunded,
                "has_refunds": refunded is not None and refunded > 0,
                "refundable_remaining": refundable,
                "can_refund": refundable_source,
                "tax_label": TAX_LABELS.get(review.get("status"), TAX_LABELS["held"]),
                "tax_status": review.get("status", "held"),
                "tax_blockers": review.get("blockers", []),
                "can_assess": bool(position.get("current")),
            }
        )
    refunds = []
    for refund in position.get("refunds", []) if position.get("current") else []:
        try:
            review = opening_refund_review_context(
                organization=request.organization, refund=refund
            )
        except ValidationError as error:
            review = {"status": "held", "blockers": error.messages}
        refunds.append(
            {
                "refund": refund,
                "bank": refund.bank_posting.transaction,
                "original_bank": refund.settlement.bank_posting.transaction,
                "tax_label": TAX_LABELS.get(review.get("status"), TAX_LABELS["held"]),
                "tax_status": review.get("status", "held"),
                "tax_blockers": review.get("blockers", []),
                "can_assess": bool(position.get("current")),
            }
        )
    return render(
        request,
        "app/opening_balance_detail.html",
        _common(
            request,
            item=item,
            position=position,
            party=_party(request, item, position),
            settlements=settlements,
            refunds=refunds,
            kind_label=KIND_LABELS.get(item.kind, "Jenis perlu diperiksa"),
            as_of=as_of,
            is_today=as_of == timezone.localdate(),
        ),
    )


def _errors(form, error):
    if hasattr(error, "message_dict"):
        for name, errors in error.message_dict.items():
            for message in errors:
                form.add_error(name if name in form.fields else None, message)
    else:
        for message in error.messages:
            form.add_error(None, message)


def _context(request, action, item, values, *, settlement=None):
    sources = {
        key: values[key]
        for key in (
            "kind",
            "party",
            "date",
            "source_date",
            "source_reference",
            "amount",
            "transaction",
            "direction",
        )
        if key in values
    }
    if settlement is not None:
        sources["settlement"] = settlement
    elif item is not None:
        sources["item"] = item
    return workflow.opening_balance_action_context(
        organization=request.organization, action=action, **sources
    )


def _common_date(organization):
    dates = list(
        OpeningBalanceItem.objects.filter(organization=organization)
        .order_by()
        .values_list("date", flat=True)
        .distinct()[:2]
    )
    return dates[0] if len(dates) == 1 else None


@require_http_methods(["GET", "POST"])
@organization_required
def opening_balance_action(request, action, pk=None):
    require_role(
        request, "owner", *(("finance",) if action in ("settle", "refund") else ())
    )
    settlement = (
        get_object_or_404(
            OpeningBalanceSettlement,
            organization=request.organization,
            item__organization=request.organization,
            pk=pk,
        )
        if action in ("cancel", "refund")
        else None
    )
    item = (
        _item(request, settlement.item_id)
        if settlement
        else _item(request, pk)
        if action in ("settle", "correct")
        else None
    )
    intent = request.POST.get("intent", "") if request.method == "POST" else ""
    checking = intent == "check"
    data = request.POST.copy() if request.method == "POST" else None
    if checking:
        for field in ("request_key", "expected_fingerprint", "confirmed"):
            if len(data.getlist(field)) <= 1:
                data.pop(field, None)
    kind = (
        item.kind
        if item
        else request.POST.get("kind", "receivable")
        if data is not None
        else request.GET.get("kind", "receivable")
    )
    if kind not in KIND_LABELS:
        kind = "receivable"
    common_date = _common_date(request.organization) if action == "record" else None
    form_class = {
        "record": OpeningBalanceRecordForm,
        "settle": OpeningBalanceMatchForm,
        "correct": OpeningBalanceCorrectionForm,
        "cancel": OpeningBalanceCancelForm,
        "refund": OpeningBalanceRefundForm,
    }[action]
    kwargs = {"organization": request.organization, "checking": checking}
    if action == "record":
        kwargs.update(kind=kind, common_date=common_date)
    elif action != "cancel":
        kwargs["item"] = item
    form = form_class(data, **kwargs)
    valid = form.is_valid() if request.method == "POST" else False
    if request.method == "POST" and intent not in ("check", "save"):
        form.add_error(None, "Pilih Periksa sumber atau Konfirmasi dan simpan.")
        valid = False
    values = dict(form.cleaned_data) if form.is_bound else {}
    context = _context(request, action, item, values, settlement=settlement)
    if action == "refund" and form.bank_source_errors:
        context.update(
            current=False,
            integrity=False,
            blockers=form.bank_source_errors,
            opening=None,
            settled=None,
            remaining=None,
            before=None,
            after=None,
            bank_remaining=None,
            bank_after=None,
        )
    if request.method == "POST" and intent == "save" and valid:
        command_values = dict(form.cleaned_data)
        if settlement:
            command_values["settlement"] = settlement
        elif item:
            command_values["item"] = item
        command = getattr(
            workflow,
            {
                "record": "record_opening_balance",
                "settle": "settle_opening_balance",
                "correct": "correct_opening_balance",
                "cancel": "cancel_opening_settlement",
                "refund": "refund_opening_settlement",
            }[action],
        )
        try:
            result = command(
                organization=request.organization, actor=request.user, **command_values
            )
        except ValidationError as error:
            _errors(form, error)
            form.add_error(
                None,
                "Isian Anda tetap tersimpan di formulir. Tekan Periksa sumber, lalu periksa dan konfirmasi lagi.",
            )
        else:
            messages.success(
                request,
                {
                    "record": "Saldo lama tersimpan.",
                    "settle": "Pembayaran saldo lama sudah dicocokkan.",
                    "correct": "Perbaikan jumlah saldo lama sudah tercatat. Dokumen asli tetap tersimpan.",
                    "cancel": "Pencocokan dibatalkan. Mutasi bank asli tetap ada dan perlu dicocokkan kembali.",
                    "refund": "Pengembalian uang sudah dicocokkan. Periksa tugas pajak pengembalian uang secara terpisah.",
                }[action],
            )
            return redirect(
                "opening_balance_detail",
                pk=result.pk if action == "record" else item.pk,
            )
    if checking and valid:
        if (
            action == "settle"
            and values.get("amount") is None
            and context.get("current")
        ):
            remaining, bank_remaining = (
                context.get("remaining"),
                context.get("bank_remaining"),
            )
            if remaining is not None and bank_remaining is not None:
                suggested = min(Decimal(remaining), abs(Decimal(bank_remaining)))
                if suggested > 0:
                    values["amount"] = suggested
                    context = _context(request, action, item, values)
        fresh_data = form.data.copy()
        fresh_data["request_key"] = str(uuid4())
        fresh_data["expected_fingerprint"] = context["fingerprint"]
        fresh_data.pop("confirmed", None)
        if values.get("amount") is not None:
            fresh_data["amount"] = str(values["amount"])
        form = form_class(fresh_data, **kwargs)
        form.is_valid()
        messages.info(
            request,
            "Sumber terbaru diperiksa. Periksa ringkasan lalu konfirmasi kembali.",
        )
    save_available = bool(
        checking
        and valid
        and not form.errors
        and context.get("current")
        and not context.get("blockers")
    )
    request_error = request.method == "POST" and (
        any(len(request.POST.getlist(name)) > 1 for name in (*form.fields, "intent"))
        or bool(
            set(request.POST) - set(form.fields) - {"intent", "csrfmiddlewaretoken"}
        )
    )
    cancellation = (
        next(
            (
                event
                for event in context.get("cancellations", [])
                if settlement and event.settlement_id == settlement.pk
            ),
            None,
        )
        if context.get("current")
        else None
    )
    return render(
        request,
        "app/opening_balance_form.html",
        _common(
            request,
            form=form,
            action=action,
            item=item,
            settlement=settlement,
            kind=kind,
            kind_label=KIND_LABELS[kind],
            common_date=common_date,
            title={
                "record": "Catat saldo lama",
                "settle": "Cocokkan pembayaran saldo lama",
                "correct": "Perbaiki jumlah saldo lama",
                "cancel": "Batalkan pencocokan",
                "refund": "Cocokkan pengembalian uang",
            }[action],
            source_context=context,
            checked=checking and valid,
            save_available=save_available,
            show_summary=bool(
                context.get("current") and (checking and valid or action == "cancel")
            ),
            cancellation=cancellation,
            original_payment=_original_payment(request, settlement)
            if action == "refund"
            else None,
            cancel_url=reverse("opening_balance_detail", args=[item.pk])
            if item
            else reverse("opening_balances"),
            hidden_errors=[
                error for field in form.hidden_fields() for error in field.errors
            ],
        ),
        status=400
        if request_error or (request.method == "POST" and intent != "check")
        else 200,
    )


opening_balance_urls = [
    path("finance/opening-balances/", opening_balances, name="opening_balances"),
    path(
        "finance/opening-balances/new/",
        opening_balance_action,
        {"action": "record"},
        name="opening_balance_new",
    ),
    path(
        "finance/opening-balances/<int:pk>/",
        opening_balance_detail,
        name="opening_balance_detail",
    ),
    path(
        "finance/opening-balances/<int:pk>/match/",
        opening_balance_action,
        {"action": "settle"},
        name="opening_balance_match",
    ),
    path(
        "finance/opening-balances/<int:pk>/correct/",
        opening_balance_action,
        {"action": "correct"},
        name="opening_balance_correct",
    ),
    path(
        "finance/opening-balance-settlements/<int:pk>/cancel/",
        opening_balance_action,
        {"action": "cancel"},
        name="opening_balance_settlement_cancel",
    ),
    path(
        "finance/opening-balance-settlements/<int:pk>/refund/",
        opening_balance_action,
        {"action": "refund"},
        name="opening_balance_settlement_refund",
    ),
]
