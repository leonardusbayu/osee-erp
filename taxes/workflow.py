"""Authorized document-led preparation, external review and evidence matching.

Owner authorization records a professional's documented decision; it does not
certify that professional's credentials or make the owner a tax specialist.
"""
import hashlib
import io
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
from pypdf import PdfReader

from core.audit import record
from core.models import Membership, Organization
from .models import (AnnualTaxApproval, AnnualTaxFiling, AnnualTaxWorkpaper, BillTaxDecision,
    ExternalTaxReview, TaxEvidence, TaxObligation, TaxProfile, TaxProfileDecision, TaxVerification)
from .rules import CALCULATION_VERSION, calculate_annual, calculate_tax, number, validate_monthly_period


def authorize(organization, actor, *, approve=False):
    roles = ("owner", "reviewer") if approve else ("owner", "finance", "reviewer")
    if not getattr(actor, "is_authenticated", False) or not Membership.objects.filter(
        organization=organization, user=actor, user__is_active=True, role__in=roles).exists():
        raise PermissionDenied("Lakukan dengan akun aktif dan kewenangan yang sesuai. Persetujuan pemilik mencatat laporan profesional, bukan menggantikan pemeriksaannya.")


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
    return json.loads(json.dumps(value, default=lambda item: item.isoformat() if isinstance(item, date) else str(item), sort_keys=True))


