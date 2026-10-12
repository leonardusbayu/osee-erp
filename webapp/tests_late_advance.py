"""Late certificate UI acceptance through public routes, synthetic sources only."""

from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from core.models import Membership
from finance import services as finance
from finance.models import (
    AccountingPeriod,
    BankPosting,
    CustomerTaxSettlement,
)
from finance.tests_tax_money import TaxMoneyFixtures
from taxes import workflow as tax


class LateAdvanceFixtures(TaxMoneyFixtures):
    def advance_flow(self, *, credit=True):
        bank = self.movement(
            "490000", day=date(2026, 9, 1), reference="SYN-ADVANCE-OLD-CASH"
        )
        advance = finance.record_customer_advance(
            organization=self.org,
            actor=self.actor,
            party=self.party,
            transaction=bank,
            amount="490000",
            reference="SYN-ADVANCE",
            evidence="Synthetic old customer bank source",
        )
        invoice = finance.issue_invoice(
            organization=self.org, actor=self.actor, invoice=self.invoice()
        )
        application = finance.apply_customer_advance(
            organization=self.org,
            actor=self.actor,
            advance=advance,
            invoice=invoice,
            date=self.day,
            amount="490000",
            reference="SYN-ADVANCE-APPLIED",
            evidence="Synthetic exact invoice application",
        )
        context = self.pph23_context(
            direction="receivable", tax_point_date=date(2026, 9, 1), invoice=invoice
        )
        context["tax_point_basis"] = "paid"
        obligation = tax.prepare_obligation(
            organization=self.org,
            actor=self.actor,
            period="2026-09",
            rule_code="pph23_service_2",
            base="500000",
            direction="receivable",
            source_invoice=invoice,
            source_reference=invoice.number,
            source_evidence=self.evidence(),
            rule_reference="Synthetic professional treatment of actual dated advance",
            object_code="24-104-99",
            note="Synthetic exact tax point; no automatic treatment from advance",
            **context,
        )
        review = tax.record_external_review(
            organization=self.org,
            actor=self.actor,
            subject=obligation,
            decision={"action": "approve_obligation"},
            evidence=self.evidence(),
            professional_name="Synthetic professional",
            qualification_reference="Synthetic engagement",
            reviewed_on=date(2026, 10, 1),
            statement="Synthetic received report confirms tax point and both parties",
            report_confirmed=True,
        )
        tax.approve_tax_obligation(obligation, self.actor, external_review=review)
        if credit:
            self.credit(obligation)
        return bank, advance, invoice, application, obligation

    @staticmethod
    def journal_lines(journal):
        return list(
            journal.lines.order_by("pk").values_list("account", "debit", "credit")
        )


