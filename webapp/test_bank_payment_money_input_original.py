"""PRIVATE ORIGINAL DRAFT / UNEXECUTED: human IDR supplier payment input.

Root owns archival, promotion and native RED/GREEN. Exercise the existing
bank_match HTTP seam and public received professional authority. Canonical
service Decimal semantics, machine statement input and company data stay separate.
"""

from copy import deepcopy
from datetime import date
from decimal import Decimal
import io

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django import test as django_test
from django.urls import reverse
from pypdf import PdfWriter

from core.models import AuditEvent, DomainEvent, Membership, Organization
from finance import services, statement_services
from finance.models import (
    Allocation,
    BankAccount,
    BankOpening,
    BankTransaction,
    Bill,
    BillPayment,
    Invoice,
    Journal,
    JournalLine,
    Party,
)
from finance.statement_models import StatementPeriodReview
from finance.test_statement_fixtures import reviewed_statement
from taxes import workflow as tax_workflow
from taxes.models import (
    BillTaxDecision,
    ExternalTaxReview,
    TaxEvidence,
    TaxObligation,
    TaxObligationWithdrawal,
    TaxVerification,
)
from webapp.models import StatementImport


DAY = date(2026, 1, 5)
START = date(2026, 1, 1)
END = date(2026, 1, 31)


