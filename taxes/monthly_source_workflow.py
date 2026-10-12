"""Monthly fact extraction and immutable, professionally reviewed fiscal bridge."""

import hashlib
import re
from datetime import date, timedelta
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import models, transaction
from django.utils import timezone

from core.audit import record
from core.models import AuditEvent, Membership, Organization

from .models import (
    MonthlyFiscalApproval,
    MonthlyTaxSourcePackage,
    TaxEvidence,
    TaxObligation,
    TaxProfileDecision,
    TaxSource,
)
from .workflow import _assert_review, _lock, _save, _scope, digest, json_value

WORKFLOW_VERSION = "monthly-tax-source-v1"
MAX_MONTHLY_ADJUSTMENTS = 1000


def _period(value):
    from .rules import validate_monthly_period
    from .services import parse_period

    value = parse_period(value) if isinstance(value, str) else value
    if type(value) is not date:
        raise ValidationError(
            "Pilih masa pajak menggunakan tanggal, bukan waktu atau nilai lain."
        )
    validate_monthly_period(value)
    return value


def _owner(organization, actor):
    if not Membership.objects.filter(
        organization=organization, user=actor, user__is_active=True, role="owner"
    ).exists():
        raise PermissionDenied(
            "Owner aktif mencatat hasil pemeriksaan profesional paket ini."
        )


def _prepare_role(organization, actor):
    if not Membership.objects.filter(
        organization=organization,
        user=actor,
        user__is_active=True,
        role__in=("owner", "finance"),
    ).exists():
        raise PermissionDenied(
            "Owner atau Finance aktif menyiapkan paket sumber pajak."
        )


def _evidence_identity(evidence):
    actual = hashlib.sha256(bytes(evidence.content)).hexdigest()
    return {
        "id": evidence.pk,
        "sha256": evidence.sha256,
        "actual_sha256": actual,
        "intact": actual == evidence.sha256,
    }


def _review_identity(review):
    return {
        "id": review.pk,
        "subject_digest": review.subject_digest,
        "reviewed_on": review.reviewed_on.isoformat(),
        "decision_hash": digest(review.decision),
        "evidence": _evidence_identity(review.evidence),
    }


def _review_current(review):
    if (
        not 0 <= (timezone.localdate() - review.reviewed_on).days <= 180
        or not _evidence_identity(review.evidence)["intact"]
    ):
        raise ValidationError(
            "Tanggal pemeriksaan atau integritas PDF laporan perlu diperiksa kembali."
        )


def _profile(organization, period):
    from .services import profile_gates

    profile, gates = profile_gates(organization, period)
    row = (
        TaxProfileDecision.objects.filter(
            organization=organization, effective_from__lte=period
        )
        .order_by("-effective_from", "-pk")
        .first()
    )
    review = _review_identity(row.external_review) if row else None
    if row:
        try:
            _review_current(row.external_review)
        except ValidationError as exc:
            gates = list(gates) + [
                {"code": "profile_report", "detail": " ".join(exc.messages)}
            ]
    snapshot = json_value(
        {
            "decision_id": row.pk if row else None,
            "decision": row.snapshot if row else None,
            "regime": getattr(profile, "regime", "undecided"),
            "gates": gates,
            "review": review,
            "organization": {
                "legal_form": organization.legal_form,
                "vat_status": organization.vat_status,
                "fiscal_year_start_month": organization.fiscal_year_start_month,
            },
        }
    )
    return snapshot


def _required_slugs(period, profile_snapshot):
    cutoff = date(
        period.year + (period.month == 12), period.month % 12 + 1, 1
    ) - timedelta(days=1)
    slugs = ["pp55-2022"]
    if cutoff >= date(2026, 4, 22):
        slugs.append("pp20-2026")
    if profile_snapshot["regime"] == "final":
        slugs.extend(["pmk164-2023", "per11-2025", "pmk81-2024"])
    return slugs


def _lock_legal_sources(organization, period):
    organization = Organization.objects.get(pk=organization.pk)
    slugs = _required_slugs(period, _profile(organization, period))
    # Source publication uses the same rows. GET helpers deliberately do not
    # acquire transaction-only locks.
    list(TaxSource.objects.select_for_update().filter(slug__in=slugs).order_by("slug"))


def _legal(organization, period, profile_snapshot):
    from .knowledge import reviewed_sources_for_period

    slugs = _required_slugs(period, profile_snapshot)
    try:
        return {
            "sources": reviewed_sources_for_period(
                period, slugs=slugs, partial_slugs=("pp20-2026",)
            ),
            "issues": [],
        }
    except ValidationError as exc:
        return {"sources": [], "issues": exc.messages}


def _state(organization, period):
    from .monthly_sources import build_monthly_sources

    organization = Organization.objects.get(pk=organization.pk)
    facts = json_value(build_monthly_sources(organization, period))
    profile = _profile(organization, period)
    profile["fiscal_history"] = _history(organization, period, facts)
    from .supplier_advance_dependencies import supplier_advance_report_dependency

    dependency, advance_gates, _ = supplier_advance_report_dependency(
        organization=organization, cutoff=date.fromisoformat(facts["cutoff"])
    )
    if dependency["inventory"]:
        profile["supplier_advance_reviews"] = dependency
        profile["gates"].extend(advance_gates)
    from .opening_balance_review_dependencies import opening_balance_report_dependency

    opening_dependency, opening_gates, _ = opening_balance_report_dependency(
        organization=organization, cutoff=date.fromisoformat(facts["cutoff"])
    )
    if opening_dependency["inventory"]:
        profile["opening_cash_reviews"] = opening_dependency
        profile["gates"].extend(opening_gates)
    from .opening_refund_review_dependencies import opening_refund_report_dependency

    refund_dependency, refund_gates, _ = opening_refund_report_dependency(
        organization=organization, cutoff=date.fromisoformat(facts["cutoff"])
    )
    if refund_dependency["inventory"]:
        profile["opening_refund_reviews"] = refund_dependency
        profile["gates"].extend(refund_gates)
    legal = _legal(organization, period, profile)
    identity = digest({"snapshot": facts, "profile": profile, "legal": legal})
    return facts, profile, legal, identity


