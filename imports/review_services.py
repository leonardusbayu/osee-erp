"""Finance decisions and corrections are overlays, never edits to imported originals."""

import hashlib
import json

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import transaction

from core.audit import record
from core.models import Membership
from evidence.services import inspect_upload
from finance.services import _lock_org, _scoped, _write_actor

from .models import ImportException, SourceDocument
from .review_models import ImportExceptionReview


def _text(value, label, *, required=True):
    if (
        not isinstance(value, str)
        or len(value) > 4000
        or "\x00" in value
        or required
        and not value.strip()
    ):
        raise ValidationError(
            f"{label} wajib diisi dengan jelas, maksimal 4.000 karakter."
        )
    return value.strip()


def _hash(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _source_documents(batch):
    from .services import MAX_SOURCE_BYTES

    documents, valid = [], True
    for doc in batch.source_documents.order_by("pk"):
        try:
            with doc.file.open("rb") as handle:
                content = handle.read(MAX_SOURCE_BYTES + 1)
            actual = hashlib.sha256(content).hexdigest()
        except OSError:
            actual = "unavailable"
        valid = valid and actual == doc.sha256
        documents.append(
            {"id": doc.pk, "declared_sha256": doc.sha256, "actual_sha256": actual}
        )
    return documents, valid and bool(documents)


def _source_snapshot(exception, source_documents=None):
    documents, valid = (
        source_documents
        if source_documents is not None
        else _source_documents(exception.batch)
    )
    snapshot = {
        "schema": "source-exception-v1",
        "organization_id": exception.organization_id,
        "exception_id": exception.pk,
        "payload": exception.payload,
        "source_locator": exception.source_locator,
        "bundle_hash": exception.batch.bundle_hash,
        "documents": documents,
    }
    return snapshot, valid


def exception_state(*, organization, exception):
    source = _scoped(ImportException, exception, organization)
    return _state(source)


def exception_states(*, organization, exceptions):
    """Read each original document once per call, even with many discrepancies."""
    sources = list(exceptions)
    org_id = getattr(organization, "pk", organization)
    if any(source.organization_id != org_id for source in sources):
        raise ValidationError("Catatan sumber berasal dari perusahaan lain.")
    ids = {source.pk for source in sources}
    sources = list(
        ImportException.objects.filter(organization_id=org_id, pk__in=ids)
        .select_related("batch")
        .order_by("pk")
    )
    if len(sources) != len(ids):
        raise ValidationError("Catatan sumber tidak tersedia untuk perusahaan ini.")
    documents, states = {}, {}
    for source in sources:
        if source.batch_id not in documents:
            documents[source.batch_id] = _source_documents(source.batch)
        states[source.pk] = _state(source, documents[source.batch_id])
    return states


def _state(source, documents=None):
    organization = source.organization_id
    latest = source.reviews.filter(
        organization=organization, replacement__isnull=True
    ).first()
    snapshot, valid = _source_snapshot(source, documents)
    actual_attachment = ""
    if latest and latest.file:
        try:
            with latest.file.open("rb") as handle:
                actual_attachment = hashlib.sha256(
                    handle.read(10 * 1024 * 1024 + 1)
                ).hexdigest()
        except OSError:
            actual_attachment = "unavailable"
        valid = valid and actual_attachment == latest.sha256
    if latest:
        valid = valid and latest.snapshot == snapshot
    return {
        "status": "resolved"
        if latest and latest.decision == "resolved" and valid
        else "open",
        "review": latest,
        "previous_id": latest.pk if latest else None,
        "requires_recheck": bool(latest) and not valid,
        "fingerprint": _hash(
            {
                "source": snapshot,
                "review": latest.pk if latest else None,
                "review_fingerprint": latest.fingerprint if latest else "",
                "attachment_actual": actual_attachment,
            }
        ),
    }


def review_exception(
    *,
    organization,
    actor,
    exception,
    expected_previous_id,
    decision,
    responsible_user,
    reason,
    evidence,
    correction="",
    source_document=None,
    upload=None,
):
    """Own the commit boundary so new private evidence is removed on failed saves."""
    _write_actor(organization, actor)
    if expected_previous_id is not None and (
        type(expected_previous_id) is not int or expected_previous_id <= 0
    ):
        raise ValidationError("Versi pemeriksaan tidak valid; buka kembali catatan.")
    if decision not in {"open", "resolved", "reopened"}:
        raise ValidationError("Pilih keputusan pemeriksaan yang tersedia.")
    reason, evidence = (
        _text(reason, "Alasan keputusan"),
        _text(evidence, "Referensi bukti"),
    )
    correction = _text(
        correction, "Interpretasi / koreksi", required=decision == "resolved"
    )
    inspected = inspect_upload(upload) if upload is not None else None
    stored_name, storage = None, None
    try:
        with transaction.atomic(durable=True):
            org = _lock_org(organization)
            _write_actor(org, actor)
            source = _scoped(ImportException, exception, org, lock=True)
            latest = source.reviews.filter(
                organization=org, replacement__isnull=True
            ).first()
            if (latest.pk if latest else None) != expected_previous_id:
                raise ValidationError(
                    "Pemeriksaan telah berubah. Buka kembali versi terbaru sebelum menyimpan."
                )
            if decision == "reopened" and (not latest or latest.decision != "resolved"):
                raise ValidationError(
                    "Hanya catatan yang telah diselesaikan dapat dibuka kembali."
                )
            if decision == "open" and latest and latest.decision == "resolved":
                raise ValidationError(
                    "Gunakan keputusan dibuka kembali untuk catatan yang telah selesai."
                )
            user_id = getattr(responsible_user, "pk", responsible_user)
            if not Membership.objects.filter(
                organization=org,
                user_id=user_id,
                user__is_active=True,
                role__in=["owner", "finance"],
            ).exists():
                raise ValidationError(
                    "PIC harus Owner/Direktur atau Finance aktif di perusahaan ini."
                )
            responsible = get_user_model().objects.get(pk=user_id)
            document = (
                _scoped(SourceDocument, source_document, org)
                if source_document is not None
                else None
            )
            if document and document.batch_id != source.batch_id:
                raise ValidationError("Dokumen sumber berasal dari impor yang berbeda.")
            if decision == "resolved" and not document and inspected is None:
                raise ValidationError(
                    "Penyelesaian memerlukan dokumen sumber atau lampiran bukti pendukung."
                )
            snapshot, intact = _source_snapshot(source)
            if not intact:
                raise ValidationError(
                    "Dokumen sumber berubah atau tidak tersedia. Pulihkan sumber asli sebelum menetapkan interpretasi."
                )
            row = ImportExceptionReview(
                organization=org,
                exception=source,
                decision=decision,
                responsible_user=responsible,
                created_by=actor,
                reason=reason,
                evidence=evidence,
                correction=correction,
                source_document=document,
                supersedes=latest,
                snapshot=snapshot,
            )
            if inspected:
                row.original_name, row.content_type, row.size, row.sha256 = (
                    inspected[key]
                    for key in ("original_name", "content_type", "size", "sha256")
                )
                storage = row.file.storage
                row.file.save(
                    inspected["original_name"],
                    ContentFile(inspected["data"]),
                    save=False,
                )
                stored_name = row.file.name
            row.fingerprint = _hash(
                {
                    "source": snapshot,
                    "decision": decision,
                    "responsible_user_id": responsible.pk,
                    "reason": reason,
                    "evidence": evidence,
                    "correction": correction,
                    "source_document_id": document.pk if document else None,
                    "attachment_sha256": row.sha256,
                    "supersedes_id": latest.pk if latest else None,
                }
            )
            row._exception_review_write = True
            row.save()
            record(
                org,
                actor,
                f"imports.exception.{decision}",
                obj=row,
                detail={
                    "exception_id": source.pk,
                    "before_review_id": latest.pk if latest else None,
                    "after_review_id": row.pk,
                    "before_decision": latest.decision if latest else "open",
                    "after_decision": decision,
                    "responsible_user_id": responsible.pk,
                    "fingerprint": row.fingerprint,
                },
            )
        return row
    except Exception:
        if stored_name and storage:
            storage.delete(stored_name)
        raise
