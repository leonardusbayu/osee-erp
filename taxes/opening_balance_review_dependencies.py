"""Read-only dependencies that protect already-used opening-balance cash reviews."""

import calendar
from datetime import date

from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import transaction
from django.db.models import Q


def _references_settlement(snapshot, settlement_id):
    if not isinstance(snapshot, dict):
        raise ValidationError(
            "Bukti ketergantungan pemeriksaan pajak tidak dapat dibaca."
        )
    dependency = snapshot.get("opening_cash_reviews")
    if dependency is None:
        return False
    if not isinstance(dependency, dict) or not isinstance(
        dependency.get("inventory"), list
    ):
        raise ValidationError(
            "Bukti pemeriksaan saldo awal pada paket pajak perlu diperbaiki."
        )
    for row in dependency["inventory"]:
        if not isinstance(row, dict) or type(row.get("settlement_id")) is not int:
            raise ValidationError(
                "Identitas saldo awal pada bukti paket pajak tidak sah."
            )
        if row["settlement_id"] == settlement_id:
            return True
    return False


def opening_settlement_review_correction_blockers(*, organization, settlement):
    """Changing a used decision needs an explicit downstream correction first.

    Reading historical drafts remains allowed. This function neither withdraws
    approvals nor changes cash, journals, report snapshots or closed periods.
    """
    from core.models import Organization
    from finance.models import AccountingPeriod, OpeningBalanceSettlement

    from .models import (
        AnnualTaxApproval,
        AnnualTaxFiling,
        AnnualTaxWorkpaper,
        TaxObligation,
    )
    from .monthly_source_models import MonthlyFiscalApproval, MonthlyTaxSourcePackage
    from .workflow import _assert_review, digest

    org_id, settlement_id = (
        getattr(organization, "pk", organization),
        getattr(settlement, "pk", settlement),
    )
    if type(org_id) is not int or type(settlement_id) is not int:
        raise ValidationError("Identitas perusahaan atau saldo awal tidak sah.")
    try:
        org = Organization.objects.get(pk=org_id)
        source = OpeningBalanceSettlement.objects.get(
            organization=org, pk=settlement_id
        )
    except (Organization.DoesNotExist, OpeningBalanceSettlement.DoesNotExist):
        raise ValidationError(
            "Kas saldo awal tidak tersedia untuk perusahaan ini."
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
                f"Buku {period.month:02d}/{period.year} sudah ditutup. Perubahan pemeriksaan saldo awal memerlukan alur koreksi periode tertutup."
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
                "Integritas ketergantungan paket pajak berubah. Periksa paket asli sebelum mengganti keputusan saldo awal."
            )
        approval = MonthlyFiscalApproval.objects.filter(package=package).first()
        if approval:
            if approval.organization_id != org.pk:
                raise ValidationError(
                    "Persetujuan paket pajak mempunyai perusahaan yang tidak sesuai."
                )
            from .monthly_source_workflow import _historical_integrity

            try:
                _historical_integrity(org, approval)
            except (
                ObjectDoesNotExist,
                TypeError,
                ValueError,
                KeyError,
                AttributeError,
            ):
                raise ValidationError(
                    "Bukti persetujuan atau ketergantungan paket bulanan rusak. Periksa sumber sebelum keputusan kas diubah."
                ) from None
        if _references_settlement(package.profile_snapshot, source.pk):
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
                f"Kewajiban pajak {obligation.period:%m/%Y} memakai pemeriksaan saldo awal ini. Periksa dan koreksi kewajiban tersebut terlebih dahulu; dokumen lama tetap tersimpan."
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
                    f"Paket fiskal {approval.package.period:%m/%Y} sudah disetujui dan memakai pemeriksaan ini. Mulai koreksi paket secara jelas sebelum mengganti keputusan saldo awal."
                )
                break

    for workpaper in AnnualTaxWorkpaper.objects.filter(organization=org).order_by(
        "year", "pk"
    ):
        approval = AnnualTaxApproval.objects.filter(workpaper=workpaper).first()
        filing = AnnualTaxFiling.objects.filter(workpaper=workpaper).first()
        if approval:
            if approval.organization_id != org.pk:
                raise ValidationError(
                    "Persetujuan tahunan mempunyai perusahaan yang tidak sesuai."
                )
            try:
                _assert_review(
                    org,
                    workpaper,
                    {"action": "approve_annual"},
                    approval.external_review,
                )
            except (
                ObjectDoesNotExist,
                TypeError,
                ValueError,
                KeyError,
                AttributeError,
            ):
                raise ValidationError(
                    "Bukti persetujuan tahunan rusak. Periksa sumber sebelum keputusan kas diubah."
                ) from None
        if filing and (not approval or filing.organization_id != org.pk):
            raise ValidationError(
                "Bukti pelaporan tahunan tidak memiliki persetujuan asal yang dapat diperiksa."
            )
        if not isinstance(workpaper.inputs, dict):
            raise ValidationError(
                "Bukti sumber kertas kerja tahunan tidak dapat dibaca."
            )
        if _references_settlement(
            workpaper.inputs.get("tax_snapshot", {}), source.pk
        ) and (approval or filing):
            blockers.append(
                f"SPT tahunan {workpaper.year} yang disetujui/dilaporkan memakai pemeriksaan saldo awal ini. Pembetulan formal perlu ditinjau sebelum perubahan keputusan."
            )
            break
    return blockers