@override_settings(OPENROUTER_API_KEY="")
class LateAdvanceHTTPTests(LateAdvanceFixtures, TestCase):
    def setUp(self):
        super().setUp()
        self.users = {"owner": self.actor}
        for role in ("finance", "marketing", "auditor", "reviewer"):
            actor = get_user_model().objects.create_user(
                username="late-advance-" + role
            )
            Membership.objects.create(organization=self.org, user=actor, role=role)
            self.users[role] = actor

    def test_advance_source_is_a_friendly_existing_payment_choice(self):
        bank, advance, invoice, application, obligation = self.advance_flow()
        self.client.force_login(self.users["finance"])
        response = self.client.get(reverse("customer_tax_credit", args=[obligation.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Uang muka digunakan")
        self.assertContains(response, f'value="advance:{application.pk}"')
        self.assertContains(response, invoice.number)
        self.assertContains(response, "Uang bank baru")
        self.assertNotContains(response, "AdvanceApplication")
        self.assertNotContains(response, "Allocation")
        detail = self.client.get(reverse("taxes:obligation", args=[obligation.pk]))
        self.assertContains(
            detail, reverse("customer_tax_credit", args=[obligation.pk])
        )

    def payload(self, application, **changes):
        return dict(
            source_allocation=f"advance:{application.pk}",
            date="2026-10-01",
            reference="SYN-UI-LATE-CREDIT",
            evidence="Synthetic exact old advance application and verified certificate",
            confirmed="on",
            **changes,
        )

    def test_public_post_and_retry_settle_without_new_cash_or_changed_old_journals(
        self,
    ):
        bank, advance, invoice, application, obligation = self.advance_flow()
        before = [
            self.journal_lines(item) for item in (advance.journal, application.journal)
        ]
        AccountingPeriod.objects.update_or_create(
            organization=self.org, year=2026, month=9, defaults={"closed": True}
        )
        self.client.force_login(self.users["finance"])
        url = reverse("customer_tax_credit", args=[obligation.pk])
        payload = self.payload(application)
        for _ in range(2):
            response = self.client.post(url, payload)
            self.assertRedirects(response, reverse("financial_movements"))
        row = CustomerTaxSettlement.objects.get(organization=self.org)
        self.assertEqual(
            (
                row.source_advance_application_id,
                row.source_allocation_id,
                row.bank_posting_id,
            ),
            (application.pk, None, None),
        )
        self.assertEqual(
            (row.cash_amount, row.tax_amount, row.gross_amount),
            (Decimal(0), Decimal(10000), Decimal(10000)),
        )
        invoice.refresh_from_db()
        self.assertEqual(
            (invoice.paid_amount, invoice.outstanding_amount),
            (Decimal(500000), Decimal(0)),
        )
        self.assertEqual(
            finance.report_balances(organization=self.org)["BANK"], Decimal(490000)
        )
        self.assertEqual(advance.remaining_amount, Decimal(0))
        self.assertEqual(
            before,
            [
                self.journal_lines(item)
                for item in (advance.journal, application.journal)
            ],
        )
        self.assertFalse(row.journal.lines.filter(account="BANK").exists())
        self.assertEqual(CustomerTaxSettlement.objects.count(), 1)
        self.assertEqual(BankPosting.objects.filter(organization=self.org).count(), 1)
        response = self.client.get(url)
        self.assertContains(response, "Kredit pajak sudah tercatat")
        self.assertContains(response, "01 Okt 2026")
        self.assertNotContains(response, 'name="source_allocation"')
        history = self.client.get(reverse("financial_movements"))
        self.assertContains(history, "Uang muka digunakan untuk invoice")
        self.assertContains(history, "Uang masuk bank: 01 Sep 2026")
        self.assertContains(history, "Rp490.000")
        self.assertContains(history, "Rp10.000")
        detail = self.client.get(reverse("taxes:obligation", args=[obligation.pk]))
        self.assertContains(detail, "Uang bank baru")
        self.assertNotContains(detail, "Catat bukti potong untuk pembayaran lama")

    def test_backdated_error_preserves_source_date_reference_and_evidence(self):
        *_, application, obligation = self.advance_flow()
        self.client.force_login(self.users["finance"])
        data = self.payload(application)
        data["date"] = "2026-09-01"
        response = self.client.post(
            reverse("customer_tax_credit", args=[obligation.pk]), data
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors)
        for field in ("source_allocation", "date", "reference", "evidence"):
            self.assertEqual(response.context["form"][field].value(), data[field])
        self.assertFalse(CustomerTaxSettlement.objects.exists())

    def test_forged_dual_and_repeated_sources_are_rejected_without_writes(self):
        *_, application, obligation = self.advance_flow()
        self.client.force_login(self.users["finance"])
        url = reverse("customer_tax_credit", args=[obligation.pk])
        for conflict in (
            {"source_advance_application": str(application.pk)},
            {
                "source_allocation": [
                    f"advance:{application.pk}",
                    f"advance:{application.pk}",
                ]
            },
        ):
            data = self.payload(application)
            data.update(conflict)
            response = self.client.post(url, data)
            self.assertContains(response, "Pilih tepat satu pembayaran")
        self.assertFalse(CustomerTaxSettlement.objects.exists())

    def test_foreign_invoice_payment_choice_rejected_and_not_disclosed(self):
        *_, application, obligation = self.advance_flow()
        other_invoice = finance.issue_invoice(
            organization=self.org, actor=self.actor, invoice=self.invoice()
        )
        bank = self.movement("490000", reference="SYN-FOREIGN-INVOICE-PAYMENT")
        source = finance.reconcile_receipt(
            organization=self.org,
            actor=self.actor,
            invoice=other_invoice,
            transaction=bank,
            amount="490000",
        )
        self.client.force_login(self.users["finance"])
        data = self.payload(application)
        data["source_allocation"] = str(source.pk)
        response = self.client.post(
            reverse("customer_tax_credit", args=[obligation.pk]), data
        )
        self.assertTrue(response.context["form"].errors)
        self.assertNotContains(response, bank.reference)
        self.assertNotContains(response, other_invoice.number)
        self.assertFalse(CustomerTaxSettlement.objects.exists())

    def test_allocation_numeric_option_remains_usable(self):
        invoice = finance.issue_invoice(
            organization=self.org, actor=self.actor, invoice=self.invoice()
        )
        bank = self.movement("490000", reference="SYN-OLD-ALLOCATED-CASH")
        source = finance.reconcile_receipt(
            organization=self.org,
            actor=self.actor,
            invoice=invoice,
            transaction=bank,
            amount="490000",
        )
        obligation = self.approved(invoice=invoice)
        self.credit(obligation)
        self.client.force_login(self.users["finance"])
        data = self.payload(source)
        data["source_allocation"] = str(source.pk)
        response = self.client.post(
            reverse("customer_tax_credit", args=[obligation.pk]), data
        )
        self.assertRedirects(response, reverse("financial_movements"))
        row = CustomerTaxSettlement.objects.get()
        self.assertEqual(
            (row.source_allocation_id, row.source_advance_application_id),
            (source.pk, None),
        )

    def test_roles_csrf_and_other_organization_boundaries(self):
        *_, application, obligation = self.advance_flow()
        url = reverse("customer_tax_credit", args=[obligation.pk])
        for role in ("marketing", "reviewer", "auditor"):
            self.client.force_login(self.users[role])
            self.assertEqual(self.client.get(url).status_code, 403)
            self.assertEqual(
                self.client.post(url, self.payload(application)).status_code, 403
            )
        for role in ("reviewer", "auditor"):
            self.client.force_login(self.users[role])
            self.assertEqual(
                self.client.get(reverse("financial_movements")).status_code, 200
            )
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.users["finance"])
        self.assertEqual(
            csrf_client.post(url, self.payload(application)).status_code, 403
        )
        from core.models import Organization

        other = Organization.objects.create(name="Synthetic other private organization")
        outsider = get_user_model().objects.create_user(username="late-other-org")
        Membership.objects.create(organization=other, user=outsider, role="finance")
        self.client.force_login(outsider)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertFalse(CustomerTaxSettlement.objects.exists())

    def test_missing_certificate_has_one_clear_next_action(self):
        *_, application, obligation = self.advance_flow(credit=False)
        self.client.force_login(self.users["finance"])
        response = self.client.get(reverse("customer_tax_credit", args=[obligation.pk]))
        self.assertContains(response, "Periksa bukti potong terlebih dahulu")
        self.assertContains(response, reverse("taxes:obligation", args=[obligation.pk]))
        self.assertNotContains(response, 'name="source_allocation"')
        response = self.client.post(
            reverse("customer_tax_credit", args=[obligation.pk]),
            self.payload(application),
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(CustomerTaxSettlement.objects.exists())

    def test_changed_retry_is_an_error_with_inputs_retained(self):
        *_, application, obligation = self.advance_flow()
        self.client.force_login(self.users["finance"])
        url = reverse("customer_tax_credit", args=[obligation.pk])
        data = self.payload(application)
        self.assertEqual(self.client.post(url, data).status_code, 302)
        data["evidence"] = "Synthetic changed retry evidence must not replace original"
        response = self.client.post(url, data)
        self.assertTrue(response.context["form"].errors)
        self.assertEqual(response.context["form"]["evidence"].value(), data["evidence"])
        self.assertContains(response, "Permintaan yang berbeda belum tersimpan")
        self.assertEqual(CustomerTaxSettlement.objects.count(), 1)
