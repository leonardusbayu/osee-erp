"""One dated budget position and controlled spend workflow. No Finance writes."""

import hashlib
import json
from datetime import date as Date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from core.audit import record
from finance.models import Bill

from . import services as planning
from .models import Budget, BudgetEntry
from .spend_models import (
    BudgetBillLink,
    BudgetPaymentMatch,
    BudgetSpend,
    BudgetSpendMovement,
    record_digest,
)

ZERO = Decimal("0.00")
BUCKETS = ("reservation", "open_commitment", "incurred")
MONEY_KEYS = (
    "incurred",
    "open_commitments",
    "reservations",
    "consumed",
    "available",
    "overrun",
    "payments",
    "reported_payments",
    "unmatched_reported_payments",
    "verified_cash",
    "unpaid_bills",
    "withholding_payable",
)
SCHEMA_VERSION = "director-budget-position-v1"
PAYMENT_FIELDS = {
    "bill_payment": "bill_payment_id",
    "withholding_supplier": "withholding_settlement_id",
    "tax_remittance": "tax_remittance_id",
    "supplier_advance_application": "supplier_advance_application_id",
}


def _finance_event_id(match):
    selected = [
        (kind, getattr(match, field))
        for kind, field in PAYMENT_FIELDS.items()
        if getattr(match, field)
    ]
    if len(selected) != 1:
        return None  # A damaged match must still expose its explicit repair row.
    return f"{selected[0][0]}:{selected[0][1]}"


