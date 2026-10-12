"""Authorized document-led preparation, external review and evidence matching.

Owner authorization records a professional's documented decision; it does not
certify that professional's credentials or make the owner a tax specialist.
"""

import hashlib
import json
import re
from datetime import date
from decimal import Decimal
from pathlib import PurePath
from types import SimpleNamespace

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from core.audit import record
from core.models import Membership, Organization

from .models import (
    AnnualTaxApproval,
    AnnualTaxFiling,
    AnnualTaxWorkpaper,
    BillTaxDecision,
    ExternalTaxReview,
    TaxEvidence,
    TaxObligation,
    TaxObligationWithdrawal,
    TaxProfile,
    TaxProfileDecision,
    TaxVerification,
)
from .rules import (
    ANNUAL_WORKPAPER_YEARS,
    CALCULATION_VERSION,
    calculate_annual,
    calculate_tax,
    number,
    validate_monthly_period,
)


def authorize(organization, actor, *, approve=False):
    roles = ("owner", "reviewer") if approve else ("owner", "finance", "reviewer")
    if (
        not getattr(actor, "is_authenticated", False)
        or not Membership.objects.filter(
            organization=organization, user=actor, user__is_active=True, role__in=roles
        ).exists()
    ):
        raise PermissionDenied(
            "Lakukan dengan akun aktif dan kewenangan yang sesuai. Persetujuan pemilik mencatat laporan profesional, bukan menggantikan pemeriksaannya."
        )


def _lock(organization, actor, *, approve=False):
    Organization.objects.select_for_update().get(pk=organization.pk)
    authorize(organization, actor, approve=approve)


def _scope(model, value, organization, *, lock=False):
    pk = value.pk if isinstance(value, model) else value
    query = model.objects.select_for_update() if lock else model.objects
    try:
        return query.get(pk=pk, organization=organization)
    except (model.DoesNotExist, ValueError, TypeError):
        raise ValidationError("Catatan tidak tersedia untuk perusahaan ini.") from None


def _save(row):
    row._tax_write = True
    try:
        row.save()
    finally:
        row.__dict__.pop("_tax_write", None)
    return row


def json_value(value):
    return json.loads(
        json.dumps(
            value,
            default=lambda item: (
                item.isoformat() if isinstance(item, date) else str(item)
            ),
            sort_keys=True,
        )
    )