@transaction.atomic
def opening_cash_recovery_blockers(*, organization, settlement, date, action):
    """Fence an original-date wrong-match cancellation without changing authority.

    A received original cash report cannot be released by this first recovery
    branch, even when it records explicit zero. Eligible monthly approvals must
    be explicitly reopened while their sources are still intact. Actual refunds
    and later-date cancellations require separate supported recovery contracts.
    """
    from datetime import date as PostingDate

    from core.models import AuditEvent, Organization
    from finance.models import OpeningBalanceSettlement
    from finance.opening_balances import opening_settlement_source_snapshot

    from .models import ExternalTaxReview, OpeningSettlementTaxReview
    from .monthly_source_models import MonthlyFiscalApproval, MonthlyTaxSourcePackage
    from .workflow import digest

    org_id, settlement_id = (
        getattr(organization, "pk", organization),
        getattr(settlement, "pk", settlement),
    )
    if (
        type(org_id) is not int
        or type(settlement_id) is not int
        or type(date) is not PostingDate
    ):
        raise ValidationError(
            "Perusahaan, pencocokan kas dan tanggal pembatalan harus sah."
        )
    try:
        org = Organization.objects.select_for_update().get(pk=org_id)
        source = OpeningBalanceSettlement.objects.select_for_update().get(
            organization=org, pk=settlement_id
        )
    except (Organization.DoesNotExist, OpeningBalanceSettlement.DoesNotExist):
        raise ValidationError(
            "Pencocokan kas asli tidak tersedia untuk perusahaan ini."
        ) from None
    original = opening_settlement_source_snapshot(organization=org, settlement=source)
    if action == "refund":
        return [
            "Pengembalian uang nyata memerlukan sumber bank dan pemeriksaan pajak tersendiri; alur ini belum mendukungnya."
        ]
    if action != "cancel":
        raise ValidationError("Pilih pembatalan pencocokan kas asli yang didukung.")
    bank_snapshot = original.get("bank") if isinstance(original, dict) else None
    original_date = (
        _date_anchor(bank_snapshot.get("date"))
        if isinstance(bank_snapshot, dict)
        else None
    )
    if original_date is None or date != original_date:
        raise ValidationError(
            "Pembatalan harus berlaku tepat pada tanggal bank asli. Pembatalan bertanggal lain belum didukung."
        )

    blockers = opening_principal_correction_blockers(
        organization=org, item=source.item_id, date=date
    )
    blockers.extend(
        opening_settlement_review_correction_blockers(
            organization=org, settlement=source
        )
    )
    # Keep received authority discoverable even if a damaged live review FK no
    # longer points to the original receipt. Absence of a downstream approval
    # never releases an already received original professional report.
    authorities = OpeningSettlementTaxReview.objects.filter(
        Q(settlement_id=source.pk)
        | Q(source_snapshot__id=source.pk, source_snapshot__organization_id=org.pk)
        | Q(
            request_payload__settlement_id=source.pk,
            request_payload__organization_id=org.pk,
        )
    )
    reports = ExternalTaxReview.objects.filter(
        subject_type="openingbalancesettlement", subject_id=source.pk
    )
    # The received-report audit keeps the original subject independently of
    # the report's live fields. Its presence retains the cancellation hold
    # even if the report is missing, foreign or now points elsewhere. It is
    # evidence of received authority only, never proof of zero or withdrawal.
    received_report_audits = AuditEvent.objects.filter(
        organization=org,
        action="tax.external_review.recorded",
        object_type="ExternalTaxReview",
        detail__subject_type="openingbalancesettlement",
        detail__subject_id=source.pk,
    )
    if authorities.exists() or reports.exists() or received_report_audits.exists():
        blockers.append(
            "Laporan profesional untuk kas asli sudah diterima, termasuk hasil tanpa pemotongan. Pelepasan laporan tersebut sebelum pembatalan memerlukan alur koreksi tersendiri yang belum didukung."
        )

    # The original cash-review guard retains downstream protection. Strengthen
    # its approval release here: a newer prepared draft alone is insufficient.
    packages = list(
        MonthlyTaxSourcePackage.objects.filter(
            Q(organization=org)
            | Q(snapshot__organization_id=org.pk)
            | Q(approval__organization=org)
        )
        .distinct()
        .order_by("period", "revision")
    )
    affected = set()
    for package in packages:
        if (
            package.organization_id != org.pk
            or not isinstance(package.snapshot, dict)
            or package.snapshot.get("organization_id") != org.pk
            or digest(
                {
                    "snapshot": package.snapshot,
                    "profile": package.profile_snapshot,
                    "legal": package.legal_sources,
                }
            )
            != package.package_hash
        ):
            raise ValidationError(
                "Integritas atau perusahaan paket bulanan berubah; periksa paket asli sebelum pembatalan kas."
            )
        if _references_settlement(package.profile_snapshot, source.pk):
            affected.add(package.pk)
    for package in packages:
        if not isinstance(package.profile_snapshot, dict):
            raise ValidationError(
                "Bukti ketergantungan paket bulanan tidak dapat dibaca."
            )
        history = package.profile_snapshot.get("fiscal_history", [])
        if not isinstance(history, list) or any(
            not isinstance(row, dict) or type(row.get("package_id")) is not int
            for row in history
        ):
            raise ValidationError(
                "Riwayat ketergantungan kas pada paket bulanan perlu diperiksa."
            )
        if any(row["package_id"] in affected for row in history):
            affected.add(package.pk)
    for approval in MonthlyFiscalApproval.objects.filter(
        package_id__in=affected
    ).order_by("package__period", "pk"):
        if approval.organization_id != org.pk:
            raise ValidationError(
                "Persetujuan terkait kas asli mempunyai perusahaan yang tidak sesuai."
            )
        if not _principal_revision_released(org, approval, packages):
            blockers.append(
                f"Paket fiskal {approval.package.period:%m/%Y} memakai kas asli ini. Mulai pemeriksaan ulang secara jelas sebelum membatalkan pencocokan; persetujuan lama tetap tersimpan."
            )
    return list(dict.fromkeys(blockers))


