"""Public budget-spend journeys; all fixtures belong to a synthetic company."""

from datetime import date
from importlib import import_module

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.test import TestCase
from django.utils import timezone

from core.models import Membership, Organization
from finance.models import BankTransaction, Journal
from taxes import tests_workflow as tax_fixtures

from . import services as legacy
from .models import BudgetEntry


class BudgetSpendTests(TestCase):
    def test_partial_recognition_preserves_commitment_and_reported_payment_history(
        self,
    ):
        org = Organization.objects.create(
            name="Synthetic DIR spend partial recognition", is_demo=True
        )
        finance = get_user_model().objects.create_user(username="spend-partial-finance")
        owner = get_user_model().objects.create_user(username="spend-partial-owner")
        Membership.objects.create(organization=org, user=finance, role="finance")
        Membership.objects.create(organization=org, user=owner, role="owner")
        budget = legacy.create_budget(
            organization=org,
            actor=finance,
            title="September campaign",
            year=2026,
            month=9,
            channel="meta_ads",
            amount="80000000",
        )
        budget = legacy.transition_budget(
            organization=org,
            actor=finance,
            budget=budget,
            action="submit",
            expected_version=budget.version,
        )
        budget = legacy.transition_budget(
            organization=org,
            actor=owner,
            budget=budget,
            action="approve",
            expected_version=budget.version,
            notes="Otorisasi Rp80 juta; pembayaran dan kas diperiksa terpisah.",
        )
        commitment = legacy.add_budget_entry(
            organization=org,
            actor=finance,
            budget=budget,
            kind="open_commitment",
            amount="80000000",
            date=date(2026, 9, 8),
            description="Komitmen kampanye",
            reference="PARTIAL-COMMITMENT",
            idempotency_key="spend-partial-commitment",
        )
        payment = legacy.record_budget_payment(
            organization=org,
            actor=finance,
            entry=commitment,
            amount="30000000",
            date=date(2026, 9, 9),
            description="Pembayaran yang dilaporkan",
            reference="PARTIAL-REPORTED-PAYMENT",
            idempotency_key="spend-partial-payment",
        )
        original_rows = list(
            BudgetEntry.objects.filter(organization=org).order_by("pk").values()
        )
        self.assertFalse(Journal.objects.filter(organization=org).exists())
        self.assertFalse(BankTransaction.objects.filter(organization=org).exists())

        # Resolve the future public module inside the test: discovery still runs
        # normally before this capability exists, preserving a behavioral RED.
        try:
            public = import_module("director.spend_services")
        except ModuleNotFoundError as exc:
            if exc.name != "director.spend_services":
                raise
            self.fail(
                "The public Spend workflow cannot yet preserve partial recognition and payment history."
            )

        as_of = timezone.localdate()
        before = public.budget_position(organization=org, budget=budget, as_of=as_of)
        spend = public.adopt_budget_entry(
            organization=org,
            actor=finance,
            entry=commitment,
            date=date(2026, 9, 10),
            reference="PARTIAL-ADOPTION",
            reason="Pertahankan komitmen dan pembayaran asal dalam satu belanja.",
            evidence="Catatan komitmen dan pembayaran dilaporkan diperiksa; belum dicocokkan bank.",
            expected_version=budget.version,
            expected_fingerprint=before["fingerprint"],
        )
        source = public.spend_position(organization=org, spend=spend, as_of=as_of)
        public.convert_spend(
            organization=org,
            actor=finance,
            spend=spend,
            from_kind="open_commitment",
            to_kind="incurred",
            amount="20000000",
            date=date(2026, 9, 20),
            reference="PARTIAL-RECOGNITION",
            reason="Sebagian pekerjaan kampanye telah dilaksanakan.",
            evidence="Bukti pekerjaan senilai Rp20 juta telah diperiksa; sisa komitmen Rp60 juta.",
            expected_version=spend.version,
            expected_fingerprint=source["fingerprint"],
        )
        result = public.budget_position(organization=org, budget=budget, as_of=as_of)

        self.assertTrue(result["current"])
        self.assertEqual(result["incurred"], "20000000.00")
        self.assertEqual(result["open_commitments"], "60000000.00")
        self.assertEqual(result["reservations"], "0.00")
        self.assertEqual(result["consumed"], "80000000.00")
        self.assertEqual(result["available"], "0.00")
        self.assertEqual(result["reported_payments"], "30000000.00")
        self.assertEqual(result["unmatched_reported_payments"], "30000000.00")
        self.assertEqual(result["payments"], "30000000.00")
        self.assertEqual(result["verified_cash"], "0.00")
        self.assertEqual(
            list(BudgetEntry.objects.filter(organization=org).order_by("pk").values()),
            original_rows,
        )
        payment.refresh_from_db()
        self.assertEqual(payment.settles_id, commitment.pk)
        self.assertFalse(Journal.objects.filter(organization=org).exists())
        self.assertFalse(BankTransaction.objects.filter(organization=org).exists())


