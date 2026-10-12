"""Attribute validated supplier principal to Bills without posting cash again."""

from decimal import Decimal

from django.core.exceptions import ValidationError


def supplier_advance_budget_facts(*, organization, bill, as_of):
    from .supplier_advances import (
        supplier_advance_application_source_snapshot,
        supplier_advance_bill_settlement,
    )

    facts = supplier_advance_bill_settlement(
        organization=organization, bill=bill, as_of=as_of
    )
    applications = {row["id"]: row for row in facts["applications"]}
    reversed_ids = {row["application_id"] for row in facts["reversals"]}
    payments = []
    for pk, row in applications.items():
        if pk in reversed_ids:
            continue
        proof = supplier_advance_application_source_snapshot(
            organization=organization, application=pk
        )
        payments.append(
            {
                "kind": "supplier_advance_application",
                "id": pk,
                "event_id": f"supplier_advance_application:{pk}",
                "date": proof["source"]["date"],
                "available_from": row["date"].isoformat(),
                "amount": format(row["amount"], ".2f"),
                "source_hash": proof["fingerprint"],
                "advance_id": proof["advance_id"],
                "bank_transaction_id": proof["source"]["bank"]["id"],
            }
        )
    return {
        **facts,
        "payments": payments,
        "attributed_cash": sum(
            (Decimal(row["amount"]) for row in payments), Decimal("0.00")
        ),
    }


def validate_supplier_advance_reversal_budget(*, organization, application, as_of):
    """A reversal needs an explicit withdrawal of any existing cash match."""
    from django.db.models import Q

    from director.spend_models import BudgetPaymentMatch

    from . import services as ledger
    from .models import SupplierAdvanceApplication

    row = ledger._scoped(SupplierAdvanceApplication, application, organization)
    # Do not hide a damaged cross-company match by filtering its organization.
    cutoff = ledger._date(as_of)
    if (
        BudgetPaymentMatch.objects.filter(supplier_advance_application_id=row.pk)
        .filter(Q(voided_at__isnull=True) | Q(voided_at__gt=cutoff))
        .exists()
    ):
        raise ValidationError(
            "Uang muka ini sudah dicocokkan dengan pembayaran budget. Owner perlu menarik pencocokan tersebut terlebih dahulu; riwayat pembayaran tetap tersimpan."
        )


