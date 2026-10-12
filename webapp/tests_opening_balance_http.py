"""Public staff contracts for old principal and actual bank cash only."""

from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.db import connection
from django.http import QueryDict
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import AuditEvent, DomainEvent, Membership, Organization
from finance import opening_balances as openings
from finance import services as ledger
from finance.models import (
    AccountingPeriod,
    BankAccount,
    BankPosting,
    BankTransaction,
    Bill,
    Invoice,
    Journal,
    JournalLine,
    OpeningBalanceItem,
    OpeningBalanceSettlement,
    Party,
)


class OpeningBalanceHTTPTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(
            name="Synthetic old-balance UI", is_demo=True
        )
        self.owner = get_user_model().objects.create_user(username="opening-ui-owner")
        Membership.objects.create(organization=self.org, user=self.owner, role="owner")
        self.party = Party.objects.create(
            organization=self.org, name="Synthetic old reseller", kind="reseller"
        )
        self.customer = Party.objects.create(
            organization=self.org, name="Synthetic old customer", kind="customer"
        )
        self.supplier = Party.objects.create(
            organization=self.org, name="Synthetic old supplier", kind="supplier"
        )
        self.account = BankAccount.objects.create(
            organization=self.org, name="Synthetic bank", account_number="SYN-OPEN-UI"
        )
        self.cutoff = date(2026, 9, 1)
        self.cash_day = date(2026, 9, 10)
        self.client.force_login(self.owner)

    def counts(self):
        return tuple(
            model.objects.count()
            for model in (
                OpeningBalanceItem,
                OpeningBalanceSettlement,
                BankPosting,
                Journal,
                JournalLine,
                Invoice,
                Bill,
                AuditEvent,
                DomainEvent,
            )
        )

    def user(self, role):
        user = get_user_model().objects.create_user(username=f"opening-ui-{role}")
        Membership.objects.create(organization=self.org, user=user, role=role)
        return user

    def movement(self, amount, reference, *, day=None):
        ledger.import_bank_csv(
            organization=self.org,
            actor=self.owner,
            account=self.account,
            content=f"date,reference,description,amount\n{day or self.cash_day},{reference},Synthetic,{amount}\n",
        )
        return BankTransaction.objects.get(organization=self.org, reference=reference)

    def record_data(
        self, *, kind="receivable", amount="1000", reference="SYN-OLD-AR", party=None
    ):
        return {
            "kind": kind,
            "party": str(
                (party or (self.supplier if kind == "payable" else self.party)).pk
            ),
            "date": self.cutoff.isoformat(),
            "source_date": "2026-08-20",
            "source_reference": reference,
            "amount": amount,
            "reason": "Synthetic actual old residual reviewed by Owner",
            "evidence": "Synthetic original document and verified outstanding list",
        }

    def checked(self, url, data):
        before = self.counts()
        response = self.client.post(url, {**data, "intent": "check"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.counts(), before)
        self.assertTrue(
            response.context["save_available"], response.context["source_context"]
        )
        form = response.context["form"]
        self.assertFalse(form["confirmed"].value())
        return response, {
            **data,
            "request_key": form["request_key"].value(),
            "expected_fingerprint": form["expected_fingerprint"].value(),
            "confirmed": "on",
            "intent": "save",
        }

    def recorded(self, **changes):
        url = reverse("opening_balance_new")
        _, data = self.checked(url, self.record_data(**changes))
        response = self.client.post(url, data)
        row = OpeningBalanceItem.objects.get(
            organization=self.org, request_key=data["request_key"]
        )
        self.assertRedirects(response, reverse("opening_balance_detail", args=[row.pk]))
        return row, data

    def matched(self, item, bank, amount):
        url = reverse("opening_balance_match", args=[item.pk])
        _, data = self.checked(
            url,
            {
                "transaction": str(bank.pk),
                "amount": str(amount),
                "reason": "Synthetic payment against original old document",
                "evidence": "Synthetic actual bank payment and old document matched",
            },
        )
        self.assertRedirects(
            self.client.post(url, data),
            reverse("opening_balance_detail", args=[item.pk]),
        )
        return OpeningBalanceSettlement.objects.get(
            request_key=data["request_key"]
        ), data

    def test_evidenced_old_ar_ap_and_actual_cash_leave_current_sales_and_costs_zero(
        self,
    ):
        ledger.record_bank_opening(
            organization=self.org,
            actor=self.owner,
            account=self.account,
            date=self.cutoff,
            amount="1000",
            reference="SYN-OLD-BANK",
            evidence="Synthetic confirmed opening bank balance",
        )
        ar, _ = self.recorded(amount="300")
        ap, _ = self.recorded(kind="payable", amount="200", reference="SYN-OLD-AP")
        self.assertEqual(
            (
                Invoice.objects.count(),
                Bill.objects.count(),
                BankTransaction.objects.count(),
            ),
            (0, 0, 0),
        )
        before = ledger.report_balances(organization=self.org, as_of=date(2026, 9, 9))
        self.assertEqual(
            (before["BANK"], before["AR"], before["AP"], before["EQUITY"]),
            (Decimal(1000), Decimal(300), Decimal(-200), Decimal(-1100)),
        )
        incoming = self.movement("120", "SYN-OLD-IN")
        outgoing = self.movement("-50", "SYN-OLD-OUT")
        self.client.force_login(self.user("finance"))
        self.matched(ar, incoming, "120")
        self.matched(ap, outgoing, "50")
        balances = ledger.report_balances(organization=self.org, as_of=self.cash_day)
        self.assertEqual(
            (balances["BANK"], balances["AR"], balances["AP"], balances["EQUITY"]),
            (Decimal(1070), Decimal(180), Decimal(-150), Decimal(-1100)),
        )
        self.assertEqual(
            (balances["REVENUE"], balances["EXPENSE"], balances["DEFERRED_REVENUE"]),
            (0, 0, 0),
        )
        self.assertEqual(sum(balances.values()), 0)
        self.assertEqual((Invoice.objects.count(), Bill.objects.count()), (0, 0))
        for item in (ar, ap):
            detail = self.client.get(reverse("opening_balance_detail", args=[item.pk]))
            self.assertContains(detail, "Pemeriksaan pajak masih diperlukan")
            self.assertContains(detail, "Pembayaran dicocokkan")
            self.assertEqual(
                detail.context["position"]["settled"],
                Decimal(120) if item == ar else Decimal(50),
            )

    def test_owner_creates_finance_matches_readers_read_and_marketing_is_denied(self):
        item, _ = self.recorded()
        for role in ("finance", "reviewer", "auditor", "director", "marketing"):
            self.client.force_login(self.user(role))
            before = self.counts()
            for url in (
                reverse("opening_balances"),
                reverse("opening_balance_detail", args=[item.pk]),
            ):
                response = self.client.get(url)
                self.assertEqual(
                    response.status_code, 403 if role == "marketing" else 200
                )
                if role != "marketing":
                    self.assertNotContains(response, "Catat saldo lama")
                    if role != "finance":
                        self.assertNotContains(
                            response,
                            'href="'
                            + reverse("opening_balance_match", args=[item.pk])
                            + '"',
                        )
            for url in (reverse("opening_balance_new"),):
                self.assertEqual(self.client.get(url).status_code, 403)
                self.assertEqual(
                    self.client.post(url, {"intent": "check"}).status_code, 403
                )
            match_url = reverse("opening_balance_match", args=[item.pk])
            self.assertEqual(
                self.client.get(match_url).status_code,
                200 if role == "finance" else 403,
            )
            self.assertEqual(
                self.client.post(match_url, {"intent": "check"}).status_code,
                200 if role == "finance" else 403,
            )
            self.assertEqual(self.counts(), before)

    def test_kind_choices_filter_company_parties_and_reuse_one_common_cutoff(self):
        url = reverse("opening_balance_new")
        ar = self.client.get(url, {"kind": "receivable"})
        self.assertEqual(
            set(
                ar.context["form"].fields["party"].queryset.values_list("pk", flat=True)
            ),
            {self.party.pk, self.customer.pk},
        )
        ap = self.client.get(url, {"kind": "payable"})
        self.assertEqual(
            list(
                ap.context["form"].fields["party"].queryset.values_list("pk", flat=True)
            ),
            [self.supplier.pk],
        )
        self.recorded()
        self.recorded(reference="SYN-OLD-AR2", party=self.customer)
        response = self.client.get(url, {"kind": "payable"})
        self.assertEqual(response.context["form"]["date"].value(), self.cutoff)
        self.assertTrue(
            response.context["form"].fields["date"].widget.attrs["readonly"]
        )
        before = self.counts()
        invalid = {
            **self.record_data(kind="payable", reference="SYN-OTHER-DATE"),
            "date": "2026-09-02",
            "intent": "check",
        }
        held = self.client.post(url, invalid)
        self.assertFalse(held.context["save_available"])
        self.assertEqual(self.counts(), before)

    def test_stale_match_preserves_every_input_until_new_check_and_confirmation(self):
        item, _ = self.recorded()
        bank = self.movement("1000", "SYN-STALE-OLD-IN")
        url = reverse("opening_balance_match", args=[item.pk])
        _, original = self.checked(
            url,
            {
                "transaction": str(bank.pk),
                "amount": "700",
                "reason": "Synthetic stale payment preview",
                "evidence": "Synthetic unchanged original evidence",
            },
        )
        context = openings.opening_balance_action_context(
            organization=self.org,
            action="settle",
            item=item,
            transaction=bank,
            amount="400",
        )
        openings.settle_opening_balance(
            organization=self.org,
            actor=self.owner,
            item=item,
            transaction=bank,
            amount="400",
            reason="Synthetic competing partial payment",
            evidence="Synthetic source checked in another session",
            request_key=str(uuid4()),
            expected_fingerprint=context["fingerprint"],
            confirmed=True,
        )
        before = self.counts()
        failed = self.client.post(url, original)
        self.assertEqual(failed.status_code, 400)
        for field in (
            "transaction",
            "amount",
            "reason",
            "evidence",
            "request_key",
            "expected_fingerprint",
        ):
            self.assertEqual(
                str(failed.context["form"][field].value()), str(original[field])
            )
        self.assertEqual(self.counts(), before)
        refreshed = self.client.post(url, {**original, "intent": "check"})
        self.assertNotEqual(
            refreshed.context["form"]["request_key"].value(), original["request_key"]
        )
        self.assertNotEqual(
            refreshed.context["form"]["expected_fingerprint"].value(),
            original["expected_fingerprint"],
        )
        self.assertFalse(refreshed.context["form"]["confirmed"].value())
        self.assertFalse(refreshed.context["save_available"])
        _, revised = self.checked(url, {**original, "amount": "600"})
        self.assertEqual(
            self.client.post(url, {**revised, "confirmed": ""}).status_code, 400
        )
        self.assertEqual(self.counts(), before)
        self.assertRedirects(
            self.client.post(url, revised),
            reverse("opening_balance_detail", args=[item.pk]),
        )
        self.assertEqual(
            openings.opening_balance_position(organization=self.org, item=item)[
                "remaining"
            ],
            0,
        )

    def test_fully_paid_old_balance_keeps_each_original_cash_tax_task(self):
        item, _ = self.recorded(amount="300")
        for amount, reference in (("120", "SYN-PART1"), ("180", "SYN-PART2")):
            bank = self.movement(amount, reference)
            self.matched(item, bank, amount)
        response = self.client.get(reverse("opening_balance_detail", args=[item.pk]))
        self.assertTrue(response.context["position"]["exhausted"])
        self.assertEqual(len(response.context["settlements"]), 2)
        self.assertContains(response, "Pemeriksaan pajak masih diperlukan", count=2)
        for event in OpeningBalanceSettlement.objects.filter(item=item):
            self.assertContains(
                response, reverse("opening_balance_tax:review", args=[event.pk])
            )
        self.assertNotContains(
            response, 'href="' + reverse("opening_balance_match", args=[item.pk]) + '"'
        )

    def test_exact_retry_after_close_never_rewrites_cash_or_original_principal(self):
        item, record_data = self.recorded(amount="100")
        bank = self.movement("100", "SYN-OLD-RETRY")
        _, match_data = self.matched(item, bank, "100")
        AccountingPeriod.objects.update_or_create(
            organization=self.org,
            year=2026,
            month=9,
            defaults={"closed": True, "closed_at": timezone.now()},
        )
        before = self.counts()
        for url, data in (
            (reverse("opening_balance_new"), record_data),
            (reverse("opening_balance_match", args=[item.pk]), match_data),
        ):
            self.assertRedirects(
                self.client.post(url, data),
                reverse("opening_balance_detail", args=[item.pk]),
            )
            self.assertEqual(
                self.client.post(
                    url, {**data, "evidence": "Changed synthetic evidence"}
                ).status_code,
                400,
            )
        self.assertEqual(self.counts(), before)

    def test_confirm_csrf_duplicate_fields_unknown_fields_and_get_never_write(self):
        url = reverse("opening_balance_new")
        _, data = self.checked(url, self.record_data())
        repeated = QueryDict("", mutable=True)
        for key, value in data.items():
            repeated[key] = value
        repeated.setlist("amount", ["1000", "1"])
        before = self.counts()
        self.assertEqual(
            self.client.post(
                url,
                repeated.urlencode(),
                content_type="application/x-www-form-urlencoded",
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.post(url, {**data, "confirmed": ""}).status_code, 400
        )
        self.assertEqual(
            self.client.post(url, {**data, "tax_amount": "0"}).status_code, 400
        )
        self.assertEqual(
            self.client.post(url, {**data, "intent": "unexpected"}).status_code, 400
        )
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.owner)
        self.assertEqual(csrf.post(url, data).status_code, 403)
        self.assertEqual(self.client.get(url, data).status_code, 200)
        self.assertEqual(self.client.put(url).status_code, 405)
        self.assertEqual(self.counts(), before)

    def test_foreign_party_bank_and_opening_routes_never_disclose_or_write(self):
        foreign = Organization.objects.create(
            name="Private synthetic company", is_demo=True
        )
        Membership.objects.create(organization=foreign, user=self.owner, role="owner")
        party = Party.objects.create(
            organization=foreign, name="PRIVATE OLD PARTY", kind="customer"
        )
        account = BankAccount.objects.create(
            organization=foreign, name="PRIVATE OLD BANK", account_number="PRIVATE-OLD"
        )
        bank = BankTransaction.objects.create(
            organization=foreign,
            account=account,
            date=self.cash_day,
            amount="1000",
            reference="PRIVATE OLD REFERENCE",
        )
        context = openings.opening_balance_action_context(
            organization=foreign,
            action="record",
            kind="receivable",
            party=party,
            date=self.cutoff,
            source_date=date(2026, 8, 20),
            source_reference="PRIVATE OLD DOCUMENT",
            amount="1000",
        )
        foreign_item = openings.record_opening_balance(
            organization=foreign,
            actor=self.owner,
            kind="receivable",
            party=party,
            date=self.cutoff,
            source_date=date(2026, 8, 20),
            source_reference="PRIVATE OLD DOCUMENT",
            amount="1000",
            reason="Synthetic private principal",
            evidence="Synthetic private original source",
            request_key=str(uuid4()),
            expected_fingerprint=context["fingerprint"],
            confirmed=True,
        )
        session = self.client.session
        session["organization_id"] = self.org.pk
        session.save()
        before = self.counts()
        response = self.client.post(
            reverse("opening_balance_new"),
            {**self.record_data(party=party), "intent": "check"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "PRIVATE OLD")
        self.assertFalse(response.context["save_available"])
        for name in ("opening_balance_detail", "opening_balance_match"):
            response = self.client.get(reverse(name, args=[foreign_item.pk]))
            self.assertEqual(response.status_code, 404)
            self.assertNotContains(response, "PRIVATE OLD", status_code=404)
        self.assertEqual(self.counts(), before)
        item, _ = self.recorded()
        before = self.counts()
        response = self.client.post(
            reverse("opening_balance_match", args=[item.pk]),
            {"transaction": str(bank.pk), "amount": "1000", "intent": "check"},
        )
        self.assertNotContains(response, "PRIVATE OLD")
        self.assertFalse(response.context["save_available"])
        self.assertEqual(self.counts(), before)

    def test_historical_position_does_not_borrow_later_cash_and_invalid_cutoffs_fail(
        self,
    ):
        item, _ = self.recorded(amount="300")
        bank = self.movement("120", "SYN-DATED-CASH")
        self.matched(item, bank, "120")
        before = self.counts()
        detail_url = reverse("opening_balance_detail", args=[item.pk])
        earlier = self.client.get(detail_url, {"as_of": "2026-09-09"})
        self.assertEqual(
            (
                earlier.context["position"]["settled"],
                earlier.context["position"]["remaining"],
            ),
            (0, Decimal(300)),
        )
        self.assertEqual(earlier.context["settlements"], [])
        self.assertContains(earlier, "Saldo per")
        self.assertNotContains(
            earlier, 'href="' + reverse("opening_balance_match", args=[item.pk]) + '"'
        )
        current = self.client.get(detail_url, {"as_of": "2026-09-10"})
        self.assertEqual(
            (
                current.context["position"]["settled"],
                current.context["position"]["remaining"],
            ),
            (Decimal(120), Decimal(180)),
        )
        listing = self.client.get(reverse("opening_balances"), {"as_of": "2026-09-09"})
        self.assertEqual(
            listing.context["items"][0]["position"]["remaining"], Decimal(300)
        )
        self.assertContains(listing, "as_of=2026-09-09")
        for url in (detail_url, reverse("opening_balances")):
            for invalid in (
                "nonsense",
                "2026-02-30",
                "1999-12-31",
                "20260909",
                (timezone.localdate() + timedelta(days=1)).isoformat(),
            ):
                self.assertEqual(
                    self.client.get(url, {"as_of": invalid}).status_code, 400
                )
            self.assertEqual(
                self.client.get(url + "?as_of=2026-09-09&as_of=2026-09-10").status_code,
                400,
            )
        self.assertEqual(self.counts(), before)

    def test_damage_is_unknown_and_does_not_show_foreign_legal_identity(self):
        item, data = self.recorded()
        foreign = Organization.objects.create(
            name="Synthetic damaged unrelated company"
        )
        party = Party.objects.create(
            organization=foreign, name="PRIVATE DAMAGED PARTY", kind="customer"
        )
        self.assertTrue(connection.in_atomic_block)
        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {connection.ops.quote_name(item._meta.db_table)} SET party_id=%s WHERE id=%s",
                [party.pk, item.pk],
            )
        before = self.counts()
        response = self.client.get(reverse("opening_balance_detail", args=[item.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["position"]["current"])
        self.assertIsNone(response.context["position"]["remaining"])
        self.assertContains(response, "Belum dapat diperiksa")
        self.assertNotContains(response, "PRIVATE DAMAGED PARTY")
        self.assertEqual(
            self.client.post(reverse("opening_balance_new"), data).status_code, 400
        )
        self.assertEqual(self.counts(), before)

    def test_source_check_uses_available_cash_and_actual_bank_date_without_tax_guess(
        self,
    ):
        item, _ = self.recorded(amount="300")
        bank = self.movement("120", "SYN-ACTUAL-DATE")
        before = self.counts()
        response = self.client.post(
            reverse("opening_balance_match", args=[item.pk]),
            {"intent": "check", "transaction": str(bank.pk)},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            Decimal(response.context["form"]["amount"].value()), Decimal(120)
        )
        self.assertEqual(response.context["source_context"]["date"], self.cash_day)
        self.assertNotIn("date", response.context["form"].fields)
        self.assertNotIn("tax_amount", response.context["form"].fields)
        self.assertNotIn("gross_amount", response.context["form"].fields)
        self.assertContains(response, "Tanggal pembayaran sesuai bank")
        self.assertEqual(self.counts(), before)