def _history(organization, period, facts):
    invoice_ids = {
        row.get("invoice_id") for row in facts["rows"] if row.get("invoice_id")
    }
    invoice_ids.update(row["id"] for row in facts.get("invoices", []))
    source_ids = {row["row_id"] for row in facts["rows"]}
    seen_periods, history = set(), []
    latest = {}
    for package in MonthlyTaxSourcePackage.objects.filter(
        organization=organization, period__lt=period
    ).order_by("period", "-revision"):
        latest.setdefault(package.period, package)
    approvals = (
        MonthlyFiscalApproval.objects.filter(
            organization=organization, package__period__lt=period
        )
        .select_related("package", "external_review__evidence")
        .order_by("-package__period", "-package__revision")
    )
    for approval in approvals:
        prior_period = approval.package.period.isoformat()
        if prior_period in seen_periods:
            continue
        seen_periods.add(prior_period)
        pending = latest[approval.package.period]
        pending_review = pending.revision > approval.package.revision
        pending_state = (
            {"pending_review": True, "pending_package_id": pending.pk}
            if pending_review
            else {}
        )
        pending_issues = (
            [
                f"Hasil pemeriksaan {approval.package.period:%Y-%m} sedang diperbaiki. Sahkan versi {pending.revision} sebelum melanjutkan bulan ini."
            ]
            if pending_review
            else []
        )
        try:
            _historical_integrity(organization, approval)
        except ValidationError as exc:
            history.append(
                {
                    "period": prior_period,
                    "package_id": approval.package_id,
                    "approval_id": approval.pk,
                    "decision_hash": approval.decision_hash,
                    "allocations": [],
                    "invoice_allocations": [],
                    "integrity": False,
                    "issues": exc.messages + pending_issues,
                    "mapping_issues": [],
                    "report": _review_identity(approval.external_review),
                    **pending_state,
                }
            )
            continue
        if approval.result.get("final_tax") is None and not pending_review:
            continue
        allocations = (
            [
                row
                for row in approval.result.get("allocations", [])
                if row.get("invoice_id") in invoice_ids
                or set(row.get("source_row_ids", [])) & source_ids
            ]
            if approval.result.get("final_tax") is not None
            else []
        )
        if allocations or pending_review:
            projections, mapping_issues = _advance_projections(
                approval, allocations, facts
            )
            history.append(
                {
                    "period": prior_period,
                    "package_id": approval.package_id,
                    "approval_id": approval.pk,
                    "decision_hash": approval.decision_hash,
                    "allocations": allocations,
                    "invoice_allocations": projections,
                    "integrity": True,
                    "issues": pending_issues,
                    "mapping_issues": mapping_issues,
                    "report": _review_identity(approval.external_review),
                    **pending_state,
                }
            )
    return history


def _historical_integrity(organization, approval):
    """Verify recorded history without treating live staleness as untaxed income."""
    package = approval.package
    if (
        digest(
            {
                "snapshot": package.snapshot,
                "profile": package.profile_snapshot,
                "legal": package.legal_sources,
            }
        )
        != package.package_hash
    ):
        raise ValidationError(
            "Integritas paket bulan sebelumnya berubah; periksa riwayat sebelum melanjutkan."
        )
    decision = _canonical_decision(package, approval.decision)
    if approval.decision_hash != digest(decision) or approval.result != _result(
        package, decision
    ):
        raise ValidationError(
            "Integritas alokasi fiskal bulan sebelumnya berubah; periksa riwayat sebelum melanjutkan."
        )
    review = _assert_review(organization, package, decision, approval.external_review)
    if not _evidence_identity(review.evidence)["intact"]:
        raise ValidationError("Integritas PDF pemeriksaan bulan sebelumnya berubah.")


def _advance_projections(approval, allocations, facts):
    original = {row["row_id"]: row for row in approval.package.snapshot["rows"]}
    applications = [
        row for row in facts["rows"] if row["source_kind"] == "advance-application"
    ]
    projections, issues = [], []
    for allocation in allocations:
        if allocation.get("invoice_id") is not None:
            continue
        advances = [
            original[ref]
            for ref in allocation["source_row_ids"]
            if ref in original and original[ref]["source_kind"] == "customer-advance"
        ]
        if not advances:
            continue
        advance_ids = {row["source_id"] for row in advances}
        linked = [row for row in applications if row.get("advance_id") in advance_ids]
        if not linked:
            continue
        original_amount = sum(
            (Decimal(row["gross_amount"]) for row in advances), Decimal(0)
        )
        if (
            allocation["kind"] != "timing"
            or Decimal(allocation["amount"]) != original_amount
        ):
            issues.append(
                "Alokasi uang muka parsial atau bruto khusus belum memiliki pemetaan fiskal ke invoice; pemeriksa harus menyelesaikan pemetaan sebelum draf baru."
            )
            continue
        for application in linked:
            projections.append(
                {
                    "kind": "timing",
                    "invoice_id": application["invoice_id"],
                    "source_row_ids": allocation["source_row_ids"],
                    "amount": application["amount"],
                    "projection": True,
                    "advance_application_id": application["source_id"],
                }
            )
    return projections, issues


