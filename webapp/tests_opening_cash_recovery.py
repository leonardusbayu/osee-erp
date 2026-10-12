"""Public staff recovery keeps original cash proof and releases a wrong match."""

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
    OpeningBalanceCorrection,
    OpeningBalanceItem,
    OpeningBalanceSettlement,
    Party,
)


class OpeningCashRecoveryHTTPTests(TestCase):
    def test_owner_cancels_fully_settled_wrong_match_at_original_bank_date_without_new_cash(
        self,
    ):
        org = Organization.objects.create(
            name="Synthetic opening cash recovery UI", is_demo=True
        )
        owner = get_user_model().objects.create_user(
            username="opening-cash-recovery-owner"
        )
        Membership.objects.create(organization=org, user=owner, role="owner")
        self.client.force_login(owner)
        cutoff, bank_day = date(2026, 9, 1), date(2026, 9, 10)
        party = Party.objects.create(
            organization=org, name="Synthetic cash recovery reseller", kind="reseller"
        )
        account = BankAccount.objects.create(
            organization=org,
            name="Synthetic recovery bank",
            account_number="SYN-RECOVERY-UI",
        )
        ledger.record_bank_opening(
            organization=org,
            actor=owner,
            account=account,
            date=cutoff,
            amount="1000",
            reference="SYN-RECOVERY-BANK",
            evidence="Synthetic verified opening bank balance",
        )
        selection = {
            "kind": "receivable",
            "party": party,
            "date": cutoff,
            "source_date": date(2026, 8, 20),
            "source_reference": "SYN-RECOVERY-OLD-AR",
            "amount": "300",
        }
        context = openings.opening_balance_action_context(
            organization=org, action="record", **selection
        )
        item = openings.record_opening_balance(
            organization=org,
            actor=owner,
            **selection,
            reason="Synthetic original outstanding principal checked",
            evidence="Synthetic old document and outstanding list",
            request_key=str(uuid4()),
            expected_fingerprint=context["fingerprint"],
            confirmed=True,
        )
        ledger.import_bank_csv(
            organization=org,
            actor=owner,
            account=account,
            content=f"date,reference,description,amount\n{bank_day},SYN-RECOVERY-IN,Synthetic original receipt,300\n",
        )
        bank = BankTransaction.objects.get(
            organization=org, reference="SYN-RECOVERY-IN"
        )
        context = openings.opening_balance_action_context(
            organization=org, action="settle", item=item, transaction=bank, amount="300"
        )
        settlement = openings.settle_opening_balance(
            organization=org,
            actor=owner,
            item=item,
            transaction=bank,
            amount="300",
            reason="Synthetic original match to the wrong document",
            evidence="Synthetic original receipt and document checked",
            request_key=str(uuid4()),
            expected_fingerprint=context["fingerprint"],
            confirmed=True,
        )
        original_proof = deepcopy(
            openings.opening_settlement_source_snapshot(
                organization=org, settlement=settlement
            )
        )
        original_item = (
            item.amount,
            item.date,
            item.source_date,
            item.source_reference,
            item.party_id,
            item.source_hash,
            deepcopy(item.source_snapshot),
        )
        original_settlement = (
            settlement.amount,
            settlement.date,
            settlement.bank_posting_id,
            settlement.journal_id,
            settlement.source_hash,
            deepcopy(settlement.source_snapshot),
        )

        def counts():
            return tuple(
                model.objects.count()
                for model in (
                    OpeningBalanceItem,
                    OpeningBalanceCorrection,
                    OpeningBalanceSettlement,
                    BankPosting,
                    BankTransaction,
                    Journal,
                    JournalLine,
                    Invoice,
                    Bill,
                    AuditEvent,
                    DomainEvent,
                )
            )

        detail_url = reverse("opening_balance_detail", args=[item.pk])
        cancel_url = f"/finance/opening-balance-settlements/{settlement.pk}/cancel/"
        before = counts()
        detail = self.client.get(detail_url)
        self.assertEqual(detail.status_code, 200)
        self.assertTrue(detail.context["position"]["exhausted"])
        self.assertContains(detail, 'href="' + cancel_url + '"')
        self.assertContains(detail, "Batalkan pencocokan")
        self.assertEqual(counts(), before)

        page = self.client.get(cancel_url)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Batalkan pencocokan")
        self.assertContains(page, "Periksa sumber")
        self.assertContains(page, "Konfirmasi dan simpan")
        for fixed in ("amount", "date", "party", "item", "transaction"):
            self.assertNotIn(fixed, page.context["form"].fields)
        self.assertEqual(page.context["source_context"]["date"], bank_day)
        self.assertEqual(page.context["source_context"]["amount"], Decimal(300))
        self.assertEqual(counts(), before)
        data = {
            "reason": "Pencocokan ke dokumen ini keliru",
            "evidence": "Rekening koran asli dan dokumen yang benar sudah diperiksa",
        }
        checked = self.client.post(cancel_url, {**data, "intent": "check"})
        self.assertEqual(checked.status_code, 200)
        self.assertTrue(
            checked.context["save_available"], checked.context["source_context"]
        )
        self.assertEqual(checked.context["source_context"]["date"], bank_day)
        self.assertEqual(checked.context["source_context"]["amount"], Decimal(300))
        self.assertFalse(checked.context["form"]["confirmed"].value())
        self.assertEqual(counts(), before)
        form = checked.context["form"]
        saved = self.client.post(
            cancel_url,
            {
                **data,
                "intent": "save",
                "confirmed": "on",
                "request_key": form["request_key"].value(),
                "expected_fingerprint": form["expected_fingerprint"].value(),
            },
        )
        self.assertRedirects(saved, detail_url)

        item.refresh_from_db()
        settlement.refresh_from_db()
        bank.refresh_from_db()
        self.assertEqual(
            (
                item.amount,
                item.date,
                item.source_date,
                item.source_reference,
                item.party_id,
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
            original_settlement,
        )
        self.assertEqual(
            openings.opening_settlement_source_snapshot(
                organization=org, settlement=settlement
            ),
            original_proof,
        )
        self.assertEqual((bank.amount, bank.date), (Decimal(300), bank_day))
        self.assertEqual(BankTransaction.objects.count(), 1)
        self.assertEqual(OpeningBalanceSettlement.objects.count(), 1)
        self.assertEqual(OpeningBalanceCorrection.objects.count(), 0)
        self.assertEqual((Invoice.objects.count(), Bill.objects.count()), (0, 0))
        available = ledger.pending_bank_transactions(organization=org).get(pk=bank.pk)
        self.assertEqual(available.remaining_amount, Decimal(300))
        position = openings.opening_balance_position(
            organization=org, item=item, as_of=bank_day
        )
        self.assertTrue(position["current"], position)
        self.assertEqual(
            (position["opening"], position["settled"], position["remaining"]),
            (Decimal(300), 0, Decimal(300)),
        )
        cancelled = [event for event in position["history"] if event.action == "cancel"]
        self.assertEqual(len(cancelled), 1)
        self.assertEqual(
            (cancelled[0].amount, cancelled[0].date), (Decimal(300), bank_day)
        )
        self.assertIn(
            settlement.pk,
            [event.pk for event in position["history"] if event.action == "settle"],
        )
        balances = ledger.report_balances(organization=org, as_of=bank_day)
        self.assertEqual(
            (balances["BANK"], balances["AR"], balances["EQUITY"]),
            (Decimal(1000), Decimal(300), Decimal(-1300)),
        )
        self.assertEqual((balances["REVENUE"], balances["EXPENSE"]), (0, 0))
        self.assertEqual(sum(balances.values()), 0)
        updated = self.client.get(detail_url)
        self.assertContains(updated, "Pencocokan dibatalkan")
        self.assertContains(updated, "Pembayaran dicocokkan")
        self.assertContains(
            updated, reverse("opening_balance_tax:review", args=[settlement.pk])
        )

        before = counts()
        for role in ("finance", "auditor", "director", "marketing"):
            user = get_user_model().objects.create_user(
                username=f"opening-cash-recovery-{role}"
            )
            Membership.objects.create(organization=org, user=user, role=role)
            self.client.force_login(user)
            self.assertEqual(self.client.get(cancel_url).status_code, 403)
            self.assertEqual(
                self.client.post(cancel_url, {**data, "intent": "check"}).status_code,
                403,
            )
            reading = self.client.get(detail_url)
            self.assertEqual(reading.status_code, 403 if role == "marketing" else 200)
            if role != "marketing":
                self.assertContains(reading, "Pencocokan dibatalkan")
                self.assertNotContains(reading, 'href="' + cancel_url + '"')
        self.assertEqual(counts(), before)


class OpeningCashRecoveryPageFailureHTTPTests(TestCase):
    def setUp(self):
        from finance.models import Product

        self.org = Organization.objects.create(
            name="Synthetic damaged recovery pages", is_demo=True
        )
        self.owner = get_user_model().objects.create_user(
            username="opening-recovery-page-owner"
        )
        Membership.objects.create(organization=self.org, user=self.owner, role="owner")
        self.client.force_login(self.owner)
        self.party = Party.objects.create(
            organization=self.org,
            name="Synthetic recovery page reseller",
            kind="reseller",
        )
        self.product = Product.objects.create(
            organization=self.org,
            code="SYN-RECOVERY-PAGE",
            name="Synthetic matching product",
            kind="itp",
            default_price=Decimal(500000),
        )
        self.account = BankAccount.objects.create(
            organization=self.org,
            name="Synthetic recovery page bank",
            account_number="SYN-RECOVERY-PAGE",
        )
        self.cutoff, self.bank_day = date(2026, 9, 1), date(2026, 9, 10)

    def test_damaged_balanced_cancellation_keeps_bank_and_home_unknown_without_match_actions(
        self,
    ):
        from django.db import connection

        from finance.models import OpeningBalanceCancellation

        ledger.record_bank_opening(
            organization=self.org,
            actor=self.owner,
            account=self.account,
            date=self.cutoff,
            amount="1000",
            reference="SYN-PAGE-RECOVERY-OPENING",
            evidence="Synthetic verified original opening bank balance",
        )
        selection = dict(
            kind="receivable",
            party=self.party,
            date=self.cutoff,
            source_date=date(2026, 8, 20),
            source_reference="SYN-PAGE-RECOVERY-OLD-AR",
            amount="300",
        )
        context = openings.opening_balance_action_context(
            organization=self.org, action="record", **selection
        )
        item = openings.record_opening_balance(
            organization=self.org,
            actor=self.owner,
            **selection,
            reason="Synthetic old principal independently checked",
            evidence="Synthetic original outstanding document",
            request_key=str(uuid4()),
            expected_fingerprint=context["fingerprint"],
            confirmed=True,
        )
        ledger.import_bank_csv(
            organization=self.org,
            actor=self.owner,
            account=self.account,
            content=f"date,reference,description,amount\n{self.bank_day},SYN-PAGE-RECOVERY-CASH,Synthetic physical receipt,120\n",
        )
        bank = BankTransaction.objects.get(
            organization=self.org, reference="SYN-PAGE-RECOVERY-CASH"
        )
        context = openings.opening_balance_action_context(
            organization=self.org,
            action="settle",
            item=item,
            transaction=bank,
            amount="120",
        )
        settlement = openings.settle_opening_balance(
            organization=self.org,
            actor=self.owner,
            item=item,
            transaction=bank,
            amount="120",
            reason="Synthetic original wrong document assignment",
            evidence="Synthetic original bank proof checked",
            request_key=str(uuid4()),
            expected_fingerprint=context["fingerprint"],
            confirmed=True,
        )
        invoice = ledger.create_invoice(
            organization=self.org,
            actor=self.owner,
            party=self.party,
            product=self.product,
            quantity=1,
            date=self.bank_day,
            service_date=self.bank_day,
        )
        ledger.issue_invoice(organization=self.org, actor=self.owner, invoice=invoice)
        context = openings.opening_balance_action_context(
            organization=self.org, action="cancel", settlement=settlement
        )
        cancellation = openings.cancel_opening_settlement(
            organization=self.org,
            actor=self.owner,
            settlement=settlement,
            reason="Synthetic original receipt belongs to another document",
            evidence="Synthetic bank and both documents independently checked",
            request_key=str(uuid4()),
            expected_fingerprint=context["fingerprint"],
            confirmed=True,
        )
        self.assertEqual(bank.unallocated_amount, Decimal(120))
        known = self.client.get(reverse("bank"))
        self.assertEqual(known.status_code, 200)
        self.assertContains(
            known, 'action="' + reverse("bank_match", args=[bank.pk]) + '"'
        )

        original_bank = (bank.amount, bank.date, bank.account_id, bank.reference)
        original_cash = (
            settlement.amount,
            settlement.date,
            settlement.bank_posting_id,
            settlement.source_hash,
            deepcopy(settlement.source_snapshot),
        )
        original_cancel = (
            cancellation.amount,
            cancellation.date,
            cancellation.settlement_id,
            cancellation.source_hash,
            deepcopy(cancellation.source_snapshot),
        )
        before = tuple(
            model.objects.count()
            for model in (
                BankTransaction,
                BankPosting,
                OpeningBalanceSettlement,
                OpeningBalanceCancellation,
                Journal,
                JournalLine,
                AuditEvent,
                DomainEvent,
            )
        )
        # Persisted-source fault fixture only: totals stay balanced, but this
        # receipt now names the wrong account instead of releasing AR.
        debit = cancellation.journal.lines.get(account="AR")
        table = connection.ops.quote_name(JournalLine._meta.db_table)
        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {table} SET account = %s WHERE id = %s", ["EXPENSE", debit.pk]
            )
        self.assertEqual(
            sum(
                (line.debit - line.credit for line in cancellation.journal.lines.all()),
                Decimal(0),
            ),
            0,
        )
        held = openings.opening_balance_position(
            organization=self.org, item=item, as_of=self.bank_day
        )
        self.assertFalse(held["current"])
        self.assertIsNone(held["remaining"])
        self.assertTrue(held["blockers"])

        with self.subTest(page="bank"):
            page = self.client.get(reverse("bank"))
            self.assertEqual(page.status_code, 200)
            self.assertContains(page, "SYN-PAGE-RECOVERY-CASH")
            self.assertContains(page, "belum dapat diperiksa")
            self.assertContains(page, "Owner")
            self.assertIsNone(page.context["unmatched_count"])
            self.assertNotContains(page, "Sudah cocok")
            self.assertNotContains(
                page, 'action="' + reverse("bank_match", args=[bank.pk]) + '"'
            )
            self.assertNotContains(
                page, 'href="' + reverse("bank_adjustment", args=[bank.pk]) + '"'
            )
            self.assertNotContains(page, 'href="' + reverse("customer_advance") + '"')
            self.assertNotContains(page, "0 perlu dicocokkan")
        with self.subTest(page="home"):
            page = self.client.get(reverse("dashboard"), {"period": "2026-09"})
            self.assertEqual(page.status_code, 200)
            self.assertContains(page, "belum dapat diperiksa")
            self.assertContains(page, "Owner")
            self.assertIsNone(page.context["dashboard"]["unmatched_count"])
            self.assertNotContains(page, "0 mutasi</strong> belum dicocokkan")

        bank.refresh_from_db()
        settlement.refresh_from_db()
        cancellation.refresh_from_db()
        self.assertEqual(
            (bank.amount, bank.date, bank.account_id, bank.reference), original_bank
        )
        self.assertEqual(
            (
                settlement.amount,
                settlement.date,
                settlement.bank_posting_id,
                settlement.source_hash,
                settlement.source_snapshot,
            ),
            original_cash,
        )
        self.assertEqual(
            (
                cancellation.amount,
                cancellation.date,
                cancellation.settlement_id,
                cancellation.source_hash,
                cancellation.source_snapshot,
            ),
            original_cancel,
        )
        self.assertEqual(
            tuple(
                model.objects.count()
                for model in (
                    BankTransaction,
                    BankPosting,
                    OpeningBalanceSettlement,
                    OpeningBalanceCancellation,
                    Journal,
                    JournalLine,
                    AuditEvent,
                    DomainEvent,
                )
            ),
            before,
        )


