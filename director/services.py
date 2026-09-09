"""Controlled planning workflows. No bank, tax, advertising, or ledger writes."""

import json
import re
from datetime import date as Date, datetime
from decimal import Decimal, InvalidOperation

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from core.audit import record
from core.models import Membership, Organization
from .models import Budget, BudgetEntry, CashPlan, CashPlanLine, Decision, MarketingObservation, Objective, Scenario, ZERO


WRITERS = {"owner", "director", "finance"}
APPROVERS = {"owner", "director"}
MARKETERS = WRITERS | {"marketing"}
OBJECTIVE_FIELDS = {"title", "metric_key", "target", "period_start", "period_end", "owner_name", "status", "progress_notes"}
DECISION_FIELDS = {"title", "problem", "evidence", "option", "risks", "owner_name", "due_date", "review_date", "amount"}
BUDGET_FIELDS = {"title", "year", "month", "channel", "amount"}
CASH_FIELDS = {"title", "as_of", "opening_balance", "reserve", "unknown_obligations"}
CASH_LINE_FIELDS = {"direction", "amount", "date", "category", "description", "source_reference", "evidence"}
MARKETING_FIELDS = {"date", "channel", "spend", "leads", "paid_orders", "reported_value", "source", "reference", "basis"}
DATE_FIELDS = {"period_start", "period_end", "due_date", "review_date", "as_of", "date"}
MONEY_FIELDS = {"target", "amount", "opening_balance", "reserve", "spend", "reported_value"}


def _authorize(organization, actor, roles=WRITERS):
    if not getattr(actor, "is_authenticated", False) or not Membership.objects.filter(
        organization=organization, user_id=actor.pk, user__is_active=True, role__in=roles
    ).exists():
        raise PermissionDenied("Akun aktif dengan peran yang sesuai diperlukan untuk tindakan ini.")


def _org(organization, actor, roles=WRITERS):
    try:
        org = Organization.objects.select_for_update().get(pk=getattr(organization, "pk", organization))
    except (Organization.DoesNotExist, ValueError, TypeError) as exc:
        raise ValidationError("Perusahaan tidak ditemukan.") from exc
    _authorize(org, actor, roles)
    return org


def _scoped(model, value, org):
    try:
        return model.objects.select_for_update().get(organization=org, pk=getattr(value, "pk", value))
    except (model.DoesNotExist, ValueError, TypeError) as exc:
        raise ValidationError("Catatan tidak tersedia untuk perusahaan ini.") from exc


def _date(value):
    try:
        if isinstance(value, Date) and not isinstance(value, datetime):
            result = value
        elif isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            result = Date.fromisoformat(value)
        else:
            raise ValueError
        if not 2000 <= result.year <= 2200:
            raise ValueError
        return result
    except ValueError as exc:
        raise ValidationError("Tanggal harus valid dalam rentang tahun 2000–2200.") from exc


def _money(value):
    try:
        if isinstance(value, (float, bool)):
            raise InvalidOperation
        amount = Decimal(value)
        if not amount.is_finite() or amount != amount.quantize(Decimal("0.01")) or abs(amount) >= Decimal("1000000000000000000"):
            raise InvalidOperation
        return amount.quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationError("Jumlah harus berupa angka dengan maksimal dua angka desimal.") from exc


def _fields(data, allowed):
    if set(data) - allowed:
        raise ValidationError("Ada kolom yang tidak boleh diubah melalui tindakan ini.")
    result = dict(data)
    for key, value in result.items():
        if key in DATE_FIELDS:
            if value in (None, "") and key in {"due_date", "review_date"}:
                result[key] = None
            else:
                result[key] = _date(value)
        elif key in MONEY_FIELDS:
            if value in (None, "") and key in {"target", "spend", "reported_value", "amount"}:
                result[key] = None
            else:
                result[key] = _money(value)
        elif key in {"year", "month", "leads", "paid_orders"}:
            if value in (None, "") and key in {"leads", "paid_orders"}:
                result[key] = None
            elif type(value) is not int or value < 0:
                raise ValidationError("Jumlah dan periode harus berupa bilangan bulat nonnegatif.")
        elif not isinstance(value, str) or len(value) > 12000:
            raise ValidationError("Teks isian tidak valid atau terlalu panjang.")
    return result


def _save(obj):
    obj._service_write = True
    obj.save()
    return obj


