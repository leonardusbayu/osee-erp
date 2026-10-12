"""Public staff principal corrections preserve the original document and cash."""

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
    OpeningBalanceCorrection,
    OpeningBalanceItem,
    OpeningBalanceSettlement,
    Party,
)


class OpeningPrincipalCorrectionHTTPTests(TestCase):
    def test_owner_corrects_old_ar_and_ap_without_changing_originals_cash_sales_or_costs(
        self,
    ):
        org = Organization.objects.create(
            name="Synthetic principal correction UI", is_demo=True
        )
        owner = get_user_model().objects.create_user(
            username="opening-correction-owner"
        )
        Membership.objects.create(organization=org, user=owner, role="owner")
        self.client.force_login(owner)
        cutoff, effective = date(2026, 9, 1), date(2026, 9, 12)
        bank = BankAccount.objects.create(
            organization=org,
            name="Synthetic principal bank",
            account_number="SYN-CORR-UI",
        )
        ledger.record_bank_opening(
            organization=org,
            actor=owner,
            account=bank,
            date=cutoff,
            amount="1000",
            reference="SYN-CORR-BANK",
            evidence="Synthetic verified opening bank balance",
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

        items = []
        for kind, party_kind, amount, direction, correction, after in (
            ("receivable", "reseller", "300", "increase", "50", "350"),
            ("payable", "supplier", "200", "decrease", "20", "180"),
        ):
            party = Party.objects.create(
                organization=org, name=f"Synthetic {party_kind}", kind=party_kind
            )
            selection = {
                "kind": kind,
                "party": party,
                "date": cutoff,
                "source_date": date(2026, 8, 20),
                "source_reference": f"SYN-ORIGINAL-{kind}",
                "amount": amount,
            }
            context = openings.opening_balance_action_context(
                organization=org, action="record", **selection
            )
            item = openings.record_opening_balance(
                organization=org,
                actor=owner,
                **selection,
                reason="Synthetic original residual reviewed",
                evidence="Synthetic original document and outstanding list",
                request_key=str(uuid4()),
                expected_fingerprint=context["fingerprint"],
                confirmed=True,
            )
            original = (
                item.amount,
                item.kind,
                item.party_id,
                item.date,
                item.source_date,
                item.source_reference,
                item.source_hash,
            )
            url = reverse("opening_balance_correct", args=[item.pk])
            before = counts()
            form_page = self.client.get(url)
            self.assertEqual(form_page.status_code, 200)
            self.assertContains(form_page, "Tambah")
            self.assertContains(form_page, "Kurangi")
            self.assertNotIn("party", form_page.context["form"].fields)
            self.assertNotIn("source_reference", form_page.context["form"].fields)
            self.assertEqual(counts(), before)
            data = {
                "date": effective.isoformat(),
                "direction": direction,
                "amount": correction,
                "reason": "Synthetic original residual amount was incorrect",
                "evidence": "Synthetic original document and verified corrected outstanding amount",
            }
            checked = self.client.post(url, {**data, "intent": "check"})
            self.assertEqual(checked.status_code, 200)
            self.assertTrue(
                checked.context["save_available"], checked.context["source_context"]
            )
            self.assertEqual(
                checked.context["source_context"]["before"], Decimal(amount)
            )
            self.assertEqual(checked.context["source_context"]["after"], Decimal(after))
            self.assertFalse(checked.context["form"]["confirmed"].value())
            self.assertEqual(counts(), before)
            form = checked.context["form"]
            saved = self.client.post(
                url,
                {
                    **data,
                    "intent": "save",
                    "confirmed": "on",
                    "request_key": form["request_key"].value(),
                    "expected_fingerprint": form["expected_fingerprint"].value(),
                },
            )
            self.assertRedirects(
                saved, reverse("opening_balance_detail", args=[item.pk])
            )
            item.refresh_from_db()
            self.assertEqual(
                (
                    item.amount,
                    item.kind,
                    item.party_id,
                    item.date,
                    item.source_date,
                    item.source_reference,
                    item.source_hash,
                ),
                original,
            )
            detail = self.client.get(reverse("opening_balance_detail", args=[item.pk]))
            self.assertEqual(
                detail.context["position"]["original_opening"], Decimal(amount)
            )
            self.assertEqual(detail.context["position"]["opening"], Decimal(after))
            self.assertEqual(detail.context["position"]["remaining"], Decimal(after))
            self.assertContains(
                detail,
                "Jumlah ditambah" if direction == "increase" else "Jumlah dikurangi",
            )
            self.assertEqual(detail.context["settlements"], [])
            earlier = self.client.get(
                reverse("opening_balance_detail", args=[item.pk]),
                {"as_of": "2026-09-11"},
            )
            self.assertEqual(earlier.context["position"]["opening"], Decimal(amount))
            self.assertEqual(earlier.context["position"]["correction_delta"], 0)
            items.append(item)

        balances = ledger.report_balances(organization=org, as_of=effective)
        self.assertEqual(
            (balances["BANK"], balances["AR"], balances["AP"], balances["EQUITY"]),
            (Decimal(1000), Decimal(350), Decimal(-180), Decimal(-1170)),
        )
        self.assertEqual(sum(balances.values()), 0)
        self.assertEqual(
            (balances["REVENUE"], balances["EXPENSE"], balances["DEFERRED_REVENUE"]),
            (0, 0, 0),
        )
        self.assertEqual(
            (
                BankTransaction.objects.count(),
                BankPosting.objects.count(),
                Invoice.objects.count(),
                Bill.objects.count(),
            ),
            (0, 0, 0, 0),
        )
        self.assertEqual(OpeningBalanceCorrection.objects.count(), 2)
        finance = get_user_model().objects.create_user(
            username="opening-correction-finance"
        )
        Membership.objects.create(organization=org, user=finance, role="finance")
        self.client.force_login(finance)
        before = counts()
        for item in items:
            url = reverse("opening_balance_correct", args=[item.pk])
            self.assertEqual(self.client.get(url).status_code, 403)
            self.assertEqual(
                self.client.post(url, {"intent": "check"}).status_code, 403
            )
            detail = self.client.get(reverse("opening_balance_detail", args=[item.pk]))
            self.assertEqual(detail.status_code, 200)
            self.assertNotContains(detail, 'href="' + url + '"')
        self.assertEqual(counts(), before)

    def test_correction_http_boundaries_scope_roles_csrf_fields_and_historical_read_only(
        self,
    ):
        org = Organization.objects.create(
            name="Synthetic correction boundaries", is_demo=True
        )
        foreign_org = Organization.objects.create(
            name="Synthetic private correction company", is_demo=True
        )
        owner = get_user_model().objects.create_user(
            username="opening-correction-boundary-owner"
        )
        for company in (org, foreign_org):
            Membership.objects.create(organization=company, user=owner, role="owner")
        cutoff, effective = date(2026, 9, 1), date(2026, 9, 12)

        def record(company, name, reference):
            party = Party.objects.create(
                organization=company, name=name, kind="reseller"
            )
            selection = {
                "kind": "receivable",
                "party": party,
                "date": cutoff,
                "source_date": date(2026, 8, 20),
                "source_reference": reference,
                "amount": "300",
            }
            context = openings.opening_balance_action_context(
                organization=company, action="record", **selection
            )
            return openings.record_opening_balance(
                organization=company,
                actor=owner,
                **selection,
                reason="Synthetic original amount reviewed",
                evidence="Synthetic original source and outstanding list",
                request_key=str(uuid4()),
                expected_fingerprint=context["fingerprint"],
                confirmed=True,
            )

        def select_company(client):
            session = client.session
            session["organization_id"] = org.pk
            session.save()

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

        item = record(org, "Synthetic boundary reseller", "SYN-BOUNDARY-ORIGINAL")
        private = record(
            foreign_org, "PRIVATE CORRECTION PARTY", "PRIVATE CORRECTION DOCUMENT"
        )
        self.client.force_login(owner)
        select_company(self.client)
        url = reverse("opening_balance_correct", args=[item.pk])
        foreign_url = reverse("opening_balance_correct", args=[private.pk])
        data = {
            "direction": "increase",
            "date": effective.isoformat(),
            "amount": "50",
            "reason": "Synthetic original amount needs correction",
            "evidence": "Synthetic correction source checked by Owner",
        }
        before = counts()
        checked = self.client.post(url, {**data, "intent": "check"})
        self.assertEqual(checked.status_code, 200)
        self.assertTrue(
            checked.context["save_available"], checked.context["source_context"]
        )
        form = checked.context["form"]
        saved_data = {
            **data,
            "intent": "save",
            "confirmed": "on",
            "request_key": form["request_key"].value(),
            "expected_fingerprint": form["expected_fingerprint"].value(),
        }
        self.assertEqual(counts(), before)

        for target in (
            foreign_url,
            reverse("opening_balance_detail", args=[private.pk]),
        ):
            for response, expected in (
                (self.client.get(target), 404),
                (
                    self.client.post(target, saved_data),
                    404 if target == foreign_url else 405,
                ),
            ):
                self.assertEqual(response.status_code, expected)
                self.assertNotContains(
                    response, "PRIVATE CORRECTION", status_code=response.status_code
                )
        self.assertEqual(counts(), before)

        for role in ("finance", "marketing", "reviewer", "auditor", "director"):
            user = get_user_model().objects.create_user(
                username=f"opening-correction-boundary-{role}"
            )
            Membership.objects.create(organization=org, user=user, role=role)
            self.client.force_login(user)
            select_company(self.client)
            for target in (url, foreign_url):
                for response in (
                    self.client.get(target),
                    self.client.post(target, saved_data),
                ):
                    self.assertEqual(response.status_code, 403)
                    self.assertNotContains(
                        response, "PRIVATE CORRECTION", status_code=403
                    )
            detail = self.client.get(reverse("opening_balance_detail", args=[item.pk]))
            self.assertEqual(detail.status_code, 403 if role == "marketing" else 200)
            if role != "marketing":
                self.assertNotContains(detail, 'href="' + url + '"')
            self.assertEqual(counts(), before)

        self.client.force_login(owner)
        select_company(self.client)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(owner)
        select_company(csrf_client)
        self.assertEqual(csrf_client.post(url, saved_data).status_code, 403)
        self.assertEqual(counts(), before)
        for field, alternative in (
            ("direction", "decrease"),
            ("date", "2026-09-13"),
            ("amount", "1"),
            ("request_key", str(uuid4())),
            ("expected_fingerprint", "f" * 64),
            ("confirmed", ""),
            ("intent", "check"),
        ):
            repeated = QueryDict("", mutable=True)
            for name, value in saved_data.items():
                repeated[name] = value
            repeated.setlist(field, [saved_data[field], alternative])
            response = self.client.post(
                url,
                repeated.urlencode(),
                content_type="application/x-www-form-urlencoded",
            )
            self.assertEqual(response.status_code, 400, field)
            self.assertFalse(response.context["save_available"])
            self.assertEqual(counts(), before)
        for field, value in (
            ("party", str(private.party_id)),
            ("source_reference", "PRIVATE CORRECTION DOCUMENT"),
            ("tax_amount", "0"),
            ("bank_posting", "1"),
        ):
            response = self.client.post(url, {**saved_data, field: value})
            self.assertEqual(response.status_code, 400, field)
            self.assertFalse(response.context["save_available"])
            self.assertNotContains(response, "PRIVATE CORRECTION", status_code=400)
            self.assertEqual(counts(), before)
        self.assertEqual(self.client.get(url, saved_data).status_code, 200)
        self.assertEqual(counts(), before)
        self.assertRedirects(
            self.client.post(url, saved_data),
            reverse("opening_balance_detail", args=[item.pk]),
        )

        before = counts()
        historical_url = (
            reverse("opening_balance_detail", args=[item.pk]) + "?as_of=2026-09-11"
        )
        historical = self.client.get(historical_url)
        self.assertEqual(historical.status_code, 200)
        self.assertEqual(historical.context["position"]["opening"], Decimal(300))
        self.assertEqual(historical.context["position"]["correction_delta"], 0)
        self.assertNotContains(historical, 'href="' + url + '"')
        self.assertNotContains(
            historical,
            'href="' + reverse("opening_balance_match", args=[item.pk]) + '"',
        )
        self.assertEqual(self.client.post(historical_url, saved_data).status_code, 405)
        listing = self.client.get(reverse("opening_balances"), {"as_of": "2026-09-11"})
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(
            listing.context["items"][0]["position"]["opening"], Decimal(300)
        )
        self.assertNotContains(listing, 'href="' + url + '"')
        current = self.client.get(reverse("opening_balance_detail", args=[item.pk]))
        self.assertEqual(current.context["position"]["opening"], Decimal(350))
        self.assertEqual(counts(), before)

    def test_stale_correction_preserves_inputs_until_a_fresh_check_and_confirmation(
        self,
    ):
        org = Organization.objects.create(
            name="Synthetic stale principal correction", is_demo=True
        )
        owner = get_user_model().objects.create_user(
            username="opening-correction-stale-owner"
        )
        Membership.objects.create(organization=org, user=owner, role="owner")
        party = Party.objects.create(
            organization=org, name="Synthetic stale reseller", kind="reseller"
        )
        self.client.force_login(owner)
        cutoff, effective = date(2026, 9, 1), date(2026, 9, 12)
        selection = {
            "kind": "receivable",
            "party": party,
            "date": cutoff,
            "source_date": date(2026, 8, 20),
            "source_reference": "SYN-STALE-ORIGINAL",
            "amount": "300",
        }
        context = openings.opening_balance_action_context(
            organization=org, action="record", **selection
        )
        item = openings.record_opening_balance(
            organization=org,
            actor=owner,
            **selection,
            reason="Synthetic verified original old residual",
            evidence="Synthetic original document and outstanding list",
            request_key=str(uuid4()),
            expected_fingerprint=context["fingerprint"],
            confirmed=True,
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

        url = reverse("opening_balance_correct", args=[item.pk])
        data = {
            "direction": "increase",
            "date": effective.isoformat(),
            "amount": "50",
            "reason": "Synthetic document proves original amount too small",
            "evidence": "Synthetic source and corrected outstanding list retained during recheck",
        }
        before = counts()
        checked = self.client.post(url, {**data, "intent": "check"})
        self.assertEqual(checked.status_code, 200)
        self.assertTrue(
            checked.context["save_available"], checked.context["source_context"]
        )
        self.assertEqual(counts(), before)
        form = checked.context["form"]
        original = {
            **data,
            "intent": "save",
            "confirmed": "on",
            "request_key": form["request_key"].value(),
            "expected_fingerprint": form["expected_fingerprint"].value(),
        }

        competing = openings.opening_balance_action_context(
            organization=org,
            action="correct",
            item=item,
            direction="decrease",
            date=effective,
            amount="20",
        )
        openings.correct_opening_balance(
            organization=org,
            actor=owner,
            item=item,
            direction="decrease",
            date=effective,
            amount="20",
            reason="Synthetic competing documented principal correction",
            evidence="Synthetic independently checked original source",
            request_key=str(uuid4()),
            expected_fingerprint=competing["fingerprint"],
            confirmed=True,
        )
        before = counts()
        rejected = self.client.post(url, original)
        self.assertEqual(rejected.status_code, 400)
        self.assertFalse(rejected.context["save_available"])
        self.assertContains(rejected, "Isian Anda tetap tersimpan", status_code=400)
        for name in (
            "direction",
            "date",
            "amount",
            "reason",
            "evidence",
            "request_key",
            "expected_fingerprint",
        ):
            self.assertEqual(
                str(rejected.context["form"][name].value()), str(original[name])
            )
        self.assertEqual(counts(), before)
        self.assertFalse(
            OpeningBalanceCorrection.objects.filter(
                request_key=original["request_key"]
            ).exists()
        )

        refreshed = self.client.post(url, {**original, "intent": "check"})
        self.assertEqual(refreshed.status_code, 200)
        self.assertTrue(
            refreshed.context["save_available"], refreshed.context["source_context"]
        )
        fresh_form = refreshed.context["form"]
        for name in ("direction", "date", "reason", "evidence"):
            self.assertEqual(str(fresh_form[name].value()), str(data[name]))
        self.assertEqual(Decimal(fresh_form["amount"].value()), Decimal(data["amount"]))
        self.assertNotEqual(fresh_form["request_key"].value(), original["request_key"])
        self.assertNotEqual(
            fresh_form["expected_fingerprint"].value(), original["expected_fingerprint"]
        )
        self.assertFalse(fresh_form["confirmed"].value())
        self.assertEqual(refreshed.context["source_context"]["before"], Decimal(280))
        self.assertEqual(refreshed.context["source_context"]["after"], Decimal(330))
        self.assertEqual(counts(), before)
        revised = {
            **data,
            "intent": "save",
            "request_key": fresh_form["request_key"].value(),
            "expected_fingerprint": fresh_form["expected_fingerprint"].value(),
        }
        self.assertEqual(self.client.post(url, revised).status_code, 400)
        self.assertEqual(counts(), before)
        saved = self.client.post(url, {**revised, "confirmed": "on"})
        self.assertRedirects(saved, reverse("opening_balance_detail", args=[item.pk]))
        self.assertEqual(OpeningBalanceCorrection.objects.filter(item=item).count(), 2)
        self.assertEqual(
            OpeningBalanceCorrection.objects.filter(
                request_key=revised["request_key"]
            ).count(),
            1,
        )
        self.assertFalse(
            OpeningBalanceCorrection.objects.filter(
                request_key=original["request_key"]
            ).exists()
        )
        item.refresh_from_db()
        self.assertEqual(item.amount, Decimal(300))
        balances = ledger.report_balances(organization=org, as_of=effective)
        self.assertEqual(
            (balances["AR"], balances["EQUITY"], balances["BANK"]),
            (Decimal(330), Decimal(-330), 0),
        )
        self.assertEqual((balances["REVENUE"], balances["EXPENSE"]), (0, 0))
        self.assertEqual(sum(balances.values()), 0)
        self.assertEqual(
            (
                BankTransaction.objects.count(),
                BankPosting.objects.count(),
                Invoice.objects.count(),
                Bill.objects.count(),
            ),
            (0, 0, 0, 0),
        )