def supplier_advance_bill_identity(*, bill_id):
    """Opaque repair identity, including damaged pointers, never monetary facts."""
    from django.db.models import Q

    from taxes.models import BillTaxDecision, ExternalTaxReview, TaxEvidence

    from .budget_adapter import _identity_rows
    from .models import (
        Allocation,
        BankAccount,
        BankPosting,
        BankTransaction,
        Bill,
        BillPayment,
        Invoice,
        Journal,
        JournalLine,
        Party,
        SupplierAdvance,
        SupplierAdvanceApplication,
        SupplierAdvanceApplicationReversal,
        SupplierAdvanceRefund,
        WithholdingSettlement,
    )

    if not SupplierAdvanceApplication.objects.filter(bill_id=bill_id).exists():
        return None
    bill_ids, advance_ids, bank_ids = {bill_id}, set(), set()
    while True:
        previous = (set(bill_ids), set(advance_ids), set(bank_ids))
        connected = SupplierAdvanceApplication.objects.filter(
            Q(bill_id__in=bill_ids) | Q(advance_id__in=advance_ids)
        )
        bill_ids.update(connected.values_list("bill_id", flat=True))
        advance_ids.update(connected.values_list("advance_id", flat=True))
        posting_ids = set(
            SupplierAdvance.objects.filter(pk__in=advance_ids).values_list(
                "bank_posting_id", flat=True
            )
        )
        posting_ids.update(
            SupplierAdvanceRefund.objects.filter(
                advance_id__in=advance_ids
            ).values_list("bank_posting_id", flat=True)
        )
        posting_ids.update(
            WithholdingSettlement.objects.filter(bill_id__in=bill_ids).values_list(
                "bank_posting_id", flat=True
            )
        )
        bank_ids.update(
            BankPosting.objects.filter(pk__in=posting_ids).values_list(
                "transaction_id", flat=True
            )
        )
        bank_ids.update(
            BillPayment.objects.filter(bill_id__in=bill_ids).values_list(
                "transaction_id", flat=True
            )
        )
        # Shared bank claims are also part of conservation proof. Retain them
        # even when a damaged pointer names another organization.
        advance_ids.update(
            SupplierAdvance.objects.filter(
                bank_posting__transaction_id__in=bank_ids
            ).values_list("pk", flat=True)
        )
        advance_ids.update(
            SupplierAdvanceRefund.objects.filter(
                bank_posting__transaction_id__in=bank_ids
            ).values_list("advance_id", flat=True)
        )
        bill_ids.update(
            BillPayment.objects.filter(transaction_id__in=bank_ids).values_list(
                "bill_id", flat=True
            )
        )
        if previous == (bill_ids, advance_ids, bank_ids):
            break
    applications = _identity_rows(
        SupplierAdvanceApplication.objects.filter(
            Q(advance_id__in=advance_ids) | Q(bill_id__in=bill_ids)
        )
    )
    reversals = _identity_rows(
        SupplierAdvanceApplicationReversal.objects.filter(
            application_id__in=[row["id"] for row in applications]
        )
    )
    advances = _identity_rows(SupplierAdvance.objects.filter(pk__in=advance_ids))
    refunds = _identity_rows(
        SupplierAdvanceRefund.objects.filter(advance_id__in=advance_ids)
    )
    settlements = _identity_rows(
        WithholdingSettlement.objects.filter(bill_id__in=bill_ids)
    )
    payments = _identity_rows(
        BillPayment.objects.filter(
            Q(bill_id__in=bill_ids) | Q(transaction_id__in=bank_ids)
        )
    )
    claims = _identity_rows(Allocation.objects.filter(transaction_id__in=bank_ids))
    claim_invoice_ids = {row["invoice_id"] for row in claims}
    claim_invoices = _identity_rows(Invoice.objects.filter(pk__in=claim_invoice_ids))
    claim_party_ids = {row["party_id"] for row in claim_invoices}
    claim_parties = _identity_rows(Party.objects.filter(pk__in=claim_party_ids))
    postings = _identity_rows(
        BankPosting.objects.filter(
            Q(
                pk__in=[
                    row["bank_posting_id"] for row in advances + refunds + settlements
                ]
            )
            | Q(transaction_id__in=bank_ids)
        )
    )
    journal_ids = {
        row["journal_id"]
        for row in advances
        + refunds
        + applications
        + reversals
        + postings
        + settlements
    }
    journal_keys = {f"bill-payment:{row['id']}" for row in payments} | {
        f"bill:{pk}:approval" for pk in bill_ids
    }
    journal_keys.update(f"allocation:{row['id']}" for row in claims)
    journals = _identity_rows(
        Journal.objects.filter(Q(pk__in=journal_ids) | Q(source_key__in=journal_keys))
    )
    journal_ids.update(row["id"] for row in journals)
    banks = _identity_rows(BankTransaction.objects.filter(pk__in=bank_ids))
    account_ids = {row["account_id"] for row in banks}
    supplier_ids = set(
        Bill.objects.filter(pk__in=bill_ids).values_list("supplier_id", flat=True)
    ) | {row["supplier_id"] for row in advances}
    decisions = _identity_rows(BillTaxDecision.objects.filter(bill_id__in=bill_ids))
    reports = _identity_rows(
        ExternalTaxReview.objects.filter(
            pk__in=[row["external_review_id"] for row in decisions]
        )
    )
    documents = _identity_rows(
        TaxEvidence.objects.filter(pk__in=[row["evidence_id"] for row in reports])
    )
    return {
        "advances": advances,
        "applications": applications,
        "refunds": refunds,
        "reversals": reversals,
        "postings": postings,
        "banks": banks,
        "payments": payments,
        "settlements": settlements,
        "accounts": _identity_rows(BankAccount.objects.filter(pk__in=account_ids)),
        "bills": _identity_rows(Bill.objects.filter(pk__in=bill_ids)),
        "decisions": decisions,
        "reports": reports,
        "documents": documents,
        "suppliers": _identity_rows(Party.objects.filter(pk__in=supplier_ids)),
        "claims": claims,
        "claim_invoices": claim_invoices,
        "claim_parties": claim_parties,
        "journals": journals,
        "lines": _identity_rows(JournalLine.objects.filter(journal_id__in=journal_ids)),
        "referenced_ids": {
            "advances": sorted(advance_ids),
            "bills": sorted(bill_ids),
            "banks": sorted(bank_ids),
            "accounts": sorted(account_ids),
            "suppliers": sorted(supplier_ids),
            "journals": sorted(journal_ids),
            "invoices": sorted(claim_invoice_ids),
            "claim_parties": sorted(claim_party_ids),
            "journal_source_keys": sorted(journal_keys),
        },
    }
