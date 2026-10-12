"""Documented identity decisions, never a live DJP verification claim."""

import hashlib
import re
from datetime import date

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from core.audit import record
from core.models import Membership, Organization
from finance.models import Party

from .knowledge import reviewed_sources_for_date
from .models import (
    RecipientIdentityApproval,
    RecipientTaxIdentity,
    TaxEvidence,
    TaxObligation,
    TaxSource,
    TaxSourcePublication,
    TaxSourceRevision,
)
from .rules import PPH23_START, calculate_pph23
from .workflow import (
    _assert_review,
    _save,
    _scope,
    digest,
    json_value,
    subject_snapshot,
)

IDENTITY_VERSION = "recipient-identity-v1"
IDENTITY_FIELDS = (
    "organization_id",
    "recipient_party_id",
    "legal_name",
    "name_variation_reason",
    "recipient_type",
    "tax_residency",
    "population_status",
    "identity_type",
    "number",
    "registration_status",
    "nik_validated",
    "nik_integrated",
    "nik_activated",
    "effective_from",
    "effective_until",
    "evidence_id",
    "reference",
    "reason",
    "previous_id",
    "revision",
    "request_key",
    "target_snapshot",
    "legal_sources",
    "prepared_by_id",
)
TREATMENTS = {
    "standard_npwp": 1,
    "standard_integrated_nik": 1,
    "increased_no_npwp": 2,
    "unresolved": None,
}


def _role(organization, actor, *, approve=False):
    roles = ["owner"] if approve else ["owner", "finance"]
    if (
        not getattr(actor, "is_authenticated", False)
        or not Membership.objects.filter(
            organization=organization, user=actor, user__is_active=True, role__in=roles
        ).exists()
    ):
        raise PermissionDenied(
            "Finance menyiapkan identitas; Owner mencatat persetujuan laporan profesional. Akses ini terbatas pada perusahaan Anda."
        )


def _locked(organization, actor, *, approve=False):
    organization = Organization.objects.select_for_update().get(pk=organization.pk)
    _role(organization, actor, approve=approve)
    return organization


def _evidence(row):
    if hashlib.sha256(bytes(row.content)).hexdigest() != row.sha256:
        raise ValidationError(
            "Isi dokumen identitas/pemeriksaan berubah; hentikan dan periksa dokumen asli."
        )
    return row.sha256


def _target(organization, party):
    organization = Organization.objects.get(pk=organization.pk)
    if party is not None:
        party = _scope(Party, party, organization)
    return {
        "organization_id": organization.pk,
        "recipient_party_id": party.pk if party else None,
        "canonical_name": party.name if party else organization.name,
    }


def _legal(point, *, integrated=False):
    slugs = ["pph23-identity-law", "pmk81-identity-2024", "per7-2025", "per11-2025"]
    if integrated:
        slugs.append("peng6-2024")
    # All writes hold the organization lock, then legal heads in stable order.
    # On read-only paths this is a normal query (no accidental transaction).
    if transaction.get_connection().in_atomic_block:
        list(
            TaxSource.objects.select_for_update()
            .filter(slug__in=slugs)
            .order_by("slug")
        )
    return reviewed_sources_for_date(point, slugs=slugs)


def _historical_legal(sources, point):
    """Retained publication integrity for read-only checks, never new actions."""
    if not isinstance(sources, list) or not sources:
        raise ValidationError("Riwayat versi hukum tidak tersedia.")
    for source in sources:
        if not isinstance(source, dict):
            raise ValidationError("Riwayat versi hukum tidak sah.")
        revision = TaxSourceRevision.objects.filter(
            pk=source.get("version_id"), source__slug=source.get("id")
        ).first()
        publication = (
            TaxSourcePublication.objects.filter(
                pk=source.get("publication_id"), revision=revision
            ).first()
            if revision
            else None
        )
        if (
            not revision
            or not publication
            or digest(revision.snapshot) != revision.content_hash
            or source.get("content_hash") != revision.content_hash
            or source.get("publication_reference") != publication.review_reference
            or source.get("application_from") != point.isoformat()
            or source.get("application_until") != point.isoformat()
        ):
            raise ValidationError(
                "Riwayat publikasi hukum berubah atau tidak sesuai tanggal terutang."
            )
        for field in (
            "title",
            "url",
            "article",
            "reviewed_on",
            "effective_from",
            "effective_until",
        ):
            if source.get(field) != revision.snapshot.get(field):
                raise ValidationError(
                    "Kutipan versi hukum tidak sama dengan publikasi yang tersimpan."
                )
    return sources


