"""Audited marketing commands. Financial links are read-only references."""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import re

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from core.audit import record
from core.models import Membership, Organization
from finance.models import Invoice, Party, Product
from .models import AdDailyObservation, Campaign, Lead, LeadStageEvent, MarketingAction, MarketingAIPolicy, TeamMember


WRITERS = {"owner", "director", "finance", "marketing"}
FINANCE_LINKERS = {"owner", "director", "finance"}
MEMBER_FIELDS = {"name", "discipline", "team", "active"}
CAMPAIGN_FIELDS = {"name", "channel", "product", "owner", "objective", "start_date", "end_date", "status"}
AD_FIELDS = {"campaign", "date", "ad_name", "audience", "region", "spend", "impressions", "clicks", "source", "reference"}
LEAD_FIELDS = {"reference", "name", "received_at", "campaign", "channel", "owner", "product", "party", "segment", "region", "stage", "first_response_at", "next_follow_up", "notes"}
LEAD_UPDATES = {"stage", "first_response_at", "next_follow_up", "notes"}
ACTION_FIELDS = {"title", "evidence", "description", "owner", "due_date", "metric_key"}
DATES = {"date", "start_date", "end_date", "next_follow_up", "due_date"}
TIMES = {"received_at", "first_response_at"}
REFERENCES = {"owner": TeamMember, "campaign": Campaign, "product": Product, "party": Party}


def _org(organization, actor, roles=WRITERS):
    value = getattr(organization, "pk", organization)
    if type(value) is not int or value < 1:
        raise ValidationError("Perusahaan tersimpan wajib dipilih.")
    try:
        org = Organization.objects.select_for_update().get(pk=value)
    except Organization.DoesNotExist as exc:
        raise ValidationError("Perusahaan tidak ditemukan.") from exc
    if not getattr(actor, "is_authenticated", False) or not Membership.objects.filter(organization=org, user=actor, user__is_active=True, role__in=roles).exists():
        raise PermissionDenied("Akun aktif dengan peran yang sesuai diperlukan.")
    return org


def _scoped(model, value, org):
    value = getattr(value, "pk", value)
    if type(value) is not int or value < 1:
        raise ValidationError("Pilih catatan terkait yang sah.")
    try:
        return model.objects.select_for_update().get(organization=org, pk=value)
    except model.DoesNotExist as exc:
        raise ValidationError("Catatan terkait tidak tersedia untuk perusahaan ini.") from exc


def _decimal(value, places=2, limit="1000000000000000000"):
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (float, bool)):
            raise InvalidOperation
        number = Decimal(value)
        precision = Decimal(1).scaleb(-places)
        if not number.is_finite() or number < 0 or number >= Decimal(limit) or number != number.quantize(precision):
            raise InvalidOperation
        return number.quantize(precision)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationError(f"Nilai harus angka nonnegatif dengan maksimal {places} desimal.") from exc


def _fields(data, allowed, org):
    if set(data) - allowed:
        raise ValidationError("Ada kolom yang tidak boleh diubah melalui tindakan ini.")
    result = {}
    for key, value in data.items():
        if key in REFERENCES:
            result[key] = None if value in (None, "") else _scoped(REFERENCES[key], value, org)
            if key == "owner" and result[key] is not None and not result[key].active:
                raise ValidationError("Penanggung jawab harus anggota tim aktif.")
        elif key in DATES:
            if value in (None, "") and key != "date":
                result[key] = None
                continue
            try:
                parsed = date.fromisoformat(value) if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) else value
                if not isinstance(parsed, date) or isinstance(parsed, datetime) or not 2000 <= parsed.year <= 2200:
                    raise ValueError
                result[key] = parsed
            except (TypeError, ValueError) as exc:
                raise ValidationError("Tanggal harus YYYY-MM-DD, dalam tahun 2000–2200.") from exc
        elif key in TIMES:
            if value in (None, "") and key == "first_response_at":
                result[key] = None
                continue
            try:
                parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
                if not isinstance(parsed, datetime) or timezone.is_naive(parsed) or not 2000 <= parsed.year <= 2200:
                    raise ValueError
                result[key] = parsed
            except (TypeError, ValueError) as exc:
                raise ValidationError("Waktu harus valid dan menyertakan zona waktu.") from exc
        elif key == "spend":
            result[key] = _decimal(value)
        elif key in {"impressions", "clicks"}:
            if value in (None, ""):
                result[key] = None
            elif type(value) is not int or not 0 <= value <= 2147483647:
                raise ValidationError("Jumlah harus bilangan bulat nonnegatif yang valid.")
            else:
                result[key] = value
        elif key == "active":
            if type(value) is not bool:
                raise ValidationError("Status anggota tim tidak valid.")
            result[key] = value
        else:
            if not isinstance(value, str) or "\x00" in value or len(value) > 4000:
                raise ValidationError("Teks isian tidak valid atau terlalu panjang.")
            if key not in {"notes", "evidence", "description", "outcome"} and any(ord(character) < 32 or ord(character) == 127 for character in value):
                raise ValidationError("Nama, label, dan referensi harus berupa teks satu baris.")
            result[key] = value.strip()
    return result


