"""Read actual Bill ledger facts for Director budgets; never post or infer tax.

Amounts are positive economic magnitudes, except the signed commitment change
on a prepaid release. Journal lines retain their original debit/credit sides.
Source corruption raises ValidationError; callers must not substitute zero.
"""

import hashlib
import json
from datetime import date as Date
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db.models import Q

from core.models import Organization

from .models import (
    Bill,
    Journal,
    PrepaymentRelease,
    TaxRemittance,
    WithholdingSettlement,
)

ZERO = Decimal("0.00")
VERSION = "finance-bill-budget-v1"
MONEY = (
    "gross",
    "incurred",
    "commitment",
    "ap_settled",
    "withholding_created",
    "tax_remitted",
    "cash_outflow",
)


def _hash(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


def _money(value, *, positive=False):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or (positive and amount <= ZERO):
            raise ValueError
        return amount.quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationError(
            "Nominal sumber tagihan tidak valid. Periksa bukti Finance."
        ) from exc


def _text_amount(value):
    return format(_money(value), ".2f")


def _date(value):
    if type(value) is Date:
        return value
    if isinstance(value, str):
        try:
            parsed = Date.fromisoformat(value)
        except ValueError as exc:
            raise ValidationError(
                "Pilih tanggal posisi dalam format tahun-bulan-tanggal."
            ) from exc
        if parsed.isoformat() == value:
            return parsed
    raise ValidationError("Pilih tanggal posisi dalam format tahun-bulan-tanggal.")


def _scope(obj, org):
    if obj.organization_id != org.pk:
        raise ValidationError(
            "Bukti tagihan, jurnal, atau bank berasal dari perusahaan lain."
        )
    return obj


def _journal(org, *, key, date, entries, journal_id=None):
    query = Journal.objects.filter(organization=org, source_key=key)
    if journal_id is not None:
        query = query.filter(pk=journal_id)
    rows = list(
        Journal.objects.filter(pk__in=query.values("pk")[:1])
        .order_by("lines__pk")
        .values(
            "pk",
            "source_key",
            "date",
            "status",
            "posted_at",
            "lines__pk",
            "lines__organization_id",
            "lines__account",
            "lines__debit",
            "lines__credit",
        )
    )
    journal = rows[0] if rows else None
    if (
        not journal
        or journal["status"] != "posted"
        or journal["date"] != date
        or not journal["posted_at"]
    ):
        raise ValidationError(
            "Jurnal asli tagihan atau pembayaran belum lengkap atau tidak cocok dengan tanggal sumber."
        )
    lines = [
        {
            field: row["lines__" + field]
            for field in ("pk", "organization_id", "account", "debit", "credit")
        }
        for row in rows
        if row["lines__pk"] is not None
    ]
    actual = [
        (line["organization_id"], line["account"], line["debit"], line["credit"])
        for line in lines
    ]
    expected = [(org.pk, account, debit, credit) for account, debit, credit in entries]
    if actual != expected:
        raise ValidationError(
            "Seluruh baris jurnal asli harus sesuai perusahaan, akun, dan nominal tagihan/pembayaran."
        )
    return {
        "id": journal["pk"],
        "source_key": journal["source_key"],
        "date": journal["date"].isoformat(),
        "status": journal["status"],
        "posted_at": journal["posted_at"].isoformat(),
        "lines": [
            {
                **line,
                "debit": _text_amount(line["debit"]),
                "credit": _text_amount(line["credit"]),
            }
            for line in lines
        ],
    }


def _bank(org, bank, amount):
    _scope(bank, org)
    _scope(bank.account, org)
    if bank.amount >= ZERO or amount <= ZERO or abs(bank.amount) < amount:
        raise ValidationError(
            "Bukti bank keluar tidak sesuai nominal pembayaran tagihan."
        )
    # A split bank outflow is allowed, but all recorded claims must still fit
    # the one original transaction. This checks proof integrity, not today's
    # remaining capacity and not another Bill's budget consumption.
    if bank.allocations.exists():
        raise ValidationError(
            "Mutasi bank keluar mempunyai alokasi penerimaan yang tidak sesuai arah uang."
        )
    claimed = ZERO
    for row in bank.bill_payments.values(
        "organization_id", "bill__organization_id", "amount"
    ):
        if row["organization_id"] != org.pk or row["bill__organization_id"] != org.pk:
            raise ValidationError("Alokasi mutasi bank berasal dari perusahaan lain.")
        claimed += _money(row["amount"], positive=True)
    for row in bank.postings.values(
        "organization_id", "signed_amount", "date", "journal__organization_id"
    ):
        value = _money(row["signed_amount"])
        if (
            row["organization_id"] != org.pk
            or row["journal__organization_id"] != org.pk
            or row["date"] != bank.date
            or value >= ZERO
        ):
            raise ValidationError(
                "Arah, tanggal atau perusahaan pencatatan mutasi bank tidak sesuai bukti asli."
            )
        claimed -= value
    from .opening_balances import opening_cash_cancellation_facts

    recovery = opening_cash_cancellation_facts(organization=org, transaction=bank)
    claimed += recovery["released"]  # Outgoing releases are signed negative.
    if claimed > abs(bank.amount):
        raise ValidationError(
            "Seluruh alokasi melebihi uang yang benar-benar keluar pada mutasi bank ini."
        )
    proof = {
        "transaction_id": bank.pk,
        "organization_id": bank.organization_id,
        "account_id": bank.account_id,
        "date": bank.date.isoformat(),
        "reference": bank.reference,
        "amount": _text_amount(bank.amount),
        "description_sha256": _hash(bank.description),
        "account_identity_sha256": _hash(
            {
                "id": bank.account_id,
                "organization_id": bank.account.organization_id,
                "name": bank.account.name,
                "number": bank.account.account_number,
            }
        ),
    }
    if recovery["releases"]:
        proof["opening_cancellations"] = recovery["releases"]
    return proof


def _posting(org, event, kind, amount):
    posting = _scope(event.bank_posting, org)
    if (
        posting.kind != kind
        or posting.signed_amount != -amount
        or posting.date != event.date
        or posting.journal_id != event.journal_id
        or posting.reference != event.reference
        or posting.evidence != event.evidence
        or posting.date != posting.transaction.date
    ):
        raise ValidationError(
            "Hubungan pembayaran, mutasi bank dan jurnal tidak sesuai bukti asli."
        )
    bank = _bank(org, posting.transaction, amount)
    bank["posting"] = {
        "id": posting.pk,
        "kind": posting.kind,
        "reference": posting.reference,
        "date": posting.date.isoformat(),
        "signed_amount": _text_amount(posting.signed_amount),
        "journal_id": posting.journal_id,
        "evidence_sha256": _hash(posting.evidence),
    }
    return bank


def _event(kind, obj, date, *, journal, bank=None, **money):
    row = {
        "event_id": f"{kind}:{obj.pk}",
        "kind": kind,
        "source_model": obj._meta.label,
        "source_id": obj.pk,
        "date": date.isoformat(),
        "journal": journal,
        "bank": bank,
        **{field: _text_amount(money.get(field, ZERO)) for field in MONEY},
    }
    if hasattr(obj, "reference"):
        row["reference"] = obj.reference
    if hasattr(obj, "evidence"):
        row["evidence_sha256"] = _hash(obj.evidence)
    row["fingerprint"] = _hash(row)
    return row


def _bill_payments_at(org, bill_id, cutoff):
    """Keep an original past journal visible if its bank date is damaged."""
    from .models import BillPayment

    payments = BillPayment.objects.filter(bill_id=bill_id)
    keys = {f"bill-payment:{pk}": pk for pk in payments.values_list("pk", flat=True)}
    anchored = [
        keys[key]
        for key in Journal.objects.filter(
            organization=org, source_key__in=keys, date__lte=cutoff
        ).values_list("source_key", flat=True)
    ]
    return payments.filter(Q(transaction__date__lte=cutoff) | Q(pk__in=anchored))


def bill_budget_facts(*, organization, bill, as_of):
    """Return whole-Bill facts by accounting date, with exact typed proof IDs.

    Draft or not-yet-effective Bills remain provisional. VAT is already included
    in Bill.amount; CostAllocation is attribution and is deliberately not summed.
    The fingerprint binds selected facts, not a changing wall-clock/cutoff label.
    """
    from taxes.models import BillTaxDecision

    try:
        org_pk, bill_pk = (
            getattr(organization, "pk", organization),
            getattr(bill, "pk", bill),
        )
        if isinstance(org_pk, bool) or isinstance(bill_pk, bool):
            raise ValueError
        org = Organization.objects.get(pk=org_pk)
        bill = Bill.objects.select_related("supplier").get(organization=org, pk=bill_pk)
    except (Organization.DoesNotExist, Bill.DoesNotExist, TypeError, ValueError) as exc:
        raise ValidationError("Tagihan tidak tersedia untuk perusahaan ini.") from exc
    cutoff = _date(as_of)
    _scope(bill.supplier, org)
    if bill.supplier.kind != "supplier":
        raise ValidationError(
            "Pihak pada tagihan ini tidak lagi tercatat sebagai pemasok. Minta Owner memeriksa identitas sumber."
        )
    amount = _money(bill.amount, positive=True)
    vat = _money(bill.supplier_vat)
    if bill.status not in ("draft", "approved") or vat < ZERO or vat > amount:
        raise ValidationError(
            "Status atau bruto/PPN tagihan tidak valid; periksa sumber Finance."
        )
    prepaid = bill.category == "prepayment" or bill.service_date > bill.date
    meta = {
        "number": bill.number,
        "status": bill.status,
        "supplier_id": bill.supplier_id,
        "supplier_name": bill.supplier.name,
        "supplier_reference": bill.supplier_reference,
        "date": bill.date.isoformat(),
        "service_date": bill.service_date.isoformat(),
        "category": bill.category,
        "amount": _text_amount(amount),
        "supplier_vat": _text_amount(vat),
    }
    events = []
    effective = bill.status == "approved" and bill.date <= cutoff
    if effective:
        journal = _journal(
            org,
            key=f"bill:{bill.pk}:approval",
            date=bill.date,
            entries=[
                ("PREPAID" if prepaid else "EXPENSE", amount, ZERO),
                ("AP", ZERO, amount),
            ],
        )
        events.append(
            _event(
                "bill_approval",
                bill,
                bill.date,
                journal=journal,
                gross=amount,
                incurred=ZERO if prepaid else amount,
                commitment=amount if prepaid else ZERO,
            )
        )
    elif Journal.objects.filter(
        organization=org, source_key=f"bill:{bill.pk}:approval", date__lte=cutoff
    ).exists():
        raise ValidationError(
            "Status tagihan bertentangan dengan jurnal pengesahan yang sudah tercatat."
        )
    releases = list(
        PrepaymentRelease.objects.filter(bill_id=bill.pk, date__lte=cutoff)
        .select_related("journal")
        .order_by("date", "pk")
    )
    payments = list(
        _bill_payments_at(org, bill.pk, cutoff)
        .select_related("transaction__account")
        .order_by("transaction__date", "pk")
    )
    settlements = list(
        WithholdingSettlement.objects.filter(bill_id=bill.pk, date__lte=cutoff)
        .select_related("obligation", "bank_posting__transaction__account")
        .order_by("date", "pk")
    )
    related_obligations = [row.obligation_id for row in settlements]
    remits = list(
        TaxRemittance.objects.filter(
            Q(obligation__source_bill_id=bill.pk)
            | Q(obligation_id__in=related_obligations),
            date__lte=cutoff,
        )
        .select_related("obligation", "bank_posting__transaction__account")
        .order_by("date", "pk")
    )
    if not effective and (releases or payments or settlements or remits):
        raise ValidationError(
            "Tagihan belum disahkan pada tanggal posisi tetapi sudah mempunyai biaya atau pembayaran terposting."
        )
    for release in releases:
        _scope(release, org)
        value = _money(release.amount, positive=True)
        if not prepaid or release.date < max(bill.date, bill.service_date):
            raise ValidationError(
                "Pelepasan biaya di muka tidak sesuai jenis atau tanggal layanan tagihan."
            )
        journal = _journal(
            org,
            key=f"prepayment:{release.reference}",
            date=release.date,
            journal_id=release.journal_id,
            entries=[("EXPENSE", value, ZERO), ("PREPAID", ZERO, value)],
        )
        events.append(
            _event(
                "prepayment_release",
                release,
                release.date,
                journal=journal,
                incurred=value,
                commitment=-value,
            )
        )
    for payment in payments:
        _scope(payment, org)
        value = _money(payment.amount, positive=True)
        bank = _bank(org, payment.transaction, value)
        if payment.transaction.date < bill.date:
            raise ValidationError(
                "Pembayaran pemasok mendahului tagihan; alur uang muka pemasok belum didukung."
            )
        journal = _journal(
            org,
            key=f"bill-payment:{payment.pk}",
            date=payment.transaction.date,
            entries=[("AP", value, ZERO), ("BANK", ZERO, value)],
        )
        events.append(
            _event(
                "bill_payment",
                payment,
                payment.transaction.date,
                journal=journal,
                bank=bank,
                ap_settled=value,
                cash_outflow=value,
            )
        )
    if settlements and payments:
        raise ValidationError(
            "Pelunasan potongan penuh bertentangan dengan pembayaran biasa pada tagihan yang sama."
        )
    for settlement in settlements:
        _scope(settlement, org)
        obligation = _scope(settlement.obligation, org)
        decision = BillTaxDecision.objects.filter(bill_id=bill.pk).first()
        tax = _money(settlement.withholding_amount, positive=True)
        net = _money(settlement.net_amount, positive=True)
        if (
            settlement.gross_amount != amount
            or net + tax != amount
            or settlement.date < bill.date
            or obligation.source_bill_id != bill.pk
            or obligation.direction != "payable"
            or obligation.tax_type != "pph23"
            or obligation.status != "approved"
            or obligation.amount != tax
            or bill.tax_amount != tax
            or bill.tax_status != "reviewed"
            or not decision
            or decision.organization_id != org.pk
            or decision.obligation_id != obligation.pk
            or decision.amount != tax
            or decision.period != obligation.period
            or decision.rule_code != obligation.rule_code
            or decision.base != obligation.base
            or decision.rate != obligation.rate
            or decision.external_review_id != obligation.external_review_id
            or settlement.date.replace(day=1) != decision.period
        ):
            raise ValidationError(
                "Pelunasan neto, kewajiban potongan dan tagihan bruto tidak sesuai satu sama lain."
            )
        bank = _posting(org, settlement, "withheld_supplier", net)
        journal = _journal(
            org,
            key=f"bank-event:{settlement.bank_posting.reference}",
            date=settlement.date,
            journal_id=settlement.journal_id,
            entries=[
                ("AP", amount, ZERO),
                ("BANK", ZERO, net),
                ("TAX_PAYABLE", ZERO, tax),
            ],
        )
        event = _event(
            "withholding_supplier",
            settlement,
            settlement.date,
            journal=journal,
            bank=bank,
            ap_settled=amount,
            withholding_created=tax,
            cash_outflow=net,
        )
        # Bind the recorded economic decision; do not reclassify the tax or
        # require a new current legal review merely to observe historical cash.
        event.update(
            obligation_id=obligation.pk,
            decision_id=decision.pk,
            tax_source={
                "period": obligation.period.isoformat(),
                "rule_code": decision.rule_code,
                "base": _text_amount(decision.base),
                "rate": format(decision.rate, ".6f"),
                "amount": _text_amount(tax),
                "reason_sha256": _hash(decision.reason),
                "external_review_id": decision.external_review_id,
                "source_evidence_id": obligation.source_evidence_id,
                "identity_sha256": _hash(obligation.identity_snapshot),
                "object_decision_sha256": _hash(obligation.object_decision),
                "legal_sources_sha256": _hash(obligation.legal_sources),
                "tax_point_date": obligation.tax_point_date.isoformat()
                if obligation.tax_point_date
                else None,
                "tax_point_basis": obligation.tax_point_basis,
            },
        )
        event["fingerprint"] = _hash(
            {key: value for key, value in event.items() if key != "fingerprint"}
        )
        events.append(event)
    for remit in remits:
        _scope(remit, org)
        obligation = _scope(remit.obligation, org)
        source = next(
            (row for row in settlements if row.obligation_id == obligation.pk), None
        )
        value = _money(remit.amount, positive=True)
        if (
            not source
            or obligation.source_bill_id != bill.pk
            or remit.date < source.date
        ):
            raise ValidationError(
                "Setoran potongan belum mempunyai pelunasan pemasok yang cocok pada tanggal posisi."
            )
        bank = _posting(org, remit, "tax_remittance", value)
        journal = _journal(
            org,
            key=f"bank-event:{remit.bank_posting.reference}",
            date=remit.date,
            journal_id=remit.journal_id,
            entries=[("TAX_PAYABLE", value, ZERO), ("BANK", ZERO, value)],
        )
        event = _event(
            "tax_remittance",
            remit,
            remit.date,
            journal=journal,
            bank=bank,
            tax_remitted=value,
            cash_outflow=value,
        )
        event.update(obligation_id=obligation.pk, settlement_id=source.pk)
        event["fingerprint"] = _hash(
            {key: value for key, value in event.items() if key != "fingerprint"}
        )
        events.append(event)
    advance_payments = []
    advance_graph = None
    from .models import SupplierAdvanceApplication, SupplierAdvanceApplicationReversal

    if SupplierAdvanceApplication.objects.filter(bill_id=bill.pk).exists():
        from .supplier_advance_budget import supplier_advance_budget_facts

        advance_facts = supplier_advance_budget_facts(
            organization=org, bill=bill, as_of=cutoff
        )
        advance_graph = advance_facts["fingerprint"]
        if not effective and (
            advance_facts["applications"] or advance_facts["reversals"]
        ):
            raise ValidationError("Penerapan uang muka mendahului pengesahan tagihan.")
        for row in advance_facts["applications"]:
            obj = SupplierAdvanceApplication.objects.get(pk=row["id"])
            events.append(
                _event(
                    "supplier_advance_application",
                    obj,
                    row["date"],
                    journal=row["proof"]["journal"],
                    ap_settled=row["amount"],
                )
            )
        for row in advance_facts["reversals"]:
            obj = SupplierAdvanceApplicationReversal.objects.get(pk=row["id"])
            events.append(
                _event(
                    "supplier_advance_reversal",
                    obj,
                    row["date"],
                    journal=row["proof"]["journal"],
                    ap_settled=-row["amount"],
                )
            )
        advance_payments = advance_facts["payments"]
    events.sort(key=lambda row: (row["date"], row["event_id"]))
    totals = {
        field: sum((Decimal(row[field]) for row in events), ZERO) for field in MONEY
    }
    ap, withholding = (
        totals["gross"] - totals["ap_settled"],
        totals["withholding_created"] - totals["tax_remitted"],
    )
    if totals["commitment"] < ZERO or ap < ZERO or withholding < ZERO:
        raise ValidationError(
            "Realisasi, pembayaran atau setoran melebihi sumber tagihan yang terposting."
        )
    source_references = [
        {
            "kind": "bill",
            "id": bill.pk,
            "number": bill.number,
            "date": bill.date.isoformat(),
        }
    ]
    source_references.extend(
        {
            "kind": row["kind"],
            "id": row["source_id"],
            "event_id": row["event_id"],
            "date": row["date"],
            "journal_id": row["journal"]["id"],
            "source_hash": row["fingerprint"],
        }
        for row in events
    )
    result = {
        "version": VERSION,
        "organization_id": org.pk,
        "bill_id": bill.pk,
        "as_of": cutoff.isoformat(),
        "bill": meta,
        "state": bill.status,
        "provisional": not effective,
        **{
            field: _text_amount(totals[field])
            for field in MONEY
            if field != "cash_outflow"
        },
        "ap_outstanding": _text_amount(ap),
        "withholding_outstanding": _text_amount(withholding),
        "verified_cash_outflow": _text_amount(
            totals["cash_outflow"]
            + sum((Decimal(p["amount"]) for p in advance_payments), ZERO)
        ),
        "events": events,
        "payments": [
            {
                "kind": row["kind"],
                "id": row["source_id"],
                "event_id": row["event_id"],
                "date": row["date"],
                "amount": row["cash_outflow"],
                "source_hash": row["fingerprint"],
            }
            for row in events
            if Decimal(row["cash_outflow"]) > ZERO
        ]
        + advance_payments,
        "source_references": source_references,
        "issues": [],
        "current": True,
    }
    if advance_graph is not None:
        result["supplier_advance_graph"] = advance_graph
    result["fingerprint"] = _hash(
        {key: value for key, value in result.items() if key != "as_of"}
    )
    return result


def sync_bill_budget(*, organization, bill, actor):
    """Called after a new complete Finance event, inside its Org-first transaction."""
    from director.spend_services import sync_bill_budget as director_sync

    return director_sync(organization=organization, bill=bill, actor=actor)


def _identity_value(value):
    if isinstance(value, (Date, Decimal)):
        return value.isoformat() if isinstance(value, Date) else str(value)
    if isinstance(value, dict):
        return {key: _identity_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_identity_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _identity_rows(query, fields=None):
    fields = fields or [field.attname for field in query.model._meta.concrete_fields]
    return [_identity_value(row) for row in query.order_by("pk").values(*fields)]


def bill_budget_source_identity(*, organization, bill, as_of):
    """Return only an opaque digest of the selected Bill's source graph.

    This is NOT validation or a monetary fact. Known dated source rows and
    referenced identities are hashed even when their scope/proof is damaged,
    so a new failing pointer cannot reuse an old Owner repair intention. The
    projection never leaves this function; foreign names/accounts are never
    returned. Missing referenced IDs remain in the digest as missing proofs.
    """
    from taxes.models import BillTaxDecision, TaxObligation

    from .models import (
        Allocation,
        BankAccount,
        BankPosting,
        BankTransaction,
        BillPayment,
        Invoice,
        JournalLine,
        Party,
    )

    cutoff = _date(as_of)
    try:
        org_pk, bill_pk = (
            getattr(organization, "pk", organization),
            getattr(bill, "pk", bill),
        )
        if isinstance(org_pk, bool) or isinstance(bill_pk, bool):
            raise ValueError
        org = Organization.objects.get(pk=org_pk)
        local_bill = Bill.objects.get(organization=org, pk=bill_pk)
    except (Organization.DoesNotExist, Bill.DoesNotExist, TypeError, ValueError) as exc:
        raise ValidationError("Tagihan tidak tersedia untuk perusahaan ini.") from exc

    # Read FKs as values, rather than filtering away cross-org children or
    # dereferencing them through a successful-proof-only relation.
    payment_rows = _identity_rows(_bill_payments_at(org, local_bill.pk, cutoff))
    releases = _identity_rows(
        PrepaymentRelease.objects.filter(bill_id=local_bill.pk, date__lte=cutoff)
    )
    settlements = _identity_rows(
        WithholdingSettlement.objects.filter(bill_id=local_bill.pk, date__lte=cutoff)
    )
    obligation_ids = {row["obligation_id"] for row in settlements}
    remits = _identity_rows(
        TaxRemittance.objects.filter(
            Q(obligation__source_bill_id=local_bill.pk)
            | Q(obligation_id__in=obligation_ids),
            date__lte=cutoff,
        )
    )
    decisions = (
        _identity_rows(BillTaxDecision.objects.filter(bill_id=local_bill.pk))
        if local_bill.date <= cutoff
        else []
    )
    obligation_ids.update(row["obligation_id"] for row in remits)
    obligation_ids.update(
        row["obligation_id"] for row in decisions if row["obligation_id"] is not None
    )
    posting_ids = {row["bank_posting_id"] for row in settlements + remits}
    selected_postings = _identity_rows(BankPosting.objects.filter(pk__in=posting_ids))
    bank_ids = {row["transaction_id"] for row in payment_rows + selected_postings}
    bank_rows = _identity_rows(BankTransaction.objects.filter(pk__in=bank_ids))
    account_ids = {row["account_id"] for row in bank_rows}
    attached_payments = _identity_rows(
        BillPayment.objects.filter(transaction_id__in=bank_ids)
    )
    attached_postings = _identity_rows(
        BankPosting.objects.filter(transaction_id__in=bank_ids)
    )
    attached_allocations = _identity_rows(
        Allocation.objects.filter(transaction_id__in=bank_ids)
    )
    journal_ids = {
        row["journal_id"]
        for row in releases
        + settlements
        + remits
        + selected_postings
        + attached_postings
    }
    journal_keys = {f"bill-payment:{row['id']}" for row in attached_payments}
    if local_bill.date <= cutoff:
        journal_keys.add(f"bill:{local_bill.pk}:approval")
    journals = _identity_rows(
        Journal.objects.filter(
            Q(pk__in=journal_ids) | Q(organization=org, source_key__in=journal_keys)
        )
    )
    journal_ids.update(row["id"] for row in journals)
    bill_ids = {row["bill_id"] for row in attached_payments}
    invoice_ids = {row["invoice_id"] for row in attached_allocations}
    tax_fields = (
        "id",
        "organization_id",
        "source_bill_id",
        "direction",
        "tax_type",
        "period",
        "base",
        "rate",
        "amount",
        "status",
        "rule_code",
        "external_review_id",
        "source_evidence_id",
        "tax_point_date",
        "tax_point_basis",
        "identity_snapshot",
        "object_decision",
        "legal_sources",
    )
    projection = {
        "version": "finance-bill-source-identity-v1",
        "organization_id": org.pk,
        "bill": _identity_rows(Bill.objects.filter(pk=local_bill.pk)),
        "supplier": _identity_rows(Party.objects.filter(pk=local_bill.supplier_id)),
        "payments": payment_rows,
        "releases": releases,
        "settlements": settlements,
        "remittances": remits,
        "decisions": decisions,
        "obligations": _identity_rows(
            TaxObligation.objects.filter(pk__in=obligation_ids), tax_fields
        ),
        "selected_postings": selected_postings,
        "bank_transactions": bank_rows,
        "accounts": _identity_rows(BankAccount.objects.filter(pk__in=account_ids)),
        "attached_payments": attached_payments,
        "attached_postings": attached_postings,
        "attached_allocations": attached_allocations,
        "claim_bills": _identity_rows(
            Bill.objects.filter(pk__in=bill_ids), ("id", "organization_id")
        ),
        "claim_invoices": _identity_rows(
            Invoice.objects.filter(pk__in=invoice_ids), ("id", "organization_id")
        ),
        "journals": journals,
        "lines": _identity_rows(JournalLine.objects.filter(journal_id__in=journal_ids)),
        "referenced_ids": {
            "journals": sorted(journal_ids),
            "postings": sorted(posting_ids),
            "banks": sorted(bank_ids),
            "accounts": sorted(account_ids),
            "obligations": sorted(obligation_ids),
            "bills": sorted(bill_ids),
            "invoices": sorted(invoice_ids),
        },
    }
    from .supplier_advance_budget import supplier_advance_bill_identity

    supplier_identity = supplier_advance_bill_identity(bill_id=local_bill.pk)
    if supplier_identity is not None:
        projection["supplier_advances"] = supplier_identity
    from .opening_balances import (
        _unknown_recovery_sources,
        _unknown_refund_sources,
        opening_cash_cancellation_facts,
        opening_cash_refund_facts,
    )

    recovery_sources, refund_sources = [], []
    for bank_id in sorted(bank_ids):
        try:
            recovery = opening_cash_cancellation_facts(
                organization=org, transaction=bank_id
            )
            if recovery["releases"]:
                recovery_sources.append(
                    {"bank_id": bank_id, "fingerprint": recovery["fingerprint"]}
                )
            refunds = opening_cash_refund_facts(organization=org, transaction=bank_id)
            if refunds["refunds"]:
                refund_sources.append(
                    {"bank_id": bank_id, "fingerprint": refunds["fingerprint"]}
                )
        except ValidationError:
            # This is an opaque repair identity, never a substituted zero.
            recovery_sources.append(
                {"bank_id": bank_id, **_unknown_recovery_sources(org)}
            )
            refund_repairs = _unknown_refund_sources(org)
            if refund_repairs:
                refund_sources.append({"bank_id": bank_id, **refund_repairs})
    if recovery_sources:
        projection["opening_cancellations"] = recovery_sources
    if refund_sources:
        projection["opening_refunds"] = refund_sources
    return _hash(projection)