def _same_normative_sources(original, current, point):
    """Review-date renewal preserves an immutable legal binding, not new law."""
    _historical_legal(original, point)
    _historical_legal(current, point)
    if {row["id"] for row in original} != {row["id"] for row in current}:
        return False
    old_by_id = {row["id"]: row for row in original}
    for row in current:
        previous = TaxSourceRevision.objects.get(pk=old_by_id[row["id"]]["version_id"])
        latest = TaxSourceRevision.objects.get(pk=row["version_id"])
        old_facts = {
            key: value
            for key, value in previous.snapshot.items()
            if key != "reviewed_on"
        }
        new_facts = {
            key: value for key, value in latest.snapshot.items() if key != "reviewed_on"
        }
        if old_facts != new_facts:
            return False
    return True


def _review_date(review):
    # Reports do not expire simply by age. They bind dated immutable facts;
    # source freshness is a separate operator review policy, not a law claim.
    if (
        type(review.reviewed_on) is not date
        or review.reviewed_on > timezone.localdate()
    ):
        raise ValidationError(
            "Tanggal pemeriksaan profesional harus sesuai laporan yang sudah diterima."
        )


def identity_snapshot(identity):
    return json_value(
        {
            "version": IDENTITY_VERSION,
            **{field: getattr(identity, field) for field in IDENTITY_FIELDS},
            "evidence_sha256": _evidence(identity.evidence),
        }
    )


def identity_decision(*, treatment):
    if treatment not in TREATMENTS:
        raise ValidationError(
            "Pilih kesimpulan tarif identitas dari laporan profesional."
        )
    return {"action": "approve_recipient_identity", "treatment": treatment}


def _factor(identity, treatment):
    identity_decision(treatment=treatment)
    if treatment == "unresolved":
        return None
    if identity.tax_residency not in ("spdn", "but"):
        raise ValidationError(
            "Residensi belum mendukung PPh 23; rute PPh 26/penentuan subjek memerlukan penanganan profesional."
        )
    if treatment == "standard_npwp":
        if (
            identity.registration_status != "registered"
            or identity.identity_type not in ("npwp16", "nik")
        ):
            raise ValidationError(
                "Tarif standar memerlukan NPWP terdaftar yang terdokumentasi; format nomor saja tidak cukup."
            )
        if identity.identity_type == "nik" and (
            identity.population_status != "resident"
            or identity.nik_validated is not True
            or identity.nik_integrated is not True
            or identity.nik_activated is not True
        ):
            raise ValidationError(
                "NIK sebagai NPWP terdaftar memerlukan fakta validasi, integrasi, dan aktivasi yang terpisah."
            )
        if (
            identity.recipient_type == "person"
            and identity.identity_type == "npwp16"
            and identity.population_status != "nonresident"
        ):
            raise ValidationError(
                "Orang pribadi Penduduk menggunakan NIK yang ditinjau; kategori Penduduk belum diperiksa tidak dapat digantikan oleh format NPWP saja."
            )
    elif treatment == "standard_integrated_nik":
        if (
            identity.recipient_type != "person"
            or identity.population_status != "resident"
            or identity.identity_type != "nik"
            or identity.nik_validated is not True
            or identity.nik_integrated is not True
            or identity.registration_status in ("nonactive", "deleted", "unknown")
        ):
            raise ValidationError(
                "NIK terintegrasi harus didokumentasikan dan disimpulkan profesional; KTP/validasi saja tidak menetapkan tarif standar."
            )
    elif treatment == "increased_no_npwp":
        if (
            identity.registration_status != "no_npwp"
            or identity.identity_type != "none"
            or identity.number
            or identity.nik_integrated is True
        ):
            raise ValidationError(
                "Tarif naik hanya untuk tidak memiliki NPWP yang terdokumentasi, bukan nomor kosong atau NIK terintegrasi."
            )
    return TREATMENTS[treatment]


