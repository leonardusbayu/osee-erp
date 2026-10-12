"""Synthetic payroll service and ledger invariants."""

from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase

from core.models import AuditEvent, Membership, Organization
from finance.models import BankAccount, BankPosting, BankTransaction
from people import payroll
from people.models import PayrollPerson, PayrollRun


class PayrollWorkflowTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(
            name="Synthetic Payroll Co", is_demo=True
        )
        self.other = Organization.objects.create(
            name="Other Synthetic Co", is_demo=True
        )
        self.users = {}
        for role in ("owner", "director", "finance", "marketing"):
            user = get_user_model().objects.create_user(username=f"payroll-{role}")
            Membership.objects.create(organization=self.org, user=user, role=role)
            self.users[role] = user
        self.other_finance = get_user_model().objects.create_user(
            username="payroll-other-finance"
        )
        Membership.objects.create(
            organization=self.other, user=self.other_finance, role="finance"
        )
        self.person = payroll.add_person(
            organization=self.org,
            actor=self.users["finance"],
            name="Synthetic Tutor",
            kind=PayrollPerson.Kind.CONTRACTOR,
        )
        self.run = payroll.create_run(
            organization=self.org,
            actor=self.users["finance"],
            year=2026,
            month=9,
        )

    def add_line(self, **changes):
        values = {
            "gross_amount": "5000000.00",
            "pph21_amount": "250000.00",
            "bpjs_employee_amount": "100000.00",
            "bpjs_employer_amount": "200000.00",
            "basis_reference": "SYNTHETIC-APPROVED-REGISTER",
        }
        values.update(changes)
        result = payroll.add_line(
            organization=self.org,
            actor=self.users["finance"],
            run=self.run,
            person=self.person,
            **values,
        )
        self.run.refresh_from_db()
        return result

    def submit_and_approve(self):
        self.add_line()
        submitted = payroll.submit_run(
            organization=self.org,
            actor=self.users["finance"],
            run=self.run,
            expected_version=self.run.version,
        )
        return payroll.approve_run(
            organization=self.org,
            actor=self.users["owner"],
            run=submitted,
            expected_version=submitted.version,
            approval_note="Synthetic review completed",
        )

    def test_approval_posts_balanced_ledger_using_only_entered_amounts(self):
        run = self.submit_and_approve()
        self.assertEqual(run.status, PayrollRun.Status.POSTED)
        self.assertEqual(run.journal.status, "posted")
        lines = list(run.journal.lines.order_by("account", "credit"))
        self.assertEqual(
            sum((line.debit for line in lines), Decimal(0)), Decimal("5200000.00")
        )
        self.assertEqual(
            sum((line.credit for line in lines), Decimal(0)), Decimal("5200000.00")
        )
        self.assertEqual(
            sum(
                (line.credit for line in lines if line.account == "TAX_PAYABLE"),
                Decimal(0),
            ),
            Decimal("250000.00"),
        )
        self.assertEqual(
            sum((line.credit for line in lines if line.account == "AP"), Decimal(0)),
            Decimal("4950000.00"),
        )
        self.assertEqual(run.lines.get().pph21_amount, Decimal("250000.00"))
        self.assertTrue(
            AuditEvent.objects.filter(
                organization=self.org, action="people.payroll.approved"
            ).exists()
        )

    def test_only_owner_or_legacy_director_can_approve_and_marketing_cannot_read(self):
        self.add_line()
        submitted = payroll.submit_run(
            organization=self.org,
            actor=self.users["finance"],
            run=self.run,
            expected_version=self.run.version,
        )
        with self.assertRaises(PermissionDenied):
            payroll.approve_run(
                organization=self.org,
                actor=self.users["finance"],
                run=submitted,
                expected_version=submitted.version,
                approval_note="No self approval",
            )
        approved = payroll.approve_run(
            organization=self.org,
            actor=self.users["director"],
            run=submitted,
            expected_version=submitted.version,
            approval_note="Synthetic review",
        )
        self.assertEqual(approved.status, PayrollRun.Status.POSTED)
        with self.assertRaises(PermissionDenied):
            payroll.payroll_summary(
                organization=self.org, actor=self.users["marketing"]
            )

    def test_cross_organization_records_cannot_be_used(self):
        with self.assertRaises(ValidationError):
            payroll.payroll_summary(organization=self.org, actor=self.other_finance)
        with self.assertRaises(ValidationError):
            payroll.add_line(
                organization=self.org,
                actor=self.users["finance"],
                run=self.run,
                person=payroll.add_person(
                    organization=self.other,
                    actor=self.other_finance,
                    name="Foreign Synthetic",
                    kind=PayrollPerson.Kind.EMPLOYEE,
                ),
                gross_amount="10",
                pph21_amount="0",
                bpjs_employee_amount="0",
                bpjs_employer_amount="0",
                basis_reference="",
            )

    def test_rejects_invalid_or_unsubstantiated_manual_amounts(self):
        with self.assertRaises(ValidationError):
            self.add_line(pph21_amount="-1")
        with self.assertRaises(ValidationError):
            self.add_line(
                pph21_amount="1",
                bpjs_employee_amount="0",
                bpjs_employer_amount="0",
                basis_reference="",
            )
        with self.assertRaises(ValidationError):
            self.add_line(
                gross_amount="100", pph21_amount="80", bpjs_employee_amount="30"
            )

    def test_submitted_payroll_is_immutable_and_approval_is_versioned(self):
        self.add_line()
        submitted = payroll.submit_run(
            organization=self.org,
            actor=self.users["finance"],
            run=self.run,
            expected_version=self.run.version,
        )
        with self.assertRaises(ValidationError):
            payroll.add_line(
                organization=self.org,
                actor=self.users["finance"],
                run=submitted,
                person=self.person,
                gross_amount="1",
                pph21_amount="0",
                bpjs_employee_amount="0",
                bpjs_employer_amount="0",
                basis_reference="",
            )
        with self.assertRaises(ValidationError):
            payroll.approve_run(
                organization=self.org,
                actor=self.users["owner"],
                run=submitted,
                expected_version=submitted.version - 1,
                approval_note="Stale approval",
            )

    def test_draft_line_can_be_corrected_or_removed_with_audit_history(self):
        line = self.add_line()
        corrected = payroll.update_line(
            organization=self.org,
            actor=self.users["finance"],
            run=self.run,
            line=line,
            person=self.person,
            gross_amount="5100000",
            pph21_amount="200000",
            bpjs_employee_amount="100000",
            bpjs_employer_amount="200000",
            basis_reference="SYNTHETIC-UPDATED-APPROVAL",
            note="Corrected before submission",
        )
        self.assertEqual(corrected.gross_amount, Decimal("5100000.00"))
        self.run.refresh_from_db()
        payroll.remove_line(
            organization=self.org,
            actor=self.users["finance"],
            run=self.run,
            line=corrected,
            reason="Synthetic duplicate entry",
        )
        self.assertFalse(self.run.lines.exists())
        self.assertTrue(
            AuditEvent.objects.filter(
                organization=self.org, action="people.payroll.line_corrected"
            ).exists()
        )
        self.assertTrue(
            AuditEvent.objects.filter(
                organization=self.org, action="people.payroll.line_removed"
            ).exists()
        )

    def test_owner_can_reject_with_reason_and_finance_can_create_a_revision(self):
        self.add_line()
        submitted = payroll.submit_run(
            organization=self.org,
            actor=self.users["finance"],
            run=self.run,
            expected_version=self.run.version,
        )
        rejected = payroll.reject_run(
            organization=self.org,
            actor=self.users["owner"],
            run=submitted,
            expected_version=submitted.version,
            rejection_note="Synthetic review: revise amount",
        )
        self.assertEqual(rejected.status, PayrollRun.Status.REJECTED)
        revised = payroll.create_run(
            organization=self.org, actor=self.users["finance"], year=2026, month=9
        )
        self.assertEqual(revised.revision, 2)

    def test_bank_match_posts_one_audited_liability_settlement(self):
        run = self.submit_and_approve()
        account = BankAccount.objects.create(
            organization=self.org, name="Synthetic BNI", account_number="TEST-ONLY"
        )
        tx = BankTransaction.objects.create(
            organization=self.org,
            account=account,
            date=date(2026, 9, 30),
            reference="SYN-PAYROLL-01",
            amount=Decimal("-4650000.00"),
        )
        payment = payroll.match_payment(
            organization=self.org,
            actor=self.users["finance"],
            run=run,
            transaction=tx,
            kind="net_pay",
            amount="4650000.00",
        )
        repeated = payroll.match_payment(
            organization=self.org,
            actor=self.users["finance"],
            run=run,
            transaction=tx,
            kind="net_pay",
            amount="4650000.00",
        )
        self.assertEqual(payment.pk, repeated.pk)
        self.assertEqual(BankPosting.objects.filter(transaction=tx).count(), 1)
        self.assertEqual(tx.unallocated_amount, Decimal("0.00"))
        self.assertEqual(payment.bank_posting.journal.status, "posted")

    def test_audit_timeline_shows_bank_payments_with_string_object_ids(self):
        run = self.submit_and_approve()
        account = BankAccount.objects.create(
            organization=self.org,
            name="Synthetic payroll bank",
            account_number="SYN-TIMELINE",
        )
        tx = BankTransaction.objects.create(
            organization=self.org,
            account=account,
            date=date(2026, 9, 30),
            reference="SYNTHETIC-PAYROLL-TIMELINE",
            amount=Decimal("-4650000.00"),
        )
        payroll.match_payment(
            organization=self.org,
            actor=self.users["finance"],
            run=run,
            transaction=tx,
            kind="net_pay",
            amount="4650000.00",
        )

        events = payroll.audit_timeline(
            organization=self.org, actor=self.users["finance"], run=run
        )

        payment_events = [
            event
            for event in events
            if event["label"] == "Pembayaran dicocokkan ke bank"
        ]
        self.assertEqual(len(payment_events), 1)
        self.assertIn("mutasi #", payment_events[0]["summary"])

    def test_posted_run_cannot_be_edited_or_deleted(self):
        run = self.submit_and_approve()
        with self.assertRaises(ValidationError):
            payroll.add_line(
                organization=self.org,
                actor=self.users["finance"],
                run=run,
                person=self.person,
                gross_amount="1",
                pph21_amount="0",
                bpjs_employee_amount="0",
                bpjs_employer_amount="0",
                basis_reference="",
            )
        with self.assertRaises(ValidationError):
            run.delete()

    def test_unpaid_posted_run_can_only_be_corrected_by_an_approver_and_creates_a_new_revision(
        self,
    ):
        run = self.submit_and_approve()
        with self.assertRaises(PermissionDenied):
            payroll.reverse_run(
                organization=self.org,
                actor=self.users["finance"],
                run=run,
                expected_version=run.version,
                reversal_date=date(2026, 10, 9),
                reason="Synthetic correction",
            )
        reversed_run = payroll.reverse_run(
            organization=self.org,
            actor=self.users["owner"],
            run=run,
            expected_version=run.version,
            reversal_date=date(2026, 10, 9),
            reason="Synthetic corrected amount",
        )
        self.assertEqual(reversed_run.status, PayrollRun.Status.REVERSED)
        self.assertEqual(reversed_run.reversal_journal.status, "posted")
        new_run = payroll.create_run(
            organization=self.org, actor=self.users["finance"], year=2026, month=9
        )
        self.assertEqual(new_run.revision, 2)
