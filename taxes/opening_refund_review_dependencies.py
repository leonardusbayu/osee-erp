"""Typed fiscal boundaries for genuine refunds of original opening cash.

This record guard does not approve a refund's tax treatment or change the
received professional authority for its original payment.
"""

from datetime import date as PostingDate

from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import transaction
from django.db.models import Q


def _original_review_blockers(organization, settlement):
    """Retain intact original authority; damaged or unresolved evidence holds."""
    from core.models import AuditEvent

    from .models import ExternalTaxReview, OpeningSettlementTaxReview
    from .opening_balance_review import (
        _report_snapshot,
        opening_settlement_review_context,
        validate_review_record,
    )

    context = opening_settlement_review_context(
        organization=organization, settlement=settlement
    )
    if context["source_snapshot"] is None:
        return context["blockers"]
    blockers = []
    authorities = list(
        OpeningSettlementTaxReview.objects.filter(
            Q(settlement_id=settlement.pk)
            | Q(
                source_snapshot__id=settlement.pk,
                source_snapshot__organization_id=organization.pk,
            )
            | Q(
                request_payload__settlement_id=settlement.pk,
                request_payload__organization_id=organization.pk,
            )
        ).order_by("version", "pk")
    )
    for authority in authorities:
        try:
            if (
                authority.organization_id != organization.pk
                or authority.settlement_id != settlement.pk
            ):
                raise ValidationError(
                    "Hubungan keputusan profesional asli berubah; periksa riwayat sebelum mencocokkan refund."
                )
            validate_review_record(authority)
        except (ValidationError, ObjectDoesNotExist, TypeError, ValueError, KeyError):
            blockers.append(
                "Bukti keputusan profesional pembayaran asli tidak utuh; periksa laporan asal sebelum mencocokkan refund."
            )
    if authorities and context["status"] != "reviewed":
        blockers.extend(context["blockers"])

    # An original received report remains discoverable independently of its
    # mutable live subject/FK. Its audit proves receipt, not a refund decision.
    audits = list(
        AuditEvent.objects.filter(
            organization=organization,
            action="tax.external_review.recorded",
            object_type="ExternalTaxReview",
            detail__subject_type="openingbalancesettlement",
            detail__subject_id=settlement.pk,
        )
    )
    report_ids = set(
        ExternalTaxReview.objects.filter(
            subject_type="openingbalancesettlement", subject_id=settlement.pk
        ).values_list("pk", flat=True)
    )
    report_ids.update(authority.external_review_id for authority in authorities)
    for audit in audits:
        identity = audit.object_id
        if (
            not isinstance(identity, str)
            or not identity.isdecimal()
            or int(identity) <= 0
        ):
            blockers.append(
                "Identitas penerimaan laporan asli tidak dapat diperiksa; refund masih ditahan."
            )
        else:
            report_ids.add(int(identity))
    for report_id in sorted(report_ids):
        try:
            report = ExternalTaxReview.objects.get(pk=report_id)
            matching = [
                authority
                for authority in authorities
                if authority.external_review_id == report_id
            ]
            if len(matching) != 1:
                raise ValidationError(
                    "Laporan kas asli belum memiliki keputusan yang dicocokkan dalam riwayat profesional."
                )
            authority = matching[0]
            validate_review_record(authority)
            _report_snapshot(report)
            if report.reviewed_on < settlement.date:
                raise ValidationError("Tanggal laporan mendahului kas asli.")
            receipts = [audit for audit in audits if audit.object_id == str(report.pk)]
            if (
                len(receipts) != 1
                or receipts[0].actor_id != report.recorded_by_id
                or receipts[0].detail.get("evidence_sha256") != report.evidence_sha256
            ):
                raise ValidationError(
                    "Penerimaan laporan tidak cocok dengan bukti asli."
                )
            decision_receipts = list(
                AuditEvent.objects.filter(
                    organization=organization,
                    action="tax.opening_balance_review.recorded",
                    object_type="OpeningSettlementTaxReview",
                    object_id=str(authority.pk),
                )
            )
            expected_detail = {
                "settlement_id": settlement.pk,
                "version": authority.version,
                "external_review_id": report.pk,
                "evidence_sha256": report.evidence_sha256,
                "source_fingerprint": authority.source_fingerprint,
                "resolution": authority.decision["resolution"],
                "scope": "original_cash_event",
                "credentials_independently_verified": False,
            }
            if (
                len(decision_receipts) != 1
                or decision_receipts[0].actor_id != authority.actor_id
                or decision_receipts[0].detail != expected_detail
            ):
                raise ValidationError(
                    "Riwayat penerimaan keputusan pembayaran asli tidak cocok."
                )
            # Every historical report must remain intact against its own sealed
            # subject. Only the current typed conclusion determines whether the
            # original evidence request has been resolved; an old request is
            # neither the current verdict nor authority for this new refund.
        except (
            ExternalTaxReview.DoesNotExist,
            ValidationError,
            ObjectDoesNotExist,
            TypeError,
            ValueError,
            KeyError,
        ):
            blockers.append(
                "Laporan profesional pembayaran asli hilang, berubah atau tidak cocok; periksa bukti asli sebelum mencocokkan refund."
            )
    return list(dict.fromkeys(blockers))