@transaction.atomic
def prepare_recipient_identity(
    *,
    organization,
    actor,
    legal_name,
    recipient_type,
    tax_residency,
    population_status,
    identity_type,
    registration_status,
    effective_from,
    evidence,
    reference,
    reason,
    request_key,
    recipient_party=None,
    number="",
    name_variation_reason="",
    nik_validated=None,
    nik_integrated=None,
    nik_activated=None,
    effective_until=None,
    previous=None,
):
    organization = _locked(organization, actor)
    party = (
        _scope(Party, recipient_party, organization)
        if recipient_party is not None
        else None
    )
    previous = (
        _scope(RecipientTaxIdentity, previous, organization, lock=True)
        if previous is not None
        else None
    )
    if (
        type(effective_from) is not date
        or effective_from < PPH23_START
        or (
            effective_until is not None
            and (type(effective_until) is not date or effective_until < effective_from)
        )
    ):
        raise ValidationError(
            "Tanggal berlaku baru didukung sejak 22 Mei 2025; isi tanggal kalender yang tepat."
        )
    if (
        any(
            not isinstance(value, str) or not value.strip()
            for value in (legal_name, reference, reason, request_key)
        )
        or not 10 <= len(reason.strip()) <= 2000
    ):
        raise ValidationError(
            "Nama, referensi dokumen, identitas permintaan, dan alasan minimal 10 karakter wajib diisi."
        )
    if not isinstance(number, str) or not isinstance(name_variation_reason, str):
        raise ValidationError("Nomor identitas disimpan sebagai teks sesuai dokumen.")
    number, legal_name = number.strip(), legal_name.strip()
    target = _target(organization, party)
    if legal_name != target["canonical_name"] and (
        party is None or len(name_variation_reason.strip()) < 10
    ):
        raise ValidationError(
            "Nama penerima harus cocok dengan nama legal perusahaan/pemasok. Variasi pemasok memerlukan alasan dokumen yang jelas."
        )
    if identity_type in ("npwp16", "nik") and (
        not re.fullmatch(r"[0-9]{16}", number) or len(set(number)) == 1
    ):
        raise ValidationError(
            "Masukkan 16 digit sesuai dokumen; aplikasi tidak mengubah nomor lama atau memverifikasi DJP otomatis."
        )
    if identity_type in ("none", "unknown") and number:
        raise ValidationError("Nomor hanya dicatat dengan jenis identitas yang jelas.")
    if recipient_type == "body" and (
        population_status != "na"
        or identity_type == "nik"
        or any(
            value is not None
            for value in (nik_validated, nik_integrated, nik_activated)
        )
    ):
        raise ValidationError("Badan memakai identitas badan, bukan NIK direktur/PIC.")
    if party is None and recipient_type != "body":
        raise ValidationError(
            "Penerima PT OSEE harus menggunakan identitas badan perusahaan sendiri."
        )
    if recipient_type == "person" and population_status == "na":
        raise ValidationError(
            "Status Penduduk orang pribadi harus dicatat terpisah dari residensi pajak."
        )
    if any(
        value is not None and type(value) is not bool
        for value in (nik_validated, nik_integrated, nik_activated)
    ):
        raise ValidationError("Fakta NIK harus ya, tidak, atau belum diperiksa.")
    evidence = _scope(TaxEvidence, evidence, organization)
    _evidence(evidence)
    legal = _legal(effective_from, integrated=identity_type == "nik")
    row = RecipientTaxIdentity(
        organization=organization,
        recipient_party=party,
        legal_name=legal_name,
        name_variation_reason=name_variation_reason.strip(),
        recipient_type=recipient_type,
        tax_residency=tax_residency,
        population_status=population_status,
        identity_type=identity_type,
        number=number,
        registration_status=registration_status,
        nik_validated=nik_validated,
        nik_integrated=nik_integrated,
        nik_activated=nik_activated,
        effective_from=effective_from,
        effective_until=effective_until,
        evidence=evidence,
        reference=reference.strip(),
        reason=reason.strip(),
        request_key=request_key.strip(),
        previous=previous,
        revision=previous.revision + 1 if previous else 1,
        target_snapshot=target,
        legal_sources=legal,
        prepared_by=actor,
    )
    row.fingerprint = digest(identity_snapshot(row))
    old = RecipientTaxIdentity.objects.filter(
        organization=organization, request_key=row.request_key
    ).first()
    if old:
        old_input, new_input = identity_snapshot(old), identity_snapshot(row)
        # A retry returns its recorded result. Operator source re-publication
        # is derived context, not a new user-entered request payload.
        old_input.pop("legal_sources")
        new_input.pop("legal_sources")
        if old_input == new_input and digest(identity_snapshot(old)) == old.fingerprint:
            return old
        raise ValidationError(
            "Permintaan ini sudah digunakan dengan data berbeda; muat ulang sebelum menyiapkan versi baru."
        )
    _save(row)
    record(
        organization,
        actor,
        "tax.recipient_identity.prepared",
        row,
        {
            "revision": row.revision,
            "fingerprint": row.fingerprint,
            "recipient_party_id": row.recipient_party_id,
        },
    )
    return row