def digest(value):
    return hashlib.sha256(
        json.dumps(json_value(value), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def subject_snapshot(subject):
    names = {
        "taxprofile": (
            "organization_id",
            "registration_date",
            "registration_evidence",
            "taxpayer_reference",
            "vat_status_effective_from",
            "vat_status_evidence",
        ),
        "bill": (
            "organization_id",
            "number",
            "supplier_id",
            "amount",
            "supplier_vat",
            "date",
            "service_date",
            "category",
            "status",
        ),
        "taxobligation": (
            "organization_id",
            "period",
            "tax_type",
            "direction",
            "base",
            "rate",
            "amount",
            "source_bill_id",
            "source_invoice_id",
            "source_package_id",
            "rule_code",
            "calculation_version",
            "object_code",
            "source_evidence_id",
            "source_reference",
            "rule_reference",
            "reporting_method",
        ),
        "annualtaxworkpaper": (
            "organization_id",
            "year",
            "book_snapshot",
            "inputs",
            "result",
            "calculation_version",
        ),
        "monthlytaxsourcepackage": (
            "organization_id",
            "period",
            "cutoff",
            "revision",
            "snapshot",
            "source_hash",
            "profile_snapshot",
            "legal_sources",
            "package_hash",
            "extraction_version",
        ),
    }
    kind = subject._meta.model_name
    if kind == "supplieradvance":
        from .supplier_advance_review import supplier_advance_subject_snapshot

        return supplier_advance_subject_snapshot(
            organization=subject.organization, advance=subject
        )
    if kind == "openingbalancesettlement":
        from .opening_balance_review import opening_settlement_subject_snapshot

        return opening_settlement_subject_snapshot(
            organization=subject.organization, settlement=subject
        )
    if kind == "openingbalancerefund":
        from .opening_refund_review import opening_refund_subject_snapshot

        return opening_refund_subject_snapshot(
            organization=subject.organization, refund=subject
        )
    if kind == "recipienttaxidentity":
        from .recipient_identity import identity_snapshot

        return {"kind": kind, "id": subject.pk, **identity_snapshot(subject)}
    if kind not in names:
        raise ValidationError("Jenis pemeriksaan tidak didukung.")
    snapshot = {
        "kind": kind,
        "id": subject.pk,
        **{name: getattr(subject, name) for name in names[kind]},
    }
    # Preserve the identity of historical, unbound obligations. A bound invoice
    # is part of every new review and certificate digest and cannot be changed.
    if kind == "taxobligation" and subject.source_invoice_id is None:
        snapshot.pop("source_invoice_id")
    if kind == "taxobligation" and subject.source_package_id is None:
        snapshot.pop("source_package_id")
    if kind == "taxobligation" and subject.recipient_identity_id is not None:
        snapshot.update(
            {
                name: getattr(subject, name)
                for name in (
                    "recipient_identity_id",
                    "withholder_identity_id",
                    "recipient_party_id",
                    "withholder_party_id",
                    "tax_point_date",
                    "tax_point_basis",
                    "identity_snapshot",
                    "object_decision",
                    "legal_sources",
                )
            }
        )
    return snapshot


@transaction.atomic
def upload_evidence(*, organization, actor, file):
    from evidence.services import validate_pdf_content

    _lock(organization, actor)
    content = file.read(2 * 1024 * 1024 + 1)
    if len(content) > 2 * 1024 * 1024 or not content.startswith(b"%PDF-"):
        raise ValidationError("Unggah PDF hingga 2 MB.")
    validate_pdf_content(content, max_pages=200)
    filename = PurePath(
        str(getattr(file, "name", "bukti.pdf")).replace("\\", "/")
    ).name[:200]
    row = _save(
        TaxEvidence(
            organization=organization,
            uploaded_by=actor,
            original_filename=filename,
            content=content,
            sha256=hashlib.sha256(content).hexdigest(),
        )
    )
    record(organization, actor, "tax.evidence.uploaded", row, {"sha256": row.sha256})
    return row


@transaction.atomic
def record_external_review(
    *,
    organization,
    actor,
    subject,
    decision,
    evidence,
    professional_name,
    qualification_reference,
    reviewed_on,
    statement,
    report_confirmed=False,
):
    _lock(organization, actor, approve=True)
    subject = _scope(type(subject), subject, organization, lock=True)
    if (
        subject._meta.model_name == "recipienttaxidentity"
        or (subject._meta.model_name == "taxobligation" and subject.tax_type == "pph23")
        or (
            subject._meta.model_name == "bill"
            and isinstance(decision, dict)
            and decision.get("rule_code") in ("pph23_service_2", "pph23_other_15")
        )
    ):
        from .recipient_identity import _role

        _role(organization, actor, approve=True)
    evidence = _scope(TaxEvidence, evidence, organization)
    original_hash = hashlib.sha256(bytes(evidence.content)).hexdigest()
    if original_hash != evidence.sha256:
        raise ValidationError(
            "Dokumen laporan berubah; unggah dan periksa laporan asli."
        )
    if (
        report_confirmed is not True
        or type(reviewed_on) is not date
        or reviewed_on > timezone.localdate()
    ):
        raise ValidationError(
            "Konfirmasi laporan profesional yang benar-benar diterima, dengan tanggal pemeriksaan yang sudah terjadi."
        )
    if any(
        not isinstance(item, str) or len(item.strip()) < 3
        for item in (professional_name, qualification_reference, statement)
    ):
        raise ValidationError(
            "Nama profesional, referensi kompetensi/penugasan, dan kesimpulan laporan wajib diisi."
        )
    decision_limit = (
        1024 * 1024 if subject._meta.model_name == "monthlytaxsourcepackage" else 20000
    )
    if (
        not isinstance(decision, dict)
        or len(
            json.dumps(
                json_value(decision),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        > decision_limit
    ):
        raise ValidationError("Keputusan pemeriksaan tidak sah.")
    row = _save(
        ExternalTaxReview(
            organization=organization,
            evidence=evidence,
            evidence_sha256=original_hash,
            professional_name=professional_name.strip(),
            qualification_reference=qualification_reference.strip(),
            reviewed_on=reviewed_on,
            statement=statement.strip(),
            subject_type=subject._meta.model_name,
            subject_id=subject.pk,
            subject_digest=digest(
                {"subject": subject_snapshot(subject), "decision": decision}
            ),
            decision=json_value(decision),
            recorded_by=actor,
        )
    )
    record(
        organization,
        actor,
        "tax.external_review.recorded",
        row,
        {
            "subject_type": row.subject_type,
            "subject_id": row.subject_id,
            "evidence_sha256": evidence.sha256,
            "credentials_independently_verified": False,
        },
    )
    return row


def _assert_review(organization, subject, decision, review):
    review = _scope(ExternalTaxReview, review, organization)
    if (
        review.subject_type != subject._meta.model_name
        or review.subject_id != subject.pk
        or review.decision != json_value(decision)
        or review.subject_digest
        != digest({"subject": subject_snapshot(subject), "decision": decision})
    ):
        raise ValidationError(
            "Laporan pemeriksaan tidak cocok dengan catatan dan perhitungan ini; buat pemeriksaan baru setelah perubahan."
        )
    identity_bound = (
        subject._meta.model_name
        in (
            "recipienttaxidentity",
            "supplieradvance",
            "openingbalancesettlement",
            "openingbalancerefund",
        )
        or (
            subject._meta.model_name == "taxobligation"
            and subject.recipient_identity_id is not None
        )
        or (
            subject._meta.model_name == "bill"
            and decision.get("rule_code") in ("pph23_service_2", "pph23_other_15")
        )
    )
    if identity_bound and not review.evidence_sha256:
        if subject._meta.model_name == "openingbalancerefund":
            raise ValidationError(
                "Laporan pengembalian uang wajib mengikat hash PDF asli yang diterima."
            )
        if subject._meta.model_name == "openingbalancesettlement":
            raise ValidationError(
                "Laporan kas saldo awal wajib mengikat hash PDF asli yang diterima."
            )
        if subject._meta.model_name == "supplieradvance":
            raise ValidationError(
                "Laporan uang muka pemasok wajib mengikat hash PDF asli yang diterima."
            )
        raise ValidationError(
            "Laporan belum mengikat hash dokumen asli untuk pemeriksaan identitas PPh 23 baru."
        )
    if review.evidence_sha256 and (
        review.evidence_sha256 != review.evidence.sha256
        or review.evidence_sha256
        != hashlib.sha256(bytes(review.evidence.content)).hexdigest()
    ):
        raise ValidationError(
            "Isi laporan profesional berubah dari dokumen asli yang dicatat; pemeriksaan baru diperlukan."
        )
    return review


def profile_decision(
    *,
    regime,
    effective_from,
    final_regime_start_date=None,
    final_regime_end_date=None,
    transition_evidence="",
    qualifying_turnover=None,
    legacy_eligibility_confirmed=False,
    no_normal_election_confirmed=False,
    **unused,
):
    return json_value(
        {
            "regime": regime,
            "effective_from": effective_from,
            "final_regime_start_date": final_regime_start_date,
            "final_regime_end_date": final_regime_end_date,
            "transition_evidence": transition_evidence,
            "qualifying_turnover": str(number(qualifying_turnover))
            if qualifying_turnover is not None
            else None,
            "legacy_eligibility_confirmed": legacy_eligibility_confirmed,
            "no_normal_election_confirmed": no_normal_election_confirmed,
        }
    )


@transaction.atomic
def approve_tax_profile(
    profile,
    actor,
    *,
    regime,
    effective_from,
    review_note,
    final_regime_start_date=None,
    final_regime_end_date=None,
    transition_evidence="",
    external_review=None,
    qualifying_turnover=None,
    legacy_eligibility_confirmed=False,
    no_normal_election_confirmed=False,
):
    organization = profile.organization
    _lock(organization, actor, approve=True)
    profile = _scope(TaxProfile, profile, organization, lock=True)
    if external_review is None:
        if Membership.objects.filter(
            organization=organization, user=actor, role="owner"
        ).exists():
            raise PermissionDenied(
                "Lejitimasikan keputusan dengan laporan pemeriksa eksternal terlebih dahulu; pemilik tidak menetapkan pajak sendiri."
            )
        raise ValidationError(
            "Laporan pemeriksaan profesional wajib dilampirkan, termasuk untuk akun peninjau lama."
        )
    if (
        regime not in ("final", "normal")
        or not isinstance(effective_from, date)
        or effective_from.day != 1
    ):
        raise ValidationError("Pilih aturan dan tanggal pertama masa berlaku.")
    decision = profile_decision(
        regime=regime,
        effective_from=effective_from,
        final_regime_start_date=final_regime_start_date,
        final_regime_end_date=final_regime_end_date,
        transition_evidence=transition_evidence,
        qualifying_turnover=qualifying_turnover,
        legacy_eligibility_confirmed=legacy_eligibility_confirmed,
        no_normal_election_confirmed=no_normal_election_confirmed,
    )
    review = _assert_review(organization, profile, decision, external_review)
    if (
        not profile.registration_date
        or profile.registration_date > timezone.localdate()
        or effective_from < profile.registration_date.replace(day=1)
        or effective_from > timezone.localdate().replace(day=1)
    ):
        raise ValidationError(
            "Masa aktivasi harus sejak bulan terdaftar sampai masa berjalan; tanggal dokumen tidak boleh di masa depan."
        )
    if not profile.taxpayer_reference.strip():
        raise ValidationError(
            "Identitas pajak perusahaan yang dicocokkan dengan dokumen wajib tersedia."
        )
    for name, value in decision.items():
        setattr(
            profile,
            name,
            date.fromisoformat(value)
            if value and (name.endswith("date") or name == "effective_from")
            else value,
        )
    profile.external_review, profile.review_note = review, review_note
    profile.reviewed_by, profile.reviewed_at = actor, timezone.now()
    _save(profile)
    snapshot = json_value(
        {
            **subject_snapshot(profile),
            **decision,
            "reviewed_at": profile.reviewed_at,
            "external_review_id": review.pk,
        }
    )
    _save(
        TaxProfileDecision(
            organization=organization,
            profile=profile,
            external_review=review,
            effective_from=effective_from,
            snapshot=snapshot,
        )
    )
    record(
        organization,
        actor,
        "tax.profile.approved",
        profile,
        {
            "regime": regime,
            "external_review": review.pk,
            "effective_from": effective_from.isoformat(),
        },
    )
    return profile


def profile_for_period(organization, period):
    profile = TaxProfile.objects.filter(organization=organization).first()
    row = (
        TaxProfileDecision.objects.filter(
            organization=organization, effective_from__lte=period
        )
        .order_by("-effective_from", "-pk")
        .first()
    )
    if not row:
        return profile
    data = dict(row.snapshot)
    for name in (
        "registration_date",
        "effective_from",
        "final_regime_start_date",
        "final_regime_end_date",
        "vat_status_effective_from",
    ):
        data[name] = date.fromisoformat(data[name]) if data.get(name) else None
    return SimpleNamespace(**data)


@transaction.atomic
def prepare_obligation(
    *,
    organization,
    actor,
    period,
    rule_code,
    base,
    direction,
    source_reference,
    rule_reference,
    object_code,
    source_evidence,
    note="",
    source_bill=None,
    source_invoice=None,
    source_package=None,
    recipient_identity=None,
    tax_point_date=None,
    tax_point_basis="",
    recipient_party=None,
    withholder_party=None,
    withholder_reference="",
    withholder_name="",
    object_decision=None,
):
    _lock(organization, actor)
    from .services import parse_period, profile_gates

    period = parse_period(period) if isinstance(period, str) else period
    validate_monthly_period(period)
    if (
        not str(source_reference).strip()
        or not str(rule_reference).strip()
        or not re.fullmatch(r"[A-Za-z0-9.-]{3,40}", object_code)
    ):
        raise ValidationError(
            "Referensi sumber, dasar hukum, dan kode objek yang diperiksa wajib diisi."
        )
    source_evidence = _scope(TaxEvidence, source_evidence, organization)
    calc = (
        calculate_tax(rule_code, base)
        if rule_code not in ("pph23_service_2", "pph23_other_15")
        else None
    )
    if rule_code == "no_withholding":
        raise ValidationError(
            "Keputusan tidak dipotong dicatat pada tinjauan tagihan, bukan kewajiban pajak nihil."
        )
    tax_type = "final_turnover" if rule_code == "final_turnover_005" else "pph23"
    if (tax_type == "final_turnover" and direction != "own_tax") or (
        tax_type == "pph23" and direction not in ("payable", "receivable")
    ):
        raise ValidationError("Arah kewajiban tidak sesuai jenis pajak.")
    if tax_type == "final_turnover":
        profile, gates = profile_gates(organization, period)
        if gates or profile.regime != "final":
            raise ValidationError("Profil masa ini belum memvalidasi hak PPh final.")
    if source_bill is not None:
        from finance.models import Bill

        source_bill = _scope(Bill, source_bill, organization)
    if source_invoice is not None:
        from finance.models import Invoice

        source_invoice = _scope(Invoice, source_invoice, organization, lock=True)
    if source_package is not None:
        from .models import MonthlyTaxSourcePackage
        from .monthly_source_workflow import _prepare_role

        _prepare_role(organization, actor)
        source_package = _scope(
            MonthlyTaxSourcePackage, source_package, organization, lock=True
        )
        if (
            TaxObligation.objects.filter(
                organization=organization, period=period, tax_type="final_turnover"
            )
            .exclude(status="withdrawn")
            .exists()
        ):
            raise ValidationError(
                "Masa ini sudah memiliki kewajiban final aktif; jangan membuat draf ganda."
            )
    context = None
    if tax_type == "pph23":
        from .recipient_identity import _role, pph23_context

        _role(organization, actor)
        context = pph23_context(
            organization=organization,
            period=period,
            rule_code=rule_code,
            base=base,
            direction=direction,
            recipient_identity=recipient_identity,
            tax_point_date=tax_point_date,
            tax_point_basis=tax_point_basis,
            recipient_party=recipient_party,
            withholder_party=withholder_party,
            withholder_reference=withholder_reference,
            withholder_name=withholder_name,
            object_decision=object_decision,
            source_bill=source_bill,
            source_invoice=source_invoice,
        )
        calc = context["calculation"]
    row = TaxObligation(
        organization=organization,
        period=period,
        tax_type=tax_type,
        direction=direction,
        base=calc["base"],
        rate=calc["rate"],
        amount=calc["amount"],
        source_reference=source_reference,
        rule_reference=rule_reference,
        object_code=object_code,
        source_evidence=source_evidence,
        rule_code=rule_code,
        calculation_version=calc["version"],
        note=note,
        source_bill=source_bill,
        source_invoice=source_invoice,
        source_package=source_package,
        prepared_by=actor,
        status="review",
        reporting_method="validated_self_payment"
        if tax_type == "final_turnover"
        else "return_receipt",
    )
    if context:
        for key in (
            "recipient_identity",
            "withholder_identity",
            "recipient_party",
            "withholder_party",
            "identity_snapshot",
            "legal_sources",
        ):
            setattr(row, key, context[key])
        row.tax_point_date, row.tax_point_basis, row.object_decision = (
            tax_point_date,
            tax_point_basis,
            json_value(object_decision),
        )
    if source_package is not None:
        from .monthly_source_workflow import assert_package_for_obligation

        assert_package_for_obligation(organization, row, lock_sources=True)
    _save(row)
    record(
        organization,
        actor,
        "tax.obligation.prepared",
        row,
        {"calculation_version": calc["version"]},
    )
    return row


@transaction.atomic
def approve_tax_obligation(obligation, actor, *, external_review=None):
    organization = obligation.organization
    _lock(organization, actor, approve=True)
    obligation = _scope(TaxObligation, obligation, organization, lock=True)
    if obligation.source_package_id:
        from .monthly_source_workflow import _owner, assert_package_for_obligation

        _owner(organization, actor)
        assert_package_for_obligation(organization, obligation, lock_sources=True)
    if obligation.status not in ("draft", "review"):
        raise ValidationError(
            "Hanya draf aktif yang dapat disetujui; kertas kerja disetujui atau ditarik tidak diubah."
        )
    if external_review is None:
        raise ValidationError(
            "Persetujuan memerlukan laporan profesional yang sesuai perhitungan."
        )
    if obligation.tax_type == "pph23":
        from .recipient_identity import _role, assert_pph23_obligation

        _role(organization, actor, approve=True)
        assert_pph23_obligation(obligation)
    review = _assert_review(
        organization, obligation, {"action": "approve_obligation"}, external_review
    )
    obligation.status, obligation.external_review = "approved", review
    obligation.reviewed_by, obligation.reviewed_at = actor, timezone.now()
    _save(obligation)
    record(
        organization,
        actor,
        "tax.obligation.approved",
        obligation,
        {
            "external_review": review.pk,
            "calculation_version": obligation.calculation_version,
        },
    )
    return obligation


def withdrawal_blocker(obligation):
    """Input evidence is retained; only downstream decisions block withdrawal."""
    if obligation.status not in ("draft", "review"):
        return "Hanya draf yang belum disetujui dapat ditarik. Kertas kerja disetujui memerlukan koreksi formal yang ditinjau profesional."
    if (
        obligation.source_bill_id
        or BillTaxDecision.objects.filter(obligation=obligation).exists()
    ):
        return "Kertas kerja terkait tagihan memerlukan koreksi formal bersama keputusan pajak tagihan dan pencatatan keuangannya."
    if (
        obligation.reviewed_by_id
        or obligation.reviewed_at
        or obligation.external_review_id
        or obligation.payment_reference
        or obligation.payment_verified_at
        or obligation.filing_reference
        or obligation.filing_verified_at
        or obligation.verifications.exists()
        or hasattr(obligation, "withholding_settlement")
        or obligation.remittances.exists()
    ):
        return "Kertas kerja memiliki pemeriksaan atau bukti penyelesaian; gunakan koreksi formal yang ditinjau profesional."
    return ""


@transaction.atomic
def withdraw_obligation(*, organization, actor, obligation, reason, expected_version):
    _lock(organization, actor)
    obligation = _scope(TaxObligation, obligation, organization, lock=True)
    if not isinstance(reason, str) or not 10 <= len(reason.strip()) <= 2000:
        raise ValidationError("Jelaskan alasan penarikan dalam 10–2.000 karakter.")
    reason = reason.strip()
    previous = TaxObligationWithdrawal.objects.filter(
        organization=organization, obligation=obligation
    ).first()
    if previous:
        if (
            previous.actor_id == actor.pk
            and previous.reason == reason
            and previous.source_version == expected_version
        ):
            return previous
        raise ValidationError(
            "Kertas kerja sudah ditarik; riwayat penarikan tidak dapat ditimpa."
        )
    if (
        not isinstance(expected_version, str)
        or expected_version != obligation.updated_at.isoformat()
    ):
        raise ValidationError(
            "Kertas kerja berubah sejak halaman dibuka. Muat ulang dan periksa kembali sebelum menariknya."
        )
    blocker = withdrawal_blocker(obligation)
    if blocker:
        raise ValidationError(blocker)
    snapshot = json_value(
        {
            **subject_snapshot(obligation),
            "status": obligation.status,
            "updated_at": obligation.updated_at,
            "note": obligation.note,
            "prepared_by_id": obligation.prepared_by_id,
        }
    )
    row = TaxObligationWithdrawal(
        organization=organization,
        obligation=obligation,
        actor=actor,
        reason=reason,
        previous_status=obligation.status,
        source_version=expected_version,
        snapshot=snapshot,
        obligation_digest=digest(snapshot),
    )
    obligation.status = "withdrawn"
    obligation._withdrawal_transition = True
    try:
        _save(obligation)
    finally:
        obligation.__dict__.pop("_withdrawal_transition", None)
    _save(row)
    record(
        organization,
        actor,
        "tax.obligation.withdrawn",
        row,
        {
            "obligation_id": obligation.pk,
            "reason": reason,
            "source_version": expected_version,
            "workflow_version": row.workflow_version,
            "obligation_digest": row.obligation_digest,
        },
    )
    return row


def bill_decision(
    *,
    rule_code,
    base,
    period,
    reason,
    object_code="",
    recipient_identity=None,
    tax_point_date=None,
    tax_point_basis="",
    object_decision=None,
    withholder_name="",
    withholder_reference="",
    retained_legal_sources=None,
    require_current=True,
    **unused,
):
    decision = {
        "rule_code": rule_code,
        "base": str(number(base)),
        "period": period,
        "reason": reason,
        "object_code": object_code,
    }
    if rule_code in ("pph23_service_2", "pph23_other_15"):
        if recipient_identity is None or type(tax_point_date) is not date:
            raise ValidationError(
                "Keputusan PPh 23 tagihan memerlukan identitas penerima dan tanggal terutang yang tepat."
            )
        from .recipient_identity import identity_snapshot, pph23_context

        context = pph23_context(
            organization=recipient_identity.organization,
            period=period,
            rule_code=rule_code,
            base=base,
            direction="payable",
            recipient_identity=recipient_identity,
            recipient_party=recipient_identity.recipient_party,
            tax_point_date=tax_point_date,
            tax_point_basis=tax_point_basis,
            object_decision=object_decision,
            withholder_name=withholder_name,
            withholder_reference=withholder_reference,
            retained_legal_sources=retained_legal_sources,
            require_current=require_current,
        )
        decision.update(
            {
                "recipient_identity_id": recipient_identity.pk,
                "identity_fingerprint": digest(identity_snapshot(recipient_identity)),
                "tax_point_date": tax_point_date,
                "tax_point_basis": tax_point_basis,
                "object_decision": object_decision,
                "withholder_name": withholder_name,
                "withholder_reference": withholder_reference,
                "identity_context": context["identity_snapshot"],
                "legal_sources": context["legal_sources"],
            }
        )
    return json_value(decision)


@transaction.atomic
def review_bill_tax(
    *,
    organization,
    bill,
    actor,
    rule_code,
    base,
    period,
    reason,
    external_review,
    object_code="",
    recipient_identity=None,
    tax_point_date=None,
    tax_point_basis="",
    object_decision=None,
    withholder_name="",
    withholder_reference="",
    **unused,
):
    from finance.models import Bill

    from .services import parse_period

    _lock(organization, actor, approve=True)
    bill = _scope(Bill, bill, organization, lock=True)
    period = parse_period(period) if isinstance(period, str) else period
    validate_monthly_period(period)
    if bill.status != "approved" or BillTaxDecision.objects.filter(bill=bill).exists():
        raise ValidationError(
            "Sahkan tagihan terlebih dahulu. Keputusan pajak yang sudah tercatat tidak ditimpa."
        )
    if (
        rule_code not in ("no_withholding", "pph23_service_2", "pph23_other_15")
        or not isinstance(period, date)
        or period.day != 1
    ):
        raise ValidationError("Klasifikasi tagihan atau masa belum didukung.")
    context_kwargs = {
        "recipient_identity": recipient_identity,
        "tax_point_date": tax_point_date,
        "tax_point_basis": tax_point_basis,
        "object_decision": object_decision,
        "withholder_name": withholder_name,
        "withholder_reference": withholder_reference,
    }
    decision = bill_decision(
        rule_code=rule_code,
        base=base,
        period=period,
        reason=reason,
        object_code=object_code,
        **context_kwargs,
    )
    review = _assert_review(organization, bill, decision, external_review)
    if rule_code != "no_withholding":
        from .recipient_identity import _role, pph23_context

        _role(organization, actor, approve=True)
        context = pph23_context(
            organization=organization,
            period=period,
            rule_code=rule_code,
            base=base,
            direction="payable",
            source_bill=bill,
            **context_kwargs,
        )
        calc = context["calculation"]
    else:
        calc = calculate_tax(rule_code, base)
    if (
        calc["base"] > bill.amount
        or calc["amount"] >= bill.amount
        or not reason.strip()
    ):
        raise ValidationError(
            "Dasar dan potongan harus sesuai bruto tagihan serta kesimpulan pemeriksa."
        )
    obligation = None
    if calc["amount"]:
        obligation = prepare_obligation(
            organization=organization,
            actor=actor,
            period=period,
            rule_code=rule_code,
            base=base,
            direction="payable",
            source_reference=bill.number,
            rule_reference=reason,
            object_code=object_code,
            source_evidence=review.evidence,
            source_bill=bill,
            note="Keputusan laporan profesional atas tagihan; pembayaran neto dan setoran pajak dicatat terpisah.",
            **context_kwargs,
        )
        obligation.status, obligation.external_review = "approved", review
        obligation.reviewed_by, obligation.reviewed_at = actor, timezone.now()
        _save(obligation)
    row = _save(
        BillTaxDecision(
            organization=organization,
            bill=bill,
            external_review=review,
            obligation=obligation,
            rule_code=rule_code,
            base=calc["base"],
            rate=calc["rate"],
            amount=calc["amount"],
            period=period,
            reason=reason,
            actor=actor,
        )
    )
    bill.tax_status, bill.tax_amount = "reviewed", calc["amount"]
    bill._tax_service_transition = True
    bill.save(update_fields=["tax_status", "tax_amount"])
    record(
        organization,
        actor,
        "tax.bill.reviewed",
        row,
        {
            "bill_id": bill.pk,
            "external_review": review.pk,
            "obligation_id": obligation.pk if obligation else None,
        },
    )
    return row


@transaction.atomic
def verify_obligation_evidence(
    *,
    organization,
    actor,
    obligation,
    kind,
    evidence,
    reference,
    taxpayer_reference,
    period,
    amount,
    note,
    matches_confirmed=False,
    recipient_reference="",
    recipient_name="",
    withholder_reference="",
    withholder_name="",
    object_code="",
    base=None,
    tax_point_date=None,
    certificate_status="normal",
):
    _lock(organization, actor, approve=True)
    obligation = _scope(TaxObligation, obligation, organization, lock=True)
    if obligation.source_package_id:
        from .monthly_source_workflow import _owner, assert_package_for_obligation

        _owner(organization, actor)
        assert_package_for_obligation(organization, obligation, lock_sources=True)
    evidence = _scope(TaxEvidence, evidence, organization)
    profile = TaxProfile.objects.filter(organization=organization).first()
    from .services import parse_period

    period = parse_period(period) if isinstance(period, str) else period
    if (
        matches_confirmed is not True
        or obligation.status != "approved"
        or kind not in ("payment", "filing", "credit")
    ):
        raise ValidationError(
            "Pilih jenis bukti dan konfirmasi pencocokan dokumen untuk kertas kerja disetujui."
        )
    certificate = {}
    if obligation.tax_type == "pph23":
        from .recipient_identity import (
            _evidence,
            _role,
            assert_pph23_obligation,
            pph23_certificate_snapshot,
        )

        _role(organization, actor, approve=True)
        assert_pph23_obligation(obligation)
        _evidence(evidence)
        own_key = "recipient" if obligation.direction == "receivable" else "withholder"
        if taxpayer_reference != obligation.identity_snapshot[own_key]["reference"]:
            raise ValidationError(
                "Identitas wajib pajak harus cocok dengan identitas perusahaan yang terikat pada masa ini, bukan profil terbaru."
            )
        if kind == "credit":
            certificate = pph23_certificate_snapshot(
                obligation,
                evidence=evidence,
                reference=reference.strip(),
                period=period,
                amount=amount,
                recipient_reference=recipient_reference,
                recipient_name=recipient_name,
                withholder_reference=withholder_reference,
                withholder_name=withholder_name,
                object_code=object_code,
                base=base,
                tax_point_date=tax_point_date,
                certificate_status=certificate_status,
            )
    elif (
        not profile
        or not profile.external_review_id
        or not profile.taxpayer_reference
        or taxpayer_reference.strip() != profile.taxpayer_reference
    ):
        raise ValidationError(
            "Identitas wajib pajak harus cocok dengan profil yang ditinjau."
        )
    if (
        period != obligation.period
        or number(amount) != obligation.amount
        or len(reference.strip()) < 5
        or not note.strip()
    ):
        raise ValidationError(
            "Masa, nominal, referensi resmi, dan catatan pencocokan wajib sesuai kewajiban."
        )
    if (kind == "credit") != (obligation.direction == "receivable"):
        raise ValidationError(
            "Bukti potong pelanggan dicatat sebagai kredit; bukan pembayaran OSEE."
        )
    if kind == "filing" and obligation.reporting_method == "validated_self_payment":
        raise ValidationError(
            "Untuk cabang final setor sendiri ini, verifikasi pembayaran resmi memenuhi pelaporan masa sesuai PMK 81 Pasal 171(4)."
        )
    if kind == "payment" and (
        obligation.source_bill_id or obligation.direction == "own_tax"
    ):
        # Finance remittance allocations prove the recognized liability was settled;
        # the uploaded official document is a distinct evidence check.
        from finance.models import OwnTaxPayment, TaxRemittance

        payments = (
            OwnTaxPayment.objects.filter(
                organization=organization, recognition__obligation=obligation
            )
            if obligation.direction == "own_tax"
            else TaxRemittance.objects.filter(
                organization=organization, obligation=obligation
            )
        )
        paid = payments.aggregate(total=Sum("amount"))["total"] or Decimal(0)
        if paid != obligation.amount:
            raise ValidationError(
                "Cocokkan setoran bank terhadap seluruh kewajiban terlebih dahulu; bukti resmi diverifikasi sesudahnya."
            )
    row = _save(
        TaxVerification(
            organization=organization,
            obligation=obligation,
            kind=kind,
            evidence=evidence,
            reference=reference.strip(),
            taxpayer_reference=taxpayer_reference.strip(),
            period=period,
            amount=number(amount),
            obligation_digest=digest(subject_snapshot(obligation)),
            verifier=actor,
            note=note,
            certificate_snapshot=certificate,
        )
    )
    record(
        organization,
        actor,
        "tax.evidence.verified",
        row,
        {"kind": kind, "obligation_id": obligation.pk, "sha256": evidence.sha256},
    )
    return row


def annual_credit_inventory(organization, fiscal_start, fiscal_end, *, validate=False):
    """Identity-bound credit facts for new annual actions, no provider calls."""
    from .recipient_identity import assert_pph23_credit_verification

    rows = []
    for proof in (
        TaxVerification.objects.filter(
            organization=organization,
            kind="credit",
            period__gte=fiscal_start,
            period__lte=fiscal_end,
        )
        .exclude(obligation__status="withdrawn")
        .select_related(
            "evidence",
            "obligation__source_evidence",
            "obligation__external_review__evidence",
            "obligation__recipient_identity__evidence",
            "obligation__recipient_identity__approval__external_review__evidence",
        )
        .order_by("pk")
    ):
        obligation = proof.obligation
        if validate:
            if (
                obligation.tax_type != "pph23"
                or obligation.direction != "receivable"
                or obligation.status != "approved"
            ):
                raise ValidationError(
                    "Kredit tahunan harus berasal dari PPh 23 pelanggan yang disetujui dan dicocokkan identitasnya."
                )
            assert_pph23_credit_verification(proof, obligation)
        evidence_rows = [("certificate", proof.evidence)]
        if obligation.source_evidence_id:
            evidence_rows.append(("source", obligation.source_evidence))
        if obligation.external_review_id:
            evidence_rows.append(("object_review", obligation.external_review.evidence))
        if obligation.recipient_identity_id:
            evidence_rows.append(("recipient", obligation.recipient_identity.evidence))
            identity_approval = getattr(obligation.recipient_identity, "approval", None)
            if identity_approval:
                evidence_rows.append(
                    ("identity_review", identity_approval.external_review.evidence)
                )
        rows.append(
            {
                "verification_id": proof.pk,
                "amount": str(proof.amount),
                "obligation_id": obligation.pk,
                "obligation_digest": digest(subject_snapshot(obligation)),
                "certificate_snapshot": proof.certificate_snapshot,
                "evidence": [
                    {
                        "kind": kind,
                        "id": evidence.pk,
                        "recorded_hash": evidence.sha256,
                        "actual_hash": hashlib.sha256(
                            bytes(evidence.content)
                        ).hexdigest(),
                    }
                    for kind, evidence in evidence_rows
                ],
            }
        )
    return json_value(rows)


def annual_tax_snapshot(
    organization, fiscal_start, fiscal_end, *, include_identity=False
):
    """Capture immutable decisions and evidence affecting a fiscal package."""
    obligations = TaxObligation.objects.filter(
        organization=organization, period__gte=fiscal_start, period__lte=fiscal_end
    ).exclude(status="withdrawn")
    verifications = TaxVerification.objects.filter(
        organization=organization, period__gte=fiscal_start, period__lte=fiscal_end
    ).exclude(obligation__status="withdrawn")
    obligation_rows = list(
        obligations.order_by("pk").values(
            "pk",
            "status",
            "amount",
            "base",
            "rate",
            "rule_code",
            "external_review_id",
            "source_invoice_id",
        )
    )
    for row in obligation_rows:
        if row["source_invoice_id"] is None:
            row.pop("source_invoice_id")
    result = {
        "obligations": obligation_rows,
        "verifications": list(
            verifications.order_by("pk").values(
                "pk", "kind", "amount", "obligation_id", "evidence_id"
            )
        ),
        "profile_decision_ids": list(
            TaxProfileDecision.objects.filter(
                organization=organization, effective_from__lte=fiscal_end
            )
            .order_by("pk")
            .values_list("pk", flat=True)
        ),
    }
    if include_identity:
        result.update(
            {
                "credit_identity_version": "annual-pph23-identity-v1",
                "credit_identity_inventory": annual_credit_inventory(
                    organization, fiscal_start, fiscal_end
                ),
            }
        )
    from .supplier_advance_dependencies import supplier_advance_report_dependency

    dependency, _, _ = supplier_advance_report_dependency(
        organization=organization, cutoff=fiscal_end
    )
    if dependency["inventory"]:
        result["supplier_advance_reviews"] = dependency
    from .opening_balance_review_dependencies import (
        opening_balance_financial_dependency,
        opening_balance_report_dependency,
    )

    opening_dependency, _, _ = opening_balance_report_dependency(
        organization=organization, cutoff=fiscal_end
    )
    if opening_dependency["inventory"]:
        result["opening_cash_reviews"] = opening_dependency
    principal_dependency = opening_balance_financial_dependency(
        organization=organization, cutoff=fiscal_end
    )
    if principal_dependency["inventory"] or principal_dependency["blockers"]:
        result["opening_principal_sources"] = principal_dependency
    from .opening_refund_review_dependencies import opening_refund_report_dependency

    refund_dependency, _, _ = opening_refund_report_dependency(
        organization=organization, cutoff=fiscal_end
    )
    if refund_dependency["inventory"]:
        result["opening_refund_reviews"] = refund_dependency
    return json_value(result)


def _assert_annual_tax_snapshot(organization, workpaper, context):
    annual_credit_inventory(
        organization, context["fiscal_start"], context["fiscal_end"], validate=True
    )
    saved = workpaper.inputs.get("tax_snapshot", {})
    if saved != annual_tax_snapshot(
        organization,
        context["fiscal_start"],
        context["fiscal_end"],
        include_identity="credit_identity_version" in saved,
    ):
        raise ValidationError(
            "Bukti pajak atau keputusan profil berubah sejak persiapan; buat kertas kerja baru dan tinjau kembali."
        )


def annual_snapshot_status(organization, workpaper):
    """Read-only freshness of inputs, not certification of filing readiness."""
    workpaper = _scope(AnnualTaxWorkpaper, workpaper, organization)
    from .services import annual_readiness

    context = annual_readiness(organization, workpaper.year)
    saved = workpaper.inputs.get("tax_snapshot", {})
    tax_current = saved == annual_tax_snapshot(
        organization,
        context["fiscal_start"],
        context["fiscal_end"],
        include_identity="credit_identity_version" in saved,
    )
    try:
        annual_credit_inventory(
            organization, context["fiscal_start"], context["fiscal_end"], validate=True
        )
    except ValidationError:
        tax_current = False  # Old package remains readable, never silently validated.
    book_current = (
        json_value({key: context.get(key) for key in workpaper.book_snapshot})
        == workpaper.book_snapshot
    )
    supported = (
        type(workpaper.inputs.get("return_version", 0)) is int
        and workpaper.inputs.get("return_version", 0) == 0
    )
    return {
        "snapshot_current": tax_current and book_current,
        "tax_snapshot_current": tax_current,
        "book_snapshot_current": book_current,
        "preparation_supported": supported,
    }


def _normal_annual_return_only(return_version):
    # An amended balance needs prior-return payments/refunds and a replacement
    # snapshot. The normal-return calculator does not model that history.
    if type(return_version) is not int or return_version != 0:
        raise ValidationError(
            "Pembetulan SPT belum dapat dihitung di aplikasi. Riwayat pembayaran, pengembalian, dan SPT sebelumnya perlu direkonsiliasi oleh pemeriksa pajak. Paket lama tetap disimpan sebagai riwayat."
        )


@transaction.atomic
def prepare_annual_workpaper(
    *,
    organization,
    actor,
    year,
    positive_adjustments,
    negative_adjustments,
    loss_compensation,
    turnover,
    regime_code,
    instalments,
    support_evidence,
    reconciliation_note,
    return_version=0,
):
    _lock(organization, actor)
    _normal_annual_return_only(return_version)
    if (
        type(year) is not int
        or year not in ANNUAL_WORKPAPER_YEARS
        or year > timezone.localdate().year
    ):
        supported = (
            f"{min(ANNUAL_WORKPAPER_YEARS)} sampai {max(ANNUAL_WORKPAPER_YEARS)}"
        )
        raise ValidationError(
            f"Kalkulator tahunan ini mendukung tahun {supported}; tahun lain memerlukan versi aturan tersendiri."
        )
    from .services import annual_readiness

    context = annual_readiness(organization, year)
    evidence = _scope(TaxEvidence, support_evidence, organization)
    if not reconciliation_note.strip():
        raise ValidationError(
            "Jelaskan rekonsiliasi fiskal, termasuk pendapatan final/bukan objek, biaya, rugi, dan angsuran sesuai lampiran."
        )
    credit_inventory = annual_credit_inventory(
        organization, context["fiscal_start"], context["fiscal_end"], validate=True
    )
    credits = sum((Decimal(row["amount"]) for row in credit_inventory), Decimal(0))
    result = calculate_annual(
        book_profit=context["book_profit"],
        positive_adjustments=positive_adjustments,
        negative_adjustments=negative_adjustments,
        loss_compensation=loss_compensation,
        turnover=turnover,
        regime_code=regime_code,
        credits=credits,
        instalments=instalments,
    )
    final_rows = TaxObligation.objects.filter(
        organization=organization,
        status="approved",
        tax_type="final_turnover",
        direction="own_tax",
        period__gte=context["fiscal_start"],
        period__lte=context["fiscal_end"],
    )
    result["final_tax_assessed"] = str(
        final_rows.aggregate(total=Sum("amount"))["total"] or Decimal(0)
    )
    result["final_tax_verified_paid"] = str(
        TaxVerification.objects.filter(
            organization=organization, obligation__in=final_rows, kind="payment"
        ).aggregate(total=Sum("amount"))["total"]
        or Decimal(0)
    )
    snapshot = json_value(
        {
            name: context[name]
            for name in (
                "fiscal_start",
                "fiscal_end",
                "book_revenue",
                "book_expenses",
                "book_profit",
                "ledger_months",
                "closed_month_count",
            )
        }
    )
    inputs = json_value(
        {
            "positive_adjustments": number(positive_adjustments),
            "negative_adjustments": number(negative_adjustments),
            "loss_compensation": number(loss_compensation),
            "turnover": number(turnover),
            "instalments": number(instalments),
            "regime_code": regime_code,
            "support_evidence_id": evidence.pk,
            "support_evidence_sha256": evidence.sha256,
            "reconciliation_note": reconciliation_note,
            "verified_credit_total": credits,
            "return_version": return_version,
            "credit_verification_ids": [
                row["verification_id"] for row in credit_inventory
            ],
            "final_obligation_ids": list(final_rows.values_list("pk", flat=True)),
            "tax_snapshot": annual_tax_snapshot(
                organization,
                context["fiscal_start"],
                context["fiscal_end"],
                include_identity=True,
            ),
        }
    )
    row = _save(
        AnnualTaxWorkpaper(
            organization=organization,
            year=year,
            book_snapshot=snapshot,
            inputs=inputs,
            result=result,
            calculation_version=CALCULATION_VERSION,
            prepared_by=actor,
        )
    )
    record(
        organization,
        actor,
        "tax.annual.prepared",
        row,
        {"year": year, "calculation_version": row.calculation_version},
    )
    return row


@transaction.atomic
def approve_annual_workpaper(*, organization, actor, workpaper, external_review):
    _lock(organization, actor, approve=True)
    workpaper = _scope(AnnualTaxWorkpaper, workpaper, organization)
    _normal_annual_return_only(workpaper.inputs.get("return_version", 0))
    from .services import annual_readiness

    context = annual_readiness(organization, workpaper.year)
    _assert_annual_tax_snapshot(organization, workpaper, context)
    if (
        context["gates"]
        or context["closed_month_count"] != 12
        or context["fiscal_end"] >= timezone.localdate()
    ):
        raise ValidationError(
            "Seluruh tahun harus tercakup profil, selesai, dan 12 bulan ditutup sebelum persetujuan tahunan."
        )
    if (
        TaxObligation.objects.filter(
            organization=organization,
            period__gte=context["fiscal_start"],
            period__lte=context["fiscal_end"],
        )
        .exclude(status__in=["approved", "withdrawn"])
        .exists()
    ):
        raise ValidationError(
            "Selesaikan pemeriksaan seluruh kertas kerja pajak dalam tahun ini sebelum persetujuan tahunan."
        )
    current = json_value({name: context[name] for name in workpaper.book_snapshot})
    if current != workpaper.book_snapshot:
        raise ValidationError(
            "Buku berubah sejak kertas kerja disiapkan; buat versi persiapan baru."
        )
    review = _assert_review(
        organization, workpaper, {"action": "approve_annual"}, external_review
    )
    row = _save(
        AnnualTaxApproval(
            organization=organization,
            workpaper=workpaper,
            external_review=review,
            actor=actor,
        )
    )
    record(
        organization,
        actor,
        "tax.annual.approved",
        row,
        {"year": workpaper.year, "official_schema_validated": False},
    )
    return row


@transaction.atomic
def verify_annual_filing(
    *,
    organization,
    actor,
    workpaper,
    evidence,
    reference,
    taxpayer_reference,
    year,
    return_version,
    reported_tax,
    reported_balance_due,
    matches_confirmed=False,
):
    _lock(organization, actor, approve=True)
    workpaper = _scope(AnnualTaxWorkpaper, workpaper, organization)
    _normal_annual_return_only(workpaper.inputs.get("return_version", 0))
    _normal_annual_return_only(return_version)
    evidence = _scope(TaxEvidence, evidence, organization)
    profile = TaxProfile.objects.filter(organization=organization).first()
    from .services import annual_readiness

    context = annual_readiness(organization, workpaper.year)
    _assert_annual_tax_snapshot(organization, workpaper, context)
    if (
        json_value({name: context[name] for name in workpaper.book_snapshot})
        != workpaper.book_snapshot
    ):
        raise ValidationError(
            "Buku berubah sejak kertas kerja disetujui; catat versi dan pemeriksaan baru sebelum mencocokkan SPT."
        )
    if (
        matches_confirmed is not True
        or not AnnualTaxApproval.objects.filter(workpaper=workpaper).exists()
        or not profile
        or not profile.taxpayer_reference
        or taxpayer_reference.strip() != profile.taxpayer_reference
        or len(reference.strip()) < 5
    ):
        raise ValidationError(
            "Cocokkan BPE resmi, wajib pajak, tahun dan versi SPT yang telah disetujui."
        )
    if (
        year != workpaper.year
        or return_version != workpaper.inputs.get("return_version", 0)
        or number(reported_tax) != Decimal(workpaper.result["income_tax"])
        or number(reported_balance_due, signed=True)
        != Decimal(workpaper.result["balance_due"])
    ):
        raise ValidationError(
            "Tahun, versi pembetulan, PPh dan kurang/lebih bayar SPT harus cocok dengan paket disetujui."
        )
    row = _save(
        AnnualTaxFiling(
            organization=organization,
            workpaper=workpaper,
            evidence=evidence,
            reference=reference.strip(),
            taxpayer_reference=taxpayer_reference.strip(),
            actor=actor,
            year=year,
            return_version=return_version,
            reported_tax=number(reported_tax),
            reported_balance_due=number(reported_balance_due, signed=True),
        )
    )
    record(
        organization,
        actor,
        "tax.annual.filing_verified",
        row,
        {"workpaper": workpaper.pk, "year": workpaper.year, "sha256": evidence.sha256},
    )
    return row
