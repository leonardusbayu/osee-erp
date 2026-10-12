"""A received professional conclusion about one original supplier cash event.

This module never calculates tax, infers gross consideration from bank cash,
creates an obligation, or changes cash/application/refund journals.
"""

import hashlib
import re
from datetime import date
from uuid import UUID

from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q

from core.audit import record
from core.models import Membership, Organization
from finance.models import SupplierAdvance

from . import workflow
from .models import SupplierAdvanceTaxReview, TaxEvidence

SCHEMA = "supplier-advance-tax-review"
RESOLUTIONS = ("no_withholding", "more_evidence", "unsupported_treatment")
_FACTS = {
    "cash_amount",
    "payment_date",
    "contract_reference",
    "gross_amount",
    "withholding_amount",
    "legal_basis_reference",
    "professional_instruction",
}
_DECIMAL = re.compile(r"^(?:0|[1-9][0-9]{0,17})\.[0-9]{2}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")


def _scope(organization, advance, *, lock=False):
    org_id = getattr(organization, "pk", organization)
    advance_id = getattr(advance, "pk", advance)
    if type(org_id) is not int or type(advance_id) is not int:
        raise ValidationError("Identitas perusahaan atau uang muka tidak sah.")
    orgs = Organization.objects.select_for_update() if lock else Organization.objects
    rows = (
        SupplierAdvance.objects.select_for_update() if lock else SupplierAdvance.objects
    )
    try:
        org = orgs.get(pk=org_id)
        row = rows.select_related("supplier").get(organization=org, pk=advance_id)
    except (Organization.DoesNotExist, SupplierAdvance.DoesNotExist):
        raise ValidationError(
            "Uang muka tidak tersedia untuk perusahaan ini."
        ) from None
    return org, row


def _owner(organization, actor):
    if (
        not getattr(actor, "is_authenticated", False)
        or not Membership.objects.filter(
            organization=organization, user=actor, user__is_active=True, role="owner"
        ).exists()
    ):
        raise PermissionDenied(
            "Owner aktif perusahaan ini diperlukan untuk mencatat keputusan dalam laporan profesional."
        )


def supplier_advance_subject_snapshot(*, organization, advance):
    """Original cash proof plus current legal identity, without position/review."""
    from finance.supplier_advances import supplier_advance_source_snapshot

    org, row = _scope(organization, advance)
    source = supplier_advance_source_snapshot(organization=org, advance=row)
    supplier = row.supplier
    if supplier.organization_id != org.pk or supplier.kind != "supplier":
        raise ValidationError(
            "Identitas pemasok pada sumber uang muka tidak sesuai perusahaan."
        )
    return workflow.json_value(
        {
            "kind": "supplieradvance",
            "id": row.pk,
            "organization_id": org.pk,
            "cash": {
                "cash_amount": format(row.amount, ".2f"),
                "payment_date": row.date.isoformat(),
                "contract_reference": row.source_reference,
            },
            "supplier": {
                "id": supplier.pk,
                "organization_id": supplier.organization_id,
                "kind": supplier.kind,
                "name": supplier.name,
                "tax_id": supplier.tax_id,
            },
            "original_cash": source,
        }
    )


def _decision(decision, snapshot):
    keys = {
        "schema",
        "version",
        "scope",
        "source_fingerprint",
        "resolution",
        "reviewed_facts",
    }
    if (
        type(decision) is not dict
        or set(decision) != keys
        or decision["schema"] != SCHEMA
        or type(decision["version"]) is not int
        or decision["version"] != 1
        or decision["scope"] != "original_cash_event"
        or decision["resolution"] not in RESOLUTIONS
        or decision["source_fingerprint"] != workflow.digest(snapshot)
    ):
        raise ValidationError(
            "Keputusan harus mengikuti pemeriksaan peristiwa kas asli dan sumber yang sedang diperiksa."
        )
    facts = decision["reviewed_facts"]
    if type(facts) is not dict or set(facts) != _FACTS:
        raise ValidationError(
            "Tujuh fakta pemeriksaan profesional harus lengkap tanpa kolom tambahan."
        )
    if type(snapshot) is not dict or type(snapshot.get("cash")) is not dict:
        raise ValidationError("Bukti sumber pemeriksaan tidak dapat dibaca.")
    for key in ("cash_amount", "payment_date", "contract_reference"):
        if type(facts[key]) is not str or facts[key] != snapshot["cash"].get(key):
            raise ValidationError(
                "Nominal kas, tanggal pembayaran dan referensi kontrak harus cocok dengan peristiwa kas asli."
            )
    for key in ("cash_amount", "gross_amount", "withholding_amount"):
        if facts[key] is None and key != "cash_amount":
            continue
        if type(facts[key]) is not str or not _DECIMAL.fullmatch(facts[key]):
            raise ValidationError(
                "Nominal laporan harus berupa angka desimal tidak negatif dengan dua angka pecahan, atau belum ditentukan."
            )
    for key in ("legal_basis_reference", "professional_instruction"):
        if (
            type(facts[key]) is not str
            or not facts[key].strip()
            or len(facts[key]) > 4000
        ):
            raise ValidationError(
                "Referensi dasar laporan dan instruksi profesional wajib dicatat."
            )
    return workflow.json_value(decision)