@transaction.atomic
def prepare_monthly_package(*, organization, actor, period):
    _lock(organization, actor)
    _prepare_role(organization, actor)
    period = _period(period)
    _lock_legal_sources(organization, period)
    facts, profile, legal, identity = _state(organization, period)
    previous = (
        MonthlyTaxSourcePackage.objects.filter(organization=organization, period=period)
        .order_by("-revision")
        .first()
    )
    if previous and previous.package_hash == identity:
        approval = (
            MonthlyFiscalApproval.objects.filter(package=previous)
            .select_related("external_review__evidence")
            .first()
        )
        try:
            if approval:
                _review_current(approval.external_review)
            else:
                return previous
        except ValidationError:
            approval = None  # Same facts need a new immutable review version.
        if approval:
            return previous
    row = _save(
        MonthlyTaxSourcePackage(
            organization=organization,
            period=period,
            cutoff=date.fromisoformat(facts["cutoff"]),
            revision=previous.revision + 1 if previous else 1,
            previous=previous,
            snapshot=facts,
            source_hash=facts["source_hash"],
            profile_snapshot=profile,
            legal_sources=legal,
            package_hash=identity,
            extraction_version=facts["extraction_version"],
            prepared_by=actor,
        )
    )
    record(
        organization,
        actor,
        "tax.monthly_package.prepared",
        row,
        {
            "period": period.isoformat(),
            "revision": row.revision,
            "source_hash": row.source_hash,
        },
    )
    return row


def _latest_package(organization, period):
    return (
        MonthlyTaxSourcePackage.objects.filter(organization=organization, period=period)
        .order_by("-revision")
        .first()
    )


def _revision_event(organization, package):
    return AuditEvent.objects.filter(
        organization=organization,
        action="tax.monthly_package.review_revision",
        object_type="MonthlyTaxSourcePackage",
        object_id=str(package.pk),
    ).first()


def _correction_boundary(organization, package):
    # Follow only stored package dependencies; unrelated later months are not
    # amendments of this classification. Retain indirect dependency chains too.
    affected = {package.pk}
    for later in MonthlyTaxSourcePackage.objects.filter(
        organization=organization, period__gt=package.period
    ).order_by("period", "revision"):
        if any(item.get("package_id") in affected for item in later.fiscal_history):
            affected.add(later.pk)
    obligation = (
        TaxObligation.objects.filter(
            organization=organization, tax_type="final_turnover"
        )
        .exclude(status="withdrawn")
        .filter(
            models.Q(period=package.period) | models.Q(source_package_id__in=affected)
        )
        .order_by("period", "pk")
        .first()
    )
    if obligation:
        from .workflow import withdrawal_blocker

        protected = withdrawal_blocker(obligation)
        return (
            f"Kewajiban final {obligation.period:%Y-%m} terkait pemeriksaan ini. {protected}"
            if protected
            else f"Tarik draf final {obligation.period:%Y-%m} secara jelas sebelum memperbaiki pemeriksaan; isi dan riwayat draf tetap tersimpan."
        ), obligation
    from .models import AnnualTaxApproval, AnnualTaxFiling

    if (
        AnnualTaxApproval.objects.filter(
            organization=organization, workpaper__year=package.period.year
        ).exists()
        or AnnualTaxFiling.objects.filter(
            organization=organization, workpaper__year=package.period.year
        ).exists()
    ):
        return (
            "SPT tahunan untuk tahun ini sudah disetujui atau memiliki bukti pelaporan. Koreksi formal dan riwayat pembetulan harus ditinjau profesional; alur ini belum mendukungnya.",
            None,
        )
    return "", None


@transaction.atomic
def revise_monthly_review(
    *, organization, actor, package, reason, expected_fingerprint, expected_version
):
    """Reopen a classification without changing its recorded financial facts."""
    _lock(organization, actor, approve=True)
    _owner(organization, actor)
    package = _scope(MonthlyTaxSourcePackage, package, organization, lock=True)
    if not isinstance(reason, str) or not 10 <= len(reason.strip()) <= 2000:
        raise ValidationError(
            "Jelaskan alasan pemeriksaan ulang dalam 10–2.000 karakter."
        )
    reason = reason.strip()
    if (
        not isinstance(expected_fingerprint, str)
        or expected_fingerprint != package.package_hash
        or type(expected_version) is not int
        or expected_version != package.revision
    ):
        raise ValidationError(
            "Identitas dan versi halaman berubah. Muat ulang paket sebelum meminta perbaikan."
        )
    _lock_legal_sources(organization, package.period)
    latest = _latest_package(organization, package.period)
    if latest.pk != package.pk:
        event = _revision_event(organization, latest)
        if (
            latest.previous_id == package.pk
            and event
            and event.detail.get("reason") == reason
            and event.detail.get("source_version") == expected_version
            and event.detail.get("source_fingerprint") == expected_fingerprint
            and not MonthlyFiscalApproval.objects.filter(package=latest).exists()
        ):
            _assert_current(organization, latest)
            blocker, _ = _correction_boundary(organization, package)
            if blocker:
                raise ValidationError(blocker)
            return latest
        raise ValidationError(
            "Paket ini sudah digantikan. Buka versi terbaru; permintaan perbaikan lama tidak dapat ditimpa."
        )
    approval = MonthlyFiscalApproval.objects.filter(
        organization=organization, package=package
    ).first()
    if not approval:
        raise ValidationError(
            "Pemeriksaan ulang hanya untuk hasil yang sudah disahkan; selesaikan pemeriksaan versi ini terlebih dahulu."
        )
    _assert_approval(organization, package, approval)
    blocker, _ = _correction_boundary(organization, package)
    if blocker:
        raise ValidationError(blocker)
    row = _save(
        MonthlyTaxSourcePackage(
            organization=organization,
            period=package.period,
            cutoff=package.cutoff,
            revision=package.revision + 1,
            previous=package,
            snapshot=package.snapshot,
            source_hash=package.source_hash,
            profile_snapshot=package.profile_snapshot,
            legal_sources=package.legal_sources,
            package_hash=package.package_hash,
            extraction_version=package.extraction_version,
            prepared_by=actor,
        )
    )
    record(
        organization,
        actor,
        "tax.monthly_package.review_revision",
        row,
        {
            "reason": reason,
            "previous_package_id": package.pk,
            "previous_approval_id": approval.pk,
            "source_version": expected_version,
            "source_fingerprint": expected_fingerprint,
        },
    )
    return row