def digest(value):
    return hashlib.sha256(json.dumps(json_value(value), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def subject_snapshot(subject):
    names = {
        "taxprofile": ("organization_id", "registration_date", "registration_evidence", "taxpayer_reference", "vat_status_effective_from", "vat_status_evidence"),
        "bill": ("organization_id", "number", "supplier_id", "amount", "supplier_vat", "date", "service_date", "category", "status"),
        "taxobligation": ("organization_id", "period", "tax_type", "direction", "base", "rate", "amount", "source_bill_id", "rule_code", "calculation_version", "object_code", "source_evidence_id", "source_reference", "rule_reference", "reporting_method"),
        "annualtaxworkpaper": ("organization_id", "year", "book_snapshot", "inputs", "result", "calculation_version"),
    }
    kind = subject._meta.model_name
    if kind not in names:
        raise ValidationError("Jenis pemeriksaan tidak didukung.")
    return {"kind": kind, "id": subject.pk, **{name: getattr(subject, name) for name in names[kind]}}


@transaction.atomic
def upload_evidence(*, organization, actor, file):
    _lock(organization, actor)
    content = file.read(2 * 1024 * 1024 + 1)
    if len(content) > 2 * 1024 * 1024 or not content.startswith(b"%PDF-"):
        raise ValidationError("Unggah PDF hingga 2 MB.")
    try:
        reader = PdfReader(io.BytesIO(content), strict=True)
        if reader.is_encrypted or not 1 <= len(reader.pages) <= 200:
            raise ValueError
    except Exception:
        raise ValidationError("PDF harus dapat dibaca, tidak terenkripsi, dan maksimal 200 halaman.") from None
    filename = PurePath(str(getattr(file, "name", "bukti.pdf")).replace("\\", "/")).name[:200]
    row = _save(TaxEvidence(organization=organization, uploaded_by=actor,
        original_filename=filename, content=content, sha256=hashlib.sha256(content).hexdigest()))
    record(organization, actor, "tax.evidence.uploaded", row, {"sha256": row.sha256})
    return row


@transaction.atomic
def record_external_review(*, organization, actor, subject, decision, evidence,
                           professional_name, qualification_reference, reviewed_on, statement,
                           report_confirmed=False):
    _lock(organization, actor, approve=True)
    subject = _scope(type(subject), subject, organization, lock=True)
    evidence = _scope(TaxEvidence, evidence, organization)
    if report_confirmed is not True or not isinstance(reviewed_on, date) or reviewed_on > timezone.localdate():
        raise ValidationError("Konfirmasi laporan profesional yang benar-benar diterima, dengan tanggal pemeriksaan yang sudah terjadi.")
    if any(not isinstance(item, str) or len(item.strip()) < 3 for item in (professional_name, qualification_reference, statement)):
        raise ValidationError("Nama profesional, referensi kompetensi/penugasan, dan kesimpulan laporan wajib diisi.")
    if not isinstance(decision, dict) or len(json.dumps(json_value(decision))) > 20000:
        raise ValidationError("Keputusan pemeriksaan tidak sah.")
    row = _save(ExternalTaxReview(organization=organization, evidence=evidence,
        professional_name=professional_name.strip(), qualification_reference=qualification_reference.strip(),
        reviewed_on=reviewed_on, statement=statement.strip(), subject_type=subject._meta.model_name,
        subject_id=subject.pk, subject_digest=digest({"subject": subject_snapshot(subject), "decision": decision}),
        decision=json_value(decision), recorded_by=actor))
    record(organization, actor, "tax.external_review.recorded", row, {"subject_type": row.subject_type, "subject_id": row.subject_id, "evidence_sha256": evidence.sha256, "credentials_independently_verified": False})
    return row


def _assert_review(organization, subject, decision, review):
    review = _scope(ExternalTaxReview, review, organization)
    if review.subject_type != subject._meta.model_name or review.subject_id != subject.pk or review.subject_digest != digest({"subject": subject_snapshot(subject), "decision": decision}):
        raise ValidationError("Laporan pemeriksaan tidak cocok dengan catatan dan perhitungan ini; buat pemeriksaan baru setelah perubahan.")
    return review


def profile_decision(*, regime, effective_from, final_regime_start_date=None, final_regime_end_date=None, transition_evidence="",
                     qualifying_turnover=None, legacy_eligibility_confirmed=False, no_normal_election_confirmed=False, **unused):
    return json_value({"regime": regime, "effective_from": effective_from,
        "final_regime_start_date": final_regime_start_date, "final_regime_end_date": final_regime_end_date,
        "transition_evidence": transition_evidence, "qualifying_turnover": str(number(qualifying_turnover)) if qualifying_turnover is not None else None,
        "legacy_eligibility_confirmed": legacy_eligibility_confirmed, "no_normal_election_confirmed": no_normal_election_confirmed})


@transaction.atomic
def approve_tax_profile(profile, actor, *, regime, effective_from, review_note,
                        final_regime_start_date=None, final_regime_end_date=None,
                        transition_evidence="", external_review=None, qualifying_turnover=None,
                        legacy_eligibility_confirmed=False, no_normal_election_confirmed=False):
    organization = profile.organization
    _lock(organization, actor, approve=True)
    profile = _scope(TaxProfile, profile, organization, lock=True)
    if external_review is None:
        if Membership.objects.filter(organization=organization, user=actor, role="owner").exists():
            raise PermissionDenied("Lejitimasikan keputusan dengan laporan pemeriksa eksternal terlebih dahulu; pemilik tidak menetapkan pajak sendiri.")
        raise ValidationError("Laporan pemeriksaan profesional wajib dilampirkan, termasuk untuk akun peninjau lama.")
    if regime not in ("final", "normal") or not isinstance(effective_from, date) or effective_from.day != 1:
        raise ValidationError("Pilih aturan dan tanggal pertama masa berlaku.")
    decision = profile_decision(regime=regime, effective_from=effective_from, final_regime_start_date=final_regime_start_date,
        final_regime_end_date=final_regime_end_date, transition_evidence=transition_evidence,
        qualifying_turnover=qualifying_turnover, legacy_eligibility_confirmed=legacy_eligibility_confirmed, no_normal_election_confirmed=no_normal_election_confirmed)
    review = _assert_review(organization, profile, decision, external_review)
    if not profile.registration_date or profile.registration_date > timezone.localdate() or effective_from < profile.registration_date.replace(day=1) or effective_from > timezone.localdate().replace(day=1):
        raise ValidationError("Masa aktivasi harus sejak bulan terdaftar sampai masa berjalan; tanggal dokumen tidak boleh di masa depan.")
    if not profile.taxpayer_reference.strip():
        raise ValidationError("Identitas pajak perusahaan yang dicocokkan dengan dokumen wajib tersedia.")
    for name, value in decision.items():
        setattr(profile, name, date.fromisoformat(value) if value and (name.endswith("date") or name == "effective_from") else value)
    profile.external_review, profile.review_note = review, review_note
    profile.reviewed_by, profile.reviewed_at = actor, timezone.now()
    _save(profile)
    snapshot = json_value({**subject_snapshot(profile), **decision, "reviewed_at": profile.reviewed_at, "external_review_id": review.pk})
    _save(TaxProfileDecision(organization=organization, profile=profile, external_review=review, effective_from=effective_from, snapshot=snapshot))
    record(organization, actor, "tax.profile.approved", profile, {"regime": regime, "external_review": review.pk, "effective_from": effective_from.isoformat()})
    return profile


def profile_for_period(organization, period):
    profile = TaxProfile.objects.filter(organization=organization).first()
    row = TaxProfileDecision.objects.filter(organization=organization, effective_from__lte=period).order_by("-effective_from", "-pk").first()
    if not row:
        return profile
    data = dict(row.snapshot)
    for name in ("registration_date", "effective_from", "final_regime_start_date", "final_regime_end_date", "vat_status_effective_from"):
        data[name] = date.fromisoformat(data[name]) if data.get(name) else None
    return SimpleNamespace(**data)


@transaction.atomic
def prepare_obligation(*, organization, actor, period, rule_code, base, direction,
                       source_reference, rule_reference, object_code, source_evidence, note="", source_bill=None):
    _lock(organization, actor)
    from .services import parse_period, profile_gates
    period = parse_period(period) if isinstance(period, str) else period
    validate_monthly_period(period)
    if not str(source_reference).strip() or not str(rule_reference).strip() or not re.fullmatch(r"[A-Za-z0-9.-]{3,40}", object_code):
        raise ValidationError("Referensi sumber, dasar hukum, dan kode objek yang diperiksa wajib diisi.")
    source_evidence = _scope(TaxEvidence, source_evidence, organization)
    calc = calculate_tax(rule_code, base)
    if rule_code == "no_withholding":
        raise ValidationError("Keputusan tidak dipotong dicatat pada tinjauan tagihan, bukan kewajiban pajak nihil.")
    tax_type = "final_turnover" if rule_code == "final_turnover_005" else "pph23"
    if (tax_type == "final_turnover" and direction != "own_tax") or (tax_type == "pph23" and direction not in ("payable", "receivable")):
        raise ValidationError("Arah kewajiban tidak sesuai jenis pajak.")
    if tax_type == "final_turnover":
        profile, gates = profile_gates(organization, period)
        if gates or profile.regime != "final":
            raise ValidationError("Profil masa ini belum memvalidasi hak PPh final.")
    if source_bill is not None:
        from finance.models import Bill
        source_bill = _scope(Bill, source_bill, organization)
    row = TaxObligation(organization=organization, period=period, tax_type=tax_type, direction=direction,
        base=calc["base"], rate=calc["rate"], amount=calc["amount"], source_reference=source_reference,
        rule_reference=rule_reference, object_code=object_code, source_evidence=source_evidence,
        rule_code=rule_code, calculation_version=calc["version"], note=note, source_bill=source_bill,
        prepared_by=actor, status="review", reporting_method="validated_self_payment" if tax_type == "final_turnover" else "return_receipt")
    _save(row)
    record(organization, actor, "tax.obligation.prepared", row, {"calculation_version": calc["version"]})
    return row


@transaction.atomic
def approve_tax_obligation(obligation, actor, *, external_review=None):
    organization = obligation.organization
    _lock(organization, actor, approve=True)
    obligation = _scope(TaxObligation, obligation, organization, lock=True)
    if obligation.status == "approved":
        raise ValidationError("Kertas kerja sudah disetujui dan tidak diubah.")
    if external_review is None:
        raise ValidationError("Persetujuan memerlukan laporan profesional yang sesuai perhitungan.")
    review = _assert_review(organization, obligation, {"action": "approve_obligation"}, external_review)
    obligation.status, obligation.external_review = "approved", review
    obligation.reviewed_by, obligation.reviewed_at = actor, timezone.now()
    _save(obligation)
    record(organization, actor, "tax.obligation.approved", obligation, {"external_review": review.pk, "calculation_version": obligation.calculation_version})
    return obligation


def bill_decision(*, rule_code, base, period, reason, object_code="", **unused):
    return json_value({"rule_code": rule_code, "base": str(number(base)), "period": period, "reason": reason, "object_code": object_code})


@transaction.atomic
def review_bill_tax(*, organization, bill, actor, rule_code, base, period, reason, external_review, object_code=""):
    from finance.models import Bill
    from .services import parse_period
    _lock(organization, actor, approve=True)
    bill = _scope(Bill, bill, organization, lock=True)
    period = parse_period(period) if isinstance(period, str) else period
    validate_monthly_period(period)
    if bill.status != "approved" or BillTaxDecision.objects.filter(bill=bill).exists():
        raise ValidationError("Sahkan tagihan terlebih dahulu. Keputusan pajak yang sudah tercatat tidak ditimpa.")
    if rule_code not in ("no_withholding", "pph23_service_2", "pph23_other_15") or not isinstance(period, date) or period.day != 1:
        raise ValidationError("Klasifikasi tagihan atau masa belum didukung.")
    decision = bill_decision(rule_code=rule_code, base=base, period=period, reason=reason, object_code=object_code)
    review = _assert_review(organization, bill, decision, external_review)
    calc = calculate_tax(rule_code, base)
    if calc["base"] > bill.amount or calc["amount"] >= bill.amount or not reason.strip():
        raise ValidationError("Dasar dan potongan harus sesuai bruto tagihan serta kesimpulan pemeriksa.")
    obligation = None
    if calc["amount"]:
        obligation = prepare_obligation(organization=organization, actor=actor, period=period, rule_code=rule_code, base=base,
            direction="payable", source_reference=bill.number, rule_reference=reason, object_code=object_code,
            source_evidence=review.evidence, source_bill=bill, note="Keputusan laporan profesional atas tagihan; pembayaran neto dan setoran pajak dicatat terpisah.")
        obligation.status, obligation.external_review = "approved", review
        obligation.reviewed_by, obligation.reviewed_at = actor, timezone.now()
        _save(obligation)
    row = _save(BillTaxDecision(organization=organization, bill=bill, external_review=review, obligation=obligation,
        rule_code=rule_code, base=calc["base"], rate=calc["rate"], amount=calc["amount"], period=period, reason=reason, actor=actor))
    bill.tax_status, bill.tax_amount = "reviewed", calc["amount"]
    bill._tax_service_transition = True
    bill.save(update_fields=["tax_status", "tax_amount"])
    record(organization, actor, "tax.bill.reviewed", row, {"bill_id": bill.pk, "external_review": review.pk, "obligation_id": obligation.pk if obligation else None})
    return row


@transaction.atomic
def verify_obligation_evidence(*, organization, actor, obligation, kind, evidence, reference,
                               taxpayer_reference, period, amount, note, matches_confirmed=False):
    _lock(organization, actor, approve=True)
    obligation = _scope(TaxObligation, obligation, organization, lock=True)
    evidence = _scope(TaxEvidence, evidence, organization)
    profile = TaxProfile.objects.filter(organization=organization).first()
    from .services import parse_period
    period = parse_period(period) if isinstance(period, str) else period
    if matches_confirmed is not True or obligation.status != "approved" or kind not in ("payment", "filing", "credit"):
        raise ValidationError("Pilih jenis bukti dan konfirmasi pencocokan dokumen untuk kertas kerja disetujui.")
    if not profile or not profile.external_review_id or not profile.taxpayer_reference or taxpayer_reference.strip() != profile.taxpayer_reference:
        raise ValidationError("Identitas wajib pajak harus cocok dengan profil yang ditinjau.")
    if period != obligation.period or number(amount) != obligation.amount or len(reference.strip()) < 5 or not note.strip():
        raise ValidationError("Masa, nominal, referensi resmi, dan catatan pencocokan wajib sesuai kewajiban.")
    if (kind == "credit") != (obligation.direction == "receivable"):
        raise ValidationError("Bukti potong pelanggan dicatat sebagai kredit; bukan pembayaran OSEE.")
    if kind == "filing" and obligation.reporting_method == "validated_self_payment":
        raise ValidationError("Untuk cabang final setor sendiri ini, verifikasi pembayaran resmi memenuhi pelaporan masa sesuai PMK 81 Pasal 171(4).")
    if kind == "payment" and obligation.source_bill_id:
        # Finance remittance allocations prove the recognized liability was settled;
        # the uploaded official document is a distinct evidence check.
        from finance.models import TaxRemittance
        paid = TaxRemittance.objects.filter(organization=organization, obligation=obligation).aggregate(total=Sum("amount"))["total"] or Decimal("0")
        if paid != obligation.amount:
            raise ValidationError("Cocokkan setoran bank terhadap seluruh kewajiban terlebih dahulu; bukti resmi diverifikasi sesudahnya.")
    row = _save(TaxVerification(organization=organization, obligation=obligation, kind=kind, evidence=evidence,
        reference=reference.strip(), taxpayer_reference=taxpayer_reference.strip(), period=period, amount=number(amount),
        obligation_digest=digest(subject_snapshot(obligation)), verifier=actor, note=note))
    record(organization, actor, "tax.evidence.verified", row, {"kind": kind, "obligation_id": obligation.pk, "sha256": evidence.sha256})
    return row


def annual_tax_snapshot(organization, fiscal_start, fiscal_end):
    """Capture immutable decisions and evidence affecting a fiscal package."""
    obligations = TaxObligation.objects.filter(organization=organization, period__gte=fiscal_start, period__lte=fiscal_end)
    verifications = TaxVerification.objects.filter(organization=organization, period__gte=fiscal_start, period__lte=fiscal_end)
    return json_value({
        "obligations": list(obligations.order_by("pk").values("pk", "status", "amount", "base", "rate", "rule_code", "external_review_id")),
        "verifications": list(verifications.order_by("pk").values("pk", "kind", "amount", "obligation_id", "evidence_id")),
        "profile_decision_ids": list(TaxProfileDecision.objects.filter(organization=organization, effective_from__lte=fiscal_end).order_by("pk").values_list("pk", flat=True)),
    })


def _assert_annual_tax_snapshot(organization, workpaper, context):
    if workpaper.inputs.get("tax_snapshot") != annual_tax_snapshot(organization, context["fiscal_start"], context["fiscal_end"]):
        raise ValidationError("Bukti pajak atau keputusan profil berubah sejak persiapan; buat kertas kerja baru dan tinjau kembali.")


@transaction.atomic
def prepare_annual_workpaper(*, organization, actor, year, positive_adjustments, negative_adjustments,
                            loss_compensation, turnover, regime_code, instalments, support_evidence, reconciliation_note, return_version=0):
    _lock(organization, actor)
    from .services import annual_readiness
    context = annual_readiness(organization, year)
    if not isinstance(year, int) or isinstance(year, bool) or year not in (2025, 2026) or year > timezone.localdate().year:
        raise ValidationError("Kalkulator tahunan ini mendukung tahun 2025 sampai tahun berjalan; tahun lain memerlukan versi aturan tersendiri.")
    evidence = _scope(TaxEvidence, support_evidence, organization)
    if not reconciliation_note.strip():
        raise ValidationError("Jelaskan rekonsiliasi fiskal, termasuk pendapatan final/bukan objek, biaya, rugi, dan angsuran sesuai lampiran.")
    credits = TaxVerification.objects.filter(organization=organization, kind="credit", period__gte=context["fiscal_start"], period__lte=context["fiscal_end"]).aggregate(total=Sum("amount"))["total"] or Decimal("0")
    result = calculate_annual(book_profit=context["book_profit"], positive_adjustments=positive_adjustments,
        negative_adjustments=negative_adjustments, loss_compensation=loss_compensation, turnover=turnover,
        regime_code=regime_code, credits=credits, instalments=instalments)
    if not isinstance(return_version, int) or isinstance(return_version, bool) or not 0 <= return_version <= 999:
        raise ValidationError("Versi SPT harus 0 (normal) atau nomor pembetulan yang sah.")
    final_rows = TaxObligation.objects.filter(organization=organization, status="approved", tax_type="final_turnover", direction="own_tax", period__gte=context["fiscal_start"], period__lte=context["fiscal_end"])
    result["final_tax_assessed"] = str(final_rows.aggregate(total=Sum("amount"))["total"] or Decimal("0"))
    result["final_tax_verified_paid"] = str(TaxVerification.objects.filter(organization=organization, obligation__in=final_rows, kind="payment").aggregate(total=Sum("amount"))["total"] or Decimal("0"))
    snapshot = json_value({name: context[name] for name in ("fiscal_start", "fiscal_end", "book_revenue", "book_expenses", "book_profit", "ledger_months", "closed_month_count")})
    inputs = json_value({"positive_adjustments": number(positive_adjustments), "negative_adjustments": number(negative_adjustments),
        "loss_compensation": number(loss_compensation), "turnover": number(turnover), "instalments": number(instalments),
        "regime_code": regime_code, "support_evidence_id": evidence.pk, "support_evidence_sha256": evidence.sha256,
        "reconciliation_note": reconciliation_note, "verified_credit_total": credits, "return_version": return_version,
        "credit_verification_ids": list(TaxVerification.objects.filter(organization=organization, kind="credit", period__gte=context["fiscal_start"], period__lte=context["fiscal_end"]).values_list("pk", flat=True)),
        "final_obligation_ids": list(final_rows.values_list("pk", flat=True)),
        "tax_snapshot": annual_tax_snapshot(organization, context["fiscal_start"], context["fiscal_end"])})
    row = _save(AnnualTaxWorkpaper(organization=organization, year=year, book_snapshot=snapshot, inputs=inputs,
        result=result, calculation_version=CALCULATION_VERSION, prepared_by=actor))
    record(organization, actor, "tax.annual.prepared", row, {"year": year, "calculation_version": row.calculation_version})
    return row


@transaction.atomic
def approve_annual_workpaper(*, organization, actor, workpaper, external_review):
    _lock(organization, actor, approve=True)
    workpaper = _scope(AnnualTaxWorkpaper, workpaper, organization)
    from .services import annual_readiness
    context = annual_readiness(organization, workpaper.year)
    _assert_annual_tax_snapshot(organization, workpaper, context)
    if context["gates"] or context["closed_month_count"] != 12 or context["fiscal_end"] >= timezone.localdate():
        raise ValidationError("Seluruh tahun harus tercakup profil, selesai, dan 12 bulan ditutup sebelum persetujuan tahunan.")
    if TaxObligation.objects.filter(organization=organization, period__gte=context["fiscal_start"], period__lte=context["fiscal_end"]).exclude(status="approved").exists():
        raise ValidationError("Selesaikan pemeriksaan seluruh kertas kerja pajak dalam tahun ini sebelum persetujuan tahunan.")
    current = json_value({name: context[name] for name in workpaper.book_snapshot})
    if current != workpaper.book_snapshot:
        raise ValidationError("Buku berubah sejak kertas kerja disiapkan; buat versi persiapan baru.")
    review = _assert_review(organization, workpaper, {"action": "approve_annual"}, external_review)
    row = _save(AnnualTaxApproval(organization=organization, workpaper=workpaper, external_review=review, actor=actor))
    record(organization, actor, "tax.annual.approved", row, {"year": workpaper.year, "official_schema_validated": False})
    return row


@transaction.atomic
def verify_annual_filing(*, organization, actor, workpaper, evidence, reference, taxpayer_reference,
                         year, return_version, reported_tax, reported_balance_due, matches_confirmed=False):
    _lock(organization, actor, approve=True)
    workpaper = _scope(AnnualTaxWorkpaper, workpaper, organization)
    evidence = _scope(TaxEvidence, evidence, organization)
    profile = TaxProfile.objects.filter(organization=organization).first()
    from .services import annual_readiness
    context = annual_readiness(organization, workpaper.year)
    _assert_annual_tax_snapshot(organization, workpaper, context)
    if json_value({name: context[name] for name in workpaper.book_snapshot}) != workpaper.book_snapshot:
        raise ValidationError("Buku berubah sejak kertas kerja disetujui; catat versi dan pemeriksaan baru sebelum mencocokkan SPT.")
    if matches_confirmed is not True or not AnnualTaxApproval.objects.filter(workpaper=workpaper).exists() or not profile or not profile.taxpayer_reference or taxpayer_reference.strip() != profile.taxpayer_reference or len(reference.strip()) < 5:
        raise ValidationError("Cocokkan BPE resmi, wajib pajak, tahun dan versi SPT yang telah disetujui.")
    if year != workpaper.year or return_version != workpaper.inputs.get("return_version", 0) or number(reported_tax) != Decimal(workpaper.result["income_tax"]) or number(reported_balance_due, signed=True) != Decimal(workpaper.result["balance_due"]):
        raise ValidationError("Tahun, versi pembetulan, PPh dan kurang/lebih bayar SPT harus cocok dengan paket disetujui.")
    row = _save(AnnualTaxFiling(organization=organization, workpaper=workpaper, evidence=evidence,
        reference=reference.strip(), taxpayer_reference=taxpayer_reference.strip(), actor=actor, year=year,
        return_version=return_version, reported_tax=number(reported_tax), reported_balance_due=number(reported_balance_due, signed=True)))
    record(organization, actor, "tax.annual.filing_verified", row, {"workpaper": workpaper.pk, "year": workpaper.year, "sha256": evidence.sha256})
    return row