def _report_snapshot(report):
    evidence = report.evidence
    actual = hashlib.sha256(bytes(evidence.content)).hexdigest()
    if (
        evidence.organization_id != report.organization_id
        or not report.evidence_sha256
        or report.evidence_sha256 != evidence.sha256
        or actual != report.evidence_sha256
    ):
        raise ValidationError(
            "Laporan PDF asli atau hash penerimaannya berubah; pemeriksaan ditahan."
        )
    return workflow.json_value(
        {
            "id": report.pk,
            "organization_id": report.organization_id,
            "evidence_id": evidence.pk,
            "evidence_sha256": report.evidence_sha256,
            "evidence_filename": evidence.original_filename,
            "evidence_uploaded_by_id": evidence.uploaded_by_id,
            "evidence_created_at": evidence.created_at,
            "professional_name": report.professional_name,
            "qualification_reference": report.qualification_reference,
            "reviewed_on": report.reviewed_on,
            "statement": report.statement,
            "subject_type": report.subject_type,
            "subject_id": report.subject_id,
            "subject_digest": report.subject_digest,
            "decision": report.decision,
            "recorded_by_id": report.recorded_by_id,
            "recorded_at": report.recorded_at,
        }
    )


def _seal(row):
    return workflow.digest(
        {
            "organization_id": row.organization_id,
            "advance_id": row.advance_id,
            "previous_id": row.previous_id,
            "previous_hash": row.previous.snapshot_hash if row.previous_id else None,
            "version": row.version,
            "external_review_id": row.external_review_id,
            "actor_id": row.actor_id,
            "request_key": str(row.request_key),
            "request_payload": row.request_payload,
            "decision": row.decision,
            "source_snapshot": row.source_snapshot,
            "source_fingerprint": row.source_fingerprint,
            "report_snapshot": row.report_snapshot,
            "created_at": row.created_at,
        }
    )


def validate_review_record(row):
    """Recheck the stored authority, including when read after actor revocation."""
    if (
        type(row.version) is not int
        or row.version < 1
        or row.advance.organization_id != row.organization_id
    ):
        raise ValidationError("Versi atau perusahaan keputusan pemeriksaan tidak sah.")
    source = row.source_snapshot
    if (
        type(source) is not dict
        or source.get("kind") != "supplieradvance"
        or type(source.get("id")) is not int
        or source["id"] != row.advance_id
        or type(source.get("organization_id")) is not int
        or source["organization_id"] != row.organization_id
        or row.source_fingerprint != workflow.digest(source)
    ):
        raise ValidationError("Bukti sumber keputusan pemeriksaan berubah.")
    _decision(row.decision, source)
    if row.previous_id:
        previous = row.previous
        if (
            previous.organization_id != row.organization_id
            or previous.advance_id != row.advance_id
            or previous.version != row.version - 1
            or previous.created_at > row.created_at
        ):
            raise ValidationError("Urutan riwayat pemeriksaan tidak sah.")
    elif row.version != 1:
        raise ValidationError("Akar riwayat pemeriksaan tidak lengkap.")
    report = row.external_review
    if (
        report.organization_id != row.organization_id
        or report.subject_type != "supplieradvance"
        or report.subject_id != row.advance_id
        or report.decision != row.decision
        or report.subject_digest
        != workflow.digest({"subject": source, "decision": row.decision})
        or report.recorded_by_id != row.actor_id
        or _report_snapshot(report) != row.report_snapshot
    ):
        raise ValidationError(
            "Laporan profesional tidak cocok dengan keputusan tersimpan."
        )
    payload = row.request_payload
    required = {
        "schema",
        "organization_id",
        "actor_id",
        "advance_id",
        "decision",
        "evidence_id",
        "professional_name",
        "qualification_reference",
        "reviewed_on",
        "statement",
        "report_confirmed",
        "expected_version",
        "expected_fingerprint",
    }
    if (
        type(payload) is not dict
        or set(payload) != required
        or payload["schema"] != SCHEMA
        or payload["organization_id"] != row.organization_id
        or payload["actor_id"] != row.actor_id
        or payload["advance_id"] != row.advance_id
        or payload["decision"] != row.decision
        or payload["evidence_id"] != report.evidence_id
        or payload["report_confirmed"] is not True
        or type(payload["expected_version"]) is not int
        or payload["expected_version"] != row.version - 1
        or type(payload["expected_fingerprint"]) is not str
        or not _HASH.fullmatch(payload["expected_fingerprint"])
    ):
        raise ValidationError("Permintaan asli keputusan pemeriksaan berubah.")
    if any(
        type(payload[key]) is not int
        for key in ("organization_id", "actor_id", "advance_id", "evidence_id")
    ):
        raise ValidationError(
            "Identitas pada permintaan asli harus berupa nomor catatan yang sah."
        )
    for key in (
        "professional_name",
        "qualification_reference",
        "reviewed_on",
        "statement",
    ):
        if payload[key] != row.report_snapshot[key]:
            raise ValidationError(
                "Isi permintaan asli tidak cocok dengan laporan profesional."
            )
    if row.snapshot_hash != _seal(row):
        raise ValidationError("Hash riwayat pemeriksaan berubah.")