def package_status(*, organization, package):
    package = _scope(MonthlyTaxSourcePackage, package, organization)
    _, _, _, current = _state(organization, package.period)
    stale = (
        current != package.package_hash
        or digest(
            {
                "snapshot": package.snapshot,
                "profile": package.profile_snapshot,
                "legal": package.legal_sources,
            }
        )
        != package.package_hash
    )
    approval = MonthlyFiscalApproval.objects.filter(
        organization=organization, package=package
    ).first()
    latest = _latest_package(organization, package.period)
    superseded = latest.pk != package.pk
    event = _revision_event(organization, package)
    issues = list(package.profile_snapshot["gates"]) + [
        {"detail": item} for item in package.legal_sources["issues"]
    ]
    issues.extend(package.snapshot.get("issues", []))
    if stale:
        issues.append(
            {
                "detail": "Data atau aturan berubah. Siapkan versi paket baru sebelum melanjutkan."
            }
        )
    if package.cutoff > timezone.localdate():
        issues.append(
            {"detail": "Bulan ini belum berakhir. Paket ini masih merupakan pratinjau."}
        )
    approval_valid = False
    if approval and not stale and not superseded:
        try:
            _assert_approval(organization, package, approval)
            approval_valid = True
        except ValidationError as exc:
            stale = True
            issues.extend({"detail": message} for message in exc.messages)
    correction_blocker, blocking_obligation = _correction_boundary(
        organization, package
    )
    if superseded:
        issues.append(
            {
                "detail": f"Versi ini sudah digantikan oleh versi {latest.revision}. Laporan lama tetap tersimpan; lanjutkan pada versi terbaru."
            }
        )
    return {
        "package": package,
        "approval": approval,
        "stale": stale,
        "superseded": superseded,
        "replacement": latest if superseded else None,
        "revision_reason": event.detail.get("reason", "") if event else "",
        "can_review_revision": bool(
            approval_valid and not stale and not superseded and not correction_blocker
        ),
        "correction_blocker": correction_blocker,
        "blocking_obligation": blocking_obligation,
        "status": "superseded"
        if superseded
        else "stale"
        if stale
        else "approved"
        if approval
        else "review",
        "issues": issues,
        "fingerprint": package.package_hash,
        "version": package.revision,
        "taxable_turnover": approval.result.get("taxable_turnover")
        if approval and not stale
        else None,
        "final_tax": approval.result.get("final_tax")
        if approval and not stale
        else None,
        "regime": package.profile_snapshot["regime"],
        "can_generate": bool(
            approval_valid
            and not stale
            and not superseded
            and package.profile_snapshot["regime"] == "final"
            and package.cutoff <= timezone.localdate()
        ),
    }