def _integrity(identity, *, require_current=True):
    if digest(identity_snapshot(identity)) != identity.fingerprint:
        raise ValidationError(
            "Snapshot identitas berubah; pemeriksaan baru diperlukan."
        )
    if (
        require_current
        and _target(identity.organization, identity.recipient_party)
        != identity.target_snapshot
    ):
        raise ValidationError(
            "Nama legal/penerima berubah sejak pemeriksaan identitas; siapkan versi yang tepat."
        )
    try:
        approval = identity.approval
    except RecipientIdentityApproval.DoesNotExist:
        raise ValidationError(
            "Identitas penerima belum disetujui berdasarkan laporan profesional."
        ) from None
    factor = _factor(identity, approval.treatment)
    if (
        approval.organization_id != identity.organization_id
        or (identity.previous_id is None) != (approval.previous_approval_id is None)
        or (
            approval.previous_approval_id
            and approval.previous_approval.identity_id != identity.previous_id
        )
    ):
        raise ValidationError(
            "Jejak perusahaan atau versi sebelumnya pada persetujuan identitas tidak cocok."
        )
    expected = json_value(
        {
            "identity": identity_snapshot(identity),
            "treatment": approval.treatment,
            "factor": factor,
        }
    )
    if (
        approval.identity_fingerprint != identity.fingerprint
        or approval.factor != factor
        or approval.snapshot != expected
    ):
        raise ValidationError(
            "Jejak persetujuan identitas tidak cocok dengan snapshot yang tetap."
        )
    review = _assert_review(
        identity.organization,
        identity,
        identity_decision(treatment=approval.treatment),
        approval.external_review,
    )
    _evidence(review.evidence)
    _review_date(review)
    _historical_legal(identity.legal_sources, identity.effective_from)
    if require_current:
        if not _same_normative_sources(
            identity.legal_sources,
            _legal(identity.effective_from, integrated=identity.identity_type == "nik"),
            identity.effective_from,
        ):
            raise ValidationError(
                "Isi hukum identitas berubah; pemeriksaan profesional baru diperlukan sebelum tindakan pajak baru."
            )
    return approval


def _successor(identity):
    rows = list(
        RecipientTaxIdentity.objects.filter(
            organization=identity.organization,
            previous=identity,
            approval__isnull=False,
        ).order_by("effective_from", "pk")[:2]
    )
    if len(rows) > 1:
        raise ValidationError(
            "Ada lebih dari satu pengganti identitas; hentikan sebelum menentukan tarif."
        )
    return rows[0] if rows else None


def identity_status(*, organization, identity):
    identity = _scope(RecipientTaxIdentity, identity, organization)
    successor = _successor(identity)
    approval = RecipientIdentityApproval.objects.filter(identity=identity).first()
    obsolete_draft = None
    if not approval:
        ancestor_ids = []
        anchor = identity.previous
        while anchor:
            ancestor_ids.append(anchor.pk)
            anchor = anchor.previous
        obsolete_draft = (
            RecipientTaxIdentity.objects.filter(
                organization=organization,
                recipient_party_id=identity.recipient_party_id,
                approval__isnull=False,
            )
            .exclude(pk__in=ancestor_ids)
            .first()
        )
    issues = []
    try:
        if (
            digest(identity_snapshot(identity)) != identity.fingerprint
            or _target(organization, identity.recipient_party)
            != identity.target_snapshot
        ):
            raise ValidationError("Dokumen atau nama penerima berubah.")
        if not _same_normative_sources(
            identity.legal_sources,
            _legal(identity.effective_from, integrated=identity.identity_type == "nik"),
            identity.effective_from,
        ):
            raise ValidationError("Sumber identitas perlu pemeriksaan ulang.")
        if approval:
            _integrity(identity)
    except ValidationError as exc:
        issues = exc.messages
    status = (
        "stale"
        if issues
        else (
            "superseded"
            if successor or obsolete_draft
            else (
                "unresolved"
                if approval and approval.factor is None
                else ("approved" if approval else "draft")
            )
        )
    )
    return {
        "identity": identity,
        "status": status,
        "fingerprint": identity.fingerprint,
        "version": identity.revision,
        "approval": approval,
        "treatment": approval.treatment if approval else None,
        "factor": approval.factor if approval else None,
        "issues": issues,
        "can_approve": not approval and not issues and not obsolete_draft,
        "superseded_from": successor.effective_from
        if successor
        else (obsolete_draft.effective_from if obsolete_draft else None),
        "replacement_note": "Draf ini tetap tersimpan. Penerima sudah mempunyai identitas yang disetujui; gunakan versi tersebut untuk transaksi baru."
        if obsolete_draft
        else "",
    }


def _same_date_recovery_blocker(identity):
    from finance.models import (
        CustomerTaxSettlement,
        TaxRemittance,
        WithholdingSettlement,
    )

    from .models import TaxVerification

    affected = TaxObligation.objects.filter(organization=identity.organization).filter(
        Q(recipient_identity=identity) | Q(withholder_identity=identity)
    )
    if (
        affected.filter(status="approved").exists()
        or TaxVerification.objects.filter(obligation__in=affected).exists()
        or CustomerTaxSettlement.objects.filter(obligation__in=affected).exists()
        or WithholdingSettlement.objects.filter(obligation__in=affected).exists()
        or TaxRemittance.objects.filter(obligation__in=affected).exists()
    ):
        raise ValidationError(
            "Identitas sudah terkait persetujuan, bukti atau pencatatan pajak. Koreksi formal perlu ditangani profesional; riwayat tidak diganti."
        )