@transaction.atomic
def opening_refund_record_blockers(
    *, organization, settlement, transaction, date, amount
):
    """Protect the actual new-bank date without withdrawing original authority.

    Original pending review remains pending. An intact received zero report
    can remain in place, but never supplies a refund-specific tax conclusion.
    Finance separately validates shared claims and every-date conservation.
    """
    from core.models import Organization
    from finance import services as ledger
    from finance.models import (
        BankTransaction,
        OpeningBalanceCancellation,
        OpeningBalanceSettlement,
    )
    from finance.opening_balances import opening_settlement_source_snapshot

    from .opening_balance_review_dependencies import (
        opening_principal_correction_blockers,
    )

    org_id = getattr(organization, "pk", organization)
    settlement_id = getattr(settlement, "pk", settlement)
    transaction_id = getattr(transaction, "pk", transaction)
    if (
        any(
            type(identity) is not int or identity <= 0
            for identity in (org_id, settlement_id, transaction_id)
        )
        or type(date) is not PostingDate
    ):
        raise ValidationError(
            "Pilih perusahaan, pembayaran asli, mutasi refund dan tanggal bank yang sah."
        )
    try:
        org = Organization.objects.select_for_update().get(pk=org_id)
        source = OpeningBalanceSettlement.objects.select_for_update().get(
            organization=org, pk=settlement_id
        )
        bank = (
            BankTransaction.objects.select_for_update()
            .select_related("account")
            .get(organization=org, pk=transaction_id)
        )
    except (
        Organization.DoesNotExist,
        OpeningBalanceSettlement.DoesNotExist,
        BankTransaction.DoesNotExist,
    ):
        raise ValidationError(
            "Pembayaran atau mutasi refund tidak tersedia untuk perusahaan ini."
        ) from None
    original = opening_settlement_source_snapshot(organization=org, settlement=source)
    day, value = ledger._actual_date(date), ledger._amount(amount)
    bank_amount = ledger._amount(bank.amount, positive=False)
    original_day = PostingDate.fromisoformat(original["bank"]["date"])
    expected_sign = -1 if source.item.kind == "receivable" else 1
    if (
        bank.account.organization_id != org.pk
        or bank.pk == original["bank"]["id"]
        or bank.date != day
        or day < original_day
        or bank_amount * expected_sign <= 0
        or value > abs(bank_amount)
    ):
        raise ValidationError(
            "Refund harus memakai mutasi baru berlawanan arah, jumlah nyata dan tanggal bank setelah pembayaran asli."
        )
    if OpeningBalanceCancellation.objects.filter(settlement_id=source.pk).exists():
        raise ValidationError(
            "Pencocokan yang dibatalkan tidak dapat menjadi sumber refund uang nyata."
        )
    blockers = opening_principal_correction_blockers(
        organization=org, item=source.item_id, date=day
    )
    blockers.extend(_original_review_blockers(org, source))
    return list(dict.fromkeys(blockers))