def monthly_decision(
    package,
    *,
    rows,
    adjustments=None,
    completeness_confirmed=False,
    inventory_note="",
    nil_confirmed=False,
    self_payment_only_confirmed=False,
    customer_final_withholding=False,
    prior_history_confirmed=False,
    object_code="",
    rule_reference="",
):
    """Normalize source-bound decisions; no caller-supplied monthly total exists."""
    from .rules import number

    flags = (
        completeness_confirmed,
        nil_confirmed,
        self_payment_only_confirmed,
        customer_final_withholding,
        prior_history_confirmed,
    )
    if any(type(value) is not bool for value in flags):
        raise ValidationError("Konfirmasi harus berupa pilihan ya atau tidak yang sah.")
    if (
        completeness_confirmed is not True
        or not isinstance(inventory_note, str)
        or not 10 <= len(inventory_note.strip()) <= 4000
    ):
        raise ValidationError(
            "Konfirmasikan kelengkapan semua produk, kanal, cabang, dan sumber; jelaskan cakupannya."
        )
    if package.fiscal_history and prior_history_confirmed is not True:
        raise ValidationError(
            "Periksa alokasi fiskal yang telah dicatat pada bulan sebelumnya."
        )
    if (
        not isinstance(rule_reference, str)
        or not 3 <= len(rule_reference.strip()) <= 300
    ):
        raise ValidationError("Referensi dasar hukum hasil pemeriksaan wajib diisi.")
    if package.profile_snapshot["regime"] == "final":
        if not self_payment_only_confirmed or customer_final_withholding:
            raise ValidationError(
                "Paket ini hanya mendukung final setor sendiri; pemotongan final pelanggan memerlukan alur tersendiri."
            )
        if not isinstance(object_code, str) or not re.fullmatch(
            r"[A-Za-z0-9.-]{3,40}", object_code
        ):
            raise ValidationError("Kode objek final yang diperiksa wajib diisi.")
    elif not isinstance(object_code, str) or len(object_code) > 40:
        raise ValidationError("Kode objek tidak sah.")
    sources = {row["row_id"]: row for row in package.snapshot["rows"]}
    if (
        len(sources) != len(package.snapshot["rows"])
        or not isinstance(rows, list)
        or len(rows) != len(sources)
    ):
        raise ValidationError(
            "Setiap baris sumber, termasuk uang muka dan pengembalian, harus diperiksa tepat satu kali."
        )
    normalized, classified = [], {}
    for row in rows:
        if (
            not isinstance(row, dict)
            or set(row) != {"row_id", "decision", "reason"}
            or not isinstance(row["row_id"], str)
            or row["row_id"] not in sources
            or row["row_id"] in classified
        ):
            raise ValidationError(
                "Pilihan harus menunjuk baris sumber paket ini tanpa duplikasi."
            )
        source = sources[row["row_id"]]
        allowed = (
            ("include", "exclude")
            if source["role"] == "candidate"
            else ("exclude", "adjustment")
        )
        if (
            row["decision"] not in allowed
            or not isinstance(row["reason"], str)
            or not 3 <= len(row["reason"].strip()) <= 1000
        ):
            raise ValidationError(
                "Pilih perlakuan dan alasan pemeriksaan untuk setiap baris sumber."
            )
        if (
            source.get("opening_cancellation") is not None
            and row["decision"] != "exclude"
        ):
            raise ValidationError(
                "Pencocokan piutang lama yang dibatalkan harus dikecualikan secara jelas dengan alasan; jangan memasukkannya sebagai alokasi fiskal."
            )
        if (
            source.get("source_kind") == "opening-refund"
            and row["decision"] != "exclude"
        ):
            raise ValidationError(
                "Pengembalian pokok saldo lama harus dikecualikan secara jelas sesuai laporan tersendiri. Cabang pengurang omzet atau pemulihan pajak belum didukung."
            )
        item = {
            "row_id": row["row_id"],
            "decision": row["decision"],
            "reason": row["reason"].strip(),
        }
        classified[item["row_id"]] = item
        normalized.append(item)
    adjustments = [] if adjustments is None else adjustments
    if not isinstance(adjustments, list) or len(adjustments) > MAX_MONTHLY_ADJUSTMENTS:
        raise ValidationError(
            f"Maksimal {MAX_MONTHLY_ADJUSTMENTS} penyesuaian sumber yang didukung per paket."
        )
    corrected, touched, kind_sources = [], set(), set()
    for row in adjustments:
        allowed_keys = {
            "kind",
            "amount",
            "reason",
            "evidence_id",
            "source_row_ids",
            "evidence_identity",
        }
        if (
            not isinstance(row, dict)
            or set(row) - allowed_keys
            or not {"kind", "amount", "reason", "evidence_id", "source_row_ids"}
            <= set(row)
        ):
            raise ValidationError(
                "Penyesuaian memerlukan jenis, selisih, alasan, dokumen, dan sumber yang jelas."
            )
        if (
            row["kind"] not in ("timing", "gross_basis", "correction")
            or not isinstance(row["reason"], str)
            or not 3 <= len(row["reason"].strip()) <= 2000
        ):
            raise ValidationError("Jenis dan alasan penyesuaian sumber tidak sah.")
        amount = number(row["amount"], signed=True)
        refs = row["source_row_ids"]
        if (
            not amount
            or not isinstance(refs, list)
            or not refs
            or any(not isinstance(ref, str) or ref not in sources for ref in refs)
            or len(set(refs)) != len(refs)
        ):
            raise ValidationError(
                "Penyesuaian harus bukan nol dan merujuk sumber paket tanpa duplikasi."
            )
        invoices = {sources[ref].get("invoice_id") for ref in refs}
        if len(invoices) != 1 or any(
            (row["kind"], ref) in kind_sources for ref in refs
        ):
            raise ValidationError(
                "Satu penyesuaian hanya untuk satu invoice/sumber dan tidak mengulang jenis penyesuaian yang sama."
            )
        for ref in refs:
            source, choice = sources[ref], classified[ref]["decision"]
            if source.get("opening_cancellation") is not None:
                raise ValidationError(
                    "Pencocokan kas yang dibatalkan tidak dapat dipakai untuk penyesuaian waktu, bruto atau koreksi; periksa sumber pengganti yang benar."
                )
            if source.get("source_kind") == "opening-refund":
                raise ValidationError(
                    "Pengembalian pokok saldo lama tidak dapat dipakai sebagai penyesuaian waktu, bruto atau koreksi penghasilan pada cabang ini."
                )
            if source["role"] == "context" and choice != "adjustment":
                raise ValidationError(
                    "Sumber konteks yang disesuaikan harus ditandai sebagai penyesuaian."
                )
            if source["role"] == "candidate" and (
                (row["kind"] == "timing" and choice != "exclude")
                or (
                    row["kind"] != "timing"
                    and (
                        choice != "include"
                        or number(source["amount"], signed=True) <= 0
                    )
                )
            ):
                raise ValidationError(
                    "Penyesuaian waktu mengganti sumber yang dikecualikan; selisih bruto/koreksi menambah sumber positif yang dimasukkan."
                )
        invoice_id = next(iter(invoices))
        if (
            row["kind"] == "correction"
            and amount < 0
            and invoice_id
            and any(
                source.get("invoice_id") == invoice_id
                and source["role"] == "candidate"
                and classified[source["row_id"]]["decision"] == "include"
                and number(source["amount"], signed=True) < 0
                for source in sources.values()
            )
        ):
            raise ValidationError(
                "Kredit pendapatan sudah mengurangi buku; jangan menguranginya lagi sebagai koreksi."
            )
        if type(row["evidence_id"]) is not int:
            raise ValidationError("Dokumen penyesuaian tidak sah.")
        evidence = _scope(TaxEvidence, row["evidence_id"], package.organization)
        identity = _evidence_identity(evidence)
        if not identity["intact"]:
            raise ValidationError("Integritas PDF penyesuaian berubah.")
        corrected.append(
            {
                "kind": row["kind"],
                "amount": str(amount),
                "reason": row["reason"].strip(),
                "evidence_id": evidence.pk,
                "source_row_ids": sorted(refs),
                "evidence_identity": identity,
            }
        )
        touched.update(refs)
        kind_sources.update((row["kind"], ref) for ref in refs)
    if any(
        item["decision"] == "adjustment" and item["row_id"] not in touched
        for item in normalized
    ):
        raise ValidationError(
            "Setiap konteks penyesuaian harus memiliki penyesuaian berdokumen yang sesuai."
        )
    result = {
        "action": "approve_monthly_package",
        "workflow_version": WORKFLOW_VERSION,
        "rows": sorted(normalized, key=lambda row: row["row_id"]),
        "adjustments": sorted(
            corrected,
            key=lambda row: (row["kind"], row["source_row_ids"], row["evidence_id"]),
        ),
        "completeness_confirmed": completeness_confirmed,
        "inventory_note": inventory_note.strip(),
        "nil_confirmed": nil_confirmed,
        "self_payment_only_confirmed": self_payment_only_confirmed,
        "customer_final_withholding": customer_final_withholding,
        "prior_history_confirmed": prior_history_confirmed,
        "object_code": object_code.strip(),
        "rule_reference": rule_reference.strip(),
    }
    _result(package, result)
    return result