class SpendFixtures:
    """Reusable synthetic setup without inheriting another suite's test cases."""

    def setUp(self):
        self.org = Organization.objects.create(
            name="Synthetic spend workflow", is_demo=True
        )
        self.other = Organization.objects.create(
            name="Synthetic foreign spend", is_demo=True
        )
        for role in ("owner", "finance", "marketing"):
            user = get_user_model().objects.create_user(
                username=f"spend-workflow-{role}"
            )
            Membership.objects.create(organization=self.org, user=user, role=role)
            setattr(self, role, user)
        self.counter = 0

    def ref(self):
        self.counter += 1
        return f"SPEND-REQUEST-{self.counter}"

    def make_spend(
        self,
        kind="open_commitment",
        *,
        amount="80000000",
        budget_amount="80000000",
        adoption=date(2026, 9, 10),
    ):
        from . import spend_services as s

        budget = legacy.create_budget(
            organization=self.org,
            actor=self.finance,
            title="September campaign",
            year=2026,
            month=9,
            channel="meta_ads",
            amount=budget_amount,
        )
        budget = legacy.transition_budget(
            organization=self.org,
            actor=self.finance,
            budget=budget,
            action="submit",
            expected_version=budget.version,
        )
        budget = legacy.transition_budget(
            organization=self.org,
            actor=self.owner,
            budget=budget,
            action="approve",
            expected_version=budget.version,
            notes="Reviewed authorization; not cash execution.",
        )
        entry = legacy.add_budget_entry(
            organization=self.org,
            actor=self.finance,
            budget=budget,
            kind=kind,
            amount=amount,
            date=date(2026, 9, 8),
            description="Synthetic campaign commitment",
            idempotency_key=self.ref(),
        )
        source = s.budget_position(
            organization=self.org, budget=budget, as_of=timezone.localdate()
        )
        spend = s.adopt_budget_entry(
            organization=self.org,
            actor=self.finance,
            entry=entry,
            date=adoption,
            reference=self.ref(),
            reason="Preserve original observation and trace partial work.",
            evidence="Synthetic original commitment reviewed.",
            expected_version=budget.version,
            expected_fingerprint=source["fingerprint"],
        )
        return spend

    def controls(
        self, spend, *, actor=None, date_value=date(2026, 9, 20), reference=None
    ):
        from . import spend_services as s

        spend.refresh_from_db()
        row = s.spend_position(
            organization=self.org, spend=spend, as_of=timezone.localdate()
        )
        return {
            "organization": self.org,
            "actor": actor or self.finance,
            "date": date_value,
            "reference": reference or self.ref(),
            "reason": "Synthetic source reviewed.",
            "evidence": "Synthetic supporting work evidence preserved.",
            "expected_version": spend.version,
            "expected_fingerprint": row["fingerprint"],
        }

    def position(self, spend, as_of=None):
        from . import spend_services as s

        return s.budget_position(
            organization=self.org,
            budget=spend.budget_id,
            as_of=as_of or timezone.localdate(),
        )

    def bill(self, *, amount="60000000", category="prepayment", approved=True):
        from finance import services as f
        from finance.models import Party

        supplier = Party.objects.create(
            organization=self.org,
            name=f"Synthetic supplier {self.ref()}",
            kind="supplier",
        )
        bill = f.create_bill(
            organization=self.org,
            actor=self.finance,
            supplier=supplier,
            number=self.ref(),
            amount=amount,
            supplier_vat="0",
            date=date(2026, 9, 12),
            service_date=date(2026, 9, 20),
            category=category,
        )
        return (
            f.approve_bill(organization=self.org, actor=self.finance, bill=bill)
            if approved
            else bill
        )

    def link(self, spend, bill, *, amount="60000000", **kwargs):
        from finance.budget_adapter import bill_budget_facts

        from . import spend_services as s

        controls = self.controls(spend, **kwargs)
        facts = bill_budget_facts(
            organization=self.org, bill=bill, as_of=timezone.localdate()
        )
        return s.link_spend_bill(
            spend=spend,
            bill=bill,
            coverage=[
                {"source_key": f"entry:{spend.origin_entry_id}", "amount": amount}
            ]
            if amount
            else [],
            expected_bill_fingerprint=facts["fingerprint"],
            **controls,
        )