@transaction.atomic
def opening_refund_source_inventory(*, organization, cutoff):
    """Dated refund receipts and opaque orphan anchors, never a fiscal amount.

    Valid later refunds stay outside an earlier report. Broken receipts are
    retained before temporal projection; moving live dates/FKs cannot hide an
    original dated source or turn it into an empty, supposedly nil period.
    """
    from core.models import AuditEvent, DomainEvent, Organization
    from finance.models import (
        BankAccount,
        BankPosting,
        BankTransaction,
        Journal,
        JournalLine,
        OpeningBalanceRefund,
    )
    from finance.opening_balances import (
        opening_refund_source_graph,
        opening_refund_source_snapshot,
    )

    from .opening_balance_review_dependencies import _original_scope_dates
    from .workflow import digest

    org_id = getattr(organization, "pk", organization)
    if type(org_id) is not int or org_id <= 0 or type(cutoff) is not PostingDate:
        raise ValidationError(
            "Perusahaan dan tanggal batas pengembalian uang harus sah."
        )
    try:
        org = Organization.objects.select_for_update().get(pk=org_id)
    except Organization.DoesNotExist:
        raise ValidationError("Perusahaan tidak tersedia.") from None
    audits = list(
        AuditEvent.objects.filter(
            organization=org,
            action="finance.opening_balance.refund",
            object_type="OpeningBalanceRefund",
        ).order_by("pk")
    )
    events = list(
        DomainEvent.objects.filter(
            organization=org,
            topic="opening_balance.refund",
            aggregate_type="finance.openingbalancerefund",
        ).order_by("pk")
    )
    audited_ids = {
        int(row.object_id)
        for row in audits
        if isinstance(row.object_id, str)
        and row.object_id.isdecimal()
        and int(row.object_id) > 0
    }
    audited_ids.update(
        int(row.aggregate_id)
        for row in events
        if isinstance(row.aggregate_id, str)
        and row.aggregate_id.isdecimal()
        and int(row.aggregate_id) > 0
    )
    rows = list(
        OpeningBalanceRefund.objects.filter(
            Q(organization=org)
            | Q(item__organization=org)
            | Q(settlement__organization=org)
            | Q(settlement__bank_posting__organization=org)
            | Q(bank_posting__organization=org)
            | Q(bank_posting__transaction__organization=org)
            | Q(journal__organization=org)
            | Q(journal__lines__organization=org)
            | Q(source_snapshot__organization_id=org.pk)
            | Q(request_payload__organization_id=org.pk)
            | Q(pk__in=audited_ids)
        )
        .distinct()
        .order_by("date", "pk")
    )
    selected, orphans = [], []
    for row in rows:
        try:
            opening_refund_source_snapshot(organization=org, refund=row)
        except (ValidationError, ObjectDoesNotExist, TypeError, ValueError, KeyError):
            selected.append(row)
            continue
        if any(
            day is None or day <= cutoff
            for day in _original_scope_dates(row, cash=True)
        ):
            selected.append(row)

    linked = {row.journal_id for row in rows}
    for row in rows:
        saved = row.source_snapshot if isinstance(row.source_snapshot, dict) else {}
        original = (
            saved.get("journal") if isinstance(saved.get("journal"), dict) else {}
        )
        if type(original.get("id")) is int:
            linked.add(original["id"])
    audited_journals = {
        row.detail["journal_id"]
        for row in audits
        if isinstance(row.detail, dict) and type(row.detail.get("journal_id")) is int
    }
    audited_journals.update(
        row.payload["journal_id"]
        for row in events
        if isinstance(row.payload, dict) and type(row.payload.get("journal_id")) is int
    )
    posting_ids = {
        int(value)
        for value in AuditEvent.objects.filter(
            organization=org, action="finance.journal.posted", object_type="Journal"
        ).values_list("object_id", flat=True)
        if isinstance(value, str) and value.isdecimal() and int(value) > 0
    }
    scoped_lines = JournalLine.objects.filter(organization=org).values_list(
        "journal_id", flat=True
    )
    journals = Journal.objects.filter(
        (
            Q(source_key__startswith="opening:opening-refund:")
            | Q(source_key__startswith="bank-event:opening-refund:")
        )
        & (
            Q(organization=org)
            | Q(pk__in=scoped_lines)
            | Q(pk__in=linked | posting_ids)
        )
        | Q(pk__in=audited_journals)
    ).order_by("pk")

    def audit_graph(events):
        identities = {
            int(event["object_id"])
            for event in events
            if event.get("action") == "finance.opening_balance.refund"
            and isinstance(event.get("object_id"), str)
            and event["object_id"].isdecimal()
            and int(event["object_id"]) > 0
        }
        return [
            {
                "refund_id": identity,
                "graph": opening_refund_source_graph(organization=org, refund=identity),
            }
            for identity in sorted(identities)
        ]

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
        authority = list(
            AuditEvent.objects.filter(
                Q(
                    action="finance.opening_balance.refund",
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
        orphans.append(
            {
                "kind": "opening_refund_orphan",
                "journal_id": journal.pk,
                "intact": False,
                "source": {
                    "journal_hash": digest(fields),
                    "journal_lines_hash": digest(lines),
                    "journal_line_count": len(lines),
                    "posting_audit_hash": digest(authority),
                    "refund_graphs": audit_graph(authority),
                },
            }
        )
    existing = {row.pk for row in rows}
    for audit in audits:
        identity = (
            int(audit.object_id)
            if isinstance(audit.object_id, str) and audit.object_id.isdecimal()
            else None
        )
        if identity in existing:
            continue
        fields = {
            field.attname: getattr(audit, field.attname) for field in audit._meta.fields
        }
        orphans.append(
            {
                "kind": "opening_refund_orphan",
                "refund_id": identity,
                "audit_id": audit.pk,
                "intact": False,
                "source": {
                    "audit_hash": digest(fields),
                    "refund_graphs": audit_graph([fields]),
                },
            }
        )
    for event in events:
        identity = (
            int(event.aggregate_id)
            if isinstance(event.aggregate_id, str) and event.aggregate_id.isdecimal()
            else None
        )
        if identity in existing:
            continue
        fields = {
            field.attname: getattr(event, field.attname) for field in event._meta.fields
        }
        orphans.append(
            {
                "kind": "opening_refund_orphan",
                "refund_id": identity,
                "domain_event_id": event.pk,
                "intact": False,
                "source": {
                    "domain_event_hash": digest(fields),
                    "refund_graph": opening_refund_source_graph(
                        organization=org, refund=identity
                    )
                    if identity and identity > 0
                    else None,
                },
            }
        )
    linked_postings = {row.bank_posting_id for row in rows}
    for row in rows:
        saved = row.source_snapshot if isinstance(row.source_snapshot, dict) else {}
        posting = (
            saved.get("bank_posting")
            if isinstance(saved.get("bank_posting"), dict)
            else {}
        )
        if type(posting.get("id")) is int:
            linked_postings.add(posting["id"])
    postings = BankPosting.objects.filter(
        Q(organization=org)
        | Q(transaction__organization=org)
        | Q(journal__organization=org),
        kind__in=["opening_receipt_refund", "opening_payment_refund"],
    ).order_by("pk")
    for posting in postings:
        if posting.pk in linked_postings:
            continue
        bank = (
            BankTransaction.objects.filter(pk=posting.transaction_id).values().first()
        )
        account = (
            BankAccount.objects.filter(pk=bank["account_id"]).values().first()
            if bank
            else None
        )
        fields = {
            field.attname: getattr(posting, field.attname)
            for field in posting._meta.fields
        }
        journal = Journal.objects.filter(pk=posting.journal_id).values().first()
        lines = list(
            JournalLine.objects.filter(journal_id=posting.journal_id)
            .order_by("pk")
            .values()
        )
        orphans.append(
            {
                "kind": "opening_refund_orphan",
                "bank_posting_id": posting.pk,
                "journal_id": posting.journal_id,
                "intact": False,
                "source": {
                    "posting_hash": digest(fields),
                    "bank_hash": digest(bank),
                    "account_hash": digest(account),
                    "journal_hash": digest(journal),
                    "journal_lines_hash": digest(lines),
                },
            }
        )
    return {"refunds": selected, "orphans": orphans}


@transaction.atomic
def opening_refund_financial_dependency(*, organization, cutoff):
    from finance.opening_balances import (
        opening_refund_source_graph,
        opening_refund_source_snapshot,
    )

    from .workflow import digest, json_value

    inventory, blockers = [], []
    selected = opening_refund_source_inventory(organization=organization, cutoff=cutoff)
    for row in selected["refunds"]:
        try:
            source = opening_refund_source_snapshot(
                organization=organization, refund=row
            )
            intact = True
        except (
            ValidationError,
            ObjectDoesNotExist,
            TypeError,
            ValueError,
            KeyError,
        ) as exc:
            source = opening_refund_source_graph(organization=organization, refund=row)
            intact = False
            blockers.extend(
                exc.messages
                if isinstance(exc, ValidationError)
                else ["Bukti pengembalian uang tidak dapat diperiksa."]
            )
        inventory.append(
            {
                "kind": "opening_principal_refund",
                "refund_id": row.pk,
                "item_id": row.item_id,
                "settlement_id": row.settlement_id,
                "date": row.date.isoformat() if type(row.date) is PostingDate else None,
                "amount": format(row.amount, ".2f") if intact else None,
                "source": source,
                "intact": intact,
            }
        )
    if selected["orphans"]:
        inventory.extend(selected["orphans"])
        blockers.append(
            "Bukti pengembalian uang atau jurnal aslinya terputus. Nilai sumber belum dapat dinilai."
        )
    snapshot = json_value(
        {
            "organization_id": getattr(organization, "pk", organization),
            "cutoff": cutoff,
            "inventory": inventory,
        }
    )
    return {
        "fingerprint": digest(snapshot),
        "inventory": inventory,
        "blockers": list(dict.fromkeys(blockers)),
    }


def opening_refund_review_correction_blockers(*, organization, refund):
    """An existing refund conclusion cannot rewrite protected downstream use."""
    from finance.models import OpeningBalanceRefund
    from finance.opening_balances import opening_refund_source_snapshot

    from .opening_balance_review_dependencies import (
        opening_principal_correction_blockers,
    )

    org_id, refund_id = (
        getattr(organization, "pk", organization),
        getattr(refund, "pk", refund),
    )
    if type(org_id) is not int or type(refund_id) is not int:
        raise ValidationError("Perusahaan dan pengembalian uang harus sah.")
    try:
        row = OpeningBalanceRefund.objects.get(organization_id=org_id, pk=refund_id)
    except OpeningBalanceRefund.DoesNotExist:
        raise ValidationError(
            "Pengembalian uang tidak tersedia untuk perusahaan ini."
        ) from None
    opening_refund_source_snapshot(organization=organization, refund=row)
    return opening_principal_correction_blockers(
        organization=organization, item=row.item_id, date=row.date
    )


def opening_refund_report_dependency(*, organization, cutoff):
    from .opening_refund_review import opening_refund_tax_gates

    result = opening_refund_tax_gates(organization=organization, cutoff=cutoff)
    dependency = {
        "fingerprint": result["fingerprint"],
        "inventory": result["inventory"],
    }
    gates = [
        {
            "code": "opening_refund_review",
            "title": "Periksa pajak pengembalian uang",
            "detail": message,
            "severity": "review",
        }
        for message in result["blockers"]
    ]
    candidates = [
        {
            "refund_id": row["refund_id"],
            "item_id": row.get("item_id"),
            "date": PostingDate.fromisoformat(row["date"]) if row.get("date") else None,
            "status": row["status"],
            "reason": "Pengembalian uang memerlukan laporan tersendiri. Laporan pembayaran asli tidak menentukan dampak pajak refund.",
        }
        for row in result["inventory"]
        if type(row.get("refund_id")) is int
        and row.get("kind") != "opening_refund_orphan"
        and row["status"] != "reviewed"
    ]
    return dependency, gates, candidates
