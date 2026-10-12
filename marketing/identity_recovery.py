"""Review and recover only identity proven to have been inferred from an invoice.

The chain is the authority for new writes. Mutable historical audit rows are
read-only evidence until staff explicitly reviews and adopts a coherent source.
No command here changes an Invoice, Order, payment or accounting entry.
"""

import hashlib
import json
import re
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from core.audit import record
from core.models import AuditEvent, DomainEvent, Membership, Organization
from finance.models import (
    ZERO,
    Invoice,
    InvoiceCancellation,
    Journal,
    JournalLine,
    Party,
    Product,
)

from .models import Lead, LeadIdentityChange

FIELDS = ("party", "product")
LEGACY_ACTIONS = {
    "marketing.lead.created",
    "marketing.lead.invoice_linked",
    "marketing.lead.invoice_corrected",
}
SOURCE_TASK = "Periksa sumber pelanggan/produk: riwayat lama belum dapat membuktikan asal identitas."


def _plain(value):
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, (datetime, date, Decimal)):
        return str(value)
    return value


def _hash(value):
    return hashlib.sha256(
        json.dumps(
            _plain(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()


def _row(obj):
    return _plain(
        {
            field.attname: getattr(obj, field.attname)
            for field in obj._meta.concrete_fields
        }
    )


def _mapping(lead):
    return {field: getattr(lead, field + "_id") for field in FIELDS}


def _id(value, *, nullable=False):
    return value is None and nullable or type(value) is int and value > 0


def _require(condition, message=SOURCE_TASK):
    if not condition:
        raise ValidationError(message)


def _organization(value, *, lock=False):
    pk = getattr(value, "pk", value)
    _require(_id(pk), "Pilih perusahaan tersimpan yang sah.")
    query = (
        Organization.objects.select_for_update() if lock else Organization.objects.all()
    )
    try:
        return query.get(pk=pk)
    except Organization.DoesNotExist as exc:
        raise ValidationError("Perusahaan tidak ditemukan.") from exc


def _lead(org, value, *, lock=False):
    pk = getattr(value, "pk", value)
    _require(_id(pk), "Pilih prospek tersimpan yang sah.")
    query = Lead.objects.select_for_update() if lock else Lead.objects.all()
    try:
        return query.get(organization=org, pk=pk)
    except Lead.DoesNotExist as exc:
        raise ValidationError("Prospek tidak tersedia untuk perusahaan ini.") from exc


def _audit_rows(lead):
    # Global Lead PKs let us detect a mis-scoped audit without exposing its data.
    return list(
        AuditEvent.objects.filter(action__in=LEGACY_ACTIONS)
        .filter(
            Q(object_type="Lead", object_id=str(lead.pk))
            | Q(organization_id=lead.organization_id, object_id=str(lead.pk))
        )
        .order_by("created_at", "pk")
    )


def _invoice(org, pk):
    _require(_id(pk))
    try:
        result = Invoice.objects.get(organization=org, pk=pk)
    except Invoice.DoesNotExist as exc:
        raise ValidationError(SOURCE_TASK) from exc
    _require(
        Party.objects.filter(organization=org, pk=result.party_id).exists()
        and Product.objects.filter(organization=org, pk=result.product_id).exists()
    )
    return result


def _invoice_snapshot(invoice):
    return {"invoice": _row(invoice)}


def _historical_actor(event, organization_id, *, nullable=True):
    # Historical access may be revoked or its role changed. Retained accounts
    # still need a company membership record; deleted audit actors may be null.
    if event.actor_id is None:
        _require(nullable)
    else:
        _require(
            _id(event.actor_id)
            and Membership.all_objects.filter(
                organization_id=organization_id, user_id=event.actor_id
            ).exists()
        )


def _legacy_invoice_proof(org, invoice, linked_at):
    _require(invoice.status in {"issued", "delivered", "cancelled"})
    issues = list(
        AuditEvent.objects.filter(
            organization=org,
            object_type="Invoice",
            object_id=str(invoice.pk),
            action="finance.invoice.issued",
            created_at__lte=linked_at,
        ).order_by("created_at", "pk")
    )
    domains = list(
        DomainEvent.objects.filter(
            organization=org,
            aggregate_type="finance.invoice",
            aggregate_id=str(invoice.pk),
            topic="invoice.issued",
            created_at__lte=linked_at,
        ).order_by("created_at", "pk")
    )
    _require(len(issues) == len(domains) == 1)
    issue, domain = issues[0], domains[0]
    _historical_actor(issue, org.pk)
    _require(
        domain.version == 1
        and domain.idempotency_key == f"invoice.issued:{invoice.pk}"
        and isinstance(issue.detail, dict)
        and set(issue.detail) == {"total"}
        and issue.detail["total"] == str(invoice.total)
        and isinstance(domain.payload, dict)
        and set(domain.payload) == {"invoice_id", "total", "date"}
        and _id(domain.payload["invoice_id"])
        and domain.payload["invoice_id"] == invoice.pk
        and domain.payload["total"] == str(invoice.total)
        and domain.payload["date"] == invoice.date.isoformat()
    )
    journals = list(
        Journal.objects.filter(
            organization=org, source_key=f"invoice:{invoice.pk}:issue"
        )
    )
    _require(len(journals) == 1)
    journal = journals[0]
    lines = list(JournalLine.objects.filter(journal=journal).order_by("pk"))
    _require(
        journal.status == "posted"
        and journal.date == invoice.date
        and journal.posted_at is not None
        and not timezone.is_naive(journal.posted_at)
        and journal.created_at
        <= journal.posted_at
        <= min(issue.created_at, domain.created_at, linked_at)
        and all(line.organization_id == org.pk for line in lines)
        and [(line.account, line.debit, line.credit) for line in lines]
        == [("AR", invoice.total, ZERO), ("DEFERRED_REVENUE", ZERO, invoice.total)]
    )
    proof = {
        **_invoice_snapshot(invoice),
        "issue_audit": _row(issue),
        "issue_domain": _row(domain),
        "issue_journal": _row(journal),
        "issue_lines": [_row(line) for line in lines],
    }
    if invoice.status == "cancelled":
        cancellations = list(
            InvoiceCancellation.objects.filter(organization=org, invoice=invoice)
        )
        _require(len(cancellations) == 1)
        cancellation = cancellations[0]
        cancel_journal = cancellation.journal
        cancel_lines = list(
            JournalLine.objects.filter(journal=cancel_journal).order_by("pk")
        )
        cancel_audits = list(
            AuditEvent.objects.filter(
                organization=org,
                object_type="Invoice",
                object_id=str(invoice.pk),
                action="finance.invoice.cancelled",
            ).order_by("created_at", "pk")
        )
        _require(
            cancellation.created_at > linked_at
            and cancellation.amount == invoice.total
            and cancellation.unpaid_amount >= ZERO
            and cancellation.refund_amount >= ZERO
            and cancellation.unpaid_amount + cancellation.refund_amount == invoice.total
            and cancellation.reference == f"cancel:{invoice.pk}"
            and cancellation.date >= invoice.date
            and cancel_journal.organization_id == org.pk
            and cancel_journal.status == "posted"
            and cancel_journal.source_key == f"invoice:{invoice.pk}:cancellation"
            and cancel_journal.date == cancellation.date
            and cancel_journal.posted_at is not None
            and not timezone.is_naive(cancel_journal.posted_at)
            and linked_at
            < cancel_journal.created_at
            <= cancel_journal.posted_at
            <= cancellation.created_at
            and all(line.organization_id == org.pk for line in cancel_lines)
            and len(cancel_audits) == 1
        )
        expected_lines = [("DEFERRED_REVENUE", invoice.total, ZERO)]
        if cancellation.unpaid_amount:
            expected_lines.append(("AR", ZERO, cancellation.unpaid_amount))
        if cancellation.refund_amount:
            expected_lines.append(("CUSTOMER_REFUND", ZERO, cancellation.refund_amount))
        cancel_audit = cancel_audits[0]
        _historical_actor(cancel_audit, org.pk)
        _require(
            [(line.account, line.debit, line.credit) for line in cancel_lines]
            == expected_lines
            and cancel_audit.created_at >= cancellation.created_at
            and isinstance(cancel_audit.detail, dict)
            and set(cancel_audit.detail) == {"date", "reason"}
            and cancel_audit.detail["date"] == cancellation.date.isoformat()
            and cancel_audit.detail["reason"] == cancellation.evidence
        )
        proof.update(
            {
                "cancellation": _row(cancellation),
                "cancellation_journal": _row(cancel_journal),
                "cancellation_lines": [_row(line) for line in cancel_lines],
                "cancellation_audit": _row(cancel_audit),
            }
        )
    else:
        _require(not InvoiceCancellation.objects.filter(invoice=invoice).exists())
    return proof


def _typed_mapping(value, keys):
    _require(isinstance(value, dict) and set(value) == set(keys))
    _require(all(_id(value[key], nullable=True) for key in keys))
    return {field: value[key] for field, key in zip(FIELDS, keys)}


def _creation(lead, event):
    from .services import LEAD_FIELDS

    _historical_actor(event, lead.organization_id, nullable=False)
    _require(
        event.actor_id == lead.created_by_id and event.created_at >= lead.created_at
    )
    detail = event.detail
    _require(
        isinstance(detail, dict)
        and set(detail) == {"values", "stage_time_basis"}
        and detail["stage_time_basis"] == "recorded_now"
        and isinstance(detail["values"], dict)
        and set(detail["values"]) == LEAD_FIELDS
    )
    values = detail["values"]
    _require(
        all(
            _id(values[field], nullable=True)
            for field in ("party", "product", "campaign", "owner")
        )
    )
    _require(
        all(
            isinstance(values[field], str)
            for field in (
                "reference",
                "name",
                "channel",
                "segment",
                "region",
                "stage",
                "notes",
            )
        )
    )
    _require(
        all(
            values[field] == getattr(lead, field)
            for field in ("reference", "name", "channel", "segment", "region")
        )
        and values["campaign"] == lead.campaign_id
        and values["owner"] == lead.owner_id
    )
    try:
        received = datetime.fromisoformat(values["received_at"])
        _require(not timezone.is_naive(received) and received == lead.received_at)
        if values["first_response_at"] is not None:
            response = datetime.fromisoformat(values["first_response_at"])
            _require(not timezone.is_naive(response) and response >= received)
        if values["next_follow_up"] is not None:
            _require(
                date.fromisoformat(values["next_follow_up"])
                >= timezone.localdate(received)
            )
    except (ValueError, TypeError) as exc:
        raise ValidationError(SOURCE_TASK) from exc
    initial = {field: values[field] for field in FIELDS}
    for field, model in (("party", Party), ("product", Product)):
        if initial[field] is not None:
            _require(
                model.objects.filter(
                    organization_id=lead.organization_id, pk=initial[field]
                ).exists()
            )
    if lead.campaign_id and lead.campaign.product_id:
        _require(initial["product"] == lead.campaign.product_id)
    return initial


def _legacy_basis(org, lead):
    events = _audit_rows(lead)
    identity_events = [
        event for event in events if event.action != "marketing.lead.created"
    ]
    empty = {
        "initial_mapping": _mapping(lead),
        "first_sources": {field: None for field in FIELDS},
        "mapping": _mapping(lead),
        "invoice_id": lead.invoice_id,
        "legacy_evidence": [],
        "source_evidence": [],
    }
    if not identity_events and lead.invoice_id is None:
        creations = [
            event for event in events if event.action == "marketing.lead.created"
        ]
        if creations:
            _require(
                len(creations) == 1
                and creations[0].organization_id == org.pk
                and creations[0].object_type == "Lead"
                and creations[0].object_id == str(lead.pk)
                and _creation(lead, creations[0]) == _mapping(lead)
            )
        return {**empty, "legacy_review_required": False}
    _require(
        events
        and all(
            event.organization_id == org.pk
            and event.object_type == "Lead"
            and event.object_id == str(lead.pk)
            for event in events
        )
    )
    creations = [event for event in events if event.action == "marketing.lead.created"]
    _require(len(creations) == 1 and events[0].pk == creations[0].pk)
    initial = _creation(lead, creations[0])
    current, first = dict(initial), {field: None for field in FIELDS}
    current_invoice, last_version = None, 1
    source_evidence = []
    for event in identity_events:
        _historical_actor(event, org.pk)
        detail = event.detail
        _require(
            isinstance(detail, dict)
            and _id(detail.get("version"))
            and last_version < detail["version"] <= lead.version
        )
        if event.action == "marketing.lead.invoice_linked":
            _require(
                set(detail)
                == {
                    "invoice_id",
                    "version",
                    "basis",
                    "before_mapping",
                    "after_mapping",
                    "identity_confirmed",
                }
                and detail["basis"] == "finance_invoice_reference"
                and type(detail["identity_confirmed"]) is bool
                and current_invoice is None
                and _id(detail["invoice_id"])
            )
            before = _typed_mapping(
                detail["before_mapping"], ("party_id", "product_id")
            )
            after = _typed_mapping(detail["after_mapping"], ("party_id", "product_id"))
            source = _invoice(org, detail["invoice_id"])
            _require(
                source.date >= timezone.localdate(lead.received_at)
                and before == current
                and after == {"party": source.party_id, "product": source.product_id}
            )
            source_evidence.append(_legacy_invoice_proof(org, source, event.created_at))
            for field in FIELDS:
                if before[field] is None:
                    _require(
                        initial[field] is None
                        and first[field] is None
                        and detail["identity_confirmed"] is True
                    )
                    first[field] = source.pk
                else:
                    _require(after[field] == before[field])
            current, current_invoice = after, source.pk
        else:
            _require(
                set(detail)
                == {"before_invoice_id", "after_invoice_id", "reason", "version"}
                and _id(detail["before_invoice_id"], nullable=True)
                and _id(detail["after_invoice_id"], nullable=True)
                and isinstance(detail["reason"], str)
                and bool(detail["reason"].strip())
                and detail["before_invoice_id"] == current_invoice
            )
            if detail["after_invoice_id"] is not None:
                source = _invoice(org, detail["after_invoice_id"])
                _require(
                    source.date >= timezone.localdate(lead.received_at)
                    and current
                    == {"party": source.party_id, "product": source.product_id}
                )
                source_evidence.append(
                    _legacy_invoice_proof(org, source, event.created_at)
                )
            current_invoice = detail["after_invoice_id"]
        last_version = detail["version"]
    _require(current == _mapping(lead) and current_invoice == lead.invoice_id)
    return {
        "initial_mapping": initial,
        "first_sources": first,
        "mapping": current,
        "invoice_id": current_invoice,
        "legacy_evidence": [_row(event) for event in events],
        "source_evidence": source_evidence,
        "legacy_review_required": True,
    }


def change_snapshot_digest(change):
    names = (
        "organization_id",
        "lead_id",
        "previous_id",
        "created_by_id",
        "schema_version",
        "kind",
        "before_party_id",
        "after_party_id",
        "before_product_id",
        "after_product_id",
        "before_invoice_id",
        "after_invoice_id",
        "before_version",
        "after_version",
        "identity_confirmed",
        "origin_snapshot",
        "source_snapshot",
        "request_key",
        "request_payload",
        "viewed_fingerprint",
        "reason",
        "evidence",
    )
    return _hash({name: getattr(change, name) for name in names})


def _chain_basis(org, lead, history):
    root = history[0]
    _require(
        root.previous_id is None
        and isinstance(root.origin_snapshot, dict)
        and root.origin_snapshot.get("schema") == 1
    )
    if root.kind == "legacy_adoption":
        legacy = root.origin_snapshot.get("legacy_basis")
        _require(
            isinstance(legacy, dict)
            and isinstance(legacy.get("legacy_evidence"), list)
            and legacy["legacy_evidence"]
        )
        initial = _typed_mapping(legacy.get("initial_mapping"), FIELDS)
        first = _typed_mapping(legacy.get("first_sources"), FIELDS)
        current = _typed_mapping(legacy.get("mapping"), FIELDS)
        current_invoice = legacy.get("invoice_id")
        _require(
            _id(current_invoice, nullable=True)
            and current
            == {"party": root.before_party_id, "product": root.before_product_id}
            and current_invoice == root.before_invoice_id
        )
    else:
        _require(root.kind in {"invoice_link", "attribution"})
        initial = _typed_mapping(root.origin_snapshot.get("initial_mapping"), FIELDS)
        _require(
            initial
            == {"party": root.before_party_id, "product": root.before_product_id}
        )
        original_lead = root.origin_snapshot.get("initial_lead_snapshot")
        _require(
            isinstance(original_lead, dict)
            and original_lead.get("id") == lead.pk
            and original_lead.get("organization_id") == org.pk
            and original_lead.get("party_id") == initial["party"]
            and original_lead.get("product_id") == initial["product"]
            and all(
                original_lead.get(name) == getattr(lead, name)
                for name in (
                    "reference",
                    "name",
                    "campaign_id",
                    "channel",
                    "owner_id",
                    "segment",
                    "region",
                )
            )
        )
        first, current, current_invoice = (
            {field: None for field in FIELDS},
            dict(initial),
            root.before_invoice_id,
        )
    previous = None
    last_version = 0
    for change in history:
        _require(
            change.organization_id == org.pk
            and change.lead_id == lead.pk
            and change.schema_version == 1
            and change.previous_id == (previous.pk if previous else None)
            and change.snapshot_digest == change_snapshot_digest(change)
            and change.before_version >= last_version
            and change.after_version <= lead.version
            and (
                change.before_party_id,
                change.before_product_id,
                change.before_invoice_id,
            )
            == (current["party"], current["product"], current_invoice)
        )
        after = {"party": change.after_party_id, "product": change.after_product_id}
        _require(all(_id(after[field], nullable=True) for field in FIELDS))
        if change.kind == "legacy_adoption":
            _require(
                previous is None
                and after == current
                and change.after_invoice_id == current_invoice
                and change.before_version == change.after_version
            )
        else:
            _require(change.after_version == change.before_version + 1)
            if change.kind == "invoice_link":
                snapshot = (
                    change.source_snapshot.get("invoice")
                    if isinstance(change.source_snapshot, dict)
                    else None
                )
                _require(
                    current_invoice is None
                    and isinstance(snapshot, dict)
                    and _id(snapshot.get("id"))
                    and snapshot["id"] == change.after_invoice_id
                    and snapshot.get("organization_id") == org.pk
                    and snapshot.get("status") in {"issued", "delivered"}
                    and after
                    == {
                        "party": snapshot.get("party_id"),
                        "product": snapshot.get("product_id"),
                    }
                )
                for field in FIELDS:
                    if current[field] is None:
                        _require(
                            initial[field] is None and change.identity_confirmed is True
                        )
                        if first[field] is None:
                            first[field] = change.after_invoice_id
                    else:
                        _require(current[field] == after[field])
            elif change.kind == "attribution":
                _require(after == current)
            elif change.kind in {"replace", "reset"}:
                _require(
                    change.identity_confirmed is True
                    and change.request_key
                    and change.request_payload
                    and change.reason.strip()
                    and change.evidence.strip()
                )
                for field in FIELDS:
                    _require(
                        after[field] == current[field]
                        if initial[field] is not None
                        else first[field] is not None or after[field] == current[field]
                    )
                if change.kind == "reset":
                    _require(
                        change.after_invoice_id is None
                        and all(
                            after[field] is None for field in FIELDS if first[field]
                        )
                    )
            else:
                raise ValidationError(SOURCE_TASK)
        current, current_invoice, last_version, previous = (
            after,
            change.after_invoice_id,
            change.after_version,
            change,
        )
    _require(current == _mapping(lead) and current_invoice == lead.invoice_id)
    for field in FIELDS:
        if first[field] is not None:
            _invoice(org, first[field])
    return {
        "initial_mapping": initial,
        "first_sources": first,
        "mapping": current,
        "invoice_id": current_invoice,
        "legacy_review_required": False,
    }


def _basis(org, lead):
    history = list(
        LeadIdentityChange.objects.filter(organization=org, lead=lead).order_by(
            "created_at", "pk"
        )
    )
    try:
        facts = (
            _chain_basis(org, lead, history) if history else _legacy_basis(org, lead)
        )
        return {**facts, "history": history, "blockers": []}
    except ValidationError as exc:
        return {
            "initial_mapping": _mapping(lead),
            "first_sources": {field: None for field in FIELDS},
            "mapping": _mapping(lead),
            "invoice_id": lead.invoice_id,
            "history": history,
            "legacy_review_required": not bool(history),
            "blockers": list(exc.messages),
            "invalid_legacy_evidence": [_row(event) for event in _audit_rows(lead)]
            if not history
            else [],
        }


def validate_lead_identity_write(*, before, after, proof):
    """The model independently binds its one-use privilege to persisted proof."""
    facts = _basis(before.organization, before)
    eligible = tuple(
        field for field in FIELDS if facts["first_sources"][field] is not None
    )
    _require(
        not facts["blockers"]
        and facts["history"]
        and not facts["legacy_review_required"]
        and bool(eligible)
        and eligible == proof["eligible_fields"]
        and before.organization_id == after.organization_id,
        "Riwayat tersimpan tidak membuktikan izin memperbaiki kolom tersebut.",
    )
    for field in FIELDS:
        if field not in eligible:
            _require(
                getattr(after, field + "_id") == getattr(before, field + "_id"),
                "Pelanggan atau produk manual/kampanye tetap dilindungi.",
            )
    if after.invoice_id is None:
        _require(
            all(getattr(after, field + "_id") is None for field in eligible),
            "Belum tahu invoice berarti mengosongkan hanya identitas inferensi.",
        )
    else:
        target = _invoice(before.organization, after.invoice_id)
        _require(
            target.status in {"issued", "delivered"}
            and target.date >= timezone.localdate(before.received_at)
            and all(
                getattr(after, field + "_id") == getattr(target, field + "_id")
                for field in FIELDS
            ),
            "Perubahan identitas harus sesuai dengan invoice terbit dalam perusahaan yang sama.",
        )
    _require(
        (before.party_id, before.product_id, before.invoice_id)
        != (after.party_id, after.product_id, after.invoice_id),
        "Tidak ada perubahan identitas yang perlu disimpan.",
    )


def _scope_state(org):
    from operations.models import Order, OrderLine

    return {
        "invoices": [
            _row(obj) for obj in Invoice.objects.filter(organization=org).order_by("pk")
        ],
        "parties": [
            _row(obj) for obj in Party.objects.filter(organization=org).order_by("pk")
        ],
        "products": [
            _row(obj) for obj in Product.objects.filter(organization=org).order_by("pk")
        ],
        "lead_assignments": list(
            Lead.objects.filter(organization=org)
            .order_by("pk")
            .values_list("pk", "version", "party_id", "product_id", "invoice_id")
        ),
        "orders": [
            _row(obj) for obj in Order.objects.filter(organization=org).order_by("pk")
        ],
        "order_lines": [
            _row(obj)
            for obj in OrderLine.objects.filter(organization=org).order_by("pk")
        ],
    }


def _context(org, lead, facts, scope=None):
    from operations.models import Order, OrderLine

    scope = scope if scope is not None else _scope_state(org)
    initial, first = facts["initial_mapping"], facts["first_sources"]
    eligible = (
        [field for field in FIELDS if first[field] is not None]
        if not facts["blockers"]
        else []
    )
    origins = {
        field: {
            "kind": "unknown"
            if facts["blockers"]
            else "protected"
            if initial[field] is not None
            else "invoice_inferred"
            if first[field] is not None and getattr(lead, field + "_id") is not None
            else "unknown",
            "source_invoice_id": first[field],
        }
        for field in FIELDS
    }
    orders = list(Order.objects.filter(organization=org, lead=lead).order_by("pk"))
    dependencies = [
        {"order": order, "url": reverse("operations:order_lead", args=[order.pk])}
        for order in orders
    ]
    blockers = list(facts["blockers"])
    if orders and eligible:
        blockers.append(
            "Pesanan masih terkait prospek ini. Periksa dan koreksi hubungan pesanan terlebih dahulu; invoice serta peserta pesanan tetap dipertahankan."
        )
    taken = set(
        Lead.objects.filter(organization=org)
        .exclude(pk=lead.pk)
        .exclude(invoice=None)
        .values_list("invoice_id", flat=True)
    )
    taken.update(
        OrderLine.objects.filter(organization=org)
        .exclude(order__lead=None)
        .exclude(order__lead=lead)
        .values_list("invoice_id", flat=True)
    )
    candidates = []
    if eligible and not blockers:
        for invoice in (
            Invoice.objects.filter(
                organization=org,
                status__in=["issued", "delivered"],
                date__gte=timezone.localdate(lead.received_at),
            )
            .select_related("party", "product")
            .order_by("-date", "pk")
        ):
            if (
                invoice.pk not in taken
                and invoice.party.kind != "supplier"
                and ((lead.segment == "reseller") == (invoice.party.kind == "reseller"))
                and all(
                    field in eligible
                    or getattr(lead, field + "_id") == getattr(invoice, field + "_id")
                    for field in FIELDS
                )
            ):
                candidates.append(invoice)
    fingerprint = _hash(
        {
            "schema": "lead-invoice-identity-v1",
            "organization_id": org.pk,
            "lead": _row(lead),
            "facts": {key: value for key, value in facts.items() if key != "history"},
            "history": [_row(change) for change in facts["history"]],
            "scope": scope,
        }
    )
    source_ids = sorted({pk for pk in first.values() if pk is not None})
    return {
        "lead": lead,
        "version": lead.version,
        "fingerprint": fingerprint,
        "eligible_fields": eligible,
        "origins": origins,
        "history": facts["history"],
        "legacy_review_required": facts["legacy_review_required"],
        "blockers": blockers,
        "current_mapping": _mapping(lead),
        "current_invoice": Invoice.objects.filter(
            organization=org, pk=lead.invoice_id
        ).first(),
        "source_invoices": list(
            Invoice.objects.filter(organization=org, pk__in=source_ids).order_by("pk")
        ),
        "candidate_invoices": candidates,
        "order_dependencies": dependencies,
        "legacy_events": [
            event
            for event in _audit_rows(lead)
            if event.organization_id == org.pk
            and event.object_type == "Lead"
            and event.object_id == str(lead.pk)
        ]
        if facts["legacy_review_required"]
        else [],
        "can_recover": bool(eligible) and not blockers,
    }


@transaction.atomic
def identity_recovery_context(*, organization, lead):
    """Pure local read: it never creates an origin, adoption or review marker."""
    org = _organization(organization, lock=True)
    obj = _lead(org, lead, lock=True)
    _lock_dependencies(org, obj)
    return _context(org, obj, _basis(org, obj))


def identity_lineage_fingerprint(*, organization):
    """Bind every Lead version/mapping and chain, including detached/reset Leads."""
    org = _organization(organization)
    basis = []
    for lead in Lead.objects.filter(organization=org).order_by("pk"):
        facts = _basis(org, lead)
        basis.append(
            {
                "lead": _row(lead),
                "facts": {
                    key: value for key, value in facts.items() if key != "history"
                },
                "history": [_row(change) for change in facts["history"]],
            }
        )
    return _hash(
        {
            "schema": "lead-identity-lineage-v1",
            "organization_id": org.pk,
            "leads": basis,
        }
    )


def _append(
    org,
    actor,
    lead,
    *,
    previous,
    kind,
    before,
    before_version,
    identity_confirmed=False,
    origin_snapshot=None,
    source_snapshot=None,
    request_key=None,
    request_payload=None,
    viewed_fingerprint="",
    reason="",
    evidence="",
):
    change = LeadIdentityChange(
        organization=org,
        created_by=actor,
        lead=lead,
        previous=previous,
        kind=kind,
        before_party_id=before["party"],
        before_product_id=before["product"],
        before_invoice_id=before["invoice"],
        after_party_id=lead.party_id,
        after_product_id=lead.product_id,
        after_invoice_id=lead.invoice_id,
        before_version=before_version,
        after_version=lead.version,
        identity_confirmed=identity_confirmed,
        origin_snapshot=deepcopy(origin_snapshot or {}),
        source_snapshot=deepcopy(source_snapshot or {}),
        request_key=request_key,
        request_payload=deepcopy(request_payload or {}),
        viewed_fingerprint=viewed_fingerprint,
        reason=reason,
        evidence=evidence,
    )
    change.snapshot_digest = change_snapshot_digest(change)
    change._identity_write = change._service_write = True
    try:
        change.save()
    finally:
        change.__dict__.pop("_identity_write", None)
        change.__dict__.pop("_service_write", None)
    return change


def _adopt(org, actor, lead, facts, *, viewed_fingerprint, reason, evidence):
    before = {**_mapping(lead), "invoice": lead.invoice_id}
    legacy = {
        key: value
        for key, value in facts.items()
        if key not in {"history", "blockers", "legacy_review_required"}
    }
    change = _append(
        org,
        actor,
        lead,
        previous=None,
        kind="legacy_adoption",
        before=before,
        before_version=lead.version,
        identity_confirmed=True,
        origin_snapshot={
            "schema": 1,
            "basis": "reviewed_mutable_legacy",
            "legacy_basis": legacy,
        },
        source_snapshot={"legacy_evidence_digest": _hash(legacy)},
        viewed_fingerprint=viewed_fingerprint,
        reason=reason,
        evidence=evidence,
    )
    record(
        org,
        actor,
        "marketing.lead.identity_source_reviewed",
        obj=lead,
        detail={
            "change_id": change.pk,
            "reason": reason,
            "evidence": evidence,
            "viewed_fingerprint": viewed_fingerprint,
            "basis": "reviewed_mutable_legacy",
        },
    )
    return change


def prepare_identity_transition(
    *,
    organization,
    actor,
    lead,
    confirm_legacy_source=False,
    expected_fingerprint=None,
    reason="",
    evidence="",
):
    """Prepare the real link/correction seam, before its controlled Lead save."""
    _lock_dependencies(organization, lead)
    facts = _basis(organization, lead)
    _require(not facts["blockers"], " ".join(facts["blockers"]))
    previous = facts["history"][-1] if facts["history"] else None
    if facts["legacy_review_required"]:
        from . import services

        services._org(organization, actor, {"owner", "finance"})
        context = _context(organization, lead, facts)
        _require(
            confirm_legacy_source is True
            and expected_fingerprint == context["fingerprint"]
            and isinstance(reason, str)
            and reason.strip()
            and isinstance(evidence, str)
            and evidence.strip(),
            "Periksa dan konfirmasikan sumber lama pada halaman perbaikan pelanggan/produk terlebih dahulu.",
        )
        previous = _adopt(
            organization,
            actor,
            lead,
            facts,
            viewed_fingerprint=expected_fingerprint,
            reason=reason,
            evidence=evidence,
        )
    return {
        "previous": previous,
        "facts": facts,
        "lead_source": _row(lead),
        "before": {**_mapping(lead), "invoice": lead.invoice_id},
        "before_version": lead.version,
    }


def record_identity_transition(
    *,
    organization,
    actor,
    lead,
    prepared,
    kind,
    invoice=None,
    identity_confirmed=False,
    reason="",
    evidence="",
):
    origin = (
        {}
        if prepared["previous"]
        else {
            "schema": 1,
            "basis": "typed_" + kind,
            "initial_mapping": prepared["facts"]["initial_mapping"],
            "initial_lead_snapshot": prepared["lead_source"],
        }
    )
    return _append(
        organization,
        actor,
        lead,
        previous=prepared["previous"],
        kind=kind,
        before=prepared["before"],
        before_version=prepared["before_version"],
        identity_confirmed=identity_confirmed,
        origin_snapshot=origin,
        source_snapshot=_invoice_snapshot(invoice) if invoice else {},
        reason=reason,
        evidence=evidence,
    )


def _text(value, label, limit, *, multiline=False):
    _require(
        isinstance(value, str)
        and bool(value.strip())
        and len(value.strip()) <= limit
        and "\x00" not in value
        and (
            multiline or not any(ord(char) < 32 or ord(char) == 127 for char in value)
        ),
        f"{label} wajib diisi, maksimal {limit} karakter.",
    )
    return value.strip()


def _lock_dependencies(org, lead):
    from operations.models import Order, OrderLine

    # All public writers take Organization first. Row locks additionally hold the
    # mutable legacy evidence and target/Order rows during validation and append.
    list(Invoice.objects.select_for_update().filter(organization=org).order_by("pk"))
    list(Party.objects.select_for_update().filter(organization=org).order_by("pk"))
    list(Product.objects.select_for_update().filter(organization=org).order_by("pk"))
    list(Order.objects.select_for_update().filter(organization=org).order_by("pk"))
    list(OrderLine.objects.select_for_update().filter(organization=org).order_by("pk"))
    list(
        LeadIdentityChange.objects.select_for_update()
        .filter(organization=org, lead=lead)
        .order_by("pk")
    )
    audit_ids = [row.pk for row in _audit_rows(lead)]
    list(AuditEvent.objects.select_for_update().filter(pk__in=audit_ids).order_by("pk"))
    list(
        AuditEvent.objects.select_for_update()
        .filter(
            organization=org,
            action__in=["finance.invoice.issued", "finance.invoice.cancelled"],
        )
        .order_by("pk")
    )
    list(
        DomainEvent.objects.select_for_update()
        .filter(organization=org, topic="invoice.issued")
        .order_by("pk")
    )
    list(
        Journal.objects.select_for_update()
        .filter(organization=org, source_key__startswith="invoice:")
        .order_by("pk")
    )
    list(
        JournalLine.objects.select_for_update()
        .filter(organization=org, journal__source_key__startswith="invoice:")
        .order_by("pk")
    )
    list(
        InvoiceCancellation.objects.select_for_update()
        .filter(organization=org)
        .order_by("pk")
    )


@transaction.atomic
def recover_invoice_identity(
    *,
    organization,
    actor,
    lead,
    action,
    target_invoice=None,
    expected_version,
    expected_fingerprint,
    request_key,
    confirm_identity,
    reason,
    evidence,
    confirm_legacy_source=False,
):
    from . import services

    org = services._org(organization, actor, {"owner", "finance"})
    obj = _lead(org, lead, lock=True)
    _require(
        isinstance(action, str) and action in {"replace", "reset"},
        "Pilih tindakan perbaikan yang jelas.",
    )
    target_id = getattr(target_invoice, "pk", target_invoice)
    _require(
        (action == "replace" and _id(target_id))
        or (action == "reset" and target_invoice is None),
        "Penggantian membutuhkan invoice; pilih tindakan belum tahu untuk mengosongkan identitas inferensi.",
    )
    _require(
        type(confirm_identity) is bool and type(confirm_legacy_source) is bool,
        "Konfirmasi pemeriksaan harus berupa pilihan yang sah.",
    )
    _require(
        _id(expected_version)
        and isinstance(expected_fingerprint, str)
        and re.fullmatch(r"[a-f0-9]{64}", expected_fingerprint),
        "Versi sumber tidak sah. Muat ulang dan periksa kembali.",
    )
    key = _text(request_key, "Identitas permintaan", 160)
    reason, evidence = (
        _text(reason, "Alasan", 500),
        _text(evidence, "Bukti pemeriksaan", 4000, multiline=True),
    )
    payload = {
        "organization_id": org.pk,
        "actor_id": actor.pk,
        "lead_id": obj.pk,
        "action": action,
        "target_invoice_id": target_id,
        "expected_version": expected_version,
        "expected_fingerprint": expected_fingerprint,
        "confirm_identity": confirm_identity,
        "confirm_legacy_source": confirm_legacy_source,
        "reason": reason,
        "evidence": evidence,
    }
    accepted = (
        LeadIdentityChange.objects.filter(organization=org, request_key=key)
        .select_related("lead")
        .first()
    )
    if accepted:
        _require(
            accepted.request_payload == payload,
            "Permintaan ini sudah tersimpan dengan isi berbeda. Buka riwayat dan buat permintaan baru.",
        )
        return accepted
    _require(
        confirm_identity is True,
        "Konfirmasikan bahwa pelanggan, produk dan sumber invoice telah diperiksa.",
    )
    _lock_dependencies(org, obj)
    facts = _basis(org, obj)
    context = _context(org, obj, facts)
    _require(
        obj.version == expected_version
        and context["fingerprint"] == expected_fingerprint,
        "Sumber atau hubungan telah berubah. Periksa versi terbaru sebelum mengonfirmasi lagi.",
    )
    _require(not context["blockers"], " ".join(context["blockers"]))
    eligible = context["eligible_fields"]
    _require(
        bool(eligible),
        "Tidak ada pelanggan/produk yang terbukti terisi dari invoice. Nilai manual atau kampanye tetap dilindungi.",
    )
    if facts["legacy_review_required"]:
        _require(
            confirm_legacy_source is True,
            "Konfirmasikan pemeriksaan sumber lama; salinan audit lama belum menjadi bukti asal immutable.",
        )
    target = None
    if action == "replace":
        target = services._scoped(Invoice, target_id, org)
        _require(
            target.status in {"issued", "delivered"}
            and target.date >= timezone.localdate(obj.received_at),
            "Pilih invoice yang sudah diterbitkan dan tidak mendahului prospek.",
        )
        _require(
            all(
                field in eligible
                or getattr(obj, field + "_id") == getattr(target, field + "_id")
                for field in FIELDS
            ),
            "Invoice tujuan tidak sesuai dengan pelanggan atau produk yang dilindungi.",
        )
        _require(
            target.pk in {invoice.pk for invoice in context["candidate_invoices"]},
            "Invoice sudah terkait prospek atau pesanan lain. Periksa hubungan tersebut terlebih dahulu.",
        )
    before = {**_mapping(obj), "invoice": obj.invoice_id}
    before_version = obj.version
    after = {
        field: getattr(target, field + "_id") if target else None for field in eligible
    }
    _require(
        obj.invoice_id != target_id
        or any(getattr(obj, field + "_id") != value for field, value in after.items()),
        "Nilai ini sudah sesuai dengan kondisi saat ini. Tidak ada perbaikan yang perlu disimpan.",
    )
    previous = facts["history"][-1] if facts["history"] else None
    if facts["legacy_review_required"]:
        previous = _adopt(
            org,
            actor,
            obj,
            facts,
            viewed_fingerprint=expected_fingerprint,
            reason=reason,
            evidence=evidence,
        )
    for field, value in after.items():
        setattr(obj, field + "_id", value)
    obj.invoice, obj.version = target, obj.version + 1
    obj._identity_recovery_write = {
        "before": (
            before["party"],
            before["product"],
            before["invoice"],
            before_version,
        ),
        "after": (obj.party_id, obj.product_id, obj.invoice_id, obj.version),
        "eligible_fields": tuple(eligible),
    }
    obj._invoice_link = obj._service_write = True
    try:
        obj.save()
    finally:
        for flag in ("_identity_recovery_write", "_invoice_link", "_service_write"):
            obj.__dict__.pop(flag, None)
    decision = _append(
        org,
        actor,
        obj,
        previous=previous,
        kind=action,
        before=before,
        before_version=before_version,
        identity_confirmed=True,
        source_snapshot=_invoice_snapshot(target) if target else {},
        request_key=key,
        request_payload=payload,
        viewed_fingerprint=expected_fingerprint,
        reason=reason,
        evidence=evidence,
    )
    record(
        org,
        actor,
        "marketing.lead.identity_recovered",
        obj=obj,
        detail={
            "change_id": decision.pk,
            "action": action,
            "before_mapping": before,
            "after_mapping": {**_mapping(obj), "invoice": obj.invoice_id},
            "version": obj.version,
            "reason": reason,
            "evidence": evidence,
        },
    )
    return decision