def _version(obj, expected):
    if type(expected) is not int or expected != obj.version:
        raise ValidationError("Versi telah berubah. Muat ulang catatan sebelum melanjutkan.")


def _reason(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        raise ValidationError("Alasan wajib diisi, maksimal 500 karakter.")
    return value.strip()


def _notes(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 4000:
        raise ValidationError("Catatan wajib diisi, maksimal 4000 karakter.")
    return value.strip()


def _self_approval(org, actor, conflict, reason):
    if not isinstance(reason, str) or len(reason) > 1000:
        raise ValidationError("Alasan pengecualian harus berupa teks maksimal 1000 karakter.")
    if conflict:
        if not reason.strip():
            raise ValidationError("Persetujuan oleh orang yang sama memerlukan alasan pengecualian tim kecil yang eksplisit.")
        _authorize(org, actor, {"owner"})


def _snapshot(obj, fields):
    return {key: str(getattr(obj, key)) if isinstance(getattr(obj, key), (Decimal, Date)) else getattr(obj, key) for key in sorted(fields)}


def _audit(org, actor, action, obj, **detail):
    record(org, actor, f"director.{action}", obj=obj, detail=detail)


@transaction.atomic
def create_objective(*, organization, actor, **fields):
    org = _org(organization, actor)
    obj = _save(Objective(organization=org, created_by=actor, **_fields(fields, OBJECTIVE_FIELDS)))
    _audit(org, actor, "objective.created", obj, values=_snapshot(obj, OBJECTIVE_FIELDS))
    return obj


@transaction.atomic
def update_objective(*, organization, actor, objective, expected_version, **fields):
    org = _org(organization, actor)
    obj = _scoped(Objective, objective, org)
    _version(obj, expected_version)
    before = _snapshot(obj, OBJECTIVE_FIELDS)
    for key, value in _fields(fields, OBJECTIVE_FIELDS).items():
        setattr(obj, key, value)
    if obj.status == "completed" and not obj.progress_notes.strip():
        raise ValidationError("Catat hasil sasaran sebelum menandai selesai.")
    obj.version += 1
    _save(obj)
    _audit(org, actor, "objective.updated", obj, before=before, after=_snapshot(obj, OBJECTIVE_FIELDS), version=obj.version)
    return obj


@transaction.atomic
def create_decision(*, organization, actor, **fields):
    org = _org(organization, actor)
    obj = _save(Decision(organization=org, created_by=actor, **_fields(fields, DECISION_FIELDS)))
    _audit(org, actor, "decision.created", obj, version=obj.version)
    return obj


@transaction.atomic
def update_decision(*, organization, actor, decision, expected_version, **fields):
    org = _org(organization, actor)
    obj = _scoped(Decision, decision, org)
    _version(obj, expected_version)
    if obj.status != "draft":
        raise ValidationError("Proposal yang sudah diajukan tidak dapat diedit; buat proposal baru sebagai revisi.")
    before = _snapshot(obj, DECISION_FIELDS)
    for key, value in _fields(fields, DECISION_FIELDS).items():
        setattr(obj, key, value)
    obj.version += 1
    _save(obj)
    _audit(org, actor, "decision.updated", obj, before=before, after=_snapshot(obj, DECISION_FIELDS), version=obj.version)
    return obj


@transaction.atomic
def transition_decision(*, organization, actor, decision, action, expected_version, notes="", self_approval_reason=""):
    action_roles = {
        "submit": WRITERS,
        "review": {"owner", "finance"},
        "reject": APPROVERS,
        "approve": APPROVERS,
        "activate": APPROVERS,
        "complete": APPROVERS,
    }
    if not isinstance(action, str) or action not in action_roles:
        raise ValidationError("Tindakan keputusan tidak dikenal.")
    org = _org(organization, actor, action_roles[action])
    obj = _scoped(Decision, decision, org)
    _version(obj, expected_version)
    transitions = {"submit": ("draft", "submitted"), "review": ("submitted", "reviewed"),
                   "approve": ("reviewed", "approved"), "activate": ("approved", "active"), "complete": ("active", "completed")}
    if action == "reject":
        if obj.status not in {"submitted", "reviewed"}:
            raise ValidationError("Hanya proposal dalam pemeriksaan yang dapat ditolak.")
        target = "rejected"
        obj.review_notes = _notes(notes)
    else:
        if action not in transitions or obj.status != transitions[action][0]:
            raise ValidationError("Perubahan status keputusan tidak sesuai urutan pemeriksaan.")
        target = transitions[action][1]
    if action == "submit":
        if any(not getattr(obj, key).strip() for key in ("problem", "evidence", "option", "risks", "owner_name")):
            raise ValidationError("Lengkapi masalah, bukti, pilihan, risiko, dan penanggung jawab sebelum mengajukan.")
    if action == "review":
        obj.review_notes = _notes(notes)
        obj.reviewed_by = actor
    if action == "approve":
        _self_approval(org, actor, actor.pk in {obj.created_by_id, obj.reviewed_by_id}, self_approval_reason)
        obj.approval_notes = _notes(notes)
        obj.approved_by = actor
    if action in {"activate", "complete"}:
        obj.execution_notes = _notes(notes)
    obj.status = target
    obj.version += 1
    _save(obj)
    _audit(org, actor, f"decision.{action}", obj, version=obj.version, notes=notes,
           self_approval_reason=self_approval_reason, execution_basis="Catatan tindak lanjut manusia; bukan otorisasi pembayaran atau aktivasi kampanye otomatis.")
    return obj


@transaction.atomic
def create_budget(*, organization, actor, **fields):
    org = _org(organization, actor)
    obj = _save(Budget(organization=org, created_by=actor, **_fields(fields, BUDGET_FIELDS)))
    _audit(org, actor, "budget.created", obj, version=obj.version)
    return obj


@transaction.atomic
def update_budget(*, organization, actor, budget, expected_version, **fields):
    org = _org(organization, actor)
    obj = _scoped(Budget, budget, org)
    _version(obj, expected_version)
    if obj.status != "draft":
        raise ValidationError("Budget yang telah diajukan tidak dapat ditimpa.")
    before = _snapshot(obj, BUDGET_FIELDS)
    for key, value in _fields(fields, BUDGET_FIELDS).items():
        setattr(obj, key, value)
    obj.version += 1
    _save(obj)
    _audit(org, actor, "budget.updated", obj, before=before, after=_snapshot(obj, BUDGET_FIELDS), version=obj.version)
    return obj


@transaction.atomic
def transition_budget(*, organization, actor, budget, action, expected_version, notes="", self_approval_reason=""):
    org = _org(organization, actor, APPROVERS if action == "approve" else WRITERS)
    obj = _scoped(Budget, budget, org)
    _version(obj, expected_version)
    if action == "submit" and obj.status == "draft":
        obj.status = "submitted"
    elif action == "approve" and obj.status == "submitted":
        _self_approval(org, actor, actor.pk == obj.created_by_id, self_approval_reason)
        obj.approval_notes = _notes(notes)
        obj.approved_by = actor
        obj.status = "approved"
    else:
        raise ValidationError("Perubahan status budget tidak sesuai urutan.")
    obj.version += 1
    _save(obj)
    _audit(org, actor, f"budget.{action}", obj, version=obj.version, notes=notes,
           self_approval_reason=self_approval_reason, basis="Otorisasi envelope budget; bukan bukti kecukupan kas atau izin pembayaran.")
    return obj


def _entry(org, actor, budget, *, kind, amount, date, description, reference, idempotency_key, replaces=None):
    if not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 100:
        raise ValidationError("Kunci permintaan budget wajib diisi, maksimal 100 karakter.")
    fields = _fields({"kind": kind, "amount": amount, "date": date, "description": description, "reference": reference}, {"kind", "amount", "date", "description", "reference"})
    expected = {**fields, "budget_id": budget.pk, "replaces_id": replaces.pk if replaces else None}
    old = BudgetEntry.objects.filter(organization=org, idempotency_key=idempotency_key).first()
    if old:
        if any(getattr(old, key) != value for key, value in expected.items()):
            raise ValidationError("Kunci permintaan sudah digunakan untuk entri berbeda.")
        return old
    if budget.status != "approved":
        raise ValidationError("Entri hanya dapat dicatat pada budget yang disetujui.")
    if fields["amount"] is None or fields["amount"] <= ZERO:
        raise ValidationError("Nominal entri harus lebih besar dari nol.")
    if kind != "payment" and (fields["date"].year, fields["date"].month) != (budget.year, budget.month):
        raise ValidationError("Tanggal biaya/komitmen harus berada pada periode budget; pembayaran boleh terjadi di periode lain.")
    if kind != "payment" and fields["amount"] > budget.available:
        raise ValidationError("Entri melebihi sisa otorisasi budget.")
    obj = _save(BudgetEntry(organization=org, created_by=actor, budget=budget, replaces=replaces,
                            idempotency_key=idempotency_key, **fields))
    _audit(org, actor, "budget.entry_added", obj, kind=kind, amount=str(obj.amount), budget_id=budget.pk,
           available=str(budget.available), replaces_id=obj.replaces_id)
    return obj


@transaction.atomic
def add_budget_entry(*, organization, actor, budget, kind, amount, date, description, reference="", idempotency_key):
    org = _org(organization, actor)
    budget = _scoped(Budget, budget, org)
    return _entry(org, actor, budget, kind=kind, amount=amount, date=date, description=description,
                  reference=reference, idempotency_key=idempotency_key)


@transaction.atomic
def void_budget_entry(*, organization, actor, entry, reason):
    org = _org(organization, actor)
    obj = _scoped(BudgetEntry, entry, org)
    _scoped(Budget, obj.budget_id, org)
    reason = _reason(reason)
    if obj.voided_at:
        if obj.void_reason != reason:
            raise ValidationError("Entri telah dibatalkan dengan alasan berbeda.")
        return obj
    obj.voided_at, obj.void_reason = timezone.now(), reason
    _save(obj)
    _audit(org, actor, "budget.entry_voided", obj, reason=reason)
    return obj


@transaction.atomic
def replace_budget_entry(*, organization, actor, entry, reason, kind, amount, date, description, reference="", idempotency_key):
    org = _org(organization, actor)
    old = _scoped(BudgetEntry, entry, org)
    budget = _scoped(Budget, old.budget_id, org)
    duplicate = BudgetEntry.objects.filter(organization=org, idempotency_key=idempotency_key).first()
    if duplicate:
        return _entry(org, actor, budget, kind=kind, amount=amount, date=date, description=description,
                      reference=reference, idempotency_key=idempotency_key, replaces=old)
    if old.voided_at:
        raise ValidationError("Entri yang sudah dibatalkan tidak dapat diganti lagi.")
    void_budget_entry(organization=org, actor=actor, entry=old, reason=reason)
    return _entry(org, actor, budget, kind=kind, amount=amount, date=date, description=description,
                  reference=reference, idempotency_key=idempotency_key, replaces=old)


@transaction.atomic
def create_cash_plan(*, organization, actor, **fields):
    org = _org(organization, actor)
    obj = _save(CashPlan(organization=org, created_by=actor, **_fields(fields, CASH_FIELDS)))
    _audit(org, actor, "cash_plan.created", obj, basis="assumed", values=_snapshot(obj, CASH_FIELDS))
    return obj


@transaction.atomic
def update_cash_plan(*, organization, actor, plan, expected_version, **fields):
    org = _org(organization, actor)
    obj = _scoped(CashPlan, plan, org)
    _version(obj, expected_version)
    before = _snapshot(obj, CASH_FIELDS)
    values = _fields(fields, CASH_FIELDS)
    if "as_of" in values and obj.lines.filter(status="planned", date__lt=values["as_of"]).exists():
        raise ValidationError("Selesaikan atau pindahkan baris sebelum cutoff baru; jangan menyembunyikannya saat mengganti saldo asumsi.")
    for key, value in values.items():
        setattr(obj, key, value)
    obj.version += 1
    _save(obj)
    _audit(org, actor, "cash_plan.updated", obj, before=before, after=_snapshot(obj, CASH_FIELDS), version=obj.version)
    return obj


def _cash_line(org, actor, plan, fields, replaces=None):
    fields = _fields(fields, CASH_LINE_FIELDS)
    if fields.get("date") is not None and fields["date"] < plan.as_of:
        raise ValidationError("Tanggal rencana tidak boleh sebelum saldo pembuka asumsi.")
    obj = _save(CashPlanLine(organization=org, created_by=actor, plan=plan, replaces=replaces, **fields))
    plan.version += 1
    _save(plan)
    _audit(org, actor, "cash_plan.line_added", obj, plan_id=plan.pk, plan_version=plan.version, replaces_id=obj.replaces_id)
    return obj


@transaction.atomic
def add_cash_line(*, organization, actor, plan, **fields):
    org = _org(organization, actor)
    return _cash_line(org, actor, _scoped(CashPlan, plan, org), fields)


@transaction.atomic
def void_cash_line(*, organization, actor, line, reason):
    org = _org(organization, actor)
    obj = _scoped(CashPlanLine, line, org)
    plan = _scoped(CashPlan, obj.plan_id, org)
    reason = _reason(reason)
    if obj.status == "void":
        if obj.void_reason != reason:
            raise ValidationError("Baris kas telah dibatalkan dengan alasan berbeda.")
        return obj
    obj.status, obj.void_reason = "void", reason
    _save(obj)
    plan.version += 1
    _save(plan)
    _audit(org, actor, "cash_plan.line_voided", obj, reason=reason, plan_version=plan.version)
    return obj


@transaction.atomic
def replace_cash_line(*, organization, actor, line, reason, **fields):
    org = _org(organization, actor)
    old = _scoped(CashPlanLine, line, org)
    if old.status == "void":
        raise ValidationError("Baris yang sudah dibatalkan tidak dapat diganti lagi.")
    void_cash_line(organization=org, actor=actor, line=old, reason=reason)
    return _cash_line(org, actor, _scoped(CashPlan, old.plan_id, org), fields, replaces=old)


@transaction.atomic
def save_scenario(*, organization, actor, title, kind, inputs):
    org = _org(organization, actor)
    if kind != "price_cost" or not isinstance(inputs, dict):
        raise ValidationError("Jenis atau input simulasi tidak didukung.")
    allowed = {"quantity", "current_price", "current_cost", "variable_cost", "new_price", "new_cost", "new_variable_cost", "new_quantity", "additional_marketing_spend"}
    if set(inputs) - allowed:
        raise ValidationError("Input simulasi berisi kolom yang tidak didukung.")
    from .calculators import price_cost_scenario
    try:
        results = price_cost_scenario(**inputs)
        normalized = json.loads(json.dumps({key: str(value) if isinstance(value, Decimal) else value for key, value in inputs.items()}, allow_nan=False))
    except (ValueError, TypeError, InvalidOperation) as exc:
        raise ValidationError("Input simulasi tidak valid.") from exc
    obj = _save(Scenario(organization=org, created_by=actor, title=title, kind=kind,
                         inputs=normalized, results=results, formula_version=results["formula_version"]))
    _audit(org, actor, "scenario.saved", obj, formula_version=obj.formula_version, basis="assumptions")
    return obj


def _marketing(org, actor, fields, replaces=None):
    fields = _fields({"spend": None, "leads": None, "paid_orders": None, "reported_value": None, "basis": "reported", **fields}, MARKETING_FIELDS)
    if not any(fields.get(key) is not None for key in ("spend", "leads", "paid_orders", "reported_value")):
        raise ValidationError("Isi setidaknya satu pengamatan; nilai kosong bukan nol.")
    duplicate = MarketingObservation.objects.filter(organization=org, date=fields.get("date"), channel=fields.get("channel"),
                                                     source=fields.get("source"), reference=fields.get("reference"), status="active").first()
    if duplicate:
        compare = {**fields, "replaces_id": replaces.pk if replaces else None}
        if any(getattr(duplicate, key) != value for key, value in compare.items()):
            raise ValidationError("Referensi sumber sudah memiliki pengamatan berbeda; gunakan penggantian.")
        return duplicate
    obj = _save(MarketingObservation(organization=org, created_by=actor, replaces=replaces, **fields))
    _audit(org, actor, "marketing.observation_created", obj, basis=obj.basis, replaces_id=obj.replaces_id)
    return obj


@transaction.atomic
def create_marketing_observation(*, organization, actor, **fields):
    org = _org(organization, actor, MARKETERS)
    return _marketing(org, actor, fields)


@transaction.atomic
def void_marketing_observation(*, organization, actor, observation, reason):
    org = _org(organization, actor, MARKETERS)
    obj = _scoped(MarketingObservation, observation, org)
    reason = _reason(reason)
    if obj.status == "void":
        if obj.void_reason != reason:
            raise ValidationError("Pengamatan sudah dibatalkan dengan alasan berbeda.")
        return obj
    obj.status, obj.void_reason = "void", reason
    _save(obj)
    _audit(org, actor, "marketing.observation_voided", obj, reason=reason)
    return obj


@transaction.atomic
def replace_marketing_observation(*, organization, actor, observation, reason, **fields):
    org = _org(organization, actor, MARKETERS)
    old = _scoped(MarketingObservation, observation, org)
    if old.status == "void":
        raise ValidationError("Pengamatan yang dibatalkan tidak dapat diganti lagi.")
    void_marketing_observation(organization=org, actor=actor, observation=old, reason=reason)
    return _marketing(org, actor, fields, replaces=old)