class SpendWorkflowTests(SpendFixtures, TestCase):
    def test_exact_retry_preserves_one_conversion_and_changed_payload_is_rejected(self):
        from . import spend_services as s
        from .models import BudgetSpendMovement

        spend = self.make_spend()
        args = self.controls(spend)
        first = s.convert_spend(
            spend=spend,
            from_kind="open_commitment",
            to_kind="incurred",
            amount="20000000",
            **args,
        )
        self.assertEqual(
            s.convert_spend(
                spend=spend,
                from_kind="open_commitment",
                to_kind="incurred",
                amount="20000000",
                **args,
            ).pk,
            first.pk,
        )
        with self.assertRaises(ValidationError):
            s.convert_spend(
                spend=spend,
                from_kind="open_commitment",
                to_kind="incurred",
                amount="21000000",
                **args,
            )
        self.assertEqual(BudgetSpendMovement.objects.filter(kind="convert").count(), 1)
        self.assertEqual(self.position(spend)["consumed"], "80000000.00")

    def test_stale_version_and_source_fingerprint_cannot_change_residual(self):
        from . import spend_services as s

        spend = self.make_spend()
        args = self.controls(spend)
        for override in (
            {"expected_version": args["expected_version"] - 1},
            {"expected_fingerprint": "0" * 64},
        ):
            with self.assertRaises(ValidationError):
                s.convert_spend(
                    spend=spend,
                    from_kind="open_commitment",
                    to_kind="incurred",
                    amount="20000000",
                    **(args | override),
                )
        self.assertEqual(self.position(spend)["open_commitments"], "80000000.00")

    def test_marketing_inactive_actor_and_foreign_company_cannot_mutate_spend(self):
        from . import spend_services as s

        spend = self.make_spend()
        for override in ({"actor": self.marketing}, {"organization": self.other}):
            with self.assertRaises((PermissionDenied, ValidationError)):
                s.convert_spend(
                    spend=spend,
                    from_kind="open_commitment",
                    to_kind="incurred",
                    amount="20000000",
                    **(self.controls(spend) | override),
                )
        self.finance.is_active = False
        self.finance.save()
        with self.assertRaises(PermissionDenied):
            s.convert_spend(
                spend=spend,
                from_kind="open_commitment",
                to_kind="incurred",
                amount="20000000",
                **self.controls(spend),
            )
        self.assertEqual(self.position(spend)["incurred"], "0.00")

    def test_adopted_origin_and_its_reported_payment_reject_legacy_mutation(self):
        from . import spend_services as s

        spend = self.make_spend()
        # Reported payment after adoption belongs to the Spend, not a new legacy row.
        with self.assertRaises(ValidationError):
            legacy.record_budget_payment(
                organization=self.org,
                actor=self.finance,
                entry=spend.origin_entry,
                amount="1",
                date=date(2026, 9, 20),
                description="Wrong old path",
                reference=self.ref(),
                idempotency_key=self.ref(),
            )
        for call in (legacy.void_budget_entry, legacy.replace_budget_entry):
            args = {
                "organization": self.org,
                "actor": self.owner,
                "entry": spend.origin_entry,
                "reason": "Wrong old path",
            }
            if call == legacy.replace_budget_entry:
                args.update(
                    kind="incurred",
                    amount="1",
                    date=date(2026, 9, 20),
                    description="Wrong old path",
                    idempotency_key=self.ref(),
                    confirm_reclassification=True,
                )
            with self.assertRaises(ValidationError):
                call(**args)
        self.assertTrue(
            s.spend_position(
                organization=self.org, spend=spend, as_of=timezone.localdate()
            )["current"]
        )

    def test_owner_corrects_wrong_partial_amount_without_rewriting_old_month(self):
        from . import spend_services as s

        spend = self.make_spend()
        movement = s.convert_spend(
            spend=spend,
            from_kind="open_commitment",
            to_kind="incurred",
            amount="20000000",
            **self.controls(spend),
        )
        with self.assertRaises(PermissionDenied):
            s.correct_spend_movement(
                movement=movement,
                replacement=None,
                **self.controls(spend, date_value=date(2026, 10, 1)),
            )
        correction = s.correct_spend_movement(
            movement=movement,
            replacement={
                "from_kind": "open_commitment",
                "to_kind": "incurred",
                "amount": "15000000",
            },
            **self.controls(spend, actor=self.owner, date_value=date(2026, 10, 1)),
        )
        self.assertEqual(correction.reverse_of_id, movement.pk)
        movement.refresh_from_db()
        self.assertEqual(str(movement.amount), "20000000.00")
        self.assertEqual(
            self.position(spend, date(2026, 9, 30))["incurred"], "20000000.00"
        )
        result = self.position(spend)
        self.assertEqual(
            (result["incurred"], result["open_commitments"], result["consumed"]),
            ("15000000.00", "65000000.00", "80000000.00"),
        )
        self.assertFalse(Journal.objects.exists())

    def test_release_cannot_erase_reported_payment_and_is_owner_controlled(self):
        from . import spend_services as s

        spend = self.make_spend()
        s.record_spend_payment(spend=spend, amount="30000000", **self.controls(spend))
        with self.assertRaises(PermissionDenied):
            s.release_spend_residual(
                spend=spend,
                bucket="open_commitment",
                amount="10000000",
                **self.controls(spend),
            )
        with self.assertRaises(ValidationError):
            s.release_spend_residual(
                spend=spend,
                bucket="open_commitment",
                amount="60000000",
                **self.controls(spend, actor=self.owner),
            )
        s.release_spend_residual(
            spend=spend,
            bucket="open_commitment",
            amount="50000000",
            **self.controls(spend, actor=self.owner),
        )
        self.assertEqual(
            (self.position(spend)["consumed"], self.position(spend)["payments"]),
            ("30000000.00", "30000000.00"),
        )

    def test_adoption_today_and_later_movements_keep_original_earlier_cutoff(self):
        from . import spend_services as s

        spend = self.make_spend(adoption=date(2026, 10, 1))
        s.convert_spend(
            spend=spend,
            from_kind="open_commitment",
            to_kind="incurred",
            amount="20000000",
            **self.controls(spend, date_value=date(2026, 10, 5)),
        )
        s.record_spend_payment(
            spend=spend,
            amount="30000000",
            **self.controls(spend, date_value=date(2026, 10, 6)),
        )
        previous = self.position(spend, date(2026, 9, 30))
        self.assertEqual(
            (
                previous["incurred"],
                previous["open_commitments"],
                previous["consumed"],
                previous["payments"],
            ),
            ("0.00", "80000000.00", "80000000.00", "0.00"),
        )
        self.assertEqual(self.position(spend)["incurred"], "20000000.00")

    def test_damaged_immutable_movement_produces_unknown_totals_and_blocks_mutation(
        self,
    ):
        from . import spend_services as s

        spend = self.make_spend()
        movement = s.convert_spend(
            spend=spend,
            from_kind="open_commitment",
            to_kind="incurred",
            amount="20000000",
            **self.controls(spend),
        )
        with self.assertRaises(ValidationError):
            movement.amount = "1"
            movement.save()
        table = connection.ops.quote_name(movement._meta.db_table)
        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {table} SET amount = %s WHERE id = %s", ["1", movement.pk]
            )
        result = self.position(spend)
        self.assertFalse(result["current"])
        self.assertIsNone(result["consumed"])
        self.assertIsNone(result["verified_cash"])
        self.assertTrue(result["issues"])
        with self.assertRaises(ValidationError):
            s.record_spend_payment(spend=spend, amount="1", **self.controls(spend))

    def test_prepaid_bill_and_release_replace_only_explicit_manual_coverage(self):
        from finance import services as f

        spend = self.make_spend("reservation")
        bill = self.bill()
        self.link(spend, bill)
        f.release_prepayment(
            organization=self.org,
            actor=self.finance,
            bill=bill,
            amount="20000000",
            date=date(2026, 10, 1),
            reference=self.ref(),
            evidence="Synthetic performed supplier service.",
        )
        result = self.position(spend)
        self.assertEqual(
            (
                result["incurred"],
                result["open_commitments"],
                result["reservations"],
                result["consumed"],
            ),
            ("20000000.00", "40000000.00", "20000000.00", "80000000.00"),
        )
        self.assertEqual(
            (result["verified_cash"], result["unpaid_bills"]), ("0.00", "60000000.00")
        )

    def test_actual_bill_above_estimate_is_visible_without_invented_authorization(self):
        spend = self.make_spend()
        bill = self.bill(amount="100000000", category="expense")
        self.link(spend, bill, amount="80000000")
        result = self.position(spend)
        self.assertTrue(result["current"])
        self.assertEqual(
            (
                result["amount"],
                result["consumed"],
                result["available"],
                result["overrun"],
            ),
            ("80000000.00", "100000000.00", "-20000000.00", "20000000.00"),
        )
        self.assertTrue(
            any(task["code"] == "budget_overrun" for task in result["tasks"])
        )

    def test_bill_link_correction_keeps_history_and_never_changes_finance_journals(
        self,
    ):
        from finance.budget_adapter import bill_budget_facts

        from . import spend_services as s

        spend = self.make_spend("reservation")
        wrong = self.bill()
        link = self.link(spend, wrong)
        correct = self.bill(amount="50000000")
        original_journals = list(Journal.objects.order_by("pk").values())
        result = s.correct_spend_bill_link(
            link=link,
            bill=correct,
            coverage=[
                {"source_key": f"entry:{spend.origin_entry_id}", "amount": "50000000"}
            ],
            expected_bill_fingerprint=bill_budget_facts(
                organization=self.org, bill=correct, as_of=timezone.localdate()
            )["fingerprint"],
            **self.controls(spend, actor=self.owner, date_value=date(2026, 10, 1)),
        )
        link.refresh_from_db()
        self.assertEqual(link.bill_id, wrong.pk)
        self.assertEqual(link.voided_at, date(2026, 10, 1))
        self.assertEqual(result.bill_id, correct.pk)
        self.assertEqual(
            list(Journal.objects.order_by("pk").values()), original_journals
        )
        self.assertEqual(
            self.position(spend, date(2026, 9, 30))["open_commitments"], "60000000.00"
        )
        self.assertEqual(
            (
                self.position(spend)["open_commitments"],
                self.position(spend)["reservations"],
            ),
            ("50000000.00", "30000000.00"),
        )

    def test_selected_draft_bill_changes_since_preview_require_explicit_reconfirmation(
        self,
    ):
        from finance.budget_adapter import bill_budget_facts

        from . import spend_services as s
        from .models import BudgetBillLink

        spend = self.make_spend("reservation")
        bill = self.bill(approved=False)
        facts = bill_budget_facts(
            organization=self.org, bill=bill, as_of=timezone.localdate()
        )
        controls = self.controls(spend)
        bill.amount = "70000000"
        bill.save()
        with self.assertRaises(ValidationError):
            s.link_spend_bill(
                spend=spend,
                bill=bill,
                coverage=[],
                expected_bill_fingerprint=facts["fingerprint"],
                **controls,
            )
        self.assertFalse(BudgetBillLink.objects.exists())
        self.assertFalse(Journal.objects.exists())

    def test_unassigned_legacy_payment_remains_reported_with_an_explicit_task(self):
        spend = self.make_spend()
        legacy.add_budget_entry(
            organization=self.org,
            actor=self.finance,
            budget=spend.budget_id,
            kind="payment",
            amount="30000000",
            date=date(2026, 9, 20),
            description="Unknown original source",
            idempotency_key=self.ref(),
        )
        result = self.position(spend)
        self.assertEqual(
            (
                result["reported_payments"],
                result["unmatched_reported_payments"],
                result["payments"],
            ),
            ("30000000.00", "30000000.00", "30000000.00"),
        )
        self.assertTrue(
            any(task["code"] == "unassigned_payment" for task in result["tasks"])
        )