def _references_principal(snapshot, item_id, *, annual=False):
    """Read typed principal references; an unreadable inventory is not absence."""
    if not isinstance(snapshot, dict):
        raise ValidationError(
            "Bukti ketergantungan pokok saldo lama tidak dapat dibaca."
        )
    if annual:
        dependency = snapshot.get("opening_principal_sources")
        if dependency is None:
            return False
        if not isinstance(dependency, dict):
            raise ValidationError(
                "Bukti sumber pokok pada paket tahunan perlu diperiksa."
            )
        rows = dependency.get("inventory")
    else:
        inventory = snapshot.get("inventory")
        if not isinstance(inventory, dict):
            raise ValidationError("Inventori sumber paket bulanan perlu diperiksa.")
        rows = inventory.get("opening_balances", [])
    if not isinstance(rows, list):
        raise ValidationError("Daftar sumber pokok saldo lama tidak dapat dibaca.")
    references = False
    for row in rows:
        if isinstance(row, dict) and row.get("kind") == "opening_correction_orphan":
            if type(row.get("journal_id")) is not int or row.get("intact") is not False:
                raise ValidationError(
                    "Identitas jurnal koreksi tanpa sumber tidak dapat diperiksa."
                )
            continue
        if isinstance(row, dict) and row.get("kind") == "opening_cancellation_orphan":
            identities = (
                row.get("journal_id"),
                row.get("cancellation_id"),
                row.get("audit_id"),
            )
            if (
                not any(type(value) is int and value > 0 for value in identities)
                or row.get("intact") is not False
            ):
                raise ValidationError(
                    "Identitas jurnal koreksi tanpa sumber tidak dapat diperiksa."
                )
            continue
        if isinstance(row, dict) and row.get("kind") == "opening_refund_orphan":
            identities = (
                row.get("journal_id"),
                row.get("refund_id"),
                row.get("audit_id"),
                row.get("domain_event_id"),
                row.get("bank_posting_id"),
            )
            if (
                not any(type(value) is int and value > 0 for value in identities)
                or row.get("intact") is not False
            ):
                raise ValidationError(
                    "Identitas pengembalian uang tanpa sumber tidak dapat diperiksa."
                )
            continue
        if (
            not isinstance(row, dict)
            or row.get("kind")
            not in (
                "opening_principal",
                "opening_principal_cash",
                "opening_principal_correction",
                "opening_principal_cancellation",
                "opening_principal_refund",
            )
            or type(row.get("item_id")) is not int
        ):
            raise ValidationError("Identitas sumber pokok pada paket pajak tidak sah.")
        references = references or row["item_id"] == item_id
    return references


def _principal_revision_released(organization, approval, packages):
    """Only the public, explicit review-revision event releases an approval."""
    from core.models import AuditEvent

    from .monthly_source_models import MonthlyFiscalApproval

    same_period = [row for row in packages if row.period == approval.package.period]
    if not same_period:
        raise ValidationError("Paket asal persetujuan bulanan tidak tersedia.")
    current = max(same_period, key=lambda row: (row.revision, row.pk))
    if (
        current.pk == approval.package_id
        or MonthlyFiscalApproval.objects.filter(package=current).exists()
    ):
        return False
    indexed = {row.pk: row for row in same_period}
    while current.pk != approval.package_id:
        previous = indexed.get(current.previous_id)
        if previous is None or previous.revision != current.revision - 1:
            raise ValidationError(
                "Riwayat koreksi paket bulanan terputus; periksa paket asli."
            )
        if previous.pk == approval.package_id:
            events = list(
                AuditEvent.objects.filter(
                    organization=organization,
                    action="tax.monthly_package.review_revision",
                    object_type="MonthlyTaxSourcePackage",
                    object_id=str(current.pk),
                )
            )
            if not events:
                return False
            if len(events) != 1:
                raise ValidationError(
                    "Permintaan pemeriksaan ulang paket bulanan ambigu."
                )
            event = events[0]
            detail = event.detail
            if (
                not isinstance(detail, dict)
                or event.actor_id != current.prepared_by_id
                or detail.get("previous_package_id") != previous.pk
                or detail.get("previous_approval_id") != approval.pk
                or detail.get("source_version") != previous.revision
                or detail.get("source_fingerprint") != previous.package_hash
                or not isinstance(detail.get("reason"), str)
                or len(detail["reason"].strip()) < 10
            ):
                raise ValidationError(
                    "Bukti permintaan pemeriksaan ulang tidak cocok dengan persetujuan asal."
                )
            return True
        current = previous
    return False


