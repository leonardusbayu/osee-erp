"""Public opening principal, actual cash, scope and dated conservation contracts."""

from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.test import TestCase
from django.utils import timezone

from core.models import AuditEvent, DomainEvent, Membership, Organization
from taxes.models import TaxObligation

from . import opening_balances as opening
from . import services as ledger
from . import tests_remediation as fixtures
from .models import (
    AccountingPeriod,
    BankAccount,
    BankPosting,
    BankTransaction,
    Bill,
    BillPayment,
    Invoice,
    Journal,
    OpeningBalanceItem,
    OpeningBalanceSettlement,
    Party,
)


class OpeningBalanceFixtures:
    movement = fixtures.FinanceRemediationTests.movement
    invoice = fixtures.FinanceRemediationTests.invoice

    def setUp(self):
        fixtures.FinanceRemediationTests.setUp(self)
        self.day, self.cutoff = date(2026, 9, 10), date(2026, 9, 1)
        self.source_day = date(2026, 8, 20)

    def role(self, name):
        actor = get_user_model().objects.create_user(
            username=f"opening-{name}-{uuid4()}"
        )
        Membership.objects.create(organization=self.org, user=actor, role=name)
        return actor

    def record_data(self, **changes):
        data = dict(
            organization=self.org,
            actor=self.actor,
            kind="receivable",
            party=self.party,
            date=self.cutoff,
            source_date=self.source_day,
            source_reference=f"LEGACY-{uuid4()}",
            amount="300",
            reason="Synthetic outstanding principal verified outside this app.",
            evidence="Synthetic original document and opening reconciliation.",
            request_key=str(uuid4()),
            confirmed=True,
        )
        data.update(changes)
        keys = ("kind", "party", "date", "source_date", "source_reference", "amount")
        data.setdefault(
            "expected_fingerprint",
            opening.opening_balance_action_context(
                organization=self.org,
                action="record",
                **{key: data[key] for key in keys},
            )["fingerprint"],
        )
        return data

    def item(self, **changes):
        return opening.record_opening_balance(**self.record_data(**changes))

    def settlement_data(self, item, bank, **changes):
        data = dict(
            organization=self.org,
            actor=self.actor,
            item=item,
            transaction=bank,
            amount="120",
            reason="Synthetic exact allocation of actual bank cash to old principal.",
            evidence="Synthetic actual bank evidence and customer/supplier confirmation.",
            request_key=str(uuid4()),
            confirmed=True,
        )
        data.update(changes)
        data.setdefault(
            "expected_fingerprint",
            opening.opening_balance_action_context(
                organization=self.org,
                action="settle",
                item=data["item"],
                transaction=data["transaction"],
                amount=data["amount"],
            )["fingerprint"],
        )
        return data

    def settle(self, item, bank, **changes):
        return opening.settle_opening_balance(
            **self.settlement_data(item, bank, **changes)
        )

    def position(self, item, as_of=None):
        return opening.opening_balance_position(
            organization=self.org, item=item, as_of=as_of or self.day
        )

    def counts(self):
        return tuple(
            model.objects.count()
            for model in (
                Journal,
                BankPosting,
                OpeningBalanceItem,
                OpeningBalanceSettlement,
                Invoice,
                Bill,
                BillPayment,
                BankTransaction,
                TaxObligation,
                AccountingPeriod,
                AuditEvent,
                DomainEvent,
            )
        )

    def corrupt(self, model, pk, field, value):
        # Explicit adversarial persisted-source fixture, not a routine ORM edit.
        table = connection.ops.quote_name(model._meta.db_table)
        column = connection.ops.quote_name(model._meta.get_field(field).column)
        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {table} SET {column} = %s WHERE id = %s", [value, pk]
            )