class SpendPaymentFixtures(SpendFixtures):
    evidence = tax_fixtures.TaxReleaseWorkflowTests.evidence

    def payment_case(self):
        from finance import services as f
        from finance.models import BankAccount, BankTransaction
        from taxes import workflow as tax

        from . import spend_services as s

        spend = self.make_spend("reservation")
        bill = self.bill()
        data = {
            "rule_code": "no_withholding",
            "base": "0",
            "period": date(2026, 9, 1),
            "reason": "Synthetic professional conclusion for this exact supplier Bill.",
            "object_code": "",
        }
        report = tax.record_external_review(
            organization=self.org,
            actor=self.owner,
            subject=bill,
            decision=tax.bill_decision(**data),
            evidence=self.evidence(),
            professional_name="Synthetic professional",
            qualification_reference="Synthetic engagement",
            reviewed_on=date(2026, 9, 20),
            statement="Synthetic dated source conclusion confirms the displayed decision.",
            report_confirmed=True,
        )
        tax.review_bill_tax(
            organization=self.org,
            actor=self.owner,
            bill=bill,
            external_review=report,
            **data,
        )
        self.link(spend, bill)
        reported = s.record_spend_payment(
            spend=spend,
            amount="30000000",
            **self.controls(spend, date_value=date(2026, 9, 21)),
        )
        account = BankAccount.objects.create(
            organization=self.org,
            name="Synthetic spend bank",
            account_number="SYN-SPEND",
        )
        f.import_bank_csv(
            organization=self.org,
            actor=self.finance,
            account=account,
            content="date,reference,description,amount\n2026-10-01,SPEND-PAID,Synthetic supplier payment,-30000000\n",
        )
        bank = BankTransaction.objects.get(
            organization=self.org, reference="SPEND-PAID"
        )
        payment = f.reconcile_bill_payment(
            organization=self.org,
            actor=self.finance,
            bill=bill,
            transaction=bank,
            amount="30000000",
        )
        return spend, bill, reported, payment