class OpeningCashRecoveryValidationHTTPTests(TestCase):
    setUp = OpeningCashRecoveryPageFailureHTTPTests.setUp

    def original_match(
        self, *, organization=None, party=None, account=None, reference=None
    ):
        org, party, account = (
            organization or self.org,
            party or self.party,
            account or self.account,
        )
        reference = reference or f"SYN-RECOVERY-VALIDATION-{uuid4()}"
        selection = dict(
            kind="receivable",
            party=party,
            date=self.cutoff,
            source_date=date(2026, 8, 20),
            source_reference=reference,
            amount="300",
        )
        preview = openings.opening_balance_action_context(
            organization=org, action="record", **selection
        )
        item = openings.record_opening_balance(
            organization=org,
            actor=self.owner,
            **selection,
            reason="Synthetic original outstanding source reviewed",
            evidence="Synthetic original source and outstanding list",
            request_key=str(uuid4()),
            expected_fingerprint=preview["fingerprint"],
            confirmed=True,
        )
        ledger.import_bank_csv(
            organization=org,
            actor=self.owner,
            account=account,
            content=f"date,reference,description,amount\n{self.bank_day},{reference}-CASH,Synthetic actual receipt,300\n",
        )
        bank = BankTransaction.objects.get(
            organization=org, reference=reference + "-CASH"
        )
        preview = openings.opening_balance_action_context(
            organization=org, action="settle", item=item, transaction=bank, amount="300"
        )
        cash = openings.settle_opening_balance(
            organization=org,
            actor=self.owner,
            item=item,
            transaction=bank,
            amount="300",
            reason="Synthetic original wrong match",
            evidence="Synthetic original bank and old document",
            request_key=str(uuid4()),
            expected_fingerprint=preview["fingerprint"],
            confirmed=True,
        )
        return item, cash, bank

    def financial_counts(self):
        from finance.models import OpeningBalanceCancellation

        return tuple(
            model.objects.count()
            for model in (
                OpeningBalanceItem,
                OpeningBalanceCorrection,
                OpeningBalanceSettlement,
                OpeningBalanceCancellation,
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

    def checked_request(self, url):
        data = {
            "reason": "Keep this checked staff reason",
            "evidence": "Keep this reviewed bank and document evidence",
        }
        page = self.client.post(url, {**data, "intent": "check"})
        self.assertEqual(page.status_code, 200)
        self.assertTrue(page.context["save_available"], page.context["source_context"])
        form = page.context["form"]
        self.assertFalse(form["confirmed"].value())
        return {
            **data,
            "intent": "save",
            "confirmed": "on",
            "request_key": form["request_key"].value(),
            "expected_fingerprint": form["expected_fingerprint"].value(),
        }

    def test_stale_party_and_account_names_retain_inputs_until_new_check_then_cancel_once(
        self,
    ):
        from finance.models import OpeningBalanceCancellation

        for source in (self.party, self.account):
            with self.subTest(source=source.__class__.__name__):
                item, cash, bank = self.original_match()
                url = reverse("opening_balance_settlement_cancel", args=[cash.pk])
                data = self.checked_request(url)
                before = self.financial_counts()
                source.name += " reviewed name change"
                source.save()
                rejected = self.client.post(url, data)
                self.assertEqual(rejected.status_code, 400)
                self.assertFalse(rejected.context["save_available"])
                self.assertContains(
                    rejected, "Isian Anda tetap tersimpan", status_code=400
                )
                for field in (
                    "reason",
                    "evidence",
                    "request_key",
                    "expected_fingerprint",
                ):
                    self.assertEqual(
                        str(rejected.context["form"][field].value()), str(data[field])
                    )
                self.assertEqual(
                    (
                        rejected.context["source_context"]["amount"],
                        rejected.context["source_context"]["date"],
                    ),
                    (Decimal(300), self.bank_day),
                )
                self.assertEqual(self.financial_counts(), before)
                self.assertFalse(
                    OpeningBalanceCancellation.objects.filter(settlement=cash).exists()
                )
                refreshed = self.client.post(url, {**data, "intent": "check"})
                self.assertEqual(refreshed.status_code, 200)
                self.assertTrue(
                    refreshed.context["save_available"],
                    refreshed.context["source_context"],
                )
                form = refreshed.context["form"]
                self.assertEqual(form["reason"].value(), data["reason"])
                self.assertEqual(form["evidence"].value(), data["evidence"])
                self.assertNotEqual(form["request_key"].value(), data["request_key"])
                self.assertNotEqual(
                    form["expected_fingerprint"].value(), data["expected_fingerprint"]
                )
                self.assertFalse(form["confirmed"].value())
                revised = {
                    **data,
                    "request_key": form["request_key"].value(),
                    "expected_fingerprint": form["expected_fingerprint"].value(),
                }
                unconfirmed = {
                    key: value for key, value in revised.items() if key != "confirmed"
                }
                self.assertEqual(self.client.post(url, unconfirmed).status_code, 400)
                self.assertEqual(self.financial_counts(), before)
                self.assertRedirects(
                    self.client.post(url, revised),
                    reverse("opening_balance_detail", args=[item.pk]),
                )
                after = self.financial_counts()
                self.assertEqual(
                    OpeningBalanceCancellation.objects.filter(settlement=cash).count(),
                    1,
                )
                self.assertFalse(
                    OpeningBalanceCancellation.objects.filter(
                        request_key=data["request_key"]
                    ).exists()
                )
                self.assertRedirects(
                    self.client.post(url, revised),
                    reverse("opening_balance_detail", args=[item.pk]),
                )
                self.assertEqual(self.financial_counts(), after)
                self.assertEqual(bank.unallocated_amount, Decimal(300))
                position = openings.opening_balance_position(
                    organization=self.org, item=item
                )
                self.assertEqual(
                    (position["opening"], position["settled"], position["remaining"]),
                    (Decimal(300), 0, Decimal(300)),
                )

    def test_cancel_scope_csrf_fixed_fields_duplicates_and_historical_details_reject_writes(
        self,
    ):
        from django.http import QueryDict
        from django.test import Client

        item, cash, bank = self.original_match()
        foreign = Organization.objects.create(
            name="PRIVATE RECOVERY COMPANY", is_demo=True
        )
        Membership.objects.create(organization=foreign, user=self.owner, role="owner")
        private_party = Party.objects.create(
            organization=foreign, name="PRIVATE RECOVERY PARTY", kind="reseller"
        )
        private_account = BankAccount.objects.create(
            organization=foreign,
            name="PRIVATE RECOVERY BANK",
            account_number="PRIVATE-SYN",
        )
        private_item, private_cash, _ = self.original_match(
            organization=foreign,
            party=private_party,
            account=private_account,
            reference="PRIVATE-RECOVERY-DOCUMENT",
        )
        session = self.client.session
        session["organization_id"] = self.org.pk
        session.save()
        url = reverse("opening_balance_settlement_cancel", args=[cash.pk])
        private_url = reverse(
            "opening_balance_settlement_cancel", args=[private_cash.pk]
        )
        saved = self.checked_request(url)
        before = self.financial_counts()
        for response in (
            self.client.get(private_url),
            self.client.post(private_url, saved),
            self.client.get(reverse("opening_balance_detail", args=[private_item.pk])),
        ):
            self.assertEqual(response.status_code, 404)
            self.assertNotContains(response, "PRIVATE RECOVERY", status_code=404)
        for role in ("finance", "auditor", "marketing"):
            actor = get_user_model().objects.create_user(
                username="recovery-boundary-" + role
            )
            Membership.objects.create(organization=self.org, user=actor, role=role)
            self.client.force_login(actor)
            for target in (url, private_url):
                for response in (
                    self.client.get(target),
                    self.client.post(target, saved),
                ):
                    self.assertEqual(response.status_code, 403)
                    self.assertNotContains(
                        response, "PRIVATE RECOVERY", status_code=403
                    )
        self.client.force_login(self.owner)
        session = self.client.session
        session["organization_id"] = self.org.pk
        session.save()
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.owner)
        self.assertEqual(csrf.post(url, saved).status_code, 403)
        for field, value in (
            ("amount", "1"),
            ("date", "2026-09-20"),
            ("source_reference", "FORGED-REF"),
            ("party", str(private_party.pk)),
            ("item", str(private_item.pk)),
            ("settlement", str(private_cash.pk)),
            ("transaction", str(bank.pk)),
            ("direction", "increase"),
            ("tax_amount", "0"),
        ):
            response = self.client.post(url, {**saved, field: value})
            self.assertEqual(response.status_code, 400, field)
            self.assertFalse(response.context["save_available"])
            self.assertEqual(self.financial_counts(), before)
        for field in (
            "reason",
            "evidence",
            "request_key",
            "expected_fingerprint",
            "confirmed",
            "intent",
        ):
            repeated = QueryDict("", mutable=True)
            for key, value in saved.items():
                repeated[key] = value
            repeated.setlist(field, [saved[field], saved[field]])
            response = self.client.post(
                url,
                repeated.urlencode(),
                content_type="application/x-www-form-urlencoded",
            )
            self.assertEqual(response.status_code, 400, field)
            self.assertFalse(response.context["save_available"])
            self.assertEqual(self.financial_counts(), before)
        ignored_query = self.client.get(
            url, {"amount": "1", "date": "2026-09-20", "settlement": private_cash.pk}
        )
        self.assertEqual(ignored_query.status_code, 200)
        self.assertEqual(
            (
                ignored_query.context["source_context"]["amount"],
                ignored_query.context["source_context"]["date"],
            ),
            (Decimal(300), self.bank_day),
        )
        historical_url = (
            reverse("opening_balance_detail", args=[item.pk]) + "?as_of=2026-09-10"
        )
        historical = self.client.get(historical_url)
        self.assertEqual(historical.status_code, 200)
        self.assertEqual(historical.context["position"]["remaining"], 0)
        self.assertContains(historical, "Pembayaran dicocokkan")
        for action in (
            url,
            reverse("opening_balance_match", args=[item.pk]),
            reverse("opening_balance_correct", args=[item.pk]),
        ):
            self.assertNotContains(historical, 'href="' + action + '"')
        self.assertEqual(self.client.post(historical_url, saved).status_code, 405)
        self.assertEqual(self.financial_counts(), before)

    def test_owner_membership_revoked_after_check_cannot_cancel_or_change_cash(self):
        from finance.models import OpeningBalanceCancellation

        item, cash, bank = self.original_match()
        url = reverse("opening_balance_settlement_cancel", args=[cash.pk])
        saved = self.checked_request(url)
        before = self.financial_counts()
        member = Membership.objects.get(organization=self.org, user=self.owner)
        member.is_active = False
        member.save()
        self.assertEqual(self.client.post(url, saved).status_code, 403)
        self.assertEqual(self.financial_counts(), before)
        self.assertFalse(
            OpeningBalanceCancellation.objects.filter(settlement=cash).exists()
        )
        self.assertEqual(bank.unallocated_amount, 0)


class OpeningOriginalCashHeldHTTPTests(TestCase):
    setUp = OpeningCashRecoveryPageFailureHTTPTests.setUp
    financial_counts = OpeningCashRecoveryValidationHTTPTests.financial_counts

    def original_partial_match(self):
        ledger.record_bank_opening(
            organization=self.org,
            actor=self.owner,
            account=self.account,
            date=self.cutoff,
            amount="1000",
            reference="SYN-ORIGINAL-CASH-BANK-OPENING",
            evidence="Synthetic verified original bank opening",
        )
        values = dict(
            kind="receivable",
            party=self.party,
            date=self.cutoff,
            source_date=date(2026, 8, 20),
            source_reference="SYN-ORIGINAL-CASH-OLD-AR",
            amount="300",
        )
        context = openings.opening_balance_action_context(
            organization=self.org, action="record", **values
        )
        item = openings.record_opening_balance(
            organization=self.org,
            actor=self.owner,
            **values,
            reason="Synthetic original outstanding balance verified",
            evidence="Synthetic original old document and residual list",
            request_key=str(uuid4()),
            expected_fingerprint=context["fingerprint"],
            confirmed=True,
        )
        ledger.import_bank_csv(
            organization=self.org,
            actor=self.owner,
            account=self.account,
            content=f"date,reference,description,amount\n{self.bank_day},SYN-ORIGINAL-CASH-RECEIPT,Synthetic physical receipt,120\n",
        )
        bank = BankTransaction.objects.get(
            organization=self.org, reference="SYN-ORIGINAL-CASH-RECEIPT"
        )
        context = openings.opening_balance_action_context(
            organization=self.org,
            action="settle",
            item=item,
            transaction=bank,
            amount="120",
        )
        cash = openings.settle_opening_balance(
            organization=self.org,
            actor=self.owner,
            item=item,
            transaction=bank,
            amount="120",
            reason="Synthetic actual original payment allocation",
            evidence="Synthetic actual original bank proof",
            request_key=str(uuid4()),
            expected_fingerprint=context["fingerprint"],
            confirmed=True,
        )
        self.assertEqual(
            openings.opening_balance_position(organization=self.org, item=item)[
                "remaining"
            ],
            Decimal(180),
        )
        return item, cash, bank

    def test_balanced_original_cash_journal_damage_before_any_cancellation_holds_bank_and_home(
        self,
    ):
        from django.db import connection

        from finance.models import OpeningBalanceCancellation

        item, cash, bank = self.original_partial_match()
        original = (
            cash.amount,
            cash.date,
            cash.bank_posting_id,
            cash.source_hash,
            deepcopy(cash.source_snapshot),
        )
        self.assertFalse(OpeningBalanceCancellation.objects.exists())
        before = self.financial_counts()
        debit = cash.journal.lines.get(account="BANK")
        table = connection.ops.quote_name(JournalLine._meta.db_table)
        # Persisted-source fault only: this original cash journal is balanced
        # but records an expense where the verified bank receipt should be.
        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {table} SET account = %s WHERE id = %s", ["EXPENSE", debit.pk]
            )
        self.assertEqual(
            sum(
                (row.debit - row.credit for row in cash.journal.lines.all()), Decimal(0)
            ),
            0,
        )
        position = openings.opening_balance_position(organization=self.org, item=item)
        close = openings.opening_balance_close_facts(
            organization=self.org, cutoff=date(2026, 9, 30)
        )
        for facts in (position, close):
            self.assertFalse(facts["current"])
            self.assertTrue(facts["blockers"])
        self.assertIsNone(position["remaining"])
        self.assertIsNone(close["receivable"])
        self.assertIsNone(close["payable"])
        with self.subTest(page="bank"):
            page = self.client.get(reverse("bank"))
            self.assertEqual(page.status_code, 200)
            self.assertContains(page, "SYN-ORIGINAL-CASH-RECEIPT")
            self.assertContains(page, "belum dapat diperiksa")
            self.assertContains(page, "Owner")
            self.assertIsNone(page.context["unmatched_count"])
            self.assertNotContains(page, "Sudah cocok")
            self.assertNotContains(
                page, 'action="' + reverse("bank_match", args=[bank.pk]) + '"'
            )
            self.assertNotContains(
                page, 'href="' + reverse("bank_adjustment", args=[bank.pk]) + '"'
            )
            self.assertNotContains(page, "0 perlu dicocokkan")
        with self.subTest(page="home"):
            page = self.client.get(reverse("dashboard"), {"period": "2026-09"})
            self.assertEqual(page.status_code, 200)
            self.assertContains(page, "belum dapat diperiksa")
            self.assertContains(page, "Owner")
            summary = page.context["dashboard"]
            for field in (
                "profit",
                "revenue",
                "costs",
                "receivables",
                "payables",
                "unmatched_count",
            ):
                self.assertIsNone(summary[field], field)
            self.assertNotContains(page, 'class="bar-chart"')
            self.assertNotContains(page, "0 mutasi</strong> belum dicocokkan")
        cash.refresh_from_db()
        bank.refresh_from_db()
        self.assertEqual((bank.amount, bank.date), (Decimal(120), self.bank_day))
        self.assertEqual(
            (
                cash.amount,
                cash.date,
                cash.bank_posting_id,
                cash.source_hash,
                cash.source_snapshot,
            ),
            original,
        )
        self.assertEqual(self.financial_counts(), before)
        self.assertFalse(OpeningBalanceCancellation.objects.exists())

    def test_foreign_original_cash_dependency_shows_unknown_payment_history_without_false_empty_or_leak(
        self,
    ):
        from django.db import connection

        item, cash, bank = self.original_partial_match()
        original = (
            cash.amount,
            cash.date,
            cash.bank_posting_id,
            cash.source_hash,
            deepcopy(cash.source_snapshot),
        )
        foreign = Organization.objects.create(
            name="PRIVATE FOREIGN ORIGINAL CASH COMPANY", is_demo=True
        )
        table = connection.ops.quote_name(Journal._meta.db_table)
        # External persisted-source scope fault; the own cash receipt remains,
        # but its journal now belongs to another company and cannot be shown.
        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {table} SET organization_id = %s WHERE id = %s",
                [foreign.pk, cash.journal_id],
            )
        position = openings.opening_balance_position(organization=self.org, item=item)
        self.assertFalse(position["current"])
        self.assertIsNone(position["remaining"])
        self.assertTrue(position["blockers"])
        before = self.financial_counts()
        page = self.client.get(reverse("opening_balance_detail", args=[item.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Sumber saldo lama belum dapat diperiksa")
        self.assertNotContains(page, "Belum ada pembayaran yang dicocokkan.")
        payment_section = (
            page.content.decode()
            .split("<h2>Pembayaran dan pemeriksaan pajak</h2>")[1]
            .split("</section>")[0]
        )
        self.assertIn("belum dapat diperiksa", payment_section.lower())
        self.assertNotContains(page, foreign.name)
        self.assertNotContains(
            page, 'href="' + reverse("opening_balance_match", args=[item.pk]) + '"'
        )
        self.assertNotContains(
            page,
            'href="'
            + reverse("opening_balance_settlement_cancel", args=[cash.pk])
            + '"',
        )
        cash.refresh_from_db()
        bank.refresh_from_db()
        self.assertEqual((bank.amount, bank.date), (Decimal(120), self.bank_day))
        self.assertEqual(
            (
                cash.amount,
                cash.date,
                cash.bank_posting_id,
                cash.source_hash,
                cash.source_snapshot,
            ),
            original,
        )
        self.assertEqual(self.financial_counts(), before)
