"""Read-only dependencies that protect already-used supplier cash reviews."""

import calendar
from datetime import date

from django.core.exceptions import ValidationError


def _references_advance(snapshot, advance_id):
    if not isinstance(snapshot, dict):
        raise ValidationError(
            "Bukti ketergantungan pemeriksaan pajak tidak dapat dibaca."
        )
    dependency = snapshot.get("supplier_advance_reviews")
    if dependency is None:
        return False
    if not isinstance(dependency, dict) or not isinstance(
        dependency.get("inventory"), list
    ):
        raise ValidationError(
            "Bukti pemeriksaan uang muka pada paket pajak perlu diperbaiki."
        )
    for row in dependency["inventory"]:
        if not isinstance(row, dict) or type(row.get("advance_id")) is not int:
            raise ValidationError(
                "Identitas uang muka pada bukti paket pajak tidak sah."
            )
        if row["advance_id"] == advance_id:
            return True
    return False


def supplier_advance_review_correction_blockers(*, organization, advance):
    """Changing a used decision needs an explicit downstream correction first.

    Reading historical drafts remains allowed. This function neither withdraws
    approvals nor changes cash, journals, report snapshots or closed periods.
    """
    from core.models import Organization
    from finance.models import AccountingPeriod, SupplierAdvance

    from .models import (
        AnnualTaxApproval,
        AnnualTaxFiling,
        AnnualTaxWorkpaper,
        TaxObligation,
    )
    from .monthly_source_models import MonthlyFiscalApproval, MonthlyTaxSourcePackage
    from .workflow import _assert_review, digest

    org_id, advance_id = (
        getattr(organization, "pk", organization),
        getattr(advance, "pk", advance),
    )
    if type(org_id) is not int or type(advance_id) is not int:
        raise ValidationError("Identitas perusahaan atau uang muka tidak sah.")
    try:
        org = Organization.objects.get(pk=org_id)
        source = SupplierAdvance.objects.get(organization=org, pk=advance_id)
    except (Organization.DoesNotExist, SupplierAdvance.DoesNotExist):
        raise ValidationError(
            "Uang muka tidak tersedia untuk perusahaan ini."
        ) from None
    blockers = []
    for period in AccountingPeriod.objects.filter(
        organization=org, closed=True
    ).order_by("year", "month"):
        try:
            period_end = date(
                period.year,
                period.month,
                calendar.monthrange(period.year, period.month)[1],
            )
        except (ValueError, TypeError):
            raise ValidationError(
                "Identitas periode buku tertutup perlu diperiksa sebelum keputusan pajak diubah."
            ) from None
        if period_end >= source.date:
            blockers.append(
                f"Buku {period.month:02d}/{period.year} sudah ditutup. Perubahan pemeriksaan uang muka memerlukan alur koreksi periode tertutup."
            )
            break

    affected = set()
    packages = list(
        MonthlyTaxSourcePackage.objects.filter(organization=org).order_by(
            "period", "revision"
        )
    )
    for package in packages:
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
                "Integritas ketergantungan paket pajak berubah. Periksa paket asli sebelum mengganti keputusan uang muka."
            )
        approval = (
            MonthlyFiscalApproval.objects.filter(package=package)
            .select_related("external_review__evidence")
            .first()
        )
        if approval:
            if approval.organization_id != org.pk:
                raise ValidationError(
                    "Persetujuan paket pajak mempunyai perusahaan yang tidak sesuai."
                )
            from .monthly_source_workflow import _historical_integrity

            _historical_integrity(org, approval)
        if _references_advance(package.profile_snapshot, source.pk):
            affected.add(package.pk)
    if affected:
        # Carry dependencies forward only through stored, typed fiscal history.
        for package in packages:
            if not isinstance(package.profile_snapshot, dict):
                raise ValidationError(
                    "Bukti ketergantungan paket pajak tidak dapat dibaca."
                )
            history = package.profile_snapshot.get("fiscal_history", [])
            if not isinstance(history, list) or any(
                not isinstance(row, dict) or type(row.get("package_id")) is not int
                for row in history
            ):
                raise ValidationError(
                    "Riwayat ketergantungan paket pajak tidak dapat diperiksa."
                )
            if any(row.get("package_id") in affected for row in history):
                affected.add(package.pk)
        obligation = (
            TaxObligation.objects.filter(
                organization=org, source_package_id__in=affected
            )
            .exclude(status="withdrawn")
            .order_by("period", "pk")
            .first()
        )
        if obligation:
            blockers.append(
                f"Kewajiban pajak {obligation.period:%m/%Y} memakai pemeriksaan uang muka ini. Periksa dan koreksi kewajiban tersebut terlebih dahulu; dokumen lama tetap tersimpan."
            )
        # A revised draft explicitly releases the earlier approval for a new
        # review only if no continuing obligation/annual/frozen-book uses it.
        for approval in (
            MonthlyFiscalApproval.objects.filter(
                organization=org, package_id__in=affected
            )
            .select_related("package")
            .order_by("package__period")
        ):
            latest = (
                MonthlyTaxSourcePackage.objects.filter(
                    organization=org, period=approval.package.period
                )
                .order_by("-revision", "-pk")
                .first()
            )
            if latest and latest.pk == approval.package_id:
                blockers.append(
                    f"Paket fiskal {approval.package.period:%m/%Y} sudah disetujui dan memakai pemeriksaan ini. Mulai koreksi paket secara jelas sebelum mengganti keputusan uang muka."
                )
                break

    for workpaper in AnnualTaxWorkpaper.objects.filter(organization=org).order_by(
        "year", "pk"
    ):
        approval = (
            AnnualTaxApproval.objects.filter(workpaper=workpaper)
            .select_related("external_review__evidence")
            .first()
        )
        filing = AnnualTaxFiling.objects.filter(workpaper=workpaper).first()
        if approval:
            if approval.organization_id != org.pk:
                raise ValidationError(
                    "Persetujuan tahunan mempunyai perusahaan yang tidak sesuai."
                )
            _assert_review(
                org, workpaper, {"action": "approve_annual"}, approval.external_review
            )
        if filing and (not approval or filing.organization_id != org.pk):
            raise ValidationError(
                "Bukti pelaporan tahunan tidak memiliki persetujuan asal yang dapat diperiksa."
            )
        if not isinstance(workpaper.inputs, dict):
            raise ValidationError(
                "Bukti sumber kertas kerja tahunan tidak dapat dibaca."
            )
        if _references_advance(
            workpaper.inputs.get("tax_snapshot", {}), source.pk
        ) and (approval or filing):
            blockers.append(
                f"SPT tahunan {workpaper.year} yang disetujui/dilaporkan memakai pemeriksaan uang muka ini. Pembetulan formal perlu ditinjau sebelum perubahan keputusan."
            )
            break
    return blockers


def supplier_advance_report_dependency(*, organization, cutoff):
    """Separate professional decisions from commercial income-source hashes."""
    from .supplier_advance_review import supplier_advance_tax_gates

    result = supplier_advance_tax_gates(organization=organization, cutoff=cutoff)
    dependency = {
        "fingerprint": result["fingerprint"],
        "inventory": result["inventory"],
    }
    gates = [
        {
            "code": "supplier_advance_review",
            "title": "Periksa pajak uang muka pemasok",
            "detail": message,
            "severity": "review",
        }
        for message in result["blockers"]
    ]
    candidates = [
        {
            "advance_id": row["advance_id"],
            "date": date.fromisoformat(row["date"]),
            "status": row["status"],
            "reason": "Pembayaran asli perlu dicocokkan dengan laporan profesional. Sisa uang muka nol tidak menutup pemeriksaan pajak.",
        }
        for row in result["inventory"]
        if row["status"] != "reviewed"
    ]
    return dependency, gates, candidates