class SpendPaymentTests(SpendPaymentFixtures, TestCase):
    def test_partial_matching_counts_reported_and_verified_payment_once_with_amount_bounds(
        self,
    ):
        from . import spend_services as s
        from .models import BudgetPaymentMatch

        spend, bill, reported, payment = self.payment_case()
        args = self.controls(spend, date_value=date(2026, 10, 1))
        first = s.match_spend_payment(
            spend=spend,
            manual_source_key=f"movement:{reported.pk}",
            finance_event_id=f"bill_payment:{payment.pk}",
            amount="20000000",
            **args,
        )
        self.assertEqual(
            s.match_spend_payment(
                spend=spend,
                manual_source_key=f"movement:{reported.pk}",
                finance_event_id=f"bill_payment:{payment.pk}",
                amount="20000000",
                **args,
            ).pk,
            first.pk,
        )
        result = self.position(spend)
        self.assertEqual(
            (
                result["payments"],
                result["verified_cash"],
                result["reported_payments"],
                result["unmatched_reported_payments"],
            ),
            ("40000000.00", "30000000.00", "30000000.00", "10000000.00"),
        )
        with self.assertRaises(ValidationError):
            s.match_spend_payment(
                spend=spend,
                manual_source_key=f"movement:{reported.pk}",
                finance_event_id=f"bill_payment:{payment.pk}",
                amount="11000000",
                **self.controls(spend, date_value=date(2026, 10, 1)),
            )
        s.match_spend_payment(
            spend=spend,
            manual_source_key=f"movement:{reported.pk}",
            finance_event_id=f"bill_payment:{payment.pk}",
            amount="10000000",
            **self.controls(spend, date_value=date(2026, 10, 1)),
        )
        self.assertEqual(BudgetPaymentMatch.objects.count(), 2)
        self.assertEqual(self.position(spend)["payments"], "30000000.00")
        self.assertEqual(self.position(spend)["consumed"], "80000000.00")

    def test_owner_corrects_match_and_voids_it_without_rewriting_bank_or_manual_history(
        self,
    ):
        from finance.models import BillPayment

        from . import spend_services as s

        spend, bill, reported, payment = self.payment_case()
        matched = s.match_spend_payment(
            spend=spend,
            manual_source_key=f"movement:{reported.pk}",
            finance_event_id=f"bill_payment:{payment.pk}",
            amount="20000000",
            **self.controls(spend, date_value=date(2026, 10, 1)),
        )
        original = list(Journal.objects.order_by("pk").values())
        replacement = s.correct_spend_payment_match(
            match=matched,
            manual_source_key=f"movement:{reported.pk}",
            finance_event_id=f"bill_payment:{payment.pk}",
            amount="15000000",
            **self.controls(spend, actor=self.owner, date_value=date(2026, 10, 5)),
        )
        matched.refresh_from_db()
        self.assertEqual(
            (str(matched.amount), matched.voided_at), ("20000000.00", date(2026, 10, 5))
        )
        self.assertEqual(
            self.position(spend, date(2026, 10, 2))["payments"], "40000000.00"
        )
        self.assertEqual(self.position(spend)["payments"], "45000000.00")
        s.void_spend_payment_match(
            match=replacement,
            **self.controls(spend, actor=self.owner, date_value=date(2026, 10, 6)),
        )
        self.assertEqual(self.position(spend)["payments"], "60000000.00")
        self.assertEqual(self.position(spend)["verified_cash"], "30000000.00")
        self.assertEqual(list(Journal.objects.order_by("pk").values()), original)
        self.assertEqual(
            (BillPayment.objects.count(), str(reported.amount)), (1, "30000000.00")
        )