@transaction.atomic
def opening_principal_correction_blockers(*, organization, item, date):
    """Protect dated report uses before an append-only amount correction.

    This does not revise a report, withdraw an obligation, change a cash review,
    or rewrite earlier principal. A later effective correction cannot affect
    a report ending before that date.
    """
    from datetime import date as PostingDate

    from core.models import Organization
    from finance.models import OpeningBalanceItem, assert_open_period
    from finance.opening_balances import opening_balance_source_snapshot

    from .models import (
        AnnualTaxApproval,
        AnnualTaxFiling,
        AnnualTaxWorkpaper,
        TaxObligation,
    )
    from .monthly_source_models import MonthlyFiscalApproval, MonthlyTaxSourcePackage
    from .monthly_source_workflow import _historical_integrity
    from .workflow import _assert_review, digest

    org_id, item_id = (
        getattr(organization, "pk", organization),
        getattr(item, "pk", item),
    )
    if (
        type(org_id) is not int
        or type(item_id) is not int
        or type(date) is not PostingDate
    ):
        raise ValidationError(
            "Perusahaan, pokok saldo lama dan tanggal koreksi harus sah."
        )
    try:
        org = Organization.objects.select_for_update().get(pk=org_id)
        source = OpeningBalanceItem.objects.get(organization=org, pk=item_id)
    except (Organization.DoesNotExist, OpeningBalanceItem.DoesNotExist):
        raise ValidationError(
            "Pokok saldo lama tidak tersedia untuk perusahaan ini."
        ) from None
    opening_balance_source_snapshot(organization=org, item=source)
    if date < source.date:
        raise ValidationError("Koreksi tidak boleh mendahului tanggal saldo awal.")
    blockers = []
    try:
        assert_open_period(org.pk, date)
    except ValidationError as exc:
        blockers.extend(exc.messages)

    affected = set()
    packages = list(
        MonthlyTaxSourcePackage.objects.filter(
            Q(organization=org)
            | Q(snapshot__organization_id=org.pk)
            | Q(approval__organization=org)
        )
        .distinct()
        .order_by("period", "revision")
    )
    for package in packages:
        if (
            package.organization_id != org.pk
            or not isinstance(package.snapshot, dict)
            or package.snapshot.get("organization_id") != org.pk
            or digest(
                {
                    "snapshot": package.snapshot,
                    "profile": package.profile_snapshot,
                    "legal": package.legal_sources,
                }
            )
            != package.package_hash
        ):
            raise ValidationError(
                "Integritas atau perusahaan paket bulanan berubah; periksa paket asli sebelum koreksi pokok."
            )
        cutoff = _date_anchor(package.snapshot.get("cutoff"))
        if (
            cutoff is None
            or cutoff != package.cutoff
            or package.snapshot.get("period") != package.period.strftime("%Y-%m")
        ):
            raise ValidationError(
                "Tanggal sumber paket bulanan tidak cocok dengan periode asli."
            )
        approval = MonthlyFiscalApproval.objects.filter(package=package).first()
        if approval:
            if approval.organization_id != org.pk:
                raise ValidationError(
                    "Persetujuan paket bulanan mempunyai perusahaan yang tidak sesuai."
                )
            try:
                _historical_integrity(org, approval)
            except (
                ObjectDoesNotExist,
                TypeError,
                ValueError,
                KeyError,
                AttributeError,
            ):
                raise ValidationError(
                    "Bukti persetujuan bulanan rusak; periksa sumber sebelum koreksi pokok."
                ) from None
        references = _references_principal(package.snapshot, source.pk)
        if references and cutoff >= date:
            affected.add(package.pk)
    for package in packages:
        if not isinstance(package.profile_snapshot, dict):
            raise ValidationError(
                "Bukti ketergantungan paket bulanan tidak dapat dibaca."
            )
        history = package.profile_snapshot.get("fiscal_history", [])
        if not isinstance(history, list) or any(
            not isinstance(row, dict) or type(row.get("package_id")) is not int
            for row in history
        ):
            raise ValidationError(
                "Riwayat ketergantungan paket bulanan perlu diperiksa."
            )
        if any(row["package_id"] in affected for row in history):
            affected.add(package.pk)
    affected_periods = {row.period for row in packages if row.pk in affected}
    obligations = TaxObligation.objects.filter(
        Q(source_package_id__in=affected)
        | Q(organization=org, tax_type="final_turnover", period__in=affected_periods)
    ).exclude(status="withdrawn")
    for obligation in obligations:
        if obligation.organization_id != org.pk:
            raise ValidationError(
                "Kewajiban terkait paket asal mempunyai perusahaan yang tidak sesuai."
            )
        blockers.append(
            f"Kewajiban pajak {obligation.period:%m/%Y} memakai sumber pokok ini. Periksa dan koreksi kewajiban tersebut terlebih dahulu; dokumen lama tetap tersimpan."
        )
    for approval in MonthlyFiscalApproval.objects.filter(
        package_id__in=affected
    ).order_by("package__period", "pk"):
        if approval.organization_id != org.pk:
            raise ValidationError(
                "Persetujuan terkait pokok mempunyai perusahaan yang tidak sesuai."
            )
        if not _principal_revision_released(org, approval, packages):
            blockers.append(
                f"Paket fiskal {approval.package.period:%m/%Y} sudah disetujui dan memakai pokok ini. Mulai pemeriksaan ulang paket secara jelas sebelum mencatat koreksi pokok."
            )

    workpapers = (
        AnnualTaxWorkpaper.objects.filter(
            Q(organization=org)
            | Q(approval__organization=org)
            | Q(filing__organization=org)
        )
        .distinct()
        .order_by("year", "pk")
    )
    for workpaper in workpapers:
        if (
            workpaper.organization_id != org.pk
            or not isinstance(workpaper.inputs, dict)
            or not isinstance(workpaper.book_snapshot, dict)
        ):
            raise ValidationError(
                "Bukti sumber atau perusahaan kertas kerja tahunan tidak dapat diperiksa."
            )
        approval = AnnualTaxApproval.objects.filter(workpaper=workpaper).first()
        filing = AnnualTaxFiling.objects.filter(workpaper=workpaper).first()
        if approval:
            if approval.organization_id != org.pk:
                raise ValidationError(
                    "Persetujuan tahunan mempunyai perusahaan yang tidak sesuai."
                )
            try:
                _assert_review(
                    org,
                    workpaper,
                    {"action": "approve_annual"},
                    approval.external_review,
                )
            except (
                ObjectDoesNotExist,
                TypeError,
                ValueError,
                KeyError,
                AttributeError,
            ):
                raise ValidationError(
                    "Bukti persetujuan tahunan rusak; periksa sumber sebelum koreksi pokok."
                ) from None
        if filing and (not approval or filing.organization_id != org.pk):
            raise ValidationError(
                "Bukti pelaporan tahunan tidak memiliki persetujuan asal yang dapat diperiksa."
            )
        references = _references_principal(
            workpaper.inputs.get("tax_snapshot", {}), source.pk, annual=True
        )
        if references:
            cutoff = _date_anchor(workpaper.book_snapshot.get("fiscal_end"))
            if cutoff is None:
                raise ValidationError(
                    "Tanggal akhir sumber tahunan tidak dapat diperiksa."
                )
            if cutoff >= date and (approval or filing):
                blockers.append(
                    f"SPT tahunan {workpaper.year} yang disetujui/dilaporkan memakai pokok ini. Pembetulan formal perlu ditinjau sebelum koreksi pokok."
                )
    return list(dict.fromkeys(blockers))