def _opening_cash_lineage(sources, prior, allocations):
    """Conserve actual active assignments separately from reviewed fiscal gross.

    Several valid claims may share a bank transaction. Their physical amounts
    are checked here; documented gross/timing differences are not capped at
    cash. An original canceled assignment cannot receive any fiscal allocation.
    """
    groups, postings = {}, set()
    for source in sources.values():
        lineage, cancellation = (
            source.get("cash_lineage"),
            source.get("opening_cancellation"),
        )
        if lineage is None and cancellation is None:
            continue  # Preserve historical contracts without cash recovery.
        bank, proof = source.get("bank"), source.get("source_proof")
        if (
            source.get("source_kind") != "opening-receipt"
            or not isinstance(lineage, dict)
            or set(lineage) != {"kind", "transaction_id"}
            or lineage.get("kind") != "bank_transaction"
            or type(lineage.get("transaction_id")) is not int
            or not isinstance(bank, dict)
            or bank.get("id") != lineage["transaction_id"]
            or not isinstance(proof, dict)
            or proof.get("settlement_id") != source.get("source_id")
        ):
            raise ValidationError(
                "Identitas kas asli dan jejak pencocokan ulang tidak dapat diperiksa."
            )
        current = [
            row for row in allocations if source["row_id"] in row["source_row_ids"]
        ]
        history = [row for row in prior if source["row_id"] in row["source_row_ids"]]
        if cancellation is not None:
            if (
                not isinstance(cancellation, dict)
                or cancellation.get("settlement_id") != source["source_id"]
                or cancellation.get("transaction_id") != lineage["transaction_id"]
            ):
                raise ValidationError(
                    "Bukti pembatalan tidak cocok dengan pencocokan kas asli."
                )
            if current or history:
                raise ValidationError(
                    "Pencocokan kas yang dibatalkan masih memiliki alokasi fiskal. Periksa riwayat dan sumber pengganti tanpa menghitung kas dua kali."
                )
            continue
        if current and history:
            raise ValidationError(
                "Sumber penerimaan ini sudah memiliki alokasi fiskal masa sebelumnya. Koreksi riwayat secara jelas sebelum mengalokasikannya lagi."
            )
        posting = proof.get("bank_posting")
        if (
            not isinstance(posting, dict)
            or type(posting.get("id")) is not int
            or posting["id"] in postings
        ):
            raise ValidationError(
                "Satu klaim pencocokan bank tercantum lebih dari sekali dalam sumber fiskal."
            )
        postings.add(posting["id"])
        cash = Decimal(source["cash_amount"])
        if cash <= 0 or cash != Decimal(posting["signed_amount"]):
            raise ValidationError(
                "Jumlah klaim kas aktif tidak cocok dengan penerimaan bank aslinya."
            )
        key = lineage["transaction_id"]
        group = groups.setdefault(
            key, {"cash": Decimal(0), "bank": Decimal(bank["amount"])}
        )
        if group["bank"] != Decimal(bank["amount"]):
            raise ValidationError(
                "Jumlah mutasi yang sama tidak konsisten pada sumber pencocokan."
            )
        group["cash"] += cash
    if any(
        group["bank"] <= 0 or group["cash"] > group["bank"] for group in groups.values()
    ):
        raise ValidationError(
            "Klaim penerimaan aktif melebihi mutasi bank nyata. Periksa pencocokan asli; bruto fiskal tetap memerlukan bukti tersendiri."
        )