def _history(advance):
    return list(
        SupplierAdvanceTaxReview.objects.filter(advance_id=advance.pk)
        .select_related("advance", "previous", "external_review__evidence")
        .order_by("version", "pk")
    )


def validate_new_review_record(row):
    """The narrow append flag cannot authorize a stale or protected revision."""
    _owner(row.organization, row.actor)
    context = _context(row.organization, row.advance)
    history = _history(row.advance)
    previous = None
    for number, item in enumerate(history, 1):
        validate_review_record(item)
        if item.version != number or item.previous_id != (
            previous.pk if previous else None
        ):
            raise ValidationError(
                "Riwayat pemeriksaan harus utuh sebelum keputusan baru."
            )
        previous = item
    if (
        context["source_snapshot"] is None
        or row.source_snapshot != context["source_snapshot"]
        or row.version != context["version"] + 1
        or row.previous_id != (previous.pk if previous else None)
        or row.request_payload["expected_fingerprint"] != context["fingerprint"]
    ):
        raise ValidationError(
            "Keputusan baru memerlukan sumber, versi dan riwayat yang baru saja diperiksa."
        )
    if previous:
        from .supplier_advance_dependencies import (
            supplier_advance_review_correction_blockers,
        )

        blockers = supplier_advance_review_correction_blockers(
            organization=row.organization, advance=row.advance
        )
        if blockers:
            raise ValidationError(blockers)


def _graph(history):
    # Include current evidence identities/hashes even when proof validation fails:
    # an error cannot accidentally preserve a previously viewed token.
    rows = []
    for row in history:
        report = row.external_review
        evidence = report.evidence
        rows.append(
            {
                "id": row.pk,
                "organization_id": row.organization_id,
                "version": row.version,
                "previous_id": row.previous_id,
                "snapshot_hash": row.snapshot_hash,
                "seal": _seal(row),
                "report": {
                    field.attname: getattr(report, field.attname)
                    for field in report._meta.fields
                },
                "evidence": {
                    "id": evidence.pk,
                    "organization_id": evidence.organization_id,
                    "sha256": evidence.sha256,
                    "actual_sha256": hashlib.sha256(
                        bytes(evidence.content)
                    ).hexdigest(),
                    "filename": evidence.original_filename,
                    "uploaded_by_id": evidence.uploaded_by_id,
                    "created_at": evidence.created_at,
                },
            }
        )
    return workflow.json_value(rows)


