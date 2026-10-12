"""Private evidence versions; file bytes never enter AI or public media links."""

import hashlib

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Max

from evidence.services import inspect_upload

from .activities import _required_text, _save_activity, require_work_responsibility
from .activity_models import ActivityEvidence, WorkActivity
from .services import _audit, _org, _save, _scoped, _version


def checked_evidence_bytes(proof):
    try:
        with proof.file.open("rb") as handle:
            data = handle.read(10 * 1024 * 1024 + 1)
    except (OSError, ValueError) as exc:
        raise ValidationError("Berkas bukti tidak tersedia untuk diperiksa.") from exc
    if len(data) != proof.size or hashlib.sha256(data).hexdigest() != proof.sha256:
        raise ValidationError(
            "Isi bukti berbeda dari versi yang tersimpan; jangan gunakan sebelum diperiksa."
        )
    return data


def active_evidence_manifest(work):
    proofs = list(
        ActivityEvidence.objects.filter(
            organization=work.organization, activity=work, replacement__isnull=True
        ).order_by("version", "pk")
    )
    if len(proofs) > 25:
        raise ValidationError("Satu pengajuan maksimal 25 lampiran aktif.")
    result = []
    for proof in proofs:
        checked_evidence_bytes(proof)
        result.append(
            {"id": proof.pk, "sha256": proof.sha256, "version": proof.version}
        )
    return result


def attach_work_evidence(
    *,
    organization,
    actor,
    activity,
    expected_version,
    upload,
    replaces=None,
    reason="",
    responsibility_reason="",
):
    inspected = inspect_upload(upload)
    stored_name, storage = None, None
    try:
        with transaction.atomic(durable=True):
            org = _org(organization, actor)
            work = _scoped(WorkActivity, activity, org)
            _version(work, expected_version)
            responsibility_reason = require_work_responsibility(
                organization=org, actor=actor, work=work, reason=responsibility_reason
            )
            if work.status not in {"planned", "in_progress", "needs_revision"}:
                raise ValidationError(
                    "Unggah bukti sebelum pengajuan, atau tarik pengajuan untuk memperbaikinya."
                )
            prior = (
                None if replaces is None else _scoped(ActivityEvidence, replaces, org)
            )
            if prior and (
                prior.activity_id != work.pk
                or ActivityEvidence.objects.filter(replaces=prior).exists()
            ):
                raise ValidationError(
                    "Pilih versi bukti aktif dari pekerjaan yang sama."
                )
            if prior:
                reason = _required_text(reason, "reason")
            if (
                not prior
                and work.attachments.filter(replacement__isnull=True).count() >= 25
            ):
                raise ValidationError(
                    "Maksimal 25 lampiran aktif; ganti versi yang salah dengan alasan."
                )
            data = inspected.pop("data")
            version = (
                work.attachments.aggregate(value=Max("version"))["value"] or 0
            ) + 1
            proof = ActivityEvidence(
                organization=org,
                created_by=actor,
                activity=work,
                version=version,
                replaces=prior,
                correction_reason=reason,
                **inspected,
            )
            storage = proof.file.storage
            proof.file.save(proof.original_name, ContentFile(data), save=False)
            stored_name = proof.file.name
            proof._evidence_creation = True
            _save(proof)
            work.version += 1
            _save_activity(work)
            _audit(
                org,
                actor,
                "activity.evidence_attached",
                proof,
                work_id=work.pk,
                sha256=proof.sha256,
                version=proof.version,
                replaces_id=getattr(prior, "pk", None),
                reason=reason,
                responsibility_reason=responsibility_reason,
            )
            return proof
    except Exception:
        if stored_name and storage:
            storage.delete(stored_name)
        raise
