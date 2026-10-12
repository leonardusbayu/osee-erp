"""Atomic recovery of manual ad facts, without changing Finance records."""

import re

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from . import services
from .models import AdDailyObservation, AdObservationRecovery


def observation_fingerprint(observation):
    """Use the shared source reader's displayed identity for stale-tab protection."""
    from .ad_sources import observation_fingerprint as shared_fingerprint

    return shared_fingerprint(observation)


def _text(value, label, limit, *, multiline=False):
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.strip()) > limit
        or "\x00" in value
        or (not multiline and any(ord(char) < 32 or ord(char) == 127 for char in value))
    ):
        raise ValidationError(
            f"{label} wajib diisi, maksimal {limit} karakter tanpa karakter yang tidak valid."
        )
    return value.strip()


def _recovery_save(obj):
    obj._recovery_write = True
    return services._save(obj)


def _request(
    org,
    actor,
    predecessor,
    action,
    request_key,
    reason,
    evidence,
    fields,
    expected_version,
    expected_fingerprint,
):
    key = _text(request_key, "Identitas permintaan", 160)
    reason = _text(reason, "Alasan koreksi", 500)
    evidence = _text(evidence, "Bukti pemeriksaan", 4000, multiline=True)
    if (
        type(expected_version) is not int
        or expected_version < 1
        or not isinstance(expected_fingerprint, str)
        or not re.fullmatch(r"[a-f0-9]{64}", expected_fingerprint)
    ):
        raise ValidationError(
            "Identitas sumber yang diperiksa tidak valid. Muat ulang laporan sebelum menyimpan."
        )
    data = None
    if action == "replace":
        if "reference" in fields:
            raise ValidationError(
                "Nomor dokumen asli tetap dipakai. Koreksi tidak memerlukan nomor dokumen baru."
            )
        data = services._fields(
            {
                **{
                    key: getattr(predecessor, key)
                    for key in services.AD_FIELDS - {"reference"}
                },
                **fields,
            },
            services.AD_FIELDS - {"reference"},
            org,
        )
        if data.get("campaign") is None:
            raise ValidationError(
                {"campaign": "Pilih kampanye untuk laporan pengganti."}
            )
    elif fields:
        raise ValidationError("Penarikan tidak menerima kolom pengganti.")
    payload = {
        "action": action,
        "predecessor_id": predecessor.pk,
        "actor_id": actor.pk,
        "reason": reason,
        "evidence": evidence,
        "expected_version": expected_version,
        "expected_fingerprint": expected_fingerprint,
        "replacement": services._snapshot(
            AdDailyObservation(**data), services.AD_FIELDS - {"reference"}
        )
        if data
        else None,
    }
    return key, reason, evidence, data, payload


def _retry(org, key, payload):
    previous = (
        AdObservationRecovery.objects.filter(organization=org, request_key=key)
        .select_related("predecessor", "successor")
        .first()
    )
    if previous and previous.request_payload != payload:
        raise ValidationError(
            "Permintaan ini sudah disimpan dengan isi berbeda. Muat ulang riwayat sebelum melakukan koreksi lain."
        )
    return previous


def _guard(predecessor, expected_version, expected_fingerprint):
    if (
        predecessor.withdrawn_at
        or AdObservationRecovery.objects.filter(predecessor=predecessor).exists()
    ):
        raise ValidationError(
            "Laporan ini sudah ditarik atau diganti. Buka versi aktif dari riwayat."
        )
    services._version(predecessor, expected_version)
    if not isinstance(
        expected_fingerprint, str
    ) or expected_fingerprint != observation_fingerprint(predecessor):
        raise ValidationError(
            "Sumber laporan telah berubah. Muat ulang dan periksa lagi sebelum menyimpan."
        )


def _recover(
    *,
    organization,
    actor,
    observation,
    expected_version,
    expected_fingerprint,
    request_key,
    reason,
    evidence,
    action,
    fields,
):
    # All marketing writes acquire the same Organization lock first. That makes
    # replay, numeric correction and competing recoveries serializable in PG.
    org = services._org(organization, actor)
    predecessor = services._scoped(AdDailyObservation, observation, org)
    key, reason, evidence, data, payload = _request(
        org,
        actor,
        predecessor,
        action,
        request_key,
        reason,
        evidence,
        fields,
        expected_version,
        expected_fingerprint,
    )
    previous = _retry(org, key, payload)
    if previous:
        return previous
    _guard(predecessor, expected_version, expected_fingerprint)
    before = {
        **services._snapshot(predecessor, services.AD_FIELDS),
        "version": predecessor.version,
    }
    predecessor.withdrawn_at = timezone.now()
    predecessor.version += 1
    _recovery_save(predecessor)
    successor = None
    if action == "replace":
        successor = _recovery_save(
            AdDailyObservation(
                organization=org,
                created_by=actor,
                source_root_id=predecessor.source_root_id or predecessor.pk,
                replaces=predecessor,
                reference=predecessor.reference,
                **data,
            )
        )
    decision = _recovery_save(
        AdObservationRecovery(
            organization=org,
            created_by=actor,
            predecessor=predecessor,
            successor=successor,
            request_key=key,
            action=action,
            request_payload=payload,
            reason=reason,
            evidence=evidence,
        )
    )
    services._audit(
        org,
        actor,
        "ad." + ("withdrawn" if action == "withdraw" else "replaced"),
        decision,
        predecessor_id=predecessor.pk,
        successor_id=successor.pk if successor else None,
        source_root_id=predecessor.source_root_id or predecessor.pk,
        before=before,
        after=services._snapshot(successor, services.AD_FIELDS) if successor else None,
        predecessor_version=predecessor.version,
        reason=reason,
        evidence=evidence,
    )
    return decision


@transaction.atomic
def withdraw_ad_observation(
    *,
    organization,
    actor,
    observation,
    expected_version,
    expected_fingerprint,
    request_key,
    reason,
    evidence,
):
    return _recover(
        organization=organization,
        actor=actor,
        observation=observation,
        expected_version=expected_version,
        expected_fingerprint=expected_fingerprint,
        request_key=request_key,
        reason=reason,
        evidence=evidence,
        action="withdraw",
        fields={},
    )


@transaction.atomic
def replace_ad_observation(
    *,
    organization,
    actor,
    observation,
    expected_version,
    expected_fingerprint,
    request_key,
    reason,
    evidence,
    **fields,
):
    return _recover(
        organization=organization,
        actor=actor,
        observation=observation,
        expected_version=expected_version,
        expected_fingerprint=expected_fingerprint,
        request_key=request_key,
        reason=reason,
        evidence=evidence,
        action="replace",
        fields=fields,
    )
