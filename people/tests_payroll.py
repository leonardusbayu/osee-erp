"""Synthetic HTTP journeys through the finance payroll screens."""

from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from core.models import Membership, Organization
from finance.models import BankAccount, BankTransaction
from people import payroll
from people.models import PayrollPayment, PayrollPerson, PayrollRun


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

    def test_http_module_is_available_to_finance_but_hidden_from_marketing(self):
        client = Client()
        client.force_login(self.users["finance"])
        session = client.session
        session["organization_id"] = self.org.pk
        session.save()
        response = client.get(reverse("people:index"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "SDM &amp; Penggajian")
        self.assertContains(
            response, "Aplikasi belum menghitung atau menebak nominal tersebut."
        )
        finance_modules = client.get(reverse("modules"))
        self.assertContains(finance_modules, "SDM &amp; Penggajian")
        self.assertContains(finance_modules, 'href="/people/"')
        client.force_login(self.users["marketing"])
        marketing_modules = client.get(reverse("modules"))
        self.assertNotContains(marketing_modules, 'href="/people/"')
        response = client.get(reverse("people:index"))
        self.assertEqual(response.status_code, 403)

    def test_payroll_form_warns_against_unnecessary_sensitive_personal_data(self):
        client = Client()
        self.login_as(client, self.users["finance"])
        response = client.get(reverse("people:run_detail", args=[self.run.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response, "Jangan masukkan NIK, alamat, atau nomor rekening pribadi"
        )

    def test_draft_row_edit_and_remove_actions_appear_once(self):
        line = self.add_line()
        client = Client()
        self.login_as(client, self.users["finance"])

        response = client.get(reverse("people:run_detail", args=[self.run.pk]))

        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        edit_url = reverse("people:line_edit", args=[self.run.pk, line.pk])
        remove_url = reverse("people:line_remove", args=[self.run.pk, line.pk])
        self.assertEqual(html.count(f'href="{edit_url}"'), 1)
        self.assertEqual(html.count(f'action="{remove_url}"'), 1)

    def login_as(self, client, user):
        client.force_login(user)
        session = client.session
        session["organization_id"] = self.org.pk
        session.save()

    def line_input(self, person, reference="SYNTHETIC-HR-APPROVAL"):
        return {
            "person": str(person.pk),
            "gross_amount": "5000000.00",
            "pph21_amount": "250000.00",
            "bpjs_employee_amount": "100000.00",
            "bpjs_employer_amount": "200000.00",
            "basis_reference": reference,
            "note": "Synthetic test only",
        }

    def prepare_submitted_payroll(self):
        client = Client()
        self.login_as(client, self.users["finance"])
        response = client.post(
            reverse("people:person_create"),
            {
                "name": "Synthetic Employee",
                "kind": PayrollPerson.Kind.EMPLOYEE,
            },
        )
        self.assertEqual(response.status_code, 302)
        person = PayrollPerson.objects.get(
            organization=self.org, name="Synthetic Employee"
        )
        response = client.post(
            reverse("people:run_create"), {"year": "2026", "month": "8"}
        )
        self.assertEqual(response.status_code, 302)
        run = PayrollRun.objects.get(organization=self.org, year=2026, month=8)
        detail = reverse("people:run_detail", args=[run.pk])
        detail_response = client.get(detail)
        self.assertEqual(detail_response.status_code, 200)
        self.assertContains(detail_response, "Belum diajukan")
        response = client.post(
            reverse("people:line_add", args=[run.pk]), self.line_input(person)
        )
        self.assertEqual(response.status_code, 302)
        line = run.lines.get()
        edit_url = reverse("people:line_edit", args=[run.pk, line.pk])
        self.assertEqual(client.get(edit_url).status_code, 200)
        response = client.post(
            edit_url, self.line_input(person, "SYNTHETIC-HR-RECHECKED")
        )
        self.assertEqual(response.status_code, 302)
        run.refresh_from_db()
        response = client.post(
            reverse("people:run_action", args=[run.pk, "submit"]),
            {"version": run.version},
        )
        self.assertEqual(response.status_code, 302)
        run.refresh_from_db()
        return client, run, detail

    def test_finance_can_prepare_edit_and_submit_payroll(self):
        client, run, detail = self.prepare_submitted_payroll()
        self.assertEqual(run.status, PayrollRun.Status.SUBMITTED)
        self.assertContains(client.get(detail), "Menunggu Owner/Direktur")
        self.assertTrue(run.lines.exists())

    def test_owner_approves_and_finance_matches_the_bank_payment(self):
        client, run, detail = self.prepare_submitted_payroll()
        self.login_as(client, self.users["owner"])
        self.assertContains(client.get(detail), "Setujui payroll")
        response = client.post(
            reverse("people:run_action", args=[run.pk, "approve"]),
            {
                "version": run.version,
                "approval_note": "Synthetic owner approval",
            },
        )
        self.assertEqual(response.status_code, 302)
        run.refresh_from_db()
        self.assertEqual(run.status, PayrollRun.Status.POSTED)
        account = BankAccount.objects.create(
            organization=self.org, name="Synthetic BNI", account_number="SYN-HTTP"
        )
        tx = BankTransaction.objects.create(
            organization=self.org,
            account=account,
            date=date(2026, 8, 31),
            reference="SYN-HTTP-PAYROLL",
            amount=Decimal("-4650000.00"),
        )
        self.login_as(client, self.users["finance"])
        response = client.post(
            reverse("people:payment_match", args=[run.pk]),
            {
                "kind": PayrollPayment.Kind.NET_PAY,
                "transaction": str(tx.pk),
                "amount": "4650000.00",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            run.payments.filter(
                transaction=tx, kind=PayrollPayment.Kind.NET_PAY
            ).exists()
        )