def _result(package, decision):
    from .rules import calculate_tax, number

    sources = {row["row_id"]: row for row in package.snapshot["rows"]}
    allocations = []
    if any(
        not history.get("integrity", True)
        or history.get("mapping_issues")
        or history.get("pending_review")
        for history in package.fiscal_history
    ):
        raise ValidationError(
            "Riwayat fiskal atau pemetaan uang muka belum dapat digunakan. Periksa sumber bulan sebelumnya."
        )
    prior = [
        row
        for history in package.fiscal_history
        for row in history["allocations"] + history.get("invoice_allocations", [])
    ]
    for item in decision["rows"]:
        if item["decision"] == "include":
            source = sources[item["row_id"]]
            invoice_id = source.get("invoice_id")
            if invoice_id and any(
                row.get("invoice_id") == invoice_id
                and row["kind"] == "timing"
                and Decimal(row["amount"]) > 0
                for row in prior
            ):
                raise ValidationError(
                    "Invoice sudah mendapat alokasi waktu pada bulan sebelumnya. Kecualikan buku lalu periksa sisa yang belum dialokasikan."
                )
            allocation_kind = (
                "opening_cash" if source["source_kind"] == "opening-receipt" else "book"
            )
            allocations.append(
                {
                    "kind": allocation_kind,
                    "invoice_id": invoice_id,
                    "source_row_ids": [source["row_id"]],
                    "amount": str(number(source["amount"], signed=True)),
                }
            )
    for item in decision["adjustments"]:
        allocations.append(
            {
                "kind": item["kind"],
                "invoice_id": sources[item["source_row_ids"][0]].get("invoice_id"),
                "source_row_ids": item["source_row_ids"],
                "amount": item["amount"],
            }
        )
    _opening_cash_lineage(sources, prior, allocations)
    # Unspent advances can remain as contextual facts in later months. Their
    # already assessed source amount cannot become a new monthly adjustment.
    for source in sources.values():
        if source["source_kind"] != "customer-advance":
            continue
        related = [
            row
            for row in prior + allocations
            if row.get("invoice_id") is None
            and source["row_id"] in row["source_row_ids"]
        ]
        if related:
            total = sum((Decimal(row["amount"]) for row in related), Decimal(0))
            gross = Decimal(source["gross_amount"]) + sum(
                (
                    Decimal(row["amount"])
                    for row in related
                    if row["kind"] == "gross_basis"
                ),
                Decimal(0),
            )
            if total < 0 or total > gross:
                raise ValidationError(
                    "Alokasi uang muka melebihi sumber atau sisa yang belum pernah dikenakan pajak."
                )
    invoices = {row["id"]: row for row in package.snapshot.get("invoices", [])}
    for invoice_id in {row["invoice_id"] for row in allocations if row["invoice_id"]}:
        if invoice_id not in invoices:
            raise ValidationError("Invoice alokasi tidak tercakup dalam sumber paket.")
        related = [
            row for row in prior + allocations if row.get("invoice_id") == invoice_id
        ]
        total = sum((Decimal(row["amount"]) for row in related), Decimal(0))
        gross = Decimal(invoices[invoice_id]["total"]) + sum(
            (Decimal(row["amount"]) for row in related if row["kind"] == "gross_basis"),
            Decimal(0),
        )
        if total > gross:
            raise ValidationError(
                "Alokasi bulan ini melebihi sisa bruto invoice setelah alokasi bulan sebelumnya; jangan mengenakan pajak dua kali."
            )
        if package.profile_snapshot["regime"] == "final" and total < 0:
            raise ValidationError(
                "Koreksi melebihi jumlah yang pernah dialokasikan untuk invoice ini; jangan menutupinya dengan pendapatan invoice lain."
            )
    bridge = sum((Decimal(row["amount"]) for row in allocations), Decimal(0))
    if bridge == 0 and not decision["nil_confirmed"]:
        raise ValidationError(
            "Konfirmasikan hasil nihil setelah semua sumber dan keputusan diperiksa."
        )
    result = {
        "reviewed_bridge_amount": str(bridge.quantize(Decimal("0.01"))),
        "allocations": allocations,
        "taxable_turnover": None,
        "final_tax": None,
        "raw_taxable_turnover": None,
        "rate": None,
        "calculation_version": None,
        "regime": package.profile_snapshot["regime"],
    }
    if package.profile_snapshot["regime"] == "final":
        if bridge < 0:
            raise ValidationError(
                "Dasar final negatif memerlukan koreksi masa yang belum didukung; jangan memakai nihil atau menggeser otomatis."
            )
        calculation = calculate_tax("final_turnover_005", bridge)
        result.update(
            taxable_turnover=str(calculation["base"]),
            raw_taxable_turnover=str(bridge),
            final_tax=str(calculation["amount"]),
            rate=str(calculation["rate"]),
            calculation_version=calculation["version"],
        )
    return result


def _canonical_decision(package, decision):
    if (
        not isinstance(decision, dict)
        or decision.get("action") != "approve_monthly_package"
        or decision.get("workflow_version") != WORKFLOW_VERSION
    ):
        raise ValidationError("Keputusan paket tidak sah.")
    keys = {
        "rows",
        "adjustments",
        "completeness_confirmed",
        "inventory_note",
        "nil_confirmed",
        "self_payment_only_confirmed",
        "customer_final_withholding",
        "prior_history_confirmed",
        "object_code",
        "rule_reference",
    }
    if set(decision) != keys | {"action", "workflow_version"}:
        raise ValidationError(
            "Keputusan harus berisi klasifikasi sumber lengkap, tanpa total bulanan yang diketik."
        )
    canonical = monthly_decision(package, **{key: decision[key] for key in keys})
    if canonical != decision:
        raise ValidationError(
            "Keputusan atau bukti penyesuaian berubah. Catat pemeriksaan baru."
        )
    return canonical


def _assert_current(
    organization, package, *, expected_fingerprint=None, expected_version=None
):
    if expected_fingerprint is not None and (
        not isinstance(expected_fingerprint, str)
        or expected_fingerprint != package.package_hash
    ):
        raise ValidationError(
            "Versi sumber pada halaman berubah. Muat ulang paket sebelum melanjutkan."
        )
    if expected_version is not None and (
        type(expected_version) is not int or expected_version != package.revision
    ):
        raise ValidationError("Versi paket berubah. Muat ulang halaman.")
    latest = _latest_package(organization, package.period)
    if latest.pk != package.pk:
        raise ValidationError(
            "Paket sudah digantikan oleh versi terbaru; laporan lama tidak dapat menghasilkan atau mengesahkan kewajiban baru."
        )
    _, _, _, current = _state(organization, package.period)
    stored = digest(
        {
            "snapshot": package.snapshot,
            "profile": package.profile_snapshot,
            "legal": package.legal_sources,
        }
    )
    if current != package.package_hash or stored != package.package_hash:
        raise ValidationError(
            "Data, bukti, riwayat fiskal, atau aturan berubah. Siapkan paket sumber baru."
        )
    _period(package.period)
    if package.cutoff > timezone.localdate():
        raise ValidationError("Bulan belum berakhir; pratinjau belum dapat disahkan.")
    if package.profile_snapshot["gates"] or package.legal_sources["issues"]:
        raise ValidationError(
            "Profil dan sumber hukum masa ini harus lengkap dan masih ditinjau sebelum persetujuan."
        )
    if any(
        item.get("hard_blocker") is not False
        for item in package.snapshot.get("issues", [])
    ):
        raise ValidationError(
            "Sumber transaksi belum cocok atau integritasnya belum lengkap; perbaiki sumber sebelum persetujuan."
        )
    if any(
        not item.get("integrity", True)
        or item.get("mapping_issues")
        or item.get("pending_review")
        or not item["report"]["evidence"]["intact"]
        for item in package.fiscal_history
    ):
        raise ValidationError(
            "Integritas bukti alokasi bulan sebelumnya perlu diperiksa."
        )