@django_test.override_settings(
    MFA_REQUIRED=False,
    OPENROUTER_API_KEY="",
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class BankPaymentMoneyInputOriginalDraft(django_test.TestCase):
    def setUp(self):
        self.finance = get_user_model().objects.create_user(
            username="synthetic-human-idr-payment-finance"
        )
        self.owner = get_user_model().objects.create_user(
            username="synthetic-human-idr-payment-owner"
        )

    def fixture(self, label):
        org = Organization.objects.create(name=f"Synthetic IDR payment {label}")
        Membership.objects.create(organization=org, user=self.finance, role="finance")
        Membership.objects.create(organization=org, user=self.owner, role="owner")
        account = BankAccount.objects.create(
            organization=org,
            name="Synthetic independently controlled payment bank",
            account_number=f"PAY-MONEY-{label}",
        )
        opening = services.record_bank_opening(
            organization=org,
            actor=self.owner,
            account=account,
            date=START,
            amount=Decimal(50000),
            reference=f"SYN-PAY-OPEN-{label}",
            evidence="Synthetic independent January opening bank control: Rp50000 before outgoing cash",
        )
        self.assertEqual(opening.amount, Decimal(50000))
        supplier = Party.objects.create(
            organization=org, name="Synthetic payment supplier", kind="supplier"
        )
        bill = services.create_bill(
            organization=org,
            actor=self.finance,
            supplier=supplier,
            amount=Decimal(50000),
            supplier_vat=Decimal(0),
            date=DAY,
            service_date=DAY,
            category="expense",
            number=f"BILL-PAY-MONEY-{label}",
            supplier_reference=f"SYN-SUPPLIER-PAY-{label}",
        )
        bill = services.approve_bill(organization=org, actor=self.finance, bill=bill)
        self.assertEqual((bill.amount, bill.status), (Decimal(50000), "approved"))

        evidence, external, bill_tax = self.record_professional_bill_review(
            organization=org, bill=bill, label=label
        )

        reference = f"PAY-MONEY-TRANSFER-{label}"
        content = (
            "date,reference,description,amount\n"
            f"2026-01-05,{reference},Synthetic actual outgoing payment,-50000\n"
        ).encode()
        imported = services.import_bank_csv(
            organization=org, actor=self.finance, account=account, content=content
        )
        self.assertEqual(imported, {"created": 1, "duplicates": 0})
        bank = BankTransaction.objects.get(
            organization=org, account=account, reference=reference
        )
        self.assertEqual(bank.amount, Decimal(-50000))
        review = reviewed_statement(
            self,
            organization=org,
            actor=self.finance,
            account=account,
            start=START,
            end=END,
            opening="50000",
            closing="0",
            total_in="0",
            total_out="50000",
            count=1,
        )
        source = StatementImport.objects.get(organization=org, pk=review.statement_id)
        case = {
            "org": org,
            "account": account,
            "opening": opening,
            "bill": bill,
            "bank": bank,
            "review": review,
            "source": source,
            "evidence": evidence,
            "external": external,
            "bill_tax": bill_tax,
        }
        coverage = self.coverage(case)
        self.assertTrue(coverage["verified"], coverage)
        self.assertEqual(coverage["accounts"][0]["review_ids"], [review.pk])
        self.assertEqual(coverage["accounts"][0]["closing_balance"], "0.00")
        self.assertEqual(
            coverage["accounts"][0]["fingerprints"][0]["current"], review.fingerprint
        )
        self.assertEqual(
            (
                review.opening_balance,
                review.total_in,
                review.total_out,
                review.closing_balance,
                review.transaction_count,
            ),
            (Decimal(50000), Decimal(0), Decimal(50000), Decimal(0), 1),
        )
        self.assertEqual(BankTransaction.objects.filter(organization=org).count(), 1)
        self.assertFalse(BillPayment.objects.filter(organization=org).exists())
        self.assertEqual(
            self.balances(case),
            (
                Decimal(50000),
                Decimal(-50000),
                Decimal(-50000),
                Decimal(50000),
                Decimal(0),
            ),
        )
        self.assertEqual(bill.paid_amount, Decimal(0))
        self.assertEqual(bill.outstanding_amount, Decimal(50000))
        self.assertEqual(bank.unallocated_amount, Decimal(-50000))
        case["original_sources"] = self.sources(case)
        case["original_coverage"] = deepcopy(coverage)

        client = django_test.Client(enforce_csrf_checks=True)
        client.force_login(self.finance)
        session = client.session
        session["organization_id"] = org.pk
        session.save()
        page = client.get(reverse("bank"))
        self.assertEqual(page.status_code, 200)
        self.assertFalse(page.context["bank_source_held"])
        self.assertIn(bill.pk, [row.pk for row in page.context["open_bills"]])
        self.assertContains(page, f'id="bill-amount-{bank.pk}"')
        self.assertTrue(
            next(
                row for row in page.context["transactions"] if row.pk == bank.pk
            ).can_match_bill
        )
        case["client"] = client
        case["csrf"] = client.cookies["csrftoken"].value
        case["url"] = reverse("bank_match", args=[bank.pk])
        self.assert_sources_current(case)
        return case

    def record_professional_bill_review(self, *, organization, bill, label):
        reason = f"Synthetic received professional report for {bill.supplier_reference}: no withholding on this exact bill"
        decision = tax_workflow.bill_decision(
            rule_code="no_withholding", base=Decimal(50000), period=START, reason=reason
        )
        document = io.BytesIO()
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.add_metadata(
            {
                "/Title": f"Synthetic received report {bill.number}",
                "/Subject": f"{reason}; bill gross Rp50000; January2026; withholding Rp0",
            }
        )
        writer.write(document)
        evidence = tax_workflow.upload_evidence(
            organization=organization,
            actor=self.finance,
            file=SimpleUploadedFile(
                f"synthetic-payment-report-{label}.pdf",
                document.getvalue(),
                content_type="application/pdf",
            ),
        )
        external = tax_workflow.record_external_review(
            organization=organization,
            actor=self.owner,
            subject=bill,
            decision=decision,
            evidence=evidence,
            professional_name="Synthetic independent payment professional",
            qualification_reference=f"Synthetic payment engagement {label}",
            reviewed_on=DAY,
            statement=reason,
            report_confirmed=True,
        )
        bill_tax = tax_workflow.review_bill_tax(
            organization=organization,
            actor=self.owner,
            bill=bill,
            rule_code="no_withholding",
            base=Decimal(50000),
            period=START,
            reason=reason,
            external_review=external,
        )
        bill.refresh_from_db()
        self.assertEqual((bill.tax_status, bill.tax_amount), ("reviewed", Decimal(0)))
        self.assertEqual(
            (bill_tax.rule_code, bill_tax.amount, bill_tax.external_review_id),
            ("no_withholding", Decimal(0), external.pk),
        )
        self.assertEqual(external.decision, decision)
        self.assertEqual(external.subject_type, "bill")
        self.assertEqual(external.subject_id, bill.pk)
        self.assertEqual(external.evidence_sha256, evidence.sha256)
        self.assertFalse(
            TaxObligation.objects.filter(organization=organization).exists()
        )
        return evidence, external, bill_tax

    def rows(self, model, case):
        rows = list(
            model.objects.filter(organization=case["org"]).order_by("pk").values()
        )
        for row in rows:
            for key, value in row.items():
                if isinstance(value, (memoryview, bytearray)):
                    row[key] = bytes(value)
        return deepcopy(rows)

    def coverage(self, case):
        return statement_services.statement_coverage(
            organization=case["org"], account=case["account"], start=START, end=END
        )

    def balances(self, case):
        book = services.report_balances(organization=case["org"])
        return tuple(
            book[key] for key in ("BANK", "AP", "EQUITY", "EXPENSE", "REVENUE")
        )

    def sources(self, case):
        with case["source"].file.open("rb") as handle:
            content = handle.read()
        models = (
            BankAccount,
            BankTransaction,
            BankOpening,
            Bill,
            StatementImport,
            StatementPeriodReview,
            TaxEvidence,
            ExternalTaxReview,
            BillTaxDecision,
            TaxObligation,
            TaxObligationWithdrawal,
            TaxVerification,
        )
        return {
            "rows": {
                model._meta.label_lower: self.rows(model, case) for model in models
            },
            "statement_file_bytes": content,
        }

    def money_state(self, case):
        models = (
            Invoice,
            Allocation,
            Bill,
            BillPayment,
            Journal,
            JournalLine,
            AuditEvent,
            DomainEvent,
            TaxEvidence,
            ExternalTaxReview,
            BillTaxDecision,
            TaxObligation,
            TaxObligationWithdrawal,
            TaxVerification,
        )
        return {
            "rows": {
                model._meta.label_lower: self.rows(model, case) for model in models
            },
            "balances": self.balances(case),
            "paid": case["bill"].paid_amount,
            "outstanding": case["bill"].outstanding_amount,
            "unallocated": case["bank"].unallocated_amount,
            "pending": list(
                services.pending_bank_transactions(organization=case["org"])
                .order_by("pk")
                .values_list("pk", flat=True)
            ),
            "sources": self.sources(case),
            "coverage": self.coverage(case),
        }

    def assert_sources_current(self, case):
        self.assertEqual(self.sources(case), case["original_sources"])
        self.assertEqual(self.coverage(case), case["original_coverage"])
        self.assertTrue(self.coverage(case)["verified"])

    def submit(self, case, raw):
        self.assert_sources_current(case)
        return case["client"].post(
            case["url"],
            {
                "bill": str(case["bill"].pk),
                "amount": raw,
                "csrfmiddlewaretoken": case["csrf"],
            },
        )

    def assert_settled(self, case):
        payment = BillPayment.objects.get(
            organization=case["org"], transaction=case["bank"], bill=case["bill"]
        )
        self.assertEqual(
            payment.amount,
            Decimal(50000),
            "Staff IDR input must pay fifty thousand, not fifty",
        )
        self.assertEqual(
            BillPayment.objects.filter(organization=case["org"]).count(), 1
        )
        self.assertFalse(Allocation.objects.filter(organization=case["org"]).exists())
        self.assertEqual(
            BankTransaction.objects.filter(organization=case["org"]).count(), 1
        )
        self.assertEqual(Journal.objects.filter(organization=case["org"]).count(), 3)
        self.assertEqual(
            self.balances(case),
            (Decimal(0), Decimal(0), Decimal(-50000), Decimal(50000), Decimal(0)),
        )
        self.assertEqual(case["bill"].paid_amount, Decimal(50000))
        self.assertEqual(case["bill"].outstanding_amount, Decimal(0))
        self.assertEqual(case["bank"].unallocated_amount, Decimal(0))
        self.assertEqual(
            list(
                services.pending_bank_transactions(
                    organization=case["org"]
                ).values_list("pk", flat=True)
            ),
            [],
        )
        self.assertEqual(
            AuditEvent.objects.filter(
                organization=case["org"], action="finance.bill.payment_reconciled"
            ).count(),
            1,
        )
        self.assertEqual(
            DomainEvent.objects.filter(
                organization=case["org"], topic="bill.payment_allocated"
            ).count(),
            1,
        )
        self.assert_sources_current(case)
        before = self.money_state(case)
        replay = services.reconcile_bill_payment(
            organization=case["org"],
            actor=self.finance,
            transaction=case["bank"],
            bill=case["bill"],
            amount=Decimal("50000.00"),
        )
        self.assertEqual(replay.pk, payment.pk)
        self.assertEqual(self.money_state(case), before)
        return payment

    def canonical_control(self, label, raw):
        case = self.fixture(label)
        before = self.money_state(case)
        denied = case["client"].post(
            case["url"], {"bill": str(case["bill"].pk), "amount": raw}
        )
        self.assertEqual(
            denied.status_code,
            403,
            "A real missing CSRF token cannot bypass the outgoing HTTP guard",
        )
        self.assertEqual(self.money_state(case), before)
        response = self.submit(case, raw)
        self.assertRedirects(response, reverse("bank"), fetch_redirect_response=False)
        self.assert_settled(case)

    def test_canonical_controls_then_indonesian_grouped_payment_settles_exact_fifty_thousand(
        self,
    ):
        self.canonical_control("out-canonical-integer", "50000")
        self.canonical_control("out-canonical-two-decimals", "50000.00")
        for label, raw in (
            ("out-indonesian-grouped", "50.000"),
            ("out-indonesian-grouped-cents", "50.000,00"),
        ):
            with self.subTest(raw=raw):
                case = self.fixture(label)
                response = self.submit(case, raw)
                self.assertRedirects(
                    response, reverse("bank"), fetch_redirect_response=False
                )
                self.assert_settled(case)

    def test_invalid_or_over_capacity_payment_keeps_sources_money_and_received_authority(
        self,
    ):
        self.canonical_control("out-bad-format-control", "50000")
        case = self.fixture("out-bad-format")
        for raw, message_pattern in (
            ("1.2.3", r"(?i)(format|rupiah|valid)"),
            ("50,000", r"(?i)(format|rupiah|valid)"),
            ("50001", r"(?i)(melebihi|sisa|maksimal)"),
        ):
            with self.subTest(raw=raw):
                before = self.money_state(case)
                response = self.submit(case, raw)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(self.money_state(case), before)
                self.assertContains(response, f'value="{raw}"')
                self.assertRegex(
                    response.content.decode(),
                    rf'<details[^>]*id="match-{case["bank"].pk}"[^>]*open',
                )
                failed = next(
                    row
                    for row in response.context["transactions"]
                    if row.pk == case["bank"].pk
                )
                self.assertTrue(failed.match_errors)
                self.assertTrue(
                    all(
                        isinstance(message, str) and message.strip()
                        for message in failed.match_errors
                    )
                )
                self.assertRegex(" ".join(failed.match_errors), message_pattern)
                for message in failed.match_errors:
                    self.assertContains(response, message)
                self.assertFalse(
                    BillPayment.objects.filter(organization=case["org"]).exists()
                )
                self.assertEqual(
                    self.balances(case),
                    (
                        Decimal(50000),
                        Decimal(-50000),
                        Decimal(-50000),
                        Decimal(50000),
                        Decimal(0),
                    ),
                )
                self.assert_sources_current(case)