def opening_balance_report_dependency(*, organization, cutoff):
    """Separate professional decisions from commercial income-source hashes."""
    from .opening_balance_review import opening_balance_tax_gates

    result = opening_balance_tax_gates(organization=organization, cutoff=cutoff)
    dependency = {
        "fingerprint": result["fingerprint"],
        "inventory": result["inventory"],
    }
    gates = [
        {
            "code": "opening_balance_review",
            "title": "Periksa pajak kas saldo awal",
            "detail": message,
            "severity": "review",
        }
        for message in result["blockers"]
    ]
    candidates = [
        {
            "settlement_id": row["settlement_id"],
            "item_id": row["item_id"],
            "date": date.fromisoformat(row["date"]),
            "status": row["status"],
            "reason": "Pembayaran asli perlu dicocokkan dengan laporan profesional. Sisa saldo awal nol tidak menutup pemeriksaan pajak.",
        }
        for row in result["inventory"]
        if row["status"] != "reviewed"
    ]
    return dependency, gates, candidates


def _date_anchor(value):
    if type(value) is date:
        return value
    if type(value) is str:
        try:
            parsed = date.fromisoformat(value)
            if parsed.isoformat() == value:
                return parsed
        except ValueError:
            return None
    return None


def _original_scope_dates(row, *, cash):
    """Current joins cannot erase an original dated receipt from its period.

    These are selection anchors, never a replacement for validated proof. A
    malformed required anchor stays in scope so the normal source validator
    can hold it instead of treating a failed receipt as absent.
    """
    values = [row.date]
    try:
        values.append(row.journal.date)
    except ObjectDoesNotExist:
        values.append(None)
    request = row.request_payload if isinstance(row.request_payload, dict) else {}
    saved = row.source_snapshot if isinstance(row.source_snapshot, dict) else {}
    original_request = (
        saved.get("request_payload")
        if isinstance(saved.get("request_payload"), dict)
        else {}
    )
    original_journal = (
        saved.get("journal") if isinstance(saved.get("journal"), dict) else {}
    )
    values.extend(
        (
            request.get("date"),
            saved.get("date"),
            original_request.get("date"),
            original_journal.get("date"),
        )
    )
    if cash:
        try:
            values.extend((row.bank_posting.date, row.bank_posting.transaction.date))
        except ObjectDoesNotExist:
            values.append(None)
        original_bank = saved.get("bank") if isinstance(saved.get("bank"), dict) else {}
        original_posting = (
            saved.get("bank_posting")
            if isinstance(saved.get("bank_posting"), dict)
            else {}
        )
        values.extend((original_bank.get("date"), original_posting.get("date")))
    return [_date_anchor(value) for value in values]