class OpeningBalanceTests(OpeningBalanceFixtures, TestCase):
    def test_opening_only_preserves_old_principal_without_new_sale_cost_or_cash(self):
        receivable = self.item()
        payable = self.item(kind="payable", party=self.supplier, amount="200")
        balances = ledger.report_balances(organization=self.org)
        self.assertEqual(
            (balances["AR"], balances["AP"], balances["EQUITY"], balances["BANK"]),
            (Decimal(300), Decimal(-200), Decimal(-100), 0),
        )
        self.assertEqual(
            tuple(
                balances[key]
                for key in (
                    "REVENUE",
                    "EXPENSE",
                    "DEFERRED_REVENUE",
                    "PREPAID",
                    "TAX_PAYABLE",
                )
            ),
            (0, 0, 0, 0, 0),
        )
        self.assertEqual(
            (
                self.position(receivable)["remaining"],
                self.position(payable)["remaining"],
            ),
            (Decimal(300), Decimal(200)),
        )
        for model in (Invoice, Bill, BankPosting, BankTransaction, TaxObligation):
            self.assertFalse(model.objects.exists())

    def test_bank_1000_ar_300_ap_200_then_receipt_120_payment_50_conserve_dates(self):
        ledger.record_bank_opening(
            organization=self.org,
            actor=self.actor,
            account=self.account,
            date=self.cutoff,
            amount="1000",
            reference="SYN-OPENING-BANK",
            evidence="Synthetic reviewed start-of-day bank statement.",
        )
        ar, ap = (
            self.item(),
            self.item(kind="payable", party=self.supplier, amount="200"),
        )
        self.settle(ar, self.movement("120", reference="SYN-AR-CASH"))
        self.settle(ap, self.movement("-50", reference="SYN-AP-CASH"), amount="50")
        for cutoff, expected in (
            (date(2026, 8, 31), (0, 0, 0, 0)),
            (
                date(2026, 9, 9),
                (Decimal(1000), Decimal(300), Decimal(-200), Decimal(-1100)),
            ),
            (self.day, (Decimal(1070), Decimal(180), Decimal(-150), Decimal(-1100))),
        ):
            with self.subTest(cutoff=cutoff):
                balances = ledger.report_balances(organization=self.org, as_of=cutoff)
                self.assertEqual(
                    tuple(balances[key] for key in ("BANK", "AR", "AP", "EQUITY")),
                    expected,
                )
                self.assertEqual(sum(balances.values()), 0)
                self.assertEqual(
                    (
                        balances["REVENUE"],
                        balances["EXPENSE"],
                        balances["DEFERRED_REVENUE"],
                    ),
                    (0, 0, 0),
                )
                facts = opening.opening_balance_close_facts(
                    organization=self.org, cutoff=cutoff
                )
                self.assertFalse(facts["blockers"])
                self.assertEqual(
                    (facts["receivable"], -facts["payable"]),
                    (balances["AR"], balances["AP"]),
                )
        self.assertEqual(
            (
                Invoice.objects.count(),
                Bill.objects.count(),
                BillPayment.objects.count(),
                TaxObligation.objects.count(),
            ),
            (0, 0, 0, 0),
        )
        self.assertEqual(
            (BankPosting.objects.count(), OpeningBalanceSettlement.objects.count()),
            (2, 2),
        )

    def test_partial_actual_cash_can_be_recorded_with_unknown_tax_without_zero_decision(
        self,
    ):
        row = self.item(kind="payable", party=self.supplier, amount="200")
        bank = self.movement("-80", reference="PENDING-TAX-REAL-CASH")
        event = self.settle(row, bank, amount="80", actor=self.role("finance"))
        self.assertEqual(
            (self.position(row)["remaining"], bank.unallocated_amount),
            (Decimal(120), 0),
        )
        self.assertEqual(event.bank_posting.kind, "opening_payment")
        self.assertFalse(TaxObligation.objects.exists())
        self.assertEqual(
            ledger.report_balances(organization=self.org)["TAX_PAYABLE"], 0
        )

    def test_owner_only_creation_and_active_finance_cash_matching(self):
        data = self.record_data()
        for role in ("finance", "marketing", "reviewer", "auditor", "director"):
            with self.subTest(role=role), self.assertRaises(PermissionDenied):
                opening.record_opening_balance(**{**data, "actor": self.role(role)})
        with self.assertRaises(PermissionDenied):
            opening.record_opening_balance(**{**data, "actor": None})
        row = opening.record_opening_balance(**data)
        bank = self.movement("120", reference="ROLE-CASH")
        data = self.settlement_data(row, bank)
        for role in ("marketing", "reviewer", "auditor", "director"):
            with self.subTest(role=role), self.assertRaises(PermissionDenied):
                opening.settle_opening_balance(**{**data, "actor": self.role(role)})
        finance = self.role("finance")
        self.settle(row, bank, actor=finance)
        Membership.objects.filter(organization=self.org, user=finance).update(
            is_active=False
        )
        with self.assertRaises(PermissionDenied):
            opening.settle_opening_balance(
                **self.settlement_data(row, bank, actor=finance)
            )

    def test_authorization_precedes_exact_replay(self):
        data = self.record_data()
        row = opening.record_opening_balance(**data)
        membership = Membership.objects.get(organization=self.org, user=self.actor)
        membership.is_active = False
        membership.save()
        before = self.counts()
        with self.assertRaises(PermissionDenied):
            opening.record_opening_balance(**data)
        self.assertEqual(self.counts(), before)
        self.assertTrue(self.position(row)["integrity"])

    def test_record_and_cash_exact_replay_after_close_have_no_writes(self):
        data = self.record_data()
        row = opening.record_opening_balance(**data)
        bank = self.movement("300", reference="EXHAUST-RETRY")
        cash_data = self.settlement_data(row, bank, amount="300")
        cash = opening.settle_opening_balance(**cash_data)
        AccountingPeriod.objects.update_or_create(
            organization=self.org, year=2026, month=10, defaults={"closed": True}
        )
        before = self.counts()
        self.assertEqual(opening.record_opening_balance(**data).pk, row.pk)
        self.assertEqual(opening.settle_opening_balance(**cash_data).pk, cash.pk)
        self.assertEqual(self.counts(), before)
        for changed in (
            {"amount": "299"},
            {"evidence": "Different original document"},
            {"source_reference": "OTHER-SOURCE"},
        ):
            with self.subTest(changed=changed), self.assertRaises(ValidationError):
                opening.record_opening_balance(**{**data, **changed})
        for changed in ({"amount": "299"}, {"reason": "Different allocation"}):
            with self.subTest(changed=changed), self.assertRaises(ValidationError):
                opening.settle_opening_balance(**{**cash_data, **changed})
        self.assertEqual(self.counts(), before)

    def test_changed_new_source_after_close_is_rejected_atomically(self):
        row = self.item()
        bank = self.movement("100", reference="CLOSE-PREPARED")
        cash_data = self.settlement_data(row, bank, amount="100")
        AccountingPeriod.objects.update_or_create(
            organization=self.org, year=2026, month=10, defaults={"closed": True}
        )
        before = self.counts()
        with self.assertRaises(ValidationError):
            opening.settle_opening_balance(**cash_data)
        with self.assertRaises(ValidationError):
            self.item()
        self.assertEqual(self.counts(), before)

    def test_document_unicode_case_whitespace_identity_does_not_duplicate(self):
        row = self.item(source_reference=" Legacy  ABC-1 ")
        before = self.counts()
        with self.assertRaises(ValidationError):
            self.item(source_reference="ＬＥＧＡＣＹ ABC-1")
        self.assertEqual(self.counts(), before)
        self.assertEqual(row.source_identity, "legacy abc-1")
        other = Party.objects.create(
            organization=self.org, kind="customer", name="Synthetic second customer"
        )
        self.item(party=other, source_reference="legacy ABC-1")

    def test_opening_rejects_document_already_in_ordinary_invoice_or_bill(self):
        self.invoice(number="OLD-INVOICE")
        ledger.create_bill(
            organization=self.org,
            actor=self.actor,
            supplier=self.supplier,
            date=self.day,
            service_date=self.day,
            amount="200",
            number="OLD-BILL",
            supplier_reference="PROVIDER-OLD",
        )
        before = self.counts()
        for changes in (
            {"source_reference": " old-invoice "},
            {
                "kind": "payable",
                "party": self.supplier,
                "source_reference": " provider-old ",
            },
            {"kind": "payable", "party": self.supplier, "source_reference": "old-bill"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.item(**changes)
        self.assertEqual(self.counts(), before)

    def test_ordinary_creation_after_opening_rejects_explicit_duplicate_documents(self):
        self.item(source_reference="OLD-INVOICE")
        self.item(kind="payable", party=self.supplier, source_reference="PROVIDER-OLD")
        before = self.counts()
        with self.assertRaises(ValidationError):
            self.invoice(number=" old-invoice ")
        with self.assertRaises(ValidationError):
            ledger.create_bill(
                organization=self.org,
                actor=self.actor,
                supplier=self.supplier,
                date=self.day,
                service_date=self.day,
                amount="200",
                supplier_reference="provider-old",
            )
        with self.assertRaises(ValidationError):
            ledger.create_bill(
                organization=self.org,
                actor=self.actor,
                supplier=self.supplier,
                date=self.day,
                service_date=self.day,
                amount="200",
                number=" provider-old ",
            )
        self.assertEqual(self.counts(), before)

    def test_automatic_ordinary_numbers_skip_reserved_opening_document(self):
        self.item(source_reference="INV-202609-00001")
        invoice = self.invoice()
        self.assertEqual(invoice.number, "INV-202609-00002")
        self.assertEqual(
            self.position(OpeningBalanceItem.objects.get())["remaining"], Decimal(300)
        )

    def test_common_cutoff_and_source_before_cutoff_are_required(self):
        self.item()
        before = self.counts()
        for changes in (
            {"date": date(2026, 9, 2)},
            {"date": date(2026, 8, 31)},
            {"source_date": date(2026, 9, 2)},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.item(**changes)
        self.assertEqual(self.counts(), before)

    def test_cutoff_cannot_follow_first_posted_journal(self):
        ledger.post_journal(
            organization=self.org,
            actor=self.actor,
            date=date(2026, 8, 31),
            source_key="EARLIER-LEDGER",
            description="Synthetic earlier actual owner funding.",
            entries=[("BANK", "10", "0"), ("EQUITY", "0", "10")],
        )
        before = self.counts()
        with self.assertRaises(ValidationError):
            self.item()
        self.assertEqual(self.counts(), before)
        self.item(date=date(2026, 8, 31))

    def test_invalid_amount_future_source_and_party_kind_reject_without_writes(self):
        before = self.counts()
        for changes in (
            {"amount": "0"},
            {"amount": "-1"},
            {"amount": True},
            {"amount": 1.0},
            {"amount": "1.001"},
            {"date": timezone.localdate() + timedelta(days=1)},
            {"source_date": timezone.localdate() + timedelta(days=1)},
            {"party": self.supplier},
            {"kind": "payable"},
            {"source_reference": "\t\n"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.item(**changes)
        self.assertEqual(self.counts(), before)

    def test_foreign_item_party_and_bank_are_unavailable(self):
        row = self.item()
        other = Organization.objects.create(name="Synthetic foreign company")
        Membership.objects.create(organization=other, user=self.actor, role="owner")
        foreign = Party.objects.create(
            organization=other, name="Foreign customer", kind="customer"
        )
        bank = self.movement("120", reference="FOREIGN-CONTEXT")
        before = self.counts()
        with self.assertRaises(ValidationError):
            self.item(party=foreign)
        with self.assertRaises(ValidationError):
            opening.settle_opening_balance(
                **{**self.settlement_data(row, bank), "organization": other}
            )
        context = opening.opening_balance_action_context(
            organization=other, action="settle", item=row, transaction=bank
        )
        self.assertFalse(context["current"])
        self.assertIsNone(context["remaining"])
        self.assertEqual(self.counts(), before)

    def test_wrong_cash_direction_and_before_cutoff_reject_atomically(self):
        row = self.item()
        for bank in (
            self.movement("-120", reference="WRONG-DIRECTION"),
            self.movement("120", day=date(2026, 8, 31), reference="BEFORE-CUTOFF"),
        ):
            before = self.counts()
            with self.assertRaises(ValidationError):
                self.settle(row, bank)
            self.assertEqual(self.counts(), before)

    def test_partial_full_and_split_bank_allocation_conserve_cash_once(self):
        first, second = self.item(amount="100"), self.item(amount="100")
        bank = self.movement("200", reference="SPLIT-CASH")
        self.settle(first, bank, amount="40")
        self.settle(first, bank, amount="60")
        self.settle(second, bank, amount="100")
        self.assertEqual(
            (
                self.position(first)["remaining"],
                self.position(second)["remaining"],
                bank.unallocated_amount,
            ),
            (0, 0, 0),
        )
        self.assertEqual(
            ledger.report_balances(organization=self.org)["BANK"], Decimal(200)
        )
        self.assertEqual(
            (BankPosting.objects.count(), OpeningBalanceSettlement.objects.count()),
            (3, 3),
        )

    def test_late_import_cannot_displace_already_recorded_later_principal(self):
        row = self.item(amount="100")
        self.settle(
            row,
            self.movement("80", day=date(2026, 9, 20), reference="LATER-CASH"),
            amount="80",
        )
        bank = self.movement("30", day=date(2026, 9, 10), reference="EARLIER-CASH")
        before = self.counts()
        with self.assertRaises(ValidationError):
            self.settle(row, bank, amount="30")
        self.assertEqual(self.counts(), before)
        self.settle(row, bank, amount="20")
        self.assertEqual(
            self.position(row, date(2026, 9, 10))["remaining"], Decimal(80)
        )
        self.assertEqual(self.position(row, date(2026, 9, 20))["remaining"], 0)
        self.assertEqual(bank.unallocated_amount, Decimal(10))

    def test_stale_competing_bank_claim_and_unconfirmed_action_have_no_writes(self):
        row = self.item()
        bank = self.movement("120", reference="STALE-CASH")
        data = self.settlement_data(row, bank)
        before = self.counts()
        with self.assertRaises(ValidationError):
            opening.settle_opening_balance(**{**data, "confirmed": False})
        self.assertEqual(self.counts(), before)
        ledger.reconcile_bank_adjustment(
            organization=self.org,
            actor=self.actor,
            transaction=bank,
            kind="owner_funding",
            amount="10",
            reference="COMPETING-CLAIM",
            evidence="Synthetic competing actual funding.",
        )
        before = self.counts()
        with self.assertRaises(ValidationError):
            opening.settle_opening_balance(**{**data, "amount": "100"})
        self.assertEqual(self.counts(), before)
        self.settle(row, bank, amount="100")
        self.assertEqual(bank.unallocated_amount, Decimal(10))

    def test_account_and_party_identity_changes_invalidate_viewed_preview(self):
        row = self.item()
        bank = self.movement("120", reference="IDENTITY-CASH")
        data = self.settlement_data(row, bank)
        self.account.name = "Reviewed bank display identity changed"
        self.account.save()
        before = self.counts()
        with self.assertRaises(ValidationError):
            opening.settle_opening_balance(**data)
        self.assertEqual(self.counts(), before)
        data = self.settlement_data(row, bank)
        self.party.tax_id = "SYN-CHANGED-IDENTITY"
        self.party.save()
        with self.assertRaises(ValidationError):
            opening.settle_opening_balance(**data)
        self.assertEqual(self.counts(), before)

    def test_immutable_original_receipts_do_not_include_new_cash_or_remaining(self):
        row = self.item()
        original = opening.opening_balance_source_snapshot(
            organization=self.org, item=row
        )
        cash = self.settle(row, self.movement("120", reference="ORIGINAL-STABILITY"))
        receipt = opening.opening_settlement_source_snapshot(
            organization=self.org, settlement=cash
        )
        self.settle(
            row, self.movement("180", reference="LATER-STABILITY"), amount="180"
        )
        self.assertEqual(
            opening.opening_balance_source_snapshot(organization=self.org, item=row),
            original,
        )
        self.assertEqual(
            opening.opening_settlement_source_snapshot(
                organization=self.org, settlement=cash
            ),
            receipt,
        )
        self.assertEqual(self.position(row)["remaining"], 0)

    def test_events_are_immutable_and_do_not_retain_service_privilege(self):
        row = self.item()
        cash = self.settle(row, self.movement("120", reference="IMMUTABLE-CASH"))
        for event in (row, cash):
            self.assertFalse(hasattr(event, "_service_transition"))
            event.reason = "Changed historic reason"
            with self.assertRaises(ValidationError):
                event.save()
            with self.assertRaises(ValidationError):
                type(event).objects.filter(pk=event.pk).update(amount=Decimal(1))
            with self.assertRaises(ValidationError):
                event.delete()

    def test_tampered_old_cutoff_cannot_hide_from_read_or_close(self):
        row = self.item()
        self.corrupt(OpeningBalanceItem, row.pk, "date", date(2026, 10, 1))
        position = self.position(row)
        facts = opening.opening_balance_close_facts(
            organization=self.org, cutoff=date(2026, 9, 30)
        )
        self.assertFalse(position["integrity"])
        self.assertIsNone(position["remaining"])
        self.assertIsNone(facts["receivable"])
        self.assertTrue(facts["blockers"])
        with self.assertRaises(ValidationError):
            opening.opening_balance_source_snapshot(organization=self.org, item=row)

    def test_broken_cash_journal_holds_earlier_close_and_has_repair_identity(self):
        row = self.item()
        cash = self.settle(row, self.movement("120", reference="BROKEN-CASH"))
        line = cash.journal.lines.get(account="AR")
        self.corrupt(type(line), line.pk, "account", "EXPENSE")
        graph_before = opening.opening_settlement_source_graph(
            organization=self.org, settlement=cash
        )
        self.assertFalse(self.position(row)["integrity"])
        with self.assertRaises(ValidationError):
            opening.opening_settlement_source_snapshot(
                organization=self.org, settlement=cash
            )
        self.corrupt(type(line), line.pk, "account", "PREPAID")
        graph_after = opening.opening_settlement_source_graph(
            organization=self.org, settlement=cash
        )
        self.assertNotEqual(graph_before["fingerprint"], graph_after["fingerprint"])
        self.assertTrue(
            opening.opening_balance_close_facts(
                organization=self.org, cutoff=date(2026, 9, 9)
            )["blockers"]
        )

    def test_broken_item_journal_repair_identity_changes_without_cash(self):
        row = self.item()
        line = row.journal.lines.get(account="AR")
        self.corrupt(type(line), line.pk, "account", "EXPENSE")
        before = opening.opening_balance_source_graph(organization=self.org, item=row)[
            "fingerprint"
        ]
        self.corrupt(type(line), line.pk, "account", "PREPAID")
        after = opening.opening_balance_source_graph(organization=self.org, item=row)[
            "fingerprint"
        ]
        self.assertNotEqual(before, after)
        self.assertIsNone(self.position(row)["remaining"])

    def test_original_cash_proof_stays_stable_for_valid_sibling_but_holds_broken_shared_allocation(
        self,
    ):
        row = self.item()
        bank = self.movement("200", reference="SHARED-ALLOCATION-CASH")
        cash = self.settle(row, bank, amount="100")
        original = opening.opening_settlement_source_snapshot(
            organization=self.org, settlement=cash
        )
        invoice = ledger.issue_invoice(
            organization=self.org, actor=self.actor, invoice=self.invoice()
        )
        allocation = ledger.reconcile_receipt(
            organization=self.org,
            actor=self.actor,
            invoice=invoice,
            transaction=bank,
            amount="100",
        )
        self.assertEqual(
            opening.opening_settlement_source_snapshot(
                organization=self.org, settlement=cash
            ),
            original,
        )
        journal = Journal.objects.get(
            organization=self.org, source_key=f"allocation:{allocation.pk}"
        )
        line = journal.lines.get(account="AR")
        self.corrupt(type(line), line.pk, "account", "EXPENSE")
        before = opening.opening_settlement_source_graph(
            organization=self.org, settlement=cash
        )["fingerprint"]
        with self.assertRaises(ValidationError):
            opening.opening_settlement_source_snapshot(
                organization=self.org, settlement=cash
            )
        self.corrupt(type(line), line.pk, "account", "PREPAID")
        after = opening.opening_settlement_source_graph(
            organization=self.org, settlement=cash
        )["fingerprint"]
        self.assertNotEqual(before, after)
        self.assertIsNone(self.position(row)["remaining"])

    def test_removed_cash_decision_and_claim_do_not_restore_principal_from_orphan_journal(
        self,
    ):
        row = self.item()
        cash = self.settle(row, self.movement("120", reference="REMOVED-TYPED-CASH"))
        with connection.cursor() as cursor:
            for model, pk in (
                (OpeningBalanceSettlement, cash.pk),
                (BankPosting, cash.bank_posting_id),
            ):
                table = connection.ops.quote_name(model._meta.db_table)
                cursor.execute(f"DELETE FROM {table} WHERE id = %s", [pk])
        self.assertIsNone(self.position(row)["remaining"])
        facts = opening.opening_balance_close_facts(
            organization=self.org, cutoff=self.day
        )
        self.assertTrue(facts["blockers"])
        self.assertIsNone(facts["receivable"])

    def test_broken_current_cash_link_retains_original_bank_and_account_repair_anchors(
        self,
    ):
        row = self.item()
        bank = self.movement("120", reference="ORIGINAL-CASH-ANCHOR")
        cash = self.settle(row, bank)
        other_account = BankAccount.objects.create(
            organization=self.org,
            name="Synthetic alternate bank",
            account_number="SYN-ALT-ANCHOR",
        )
        alternate = self.movement(
            "20", reference="ALTERNATE-CASH-ANCHOR", account=other_account
        )
        replacement = ledger.reconcile_bank_adjustment(
            organization=self.org,
            actor=self.actor,
            transaction=alternate,
            kind="owner_funding",
            amount="20",
            reference="ALTERNATE-POSTING-ANCHOR",
            evidence="Synthetic separately evidenced owner funding.",
        )
        self.corrupt(OpeningBalanceSettlement, cash.pk, "bank_posting", replacement.pk)
        before = opening.opening_settlement_source_graph(
            organization=self.org, settlement=cash
        )["fingerprint"]
        self.corrupt(
            BankTransaction,
            bank.pk,
            "description",
            "Original bank proof changed while current FK remains wrong",
        )
        after_bank = opening.opening_settlement_source_graph(
            organization=self.org, settlement=cash
        )["fingerprint"]
        self.assertNotEqual(before, after_bank)
        self.account.name = (
            "Original account identity changed while current FK remains wrong"
        )
        self.account.save()
        after_account = opening.opening_settlement_source_graph(
            organization=self.org, settlement=cash
        )["fingerprint"]
        self.assertNotEqual(after_bank, after_account)
        with self.assertRaises(ValidationError):
            opening.opening_settlement_source_snapshot(
                organization=self.org, settlement=cash
            )
        self.assertIsNone(self.position(row)["remaining"])

    def test_relocated_cash_live_scope_cannot_disappear_from_original_principal_history(
        self,
    ):
        row = self.item()
        bank = self.movement("120", reference="SYN-RELOCATED-ORIGINAL-CASH")
        cash = self.settle(row, bank)
        original_hash, original_snapshot = cash.source_hash, cash.source_snapshot
        foreign = Organization.objects.create(
            name="Synthetic relocated cash destination"
        )
        Membership.objects.create(organization=foreign, user=self.actor, role="owner")
        party = Party.objects.create(
            organization=foreign,
            name="Synthetic foreign principal party",
            kind="customer",
        )
        values = dict(
            kind="receivable",
            party=party,
            date=self.cutoff,
            source_date=self.source_day,
            source_reference="SYN-FOREIGN-PRINCIPAL",
            amount="10",
        )
        context = opening.opening_balance_action_context(
            organization=foreign, action="record", **values
        )
        foreign_item = opening.record_opening_balance(
            organization=foreign,
            actor=self.actor,
            **values,
            reason="Synthetic independently evidenced foreign principal.",
            evidence="Synthetic foreign original source.",
            request_key=str(uuid4()),
            expected_fingerprint=context["fingerprint"],
            confirmed=True,
        )
        self.corrupt(OpeningBalanceSettlement, cash.pk, "item", foreign_item.pk)
        for model, pk in (
            (OpeningBalanceSettlement, cash.pk),
            (BankPosting, cash.bank_posting_id),
            (BankTransaction, bank.pk),
            (Journal, cash.journal_id),
        ):
            self.corrupt(model, pk, "organization", foreign.pk)
        cash.refresh_from_db()
        self.assertEqual(
            (cash.source_hash, cash.source_snapshot), (original_hash, original_snapshot)
        )
        facts = opening.opening_balance_close_facts(
            organization=self.org, cutoff=date(2026, 9, 30)
        )
        self.assertEqual((facts["receivable"], facts["payable"]), (None, None))
        self.assertTrue(facts["blockers"])
        self.assertFalse(self.position(row)["integrity"])
        self.assertIsNone(self.position(row)["remaining"])

    def test_incomplete_preview_is_read_only_and_explains_missing_selection(self):
        before = self.counts()
        for action in ("record", "settle", "unknown"):
            context = opening.opening_balance_action_context(
                organization=self.org, action=action
            )
            self.assertFalse(context["current"])
            self.assertTrue(context["blockers"])
            self.assertIsNone(context["remaining"])
        self.assertEqual(self.counts(), before)