def _save(obj):
    obj._service_write = True
    obj.save()
    return obj


def _version(obj, expected):
    if type(expected) is not int or obj.version != expected:
        raise ValidationError("Catatan telah berubah. Muat ulang sebelum menyimpan.")


def _snapshot(obj, fields):
    result = {}
    for key in fields:
        value = getattr(obj, key)
        if hasattr(value, "pk"):
            value = value.pk
        elif isinstance(value, (date, datetime, Decimal)):
            value = str(value)
        result[key] = value
    return result


def _audit(org, actor, action, obj, **detail):
    record(org, actor, "marketing." + action, obj, detail)


@transaction.atomic
def create_member(*, organization, actor, **fields):
    org = _org(organization, actor)
    obj = _save(TeamMember(organization=org, created_by=actor, **_fields(fields, MEMBER_FIELDS, org)))
    _audit(org, actor, "member.created", obj, values=_snapshot(obj, MEMBER_FIELDS))
    return obj


@transaction.atomic
def create_campaign(*, organization, actor, **fields):
    org = _org(organization, actor)
    obj = _save(Campaign(organization=org, created_by=actor, **_fields(fields, CAMPAIGN_FIELDS, org)))
    _audit(org, actor, "campaign.created", obj, values=_snapshot(obj, CAMPAIGN_FIELDS))
    return obj


@transaction.atomic
def save_ad_observation(*, organization, actor, observation=None, expected_version=None, reason="", **fields):
    org = _org(organization, actor)
    data = _fields(fields, AD_FIELDS, org)
    if observation is not None:
        obj = _scoped(AdDailyObservation, observation, org)
        _version(obj, expected_version)
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
            raise ValidationError("Tuliskan alasan koreksi, maksimal 500 karakter.")
        before = _snapshot(obj, AD_FIELDS)
        for key, value in data.items():
            setattr(obj, key, value)
        obj.version += 1
        _save(obj)
        _audit(org, actor, "ad.corrected", obj, before=before, after=_snapshot(obj, AD_FIELDS), reason=reason.strip(), version=obj.version)
        return obj
    obj = AdDailyObservation(organization=org, created_by=actor, **data)
    if obj.campaign_id is None:
        raise ValidationError({"campaign": "Pilih kampanye untuk laporan iklan."})
    existing = AdDailyObservation.objects.filter(organization=org, reference=obj.reference).first()
    if existing:
        if _snapshot(existing, AD_FIELDS) != _snapshot(obj, AD_FIELDS):
            raise ValidationError("Referensi laporan sudah mempunyai nilai berbeda. Gunakan koreksi dengan versi dan alasan.")
        return existing
    _save(obj)
    _audit(org, actor, "ad.created", obj, values=_snapshot(obj, AD_FIELDS))
    return obj


@transaction.atomic
def create_lead(*, organization, actor, **fields):
    org = _org(organization, actor)
    data = _fields(fields, LEAD_FIELDS, org)
    campaign = data.get("campaign")
    if campaign:
        if data.get("channel") and data["channel"] != campaign.channel:
            raise ValidationError("Kanal harus sesuai kampanye asal.")
        data["channel"] = campaign.channel
        if data.get("product") is None:
            data["product"] = campaign.product
    obj = _save(Lead(organization=org, created_by=actor, **data))
    _save(LeadStageEvent(organization=org, created_by=actor, lead=obj, from_stage="", to_stage=obj.stage, occurred_at=timezone.now()))
    _audit(org, actor, "lead.created", obj, values=_snapshot(obj, LEAD_FIELDS), stage_time_basis="recorded_now")
    return obj


@transaction.atomic
def update_lead(*, organization, actor, lead, expected_version, **fields):
    org = _org(organization, actor)
    obj = _scoped(Lead, lead, org)
    _version(obj, expected_version)
    data = _fields(fields, LEAD_UPDATES, org)
    before = _snapshot(obj, LEAD_UPDATES)
    for key, value in data.items():
        setattr(obj, key, value)
    obj.version += 1
    _save(obj)
    if obj.stage != before["stage"]:
        _save(LeadStageEvent(organization=org, created_by=actor, lead=obj, from_stage=before["stage"], to_stage=obj.stage, occurred_at=timezone.now()))
    _audit(org, actor, "lead.updated", obj, before=before, after=_snapshot(obj, LEAD_UPDATES), version=obj.version)
    return obj


