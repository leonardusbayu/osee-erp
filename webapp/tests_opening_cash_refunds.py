"""Finance matches genuine returned old cash to a distinct actual bank transfer."""

from copy import deepcopy
from datetime import date
from decimal import Decimal
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.models import AuditEvent, DomainEvent, Membership, Organization
from finance import opening_balances as openings
from finance import services as ledger
from finance.models import (
    BankAccount,
    BankPosting,
    BankTransaction,
    Bill,
    Invoice,
    Journal,
    JournalLine,
    OpeningBalanceCancellation,
    OpeningBalanceCorrection,
    OpeningBalanceItem,
    OpeningBalanceRefund,
    OpeningBalanceSettlement,
    Party,
)


class OpeningCashRefundHTTPTests(TestCase):
    def test_finance_matches_partial_customer_refund_from_fully_paid_original_cash(
        self,
    ):
        org = Organization.objects.create(
            name="Synthetic actual old-cash refund UI", is_demo=True
        )
        owner = get_user_model().objects.create_user(username="opening-refund-ui-owner")
        finance = get_user_model().objects.create_user(
            username="opening-refund-ui-finance"
        )
        Membership.objects.create(organization=org, user=owner, role="owner")
        Membership.objects.create(organization=org, user=finance, role="finance")
        cutoff, receipt_day, refund_day = (
            date(2026, 9, 1),
            date(2026, 9, 10),
            date(2026, 9, 12),
        )
        party = Party.objects.create(
            organization=org, name="Synthetic refund customer", kind="customer"
        )
        account = BankAccount.objects.create(
            organization=org,
            name="Synthetic refund bank",
            account_number="SYN-REFUND-UI",
        )
        ledger.record_bank_opening(
            organization=org,
            actor=owner,
            account=account,
            date=cutoff,
            amount="1000",
            reference="SYN-REFUND-UI-BANK",
            evidence="Synthetic verified starting bank statement",
        )
        selection = {
            "kind": "receivable",
            "party": party,
            "date": cutoff,
            "source_date": date(2026, 8, 20),
            "source_reference": "SYN-REFUND-UI-OLD-AR",
            "amount": "300",
        }
        context = openings.opening_balance_action_context(
            organization=org, action="record", **selection
        )
        item = openings.record_opening_balance(
            organization=org,
            actor=owner,
            **selection,
            reason="Synthetic original outstanding principal independently checked",
            evidence="Synthetic old document and verified outstanding list",
            request_key=str(uuid4()),
            expected_fingerprint=context["fingerprint"],
            confirmed=True,
        )
        ledger.import_bank_csv(
            organization=org,
            actor=owner,
            account=account,
            content=(
                "date,reference,description,amount\n"
                f"{receipt_day},SYN-REFUND-UI-RECEIPT,Synthetic original customer receipt,300\n"
                f"{refund_day},SYN-REFUND-UI-RETURN,Synthetic actual outgoing customer refund,-50\n"
            ),
        )
        receipt_bank = BankTransaction.objects.get(
            organization=org, reference="SYN-REFUND-UI-RECEIPT"
        )
        refund_bank = BankTransaction.objects.get(
            organization=org, reference="SYN-REFUND-UI-RETURN"
        )
        context = openings.opening_balance_action_context(
            organization=org,
            action="settle",
            item=item,
            transaction=receipt_bank,
            amount="300",
        )
        settlement = openings.settle_opening_balance(
            organization=org,
            actor=owner,
            item=item,
            transaction=receipt_bank,
            amount="300",
            reason="Synthetic full original customer payment",
            evidence="Synthetic original receipt and old document independently checked",
            request_key=str(uuid4()),
            expected_fingerprint=context["fingerprint"],
            confirmed=True,
        )
        original_item_proof = deepcopy(
            openings.opening_balance_source_snapshot(organization=org, item=item)
        )
        original_cash_proof = deepcopy(
            openings.opening_settlement_source_snapshot(
                organization=org, settlement=settlement
            )
        )
        original_item = (
            item.amount,
            item.kind,
            item.party_id,
            item.date,
            item.source_date,
            item.source_reference,
            item.source_hash,
            deepcopy(item.source_snapshot),
        )
        original_cash = (
            settlement.amount,
            settlement.date,
            settlement.bank_posting_id,
            settlement.journal_id,
            settlement.source_hash,
            deepcopy(settlement.source_snapshot),
        )
        physical_sources = {
            bank.pk: (
                bank.organization_id,
                bank.account_id,
                bank.date,
                bank.amount,
                bank.reference,
                bank.description,
            )
            for bank in (receipt_bank, refund_bank)
        }
        position = openings.opening_balance_position(
            organization=org, item=item, as_of=refund_day
        )
        self.assertTrue(position["current"], position)
        self.assertEqual(
            (position["opening"], position["settled"], position["remaining"]),
            (Decimal(300), Decimal(300), Decimal(0)),
        )
        balances = ledger.report_balances(organization=org, as_of=refund_day)
        self.assertEqual(
            (balances["BANK"], balances["AR"], balances["EQUITY"]),
            (Decimal(1300), Decimal(0), Decimal(-1300)),
        )
        self.assertEqual(sum(balances.values()), Decimal(0))
        self.assertEqual(
            ledger.pending_bank_transactions(organization=org)
            .get(pk=refund_bank.pk)
            .remaining_amount,
            Decimal(-50),
        )

        def counts():
            return tuple(
                model.objects.count()
                for model in (
                    OpeningBalanceItem,
                    OpeningBalanceSettlement,
                    OpeningBalanceRefund,
                    OpeningBalanceCancellation,
                    OpeningBalanceCorrection,
                    BankTransaction,
                    BankPosting,
                    Journal,
                    JournalLine,
                    Invoice,
                    Bill,
                    AuditEvent,
                    DomainEvent,
                )
            )

        self.client.force_login(finance)
        detail_url = reverse("opening_balance_detail", args=[item.pk])
        refund_url = f"/finance/opening-balance-settlements/{settlement.pk}/refund/"
        before = counts()
        detail = self.client.get(detail_url)
        self.assertEqual(detail.status_code, 200)
        self.assertTrue(detail.context["position"]["exhausted"])
        self.assertContains(detail, 'href="' + refund_url + '"')
        self.assertContains(detail, "Cocokkan pengembalian uang")
        self.assertEqual(counts(), before)

        page = self.client.get(refund_url)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Cocokkan pengembalian uang")
        self.assertContains(page, "Periksa sumber")
        self.assertContains(page, "Konfirmasi dan simpan")
        self.assertIn("transaction", page.context["form"].fields)
        self.assertIn("amount", page.context["form"].fields)
        for fixed in (
            "date",
            "party",
            "item",
            "settlement",
            "source_date",
            "source_reference",
        ):
            self.assertNotIn(fixed, page.context["form"].fields)
        self.assertEqual(counts(), before)
        data = {
            "transaction": str(refund_bank.pk),
            "amount": "50",
            "reason": "Sebagian uang pelanggan benar-benar dikembalikan",
            "evidence": "Pembayaran asli dan mutasi pengembalian tanggal 12 September sudah diperiksa",
        }
        checked = self.client.post(refund_url, {**data, "intent": "check"})
        self.assertEqual(checked.status_code, 200)
        self.assertTrue(
            checked.context["save_available"], checked.context["source_context"]
        )
        preview = checked.context["source_context"]
        self.assertEqual(
            (
                preview["date"],
                preview["amount"],
                preview["before"],
                preview["after"],
                preview["refundable_remaining"],
                preview["bank_remaining"],
                preview["bank_after"],
            ),
            (
                refund_day,
                Decimal(50),
                Decimal(0),
                Decimal(50),
                Decimal(300),
                Decimal(50),
                Decimal(0),
            ),
        )
        self.assertFalse(checked.context["form"]["confirmed"].value())
        self.assertEqual(counts(), before)
        form = checked.context["form"]
        saved = self.client.post(
            refund_url,
            {
                **data,
                "intent": "save",
                "confirmed": "on",
                "request_key": form["request_key"].value(),
                "expected_fingerprint": form["expected_fingerprint"].value(),
            },
        )
        self.assertRedirects(saved, detail_url)

        refund = OpeningBalanceRefund.objects.get(organization=org)
        self.assertEqual(
            (
                refund.item_id,
                refund.settlement_id,
                refund.actor_id,
                refund.date,
                refund.amount,
                refund.bank_posting.transaction_id,
                refund.bank_posting.signed_amount,
            ),
            (
                item.pk,
                settlement.pk,
                finance.pk,
                refund_day,
                Decimal(50),
                refund_bank.pk,
                Decimal(-50),
            ),
        )
        self.assertEqual(
            (
                OpeningBalanceItem.objects.count(),
                OpeningBalanceSettlement.objects.count(),
                OpeningBalanceRefund.objects.count(),
                BankTransaction.objects.count(),
                BankPosting.objects.count(),
            ),
            (1, 1, 1, 2, 2),
        )
        self.assertEqual(
            (
                OpeningBalanceCancellation.objects.count(),
                OpeningBalanceCorrection.objects.count(),
                Invoice.objects.count(),
                Bill.objects.count(),
            ),
            (0, 0, 0, 0),
        )
        item.refresh_from_db()
        settlement.refresh_from_db()
        self.assertEqual(
            (
                item.amount,
                item.kind,
                item.party_id,
                item.date,
                item.source_date,
                item.source_reference,
                item.source_hash,
                item.source_snapshot,
            ),
            original_item,
        )
        self.assertEqual(
            (
                settlement.amount,
                settlement.date,
                settlement.bank_posting_id,
                settlement.journal_id,
                settlement.source_hash,
                settlement.source_snapshot,
            ),
            original_cash,
        )
        self.assertEqual(
            openings.opening_balance_source_snapshot(organization=org, item=item),
            original_item_proof,
        )
        self.assertEqual(
            openings.opening_settlement_source_snapshot(
                organization=org, settlement=settlement
            ),
            original_cash_proof,
        )
        for bank in (receipt_bank, refund_bank):
            bank.refresh_from_db()
            self.assertEqual(
                (
                    bank.organization_id,
                    bank.account_id,
                    bank.date,
                    bank.amount,
                    bank.reference,
                    bank.description,
                ),
                physical_sources[bank.pk],
            )
            self.assertEqual(bank.unallocated_amount, Decimal(0))
        self.assertFalse(ledger.pending_bank_transactions(organization=org).exists())
        balances = ledger.report_balances(organization=org, as_of=refund_day)
        self.assertEqual(
            (balances["BANK"], balances["AR"], balances["EQUITY"]),
            (Decimal(1250), Decimal(50), Decimal(-1300)),
        )
        self.assertEqual(
            (balances["REVENUE"], balances["EXPENSE"], balances["DEFERRED_REVENUE"]),
            (0, 0, 0),
        )
        self.assertEqual(sum(balances.values()), Decimal(0))
        updated = self.client.get(detail_url)
        self.assertContains(updated, "Pengembalian uang dicocokkan")
        self.assertContains(updated, "Pembayaran dicocokkan")
        self.assertContains(
            updated, reverse("opening_balance_tax:review", args=[settlement.pk])
        )
        refund_tax_url = f"/taxes/opening-balances/refunds/{refund.pk}/review/"
        self.assertContains(updated, 'href="' + refund_tax_url + '"')
        self.assertContains(updated, "Periksa pajak pengembalian uang")
        self.assertEqual(len(updated.context["refunds"]), 1)
        self.assertEqual(updated.context["refunds"][0]["refund"].pk, refund.pk)
        self.assertEqual(updated.context["refunds"][0]["tax_status"], "pending")
        self.assertEqual(updated.context["position"]["remaining"], Decimal(50))
        before_history = counts()
        historical = self.client.get(detail_url, {"as_of": "2026-09-11"})
        self.assertEqual(historical.status_code, 200)
        self.assertEqual(historical.context["position"]["remaining"], Decimal(0))
        self.assertEqual(historical.context["refunds"], [])
        self.assertNotContains(historical, "Pengembalian uang dicocokkan")
        self.assertNotContains(historical, 'href="' + refund_url + '"')
        self.assertNotContains(historical, 'href="' + refund_tax_url + '"')
        self.assertEqual(counts(), before_history)
