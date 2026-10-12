"""Synthetic tax-money regressions: scoped sources, actual cash and credits."""

import re
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import Membership, Organization
from taxes import tests_workflow as tax_fixtures
from taxes import workflow as work
from taxes.models import TaxProfile

from . import services as f
from . import tests_remediation as finance_fixtures
from .models import (
    AccountingPeriod,
    CustomerTaxSettlement,
    Journal,
    OwnTaxPayment,
    OwnTaxRecognition,
)
from .settlement import applied_payment_amounts, collectible_amounts


class TaxMoneyFixtures:
    invoice = finance_fixtures.FinanceRemediationTests.invoice
    movement = finance_fixtures.FinanceRemediationTests.movement
    evidence = tax_fixtures.TaxReleaseWorkflowTests.evidence
    external = tax_fixtures.TaxReleaseWorkflowTests.external
    profile = tax_fixtures.TaxReleaseWorkflowTests.profile
    identity = tax_fixtures.TaxReleaseWorkflowTests.identity
    pph23_context = tax_fixtures.TaxReleaseWorkflowTests.pph23_context
    certificate = tax_fixtures.TaxReleaseWorkflowTests.certificate

    def setUp(self):
        finance_fixtures.FinanceRemediationTests.setUp(self)
        self.owner = self.actor
        self.day = date(2026, 9, 10)

    def approved(
        self,
        *,
        direction="receivable",
        invoice=None,
        period=None,
        base="500000",
        rule="pph23_service_2",
    ):
        context = (
            self.pph23_context(
                direction=direction, tax_point_date=self.day, invoice=invoice
            )
            if rule.startswith("pph23_")
            else {}
        )
        row = work.prepare_obligation(
            organization=self.org,
            actor=self.actor,
            period=period or self.day.replace(day=1),
            rule_code=rule,
            base=base,
            direction=direction,
            source_reference="Synthetic invoice or turnover",
            rule_reference="Synthetic professional classification",
            object_code="24-104-99",
            source_evidence=self.evidence(),
            source_invoice=invoice,
            **context,
        )
        return work.approve_tax_obligation(
            row,
            self.actor,
            external_review=self.external(row, {"action": "approve_obligation"}),
        )

    def credit(self, row):
        return work.verify_obligation_evidence(
            organization=self.org,
            actor=self.actor,
            obligation=row,
            kind="credit",
            evidence=self.evidence(),
            reference=f"SYN-CREDIT-{row.pk}",
            taxpayer_reference="1234567890123456",
            period=row.period,
            amount=row.amount,
            note="Synthetic original certificate matched",
            matches_confirmed=True,
            **self.certificate(row),
        )

    def net_receipt(self, row, *, amount="490000", reference="NET1", bank=None):
        return f.reconcile_customer_tax_receipt(
            organization=self.org,
            actor=self.actor,
            obligation=row,
            transaction=bank or self.movement(amount, reference=reference),
            amount=amount,
            reference=reference,
            evidence="Synthetic receipt and certificate",
        )

    def final_profile(self):
        profile = TaxProfile.objects.create(
            organization=self.org,
            registration_date=date(2024, 1, 1),
            registration_evidence="Synthetic SKT",
            taxpayer_reference="1234567890123456",
            vat_status_evidence="Synthetic non-PKP",
            vat_status_effective_from=date(2024, 1, 1),
        )
        data = dict(
            regime="final",
            effective_from=date(2026, 1, 1),
            final_regime_start_date=date(2024, 1, 1),
            final_regime_end_date=date(2026, 12, 31),
            transition_evidence="Synthetic legacy eligibility",
            qualifying_turnover="1000000000",
            legacy_eligibility_confirmed=True,
            no_normal_election_confirmed=True,
        )
        return work.approve_tax_profile(
            profile,
            self.actor,
            **data,
            review_note="Synthetic reviewed final eligibility",
            external_review=self.external(profile, work.profile_decision(**data)),
        )

    def final_obligation(self):
        self.final_profile()
        return self.approved(
            direction="own_tax", base="1000000", rule="final_turnover_005"
        )

    def recognition(self, row):
        return f.recognize_own_tax(
            organization=self.org,
            actor=self.actor,
            obligation=row,
            date=self.day,
            reference="OWN1",
            evidence="Synthetic reviewed recognition",
        )

    def payment(self, row, amount="2500", reference="PAY1", bank=None):
        return f.reconcile_own_tax_payment(
            organization=self.org,
            actor=self.actor,
            recognition=row,
            transaction=bank or self.movement(f"-{amount}", reference=reference),
            amount=amount,
            reference=reference,
            evidence="Synthetic tax transfer",
        )

    def annual(
        self,
        *,
        instalments="0",
        with_credit=False,
        book_credit=True,
        book_profit="1000000",
    ):
        self.profile()
        if with_credit:
            self.day = date(2025, 9, 10)
            invoice = f.issue_invoice(
                organization=self.org, actor=self.actor, invoice=self.invoice()
            )
            row = self.approved(invoice=invoice)
            self.credit(row)
            if book_credit:
                self.net_receipt(row)
        f.post_journal(
            organization=self.org,
            actor=self.actor,
            date=date(2025, 12, 10),
            source_key="synthetic-profit",
            description="Synthetic pretax profit",
            entries=[("BANK", book_profit, "0"), ("REVENUE", "0", book_profit)],
        )
        for month in range(1, 13):
            AccountingPeriod.objects.update_or_create(
                organization=self.org,
                year=2025,
                month=month,
                defaults={"closed": True, "closed_at": timezone.now()},
            )
        row = work.prepare_annual_workpaper(
            organization=self.org,
            actor=self.actor,
            year=2025,
            positive_adjustments="0",
            negative_adjustments="0",
            loss_compensation="0",
            turnover=book_profit,
            regime_code="corporate_22",
            instalments=instalments,
            support_evidence=self.evidence(),
            reconciliation_note="Synthetic reviewed reconciliation",
        )
        work.approve_annual_workpaper(
            organization=self.org,
            actor=self.actor,
            workpaper=row,
            external_review=self.external(row, {"action": "approve_annual"}),
        )
        self.day = date(2026, 1, 10)
        return row