def _hash(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _money(value):
    return f"{Decimal(value):.2f}"


def _positive(value):
    amount = planning._money(value)
    if amount <= ZERO:
        raise ValidationError("Nilai harus lebih besar dari nol.")
    return amount


def _save(obj):
    obj.content_hash = record_digest(obj)
    obj._spend_write = True
    obj.save()
    return obj


def _intact(obj):
    if obj.content_hash != record_digest(obj):
        raise ValidationError(
            "Identitas atau bukti belanja berubah. Minta Owner memeriksa sumber."
        )
    obj.full_clean()


def _entry_snapshot(entry):
    return {
        f.attname: str(getattr(entry, f.attname))
        if isinstance(getattr(entry, f.attname), (Decimal, Date))
        else getattr(entry, f.attname)
        for f in entry._meta.concrete_fields
        if f.name not in {"created_at", "voided_at", "void_reason"}
    }


def _entry_active(entry, as_of):
    return entry.date <= as_of and (
        entry.voided_at is None or entry.voided_at.date() > as_of
    )


def _scope(model, value, org, *, lock=False):
    qs = model.objects.filter(organization=org)
    if lock:
        qs = qs.select_for_update()
    try:
        return qs.get(pk=getattr(value, "pk", value))
    except (model.DoesNotExist, TypeError, ValueError) as exc:
        raise ValidationError("Catatan tidak tersedia untuk perusahaan ini.") from exc


def _issue(message, *, code="source_integrity", source_kind=None, source_id=None):
    return {
        "code": code,
        "message": message,
        "action_label": "Periksa sumber bersama Owner",
        "action_url": None,
        "source_kind": source_kind,
        "source_id": source_id,
    }


def _facts(org, bill, as_of):
    from finance.budget_adapter import bill_budget_facts

    facts = bill_budget_facts(organization=org, bill=bill, as_of=as_of)
    if (
        facts.get("organization_id") != org.pk
        or facts.get("bill_id") != bill.pk
        or facts.get("current") is not True
    ):
        raise ValidationError(
            "Bukti Finance tidak sesuai dengan perusahaan atau tagihan."
        )
    return facts


def _live_links(spend, as_of):
    return (
        spend.bill_links.filter(date__lte=as_of)
        .filter(Q(voided_at__isnull=True) | Q(voided_at__gt=as_of))
        .select_related("bill", "request", "void_request")
    )


def _manual_state(spend, as_of):
    """Replay immutable transfers into source-bounded manual lots."""
    _intact(spend)
    origin = spend.origin_entry
    if (
        origin.organization_id != spend.organization_id
        or origin.budget_id != spend.budget_id
        or _entry_snapshot(origin) != spend.origin_snapshot
    ):
        raise ValidationError(
            "Catatan asal belanja berubah atau tidak sesuai anggaran."
        )
    lots, payments, history, references = {}, {}, [], []
    if _entry_active(origin, as_of):
        lots[f"entry:{origin.pk}"] = {
            "kind": "entry",
            "id": origin.pk,
            "bucket": origin.kind,
            "amount": origin.amount,
            "label": origin.description,
            "date": origin.date,
        }
    for entry in origin.linked_payments.order_by("date", "pk"):
        if (
            entry.organization_id != spend.organization_id
            or entry.budget_id != spend.budget_id
            or entry.kind != "payment"
        ):
            raise ValidationError(
                "Pembayaran lama tidak sesuai perusahaan atau belanja. Minta Owner memeriksa sumber."
            )
        if _entry_active(entry, as_of):
            payments[f"entry:{entry.pk}"] = {
                "kind": "entry",
                "id": entry.pk,
                "amount": entry.amount,
                "label": entry.description,
                "date": entry.date,
            }
            references.append(
                {
                    "kind": "budget_entry",
                    "id": entry.pk,
                    "snapshot": _entry_snapshot(entry),
                }
            )
    movements = list(spend.movements.filter(date__lte=as_of).order_by("date", "pk"))
    by_id = {movement.pk: movement for movement in movements}
    reversed_ids = set()
    for movement in movements:
        _intact(movement)
        references.append(
            {"kind": "spend_movement", "id": movement.pk, "hash": movement.content_hash}
        )
        if movement.kind in {"convert", "release"}:
            allocations = movement.payload.get("allocations", [])
            if (
                not isinstance(allocations, list)
                or sum((Decimal(a["amount"]) for a in allocations), ZERO)
                != movement.amount
            ):
                raise ValidationError("Sumber perpindahan belanja tidak lengkap.")
            for allocation in allocations:
                lot = lots.get(allocation["key"])
                amount = _positive(allocation["amount"])
                if (
                    not lot
                    or lot["bucket"] != movement.from_kind
                    or amount > lot["amount"]
                ):
                    raise ValidationError(
                        "Perpindahan memakai kapasitas sumber yang tidak tersedia."
                    )
                lot["amount"] -= amount
            if movement.kind == "convert":
                lots[f"movement:{movement.pk}"] = {
                    "kind": "movement",
                    "id": movement.pk,
                    "bucket": movement.to_kind,
                    "amount": movement.amount,
                    "label": movement.reason,
                    "date": movement.date,
                }
        elif movement.kind == "payment":
            payments[f"movement:{movement.pk}"] = {
                "kind": "movement",
                "id": movement.pk,
                "amount": movement.amount,
                "label": movement.reason,
                "date": movement.date,
            }
        elif movement.kind == "reverse":
            target = by_id.get(movement.reverse_of_id)
            if (
                not target
                or target.pk in reversed_ids
                or target.kind not in {"convert", "release", "payment"}
            ):
                raise ValidationError("Riwayat koreksi belanja tidak lengkap.")
            if target.kind == "convert":
                output = lots.get(f"movement:{target.pk}")
                if not output or output["amount"] != target.amount:
                    raise ValidationError(
                        "Hasil perpindahan yang dikoreksi telah dipakai lagi."
                    )
                output["amount"] = ZERO
            if target.kind in {"convert", "release"}:
                for allocation in target.payload["allocations"]:
                    if allocation["key"] not in lots:
                        raise ValidationError("Sumber koreksi tidak tersedia.")
                    lots[allocation["key"]]["amount"] += Decimal(allocation["amount"])
            else:
                payments.pop(f"movement:{target.pk}", None)
            reversed_ids.add(target.pk)
        history.append(
            {
                "id": movement.pk,
                "kind": movement.kind,
                "date": str(movement.date),
                "amount": _money(movement.amount),
                "from_kind": movement.from_kind,
                "to_kind": movement.to_kind,
                "reason": movement.reason,
                "evidence": movement.evidence,
                "reference": movement.reference,
                "reversed_by_id": None,
                "can_correct": movement.kind in {"convert", "release", "payment"},
            }
        )
    for item in history:
        reversal = next((m for m in movements if m.reverse_of_id == item["id"]), None)
        item.update(
            reversed_by_id=reversal.pk if reversal else None,
            can_correct=item["can_correct"] and not reversal,
        )
    return lots, payments, history, references


def _matched_capacity(
    org, as_of, *, manual_entry_id=None, manual_movement_id=None, finance_event_id=None
):
    qs = BudgetPaymentMatch.objects.filter(organization=org, date__lte=as_of).filter(
        Q(voided_at__isnull=True) | Q(voided_at__gt=as_of)
    )
    if finance_event_id:
        kind, value = finance_event_id.split(":")
        field = PAYMENT_FIELDS[kind]
        qs = qs.filter(**{field: int(value)})
    elif manual_entry_id:
        qs = qs.filter(manual_entry_id=manual_entry_id)
    else:
        qs = qs.filter(manual_movement_id=manual_movement_id)
    amount = ZERO
    for match in qs:
        _intact(match)
        amount += match.amount
    return amount


def _spend_row(org, spend, as_of):
    row = {
        "spend_id": spend.pk,
        "id": spend.pk,
        "title": spend.title,
        "origin_entry_id": spend.origin_entry_id,
        "budget_id": spend.budget_id,
        "version": spend.version,
        "as_of": str(as_of),
        "current": True,
        "issues": [],
        "tasks": [],
        "references": [],
        "source_references": [],
        "coverable_sources": [],
        "manual_payment_sources": [],
        "finance_payment_sources": [],
        "repair_sources": [],
        "movements": [],
        "bill_links": [],
        "payment_matches": [],
        "allowed_actions": [
            "convert",
            "link_bill",
            "release",
            "report_payment",
            "match_payment",
            "correct_movement",
            "correct_link",
            "correct_match",
        ],
    }
    # Keep repair identities available even when a later Finance proof fails.
    repair_matches = []
    for match in spend.payment_matches.filter(date__lte=as_of).order_by("pk"):
        event_id = _finance_event_id(match)
        repair_matches.append(
            {
                "id": match.pk,
                "date": str(match.date),
                "amount": _money(match.amount),
                "manual_source_key": f"entry:{match.manual_entry_id}"
                if match.manual_entry_id
                else f"movement:{match.manual_movement_id}",
                "finance_event_id": event_id,
                "voided_at": str(match.voided_at)
                if match.voided_at and match.voided_at <= as_of
                else None,
                "can_correct": match.organization_id == org.pk
                and match.content_hash == record_digest(match)
                and (not match.voided_at or match.voided_at > as_of),
            }
        )
    repair_sources, refs = [], []
    try:
        lots, manual, history, refs = _manual_state(spend, as_of)
        row["movements"] = history
        # Scoped manual identities are useful for an Owner repair even when
        # Finance overlap cannot be resolved. They are not available capacity.
        repair_sources = [
            {
                "key": key,
                "kind": lot["kind"],
                "id": lot["id"],
                "bucket": lot["bucket"],
                "label": lot["label"],
                "amount": _money(lot["amount"]),
                "available": None,
                "requires_check": True,
            }
            for key, lot in lots.items()
        ]
        coverage = {key: ZERO for key in lots}
        approved_coverage = {key: ZERO for key in lots}
        pending_coverage = {key: ZERO for key in lots}
        finance_payments = {}
        incurred = commitment = cash = ap = withholding = ZERO
        for link in _live_links(spend, as_of).order_by("pk"):
            _intact(link)
            if link.bill.organization_id != org.pk or link.request.spend_id != spend.pk:
                raise ValidationError(
                    "Hubungan tagihan tidak sesuai perusahaan atau belanja."
                )
            row["bill_links"].append(
                {
                    "id": link.pk,
                    "bill_id": link.bill_id,
                    "bill_number": link.bill.number,
                    "date": str(link.date),
                    "coverage": link.coverage,
                    "voided_at": None,
                    "can_correct": True,
                }
            )
            from finance.budget_adapter import bill_budget_source_identity

            refs.append(
                {
                    "kind": "bill_source_identity",
                    "bill_id": link.bill_id,
                    "hash": bill_budget_source_identity(
                        organization=org, bill=link.bill, as_of=as_of
                    ),
                }
            )
            facts = _facts(org, link.bill, as_of)
            refs.append(
                {
                    "kind": "bill_link",
                    "id": link.pk,
                    "hash": link.content_hash,
                    "source_hash": facts["fingerprint"],
                }
            )
            row["source_references"].extend(facts.get("source_references", []))
            selected_coverage = sum(
                (Decimal(item["amount"]) for item in link.coverage), ZERO
            )
            whole_bill_gross = (
                link.bill.amount
                if facts.get("provisional")
                else Decimal(facts["gross"])
            )
            if selected_coverage > whole_bill_gross:
                raise ValidationError(
                    "Nilai tagihan berkurang di bawah catatan yang digantikannya. Owner perlu memperbaiki hubungan tagihan agar sisa budget tetap tercatat."
                )
            # A draft reserves its explicit overlap, but cannot recognize costs.
            for item in link.coverage:
                key, amount = item["source_key"], _positive(item["amount"])
                if key not in lots:
                    raise ValidationError(
                        "Catatan yang digantikan tagihan tidak tersedia."
                    )
                coverage[key] += amount
                if coverage[key] > lots[key]["amount"]:
                    raise ValidationError(
                        "Tagihan menggunakan kapasitas catatan yang sama dua kali."
                    )
            if not facts.get("provisional"):
                incurred += Decimal(facts["incurred"])
                commitment += Decimal(facts["commitment"])
                ap += Decimal(facts["ap_outstanding"])
                withholding += Decimal(facts["withholding_outstanding"])
                cash += Decimal(facts["verified_cash_outflow"])
                for item in link.coverage:
                    approved_coverage[item["source_key"]] += Decimal(item["amount"])
                for payment in facts.get("payments", []):
                    event_id = payment["event_id"]
                    if event_id in finance_payments:
                        raise ValidationError(
                            "Pembayaran Finance yang sama muncul lebih dari sekali."
                        )
                    finance_payments[event_id] = {
                        **payment,
                        "amount": Decimal(payment["amount"]),
                        **(
                            {
                                "available_from": max(
                                    payment["available_from"], link.date.isoformat()
                                )
                            }
                            if payment.get("available_from")
                            else {}
                        ),
                        "label": f"{link.bill.number} · {payment['date']} · Rp{payment['amount']}",
                    }
            else:
                for item in link.coverage:
                    pending_coverage[item["source_key"]] += Decimal(item["amount"])
        for key, amount in approved_coverage.items():
            lots[key]["amount"] -= amount
        for link in spend.bill_links.filter(
            date__lte=as_of, voided_at__lte=as_of
        ).select_related("bill"):
            _intact(link)
            row["bill_links"].append(
                {
                    "id": link.pk,
                    "bill_id": link.bill_id,
                    "bill_number": link.bill.number,
                    "date": str(link.date),
                    "coverage": link.coverage,
                    "voided_at": str(link.voided_at),
                    "can_correct": False,
                }
            )
            refs.append(
                {
                    "kind": "voided_bill_link",
                    "id": link.pk,
                    "hash": link.content_hash,
                    "void_request_id": link.void_request_id,
                }
            )
        manual_used, finance_used = {}, {}
        for match in spend.payment_matches.filter(date__lte=as_of).order_by("pk"):
            _intact(match)
            active = match.voided_at is None or match.voided_at > as_of
            manual_key = (
                f"entry:{match.manual_entry_id}"
                if match.manual_entry_id
                else f"movement:{match.manual_movement_id}"
            )
            event_id = _finance_event_id(match)
            refs.append(
                {
                    "kind": "payment_match",
                    "id": match.pk,
                    "hash": match.content_hash,
                    "void_request_id": match.void_request_id if not active else None,
                }
            )
            if active:
                payment = finance_payments.get(event_id)
                if (
                    manual_key not in manual
                    or not payment
                    or payment["source_hash"] != match.source_hash
                ):
                    raise ValidationError(
                        "Sumber pembayaran yang dicocokkan berubah atau tidak tersedia."
                    )
                if match.date < max(
                    planning._date(manual[manual_key]["date"]),
                    planning._date(payment.get("available_from", payment["date"])),
                ):
                    raise ValidationError(
                        "Tanggal pencocokan mendahului ketersediaan bukti pembayaran yang ditautkan."
                    )
                manual_used[manual_key] = (
                    manual_used.get(manual_key, ZERO) + match.amount
                )
                finance_used[event_id] = finance_used.get(event_id, ZERO) + match.amount
                if (
                    manual_used[manual_key] > manual[manual_key]["amount"]
                    or finance_used[event_id] > payment["amount"]
                ):
                    raise ValidationError(
                        "Pencocokan pembayaran melampaui nilai bukti asal."
                    )
            row["payment_matches"].append(
                {
                    "id": match.pk,
                    "date": str(match.date),
                    "amount": _money(match.amount),
                    "manual_source_key": manual_key,
                    "finance_event_id": event_id,
                    "voided_at": str(match.voided_at) if not active else None,
                    "can_correct": active,
                }
            )
        totals = {
            bucket: sum(
                (lot["amount"] for lot in lots.values() if lot["bucket"] == bucket),
                ZERO,
            )
            for bucket in BUCKETS
        }
        reported = sum((p["amount"] for p in manual.values()), ZERO)
        unmatched = reported - sum(manual_used.values(), ZERO)
        consumed = (
            totals["incurred"]
            + incurred
            + totals["open_commitment"]
            + commitment
            + totals["reservation"]
        )
        row.update(
            incurred=_money(totals["incurred"] + incurred),
            open_commitments=_money(totals["open_commitment"] + commitment),
            reservations=_money(totals["reservation"]),
            consumed=_money(consumed),
            reported_payments=_money(reported),
            unmatched_reported_payments=_money(unmatched),
            verified_cash=_money(cash),
            payments=_money(unmatched + cash),
            unpaid_bills=_money(ap),
            withholding_payable=_money(withholding),
        )
        row.update(
            amount=_money(spend.origin_entry.amount),
            available=_money(spend.origin_entry.amount - consumed),
            overrun=_money(max(ZERO, consumed - spend.origin_entry.amount)),
        )
        for key, lot in lots.items():
            # Recognized links were deducted; drafts still reserve overlap.
            available = max(ZERO, lot["amount"] - pending_coverage[key])
            row["coverable_sources"].append(
                {
                    "key": key,
                    "kind": lot["kind"],
                    "id": lot["id"],
                    "bucket": lot["bucket"],
                    "label": lot["label"],
                    "available": _money(available),
                }
            )
        row["manual_payment_sources"] = []
        for key, p in manual.items():
            used = _matched_capacity(
                org,
                as_of,
                **{
                    "manual_entry_id"
                    if p["kind"] == "entry"
                    else "manual_movement_id": p["id"]
                },
            )
            if used > p["amount"]:
                raise ValidationError(
                    "Sumber pembayaran dilaporkan sudah dipakai melampaui kapasitasnya."
                )
            row["manual_payment_sources"].append(
                {
                    "key": key,
                    "kind": p["kind"],
                    "id": p["id"],
                    "label": p["label"],
                    "date": str(p["date"]),
                    "amount": _money(p["amount"]),
                    "unmatched": _money(p["amount"] - used),
                }
            )
        row["finance_payment_sources"] = []
        for key, p in finance_payments.items():
            used = _matched_capacity(org, as_of, finance_event_id=key)
            if used > p["amount"]:
                raise ValidationError(
                    "Pembayaran Finance sudah dicocokkan melampaui kapasitasnya."
                )
            row["finance_payment_sources"].append(
                {
                    **p,
                    "amount": _money(p["amount"]),
                    "unmatched": _money(p["amount"] - used),
                }
            )
        row["references"] = [
            {
                "kind": "budget_spend",
                "id": spend.pk,
                "hash": spend.content_hash,
                "origin": _entry_snapshot(spend.origin_entry),
            },
            *refs,
        ]
    except (ValidationError, KeyError, TypeError, ValueError, ArithmeticError) as exc:
        messages = (
            exc.messages
            if isinstance(exc, ValidationError)
            else ["Bukti belanja tidak lengkap atau tidak dapat dibaca."]
        )
        row.update(
            current=False, allowed_actions=["correct_link"] if row["bill_links"] else []
        )
        if row["bill_links"]:
            row["repair_sources"] = repair_sources
        row["payment_matches"] = repair_matches
        if any(m["can_correct"] for m in repair_matches):
            row["allowed_actions"].append("correct_match")
        row["issues"] = [
            _issue(message, source_kind="budget_spend", source_id=spend.pk)
            for message in messages
        ]
        row["tasks"] = row["issues"]
        for key in MONEY_KEYS:
            row[key] = None
        row["references"].extend(refs)
        row["references"].append(
            {
                "kind": "invalid_spend",
                "id": spend.pk,
                "stored_hash": spend.content_hash,
                "actual_hash": record_digest(spend),
            }
        )
    row["fingerprint"] = _hash(row)
    return row


def budget_position(*, organization, budget, as_of):
    as_of = planning._date(as_of)
    org_id = getattr(organization, "pk", organization)
    budget = _scope(Budget, budget, org_id)
    org = budget.organization
    spends = list(
        BudgetSpend.objects.filter(organization=org, budget=budget)
        .select_related("origin_entry")
        .order_by("pk")
    )
    rows = [_spend_row(org, spend, as_of) for spend in spends]
    adopted = {s.origin_entry_id for s in spends}
    entries = list(
        BudgetEntry.objects.filter(organization=org, budget=budget).order_by("pk")
    )
    totals = {key: ZERO for key in MONEY_KEYS}
    refs, issues, observations = [], [], []
    for entry in entries:
        if (
            entry.pk in adopted
            or entry.settles_id in adopted
            or not _entry_active(entry, as_of)
        ):
            continue
        refs.append(
            {"kind": "budget_entry", "id": entry.pk, "snapshot": _entry_snapshot(entry)}
        )
        if entry.kind == "payment":
            totals["reported_payments"] += entry.amount
            totals["unmatched_reported_payments"] += entry.amount
            totals["payments"] += entry.amount
            if entry.settles_id is None:
                observations.append(
                    _issue(
                        "Pembayaran dilaporkan ini belum terkait belanja. Riwayat dan nominal tetap ditampilkan; pencocokan memerlukan sumber belanja yang jelas.",
                        code="unassigned_payment",
                        source_kind="budget_entry",
                        source_id=entry.pk,
                    )
                )
        elif entry.kind in BUCKETS:
            totals[
                {
                    "reservation": "reservations",
                    "open_commitment": "open_commitments",
                    "incurred": "incurred",
                }[entry.kind]
            ] += entry.amount
        else:
            issues.append(
                _issue(
                    "Jenis catatan anggaran tidak dikenal.",
                    source_kind="budget_entry",
                    source_id=entry.pk,
                )
            )
    for row in rows:
        refs.extend(row["references"])
        issues.extend(row["issues"])
        if row["current"]:
            for key in MONEY_KEYS:
                if key not in {"available", "overrun", "consumed"}:
                    totals[key] += Decimal(row[key])
    totals["consumed"] = (
        totals["incurred"] + totals["open_commitments"] + totals["reservations"]
    )
    totals["available"] = budget.amount - totals["consumed"]
    totals["overrun"] = max(ZERO, -totals["available"])
    current = not issues
    tasks = [*issues, *observations]
    if current and totals["overrun"]:
        tasks.append(
            {
                **_issue(
                    "Biaya dan komitmen melampaui otorisasi. Owner perlu meninjau pendanaan; batas anggaran tidak ditambah otomatis.",
                    code="budget_overrun",
                ),
                "role": "owner",
            }
        )
    result = {
        "schema_version": SCHEMA_VERSION,
        "budget_id": budget.pk,
        "amount": _money(budget.amount),
        "status": budget.status,
        "version": budget.version,
        "as_of": str(as_of),
        "spends": rows,
        "references": refs,
        "issues": issues,
        "tasks": tasks,
        "current": current,
        **{key: _money(value) if current else None for key, value in totals.items()},
    }
    result["fingerprint"] = _hash(result)
    return result


def spend_position(*, organization, spend, as_of):
    spend = _scope(BudgetSpend, spend, getattr(organization, "pk", organization))
    return _spend_row(spend.organization, spend, planning._date(as_of))


def _request(kind, *, date, reference, reason, evidence, **payload):
    date = planning._date(date)
    if date > timezone.localdate():
        raise ValidationError("Catatan aktual tidak boleh bertanggal di masa depan.")
    if (
        not isinstance(reference, str)
        or not reference.strip()
        or len(reference.strip()) > 120
    ):
        raise ValidationError("Referensi wajib diisi, maksimal 120 karakter.")
    return {
        "kind": kind,
        "date": str(date),
        "reference": reference.strip(),
        "reason": planning._reason(reason),
        "evidence": planning._notes(evidence),
        **payload,
    }


def _retry(org, request):
    old = BudgetSpendMovement.objects.filter(
        organization=org, reference=request["reference"]
    ).first()
    if old:
        _intact(old)
        if old.payload.get("request") != request:
            raise ValidationError(
                "Referensi sudah dipakai untuk permintaan yang berbeda."
            )
    return old


def _guard(spend, expected_version, expected_fingerprint):
    planning._version(spend, expected_version)
    row = spend_position(
        organization=spend.organization, spend=spend, as_of=timezone.localdate()
    )
    if not row["current"]:
        raise ValidationError([i["message"] for i in row["issues"]])
    if (
        not isinstance(expected_fingerprint, str)
        or expected_fingerprint != row["fingerprint"]
    ):
        raise ValidationError("Sumber belanja berubah. Muat ulang sebelum menyimpan.")
    return row


def _chronology(spend, date):
    if (
        date < spend.date
        or spend.movements.exclude(kind="finance_sync").filter(date__gt=date).exists()
    ):
        raise ValidationError(
            "Catat belanja dan koreksi berurutan; tanggal tidak boleh mendahului riwayat berikutnya."
        )


def _movement(
    spend,
    actor,
    request,
    *,
    amount=ZERO,
    from_kind="",
    to_kind="",
    extra=None,
    reverse_of=None,
    before=None,
    after=None,
):
    row = _save(
        BudgetSpendMovement(
            organization=spend.organization,
            spend=spend,
            created_by=actor,
            kind=request["kind"],
            date=request["date"],
            amount=amount,
            from_kind=from_kind,
            to_kind=to_kind,
            reference=request["reference"],
            reason=request["reason"],
            evidence=request["evidence"],
            payload={"request": request, **(extra or {})},
            reverse_of=reverse_of,
            before=before or {},
            after=after or {},
        )
    )
    spend.version += 1
    _save(spend)
    record(
        spend.organization,
        actor,
        f"director.spend.{row.kind}",
        obj=row,
        detail={
            "spend_id": spend.pk,
            "version": spend.version,
            "request_hash": row.content_hash,
        },
    )
    return row


@transaction.atomic
def adopt_budget_entry(
    *,
    organization,
    actor,
    entry,
    date,
    reference,
    reason,
    evidence,
    expected_version,
    expected_fingerprint,
):
    org = planning._org(organization, actor)
    entry = _scope(BudgetEntry, entry, org, lock=True)
    budget = _scope(Budget, entry.budget_id, org, lock=True)
    request = _request(
        "adopt",
        date=date,
        reference=reference,
        reason=reason,
        evidence=evidence,
        entry_id=entry.pk,
    )
    old = _retry(org, request)
    if old:
        return old.spend
    planning._version(budget, expected_version)
    position = budget_position(
        organization=org, budget=budget, as_of=timezone.localdate()
    )
    if not position["current"] or expected_fingerprint != position["fingerprint"]:
        raise ValidationError(
            "Sumber anggaran berubah. Muat ulang sebelum membuat belanja."
        )
    if (
        budget.status != "approved"
        or entry.kind not in BUCKETS
        or entry.voided_at
        or entry.date > planning._date(date)
    ):
        raise ValidationError(
            "Pilih biaya, komitmen atau reservasi aktif dari anggaran disetujui."
        )
    if BudgetSpend.objects.filter(origin_entry=entry).exists():
        raise ValidationError(
            "Catatan ini sudah menjadi belanja. Buka belanja yang ada."
        )
    spend = _save(
        BudgetSpend(
            organization=org,
            created_by=actor,
            budget=budget,
            origin_entry=entry,
            date=date,
            title=entry.description,
            origin_snapshot=_entry_snapshot(entry),
        )
    )
    _movement(spend, actor, request)
    return spend


def _allocations(row, bucket, amount):
    remaining = amount
    allocations = []
    for source in row["coverable_sources"]:
        if source["bucket"] == bucket and remaining:
            take = min(remaining, Decimal(source["available"]))
            if take:
                allocations.append({"key": source["key"], "amount": _money(take)})
                remaining -= take
    if remaining:
        raise ValidationError(
            "Nilai melebihi kapasitas sumber aktif yang belum dihubungkan ke tagihan."
        )
    return allocations


def _transfer(spend, actor, request, row, from_kind, to_kind, amount):
    if request["kind"] == "convert" and (from_kind, to_kind) not in {
        ("reservation", "open_commitment"),
        ("reservation", "incurred"),
        ("open_commitment", "incurred"),
    }:
        raise ValidationError(
            "Pilih perpindahan reservasi atau komitmen menuju tahap berikutnya."
        )
    if request["kind"] == "release" and from_kind not in {
        "reservation",
        "open_commitment",
    }:
        raise ValidationError(
            "Hanya sisa reservasi atau komitmen yang dapat dilepaskan."
        )
    if request["kind"] == "release" and amount > Decimal(row["consumed"]) - Decimal(
        row["payments"]
    ):
        raise ValidationError(
            "Nilai yang sudah dilaporkan atau dicocokkan dibayar tidak boleh dihapus melalui pelepasan residual."
        )
    allocations = _allocations(row, from_kind, amount)
    return _movement(
        spend,
        actor,
        request,
        amount=amount,
        from_kind=from_kind,
        to_kind=to_kind,
        extra={"allocations": allocations},
        before={key: row[key] for key in MONEY_KEYS if key in row},
        after={"from_kind": from_kind, "to_kind": to_kind, "amount": _money(amount)},
    )


@transaction.atomic
def convert_spend(
    *,
    organization,
    actor,
    spend,
    from_kind,
    to_kind,
    amount,
    date,
    reference,
    reason,
    evidence,
    expected_version,
    expected_fingerprint,
):
    org = planning._org(organization, actor)
    spend = _scope(BudgetSpend, spend, org, lock=True)
    amount = _positive(amount)
    request = _request(
        "convert",
        date=date,
        reference=reference,
        reason=reason,
        evidence=evidence,
        spend_id=spend.pk,
        from_kind=from_kind,
        to_kind=to_kind,
        amount=_money(amount),
    )
    old = _retry(org, request)
    if old:
        return old
    row = _guard(spend, expected_version, expected_fingerprint)
    _chronology(spend, planning._date(date))
    return _transfer(spend, actor, request, row, from_kind, to_kind, amount)


@transaction.atomic
def release_spend_residual(
    *,
    organization,
    actor,
    spend,
    bucket,
    amount,
    date,
    reference,
    reason,
    evidence,
    expected_version,
    expected_fingerprint,
):
    org = planning._org(organization, actor, planning.APPROVERS)
    spend = _scope(BudgetSpend, spend, org, lock=True)
    amount = _positive(amount)
    request = _request(
        "release",
        date=date,
        reference=reference,
        reason=reason,
        evidence=evidence,
        spend_id=spend.pk,
        bucket=bucket,
        amount=_money(amount),
    )
    old = _retry(org, request)
    if old:
        return old
    row = _guard(spend, expected_version, expected_fingerprint)
    _chronology(spend, planning._date(date))
    return _transfer(spend, actor, request, row, bucket, "", amount)


@transaction.atomic
def record_spend_payment(
    *,
    organization,
    actor,
    spend,
    amount,
    date,
    reference,
    reason,
    evidence,
    expected_version,
    expected_fingerprint,
):
    org = planning._org(organization, actor)
    spend = _scope(BudgetSpend, spend, org, lock=True)
    amount = _positive(amount)
    request = _request(
        "payment",
        date=date,
        reference=reference,
        reason=reason,
        evidence=evidence,
        spend_id=spend.pk,
        amount=_money(amount),
    )
    old = _retry(org, request)
    if old:
        return old
    row = _guard(spend, expected_version, expected_fingerprint)
    _chronology(spend, planning._date(date))
    if amount > Decimal(row["consumed"]) - Decimal(row["reported_payments"]):
        raise ValidationError(
            "Pembayaran dilaporkan melebihi nilai belanja yang belum dilaporkan dibayar."
        )
    return _movement(spend, actor, request, amount=amount)


def _coverage(value):
    if not isinstance(value, list) or len(value) > 1000:
        raise ValidationError(
            "Pilih catatan yang digantikan tagihan dan nilai masing-masing."
        )
    items, seen = [], set()
    for item in value:
        if (
            not isinstance(item, dict)
            or set(item) != {"source_key", "amount"}
            or not isinstance(item["source_key"], str)
            or item["source_key"] in seen
        ):
            raise ValidationError("Setiap sumber pengganti hanya boleh dipilih sekali.")
        seen.add(item["source_key"])
        items.append(
            {
                "source_key": item["source_key"],
                "amount": _money(_positive(item["amount"])),
            }
        )
    return sorted(items, key=lambda item: item["source_key"])


def _assert_bill_fingerprint(facts, expected):
    if not isinstance(expected, str) or expected != facts["fingerprint"]:
        raise ValidationError(
            "Tagihan atau bukti Finance berubah sejak diperiksa. Muat ulang dan periksa kembali sebelum menghubungkan."
        )


def _link(spend, actor, request, bill, coverage, facts):
    if BudgetBillLink.objects.filter(bill=bill, voided_at__isnull=True).exists():
        raise ValidationError(
            "Tagihan sudah terhubung ke belanja lain. Gunakan koreksi Owner untuk memperbaiki hubungan."
        )
    row = spend_position(
        organization=spend.organization, spend=spend, as_of=timezone.localdate()
    )
    if not row["current"]:
        raise ValidationError([i["message"] for i in row["issues"]])
    options = {item["key"]: item for item in row["coverable_sources"]}
    for item in coverage:
        source = options.get(item["source_key"])
        if not source or Decimal(item["amount"]) > Decimal(source["available"]):
            raise ValidationError(
                "Nilai pengganti melebihi kapasitas catatan yang belum dipakai."
            )
    # Coverage cannot silently release an estimate larger than this Bill.
    if sum((Decimal(item["amount"]) for item in coverage), ZERO) > bill.amount:
        raise ValidationError(
            "Nilai catatan yang digantikan tidak boleh melebihi tagihan. Lepaskan sisa estimasi secara terpisah melalui Owner."
        )
    movement = _movement(
        spend,
        actor,
        request,
        extra={"bill_id": bill.pk, "source_hash": facts["fingerprint"]},
    )
    return _save(
        BudgetBillLink(
            organization=spend.organization,
            spend=spend,
            bill=bill,
            created_by=actor,
            request=movement,
            date=request["date"],
            coverage=coverage,
            source_snapshot=facts,
            source_hash=facts["fingerprint"],
        )
    )


@transaction.atomic
def link_spend_bill(
    *,
    organization,
    actor,
    spend,
    bill,
    coverage,
    date,
    reference,
    reason,
    evidence,
    expected_version,
    expected_fingerprint,
    expected_bill_fingerprint,
):
    org = planning._org(organization, actor)
    spend = _scope(BudgetSpend, spend, org, lock=True)
    bill = _scope(Bill, bill, org, lock=True)
    coverage = _coverage(coverage)
    request = _request(
        "link_bill",
        date=date,
        reference=reference,
        reason=reason,
        evidence=evidence,
        spend_id=spend.pk,
        bill_id=bill.pk,
        coverage=coverage,
    )
    old = _retry(org, request)
    if old:
        return old.bill_link
    _guard(spend, expected_version, expected_fingerprint)
    _chronology(spend, planning._date(date))
    if planning._date(date) < bill.date:
        raise ValidationError("Tanggal hubungan tidak boleh sebelum tanggal tagihan.")
    facts = _facts(org, bill, timezone.localdate())
    _assert_bill_fingerprint(facts, expected_bill_fingerprint)
    return _link(spend, actor, request, bill, coverage, facts)


def _match(spend, actor, request, manual_source_key, finance_event_id, amount):
    row = spend_position(
        organization=spend.organization, spend=spend, as_of=timezone.localdate()
    )
    if not row["current"]:
        raise ValidationError([i["message"] for i in row["issues"]])
    manual = next(
        (s for s in row["manual_payment_sources"] if s["key"] == manual_source_key),
        None,
    )
    finance = next(
        (
            s
            for s in row["finance_payment_sources"]
            if s["event_id"] == finance_event_id
        ),
        None,
    )
    if (
        not manual
        or not finance
        or amount > min(Decimal(manual["unmatched"]), Decimal(finance["unmatched"]))
    ):
        raise ValidationError(
            "Pilih dua bukti dari belanja yang sama; nilai tidak boleh melampaui sisanya."
        )
    if planning._date(request["date"]) < max(
        planning._date(manual["date"]),
        planning._date(finance.get("available_from", finance["date"])),
    ):
        raise ValidationError(
            "Tanggal pencocokan tidak boleh sebelum kedua bukti pembayaran."
        )
    kind, manual_id = manual_source_key.split(":")
    payment_kind, payment_id = finance_event_id.split(":")
    field = PAYMENT_FIELDS.get(payment_kind)
    if not field:
        raise ValidationError("Jenis pembayaran Finance tidak didukung.")
    movement = _movement(spend, actor, request, amount=amount)
    return _save(
        BudgetPaymentMatch(
            organization=spend.organization,
            spend=spend,
            created_by=actor,
            request=movement,
            date=request["date"],
            amount=amount,
            **{
                "manual_entry_id" if kind == "entry" else "manual_movement_id": int(
                    manual_id
                ),
                field: int(payment_id),
            },
            source_snapshot=finance,
            source_hash=finance["source_hash"],
        )
    )


@transaction.atomic
def match_spend_payment(
    *,
    organization,
    actor,
    spend,
    manual_source_key,
    finance_event_id,
    amount,
    date,
    reference,
    reason,
    evidence,
    expected_version,
    expected_fingerprint,
):
    org = planning._org(organization, actor)
    spend = _scope(BudgetSpend, spend, org, lock=True)
    amount = _positive(amount)
    request = _request(
        "match_payment",
        date=date,
        reference=reference,
        reason=reason,
        evidence=evidence,
        spend_id=spend.pk,
        manual_source_key=manual_source_key,
        finance_event_id=finance_event_id,
        amount=_money(amount),
    )
    old = _retry(org, request)
    if old:
        return old.payment_match
    _guard(spend, expected_version, expected_fingerprint)
    _chronology(spend, planning._date(date))
    return _match(spend, actor, request, manual_source_key, finance_event_id, amount)


@transaction.atomic
def sync_bill_budget(*, organization, bill, actor):
    """Finance calls after a complete event, under its Org-first transaction."""
    org_id = getattr(organization, "pk", organization)
    links = BudgetBillLink.objects.filter(
        organization_id=org_id,
        bill_id=getattr(bill, "pk", bill),
        voided_at__isnull=True,
    )
    if not links.exists():
        return None
    org = planning._org(organization, actor)
    link = links.select_for_update().select_related("spend", "bill").get()
    spend = _scope(BudgetSpend, link.spend_id, org, lock=True)
    _intact(link)
    facts = _facts(org, link.bill, timezone.localdate())
    latest = (
        spend.movements.filter(kind="finance_sync", payload__request__link_id=link.pk)
        .order_by("-pk")
        .first()
    )
    if latest:
        _intact(latest)
    previous_hash = (
        latest.payload["request"]["source_hash"] if latest else link.source_hash
    )
    if previous_hash == facts["fingerprint"]:
        return None
    reference = f"FIN-{link.pk}-{facts['fingerprint']}"
    request = _request(
        "finance_sync",
        date=timezone.localdate(),
        reference=reference,
        reason="Perubahan sumber Finance yang terposting.",
        evidence="Identitas jurnal dan bukti Finance diperiksa dalam transaksi yang sama.",
        spend_id=spend.pk,
        bill_id=link.bill_id,
        link_id=link.pk,
        source_hash=facts["fingerprint"],
    )
    old = _retry(org, request)
    if old:
        return old
    # Post valid Finance facts even when a draft shrank below selected overlap.
    # The canonical reader gates the inconsistent link for Owner correction;
    # this synchronization must not erase the residual or reject the posting.
    row = _movement(spend, actor, request, extra={"source_snapshot": facts})
    return row


def _void_record(obj, movement):
    if obj.voided_at:
        raise ValidationError("Catatan sudah ditarik melalui koreksi sebelumnya.")
    if planning._date(movement.date) < obj.date:
        raise ValidationError("Tanggal koreksi tidak boleh sebelum catatan asal.")
    obj.voided_at, obj.void_request = planning._date(movement.date), movement
    _save(obj)


def _recovery(
    org,
    actor,
    model,
    target,
    request,
    expected_version,
    expected_fingerprint,
    *,
    allow_source_repair=False,
):
    obj = _scope(model, target, org, lock=True)
    spend = _scope(BudgetSpend, obj.spend_id, org, lock=True)
    old = _retry(org, request)
    if old:
        return obj, spend, old
    _intact(obj)
    if allow_source_repair:
        planning._version(spend, expected_version)
        row = spend_position(organization=org, spend=spend, as_of=timezone.localdate())
        if row["fingerprint"] != expected_fingerprint:
            raise ValidationError(
                "Sumber belanja berubah. Muat ulang sebelum mengoreksi."
            )
    else:
        _guard(spend, expected_version, expected_fingerprint)
    _chronology(spend, planning._date(request["date"]))
    return obj, spend, None


def _replacement(value, kind):
    if value is None:
        return None
    fields = {
        "convert": {"from_kind", "to_kind", "amount"},
        "release": {"bucket", "amount"},
        "payment": {"amount"},
    }[kind]
    if not isinstance(value, dict) or set(value) != fields:
        raise ValidationError(
            "Isi pengganti tidak sesuai jenis catatan yang dikoreksi."
        )
    return {**value, "amount": _money(_positive(value["amount"]))}


@transaction.atomic
def correct_spend_movement(
    *,
    organization,
    actor,
    movement,
    replacement,
    date,
    reference,
    reason,
    evidence,
    expected_version,
    expected_fingerprint,
):
    org = planning._org(organization, actor, planning.APPROVERS)
    target = _scope(BudgetSpendMovement, movement, org, lock=True)
    if target.kind not in {"convert", "release", "payment"}:
        raise ValidationError(
            "Hanya perpindahan, pelepasan atau pembayaran dilaporkan yang dapat dikoreksi."
        )
    replacement = _replacement(replacement, target.kind)
    request = _request(
        "reverse",
        date=date,
        reference=reference,
        reason=reason,
        evidence=evidence,
        target_id=target.pk,
        replacement=replacement,
    )
    target, spend, old = _recovery(
        org,
        actor,
        BudgetSpendMovement,
        target,
        request,
        expected_version,
        expected_fingerprint,
    )
    if old:
        return old
    if BudgetSpendMovement.objects.filter(reverse_of=target).exists():
        raise ValidationError("Catatan sudah dikoreksi; gunakan catatan penggantinya.")
    row = spend_position(organization=org, spend=spend, as_of=timezone.localdate())
    if target.kind == "convert":
        source = next(
            (
                s
                for s in row["coverable_sources"]
                if s["key"] == f"movement:{target.pk}"
            ),
            None,
        )
        if not source or Decimal(source["available"]) != target.amount:
            raise ValidationError(
                "Hasil perpindahan sudah digunakan. Periksa hubungan dan perpindahan berikutnya terlebih dahulu."
            )
    if (
        target.kind == "payment"
        and spend.payment_matches.filter(
            manual_movement=target, voided_at__isnull=True
        ).exists()
    ):
        raise ValidationError(
            "Tarik pencocokan pembayaran terlebih dahulu sebelum mengoreksi pengamatan asal."
        )
    reverse = _movement(spend, actor, request, amount=target.amount, reverse_of=target)
    row = spend_position(organization=org, spend=spend, as_of=timezone.localdate())
    if not row["current"]:
        raise ValidationError([i["message"] for i in row["issues"]])
    if replacement:
        new_request = {**request, "kind": target.kind, "reference": f"{reference}:R"}
        if len(new_request["reference"]) > 120:
            raise ValidationError(
                "Referensi koreksi maksimal 118 karakter bila memakai pengganti."
            )
        if target.kind == "payment":
            amount = Decimal(replacement["amount"])
            if amount > Decimal(row["consumed"]) - Decimal(row["reported_payments"]):
                raise ValidationError("Pengganti pembayaran melebihi nilai belanja.")
            _movement(
                spend,
                actor,
                new_request,
                amount=amount,
                extra={"replaces_id": target.pk},
            )
        else:
            _transfer(
                spend,
                actor,
                new_request,
                row,
                replacement.get("from_kind", replacement.get("bucket")),
                replacement.get("to_kind", ""),
                Decimal(replacement["amount"]),
            )
    return reverse


def _link_recovery(
    *,
    organization,
    actor,
    link,
    bill,
    coverage,
    date,
    reference,
    reason,
    evidence,
    expected_version,
    expected_fingerprint,
    expected_bill_fingerprint=None,
):
    org = planning._org(organization, actor, planning.APPROVERS)
    target = _scope(BudgetBillLink, link, org, lock=True)
    if bill is not None:
        bill = _scope(Bill, bill, org, lock=True)
        coverage = _coverage(coverage)
    request = _request(
        "correct_link" if bill else "void_link",
        date=date,
        reference=reference,
        reason=reason,
        evidence=evidence,
        link_id=target.pk,
        bill_id=bill.pk if bill else None,
        coverage=coverage if bill else [],
    )
    target, spend, old = _recovery(
        org,
        actor,
        BudgetBillLink,
        target,
        request,
        expected_version,
        expected_fingerprint,
        allow_source_repair=True,
    )
    if old:
        return old.bill_link if bill else old
    facts = _facts(org, bill, timezone.localdate()) if bill else None
    if bill:
        _assert_bill_fingerprint(facts, expected_bill_fingerprint)
    if (
        spend.payment_matches.filter(voided_at__isnull=True)
        .filter(
            Q(bill_payment__bill_id=target.bill_id)
            | Q(withholding_settlement__bill_id=target.bill_id)
            | Q(tax_remittance__obligation__source_bill_id=target.bill_id)
            | Q(supplier_advance_application__bill_id=target.bill_id)
        )
        .exists()
    ):
        raise ValidationError(
            "Tarik pencocokan pembayaran terkait sebelum mengoreksi hubungan tagihan."
        )
    movement = _movement(spend, actor, request)
    _void_record(target, movement)
    if bill:
        if planning._date(date) < bill.date:
            raise ValidationError(
                "Tanggal koreksi tidak boleh sebelum tagihan pengganti."
            )
        # Reuse the one immutable recovery request as the replacement identity.
        if BudgetBillLink.objects.filter(bill=bill, voided_at__isnull=True).exists():
            raise ValidationError("Tagihan pengganti sudah terhubung ke belanja lain.")
        row = spend_position(organization=org, spend=spend, as_of=timezone.localdate())
        options = {s["key"]: s for s in row["coverable_sources"]}
        if not row["current"] or any(
            i["source_key"] not in options
            or Decimal(i["amount"]) > Decimal(options[i["source_key"]]["available"])
            for i in coverage
        ):
            raise ValidationError(
                "Kapasitas pengganti tidak tersedia setelah hubungan lama ditarik."
            )
        if sum((Decimal(i["amount"]) for i in coverage), ZERO) > bill.amount:
            raise ValidationError(
                "Nilai pengganti melebihi tagihan; pelepasan residual memerlukan tindakan terpisah."
            )
        return _save(
            BudgetBillLink(
                organization=org,
                created_by=actor,
                spend=spend,
                bill=bill,
                request=movement,
                date=date,
                coverage=coverage,
                source_snapshot=facts,
                source_hash=facts["fingerprint"],
            )
        )
    return movement


@transaction.atomic
def void_spend_bill_link(**kwargs):
    return _link_recovery(bill=None, coverage=None, **kwargs)


@transaction.atomic
def correct_spend_bill_link(**kwargs):
    return _link_recovery(**kwargs)


def _match_recovery(
    *,
    organization,
    actor,
    match,
    manual_source_key,
    finance_event_id,
    amount,
    date,
    reference,
    reason,
    evidence,
    expected_version,
    expected_fingerprint,
):
    org = planning._org(organization, actor, planning.APPROVERS)
    target = _scope(BudgetPaymentMatch, match, org, lock=True)
    amount = _positive(amount) if amount is not None else None
    request = _request(
        "correct_match" if amount else "void_match",
        date=date,
        reference=reference,
        reason=reason,
        evidence=evidence,
        match_id=target.pk,
        manual_source_key=manual_source_key,
        finance_event_id=finance_event_id,
        amount=_money(amount) if amount else None,
    )
    target, spend, old = _recovery(
        org,
        actor,
        BudgetPaymentMatch,
        target,
        request,
        expected_version,
        expected_fingerprint,
        allow_source_repair=True,
    )
    if old:
        return old.payment_match if amount else old
    movement = _movement(spend, actor, request)
    _void_record(target, movement)
    if amount:
        # Create the replacement from current capacities after the atomic void.
        row = spend_position(organization=org, spend=spend, as_of=timezone.localdate())
        manual = next(
            (s for s in row["manual_payment_sources"] if s["key"] == manual_source_key),
            None,
        )
        finance = next(
            (
                s
                for s in row["finance_payment_sources"]
                if s["event_id"] == finance_event_id
            ),
            None,
        )
        if (
            not row["current"]
            or not manual
            or not finance
            or amount > min(Decimal(manual["unmatched"]), Decimal(finance["unmatched"]))
        ):
            raise ValidationError(
                "Pengganti pencocokan melebihi kapasitas dua sumber pembayaran."
            )
        if planning._date(date) < max(
            planning._date(manual["date"]),
            planning._date(finance.get("available_from", finance["date"])),
        ):
            raise ValidationError(
                "Tanggal pengganti tidak boleh sebelum kedua bukti pembayaran."
            )
        kind, manual_id = manual_source_key.split(":")
        payment_kind, payment_id = finance_event_id.split(":")
        field = PAYMENT_FIELDS[payment_kind]
        return _save(
            BudgetPaymentMatch(
                organization=org,
                created_by=actor,
                spend=spend,
                request=movement,
                date=date,
                amount=amount,
                **{
                    "manual_entry_id" if kind == "entry" else "manual_movement_id": int(
                        manual_id
                    ),
                    field: int(payment_id),
                },
                source_snapshot=finance,
                source_hash=finance["source_hash"],
            )
        )
    return movement


@transaction.atomic
def void_spend_payment_match(**kwargs):
    return _match_recovery(
        manual_source_key=None, finance_event_id=None, amount=None, **kwargs
    )


@transaction.atomic
def correct_spend_payment_match(**kwargs):
    return _match_recovery(**kwargs)
