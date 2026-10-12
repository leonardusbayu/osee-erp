"""Public staff contracts for actual supplier advances; synthetic sources only."""

from datetime import date
from decimal import Decimal
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.db import connection
from django.http import QueryDict
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import AuditEvent, DomainEvent, Membership, Organization

from . import services as ledger
from . import supplier_advances as advances
from . import tests_budget_link as bill_fixtures
from . import tests_tax_money as tax_fixtures
from .models import (
    AccountingPeriod,
    BankAccount,
    BankPosting,
    BankTransaction,
    Journal,
    Party,
    SupplierAdvance,
    SupplierAdvanceApplication,
    SupplierAdvanceApplicationReversal,
    SupplierAdvanceRefund,
)


class SupplierAdvanceHTTPTests(TestCase):
    # Assignment avoids rediscovering tests from an inherited test class.
    fixture_setup = tax_fixtures.TaxMoneyFixtures.setUp
    movement = tax_fixtures.TaxMoneyFixtures.movement
    evidence = tax_fixtures.TaxMoneyFixtures.evidence
    external = tax_fixtures.TaxMoneyFixtures.external
    identity = tax_fixtures.TaxMoneyFixtures.identity
    pph23_context = tax_fixtures.TaxMoneyFixtures.pph23_context
    no_withholding = bill_fixtures.BillBudgetLinkTests.no_withholding
    positive_withholding = bill_fixtures.BillBudgetLinkTests.positive_withholding

    def setUp(self):
        self.fixture_setup()
        self.org.is_demo = True
        self.org.save(update_fields=["is_demo"])
        self.day = date(2026, 9, 10)
        self.client.force_login(self.owner)

    def counts(self):
        return tuple(
            model.objects.count()
            for model in (
                SupplierAdvance,
                SupplierAdvanceApplication,
                SupplierAdvanceRefund,
                SupplierAdvanceApplicationReversal,
                BankPosting,
                Journal,
                AuditEvent,
                DomainEvent,
            )
        )

    def record_data(self, bank=None, amount="1000"):
        bank = bank or self.movement(
            "-1000", day=date(2026, 9, 1), reference="SYN-ADVANCE-OUT"
        )
        return {
            "supplier": str(self.supplier.pk),
            "transaction": str(bank.pk),
            "amount": amount,
            "source_reference": "SYN-PROFORMA-1",
            "reason": "Synthetic documented pre-Bill cash",
            "evidence": "Synthetic original bank statement and purchase reference",
        }

    def checked(self, url, data):
        before = self.counts()
        response = self.client.post(url, {**data, "intent": "check"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.counts(), before)
        form = response.context["form"]
        self.assertFalse(form["confirmed"].value())
        return response, {
            **data,
            "request_key": form["request_key"].value(),
            "expected_fingerprint": form["expected_fingerprint"].value(),
            "confirmed": "on",
            "intent": "save",
        }

    def recorded(self, amount="1000", bank=None):
        url = reverse("supplier_advance_new")
        _, data = self.checked(url, self.record_data(bank, amount))
        response = self.client.post(url, data)
        row = SupplierAdvance.objects.get(
            organization=self.org, request_key=data["request_key"]
        )
        self.assertRedirects(
            response, reverse("supplier_advance_detail", args=[row.pk])
        )
        return row, data

    def bill(self, amount="600", *, reviewed=True, supplier=None):
        row = ledger.create_bill(
            organization=self.org,
            actor=self.owner,
            supplier=supplier or self.supplier,
            amount=amount,
            supplier_vat="0",
            date=self.day,
            service_date=self.day,
            category="expense",
            supplier_reference=f"SYN-BILL-{Journal.objects.count()}",
        )
        ledger.approve_bill(organization=self.org, actor=self.owner, bill=row)
        if reviewed:
            self.no_withholding(row)
        return row

    def application_data(self, bill, amount="400"):
        return {
            "bill": str(bill.pk),
            "date": "2026-09-10",
            "amount": amount,
            "reason": "Synthetic cash already paid applied to this Bill",
            "evidence": "Synthetic proforma and exact Bill checked",
        }

    def applied(self, row, bill, amount="400"):
        url = reverse("supplier_advance_apply", args=[row.pk])
        _, data = self.checked(url, self.application_data(bill, amount))
        self.assertRedirects(
            self.client.post(url, data),
            reverse("supplier_advance_detail", args=[row.pk]),
        )
        return SupplierAdvanceApplication.objects.get(
            request_key=data["request_key"]
        ), data

    def position(self, row):
        return advances.supplier_advance_position(
            organization=self.org, advance=row, as_of=timezone.localdate()
        )

    def user(self, role):
        user = get_user_model().objects.create_user(username=f"supplier-ui-{role}")
        Membership.objects.create(organization=self.org, user=user, role=role)
        return user

    def test_record_before_bill_apply_twice_refund_twice_and_keep_tax_task(self):
        row, _ = self.recorded()
        self.assertEqual(Decimal(self.position(row)["remaining"]), Decimal(1000))
        bill = self.bill()
        self.applied(row, bill, "400")
        self.assertEqual(
            (Decimal(self.position(row)["remaining"]), bill.outstanding_amount),
            (Decimal(600), Decimal(200)),
        )
        self.applied(row, bill, "200")
        self.assertEqual(
            (Decimal(self.position(row)["remaining"]), bill.outstanding_amount),
            (Decimal(400), Decimal(0)),
        )
        for amount, expected in (("150", "250"), ("250", "0")):
            bank = self.movement(
                amount, day=date(2026, 9, 12), reference=f"SYN-RETURN-{amount}"
            )
            url = reverse("supplier_advance_refund", args=[row.pk])
            _, data = self.checked(
                url,
                {
                    "transaction": str(bank.pk),
                    "amount": amount,
                    "reason": "Synthetic unused cash returned",
                    "evidence": "Synthetic incoming bank proof",
                },
            )
            self.assertRedirects(
                self.client.post(url, data),
                reverse("supplier_advance_detail", args=[row.pk]),
            )
            self.assertEqual(
                Decimal(self.position(row)["remaining"]), Decimal(expected)
            )
        self.assertEqual(self.position(row)["tax_review_status"], "pending")
        for url in (
            reverse("supplier_advances"),
            reverse("supplier_advance_detail", args=[row.pk]),
        ):
            response = self.client.get(url)
            self.assertContains(response, "Pemeriksaan pajak masih diperlukan")
            self.assertContains(response, "SYN-PROFORMA-1")
        self.assertEqual(
            ledger.report_balances(organization=self.org)["BANK"], Decimal(-600)
        )

    def test_owner_and_finance_write_legacy_readers_read_marketing_denied(self):
        row, _ = self.recorded()
        for role in ("finance", "reviewer", "auditor", "director", "marketing"):
            self.client.force_login(self.user(role))
            for url in (
                reverse("supplier_advances"),
                reverse("supplier_advance_detail", args=[row.pk]),
            ):
                response = self.client.get(url)
                self.assertEqual(
                    response.status_code, 403 if role == "marketing" else 200
                )
                if role in ("reviewer", "auditor", "director"):
                    self.assertNotContains(response, "Catat uang muka pemasok")
            form_url = reverse("supplier_advance_new")
            self.assertEqual(
                self.client.get(form_url).status_code, 200 if role == "finance" else 403
            )
            before = self.counts()
            self.assertEqual(
                self.client.post(form_url, {"intent": "check"}).status_code,
                200 if role == "finance" else 403,
            )
            self.assertEqual(self.counts(), before)

    def test_exact_record_retry_after_full_bank_use_and_close_changed_payload_conflicts(
        self,
    ):
        row, data = self.recorded()
        AccountingPeriod.objects.update_or_create(
            organization=self.org,
            year=2026,
            month=9,
            defaults={"closed": True, "closed_at": timezone.now()},
        )
        before = self.counts()
        url = reverse("supplier_advance_new")
        self.assertRedirects(
            self.client.post(url, data),
            reverse("supplier_advance_detail", args=[row.pk]),
        )
        self.assertEqual(self.counts(), before)
        response = self.client.post(
            url, {**data, "evidence": "Changed synthetic proof"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.context["form"]["transaction"].value(), data["transaction"]
        )
        self.assertEqual(self.counts(), before)

    def test_stale_save_preserves_inputs_until_explicit_check_and_new_confirmation(
        self,
    ):
        bank = self.movement("-1000", day=date(2026, 9, 1), reference="SYN-STALE-OUT")
        url = reverse("supplier_advance_new")
        _, data = self.checked(url, self.record_data(bank, "700"))
        context = advances.supplier_advance_action_context(
            organization=self.org,
            action="record",
            supplier=self.supplier,
            transaction=bank,
        )
        advances.record_supplier_advance(
            organization=self.org,
            actor=self.owner,
            supplier=self.supplier,
            transaction=bank,
            amount="400",
            source_reference="SYN-OTHER-PURPOSE",
            reason="Synthetic separate advance",
            evidence="Synthetic original source checked",
            request_key=str(uuid4()),
            expected_fingerprint=context["fingerprint"],
            confirmed=True,
        )
        before = self.counts()
        failed = self.client.post(url, data)
        self.assertEqual(failed.status_code, 400)
        for key in (
            "amount",
            "source_reference",
            "reason",
            "evidence",
            "request_key",
            "expected_fingerprint",
        ):
            self.assertEqual(str(failed.context["form"][key].value()), str(data[key]))
        self.assertEqual(self.counts(), before)
        refreshed = self.client.post(url, {**data, "intent": "check"})
        self.assertEqual(refreshed.status_code, 200)
        form = refreshed.context["form"]
        self.assertNotEqual(form["request_key"].value(), data["request_key"])
        self.assertNotEqual(
            form["expected_fingerprint"].value(), data["expected_fingerprint"]
        )
        self.assertFalse(form["confirmed"].value())
        self.assertEqual(form["amount"].value(), "700")
        self.assertEqual(self.counts(), before)

    def test_unknown_tax_and_other_supplier_bill_are_held_without_new_cash(self):
        row, _ = self.recorded()
        unknown = self.bill(reviewed=False)
        other_supplier = Party.objects.create(
            organization=self.org, name="Other synthetic supplier", kind="supplier"
        )
        wrong = self.bill(supplier=other_supplier)
        url = reverse("supplier_advance_apply", args=[row.pk])
        before = self.counts()
        for bill in (unknown, wrong):
            response = self.client.post(
                url, {**self.application_data(bill), "intent": "check"}
            )
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.context["save_available"])
            self.assertEqual(self.counts(), before)
        self.assertEqual(Decimal(self.position(row)["remaining"]), Decimal(1000))

    def test_positive_withholding_bill_is_held_with_a_specific_tax_next_action(self):
        row, _ = self.recorded()
        bill = self.bill(amount="300000", reviewed=False)
        self.positive_withholding(bill)
        before = self.counts()
        url = reverse("supplier_advance_apply", args=[row.pk])
        response = self.client.post(
            url, {**self.application_data(bill), "intent": "check"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "Penerapan uang muka untuk tagihan dengan potongan pajak belum didukung",
        )
        self.assertContains(response, "Periksa pajak tagihan")
        self.assertFalse(response.context["save_available"])
        self.assertEqual(self.counts(), before)

    def test_duplicate_fields_missing_confirmation_csrf_and_get_never_write(self):
        data = self.record_data()
        url = reverse("supplier_advance_new")
        _, checked = self.checked(url, data)
        repeated = QueryDict("", mutable=True)
        for key, value in checked.items():
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
            self.client.post(url, {**checked, "confirmed": ""}).status_code, 400
        )
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.owner)
        self.assertEqual(csrf_client.post(url, checked).status_code, 403)
        self.assertEqual(
            self.client.get(url, {**checked, "intent": "save"}).status_code, 200
        )
        self.assertEqual(self.client.put(url).status_code, 405)
        self.assertEqual(self.counts(), before)

    def test_full_owner_reversal_readback_and_finance_denial(self):
        row, _ = self.recorded()
        bill = self.bill()
        application, _ = self.applied(row, bill)
        url = reverse("supplier_advance_application_reverse", args=[application.pk])
        bank_before = ledger.report_balances(organization=self.org)["BANK"]
        response, data = self.checked(
            url,
            {
                "date": "2026-09-12",
                "reason": "Synthetic wrong Bill application",
                "evidence": "Synthetic correction instruction and original Bill",
            },
        )
        self.assertNotIn("amount", response.context["form"].fields)
        self.assertRedirects(
            self.client.post(url, data),
            reverse("supplier_advance_detail", args=[row.pk]),
        )
        self.assertEqual(
            (Decimal(self.position(row)["remaining"]), bill.outstanding_amount),
            (Decimal(1000), Decimal(600)),
        )
        self.assertEqual(
            ledger.report_balances(organization=self.org)["BANK"], bank_before
        )
        detail = self.client.get(reverse("supplier_advance_detail", args=[row.pk]))
        self.assertContains(detail, "Penerapan dibalik")
        before = self.counts()
        self.assertRedirects(
            self.client.post(url, data),
            reverse("supplier_advance_detail", args=[row.pk]),
        )
        self.assertEqual(self.counts(), before)
        self.client.force_login(self.user("finance"))
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(url, data).status_code, 403)
        self.assertEqual(self.counts(), before)

    def test_foreign_source_selection_does_not_reveal_foreign_names(self):
        foreign = Organization.objects.create(name="Foreign synthetic organization")
        account = BankAccount.objects.create(
            organization=foreign, name="PRIVATE FOREIGN BANK", account_number="FOREIGN"
        )
        bank = BankTransaction.objects.create(
            organization=foreign,
            account=account,
            date=self.day,
            amount="-1000",
            reference="PRIVATE FOREIGN REFERENCE",
        )
        data = self.record_data(bank)
        before = self.counts()
        response = self.client.post(
            reverse("supplier_advance_new"), {**data, "intent": "check"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "PRIVATE FOREIGN")
        self.assertFalse(response.context["save_available"])
        self.assertEqual(self.counts(), before)

    def test_application_and_refund_exact_retries_after_all_remaining_is_consumed(self):
        row, _ = self.recorded()
        bill = self.bill(amount="600")
        _, application_data = self.applied(row, bill, "600")
        bank = self.movement("400", day=date(2026, 9, 12), reference="SYN-ALL-RETURN")
        refund_url = reverse("supplier_advance_refund", args=[row.pk])
        _, refund_data = self.checked(
            refund_url,
            {
                "transaction": str(bank.pk),
                "amount": "400",
                "reason": "Synthetic final unused cash returned",
                "evidence": "Synthetic final incoming proof",
            },
        )
        self.assertRedirects(
            self.client.post(refund_url, refund_data),
            reverse("supplier_advance_detail", args=[row.pk]),
        )
        before = self.counts()
        for url, data in (
            (reverse("supplier_advance_apply", args=[row.pk]), application_data),
            (refund_url, refund_data),
        ):
            self.assertRedirects(
                self.client.post(url, data),
                reverse("supplier_advance_detail", args=[row.pk]),
            )
        self.assertEqual(self.counts(), before)
        self.assertEqual(Decimal(self.position(row)["remaining"]), Decimal(0))

    def test_source_damage_is_unassessable_and_exact_retry_does_not_bypass_proof(self):
        row, data = self.recorded()
        self.assertTrue(self.org.is_demo)
        self.assertTrue(connection.in_atomic_block)
        line = row.journal.lines.get(account="SUPPLIER_ADVANCE")
        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {connection.ops.quote_name(line._meta.db_table)} SET account=%s WHERE id=%s",
                ["EXPENSE", line.pk],
            )
        before = self.counts()
        response = self.client.get(reverse("supplier_advance_detail", args=[row.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Belum dapat diperiksa")
        self.assertFalse(response.context["position"]["current"])
        self.assertIsNone(response.context["position"]["remaining"])
        self.assertEqual(
            self.client.post(reverse("supplier_advance_new"), data).status_code, 400
        )
        self.assertEqual(self.counts(), before)

    def test_source_check_suggests_actual_remaining_without_guessing_original_amount(
        self,
    ):
        bank = self.movement("-1000", day=date(2026, 9, 1), reference="SYN-SUGGESTION")
        row, _ = self.recorded("400", bank)
        before = self.counts()
        response = self.client.post(
            reverse("supplier_advance_new"),
            {
                "intent": "check",
                "supplier": str(self.supplier.pk),
                "transaction": str(bank.pk),
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            Decimal(response.context["form"]["amount"].value()), Decimal(600)
        )
        self.assertContains(response, "Saran dari sisa yang tersedia")
        self.assertEqual(self.counts(), before)
        self.assertEqual(Decimal(self.position(row)["remaining"]), Decimal(400))

    def test_other_organization_advance_route_is_not_readable_or_writable(self):
        foreign = Organization.objects.create(
            name="Private synthetic company", is_demo=True
        )
        Membership.objects.create(organization=foreign, user=self.owner, role="owner")
        supplier = Party.objects.create(
            organization=foreign, kind="supplier", name="PRIVATE SUPPLIER"
        )
        account = BankAccount.objects.create(
            organization=foreign, name="PRIVATE BANK", account_number="PRIVATE"
        )
        ledger.import_bank_csv(
            organization=foreign,
            actor=self.owner,
            account=account,
            content="date,reference,description,amount\n2026-09-01,PRIVATE-OUT,Synthetic,-10\n",
        )
        bank = BankTransaction.objects.get(
            organization=foreign, reference="PRIVATE-OUT"
        )
        context = advances.supplier_advance_action_context(
            organization=foreign, action="record", supplier=supplier, transaction=bank
        )
        row = advances.record_supplier_advance(
            organization=foreign,
            actor=self.owner,
            supplier=supplier,
            transaction=bank,
            amount="10",
            source_reference="PRIVATE PURPOSE",
            reason="Synthetic private company advance",
            evidence="Synthetic original foreign source",
            request_key=str(uuid4()),
            expected_fingerprint=context["fingerprint"],
            confirmed=True,
        )
        before = self.counts()
        session = self.client.session
        session["organization_id"] = self.org.pk
        session.save()
        for route in (
            "supplier_advance_detail",
            "supplier_advance_apply",
            "supplier_advance_refund",
        ):
            response = self.client.get(reverse(route, args=[row.pk]))
            self.assertEqual(response.status_code, 404)
            self.assertNotContains(response, "PRIVATE", status_code=404)
        self.assertEqual(
            self.client.post(
                reverse("supplier_advance_refund", args=[row.pk]), {"intent": "check"}
            ).status_code,
            404,
        )
        self.assertEqual(self.counts(), before)

    def test_check_enforces_each_dated_balance_without_a_blanket_latest_date_ban(self):
        row, _ = self.recorded()
        bill = self.bill(amount="1000")
        application, _ = self.applied(row, bill, "600")
        reverse_url = reverse(
            "supplier_advance_application_reverse", args=[application.pk]
        )
        _, reversal = self.checked(
            reverse_url,
            {
                "date": "2026-09-12",
                "reason": "Synthetic corrected application",
                "evidence": "Synthetic full correction proof",
            },
        )
        self.assertRedirects(
            self.client.post(reverse_url, reversal),
            reverse("supplier_advance_detail", args=[row.pk]),
        )
        url = reverse("supplier_advance_apply", args=[row.pk])
        data = {
            **self.application_data(bill, "800"),
            "date": "2026-09-11",
            "intent": "check",
        }
        before = self.counts()
        held = self.client.post(url, data)
        self.assertEqual(held.status_code, 200)
        self.assertFalse(held.context["save_available"])
        self.assertEqual(self.counts(), before)
        # Rp400 on 11 Sep preserves every prefix; the later reversal is not
        # an artificial prohibition on an otherwise valid actual date.
        _, valid = self.checked(url, {**data, "amount": "400"})
        self.assertRedirects(
            self.client.post(url, valid),
            reverse("supplier_advance_detail", args=[row.pk]),
        )
        self.assertEqual(
            (Decimal(self.position(row)["remaining"]), bill.outstanding_amount),
            (Decimal(600), Decimal(600)),
        )