class TaxMoneyTests(TaxMoneyFixtures, TestCase):
    def test_public_late_certificate_recovers_posted_receipt_without_new_cash(self):
        self.profile()
        invoice = f.issue_invoice(
            organization=self.org, actor=self.actor, invoice=self.invoice()
        )
        obligation = self.approved(invoice=invoice)
        bank = self.movement("490000")
        allocation = f.reconcile_receipt(
            organization=self.org,
            actor=self.actor,
            invoice=invoice,
            transaction=bank,
            amount="490000",
        )
        self.assertEqual(invoice.outstanding_amount, Decimal(10000))
        self.assertEqual(bank.unallocated_amount, 0)
        self.credit(obligation)
        original_cash_journal = list(
            Journal.objects.filter(
                organization=self.org, source_key=f"allocation:{allocation.pk}"
            ).values("pk", "date", "source_key")
        )
        self.assertEqual(len(original_cash_journal), 1)
        self.client.force_login(self.actor)
        response = self.client.post(
            reverse("customer_tax_credit", args=[obligation.pk]),
            {
                "source_allocation": allocation.pk,
                "date": "2026-10-01",
                "reference": "LATE-CERT",
                "evidence": "Synthetic certificate received after cash matching",
                "confirmed": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        row = CustomerTaxSettlement.objects.get(
            organization=self.org, obligation=obligation
        )
        self.assertEqual(
            (
                row.source_allocation_id,
                row.bank_posting_id,
                row.cash_amount,
                row.tax_amount,
                row.gross_amount,
            ),
            (allocation.pk, None, 0, Decimal(10000), Decimal(10000)),
        )
        self.assertEqual(invoice.outstanding_amount, 0)
        self.assertEqual(invoice.paid_amount, Decimal(500000))
        self.assertEqual(bank.unallocated_amount, 0)
        self.assertEqual(
            f.report_balances(organization=self.org)["BANK"], Decimal(490000)
        )
        self.assertEqual(
            list(
                Journal.objects.filter(
                    organization=self.org, source_key=f"allocation:{allocation.pk}"
                ).values("pk", "date", "source_key")
            ),
            original_cash_journal,
        )
        self.assertEqual(
            collectible_amounts(
                organization=self.org, invoice_ids=[invoice.pk], as_of=self.day
            )[invoice.pk],
            Decimal(10000),
        )
        self.assertEqual(
            collectible_amounts(
                organization=self.org, invoice_ids=[invoice.pk], as_of=date(2026, 10, 1)
            )[invoice.pk],
            0,
        )
        history = self.client.get(reverse("financial_movements"))
        self.assertContains(history, "Uang bank sebelumnya")
        self.assertContains(history, "Kredit pajak baru")
        self.assertContains(
            history, "Synthetic certificate received after cash matching"
        )

    def test_net_cash_and_verified_credit_settle_gross_invoice_without_phantom_cash(
        self,
    ):
        self.profile()
        invoice = f.issue_invoice(
            organization=self.org, actor=self.actor, invoice=self.invoice()
        )
        row = self.approved(invoice=invoice)
        bank = self.movement("490000")
        with self.assertRaisesMessage(ValidationError, "bukti potong resmi"):
            self.net_receipt(row, bank=bank)
        self.credit(row)
        receipt = self.net_receipt(row, bank=bank)
        self.assertEqual(invoice.paid_amount, Decimal(500000))
        self.assertEqual(invoice.outstanding_amount, 0)
        self.assertEqual(bank.unallocated_amount, 0)
        self.assertFalse(invoice.allocations.exists())
        balances = f.report_balances(organization=self.org)
        self.assertEqual(
            (balances["BANK"], balances["TAX_RECEIVABLE"], balances["AR"]),
            (Decimal(490000), Decimal(10000), 0),
        )
        self.assertEqual(
            applied_payment_amounts(
                organization=self.org, invoice_ids=[invoice.pk], as_of=self.day
            )[invoice.pk],
            Decimal(500000),
        )
        self.assertEqual(
            collectible_amounts(
                organization=self.org, invoice_ids=[invoice.pk], as_of=date(2026, 9, 9)
            )[invoice.pk],
            0,
        )
        self.assertEqual(self.net_receipt(row, bank=bank).pk, receipt.pk)
        with self.assertRaises(ValidationError):
            self.net_receipt(row, reference="OTHER", bank=bank)
        self.assertEqual(CustomerTaxSettlement.objects.count(), 1)

    def test_late_credit_requires_exact_posted_payment_open_date_and_unused_reference(
        self,
    ):
        self.profile()
        invoices = [
            f.issue_invoice(
                organization=self.org, actor=self.actor, invoice=self.invoice()
            )
            for _ in range(2)
        ]
        rows = [self.approved(invoice=invoice) for invoice in invoices]
        allocations = [
            f.reconcile_receipt(
                organization=self.org,
                actor=self.actor,
                invoice=invoice,
                transaction=self.movement("490000", reference=f"LATE-{n}"),
                amount="490000",
            )
            for n, invoice in enumerate(invoices)
        ]
        for row in rows:
            self.credit(row)
        data = dict(
            organization=self.org,
            actor=self.actor,
            obligation=rows[0],
            source_allocation=allocations[0],
            date=date(2026, 10, 1),
            reference="LATE-ONE",
            evidence="Synthetic late certificate matched",
        )
        for changes in (
            {"source_allocation": allocations[1]},
            {"date": date(2026, 9, 9)},
        ):
            with self.assertRaises(ValidationError):
                f.recognize_customer_tax_credit(**{**data, **changes})
        AccountingPeriod.objects.update_or_create(
            organization=self.org,
            year=2026,
            month=9,
            defaults={"closed": True, "closed_at": timezone.now()},
        )
        with self.assertRaises(ValidationError):
            f.recognize_customer_tax_credit(**{**data, "date": self.day})
        row = f.recognize_customer_tax_credit(**data)
        self.assertEqual(f.recognize_customer_tax_credit(**data).pk, row.pk)
        with self.assertRaisesMessage(ValidationError, "Referensi sudah dipakai"):
            f.recognize_customer_tax_credit(
                **{**data, "obligation": rows[1], "source_allocation": allocations[1]}
            )
        with self.assertRaises(ValidationError):
            self.net_receipt(rows[0], bank=allocations[0].transaction)
        self.assertEqual(CustomerTaxSettlement.objects.count(), 1)
        self.assertEqual(invoices[1].outstanding_amount, Decimal(10000))
        self.assertEqual(
            f.report_balances(organization=self.org)["BANK"], Decimal(980000)
        )
        self.assertEqual(
            set(row.journal.lines.values_list("account", flat=True)),
            {"TAX_RECEIVABLE", "AR"},
        )

    def test_credit_only_dual_proof_and_foreign_allocation_are_rejected(self):
        from core.models import AuditEvent

        self.profile()
        invoice = f.issue_invoice(
            organization=self.org, actor=self.actor, invoice=self.invoice()
        )
        row = self.approved(invoice=invoice)
        self.credit(row)
        allocation = f.reconcile_receipt(
            organization=self.org,
            actor=self.actor,
            invoice=invoice,
            transaction=self.movement("490000"),
            amount="490000",
        )
        marketing = get_user_model().objects.create_user(
            username="late-credit-marketing"
        )
        Membership.objects.create(
            organization=self.org, user=marketing, role="marketing"
        )
        kwargs = dict(
            organization=self.org,
            actor=self.actor,
            obligation=row,
            source_allocation=allocation,
            date=date(2026, 10, 1),
            reference="LATE",
            evidence="Synthetic late certificate",
        )
        with self.assertRaises(PermissionDenied):
            f.recognize_customer_tax_credit(**{**kwargs, "actor": marketing})
        other = Organization.objects.create(name="Synthetic foreign late credit")
        Membership.objects.create(organization=other, user=self.actor, role="owner")
        with self.assertRaises(ValidationError):
            f.recognize_customer_tax_credit(**{**kwargs, "organization": other})
        self.client.force_login(marketing)
        self.assertEqual(
            self.client.get(reverse("customer_tax_credit", args=[row.pk])).status_code,
            403,
        )
        event = f.recognize_customer_tax_credit(**kwargs)
        self.assertTrue(
            AuditEvent.objects.filter(
                organization=self.org,
                action="finance.customer_tax.late_credit_recognized",
            ).exists()
        )
        event.cash_amount = "1"
        with self.assertRaises(ValidationError):
            event.save()

    def test_net_receipt_needs_exact_bound_invoice_and_open_period(self):
        self.profile()
        invoice = f.issue_invoice(
            organization=self.org, actor=self.actor, invoice=self.invoice()
        )
        unbound = self.approved()
        self.credit(unbound)
        with self.assertRaises(ValidationError):
            self.net_receipt(unbound)
        bound = self.approved(invoice=invoice)
        self.credit(bound)
        with self.assertRaises(ValidationError):
            self.net_receipt(bound, amount="500000", reference="OVER")
        bank = self.movement("490000", reference="LOCKED")
        AccountingPeriod.objects.update_or_create(
            organization=self.org,
            year=2026,
            month=9,
            defaults={"closed": True, "closed_at": timezone.now()},
        )
        before = Journal.objects.count()
        with self.assertRaises(ValidationError):
            self.net_receipt(bound, reference="LOCKED", bank=bank)
        self.assertEqual(Journal.objects.count(), before)
        self.assertFalse(CustomerTaxSettlement.objects.exists())

    def test_partial_receipt_keeps_remaining_ar_and_guards_tax_refunds(self):
        self.profile()
        invoice = f.issue_invoice(
            organization=self.org, actor=self.actor, invoice=self.invoice()
        )
        row = self.approved(invoice=invoice, base="250000")
        self.credit(row)
        receipt = self.net_receipt(row, amount="245000")
        self.assertEqual(invoice.outstanding_amount, Decimal(250000))
        f.reconcile_receipt(
            organization=self.org,
            actor=self.actor,
            invoice=invoice,
            transaction=self.movement("250000", reference="REST"),
            amount="250000",
        )
        self.assertEqual(invoice.outstanding_amount, 0)
        with self.assertRaises(ValidationError):
            f.cancel_invoice(
                organization=self.org,
                actor=self.actor,
                invoice=invoice,
                date=self.day,
                reason="Synthetic cancellation",
            )
        from .fulfillment import credit_units

        with self.assertRaises(ValidationError):
            credit_units(
                organization=self.org,
                actor=self.actor,
                invoice=invoice,
                date=self.day,
                kind="unfulfilled",
                quantity=1,
                reference="CREDIT",
                evidence="Synthetic cancellation",
            )
        receipt.cash_amount = 1
        with self.assertRaises(ValidationError):
            receipt.save()
        with self.assertRaises(ValidationError):
            CustomerTaxSettlement.objects.filter(pk=receipt.pk).update(cash_amount=1)

    def test_final_tax_recognition_partial_payments_and_official_proof_are_distinct(
        self,
    ):
        obligation = self.final_obligation()
        verification = dict(
            organization=self.org,
            actor=self.actor,
            obligation=obligation,
            kind="payment",
            evidence=self.evidence(),
            reference="SYN-NTPN",
            taxpayer_reference="1234567890123456",
            period=obligation.period,
            amount=obligation.amount,
            note="Synthetic official proof",
            matches_confirmed=True,
        )
        with self.assertRaises(ValidationError):
            work.verify_obligation_evidence(**verification)
        recognized = self.recognition(obligation)
        self.assertEqual(self.recognition(obligation).pk, recognized.pk)
        bank = self.movement("-2500")
        first = self.payment(recognized, bank=bank)
        self.assertEqual(self.payment(recognized, bank=bank).pk, first.pk)
        self.assertEqual(recognized.remaining_amount, Decimal(2500))
        with self.assertRaises(ValidationError):
            work.verify_obligation_evidence(**verification)
        with self.assertRaises(ValidationError):
            self.payment(recognized, amount="2501", reference="OVER")
        self.payment(recognized, reference="PAY2")
        self.assertFalse(obligation.verifications.exists())
        self.assertEqual(f.report_balances(organization=self.org)["OWN_TAX_PAYABLE"], 0)
        self.assertEqual(
            f.report_balances(organization=self.org)["TAX_EXPENSE"], Decimal(5000)
        )
        self.assertEqual(
            f.monthly_summary(organization=self.org, year=2026, month=9)["profit"], 0
        )
        work.verify_obligation_evidence(**verification)
        self.assertIn("171(4)", obligation.filing_status)
        self.assertEqual(OwnTaxPayment.objects.count(), 2)

    def test_own_recognition_rejects_duplicate_and_closed_date_without_side_effects(
        self,
    ):
        obligation = self.final_obligation()
        row = self.recognition(obligation)
        with self.assertRaises(ValidationError):
            f.recognize_own_tax(
                organization=self.org,
                actor=self.actor,
                obligation=obligation,
                date=self.day,
                reference="DUP",
                evidence="Synthetic",
            )
        row.amount = 1
        with self.assertRaises(ValidationError):
            row.save()
        another = self.approved(
            direction="own_tax", base="2000000", rule="final_turnover_005"
        )
        AccountingPeriod.objects.update_or_create(
            organization=self.org,
            year=2026,
            month=9,
            defaults={"closed": True, "closed_at": timezone.now()},
        )
        with self.assertRaises(ValidationError):
            f.recognize_own_tax(
                organization=self.org,
                actor=self.actor,
                obligation=another,
                date=self.day,
                reference="CLOSED",
                evidence="Synthetic",
            )
        self.assertEqual(OwnTaxRecognition.objects.count(), 1)

    def test_corporate_recognition_uses_only_booked_single_use_customer_credits(self):
        workpaper = self.annual(with_credit=True)
        row = f.recognize_own_tax(
            organization=self.org,
            actor=self.actor,
            workpaper=workpaper,
            date=self.day,
            reference="ANNUAL",
            evidence="Synthetic approved annual amount",
            instruction_evidence=self.evidence(),
            professional_instruction="Synthetic professional instructs journal date 2026-01-10 in the open period",
            late_recognition_confirmed=True,
        )
        self.assertEqual(
            (row.expense_amount, row.credits_amount, row.amount),
            (Decimal(220000), Decimal(10000), Decimal(210000)),
        )
        self.assertEqual(f.report_balances(organization=self.org)["TAX_RECEIVABLE"], 0)
        self.assertEqual(row.credit_applications.count(), 1)
        self.assertTrue(
            work.annual_snapshot_status(self.org, workpaper)["snapshot_current"]
        )
        with self.assertRaises(ValidationError):
            f.recognize_own_tax(
                organization=self.org,
                actor=self.actor,
                workpaper=workpaper,
                date=self.day,
                reference="DUP",
                evidence="Synthetic",
            )
        self.assertFalse(hasattr(workpaper, "filing"))
        self.payment(row, amount="210000")
        self.assertEqual(row.remaining_amount, 0)

    def test_corporate_prepayment_and_overpayment_cases_remain_explicitly_unsupported(
        self,
    ):
        row = self.annual(instalments="1000")
        with self.assertRaisesMessage(ValidationError, "PPh 25"):
            f.recognize_own_tax(
                organization=self.org,
                actor=self.actor,
                workpaper=row,
                date=self.day,
                reference="ANNUAL",
                evidence="Synthetic",
                instruction_evidence=self.evidence(),
                professional_instruction="Synthetic professional instructs 2026-01-10",
                late_recognition_confirmed=True,
            )
        self.assertFalse(OwnTaxRecognition.objects.exists())

    def test_foreign_sources_and_marketing_writes_are_rejected(self):
        obligation = self.final_obligation()
        stranger = get_user_model().objects.create_user(username="tax-money-marketing")
        Membership.objects.create(
            organization=self.org, user=stranger, role="marketing"
        )
        with self.assertRaises(PermissionDenied):
            f.recognize_own_tax(
                organization=self.org,
                actor=stranger,
                obligation=obligation,
                date=self.day,
                reference="DENIED",
                evidence="Synthetic",
            )
        other = Organization.objects.create(name="Synthetic foreign tax")
        Membership.objects.create(organization=other, user=self.actor, role="owner")
        with self.assertRaises(ValidationError):
            f.recognize_own_tax(
                organization=other,
                actor=self.actor,
                obligation=obligation,
                date=self.day,
                reference="FOREIGN",
                evidence="Synthetic",
            )
        self.client.force_login(stranger)
        self.assertEqual(
            self.client.get(
                reverse("own_tax_recognize", args=[obligation.pk])
            ).status_code,
            403,
        )

    def test_ui_exposes_scoped_tax_sources_and_payment_steps(self):
        obligation = self.final_obligation()
        self.client.force_login(self.actor)
        self.assertContains(
            self.client.get(reverse("taxes:obligation", args=[obligation.pk])),
            "Catat utang pajak final",
        )
        self.assertEqual(
            self.client.get(
                reverse("own_tax_recognize", args=[obligation.pk])
            ).status_code,
            200,
        )
        row = self.recognition(obligation)
        self.assertContains(
            self.client.get(reverse("financial_movements")), "Cocokkan transfer pajak"
        )
        self.assertEqual(
            self.client.get(reverse("own_tax_payment", args=[row.pk])).status_code, 200
        )
        self.assertEqual(
            self.client.get(reverse("taxes:obligation_new")).status_code, 200
        )

    def test_public_http_recognition_partial_payment_and_official_evidence_remain_separate(
        self,
    ):
        obligation = self.final_obligation()
        self.client.force_login(self.actor)
        response = self.client.post(
            reverse("own_tax_recognize", args=[obligation.pk]),
            {
                "date": str(self.day),
                "reference": "HTTP-OWN",
                "evidence": "Synthetic approved tax recognition",
                "confirmed": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        row = OwnTaxRecognition.objects.get(
            organization=self.org, obligation=obligation
        )
        bank = self.movement("-2000", reference="HTTP-PAY")
        data = {
            "transaction": bank.pk,
            "amount": "2000",
            "reference": "HTTP-PAY",
            "evidence": "Synthetic matched transfer",
            "confirmed": "on",
        }
        self.assertEqual(
            self.client.post(
                reverse("own_tax_payment", args=[row.pk]), data
            ).status_code,
            302,
        )
        self.assertEqual(row.remaining_amount, Decimal(3000))
        self.assertFalse(obligation.verifications.exists())
        self.assertEqual(
            self.client.post(
                reverse("own_tax_payment", args=[row.pk]), data
            ).status_code,
            302,
        )
        self.assertEqual(OwnTaxPayment.objects.count(), 1)
        data.update(amount="3001", reference="HTTP-OVER")
        self.assertEqual(
            self.client.post(
                reverse("own_tax_payment", args=[row.pk]), data
            ).status_code,
            200,
        )
        self.assertEqual(row.remaining_amount, Decimal(3000))

    def test_commercial_paid_order_uses_gross_settlement_but_cash_uses_net_transfer(
        self,
    ):
        from operations import services as operations
        from operations.analytics import commercial_snapshot

        self.profile()
        order = operations.create_order(
            organization=self.org,
            actor=self.actor,
            reference="TAX-ORDER",
            buyer=self.party,
            date=self.day,
        )
        line = operations.add_order_line(
            organization=self.org,
            actor=self.actor,
            order=order,
            product=self.product,
            quantity=1,
        )
        invoice = operations.invoice_line(
            organization=self.org, actor=self.actor, line=line, date=self.day
        )
        before = commercial_snapshot(
            organization=self.org, start=self.day, end=self.day
        )
        row = self.approved(invoice=invoice)
        self.credit(row)
        self.net_receipt(row)
        after = commercial_snapshot(organization=self.org, start=self.day, end=self.day)
        self.assertEqual(after["cohort_fully_paid_orders"], 1)
        self.assertEqual(Decimal(after["period_gross_bank_receipts"]), Decimal(490000))
        self.assertNotEqual(before["fingerprint"], after["fingerprint"])

    def test_closed_annual_year_requires_documented_late_recognition_instruction(self):
        row = self.annual()
        with self.assertRaisesMessage(
            ValidationError, "instruksi tertulis profesional"
        ):
            f.recognize_own_tax(
                organization=self.org,
                actor=self.actor,
                workpaper=row,
                date=self.day,
                reference="ANNUAL",
                evidence="Synthetic",
            )
        self.assertFalse(OwnTaxRecognition.objects.exists())

    def test_new_tax_accounts_tie_to_subledger_and_pending_sources_block_close(self):
        row = self.final_obligation()
        self.assertTrue(
            any(
                "belum dicatat sebagai utang" in message
                for message in f._close_blockers(self.org, self.day)
            )
        )
        self.recognition(row)
        blockers = f._close_blockers(self.org, self.day)
        self.assertFalse(
            any(
                "utang pajak perusahaan tidak sama" in message
                or "belum dicatat sebagai utang" in message
                for message in blockers
            )
        )

    def test_annual_recognition_rejects_certificate_without_booked_tax_asset(self):
        row = self.annual(with_credit=True, book_credit=False)
        with self.assertRaisesMessage(ValidationError, "aset kredit pajak"):
            f.recognize_own_tax(
                organization=self.org,
                actor=self.actor,
                workpaper=row,
                date=self.day,
                reference="UNBOOKED",
                evidence="Synthetic",
                instruction_evidence=self.evidence(),
                professional_instruction="Synthetic professional instructs journal date 2026-01-10",
                late_recognition_confirmed=True,
            )
        self.assertFalse(OwnTaxRecognition.objects.exists())

    def test_annual_overpayment_is_rejected_without_posting_a_refund_asset(self):
        row = self.annual(with_credit=True, book_profit="10000")
        with self.assertRaisesMessage(ValidationError, "lebih bayar"):
            f.recognize_own_tax(
                organization=self.org,
                actor=self.actor,
                workpaper=row,
                date=self.day,
                reference="REFUND",
                evidence="Synthetic",
                instruction_evidence=self.evidence(),
                professional_instruction="Synthetic professional instructs journal date 2026-01-10",
                late_recognition_confirmed=True,
            )
        self.assertFalse(OwnTaxRecognition.objects.exists())
        self.assertEqual(
            f.report_balances(organization=self.org)["TAX_RECEIVABLE"], Decimal(10000)
        )

    def test_financial_document_titles_contain_only_the_document_title(self):
        self.profile()
        invoice = f.issue_invoice(
            organization=self.org, actor=self.actor, invoice=self.invoice()
        )
        self.client.force_login(self.actor)
        response = self.client.get(reverse("sale_detail", args=[invoice.pk]))
        title = re.search(
            r"<title>(.*?)</title>", response.content.decode(), re.DOTALL
        ).group(1)
        self.assertNotIn("<", title)
        self.assertIn(invoice.number, title)
        workpaper = work.prepare_annual_workpaper(
            organization=self.org,
            actor=self.actor,
            year=2026,
            positive_adjustments="0",
            negative_adjustments="0",
            loss_compensation="0",
            turnover="0",
            regime_code="corporate_22",
            instalments="0",
            support_evidence=self.evidence(),
            reconciliation_note="Synthetic title test",
        )
        response = self.client.get(
            reverse("taxes:annual_workpaper", args=[workpaper.pk])
        )
        title = re.search(
            r"<title>(.*?)</title>", response.content.decode(), re.DOTALL
        ).group(1)
        self.assertNotIn("<", title)
        self.assertIn("Paket fiskal 2026", title)
