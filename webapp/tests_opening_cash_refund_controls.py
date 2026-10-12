"""Public HTTP controls for distinct actual returns of old matched cash."""

from copy import deepcopy
from datetime import date
from decimal import Decimal
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.http import QueryDict
from django.test import Client, TestCase
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
from taxes.models import (
    ExternalTaxReview,
    OpeningRefundTaxReview,
    TaxEvidence,
    TaxObligation,
)
from taxes.opening_refund_review import opening_refund_review_context


class OpeningCashRefundControlsHTTPTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(
            name="Synthetic refund HTTP controls", is_demo=True
        )
        self.actors = {}
        for role in ("owner", "finance", "auditor", "marketing"):
            actor = get_user_model().objects.create_user(
                username="refund-controls-" + role
            )
            Membership.objects.create(organization=self.org, user=actor, role=role)
            self.actors[role] = actor
        self.owner = self.actors["owner"]
        self.account = BankAccount.objects.create(
            organization=self.org,
            name="Synthetic controls bank",
            account_number="SYN-REFUND-CONTROLS",
        )
        self.cutoff, self.original_day = date(2026, 9, 1), date(2026, 9, 10)
        ledger.record_bank_opening(
            organization=self.org,
            actor=self.owner,
            account=self.account,
            date=self.cutoff,
            amount="1000",
            reference="SYN-REFUND-CONTROLS-OPENING",
            evidence="Synthetic independently verified starting bank balance",
        )
        self.account.refresh_from_db()
        self.ar = self.old_cash(kind="receivable", reference="SYN-REFUND-CONTROLS-AR")
        self.ap = self.old_cash(kind="payable", reference="SYN-REFUND-CONTROLS-AP")
        self.client.force_login(self.actors["finance"])

    def old_cash(self, *, kind, reference, organization=None, actor=None, account=None):
        org, actor, account = (
            organization or self.org,
            actor or self.owner,
            account or self.account,
        )
        amount, returned, refund_day = (
            ("300", "-50", date(2026, 9, 12))
            if kind == "receivable"
            else ("240", "60", date(2026, 9, 15))
        )
        party = Party.objects.create(
            organization=org,
            kind="reseller" if kind == "receivable" else "supplier",
            name=reference + " independently checked party",
        )
        values = dict(
            kind=kind,
            party=party,
            date=self.cutoff,
            source_date=date(2026, 8, 20),
            source_reference=reference,
            amount=amount,
        )
        preview = openings.opening_balance_action_context(
            organization=org, action="record", **values
        )
        self.assertTrue(preview["current"], preview)
        self.assertFalse(preview["blockers"])
        item = openings.record_opening_balance(
            organization=org,
            actor=actor,
            **values,
            reason="Synthetic old principal independently checked",
            evidence="Synthetic old document and residual list",
            request_key=str(uuid4()),
            expected_fingerprint=preview["fingerprint"],
            confirmed=True,
        )
        signed = amount if kind == "receivable" else "-" + amount
        ledger.import_bank_csv(
            organization=org,
            actor=actor,
            account=account,
            content=(
                "date,reference,description,amount\n"
                f"{self.original_day},{reference}-CASH,Synthetic original physical payment,{signed}\n"
                f"{refund_day},{reference}-RETURN,Synthetic distinct actual returned money,{returned}\n"
            ),
        )
        bank = BankTransaction.objects.get(
            organization=org, reference=reference + "-CASH"
        )
        refund_bank = BankTransaction.objects.get(
            organization=org, reference=reference + "-RETURN"
        )
        preview = openings.opening_balance_action_context(
            organization=org,
            action="settle",
            item=item,
            transaction=bank,
            amount=amount,
        )
        self.assertTrue(preview["current"], preview)
        self.assertFalse(preview["blockers"])
        cash = openings.settle_opening_balance(
            organization=org,
            actor=actor,
            item=item,
            transaction=bank,
            amount=amount,
            reason="Synthetic full original cash allocation",
            evidence="Synthetic old source and original bank proof",
            request_key=str(uuid4()),
            expected_fingerprint=preview["fingerprint"],
            confirmed=True,
        )
        self.assertEqual(
            openings.opening_balance_position(organization=org, item=item)["remaining"],
            0,
        )
        return item, cash, bank, refund_bank

    def counts(self):
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
                OpeningRefundTaxReview,
                ExternalTaxReview,
                TaxEvidence,
                TaxObligation,
            )
        )

    def checked(self, original):
        item, cash, _, bank = original
        url = reverse("opening_balance_settlement_refund", args=[cash.pk])
        data = {
            "transaction": str(bank.pk),
            "amount": "50" if item.kind == "receivable" else "60",
            "reason": "Simpan alasan staff atas pengembalian nyata",
            "evidence": "Simpan bukti pembayaran asli dan mutasi pengembalian yang berbeda",
        }
        before = self.counts()
        page = self.client.post(url, {**data, "intent": "check"})
        self.assertEqual(page.status_code, 200)
        self.assertTrue(page.context["save_available"], page.context["source_context"])
        self.assertFalse(page.context["form"]["confirmed"].value())
        self.assertEqual(
            self.counts(),
            before,
            "Checking cash never records a financial or tax action",
        )
        form = page.context["form"]
        return url, {
            **data,
            "intent": "save",
            "confirmed": "on",
            "request_key": form["request_key"].value(),
            "expected_fingerprint": form["expected_fingerprint"].value(),
        }

    def test_stale_party_and_account_names_retain_values_recheck_unconfirmed_and_refund_once(
        self,
    ):
        for index, source in enumerate((self.ar[0].party, self.account)):
            with self.subTest(source=source.__class__.__name__):
                original = (
                    self.ar
                    if index == 0
                    else self.old_cash(
                        kind="receivable", reference="SYN-REFUND-ACCOUNT-STALE"
                    )
                )
                item, cash, original_bank, bank = original
                raw_item, raw_cash = (
                    deepcopy(item.source_snapshot),
                    deepcopy(cash.source_snapshot),
                )
                url, data = self.checked(original)
                before = self.counts()
                source.name += " independently checked name change"
                source.save()
                rejected = self.client.post(url, data)
                self.assertEqual(rejected.status_code, 400)
                self.assertFalse(rejected.context["save_available"])
                self.assertContains(
                    rejected, "Isian Anda tetap tersimpan", status_code=400
                )
                for field in (
                    "transaction",
                    "amount",
                    "reason",
                    "evidence",
                    "request_key",
                    "expected_fingerprint",
                ):
                    self.assertEqual(
                        str(rejected.context["form"][field].value()), str(data[field])
                    )
                self.assertEqual(self.counts(), before)
                self.assertFalse(
                    OpeningBalanceRefund.objects.filter(settlement=cash).exists()
                )
                self.assertEqual(
                    (original_bank.unallocated_amount, bank.unallocated_amount),
                    (0, Decimal(-50)),
                )
                refreshed = self.client.post(url, {**data, "intent": "check"})
                self.assertEqual(refreshed.status_code, 200)
                self.assertTrue(
                    refreshed.context["save_available"],
                    refreshed.context["source_context"],
                )
                form = refreshed.context["form"]
                for field in ("transaction", "amount", "reason", "evidence"):
                    self.assertEqual(str(form[field].value()), str(data[field]))
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
                self.assertEqual(
                    self.client.post(
                        url,
                        {
                            key: value
                            for key, value in revised.items()
                            if key != "confirmed"
                        },
                    ).status_code,
                    400,
                )
                self.assertEqual(self.counts(), before)
                self.assertRedirects(
                    self.client.post(url, revised),
                    reverse("opening_balance_detail", args=[item.pk]),
                )
                after = self.counts()
                refund = OpeningBalanceRefund.objects.get(settlement=cash)
                self.assertEqual(
                    (
                        refund.date,
                        refund.amount,
                        refund.bank_posting.transaction_id,
                        refund.bank_posting.signed_amount,
                    ),
                    (date(2026, 9, 12), Decimal(50), bank.pk, Decimal(-50)),
                )
                self.assertFalse(
                    OpeningBalanceRefund.objects.filter(
                        request_key=data["request_key"]
                    ).exists()
                )
                self.assertRedirects(
                    self.client.post(url, revised),
                    reverse("opening_balance_detail", args=[item.pk]),
                )
                self.assertEqual(
                    self.counts(),
                    after,
                    "Exact replay must not create a second actual return",
                )
                self.assertEqual(
                    self.client.post(
                        url, {**revised, "request_key": str(uuid4())}
                    ).status_code,
                    400,
                )
                self.assertEqual(self.counts(), after)
                self.assertEqual(
                    openings.opening_balance_position(organization=self.org, item=item)[
                        "remaining"
                    ],
                    Decimal(50),
                )
                self.assertEqual(
                    (original_bank.unallocated_amount, bank.unallocated_amount), (0, 0)
                )
                item.refresh_from_db()
                cash.refresh_from_db()
                self.assertEqual(
                    (item.source_snapshot, cash.source_snapshot), (raw_item, raw_cash)
                )
                self.assertEqual(
                    opening_refund_review_context(organization=self.org, refund=refund)[
                        "status"
                    ],
                    "pending",
                )

    def test_roles_csrf_foreign_sources_forged_fields_and_adverse_cash_selection_do_not_write(
        self,
    ):
        item, cash, bank, refund_bank = self.ar
        foreign = Organization.objects.create(
            name="PRIVATE SYNTHETIC REFUND COMPANY", is_demo=True
        )
        foreign_owner = get_user_model().objects.create_user(
            username="private-refund-controls-owner"
        )
        Membership.objects.create(
            organization=foreign, user=foreign_owner, role="owner"
        )
        foreign_account = BankAccount.objects.create(
            organization=foreign,
            name="PRIVATE SYNTHETIC REFUND BANK",
            account_number="PRIVATE-SYN-REFUND",
        )
        foreign_item, foreign_cash, _, foreign_bank = self.old_cash(
            kind="receivable",
            reference="PRIVATE-SYN-REFUND-SOURCE",
            organization=foreign,
            actor=foreign_owner,
            account=foreign_account,
        )
        foreign_url = reverse(
            "opening_balance_settlement_refund", args=[foreign_cash.pk]
        )
        url, saved = self.checked(self.ar)
        before = self.counts()
        for role in ("finance", "owner"):
            self.client.force_login(self.actors[role])
            for response in (
                self.client.get(foreign_url),
                self.client.post(foreign_url, saved),
                self.client.get(
                    reverse("opening_balance_detail", args=[foreign_item.pk])
                ),
            ):
                self.assertEqual(response.status_code, 404)
                self.assertNotContains(response, "PRIVATE SYNTHETIC", status_code=404)
        for role in ("auditor", "marketing"):
            self.client.force_login(self.actors[role])
            for target in (url, foreign_url):
                for response in (
                    self.client.get(target),
                    self.client.post(target, saved),
                ):
                    self.assertEqual(response.status_code, 403)
                    self.assertNotContains(
                        response, "PRIVATE SYNTHETIC", status_code=403
                    )
            detail = self.client.get(reverse("opening_balance_detail", args=[item.pk]))
            self.assertEqual(detail.status_code, 403 if role == "marketing" else 200)
            if role == "auditor":
                self.assertContains(detail, "Pembayaran dicocokkan")
                self.assertNotContains(detail, 'href="' + url + '"')
        self.client.force_login(self.actors["finance"])
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.actors["finance"])
        self.assertEqual(csrf.post(url, saved).status_code, 403)
        for field, value in (
            ("date", "2026-09-20"),
            ("source_date", "2026-09-20"),
            ("source_reference", "FORGED"),
            ("item", str(foreign_item.pk)),
            ("settlement", str(foreign_cash.pk)),
            ("party", str(foreign_item.party_id)),
            ("direction", "increase"),
            ("tax_amount", "0"),
        ):
            response = self.client.post(url, {**saved, field: value})
            self.assertEqual(response.status_code, 400, field)
            self.assertFalse(response.context["save_available"])
            self.assertEqual(self.counts(), before)
        for field in (
            "transaction",
            "amount",
            "reason",
            "evidence",
            "request_key",
            "expected_fingerprint",
            "confirmed",
            "intent",
        ):
            duplicate = QueryDict("", mutable=True)
            for key, value in saved.items():
                duplicate[key] = value
            duplicate.setlist(field, [saved[field], saved[field]])
            response = self.client.post(
                url,
                duplicate.urlencode(),
                content_type="application/x-www-form-urlencoded",
            )
            self.assertEqual(response.status_code, 400, field)
            self.assertFalse(response.context["save_available"])
            self.assertEqual(self.counts(), before)
        for selected in (foreign_bank, bank, self.ap[2], self.ap[3]):
            response = self.client.post(url, {**saved, "transaction": str(selected.pk)})
            self.assertEqual(response.status_code, 400, selected.reference)
            self.assertFalse(response.context["save_available"])
            self.assertNotContains(response, "PRIVATE SYNTHETIC", status_code=400)
            self.assertEqual(self.counts(), before)
        for amount in ("0", "-1", "50.001", "51", "301"):
            response = self.client.post(url, {**saved, "amount": amount})
            self.assertEqual(response.status_code, 400, amount)
            self.assertFalse(response.context["save_available"])
            self.assertEqual(self.counts(), before)
        self.assertEqual(
            (bank.unallocated_amount, refund_bank.unallocated_amount), (0, Decimal(-50))
        )
        self.assertFalse(OpeningBalanceRefund.objects.exists())
        member = Membership.objects.get(
            organization=self.org, user=self.actors["finance"]
        )
        member.is_active = False
        member.save()
        self.assertEqual(self.client.post(url, saved).status_code, 403)
        self.assertEqual(self.counts(), before)

    def test_supplier_actual_return_uses_new_bank_date_and_separate_pending_tax_task(
        self,
    ):
        item, cash, original_bank, refund_bank = self.ap
        original_item, original_cash = (
            deepcopy(item.source_snapshot),
            deepcopy(cash.source_snapshot),
        )
        self.client.force_login(self.owner)
        url, saved = self.checked(self.ap)
        before = self.counts()
        page = self.client.get(
            url, {"date": "2026-09-01", "transaction": self.ar[3].pk, "amount": "1"}
        )
        self.assertEqual(page.status_code, 200)
        for field in (
            "date",
            "party",
            "item",
            "settlement",
            "source_date",
            "source_reference",
        ):
            self.assertNotIn(field, page.context["form"].fields)
        self.assertEqual(self.counts(), before)
        detail_url = reverse("opening_balance_detail", args=[item.pk])
        self.assertRedirects(self.client.post(url, saved), detail_url)
        refund = OpeningBalanceRefund.objects.get(organization=self.org)
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
                cash.pk,
                self.owner.pk,
                date(2026, 9, 15),
                Decimal(60),
                refund_bank.pk,
                Decimal(60),
            ),
        )
        balances = ledger.report_balances(
            organization=self.org, as_of=date(2026, 9, 15)
        )
        self.assertEqual(
            tuple(balances[key] for key in ("BANK", "AR", "AP", "EQUITY")),
            (Decimal(1120), Decimal(0), Decimal(-60), Decimal(-1060)),
        )
        self.assertEqual(
            (
                sum(balances.values()),
                balances["REVENUE"],
                balances["EXPENSE"],
                balances["DEFERRED_REVENUE"],
            ),
            (0, 0, 0, 0),
        )
        self.assertEqual(
            (original_bank.unallocated_amount, refund_bank.unallocated_amount), (0, 0)
        )
        self.assertEqual(
            set(
                ledger.pending_bank_transactions(organization=self.org).values_list(
                    "pk", flat=True
                )
            ),
            {self.ar[3].pk},
        )
        self.assertEqual(
            openings.opening_balance_position(organization=self.org, item=item)[
                "remaining"
            ],
            Decimal(60),
        )
        item.refresh_from_db()
        cash.refresh_from_db()
        self.assertEqual(
            (item.source_snapshot, cash.source_snapshot), (original_item, original_cash)
        )
        detail = self.client.get(detail_url)
        self.assertContains(detail, "Pengembalian uang dicocokkan")
        self.assertContains(
            detail, reverse("opening_balance_tax:review", args=[cash.pk])
        )
        tax_url = reverse("opening_refund_tax:review", args=[refund.pk])
        self.assertContains(detail, 'href="' + tax_url + '"')
        self.assertEqual(detail.context["refunds"][0]["tax_status"], "pending")
        readonly_before = self.counts()
        historical = self.client.get(detail_url, {"as_of": "2026-09-14"})
        self.assertEqual(historical.status_code, 200)
        self.assertEqual(historical.context["position"]["remaining"], 0)
        self.assertEqual(historical.context["refunds"], [])
        self.assertNotContains(historical, "Pengembalian uang dicocokkan")
        for action in (
            url,
            tax_url,
            reverse("opening_balance_match", args=[item.pk]),
            reverse("opening_balance_correct", args=[item.pk]),
        ):
            self.assertNotContains(historical, 'href="' + action + '"')
        self.assertEqual(
            self.client.post(detail_url + "?as_of=2026-09-14", saved).status_code, 405
        )
        earlier = ledger.report_balances(organization=self.org, as_of=date(2026, 9, 14))
        self.assertEqual(
            tuple(earlier[key] for key in ("BANK", "AR", "AP", "EQUITY")),
            (1060, 0, 0, -1060),
        )
        for role in ("owner", "finance", "auditor"):
            self.client.force_login(self.actors[role])
            review = self.client.get(tax_url)
            self.assertEqual(review.status_code, 200)
            self.assertEqual(review.context["review_context"]["status"], "pending")
            if role == "auditor":
                self.assertIsNone(review.context["form"])
            else:
                form = review.context["form"]
                for field in (
                    "gross_amount",
                    "withholding_amount",
                    "withholding_recovery_requested",
                    "tax_credit_restitution_requested",
                    "prior_period_income_correction_requested",
                ):
                    self.assertIn(form[field].value(), (None, ""), field)
            if role != "owner":
                self.assertNotContains(review, "Simpan kesimpulan laporan")
        self.client.force_login(self.actors["marketing"])
        self.assertEqual(self.client.get(tax_url).status_code, 403)
        self.assertEqual(self.counts(), readonly_before)