def _context(org, advance):
    history = _history(advance)
    blockers, status, source = [], "pending", None
    try:
        source = supplier_advance_subject_snapshot(organization=org, advance=advance)
        source_fingerprint = workflow.digest(source)
    except (ValidationError, ObjectDoesNotExist, TypeError, ValueError) as exc:
        source_fingerprint = workflow.digest(
            {
                "damaged_source": advance.pk,
                "organization_id": org.pk,
                "stored": {
                    field.attname: getattr(advance, field.attname)
                    for field in advance._meta.fields
                },
            }
        )
        blockers.extend(
            exc.messages
            if isinstance(exc, ValidationError)
            else ["Bukti kas asli perlu diperiksa."]
        )
        status = "held"
    try:
        previous = None
        for number, row in enumerate(history, 1):
            validate_review_record(row)
            if row.version != number or row.previous_id != (
                previous.pk if previous else None
            ):
                raise ValidationError(
                    "Riwayat keputusan pemeriksaan terputus atau ambigu."
                )
            previous = row
    except (
        ValidationError,
        ObjectDoesNotExist,
        TypeError,
        ValueError,
        KeyError,
    ) as exc:
        blockers.extend(
            exc.messages
            if isinstance(exc, ValidationError)
            else ["Riwayat laporan profesional perlu diperiksa."]
        )
        status = "held"
    if not blockers and history:
        latest = history[-1]
        if latest.source_fingerprint != source_fingerprint:
            status = "stale"
            blockers.append(
                "Sumber kas atau identitas legal pemasok berubah. Periksa kembali dengan laporan profesional yang sesuai."
            )
        elif (
            latest.decision["resolution"] == "no_withholding"
            and latest.decision["reviewed_facts"]["withholding_amount"] == "0.00"
        ):
            status = "reviewed"
        else:
            status = "held"
            blockers.append(
                "Laporan profesional meminta bukti atau perlakuan lanjutan. Penyelesaian pajak belum dapat dilepas."
            )
    elif not blockers:
        blockers.append(
            "Pembayaran asli uang muka pemasok belum memiliki laporan profesional tentang pajak yang dicocokkan."
        )
    fingerprint = workflow.digest(
        {
            "schema": SCHEMA,
            "organization_id": org.pk,
            "advance_id": advance.pk,
            "source_fingerprint": source_fingerprint,
            "source": source,
            "history": _graph(history),
        }
    )
    visible_history = [
        item
        for item in history
        if item.organization_id == org.pk
        and item.advance.organization_id == org.pk
        and item.external_review.organization_id == org.pk
        and item.external_review.evidence.organization_id == org.pk
    ]
    return {
        "advance": advance,
        "status": status,
        "blockers": list(dict.fromkeys(blockers)),
        "source_fingerprint": source_fingerprint,
        "fingerprint": fingerprint,
        "version": history[-1].version if history else 0,
        "history": visible_history,
        "source_snapshot": source,
    }


@transaction.atomic
def supplier_advance_tax_context(*, organization, advance):
    org, row = _scope(organization, advance, lock=True)
    return _context(org, row)