@transaction.atomic
def approve_recipient_identity(
    *,
    organization,
    actor,
    identity,
    treatment,
    external_review,
    expected_fingerprint,
    expected_version,
):
    organization = _locked(organization, actor, approve=True)
    identity = _scope(RecipientTaxIdentity, identity, organization, lock=True)
    if (
        expected_fingerprint != identity.fingerprint
        or type(expected_version) is not int
        or expected_version != identity.revision
    ):
        raise ValidationError(
            "Versi identitas berubah sejak halaman dibuka; muat ulang dan periksa laporan yang tepat."
        )
    factor = _factor(identity, treatment)
    review = _assert_review(
        organization, identity, identity_decision(treatment=treatment), external_review
    )
    _evidence(review.evidence)
    _review_date(review)
    old = RecipientIdentityApproval.objects.filter(identity=identity).first()
    if old:
        if old.external_review_id == review.pk and old.treatment == treatment:
            _integrity(identity, require_current=False)
            return old
        raise ValidationError(
            "Persetujuan identitas bersifat tetap; siapkan versi tanggal berikutnya untuk perubahan yang sah."
        )
    state = identity_status(organization=organization, identity=identity)
    if state["issues"]:
        raise ValidationError(state["issues"])
    if not state["can_approve"]:
        raise ValidationError(
            "Draf ini sudah digantikan identitas yang disetujui; gunakan versi penerima yang aktif."
        )
    previous = None
    existing = RecipientTaxIdentity.objects.filter(
        organization=organization,
        recipient_party_id=identity.recipient_party_id,
        approval__isnull=False,
    )
    if identity.previous_id:
        previous_identity = _scope(
            RecipientTaxIdentity, identity.previous_id, organization, lock=True
        )
        previous = _integrity(previous_identity, require_current=False)
        same_date = identity.effective_from == previous_identity.effective_from
        if (
            _successor(previous_identity)
            or previous_identity.recipient_party_id != identity.recipient_party_id
            or identity.effective_from < previous_identity.effective_from
            or (
                same_date
                and (
                    previous.treatment != "unresolved"
                    or previous.factor is not None
                    or factor not in (1, 2)
                )
            )
        ):
            raise ValidationError(
                "Versi sebelumnya sudah diganti atau tanggal perubahan tidak sah."
            )
        if same_date:
            _same_date_recovery_blocker(previous_identity)
        existing = existing.exclude(pk__in=[previous_identity.pk])
    elif existing.exists():
        raise ValidationError(
            "Penerima sudah mempunyai versi disetujui. Pilih versi sebelumnya untuk perubahan tanggal yang jelas."
        )
    # Ancestors are intentionally retained; any other branch is ambiguous.
    ancestor_ids = []
    anchor = identity.previous
    while anchor:
        ancestor_ids.append(anchor.pk)
        anchor = anchor.previous
    if existing.exclude(pk__in=ancestor_ids).exists():
        raise ValidationError(
            "Ada keputusan identitas yang bertentangan untuk penerima ini."
        )
    snapshot = json_value(
        {
            "identity": identity_snapshot(identity),
            "treatment": treatment,
            "factor": factor,
        }
    )
    approval = _save(
        RecipientIdentityApproval(
            organization=organization,
            identity=identity,
            previous_approval=previous,
            external_review=review,
            treatment=treatment,
            factor=factor,
            identity_fingerprint=identity.fingerprint,
            snapshot=snapshot,
            actor=actor,
        )
    )
    record(
        organization,
        actor,
        "tax.recipient_identity.approved",
        approval,
        {
            "identity_id": identity.pk,
            "treatment": treatment,
            "factor": factor,
            "fingerprint": identity.fingerprint,
        },
    )
    return approval


def resolve_recipient_identity(
    *, organization, recipient_party=None, tax_point_date, require_current=True
):
    if type(tax_point_date) is not date or tax_point_date < PPH23_START:
        raise ValidationError("Isi tanggal terutang yang tepat sejak 22 Mei 2025.")
    party = (
        _scope(Party, recipient_party, organization)
        if recipient_party is not None
        else None
    )
    candidates = []
    for identity in RecipientTaxIdentity.objects.filter(
        organization=organization,
        recipient_party_id=party.pk if party else None,
        approval__isnull=False,
        effective_from__lte=tax_point_date,
    ).select_related("evidence", "approval", "organization", "recipient_party"):
        if identity.effective_until and identity.effective_until < tax_point_date:
            continue
        successor = _successor(identity)
        if successor and successor.effective_from <= tax_point_date:
            continue
        candidates.append(identity)
    if len(candidates) != 1:
        raise ValidationError(
            "Belum ada satu identitas disetujui yang berlaku tepat pada tanggal terutang; lengkapi pemeriksaan penerima."
        )
    _integrity(candidates[0], require_current=require_current)
    return candidates[0]


