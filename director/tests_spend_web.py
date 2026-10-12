"""Public staff journeys for budget spending, using synthetic sources only."""

from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import Membership, Organization
from director import services
from finance import tests_budget_link
from finance.tests_tax_money import TaxMoneyFixtures


@override_settings(
    DIRECTOR_OPENROUTER_API_KEY="", DIRECTOR_AI_PRIVATE_AGGREGATES_ENABLED=False
)
class BudgetSpendWebTests(TaxMoneyFixtures, TestCase):
    bill = tests_budget_link.BillBudgetLinkTests.bill
    no_withholding = tests_budget_link.BillBudgetLinkTests.no_withholding
    payment = tests_budget_link.BillBudgetLinkTests.payment

    def setUp(self):
        super().setUp()
        self.users = {"owner": self.actor}
        for role in ("finance", "marketing", "auditor"):
            user = get_user_model().objects.create_user(username="spend-web-" + role)
            Membership.objects.create(organization=self.org, user=user, role=role)
            self.users[role] = user
        self.client.force_login(self.users["finance"])

    def budget_source(self, *, kind="open_commitment", reported="30000000"):
        budget = services.create_budget(
            organization=self.org,
            actor=self.users["finance"],
            title="Synthetic campaign",
            year=2026,
            month=9,
            channel="meta_ads",
            amount="80000000",
        )
        budget = services.transition_budget(
            organization=self.org,
            actor=self.users["finance"],
            budget=budget,
            action="submit",
            expected_version=budget.version,
        )
        budget = services.transition_budget(
            organization=self.org,
            actor=self.actor,
            budget=budget,
            action="approve",
            expected_version=budget.version,
            notes="Synthetic approved campaign limit",
        )
        entry = services.add_budget_entry(
            organization=self.org,
            actor=self.users["finance"],
            budget=budget,
            kind=kind,
            amount="80000000",
            date=date(2026, 9, 8),
            description="Synthetic original campaign plan",
            reference="SYN-SPEND-ORIGIN",
            idempotency_key="SYN-SPEND-ORIGIN",
        )
        if reported:
            services.record_budget_payment(
                organization=self.org,
                actor=self.users["finance"],
                entry=entry,
                amount=reported,
                date=date(2026, 9, 9),
                description="Synthetic original reported payment",
                reference="SYN-REPORTED",
                idempotency_key="SYN-REPORTED",
            )
        return budget, entry

    def request_data(self, url, **changes):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        data = {
            name: form[name].value()
            for name in form.fields
            if form[name].is_hidden and form[name].value() is not None
        }
        data.update(
            date="2026-09-20",
            reason="Synthetic checked staff decision",
            evidence="Synthetic exact document and recorded facts checked",
            confirmed="on",
            intent="save",
        )
        data.update(changes)
        return data

    def adopt(self, *, kind="open_commitment", reported="30000000"):
        from director.spend_models import BudgetSpend

        deferred_report = reported if kind == "reservation" else ""
        budget, entry = self.budget_source(
            kind=kind, reported="" if deferred_report else reported
        )
        url = reverse("director:spend_adopt", args=[budget.pk, entry.pk])
        data = self.request_data(url, date="2026-09-10")
        self.assertEqual(self.client.post(url, data).status_code, 302)
        spend = BudgetSpend.objects.get(origin_entry=entry)
        if deferred_report:
            payment_url = reverse("director:spend_report_payment", args=[spend.pk])
            self.assertEqual(
                self.client.post(
                    payment_url,
                    self.request_data(
                        payment_url, date="2026-09-11", amount=deferred_report
                    ),
                ).status_code,
                302,
            )
        return budget, entry, spend

    def position(self, spend):
        from director.spend_services import spend_position

        return spend_position(
            organization=self.org, spend=spend, as_of=timezone.localdate()
        )

    def link_ui(self, spend, entry, bill, *, date_value="2026-09-15"):
        from director.spend_models import BudgetBillLink

        url = reverse("director:spend_link_bill", args=[spend.pk])
        data = self.request_data(
            url,
            date=date_value,
            bill=str(bill.pk),
            coverage_source=f"entry:{entry.pk}",
            coverage_amount=str(bill.amount),
            intent="preview",
        )
        before = BudgetBillLink.objects.count()
        preview = self.client.post(url, data)
        self.assertContains(preview, "Tagihan yang diperiksa")
        self.assertEqual(BudgetBillLink.objects.count(), before)
        data["expected_bill_fingerprint"] = preview.context["form"][
            "expected_bill_fingerprint"
        ].value()
        data["intent"] = "save"
        self.assertEqual(self.client.post(url, data).status_code, 302)
        return BudgetBillLink.objects.get(spend=spend, voided_at__isnull=True), data

    def test_existing_commitment_has_a_clear_manage_spend_action(self):
        org = Organization.objects.create(name="Synthetic budget spend HTTP company")
        finance = get_user_model().objects.create_user(username="spend-http-finance")
        owner = get_user_model().objects.create_user(username="spend-http-owner")
        Membership.objects.create(organization=org, user=finance, role="finance")
        Membership.objects.create(organization=org, user=owner, role="owner")
        budget = services.create_budget(
            organization=org,
            actor=finance,
            title="Synthetic campaign authorization",
            year=2026,
            month=9,
            channel="meta_ads",
            amount="80000000",
        )
        budget = services.transition_budget(
            organization=org,
            actor=finance,
            budget=budget,
            action="submit",
            expected_version=budget.version,
        )
        budget = services.transition_budget(
            organization=org,
            actor=owner,
            budget=budget,
            action="approve",
            expected_version=budget.version,
            notes="Synthetic reviewed campaign spending limit",
        )
        commitment = services.add_budget_entry(
            organization=org,
            actor=finance,
            budget=budget,
            kind="open_commitment",
            amount="80000000",
            date=date(2026, 9, 8),
            description="Synthetic contracted campaign",
            reference="SYN-SPEND-AGREEMENT",
            idempotency_key="SYN-SPEND-ORIGINAL-COMMITMENT",
        )
        services.record_budget_payment(
            organization=org,
            actor=finance,
            entry=commitment,
            amount="30000000",
            date=date(2026, 9, 9),
            description="Synthetic reported partial campaign payment",
            reference="SYN-SPEND-PAYMENT-REPORT",
            idempotency_key="SYN-SPEND-ORIGINAL-PAYMENT",
        )
        self.client.force_login(finance)
        response = self.client.get(reverse("director:budget_detail", args=[budget.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Kelola belanja ini")
        self.assertContains(
            response, reverse("director:spend_adopt", args=[budget.pk, commitment.pk])
        )

    def test_partial_conversion_and_retry_preserve_original_payment_and_cost_total(
        self,
    ):
        from director.models import BudgetEntry
        from director.spend_models import BudgetSpendMovement
        from finance.models import BankTransaction, Journal

        budget, entry, spend = self.adopt()
        originals = list(
            BudgetEntry.objects.filter(organization=self.org).order_by("pk").values()
        )
        url = reverse("director:spend_convert", args=[spend.pk])
        data = self.request_data(
            url, transfer="open_commitment:incurred", amount="20000000"
        )
        for _ in range(2):
            self.assertEqual(self.client.post(url, data).status_code, 302)
        position = self.position(spend)
        self.assertEqual(
            (
                position["incurred"],
                position["open_commitments"],
                position["consumed"],
                position["reported_payments"],
            ),
            ("20000000.00", "60000000.00", "80000000.00", "30000000.00"),
        )
        self.assertEqual(position["verified_cash"], "0.00")
        self.assertEqual(
            BudgetSpendMovement.objects.filter(spend=spend, kind="convert").count(), 1
        )
        self.assertEqual(
            originals,
            list(
                BudgetEntry.objects.filter(organization=self.org)
                .order_by("pk")
                .values()
            ),
        )
        self.assertFalse(Journal.objects.filter(organization=self.org).exists())
        self.assertFalse(BankTransaction.objects.filter(organization=self.org).exists())
        detail = self.client.get(reverse("director:spend_detail", args=[spend.pk]))
        self.assertContains(detail, "Rp20.000.000")
        self.assertContains(detail, "Rp60.000.000")
        self.assertContains(detail, "Uang keluar bank yang dicocokkan Finance")
        legacy = self.client.get(reverse("director:budget_detail", args=[budget.pk]))
        self.assertContains(legacy, reverse("director:spend_detail", args=[spend.pk]))
        self.assertNotContains(
            legacy, reverse("director:budget_entry_replace", args=[budget.pk, entry.pk])
        )

    def test_stale_spend_preserves_inputs_then_explicit_refresh_can_save(self):
        from director.spend_models import BudgetSpendMovement

        _, _, spend = self.adopt()
        url = reverse("director:spend_convert", args=[spend.pk])
        data = self.request_data(
            url, transfer="open_commitment:incurred", amount="20000000"
        )
        payment_url = reverse("director:spend_report_payment", args=[spend.pk])
        self.assertEqual(
            self.client.post(
                payment_url,
                self.request_data(payment_url, amount="1000000", date="2026-09-15"),
            ).status_code,
            302,
        )
        failed = self.client.post(url, data)
        self.assertEqual(failed.status_code, 200)
        self.assertTrue(failed.context["form"].errors)
        self.assertEqual(failed.context["form"]["amount"].value(), data["amount"])
        self.assertEqual(failed.context["form"]["reference"].value(), data["reference"])
        self.assertContains(failed, "Muat posisi terbaru")
        self.assertFalse(
            BudgetSpendMovement.objects.filter(spend=spend, kind="convert").exists()
        )
        refreshed = self.client.post(url, {**data, "intent": "refresh"})
        self.assertEqual(refreshed.context["form"]["amount"].value(), "20000000")
        for name in ("expected_version", "expected_fingerprint"):
            data[name] = refreshed.context["form"][name].value()
        self.assertEqual(self.client.post(url, data).status_code, 302)

    def test_owner_can_replace_wrong_partial_movement_and_release_residual(self):
        from director.spend_models import BudgetSpendMovement

        _, _, spend = self.adopt()
        url = reverse("director:spend_convert", args=[spend.pk])
        self.assertEqual(
            self.client.post(
                url,
                self.request_data(
                    url, transfer="open_commitment:incurred", amount="20000000"
                ),
            ).status_code,
            302,
        )
        movement = BudgetSpendMovement.objects.get(spend=spend, kind="convert")
        correction = reverse(
            "director:spend_correction",
            args=[spend.pk, "movement", movement.pk, "correct"],
        )
        self.assertEqual(self.client.get(correction).status_code, 403)
        self.client.force_login(self.actor)
        data = self.request_data(
            correction,
            date="2026-09-21",
            correction="replace",
            transfer="open_commitment:incurred",
            amount="15000000",
        )
        self.assertEqual(self.client.post(correction, data).status_code, 302)
        position = self.position(spend)
        self.assertEqual(
            (position["incurred"], position["open_commitments"]),
            ("15000000.00", "65000000.00"),
        )
        self.assertTrue(
            BudgetSpendMovement.objects.filter(reverse_of=movement).exists()
        )
        release = reverse("director:spend_release", args=[spend.pk])
        self.assertEqual(
            self.client.post(
                release,
                self.request_data(
                    release,
                    date="2026-09-22",
                    bucket="open_commitment",
                    amount="10000000",
                ),
            ).status_code,
            302,
        )
        self.assertEqual(self.position(spend)["consumed"], "70000000.00")

    def test_existing_prepaid_bill_link_and_public_release_do_not_duplicate_consumption(
        self,
    ):
        from finance.models import Journal

        _, entry, spend = self.adopt(kind="reservation", reported="")
        bill = self.bill(prepaid=True)
        before = Journal.objects.filter(organization=self.org).count()
        self.link_ui(spend, entry, bill)
        self.assertEqual(Journal.objects.filter(organization=self.org).count(), before)
        release_url = reverse("prepayment_release", args=[bill.pk])
        self.assertEqual(
            self.client.post(
                release_url, self.request_data(release_url, amount="20000000")
            ).status_code,
            302,
        )
        position = self.position(spend)
        self.assertEqual(
            (
                position["incurred"],
                position["open_commitments"],
                position["reservations"],
                position["consumed"],
            ),
            ("20000000.00", "40000000.00", "20000000.00", "80000000.00"),
        )

    def test_unlinked_bill_changed_after_preview_requires_explicit_recheck(self):
        from director.spend_models import BudgetBillLink
        from finance import services as ledger

        _, entry, spend = self.adopt(kind="reservation", reported="")
        bill = self.bill(prepaid=True, approved=False)
        url = reverse("director:spend_link_bill", args=[spend.pk])
        data = self.request_data(
            url,
            date="2026-09-15",
            bill=str(bill.pk),
            coverage_source=f"entry:{entry.pk}",
            coverage_amount="60000000",
            intent="preview",
        )
        preview = self.client.post(url, data)
        old_hash = preview.context["form"]["expected_bill_fingerprint"].value()
        ledger.approve_bill(organization=self.org, actor=self.actor, bill=bill)
        data.update(intent="save", expected_bill_fingerprint=old_hash)
        failed = self.client.post(url, data)
        self.assertTrue(failed.context["form"].errors)
        self.assertEqual(
            failed.context["form"]["expected_bill_fingerprint"].value(), old_hash
        )
        self.assertEqual(failed.context["form"]["coverage_amount"].value(), "60000000")
        self.assertContains(failed, "Periksa tagihan terbaru")
        self.assertFalse(BudgetBillLink.objects.exists())
        preview = self.client.post(url, {**data, "intent": "preview"})
        fresh_hash = preview.context["form"]["expected_bill_fingerprint"].value()
        self.assertNotEqual(fresh_hash, old_hash)
        self.assertEqual(
            self.client.post(
                url, {**data, "expected_bill_fingerprint": fresh_hash}
            ).status_code,
            302,
        )

    def test_one_bill_can_replace_both_partial_cost_and_remaining_commitment(self):
        from director.spend_models import BudgetBillLink, BudgetSpendMovement

        _, entry, spend = self.adopt(reported="")
        convert_url = reverse("director:spend_convert", args=[spend.pk])
        self.assertEqual(
            self.client.post(
                convert_url,
                self.request_data(
                    convert_url,
                    date="2026-09-12",
                    transfer="open_commitment:incurred",
                    amount="20000000",
                ),
            ).status_code,
            302,
        )
        movement = BudgetSpendMovement.objects.get(spend=spend, kind="convert")
        bill = self.bill(amount="80000000", prepaid=False)
        url = reverse("director:spend_link_bill", args=[spend.pk])
        response = self.client.get(url)
        entry_field, movement_field = (
            f"coverage_entry_{entry.pk}",
            f"coverage_movement_{movement.pk}",
        )
        self.assertIn(
            entry_field,
            response.context["form"].fields,
            "Finance must explicitly cover the remaining commitment as well as the incurred portion",
        )
        self.assertIn(movement_field, response.context["form"].fields)
        data = self.request_data(
            url,
            date="2026-09-15",
            bill=str(bill.pk),
            intent="preview",
            **{entry_field: "60000000", movement_field: "20000000"},
        )
        preview = self.client.post(url, data)
        self.assertContains(preview, "Tagihan yang diperiksa")
        data.update(
            intent="save",
            expected_bill_fingerprint=preview.context["form"][
                "expected_bill_fingerprint"
            ].value(),
        )
        for _ in range(2):
            self.assertEqual(self.client.post(url, data).status_code, 302)
        link = BudgetBillLink.objects.get(spend=spend, voided_at__isnull=True)
        self.assertEqual(
            {row["source_key"]: row["amount"] for row in link.coverage},
            {
                f"entry:{entry.pk}": "60000000.00",
                f"movement:{movement.pk}": "20000000.00",
            },
        )
        position = self.position(spend)
        self.assertEqual(
            (position["incurred"], position["open_commitments"], position["consumed"]),
            ("80000000.00", "0.00", "80000000.00"),
        )

    def test_owner_link_correction_and_void_leave_finance_journals_unchanged(self):
        from finance.models import Journal

        _, entry, spend = self.adopt(kind="reservation", reported="")
        first, second = (
            self.bill(prepaid=True),
            self.bill(amount="50000000", prepaid=True),
        )
        link, _ = self.link_ui(spend, entry, first)
        journals = list(
            Journal.objects.filter(organization=self.org).order_by("pk").values()
        )
        self.client.force_login(self.actor)
        url = reverse(
            "director:spend_correction",
            args=[spend.pk, "bill_link", link.pk, "correct"],
        )
        data = self.request_data(
            url,
            date="2026-09-16",
            bill=str(second.pk),
            coverage_source=f"entry:{entry.pk}",
            coverage_amount="50000000",
            intent="preview",
        )
        preview = self.client.post(url, data)
        data.update(
            intent="save",
            expected_bill_fingerprint=preview.context["form"][
                "expected_bill_fingerprint"
            ].value(),
        )
        self.assertEqual(self.client.post(url, data).status_code, 302)
        link.refresh_from_db()
        self.assertIsNotNone(link.voided_at)
        replacement = spend.bill_links.get(voided_at__isnull=True)
        self.assertEqual(replacement.bill_id, second.pk)
        void = reverse(
            "director:spend_correction",
            args=[spend.pk, "bill_link", replacement.pk, "void"],
        )
        self.assertEqual(
            self.client.post(
                void, self.request_data(void, date="2026-09-17")
            ).status_code,
            302,
        )
        self.assertEqual(self.position(spend)["reservations"], "80000000.00")
        self.assertEqual(
            journals,
            list(Journal.objects.filter(organization=self.org).order_by("pk").values()),
        )

    def test_owner_can_repair_reduced_draft_bill_coverage_through_public_form(self):
        from finance import services as ledger
        from finance.models import Journal

        _, entry, spend = self.adopt(kind="reservation", reported="")
        bill = self.bill(prepaid=False, approved=False)
        old_link, _ = self.link_ui(spend, entry, bill)
        bill.amount = Decimal(40000000)
        bill.save()
        ledger.approve_bill(organization=self.org, actor=self.actor, bill=bill)
        broken = self.position(spend)
        self.assertFalse(broken["current"])
        self.assertIsNone(broken["consumed"])
        original_journals = list(
            Journal.objects.filter(organization=self.org).order_by("pk").values()
        )
        self.client.force_login(self.actor)
        url = reverse(
            "director:spend_correction",
            args=[spend.pk, "bill_link", old_link.pk, "correct"],
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        field = f"coverage_entry_{entry.pk}"
        self.assertIn(
            field,
            response.context["form"].fields,
            "Owner must be able to reselect the original source and reduce the obsolete coverage",
        )
        data = self.request_data(
            url,
            date="2026-09-16",
            bill=str(bill.pk),
            intent="preview",
            **{field: "40000000"},
        )
        preview = self.client.post(url, data)
        self.assertContains(preview, "Tagihan yang diperiksa")
        data.update(
            intent="save",
            expected_bill_fingerprint=preview.context["form"][
                "expected_bill_fingerprint"
            ].value(),
        )
        self.assertEqual(self.client.post(url, data).status_code, 302)
        repaired = self.position(spend)
        self.assertTrue(repaired["current"])
        self.assertEqual(
            (repaired["incurred"], repaired["reservations"], repaired["consumed"]),
            ("40000000.00", "40000000.00", "80000000.00"),
        )
        old_link.refresh_from_db()
        self.assertIsNotNone(old_link.voided_at)
        self.assertEqual(
            old_link.coverage,
            [{"source_key": f"entry:{entry.pk}", "amount": "60000000.00"}],
        )
        self.assertEqual(
            spend.bill_links.get(voided_at__isnull=True).coverage,
            [{"source_key": f"entry:{entry.pk}", "amount": "40000000.00"}],
        )
        self.assertEqual(
            original_journals,
            list(Journal.objects.filter(organization=self.org).order_by("pk").values()),
        )

    def test_partial_payment_match_and_retry_count_cash_once_and_owner_can_withdraw_match(
        self,
    ):
        from director.spend_models import BudgetPaymentMatch
        from finance.models import Journal

        _, entry, spend = self.adopt(kind="reservation")
        bill = self.bill(prepaid=True)
        self.no_withholding(bill)
        first = self.payment(bill, "20000000", reference="SYN-UI-PAY-20")
        self.payment(bill, "15000000", day=date(2026, 9, 25), reference="SYN-UI-PAY-15")
        self.link_ui(spend, entry, bill, date_value="2026-09-26")
        journals = list(
            Journal.objects.filter(organization=self.org).order_by("pk").values()
        )
        source = self.position(spend)["manual_payment_sources"][0]["key"]
        url = reverse("director:spend_match_payment", args=[spend.pk])
        data = self.request_data(
            url,
            date="2026-09-26",
            manual_source_key=source,
            finance_event_id=f"bill_payment:{first.pk}",
            amount="20000000",
        )
        for _ in range(2):
            self.assertEqual(self.client.post(url, data).status_code, 302)
        position = self.position(spend)
        self.assertEqual(
            (
                position["verified_cash"],
                position["unmatched_reported_payments"],
                position["payments"],
            ),
            ("35000000.00", "10000000.00", "45000000.00"),
        )
        match = BudgetPaymentMatch.objects.get(spend=spend)
        self.client.force_login(self.actor)
        void = reverse(
            "director:spend_correction",
            args=[spend.pk, "payment_match", match.pk, "void"],
        )
        self.assertEqual(
            self.client.post(
                void, self.request_data(void, date="2026-09-27")
            ).status_code,
            302,
        )
        self.assertEqual(self.position(spend)["verified_cash"], "35000000.00")
        self.assertEqual(
            self.position(spend)["unmatched_reported_payments"], "30000000.00"
        )
        self.assertEqual(
            journals,
            list(Journal.objects.filter(organization=self.org).order_by("pk").values()),
        )

    def test_missing_sources_show_the_next_staff_task(self):
        _, _, spend = self.adopt(reported="")
        response = self.client.get(reverse("director:spend_link_bill", args=[spend.pk]))
        self.assertContains(response, "Belum ada tagihan yang tercatat")
        response = self.client.get(
            reverse("director:spend_match_payment", args=[spend.pk])
        )
        self.assertContains(response, "Belum ada laporan pembayaran")
        self.assertContains(response, "Periksa mutasi bank")

    def test_bill_entry_cross_org_roles_csrf_and_repeated_source_fields(self):
        from director.spend_models import BudgetBillLink

        _, entry, spend = self.adopt(kind="reservation", reported="")
        bill = self.bill(prepaid=True)
        entry_url = reverse("director:bill_spend_entry", args=[bill.pk])
        self.assertContains(
            self.client.get(reverse("bill_detail", args=[bill.pk])), entry_url
        )
        self.assertEqual(
            self.client.post(entry_url, {"spend": spend.pk}).status_code, 302
        )
        url = reverse("director:spend_link_bill", args=[spend.pk])
        data = self.request_data(
            url,
            date="2026-09-15",
            bill=[str(bill.pk), str(bill.pk)],
            coverage_source=f"entry:{entry.pk}",
            coverage_amount="60000000",
            intent="preview",
        )
        self.assertTrue(self.client.post(url, data).context["form"].errors)
        self.assertFalse(BudgetBillLink.objects.exists())
        for role in ("marketing", "auditor"):
            self.client.force_login(self.users[role])
            self.assertEqual(self.client.get(url).status_code, 403)
            self.assertEqual(self.client.post(url, data).status_code, 403)
        self.client.force_login(self.users["auditor"])
        response = self.client.get(reverse("director:spend_detail", args=[spend.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(
            response, reverse("director:spend_convert", args=[spend.pk])
        )
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.users["finance"])
        self.assertEqual(csrf.post(url, data).status_code, 403)
        other = Organization.objects.create(name="Synthetic other budget company")
        actor = get_user_model().objects.create_user(username="spend-other-finance")
        Membership.objects.create(organization=other, user=actor, role="finance")
        self.client.force_login(actor)
        self.assertEqual(self.client.get(url).status_code, 404)