@transaction.atomic
def record_supplier_advance_review(
    *,
    organization,
    actor,
    advance,
    decision,
    evidence,
    professional_name,
    qualification_reference,
    reviewed_on,
    statement,
    report_confirmed,
    expected_version,
    expected_fingerprint,
    request_key,
):
    org, row = _scope(organization, advance, lock=True)
    _owner(org, actor)
    try:
        key = UUID(str(request_key))
    except (ValueError, TypeError, AttributeError):
        raise ValidationError("Identitas permintaan pemeriksaan tidak sah.") from None
    if (
        report_confirmed is not True
        or type(expected_version) is not int
        or expected_version < 0
        or type(expected_fingerprint) is not str
        or not _HASH.fullmatch(expected_fingerprint)
    ):
        raise ValidationError(
            "Konfirmasikan laporan yang diterima setelah memeriksa versi sumber yang lengkap."
        )
    if (
        any(
            type(value) is not str or len(value.strip()) < 3
            for value in (professional_name, qualification_reference, statement)
        )
        or type(reviewed_on) is not date
    ):
        raise ValidationError(
            "Lengkapi identitas, tanggal dan kesimpulan laporan profesional."
        )
    evidence = workflow._scope(TaxEvidence, evidence, org)
    payload = workflow.json_value(
        {
            "schema": SCHEMA,
            "organization_id": org.pk,
            "actor_id": actor.pk,
            "advance_id": row.pk,
            "decision": decision,
            "evidence_id": evidence.pk,
            "professional_name": professional_name.strip(),
            "qualification_reference": qualification_reference.strip(),
            "reviewed_on": reviewed_on,
            "statement": statement.strip(),
            "report_confirmed": True,
            "expected_version": expected_version,
            "expected_fingerprint": expected_fingerprint,
        }
    )
    retry = SupplierAdvanceTaxReview.objects.filter(
        organization=org, request_key=key
    ).first()
    if retry:
        if retry.request_payload != payload:
            raise ValidationError(
                "Identitas permintaan sudah digunakan dengan isi berbeda. Periksa kembali untuk permintaan baru."
            )
        validate_review_record(retry)
        return retry
    context = _context(org, row)
    if (
        expected_version != context["version"]
        or expected_fingerprint != context["fingerprint"]
    ):
        raise ValidationError(
            "Sumber atau laporan berubah setelah dilihat. Isian tetap tersedia; periksa kembali dan konfirmasikan ulang."
        )
    if context["source_snapshot"] is None:
        raise ValidationError(context["blockers"])
    # A new source can make history stale; damaged history cannot be replaced
    # with a superficially fresh root. Every prior link must still prove itself.
    previous = None
    for number, item in enumerate(_history(row), 1):
        validate_review_record(item)
        if item.version != number or item.previous_id != (
            previous.pk if previous else None
        ):
            raise ValidationError(
                "Riwayat pemeriksaan harus utuh sebelum keputusan baru."
            )
        previous = item
    if previous:
        from .supplier_advance_dependencies import (
            supplier_advance_review_correction_blockers,
        )

        blockers = supplier_advance_review_correction_blockers(
            organization=org, advance=row
        )
        if blockers:
            raise ValidationError(blockers)
    normalized = _decision(decision, context["source_snapshot"])
    report = workflow.record_external_review(
        organization=org,
        actor=actor,
        subject=row,
        decision=normalized,
        evidence=evidence,
        professional_name=professional_name,
        qualification_reference=qualification_reference,
        reviewed_on=reviewed_on,
        statement=statement,
        report_confirmed=True,
    )
    workflow._assert_review(org, row, normalized, report)
    result = SupplierAdvanceTaxReview(
        organization=org,
        advance=row,
        previous=previous,
        version=context["version"] + 1,
        external_review=report,
        decision=normalized,
        source_snapshot=context["source_snapshot"],
        source_fingerprint=context["source_fingerprint"],
        report_snapshot=_report_snapshot(report),
        actor=actor,
        request_key=key,
        request_payload=payload,
    )
    result.snapshot_hash = _seal(result)
    result._supplier_advance_review_write = True
    result._tax_write = True
    try:
        result.save()
    finally:
        result.__dict__.pop("_supplier_advance_review_write", None)
        result.__dict__.pop("_tax_write", None)
    record(
        org,
        actor,
        "tax.supplier_advance_review.recorded",
        result,
        {
            "advance_id": row.pk,
            "version": result.version,
            "external_review_id": report.pk,
            "evidence_sha256": report.evidence_sha256,
            "source_fingerprint": result.source_fingerprint,
            "resolution": normalized["resolution"],
            "scope": "original_cash_event",
            "credentials_independently_verified": False,
        },
    )
    return result


@transaction.atomic
def supplier_advance_tax_gates(*, organization, cutoff):
    org_id = getattr(organization, "pk", organization)
    if type(org_id) is not int or type(cutoff) is not date:
        raise ValidationError("Perusahaan dan tanggal batas pemeriksaan harus sah.")
    try:
        org = Organization.objects.select_for_update().get(pk=org_id)
    except Organization.DoesNotExist:
        raise ValidationError("Perusahaan tidak tersedia.") from None
    inventory, blockers = [], []
    for row in (
        SupplierAdvance.objects.filter(organization=org)
        .filter(
            Q(date__lte=cutoff)
            | Q(journal__date__lte=cutoff)
            | Q(bank_posting__transaction__date__lte=cutoff)
        )
        .select_related("supplier")
        .order_by("date", "pk")
    ):
        context = _context(org, row)
        latest = context["history"][-1] if context["history"] else None
        report = latest.external_review if latest else None
        inventory.append(
            {
                "advance_id": row.pk,
                "date": row.date.isoformat(),
                "source_fingerprint": context["source_fingerprint"],
                "version": context["version"],
                "status": context["status"],
                "review_id": latest.pk if latest else None,
                "external_review_id": report.pk if report else None,
                "evidence_id": report.evidence_id if report else None,
                "evidence_sha256": report.evidence_sha256 if report else None,
                "snapshot_digest": latest.snapshot_hash if latest else None,
            }
        )
        blockers.extend(
            f"Uang muka pemasok #{row.pk}: {message}" for message in context["blockers"]
        )
    snapshot = {
        "schema": SCHEMA,
        "organization_id": org.pk,
        "cutoff": cutoff.isoformat(),
        "inventory": inventory,
    }
    return {
        "fingerprint": workflow.digest(snapshot),
        "inventory": inventory,
        "blockers": blockers,
    }