def _orphan_correction_journals(organization, corrections):
    """Unbound typed journals cannot disappear behind an empty row inventory.

    A valid future correction has its own sealed dated receipt and is projected
    separately. An orphan has no such receipt proving a later effective date;
    retain it as opaque failed integrity, never as principal, cash or turnover.
    """
    from core.models import AuditEvent
    from finance.models import Journal, JournalLine

    from .workflow import digest

    linked = {row.journal_id for row in corrections}
    for row in corrections:
        sealed = row.source_snapshot if isinstance(row.source_snapshot, dict) else {}
        journal = (
            sealed.get("journal") if isinstance(sealed.get("journal"), dict) else {}
        )
        if type(journal.get("id")) is int:
            linked.add(journal["id"])
    posted = AuditEvent.objects.filter(
        organization=organization,
        action="finance.journal.posted",
        object_type="Journal",
    ).values_list("object_id", flat=True)
    original_ids = {
        int(value)
        for value in posted
        if isinstance(value, str) and value.isdecimal() and int(value) > 0
    }
    scoped_lines = JournalLine.objects.filter(organization=organization).values_list(
        "journal_id", flat=True
    )
    journals = (
        Journal.objects.filter(source_key__startswith="opening:opening-correct:")
        .filter(
            Q(organization=organization)
            | Q(pk__in=scoped_lines)
            | Q(pk__in=linked | original_ids)
        )
        .order_by("pk")
    )
    result = []
    for journal in journals:
        if journal.pk in linked:
            continue
        fields = {
            field.attname: getattr(journal, field.attname)
            for field in journal._meta.fields
        }
        lines = list(
            JournalLine.objects.filter(journal_id=journal.pk).order_by("pk").values()
        )
        audits = list(
            AuditEvent.objects.filter(
                action="finance.journal.posted",
                object_type="Journal",
                object_id=str(journal.pk),
            )
            .order_by("pk")
            .values()
        )
        graph = {
            "journal_id": journal.pk,
            "journal_hash": digest(fields),
            "journal_lines_hash": digest(lines),
            "journal_line_count": len(lines),
            "posting_audit_hash": digest(audits),
        }
        result.append(
            {
                "kind": "opening_correction_orphan",
                "journal_id": journal.pk,
                "source": graph,
                "intact": False,
            }
        )
    return result


def _cancellation_audits(organization):
    from core.models import AuditEvent

    return list(
        AuditEvent.objects.filter(
            organization=organization,
            action="finance.opening_balance.cancel",
            object_type="OpeningBalanceCancellation",
        ).order_by("pk")
    )


def _orphan_cancellation_sources(organization, cancellations, audits):
    """Retain typed journals and original audited decisions without live rows."""
    from core.models import AuditEvent
    from finance.models import Journal, JournalLine
    from finance.opening_balances import opening_settlement_source_graph

    from .workflow import digest

    linked = {row.journal_id for row in cancellations}
    for row in cancellations:
        sealed = row.source_snapshot if isinstance(row.source_snapshot, dict) else {}
        journal = (
            sealed.get("journal") if isinstance(sealed.get("journal"), dict) else {}
        )
        if type(journal.get("id")) is int:
            linked.add(journal["id"])
    original_journals = {
        event.detail["journal_id"]
        for event in audits
        if isinstance(event.detail, dict)
        and type(event.detail.get("journal_id")) is int
    }
    posted = AuditEvent.objects.filter(
        organization=organization,
        action="finance.journal.posted",
        object_type="Journal",
    ).values_list("object_id", flat=True)
    posted_ids = {
        int(value)
        for value in posted
        if isinstance(value, str) and value.isdecimal() and int(value) > 0
    }
    lines = JournalLine.objects.filter(organization=organization).values_list(
        "journal_id", flat=True
    )
    journals = Journal.objects.filter(
        Q(source_key__startswith="opening:opening-cancel:")
        & (
            Q(organization=organization)
            | Q(pk__in=lines)
            | Q(pk__in=linked | posted_ids)
        )
        | Q(pk__in=original_journals)
    ).order_by("pk")
    result = []

    def cash_anchors(events):
        identities = sorted(
            {
                event["detail"]["settlement_id"]
                for event in events
                if isinstance(event.get("detail"), dict)
                and type(event["detail"].get("settlement_id")) is int
                and event["detail"]["settlement_id"] > 0
            }
        )
        return [
            {
                "settlement_id": identity,
                "graph": opening_settlement_source_graph(
                    organization=organization, settlement=identity
                ),
            }
            for identity in identities
        ]

    for journal in journals:
        if journal.pk in linked:
            continue
        fields = {
            field.attname: getattr(journal, field.attname)
            for field in journal._meta.fields
        }
        journal_lines = list(
            JournalLine.objects.filter(journal_id=journal.pk).order_by("pk").values()
        )
        authority = list(
            AuditEvent.objects.filter(
                Q(
                    action="finance.opening_balance.cancel",
                    detail__journal_id=journal.pk,
                )
                | Q(
                    action="finance.journal.posted",
                    object_type="Journal",
                    object_id=str(journal.pk),
                )
            )
            .order_by("pk")
            .values()
        )
        result.append(
            {
                "kind": "opening_cancellation_orphan",
                "journal_id": journal.pk,
                "intact": False,
                "source": {
                    "journal_id": journal.pk,
                    "journal_hash": digest(fields),
                    "journal_lines_hash": digest(journal_lines),
                    "journal_line_count": len(journal_lines),
                    "posting_audit_hash": digest(authority),
                    "original_cash_graphs": cash_anchors(authority),
                },
            }
        )
    existing = {row.pk for row in cancellations}
    for event in audits:
        identity = (
            int(event.object_id)
            if isinstance(event.object_id, str) and event.object_id.isdecimal()
            else None
        )
        if identity in existing:
            continue
        event_fields = {
            field.attname: getattr(event, field.attname) for field in event._meta.fields
        }
        result.append(
            {
                "kind": "opening_cancellation_orphan",
                "cancellation_id": identity,
                "audit_id": event.pk,
                "intact": False,
                "source": {
                    "audit_hash": digest(event_fields),
                    "missing_cancellation_id": identity,
                    "original_cash_graphs": cash_anchors([event_fields]),
                },
            }
        )
    return result