def pph23_object_decision(
    *,
    route="pph23",
    reason,
    individual_21_26_excluded=False,
    final_exempt_excluded=False,
):
    if (
        not isinstance(reason, str)
        or not 10 <= len(reason.strip()) <= 2000
        or route not in ("pph23", "pph21", "pph26", "final", "exempt", "unknown")
        or type(individual_21_26_excluded) is not bool
        or type(final_exempt_excluded) is not bool
    ):
        raise ValidationError(
            "Catat rute, alasan profesional, dan konfirmasi pengecualian secara tepat."
        )
    return {
        "route": route,
        "reason": reason.strip(),
        "individual_21_26_excluded": individual_21_26_excluded,
        "final_exempt_excluded": final_exempt_excluded,
    }


def pph23_context(
    *,
    organization,
    period,
    rule_code,
    base,
    direction,
    recipient_identity,
    tax_point_date,
    tax_point_basis,
    object_decision,
    recipient_party=None,
    withholder_party=None,
    withholder_reference="",
    withholder_name="",
    source_bill=None,
    source_invoice=None,
    require_current=True,
    retained_legal_sources=None,
):
    """Resolve the two directions without using mutable Party.tax_id/Profile."""
    organization = Organization.objects.get(pk=organization.pk)
    if type(tax_point_date) is not date or tax_point_basis not in (
        "paid",
        "available",
        "due",
    ):
        raise ValidationError(
            "Pilih tanggal dan dasar waktu terutang sesuai laporan; tanggal bank/tagihan tidak dipilih otomatis."
        )
    party = (
        _scope(Party, recipient_party, organization)
        if recipient_party is not None
        else None
    )
    holder = (
        _scope(Party, withholder_party, organization)
        if withholder_party is not None
        else None
    )
    if direction == "payable":
        if source_invoice is not None or holder is not None:
            raise ValidationError("Potongan pemasok memakai OSEE sebagai pemotong.")
        if source_bill is not None:
            if party and party.pk != source_bill.supplier_id:
                raise ValidationError("Penerima harus pemasok tagihan yang tepat.")
            party = _scope(Party, source_bill.supplier_id, organization)
        if party is None:
            raise ValidationError(
                "PPh 23 manual kepada pemasok memerlukan penerima eksplisit."
            )
        own_identity = resolve_recipient_identity(
            organization=organization,
            tax_point_date=tax_point_date,
            require_current=require_current,
        )
        own_approval = _integrity(own_identity, require_current=require_current)
        if own_approval.factor != 1 or not own_identity.number:
            raise ValidationError(
                "Lengkapi identitas NPWP badan OSEE sebagai pemotong."
            )
        if (
            withholder_name != own_identity.legal_name
            or withholder_reference != own_identity.number
        ):
            raise ValidationError(
                "Nama dan NPWP pemotong harus cocok dengan identitas badan OSEE yang ditinjau."
            )
        holder_snapshot = {
            "party_id": None,
            "name": own_identity.legal_name,
            "reference": own_identity.number,
            "identity_id": own_identity.pk,
            "fingerprint": own_identity.fingerprint,
        }
    elif direction == "receivable":
        if party is not None or source_bill is not None:
            raise ValidationError("Potongan pelanggan memakai OSEE sebagai penerima.")
        own_identity = None
        if source_invoice is not None:
            if holder and holder.pk != source_invoice.party_id:
                raise ValidationError("Pemotong harus pelanggan invoice yang tepat.")
            holder = _scope(Party, source_invoice.party_id, organization)
        if (
            holder is None
            or (require_current and withholder_name != holder.name)
            or not isinstance(withholder_reference, str)
            or not re.fullmatch(r"[0-9]{16}", withholder_reference)
            or len(set(withholder_reference)) == 1
        ):
            raise ValidationError(
                "Pelanggan pemotong, nama sesuai dokumen, dan NPWP/NIK 16 digit yang ditinjau wajib diisi; nomor master tidak menjadi verifikasi."
            )
        holder_snapshot = {
            "party_id": holder.pk,
            "name": withholder_name,
            "reference": withholder_reference,
            "identity_id": None,
            "fingerprint": None,
        }
    else:
        raise ValidationError("Arah ini bukan PPh 23 yang didukung.")
    identity = _scope(RecipientTaxIdentity, recipient_identity, organization)
    resolved = resolve_recipient_identity(
        organization=organization,
        recipient_party=party,
        tax_point_date=tax_point_date,
        require_current=require_current,
    )
    if resolved.pk != identity.pk:
        raise ValidationError(
            "Identitas yang dipilih tidak berlaku tepat pada tanggal terutang."
        )
    approval = _integrity(identity, require_current=require_current)
    facts = {
        "identity_id": identity.pk,
        "fingerprint": identity.fingerprint,
        "approval_id": approval.pk,
        "treatment": approval.treatment,
        "factor": approval.factor,
        "recipient_type": identity.recipient_type,
        "recipient_party_id": party.pk if party else None,
        "name": identity.legal_name,
        "reference": identity.number,
    }
    if require_current:
        current_legal = _legal(
            tax_point_date, integrated=approval.treatment == "standard_integrated_nik"
        )
        if retained_legal_sources is not None:
            if not _same_normative_sources(
                retained_legal_sources, current_legal, tax_point_date
            ):
                raise ValidationError(
                    "Isi atau masa berlaku hukum PPh 23 berubah; pemeriksaan baru diperlukan sebelum tindakan baru."
                )
            legal = retained_legal_sources
        else:
            legal = current_legal
    else:
        legal = _historical_legal(retained_legal_sources, tax_point_date)
    calc = calculate_pph23(
        rule_code=rule_code,
        base=base,
        period=period,
        tax_point_date=tax_point_date,
        recipient_identity_decision=facts,
        object_decision=object_decision,
        legal_sources=legal,
    )
    return {
        "recipient_identity": identity,
        "withholder_identity": own_identity,
        "recipient_party": party,
        "withholder_party": holder,
        "identity_snapshot": json_value(
            {
                "recipient": facts,
                "withholder": holder_snapshot,
                "raw_base": str(calc["raw_base"]),
            }
        ),
        "legal_sources": legal,
        "calculation": calc,
    }