def _assert_approval(organization, package, approval):
    _assert_current(organization, package)
    decision = _canonical_decision(package, approval.decision)
    if approval.decision_hash != digest(decision) or approval.result != _result(
        package, decision
    ):
        raise ValidationError("Integritas keputusan dan hasil paket berubah.")
    review = _assert_review(organization, package, decision, approval.external_review)
    _review_current(review)
    return approval


@transaction.atomic
def approve_monthly_package(
    *,
    organization,
    actor,
    package,
    decision,
    external_review,
    expected_fingerprint,
    expected_version,
):
    _lock(organization, actor, approve=True)
    _owner(organization, actor)
    package = _scope(MonthlyTaxSourcePackage, package, organization, lock=True)
    if not isinstance(expected_fingerprint, str) or type(expected_version) is not int:
        raise ValidationError("Identitas dan versi halaman wajib sesuai paket.")
    _lock_legal_sources(organization, package.period)
    if MonthlyFiscalApproval.objects.filter(package=package).exists():
        raise ValidationError(
            "Paket telah disahkan; keputusan tetap disimpan. Siapkan versi baru bila diperlukan."
        )
    _assert_current(
        organization,
        package,
        expected_fingerprint=expected_fingerprint,
        expected_version=expected_version,
    )
    if (
        TaxObligation.objects.filter(
            organization=organization, period=package.period, tax_type="final_turnover"
        )
        .exclude(status="withdrawn")
        .exclude(source_package=package)
        .exists()
    ):
        raise ValidationError(
            "Masa ini sudah memiliki kewajiban final aktif. Tinjau draf lama atau koreksi formal sebelum membuat paket pengganti."
        )
    decision = _canonical_decision(package, decision)
    review = _assert_review(organization, package, decision, external_review)
    _review_current(review)
    result = _result(package, decision)
    row = _save(
        MonthlyFiscalApproval(
            organization=organization,
            package=package,
            external_review=review,
            decision=decision,
            decision_hash=digest(decision),
            result=result,
            actor=actor,
        )
    )
    record(
        organization,
        actor,
        "tax.monthly_package.approved",
        package,
        {
            "approval_id": row.pk,
            "external_review": review.pk,
            "decision_hash": row.decision_hash,
        },
    )
    return row


def assert_package_for_obligation(organization, obligation, *, lock_sources=False):
    if not obligation.source_package_id:
        return None
    package = _scope(
        MonthlyTaxSourcePackage, obligation.source_package_id, organization
    )
    if lock_sources:
        _lock_legal_sources(organization, package.period)
    approval = MonthlyFiscalApproval.objects.filter(
        organization=organization, package=package
    ).first()
    if not approval:
        raise ValidationError(
            "Paket sumber belum memiliki pemeriksaan fiskal yang disahkan."
        )
    _assert_approval(organization, package, approval)
    from .rules import calculate_tax, number

    calculation = calculate_tax(
        "final_turnover_005", approval.result["taxable_turnover"]
    )
    if (
        obligation.period != package.period
        or obligation.rule_code != "final_turnover_005"
        or obligation.direction != "own_tax"
        or obligation.tax_type != "final_turnover"
        or obligation.base != number(calculation["base"])
        or obligation.amount != number(calculation["amount"])
        or obligation.rate != calculation["rate"]
        or obligation.calculation_version != calculation["version"]
    ):
        raise ValidationError(
            "Kewajiban harus sama persis dengan angka dan aturan paket yang disahkan."
        )
    return approval


@transaction.atomic
def generate_monthly_obligation(
    *, organization, actor, package, expected_fingerprint, expected_version
):
    _lock(organization, actor)
    _prepare_role(organization, actor)
    package = _scope(MonthlyTaxSourcePackage, package, organization, lock=True)
    if not isinstance(expected_fingerprint, str) or type(expected_version) is not int:
        raise ValidationError("Identitas dan versi halaman wajib sesuai paket.")
    _lock_legal_sources(organization, package.period)
    _assert_current(
        organization,
        package,
        expected_fingerprint=expected_fingerprint,
        expected_version=expected_version,
    )
    approval = MonthlyFiscalApproval.objects.filter(
        organization=organization, package=package
    ).first()
    if not approval:
        raise ValidationError(
            "Catat dan sahkan laporan pemeriksaan paket terlebih dahulu."
        )
    _assert_approval(organization, package, approval)
    if package.profile_snapshot["regime"] != "final":
        raise ValidationError(
            "Paket aturan umum tidak menghasilkan PPh final; tidak ada perhitungan bulanan 22% dari omzet."
        )
    active = TaxObligation.objects.filter(
        organization=organization, period=package.period, tax_type="final_turnover"
    ).exclude(status="withdrawn")
    existing = active.filter(source_package=package).first()
    if existing:
        assert_package_for_obligation(organization, existing)
        return existing
    if active.exists():
        raise ValidationError(
            "Kewajiban final aktif sudah ada untuk masa ini. Jangan membuat kewajiban atau pembayaran final ganda."
        )
    from .workflow import prepare_obligation

    return prepare_obligation(
        organization=organization,
        actor=actor,
        period=package.period,
        rule_code="final_turnover_005",
        base=approval.result["taxable_turnover"],
        direction="own_tax",
        source_reference=f"Paket pajak {package.period:%Y-%m} versi {package.revision}",
        rule_reference=approval.decision["rule_reference"],
        object_code=approval.decision["object_code"],
        source_evidence=approval.external_review.evidence,
        note="Draf dari paket sumber dan keputusan fiskal yang disahkan.",
        source_package=package,
    )
