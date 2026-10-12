"""A received professional conclusion about one distinct actual cash refund.

This module never calculates tax, infers gross consideration from bank cash,
creates an obligation, or changes cash/application/refund journals.
"""

import hashlib
import re
from datetime import date
from uuid import UUID

from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.db import transaction

from core.audit import record
from core.models import Membership, Organization
from finance.models import OpeningBalanceRefund

from . import workflow
from .models import OpeningRefundTaxReview, TaxEvidence
from .opening_balance_review import _report_snapshot

SCHEMA = "opening-refund-tax-review"
RESOLUTIONS = ("principal_only", "more_evidence", "unsupported_treatment")
_FLAGS = {
    "withholding_recovery_requested",
    "tax_credit_restitution_requested",
    "prior_period_income_correction_requested",
}
_FACTS = {
    "cash_amount",
    "payment_date",
    "contract_reference",
    "gross_amount",
    "withholding_amount",
    "legal_basis_reference",
    "professional_instruction",
} | _FLAGS
_DECIMAL = re.compile(r"^(?:0|[1-9][0-9]{0,17})\.[0-9]{2}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")


def _scope(organization, refund, *, lock=False):
    org_id = getattr(organization, "pk", organization)
    refund_id = getattr(refund, "pk", refund)
    if type(org_id) is not int or type(refund_id) is not int:
        raise ValidationError("Identitas perusahaan atau saldo awal tidak sah.")
    orgs = Organization.objects.select_for_update() if lock else Organization.objects
    rows = (
        OpeningBalanceRefund.objects.select_for_update()
        if lock
        else OpeningBalanceRefund.objects
    )
    try:
        org = orgs.get(pk=org_id)
        row = rows.get(organization=org, pk=refund_id)
    except (Organization.DoesNotExist, OpeningBalanceRefund.DoesNotExist):
        raise ValidationError(
            "Kas saldo awal tidak tersedia untuk perusahaan ini."
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


def opening_refund_subject_snapshot(*, organization, refund):
    """Original cash proof plus current legal identity, without position/review."""
    from finance.opening_balances import (
        opening_refund_source_snapshot,
        opening_settlement_source_snapshot,
    )

    org, row = _scope(organization, refund)
    source = opening_refund_source_snapshot(organization=org, refund=row)
    item, party = row.item, row.item.party
    expected_kinds = (
        ("customer", "reseller") if item.kind == "receivable" else ("supplier",)
    )
    if (
        item.organization_id != org.pk
        or item.kind not in ("receivable", "payable")
        or party.organization_id != org.pk
        or party.kind not in expected_kinds
    ):
        raise ValidationError(
            "Identitas pihak terkait pada sumber saldo awal tidak sesuai perusahaan."
        )
    snapshot = {
        "kind": "openingbalancerefund",
        "id": row.pk,
        "organization_id": org.pk,
        "cash": {
            "cash_amount": format(row.amount, ".2f"),
            "payment_date": row.date.isoformat(),
            "contract_reference": item.source_reference,
        },
        "opening_item": {
            "id": item.pk,
            "kind": item.kind,
            "date": item.date.isoformat(),
            "source_date": item.source_date.isoformat(),
            "amount": format(item.amount, ".2f"),
        },
        "party": {
            "id": party.pk,
            "organization_id": party.organization_id,
            "kind": party.kind,
            "name": party.name,
            "tax_id": party.tax_id,
        },
        "refund_cash": source,
    }
    snapshot["original_cash"] = opening_settlement_source_snapshot(
        organization=org, settlement=row.settlement_id
    )
    return workflow.json_value(snapshot)


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
        or decision["scope"] != "refund_cash_event"
        or decision["resolution"] not in RESOLUTIONS
        or decision["source_fingerprint"] != workflow.digest(snapshot)
    ):
        raise ValidationError(
            "Keputusan harus mengikuti pemeriksaan peristiwa pengembalian uang dan sumber yang sedang diperiksa."
        )
    facts = decision["reviewed_facts"]
    if type(facts) is not dict or set(facts) != _FACTS:
        raise ValidationError(
            "Seluruh fakta pemeriksaan profesional harus lengkap tanpa kolom tambahan."
        )
    if type(snapshot) is not dict or type(snapshot.get("cash")) is not dict:
        raise ValidationError("Bukti sumber pemeriksaan tidak dapat dibaca.")
    for key in ("cash_amount", "payment_date", "contract_reference"):
        if type(facts[key]) is not str or facts[key] != snapshot["cash"].get(key):
            raise ValidationError(
                "Nominal kas, tanggal pembayaran dan referensi kontrak harus cocok dengan peristiwa pengembalian uang."
            )
    for key in ("cash_amount", "gross_amount", "withholding_amount"):
        if facts[key] is None and key != "cash_amount":
            continue
        if type(facts[key]) is not str or not _DECIMAL.fullmatch(facts[key]):
            raise ValidationError(
                "Nominal laporan harus berupa angka desimal tidak negatif dengan dua angka pecahan, atau belum ditentukan."
            )
    if any(facts[key] is not None and type(facts[key]) is not bool for key in _FLAGS):
        raise ValidationError(
            "Pernyataan pemulihan pemotongan, restitusi kredit dan pembetulan penghasilan harus jelas atau belum dijelaskan."
        )
    if decision["resolution"] == "principal_only" and any(
        facts[key] is not False for key in _FLAGS
    ):
        raise ValidationError(
            "Kesimpulan pengembalian pokok saja memerlukan laporan yang secara jelas menyatakan tidak ada pemulihan pemotongan, restitusi kredit atau pembetulan penghasilan sebelumnya. Gunakan pemeriksaan lanjutan bila belum dijelaskan atau diminta."
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


def _seal(row):
    return workflow.digest(
        {
            "organization_id": row.organization_id,
            "refund_id": row.refund_id,
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
        or row.refund.organization_id != row.organization_id
    ):
        raise ValidationError("Versi atau perusahaan keputusan pemeriksaan tidak sah.")
    source = row.source_snapshot
    if (
        type(source) is not dict
        or source.get("kind") != "openingbalancerefund"
        or type(source.get("id")) is not int
        or source["id"] != row.refund_id
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
            or previous.refund_id != row.refund_id
            or previous.version != row.version - 1
            or previous.created_at > row.created_at
        ):
            raise ValidationError("Urutan riwayat pemeriksaan tidak sah.")
    elif row.version != 1:
        raise ValidationError("Akar riwayat pemeriksaan tidak lengkap.")
    report = row.external_review
    try:
        if type(
            report.reviewed_on
        ) is not date or report.reviewed_on < date.fromisoformat(
            source["cash"]["payment_date"]
        ):
            raise ValidationError(
                "Tanggal laporan asli tidak dapat mendahului peristiwa kas yang diperiksa."
            )
    except (TypeError, ValueError, KeyError):
        raise ValidationError(
            "Tanggal pada bukti pemeriksaan pengembalian uang tidak sah."
        ) from None
    if (
        report.organization_id != row.organization_id
        or report.subject_type != "openingbalancerefund"
        or report.subject_id != row.refund_id
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
        "refund_id",
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
        or payload["refund_id"] != row.refund_id
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
        for key in ("organization_id", "actor_id", "refund_id", "evidence_id")
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


def _history(refund):
    from django.db.models import Q

    from core.models import AuditEvent

    anchors = AuditEvent.objects.filter(
        action="tax.opening_refund_review.recorded", detail__refund_id=refund.pk
    ).values_list("object_id", flat=True)
    anchored_ids = [
        int(identity)
        for identity in anchors
        if isinstance(identity, str) and identity.isdecimal() and int(identity) > 0
    ]
    return list(
        OpeningRefundTaxReview.objects.filter(
            Q(refund_id=refund.pk)
            | Q(
                source_snapshot__id=refund.pk,
                source_snapshot__organization_id=refund.organization_id,
            )
            | Q(
                request_payload__refund_id=refund.pk,
                request_payload__organization_id=refund.organization_id,
            )
            | Q(pk__in=anchored_ids)
        ).order_by("version", "pk")
    )


def _history_receipt_graph(org, refund, history):
    """Receipt anchors cannot disappear with a typed decision or its aliases."""
    from django.db.models import Q

    from core.models import AuditEvent

    events = list(
        AuditEvent.objects.filter(
            Q(action="tax.opening_refund_review.recorded", detail__refund_id=refund.pk)
            | Q(
                object_type="OpeningRefundTaxReview",
                object_id__in=[str(row.pk) for row in history],
            )
        ).order_by("pk")
    )
    identities = {row.pk for row in history}
    blockers, graph = [], []
    for event in events:
        raw = {
            field.attname: getattr(event, field.attname) for field in event._meta.fields
        }
        graph.append({"audit_id": event.pk, "audit_hash": workflow.digest(raw)})
        identity = event.object_id
        if (
            not isinstance(identity, str)
            or not identity.isdecimal()
            or int(identity) <= 0
            or int(identity) not in identities
        ):
            blockers.append(
                "Keputusan pengembalian uang yang pernah diterima hilang atau berubah hubungan; riwayat tidak dapat dimulai ulang."
            )
    for row in history:
        receipts = [
            event
            for event in events
            if event.action == "tax.opening_refund_review.recorded"
            and event.object_id == str(row.pk)
        ]
        expected_detail = {
            "refund_id": refund.pk,
            "version": row.version,
            "external_review_id": row.external_review_id,
            "evidence_sha256": row.report_snapshot.get("evidence_sha256")
            if isinstance(row.report_snapshot, dict)
            else None,
            "source_fingerprint": row.source_fingerprint,
            "resolution": row.decision.get("resolution")
            if isinstance(row.decision, dict)
            else None,
            "scope": "refund_cash_event",
            "credentials_independently_verified": False,
        }
        if (
            row.organization_id != org.pk
            or row.refund_id != refund.pk
            or len(receipts) != 1
            or receipts[0].organization_id != org.pk
            or receipts[0].object_type != "OpeningRefundTaxReview"
            or receipts[0].actor_id != row.actor_id
            or receipts[0].detail != expected_detail
        ):
            blockers.append(
                "Bukti penerimaan keputusan pengembalian uang tidak utuh atau ambigu; periksa riwayat asal."
            )
    return {
        "audits": graph,
        "records": [
            {
                "review_id": row.pk,
                "record_hash": workflow.digest(
                    {
                        field.attname: getattr(row, field.attname)
                        for field in row._meta.fields
                    }
                ),
            }
            for row in history
        ],
    }, list(dict.fromkeys(blockers))


def validate_new_review_record(row):
    """The narrow append flag cannot authorize a stale or protected revision."""
    _owner(row.organization, row.actor)
    # The command creates the received report in the same atomic transition.
    # Reconstruct the viewed state before that one new receipt, while still
    # proving its bytes/subject/decision through validate_review_record above.
    context = _context(
        row.organization, row.refund, received_report_exclusion=row.external_review_id
    )
    history = _history(row.refund)
    _, receipt_blockers = _history_receipt_graph(row.organization, row.refund, history)
    if receipt_blockers:
        raise ValidationError(receipt_blockers)
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
        from .opening_refund_review_dependencies import (
            opening_refund_review_correction_blockers,
        )

        blockers = opening_refund_review_correction_blockers(
            organization=row.organization, refund=row.refund
        )
        if blockers:
            raise ValidationError(blockers)


def _graph(history):
    # Include current evidence identities/hashes even when proof validation fails:
    # an error cannot accidentally preserve a previously viewed token.
    rows = []
    for row in history:
        from .models import ExternalTaxReview

        report = (
            ExternalTaxReview.objects.filter(pk=row.external_review_id).values().first()
        )
        evidence = (
            TaxEvidence.objects.filter(pk=report["evidence_id"]).values().first()
            if report
            else None
        )
        if evidence is not None:
            evidence["actual_sha256"] = hashlib.sha256(
                bytes(evidence.pop("content"))
            ).hexdigest()
        rows.append(
            {
                "record": {
                    field.attname: getattr(row, field.attname)
                    for field in row._meta.fields
                },
                "report": report,
                "evidence": evidence,
            }
        )
    return workflow.json_value(rows)


def _visible_history(history, org):
    visible = []
    for item in history:
        try:
            if (
                item.organization_id == org.pk
                and item.refund.organization_id == org.pk
                and item.external_review.organization_id == org.pk
                and item.external_review.evidence.organization_id == org.pk
            ):
                visible.append(item)
        except ObjectDoesNotExist:
            continue
    return visible


def _received_report_graph(org, refund, history, *, exclude_report_id=None):
    """Bind durable receipt anchors and actual PDF bytes, including broken FKs."""
    from core.models import AuditEvent

    from .models import ExternalTaxReview

    audits = list(
        AuditEvent.objects.filter(
            organization=org,
            action="tax.external_review.recorded",
            object_type="ExternalTaxReview",
            detail__subject_type="openingbalancerefund",
            detail__subject_id=refund.pk,
        ).order_by("pk")
    )
    identities = set(
        ExternalTaxReview.objects.filter(
            subject_type="openingbalancerefund", subject_id=refund.pk
        ).values_list("pk", flat=True)
    )
    identities.update(row.external_review_id for row in history)
    blockers, graph = [], []
    for event in audits:
        if (
            not isinstance(event.object_id, str)
            or not event.object_id.isdecimal()
            or int(event.object_id) <= 0
        ):
            blockers.append(
                "Identitas penerimaan laporan pengembalian uang tidak dapat diperiksa."
            )
        else:
            identities.add(int(event.object_id))
    typed_ids = {row.external_review_id for row in history}
    for identity in sorted(identities):
        if identity == exclude_report_id:
            continue
        raw = ExternalTaxReview.objects.filter(pk=identity).values().first()
        evidence = (
            TaxEvidence.objects.filter(pk=raw["evidence_id"]).values().first()
            if raw
            else None
        )
        if evidence:
            evidence["actual_sha256"] = hashlib.sha256(
                bytes(evidence.pop("content"))
            ).hexdigest()
        graph.append(
            {
                "report_id": identity,
                "report_hash": workflow.digest(raw),
                "evidence_hash": workflow.digest(evidence),
            }
        )
        try:
            report = ExternalTaxReview.objects.get(pk=identity)
            if (
                report.organization_id != org.pk
                or report.subject_type != "openingbalancerefund"
                or report.subject_id != refund.pk
            ):
                raise ValidationError(
                    "Penerimaan laporan pengembalian uang berubah hubungan."
                )
            _report_snapshot(report)
            receipt_events = [
                event for event in audits if event.object_id == str(identity)
            ]
            if len(receipt_events) != 1:
                raise ValidationError(
                    "Riwayat penerimaan laporan tidak lengkap atau ambigu."
                )
            if identity not in typed_ids:
                workflow._assert_review(org, refund, report.decision, report)
                _decision(
                    report.decision,
                    opening_refund_subject_snapshot(organization=org, refund=refund),
                )
            for event in audits:
                if event.object_id == str(identity) and (
                    event.actor_id != report.recorded_by_id
                    or event.detail.get("evidence_sha256") != report.evidence_sha256
                ):
                    raise ValidationError(
                        "Bukti penerimaan tidak cocok dengan laporan asli."
                    )
        except (ValidationError, ObjectDoesNotExist, TypeError, ValueError, KeyError):
            blockers.append(
                "Laporan pengembalian uang yang pernah diterima hilang, berubah atau tidak cocok; periksa riwayat asal."
            )
    return {
        "reports": graph,
        "audits_hash": workflow.digest(
            [
                {
                    field.attname: getattr(event, field.attname)
                    for field in event._meta.fields
                }
                for event in audits
                if event.object_id != str(exclude_report_id)
            ]
        ),
    }, blockers


def _context(org, refund, *, received_report_exclusion=None):
    from finance.opening_balances import opening_refund_source_graph

    history = _history(refund)
    blockers, status, source = [], "pending", None
    try:
        source = opening_refund_subject_snapshot(organization=org, refund=refund)
        source_fingerprint = workflow.digest(source)
    except (ValidationError, ObjectDoesNotExist, TypeError, ValueError) as exc:
        source_fingerprint = workflow.digest(
            {
                "damaged_source": refund.pk,
                "organization_id": org.pk,
                "graph": opening_refund_source_graph(organization=org, refund=refund),
            }
        )
        blockers.extend(
            exc.messages
            if isinstance(exc, ValidationError)
            else ["Bukti pengembalian uang perlu diperiksa."]
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
    history_receipts, history_blockers = _history_receipt_graph(org, refund, history)
    if history_blockers:
        blockers.extend(history_blockers)
        status = "held"
    received_graph, received_blockers = _received_report_graph(
        org, refund, history, exclude_report_id=received_report_exclusion
    )
    if received_blockers:
        blockers.extend(received_blockers)
        status = "held"
    if not blockers and history:
        latest = history[-1]
        if latest.source_fingerprint != source_fingerprint:
            status = "stale"
            blockers.append(
                "Sumber kas atau identitas legal pihak terkait berubah. Periksa kembali dengan laporan profesional yang sesuai."
            )
        elif (
            latest.decision["resolution"] == "principal_only"
            and latest.decision["reviewed_facts"]["withholding_amount"] == "0.00"
            and all(latest.decision["reviewed_facts"][key] is False for key in _FLAGS)
        ):
            status = "reviewed"
        else:
            status = "held"
            blockers.append(
                "Laporan profesional meminta bukti atau perlakuan lanjutan. Penyelesaian pajak belum dapat dilepas."
            )
    elif not blockers:
        blockers.append(
            "Pengembalian uang ini belum memiliki laporan profesional tersendiri yang dicocokkan. Laporan pembayaran asli tidak menentukan pajak refund."
        )
    fingerprint = workflow.digest(
        {
            "schema": SCHEMA,
            "organization_id": org.pk,
            "refund_id": refund.pk,
            "source_fingerprint": source_fingerprint,
            "source": source,
            "history": _graph(history),
            "refund_graph": opening_refund_source_graph(
                organization=org, refund=refund
            ),
            "received_reports": received_graph,
            "history_receipts": history_receipts,
        }
    )
    visible_history = _visible_history(history, org)
    return {
        "refund": refund,
        "status": status,
        "blockers": list(dict.fromkeys(blockers)),
        "source_fingerprint": source_fingerprint,
        "fingerprint": fingerprint,
        "version": history[-1].version if history else 0,
        "history": visible_history,
        "source_snapshot": source,
    }


@transaction.atomic
def opening_refund_review_context(*, organization, refund):
    org, row = _scope(organization, refund, lock=True)
    return _context(org, row)


@transaction.atomic
def record_opening_refund_review(
    *,
    organization,
    actor,
    refund,
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
    org, row = _scope(organization, refund, lock=True)
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
            "refund_id": row.pk,
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
    retry = OpeningRefundTaxReview.objects.filter(
        organization=org, request_key=key
    ).first()
    if retry:
        if retry.request_payload != payload:
            raise ValidationError(
                "Identitas permintaan sudah digunakan dengan isi berbeda. Periksa kembali untuk permintaan baru."
            )
        validate_review_record(retry)
        _, receipt_blockers = _history_receipt_graph(org, row, _history(row))
        if receipt_blockers:
            raise ValidationError(receipt_blockers)
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
    _, receipt_blockers = _history_receipt_graph(org, row, _history(row))
    if receipt_blockers:
        raise ValidationError(receipt_blockers)
    _, received_blockers = _received_report_graph(org, row, _history(row))
    if received_blockers:
        raise ValidationError(received_blockers)
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
        from .opening_refund_review_dependencies import (
            opening_refund_review_correction_blockers,
        )

        blockers = opening_refund_review_correction_blockers(
            organization=org, refund=row
        )
        if blockers:
            raise ValidationError(blockers)
    normalized = _decision(decision, context["source_snapshot"])
    if reviewed_on < date.fromisoformat(
        context["source_snapshot"]["cash"]["payment_date"]
    ):
        raise ValidationError(
            "Tanggal laporan asli tidak dapat mendahului peristiwa kas yang diperiksa."
        )
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
    result = OpeningRefundTaxReview(
        organization=org,
        refund=row,
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
    result._opening_refund_review_write = True
    result._tax_write = True
    try:
        result.save()
    finally:
        result.__dict__.pop("_opening_refund_review_write", None)
        result.__dict__.pop("_tax_write", None)
    record(
        org,
        actor,
        "tax.opening_refund_review.recorded",
        result,
        {
            "refund_id": row.pk,
            "version": result.version,
            "external_review_id": report.pk,
            "evidence_sha256": report.evidence_sha256,
            "source_fingerprint": result.source_fingerprint,
            "resolution": normalized["resolution"],
            "scope": "refund_cash_event",
            "credentials_independently_verified": False,
        },
    )
    return result


@transaction.atomic
def opening_refund_tax_gates(*, organization, cutoff):
    """Retain every actual refund task, separately from its original payment."""
    from .opening_refund_review_dependencies import opening_refund_source_inventory

    org_id = getattr(organization, "pk", organization)
    if type(org_id) is not int or type(cutoff) is not date:
        raise ValidationError(
            "Perusahaan dan tanggal batas pengembalian uang harus sah."
        )
    try:
        org = Organization.objects.select_for_update().get(pk=org_id)
    except Organization.DoesNotExist:
        raise ValidationError("Perusahaan tidak tersedia.") from None
    selected = opening_refund_source_inventory(organization=org, cutoff=cutoff)
    inventory, blockers = [], []
    for row in selected["refunds"]:
        context = _context(org, row)
        latest = context["history"][-1] if context["history"] else None
        report = latest.external_review if latest else None
        inventory.append(
            {
                "refund_id": row.pk,
                "item_id": row.item_id,
                "settlement_id": row.settlement_id,
                "date": row.date.isoformat() if type(row.date) is date else None,
                "review_fingerprint": context["fingerprint"],
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
            f"Pengembalian saldo awal #{row.pk}: {message}"
            for message in context["blockers"]
        )
    for orphan in selected["orphans"]:
        inventory.append(
            {
                **orphan,
                "status": "held",
                "date": None,
                "review_fingerprint": workflow.digest(orphan),
                "source_fingerprint": workflow.digest(orphan["source"]),
            }
        )
        blockers.append(
            "Pengembalian uang saldo awal memiliki bukti atau jurnal asli yang terputus; periksa sumber sebelum persetujuan atau penutupan."
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
        "blockers": list(dict.fromkeys(blockers)),
    }