@transaction.atomic
def opening_balance_source_inventory(*, organization, cutoff):
    """Select original principal/cash by every durable date before validation.

    Original receipts made after the cutoff remain excluded. Damaged receipt
    dates cannot make a source disappear from monthly/annual review queues.
    """
    from core.models import Organization
    from finance.models import (
        OpeningBalanceCancellation,
        OpeningBalanceCorrection,
        OpeningBalanceItem,
        OpeningBalanceSettlement,
    )
    from finance.opening_balances import opening_cancellation_source_snapshot

    org_id = getattr(organization, "pk", organization)
    if type(org_id) is not int or type(cutoff) is not date:
        raise ValidationError(
            "Perusahaan dan tanggal batas sumber saldo awal harus sah."
        )
    try:
        org = Organization.objects.select_for_update().get(pk=org_id)
    except Organization.DoesNotExist:
        raise ValidationError("Perusahaan tidak tersedia.") from None
    items = OpeningBalanceItem.objects.filter(
        Q(organization=org)
        | Q(journal__organization=org)
        | Q(source_snapshot__organization_id=org.pk)
    ).order_by("pk")
    settlements = OpeningBalanceSettlement.objects.filter(
        Q(organization=org)
        | Q(item__organization=org)
        | Q(bank_posting__organization=org)
        | Q(journal__organization=org)
        | Q(source_snapshot__organization_id=org.pk)
    ).order_by("date", "pk")
    corrections = list(
        OpeningBalanceCorrection.objects.filter(
            Q(organization=org)
            | Q(item__organization=org)
            | Q(journal__organization=org)
            | Q(source_snapshot__organization_id=org.pk)
        ).order_by("date", "pk")
    )
    audits = _cancellation_audits(org)
    original_ids = {
        int(event.object_id)
        for event in audits
        if isinstance(event.object_id, str)
        and event.object_id.isdecimal()
        and int(event.object_id) > 0
    }
    cancellations = list(
        OpeningBalanceCancellation.objects.filter(
            Q(organization=org)
            | Q(item__organization=org)
            | Q(settlement__organization=org)
            | Q(settlement__bank_posting__organization=org)
            | Q(settlement__bank_posting__transaction__organization=org)
            | Q(journal__organization=org)
            | Q(journal__lines__organization=org)
            | Q(source_snapshot__organization_id=org.pk)
            | Q(pk__in=original_ids)
        )
        .distinct()
        .order_by("date", "pk")
    )
    selected = {"items": [], "settlements": [], "corrections": [], "cancellations": []}
    for rows, name, cash in (
        (items, "items", False),
        (settlements, "settlements", True),
        (corrections, "corrections", False),
    ):
        for row in rows:
            anchors = _original_scope_dates(row, cash=cash)
            if any(day is None or day <= cutoff for day in anchors):
                selected[name].append(row)
    selected["orphan_correction_journals"] = _orphan_correction_journals(
        org, corrections
    )
    for row in cancellations:
        # Integrity is checked before temporal projection. Moving every live
        # date forward cannot make an original failed receipt disappear.
        try:
            opening_cancellation_source_snapshot(organization=org, cancellation=row)
        except ValidationError:
            selected["cancellations"].append(row)
            continue
        anchors = _original_scope_dates(row, cash=False)
        try:
            anchors.extend(_original_scope_dates(row.settlement, cash=True))
        except ObjectDoesNotExist:
            anchors.append(None)
        if any(day is None or day <= cutoff for day in anchors):
            selected["cancellations"].append(row)
    selected["orphan_cancellation_sources"] = _orphan_cancellation_sources(
        org, cancellations, audits
    )
    return selected


@transaction.atomic
def opening_cash_recovery_metadata(*, organization, settlement, cutoff=None):
    """Conditional, validated lineage; no classification or implied gross."""
    from finance.models import OpeningBalanceSettlement
    from finance.opening_balances import opening_cancellation_source_snapshot

    org_id, settlement_id = (
        getattr(organization, "pk", organization),
        getattr(settlement, "pk", settlement),
    )
    if type(org_id) is not int or type(settlement_id) is not int:
        raise ValidationError("Perusahaan dan pencocokan kas harus sah.")
    try:
        source = OpeningBalanceSettlement.objects.get(
            organization_id=org_id, pk=settlement_id
        )
    except OpeningBalanceSettlement.DoesNotExist:
        raise ValidationError(
            "Pencocokan kas asli tidak tersedia untuk perusahaan ini."
        ) from None
    inventory = opening_balance_source_inventory(
        organization=organization, cutoff=cutoff or date.max
    )
    if inventory["orphan_cancellation_sources"]:
        raise ValidationError(
            "Bukti pembatalan pencocokan terputus; periksa keputusan dan jurnal asli."
        )
    result = {}
    for row in inventory["cancellations"]:
        proof = opening_cancellation_source_snapshot(
            organization=organization, cancellation=row
        )
        if proof["transaction_id"] != source.bank_posting.transaction_id:
            continue
        result["cash_lineage"] = {
            "kind": "bank_transaction",
            "transaction_id": proof["transaction_id"],
        }
        if proof["settlement_id"] == source.pk:
            if "opening_cancellation" in result:
                raise ValidationError("Riwayat pembatalan pencocokan asli ambigu.")
            result["opening_cancellation"] = proof
    return result