def assert_pph23_obligation(obligation, *, require_current=True):
    """Gate a new approval/payment/verification; historical reads stay intact."""
    if obligation.tax_type != "pph23":
        return None
    if not obligation.recipient_identity_id or not obligation.tax_point_date:
        raise ValidationError(
            "Kertas kerja PPh 23 lama belum mengikat identitas penerima. Lengkapi pemeriksaan baru sebelum persetujuan atau pencatatan pajak baru; riwayat tetap tersimpan."
        )
    if not obligation.source_evidence_id or (
        obligation.status == "approved" and not obligation.external_review_id
    ):
        raise ValidationError(
            "Dokumen sumber dan laporan profesional PPh 23 yang disetujui harus tetap terikat."
        )
    if not isinstance(obligation.identity_snapshot, dict) or not isinstance(
        obligation.identity_snapshot.get("withholder"), dict
    ):
        raise ValidationError("Snapshot identitas PPh 23 tidak sah.")
    organization = Organization.objects.get(pk=obligation.organization_id)
    source_bill = (
        _scope(type(obligation.source_bill), obligation.source_bill, organization)
        if obligation.source_bill_id
        else None
    )
    source_invoice = (
        _scope(type(obligation.source_invoice), obligation.source_invoice, organization)
        if obligation.source_invoice_id
        else None
    )
    holder = obligation.identity_snapshot.get("withholder", {})
    context = pph23_context(
        organization=organization,
        period=obligation.period,
        rule_code=obligation.rule_code,
        base=obligation.identity_snapshot.get("raw_base"),
        direction=obligation.direction,
        recipient_identity=obligation.recipient_identity_id,
        tax_point_date=obligation.tax_point_date,
        tax_point_basis=obligation.tax_point_basis,
        object_decision=obligation.object_decision,
        recipient_party=obligation.recipient_party_id,
        withholder_party=obligation.withholder_party_id,
        withholder_reference=holder.get("reference", ""),
        withholder_name=holder.get("name", ""),
        source_bill=source_bill,
        source_invoice=source_invoice,
        require_current=require_current,
        retained_legal_sources=obligation.legal_sources,
    )
    calc = context["calculation"]
    if (
        context["identity_snapshot"] != obligation.identity_snapshot
        or context["legal_sources"] != obligation.legal_sources
        or (
            context["withholder_identity"].pk
            if context["withholder_identity"]
            else None
        )
        != obligation.withholder_identity_id
    ):
        raise ValidationError(
            "Identitas, pemotong, atau versi hukum berubah; siapkan dan tinjau kertas kerja yang tepat."
        )
    if (
        obligation.calculation_version != calc["version"]
        or obligation.base != calc["base"]
        or obligation.rate != calc["rate"]
        or obligation.amount != calc["amount"]
    ):
        raise ValidationError(
            "Tarif/nominal PPh 23 tidak sesuai keputusan identitas yang terikat."
        )
    if obligation.source_evidence_id:
        _evidence(_scope(TaxEvidence, obligation.source_evidence_id, organization))
    if obligation.external_review_id:
        review = obligation.external_review
        if not isinstance(review.decision, dict):
            raise ValidationError(
                "Keputusan laporan PPh 23 tidak sah; hentikan dan periksa laporan asli."
            )
        _evidence(review.evidence)
        _review_date(review)
        if review.subject_type == "bill" and source_bill:
            from .workflow import bill_decision

            decision = bill_decision(
                rule_code=obligation.rule_code,
                base=obligation.identity_snapshot["raw_base"],
                period=obligation.period,
                reason=review.decision.get("reason", ""),
                object_code=obligation.object_code,
                recipient_identity=obligation.recipient_identity,
                tax_point_date=obligation.tax_point_date,
                tax_point_basis=obligation.tax_point_basis,
                object_decision=obligation.object_decision,
                withholder_name=holder.get("name", ""),
                withholder_reference=holder.get("reference", ""),
                retained_legal_sources=obligation.legal_sources,
                require_current=require_current,
            )
            _assert_review(organization, source_bill, decision, review)
            if TaxObligation.objects.filter(
                pk=obligation.pk, status="approved"
            ).exists():
                from .models import BillTaxDecision

                bill_dec = BillTaxDecision.objects.filter(
                    organization=organization,
                    bill=source_bill,
                    obligation=obligation,
                    external_review=review,
                    rule_code=obligation.rule_code,
                    base=obligation.base,
                    rate=obligation.rate,
                    amount=obligation.amount,
                    period=obligation.period,
                    reason=review.decision.get("reason", ""),
                ).first()
                if bill_dec is None:
                    raise ValidationError(
                        "Keputusan tagihan yang terikat belum lengkap atau berubah; hentikan pencatatan pajak baru."
                    )
        else:
            _assert_review(
                organization, obligation, {"action": "approve_obligation"}, review
            )
    return calc


