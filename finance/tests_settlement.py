"""Public collectible/payment facts shared by Finance and operational consumers."""

from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase

from core.models import Membership, Organization
from finance import services as f
from finance.models import BankAccount, BankTransaction, Party, Product


class SettlementFactsTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Synthetic settlement company")
        self.owner = get_user_model().objects.create_user("settlement-owner")
        Membership.objects.create(organization=self.org, user=self.owner, role="owner")
        self.party = Party.objects.create(
            organization=self.org, name="Synthetic buyer", kind="reseller"
        )
        self.product = Product.objects.create(
            organization=self.org,
            code="ITP",
            name="ITP",
            kind="itp",
            default_price=Decimal(500000),
        )
        self.bank = BankAccount.objects.create(
            organization=self.org, name="Synthetic bank", account_number="SYNTHETIC"
        )
        self.day = date(2026, 1, 10)

    def invoice(self, *, organization=None, party=None, product=None, issued=True):
        invoice = f.create_invoice(
            organization=organization or self.org,
            party=party or self.party,
            product=product or self.product,
            quantity=1,
            date=self.day,
            service_date=self.day,
        )
        return (
            f.issue_invoice(organization=organization or self.org, invoice=invoice)
            if issued
            else invoice
        )

    def movement(self, reference, amount, day=None):
        f.import_bank_csv(
            organization=self.org,
            account=self.bank,
            content=f"date,reference,description,amount\n{day or self.day},{reference},Synthetic,{amount}\n",
        )
        return BankTransaction.objects.get(organization=self.org, reference=reference)

    def test_advance_and_cancellation_preserve_historical_collectible_and_payment_facts(
        self,
    ):
        from finance.settlement import applied_payment_amounts, collectible_amounts

        invoice = self.invoice()
        advance = f.record_customer_advance(
            organization=self.org,
            actor=self.owner,
            party=self.party,
            transaction=self.movement("advance", "200000"),
            amount="200000",
            reference="advance",
            evidence="Synthetic receipt",
        )
        f.apply_customer_advance(
            organization=self.org,
            actor=self.owner,
            advance=advance,
            invoice=invoice,
            date=date(2026, 1, 11),
            amount="200000",
            reference="apply",
            evidence="Synthetic application",
        )
        f.reconcile_receipt(
            organization=self.org,
            invoice=invoice,
            transaction=self.movement("ordinary", "100000", date(2026, 1, 12)),
            amount="100000",
        )
        f.cancel_invoice(
            organization=self.org,
            actor=self.owner,
            invoice=invoice,
            date=date(2026, 1, 13),
            reason="Cancelled before test",
        )
        for cutoff, expected in [
            (date(2026, 1, 9), "0"),
            (self.day, "500000"),
            (date(2026, 1, 11), "300000"),
            (date(2026, 1, 12), "200000"),
            (date(2026, 1, 13), "0"),
            (None, "0"),
        ]:
            with self.subTest(cutoff=cutoff):
                self.assertEqual(
                    collectible_amounts(
                        organization=self.org, invoice_ids=[invoice.pk], as_of=cutoff
                    ),
                    {invoice.pk: Decimal(expected)},
                )
        self.assertEqual(
            applied_payment_amounts(organization=self.org, invoice_ids=[invoice.pk]),
            {invoice.pk: Decimal(300000)},
        )
        self.assertEqual(
            applied_payment_amounts(
                organization=self.org, invoice_ids=[invoice.pk], as_of=self.day
            ),
            {invoice.pk: Decimal(0)},
        )

    def test_scope_drafts_cutoff_validation_and_duplicate_ids(self):
        from finance.settlement import applied_payment_amounts, collectible_amounts

        draft = self.invoice(issued=False)
        issued = self.invoice()
        other = Organization.objects.create(name="Other settlement company")
        for helper in (collectible_amounts, applied_payment_amounts):
            self.assertEqual(
                helper(organization=other, invoice_ids=[issued.pk, draft.pk]), {}
            )
            self.assertEqual(
                helper(organization=self.org, invoice_ids=[draft.pk, draft.pk]),
                {draft.pk: Decimal(0)},
            )
            for bad in ("invalid", 3, True):
                with (
                    self.subTest(helper=helper.__name__, cutoff=bad),
                    self.assertRaises(ValidationError),
                ):
                    helper(organization=self.org, invoice_ids=[issued.pk], as_of=bad)
            for bad_ids in (None, [True], ["1"], [-1]):
                with self.subTest(ids=bad_ids), self.assertRaises(ValidationError):
                    helper(organization=self.org, invoice_ids=bad_ids)
        f.cancel_invoice(
            organization=self.org,
            actor=self.owner,
            invoice=draft,
            date=self.day,
            reason="Unused draft",
        )
        self.assertEqual(
            collectible_amounts(
                organization=self.org, invoice_ids=[draft.pk], as_of=self.day
            ),
            {draft.pk: Decimal(0)},
        )

    def test_large_internal_id_collections_are_chunked_without_a_company_size_limit(
        self,
    ):
        from finance.settlement import collectible_amounts

        invoice = self.invoice()
        ids = [invoice.pk] * 10001
        self.assertEqual(
            collectible_amounts(organization=self.org, invoice_ids=ids),
            {invoice.pk: Decimal(500000)},
        )
        ids = [invoice.pk] + list(range(invoice.pk + 1, invoice.pk + 503))
        self.assertEqual(
            collectible_amounts(organization=self.org, invoice_ids=ids),
            {invoice.pk: Decimal(500000)},
        )