@transaction.atomic
def opening_balance_financial_dependency(*, organization, cutoff):
    """Bind opening principal even when no cash review exists yet.

    This is commercial integrity, separate from professional decisions. It
    preserves a failed source's opaque graph rather than supplying fake zero.
    """
    from core.models import Organization
    from finance.opening_balances import (
        opening_balance_source_graph,
        opening_balance_source_snapshot,
        opening_cancellation_source_graph,
        opening_cancellation_source_snapshot,
        opening_correction_source_graph,
        opening_correction_source_snapshot,
        opening_settlement_source_graph,
        opening_settlement_source_snapshot,
    )

    from .workflow import digest, json_value

    org_id = getattr(organization, "pk", organization)
    if type(org_id) is not int or type(cutoff) is not date:
        raise ValidationError(
            "Perusahaan dan tanggal batas sumber saldo awal harus sah."
        )
    try:
        org = Organization.objects.select_for_update().get(pk=org_id)
    except Organization.DoesNotExist:
        raise ValidationError("Perusahaan tidak tersedia.") from None
    inventory, blockers = [], []
    selected = opening_balance_source_inventory(organization=org, cutoff=cutoff)
    for item in selected["items"]:
        try:
            source = opening_balance_source_snapshot(organization=org, item=item)
            intact = True
        except ValidationError as exc:
            source = opening_balance_source_graph(organization=org, item=item)
            intact = False
            blockers.extend(exc.messages)
        inventory.append(
            {
                "kind": "opening_principal",
                "item_id": item.pk,
                "source": source,
                "intact": intact,
            }
        )
    for settlement in selected["settlements"]:
        try:
            source = opening_settlement_source_snapshot(
                organization=org, settlement=settlement
            )
            intact = True
        except ValidationError as exc:
            source = opening_settlement_source_graph(
                organization=org, settlement=settlement
            )
            intact = False
            blockers.extend(exc.messages)
        inventory.append(
            {
                "kind": "opening_principal_cash",
                "settlement_id": settlement.pk,
                "item_id": settlement.item_id,
                "source": source,
                "intact": intact,
            }
        )
    for correction in selected["corrections"]:
        try:
            source = opening_correction_source_snapshot(
                organization=org, correction=correction
            )
            intact = True
        except ValidationError as exc:
            source = opening_correction_source_graph(
                organization=org, correction=correction
            )
            intact = False
            blockers.extend(exc.messages)
        inventory.append(
            {
                "kind": "opening_principal_correction",
                "correction_id": correction.pk,
                "item_id": correction.item_id,
                "date": correction.date.isoformat(),
                "direction": correction.direction,
                "amount": format(correction.amount, ".2f"),
                "source": source,
                "intact": intact,
            }
        )
    if selected["orphan_correction_journals"]:
        inventory.extend(selected["orphan_correction_journals"])
        blockers.append(
            "Ada jurnal koreksi saldo lama tanpa catatan keputusan dan bukti sumber. Periksa jurnal asli; nilainya belum dapat dinilai."
        )
    for cancellation in selected["cancellations"]:
        try:
            source = opening_cancellation_source_snapshot(
                organization=org, cancellation=cancellation
            )
            intact = True
        except ValidationError as exc:
            source = opening_cancellation_source_graph(
                organization=org, cancellation=cancellation
            )
            intact = False
            blockers.extend(exc.messages)
        observed_date = _date_anchor(cancellation.date)
        inventory.append(
            {
                "kind": "opening_principal_cancellation",
                "cancellation_id": cancellation.pk,
                "item_id": cancellation.item_id,
                "settlement_id": cancellation.settlement_id,
                "date": observed_date.isoformat() if observed_date else None,
                "amount": format(cancellation.amount, ".2f") if intact else None,
                "source": source,
                "intact": intact,
            }
        )
    if selected["orphan_cancellation_sources"]:
        inventory.extend(selected["orphan_cancellation_sources"])
        blockers.append(
            "Ada pembatalan pencocokan tanpa keputusan atau jurnal asli yang utuh. Nilai sumber belum dapat dinilai."
        )
    from .opening_refund_review_dependencies import opening_refund_financial_dependency

    refunds = opening_refund_financial_dependency(organization=org, cutoff=cutoff)
    if refunds["inventory"] or refunds["blockers"]:
        inventory.extend(refunds["inventory"])
        blockers.extend(refunds["blockers"])
    snapshot = json_value(
        {"organization_id": org.pk, "cutoff": cutoff, "inventory": inventory}
    )
    return {
        "fingerprint": digest(snapshot),
        "inventory": inventory,
        "blockers": list(dict.fromkeys(blockers)),
    }
