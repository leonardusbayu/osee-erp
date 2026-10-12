"""PRIVATE ORIGINAL DRAFT / UNEXECUTED: human IDR bank-match input.

Root owns archival, promotion and first native RED/GREEN. Only the existing
public HTTP bank-match seam is specified; canonical service Decimal semantics
remain unchanged. No browser serialization or live-company behavior is claimed.
"""

from copy import deepcopy
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django import test as django_test
from django.urls import reverse

from core.models import AuditEvent, DomainEvent, Membership, Organization
from finance import services, statement_services
from finance.models import (
    Allocation,
    BankAccount,
    BankTransaction,
    BillPayment,
    Invoice,
    Journal,
    JournalLine,
    Party,
    Product,
)
from finance.statement_models import StatementPeriodReview
from finance.test_statement_fixtures import reviewed_statement
from webapp.models import StatementImport


DAY = date(2026, 1, 5)
START = date(2026, 1, 1)
END = date(2026, 1, 31)


@django_test.override_settings(
    MFA_REQUIRED=False,
    OPENROUTER_API_KEY="",
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class BankMatchMoneyInputOriginalDraft(django_test.TestCase):
    def setUp(self):
        self.finance = get_user_model().objects.create_user(
            username="synthetic-human-idr-finance"
        )

    def fixture(self, label):
        org = Organization.objects.create(name=f"Synthetic IDR input {label}")
        Membership.objects.create(organization=org, user=self.finance, role="finance")
        account = BankAccount.objects.create(
            organization=org,
            name="Synthetic independently controlled bank",
            account_number=f"MONEY-{label}",
        )
        customer = Party.objects.create(
            organization=org, name="Synthetic customer", kind="customer"
        )
        product = Product.objects.create(
            organization=org,
            code="MONEY-ITP",
            name="Synthetic ITP",
            kind="itp",
            default_price=Decimal(50000),
        )
        draft = services.create_invoice(
            organization=org,
            actor=self.finance,
            party=customer,
            product=product,
            quantity=1,
            date=DAY,
            number=f"INV-MONEY-{label}",
        )
        invoice = services.issue_invoice(
            organization=org, actor=self.finance, invoice=draft
        )
        self.assertEqual(invoice.total, Decimal(50000))
        self.assertEqual(invoice.status, "issued")
        reference = f"MONEY-RECEIPT-{label}"
        content = (
            "date,reference,description,amount\n"
            f"2026-01-05,{reference},Synthetic actual transfer,50000\n"
        ).encode()
        imported = services.import_bank_csv(
            organization=org, actor=self.finance, account=account, content=content
        )
        self.assertEqual(imported, {"created": 1, "duplicates": 0})
        bank = BankTransaction.objects.get(
            organization=org, account=account, reference=reference
        )
        self.assertEqual(bank.amount, Decimal(50000))

        review = reviewed_statement(
            self,
            organization=org,
            actor=self.finance,
            account=account,
            start=START,
            end=END,
            opening="0",
            closing="50000",
            total_in="50000",
            total_out="0",
            count=1,
        )
        source = StatementImport.objects.get(organization=org, pk=review.statement_id)
        case = {
            "org": org,
            "account": account,
            "invoice": invoice,
            "bank": bank,
            "review": review,
            "source": source,
        }
        coverage = self.coverage(case)
        self.assertTrue(coverage["verified"], coverage)
        self.assertEqual(coverage["accounts"][0]["review_ids"], [review.pk])
        self.assertEqual(coverage["accounts"][0]["closing_balance"], "50000.00")
        self.assertEqual(
            coverage["accounts"][0]["fingerprints"][0]["current"], review.fingerprint
        )
        self.assertEqual(review.transaction_count, 1)
        self.assertEqual(
            (review.total_in, review.total_out, review.closing_balance),
            (Decimal(50000), Decimal(0), Decimal(50000)),
        )
        self.assertEqual(BankTransaction.objects.filter(organization=org).count(), 1)
        self.assertFalse(Allocation.objects.filter(organization=org).exists())
        self.assertEqual(
            self.balances(case),
            (Decimal(0), Decimal(50000), Decimal(-50000), Decimal(0), Decimal(0)),
        )
        self.assertEqual(invoice.paid_amount, Decimal(0))
        self.assertEqual(invoice.outstanding_amount, Decimal(50000))
        self.assertEqual(bank.unallocated_amount, Decimal(50000))
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
        self.assertIn(invoice.pk, [row.pk for row in page.context["open_invoices"]])
        self.assertContains(page, f'id="amount-{bank.pk}"')
        self.assertTrue(
            next(
                row for row in page.context["transactions"] if row.pk == bank.pk
            ).can_match
        )
        case["client"] = client
        case["csrf"] = client.cookies["csrftoken"].value
        case["url"] = reverse("bank_match", args=[bank.pk])
        self.assert_sources_current(case)
        return case

    def coverage(self, case):
        return statement_services.statement_coverage(
            organization=case["org"], account=case["account"], start=START, end=END
        )

    def balances(self, case):
        book = services.report_balances(organization=case["org"])
        return tuple(
            book[key]
            for key in ("BANK", "AR", "DEFERRED_REVENUE", "REVENUE", "EXPENSE")
        )

    def sources(self, case):
        with case["source"].file.open("rb") as handle:
            content = handle.read()
        return {
            "bank": list(
                BankTransaction.objects.filter(organization=case["org"])
                .order_by("pk")
                .values()
            ),
            "account": list(
                BankAccount.objects.filter(organization=case["org"])
                .order_by("pk")
                .values()
            ),
            "statement": list(
                StatementImport.objects.filter(organization=case["org"])
                .order_by("pk")
                .values()
            ),
            "review": list(
                StatementPeriodReview.objects.filter(organization=case["org"])
                .order_by("pk")
                .values()
            ),
            "file_bytes": content,
        }

    def money_state(self, case):
        models = (
            Invoice,
            Allocation,
            BillPayment,
            Journal,
            JournalLine,
            AuditEvent,
            DomainEvent,
        )
        return {
            "rows": {
                model._meta.label_lower: deepcopy(
                    list(
                        model.objects.filter(organization=case["org"])
                        .order_by("pk")
                        .values()
                    )
                )
                for model in models
            },
            "balances": self.balances(case),
            "paid": case["invoice"].paid_amount,
            "outstanding": case["invoice"].outstanding_amount,
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
                "invoice": str(case["invoice"].pk),
                "amount": raw,
                "csrfmiddlewaretoken": case["csrf"],
            },
        )

    def assert_settled(self, case):
        allocation = Allocation.objects.get(
            organization=case["org"], transaction=case["bank"], invoice=case["invoice"]
        )
        self.assertEqual(
            allocation.amount,
            Decimal(50000),
            "Staff IDR input must allocate fifty thousand, not fifty",
        )
        self.assertEqual(Allocation.objects.filter(organization=case["org"]).count(), 1)
        self.assertEqual(
            BankTransaction.objects.filter(organization=case["org"]).count(), 1
        )
        self.assertEqual(Journal.objects.filter(organization=case["org"]).count(), 2)
        self.assertEqual(
            self.balances(case),
            (Decimal(50000), Decimal(0), Decimal(-50000), Decimal(0), Decimal(0)),
        )
        self.assertEqual(case["invoice"].paid_amount, Decimal(50000))
        self.assertEqual(case["invoice"].outstanding_amount, Decimal(0))
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
                organization=case["org"], action="finance.receipt.allocated"
            ).count(),
            1,
        )
        self.assertEqual(
            DomainEvent.objects.filter(
                organization=case["org"], topic="payment.allocated"
            ).count(),
            1,
        )
        self.assert_sources_current(case)
        before = self.money_state(case)
        replay = services.reconcile_receipt(
            organization=case["org"],
            actor=self.finance,
            transaction=case["bank"],
            invoice=case["invoice"],
            amount=Decimal("50000.00"),
        )
        self.assertEqual(replay.pk, allocation.pk)
        self.assertEqual(self.money_state(case), before)
        return allocation

    def canonical_control(self, label, raw):
        case = self.fixture(label)
        before = self.money_state(case)
        denied = case["client"].post(
            case["url"], {"invoice": str(case["invoice"].pk), "amount": raw}
        )
        self.assertEqual(
            denied.status_code,
            403,
            "A real missing CSRF token cannot bypass the HTTP guard",
        )
        self.assertEqual(self.money_state(case), before)
        response = self.submit(case, raw)
        self.assertRedirects(response, reverse("bank"), fetch_redirect_response=False)
        self.assert_settled(case)

    def test_canonical_control_then_indonesian_grouped_input_settles_exact_fifty_thousand(
        self,
    ):
        # These real successful controls establish fixture/authorized HTTP
        # behavior before a localized amount can be the genuine RED cause.
        self.canonical_control("canonical-integer", "50000")
        self.canonical_control("canonical-two-decimals", "50000.00")
        for label, raw in (
            ("indonesian-grouped", "50.000"),
            ("indonesian-grouped-cents", "50.000,00"),
        ):
            with self.subTest(raw=raw):
                case = self.fixture(label)
                response = self.submit(case, raw)
                self.assertRedirects(
                    response, reverse("bank"), fetch_redirect_response=False
                )
                self.assert_settled(case)

    def test_malformed_or_ambiguous_input_is_correctable_and_cannot_write_money(self):
        self.canonical_control("bad-format-control", "50000")
        case = self.fixture("bad-format")
        for raw in ("1.2.3", "50,000"):
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
                self.assertRegex(
                    " ".join(failed.match_errors), r"(?i)(format|rupiah|valid)"
                )
                for message in failed.match_errors:
                    self.assertContains(response, message)
                self.assertFalse(
                    Allocation.objects.filter(organization=case["org"]).exists()
                )
                self.assertEqual(
                    self.balances(case),
                    (
                        Decimal(0),
                        Decimal(50000),
                        Decimal(-50000),
                        Decimal(0),
                        Decimal(0),
                    ),
                )
                self.assert_sources_current(case)