def pph23_certificate_snapshot(
    obligation,
    *,
    evidence,
    reference,
    period,
    amount,
    recipient_reference,
    recipient_name,
    withholder_reference,
    withholder_name,
    object_code,
    base,
    tax_point_date,
    certificate_status="normal",
):
    from .rules import number

    recipient = obligation.identity_snapshot.get("recipient", {})
    holder = obligation.identity_snapshot.get("withholder", {})
    if (
        certificate_status != "normal"
        or type(tax_point_date) is not date
        or tax_point_date != obligation.tax_point_date
    ):
        raise ValidationError(
            "Bukti potong baru harus berstatus normal dan cocok dengan tanggal terutang; pembetulan/pembatalan memerlukan koreksi formal."
        )
    if (
        recipient_reference != recipient.get("reference")
        or recipient_name != recipient.get("name")
        or withholder_reference != holder.get("reference")
        or withholder_name != holder.get("name")
        or object_code != obligation.object_code
        or number(base) != obligation.base
        or period != obligation.period
        or number(amount) != obligation.amount
    ):
        raise ValidationError(
            "Identitas penerima/pemotong, objek, dasar, tanggal terutang, masa dan potongan harus cocok tepat dengan kertas kerja yang ditinjau."
        )
    return json_value(
        {
            "recipient_reference": recipient_reference,
            "recipient_name": recipient_name,
            "withholder_reference": withholder_reference,
            "withholder_name": withholder_name,
            "object_code": object_code,
            "base": format(number(base), ".2f"),
            "tax_point_date": tax_point_date,
            "certificate_status": certificate_status,
            "reference": reference,
            "period": period,
            "amount": format(number(amount), ".2f"),
            "evidence_id": evidence.pk,
            "evidence_sha256": _evidence(evidence),
        }
    )


def assert_pph23_credit_verification(verification, obligation):
    """Current new use of the exact immutable certificate, separate from cash."""
    obligation = _scope(TaxObligation, obligation, obligation.organization)
    assert_pph23_obligation(obligation)
    from .models import TaxVerification

    verification = _scope(TaxVerification, verification, obligation.organization)
    if (
        verification.obligation_id != obligation.pk
        or verification.kind != "credit"
        or verification.obligation_digest != digest(subject_snapshot(obligation))
        or not isinstance(verification.certificate_snapshot, dict)
        or not verification.certificate_snapshot
    ):
        raise ValidationError(
            "Bukti kredit belum mengikat identitas dan versi kertas kerja ini; riwayat lama tetap tersedia untuk pemeriksaan profesional."
        )
    original = verification.certificate_snapshot
    try:
        point = (
            date.fromisoformat(original["tax_point_date"])
            if isinstance(original.get("tax_point_date"), str)
            else None
        )
    except ValueError:
        raise ValidationError("Tanggal pada jejak bukti potong tidak sah.") from None
    expected = pph23_certificate_snapshot(
        obligation,
        evidence=verification.evidence,
        reference=verification.reference,
        period=verification.period,
        amount=verification.amount,
        **{
            key: original.get(key)
            for key in (
                "recipient_reference",
                "recipient_name",
                "withholder_reference",
                "withholder_name",
                "object_code",
                "base",
            )
        },
        tax_point_date=point,
        certificate_status=original.get("certificate_status"),
    )
    if (
        expected != original
        or verification.taxpayer_reference
        != obligation.identity_snapshot["recipient"]["reference"]
    ):
        raise ValidationError(
            "Dokumen atau hasil pencocokan bukti potong berubah; hentikan penggunaan kredit pajak baru."
        )
    return verification