@transaction.atomic
def link_invoice(*, organization, actor, lead, invoice, expected_version, confirm_identity=False):
    org = _org(organization, actor, FINANCE_LINKERS)
    obj = _scoped(Lead, lead, org)
    _version(obj, expected_version)
    invoice = _scoped(Invoice, invoice, org)
    if obj.invoice_id == invoice.pk:
        return obj
    if obj.invoice_id:
        raise ValidationError("Prospek sudah terhubung ke invoice. Penggantian memerlukan alur koreksi Finance tersendiri.")
    if invoice.status not in (Invoice.Status.ISSUED, Invoice.Status.DELIVERED):
        raise ValidationError("Pilih invoice yang sudah diterbitkan atau layanannya selesai.")
    if type(confirm_identity) is not bool:
        raise ValidationError("Konfirmasi identitas tidak valid.")
    missing_identity = not obj.product_id or not obj.party_id
    if missing_identity and not confirm_identity:
        raise ValidationError("Konfirmasikan bahwa invoice benar milik prospek ini untuk melengkapi produk atau identitas pembeli yang belum diketahui.")
    if obj.product_id and obj.product_id != invoice.product_id or obj.party_id and obj.party_id != invoice.party_id:
        raise ValidationError("Produk dan pembeli invoice harus sama dengan prospek.")
    if invoice.date < timezone.localdate(obj.received_at):
        raise ValidationError("Invoice mendahului prospek; atribusi tidak dapat dibuat dari hubungan ini.")
    if Lead.objects.filter(organization=org, invoice=invoice).exclude(pk=obj.pk).exists():
        raise ValidationError("Invoice sudah terhubung ke prospek lain dan tidak boleh dihitung dua kali.")
    before_mapping = {"party_id": obj.party_id, "product_id": obj.product_id}
    if obj.party_id is None:
        obj.party = invoice.party
    if obj.product_id is None:
        obj.product = invoice.product
    obj.invoice = invoice
    obj._invoice_link = True
    obj.version += 1
    _save(obj)
    _audit(org, actor, "lead.invoice_linked", obj, invoice_id=invoice.pk, version=obj.version, basis="finance_invoice_reference",
           before_mapping=before_mapping, after_mapping={"party_id": obj.party_id, "product_id": obj.product_id}, identity_confirmed=confirm_identity)
    return obj


@transaction.atomic
def create_action(*, organization, actor, **fields):
    org = _org(organization, actor)
    obj = _save(MarketingAction(organization=org, created_by=actor, **_fields(fields, ACTION_FIELDS, org)))
    _audit(org, actor, "action.created", obj, values=_snapshot(obj, ACTION_FIELDS))
    return obj


@transaction.atomic
def update_action(*, organization, actor, action, expected_version, **fields):
    org = _org(organization, actor)
    obj = _scoped(MarketingAction, action, org)
    _version(obj, expected_version)
    before = _snapshot(obj, {"status", "outcome"})
    for key, value in _fields(fields, {"status", "outcome"}, org).items():
        setattr(obj, key, value)
    obj.version += 1
    _save(obj)
    _audit(org, actor, "action.updated", obj, before=before, after=_snapshot(obj, {"status", "outcome"}), version=obj.version)
    return obj


@transaction.atomic
def configure_marketing_ai_policy(*, organization, actor, enabled, monthly_budget_usd, request_cap_usd):
    from .ai import DISCLOSURE_TEXT, DISCLOSURE_VERSION
    org = _org(organization, actor, {"owner"})
    if type(enabled) is not bool:
        raise ValidationError("Status izin AI tidak valid.")
    monthly = _decimal(monthly_budget_usd, 6, "1000000")
    cap = _decimal(request_cap_usd, 6, "1000000")
    if monthly is None or cap is None or enabled and (monthly <= 0 or cap <= 0 or cap > monthly):
        raise ValidationError("AI aktif membutuhkan batas bulanan dan per permintaan yang positif; batas permintaan tidak boleh melebihi batas bulanan.")
    obj = MarketingAIPolicy.objects.filter(organization=org).first()
    if obj is None:
        obj = MarketingAIPolicy(organization=org)
    else:
        obj.policy_version += 1
    obj.enabled, obj.monthly_budget_usd, obj.request_cap_usd = enabled, monthly, cap
    obj.approved_by, obj.approved_at = actor, timezone.now()
    obj.disclosure_version, obj.disclosure_text = DISCLOSURE_VERSION, DISCLOSURE_TEXT
    obj.save()
    _audit(org, actor, "ai.policy_changed", obj, enabled=enabled, version=obj.policy_version, disclosure_version=DISCLOSURE_VERSION)
    return obj
