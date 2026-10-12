"""Original HTTP oracles for InvoiceForm.unit_price; source-only frozen draft.

The literal expected amounts are independent of any proposed money field/parser.
ROOT must archive this source before the first native RED run.
"""

from datetime import date, timedelta
from decimal import Decimal
from unittest import skipUnless

from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from core.models import (
    AuditEvent,
    DocumentCounter,
    DomainEvent,
    Membership,
    Organization,
)
from finance.costing_models import PriceOverrideRequest, PriceOverrideUse
from finance.models import Invoice, Journal, JournalLine, Party, PriceVersion, Product


@override_settings(MFA_REQUIRED=False)
class InvoiceRupiahHttpOriginalTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Synthetic original invoice IDR")
        self.owner = get_user_model().objects.create_user("original-idr-owner")
        self.finance = get_user_model().objects.create_user("original-idr-finance")
        Membership.objects.create(organization=self.org, user=self.owner, role="owner")
        Membership.objects.create(
            organization=self.org, user=self.finance, role="finance"
        )
        self.party = Party.objects.create(
            organization=self.org, name="Synthetic original partner", kind="reseller"
        )
        self.product = Product.objects.create(
            organization=self.org,
            code="ORIG-IDR",
            name="Synthetic original ITP",
            kind="itp",
            default_price="510000.00",
        )
        self.day = date(2026, 10, 10)
        self.data = {
            "party": self.party.pk,
            "product": self.product.pk,
            "quantity": "2",
            "date": self.day.isoformat(),
            "due_date": self.day.isoformat(),
            "service_date": self.day.isoformat(),
        }
        self.client.force_login(self.finance)

    def financial_source_snapshot(self):
        """Compare persisted rows, including foreign organizations and row updates."""
        models = [Organization, Membership, AuditEvent, DomainEvent, DocumentCounter]
        for app_label in ("finance", "taxes", "operations"):
            models.extend(apps.get_app_config(app_label).get_models())
        return {
            model._meta.label: list(model._base_manager.order_by("pk").values())
            for model in models
        }

    def approved_quote_via_http(self):
        """Obtain authority from the existing Finance request / Owner decision flow."""
        response = self.client.post(
            reverse("price_request_create"),
            {
                "party": self.party.pk,
                "product": self.product.pk,
                "quantity": "2",
                "date": self.day.isoformat(),
                "unit_price": "505000.25",
                "reason": "Synthetic negotiated two-participant offer",
                "evidence": "Synthetic original written offer IDR-001",
            },
        )
        self.assertEqual(response.status_code, 302)
        detail_url = response.url
        detail = self.client.get(detail_url)
        self.assertEqual(detail.status_code, 200)
        item = detail.context["item"]
        self.assertEqual(item.unit_price, Decimal("505000.25"))
        decision = {
            "action": "decision",
            "expected_fingerprint": item.fingerprint,
            "decision": "approved",
            "reason": "Synthetic margin review completed",
            "evidence": "Synthetic Owner review of original written offer IDR-001",
        }
        before = self.financial_source_snapshot()
        self.assertEqual(self.client.post(detail_url, decision).status_code, 403)
        self.assertEqual(self.financial_source_snapshot(), before)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.post(detail_url, decision).status_code, 302)
        self.client.force_login(self.finance)
        return PriceOverrideRequest.objects.get(pk=item.pk)

    def test_literal_plain_canonical_and_indonesian_prices_persist_exact_draft_totals(
        self,
    ):
        # These literals establish the magnitude independently: 50.00 is fifty;
        # 50.000 is fifty thousand. Each product baseline already permits the price.
        cases = (
            ("50000", "50000.00", "150000.00"),
            ("50000.00", "50000.00", "150000.00"),
            ("50000.5", "50000.50", "150001.50"),
            ("50.000", "50000.00", "150000.00"),
            ("50.000,00", "50000.00", "150000.00"),
            ("50.000,5", "50000.50", "150001.50"),
            ("50000,25", "50000.25", "150000.75"),
            ("50.00", "50.00", "150.00"),
            ("0,01", "0.01", "0.03"),
        )
        for index, (raw_price, expected_price, expected_total) in enumerate(cases):
            with self.subTest(raw_price=raw_price):
                product = Product.objects.create(
                    organization=self.org,
                    code=f"ORIG-IDR-{index}",
                    name=f"Synthetic literal-price product {index}",
                    kind="itp",
                    default_price=expected_price,
                )
                source_before = list(Product.objects.order_by("pk").values())
                before = Invoice.objects.count()
                response = self.client.post(
                    reverse("sale_create"),
                    {
                        **self.data,
                        "product": product.pk,
                        "quantity": "3",
                        "unit_price": raw_price,
                    },
                )
                self.assertEqual(
                    response.status_code,
                    302,
                    msg=(
                        f"Invoice form rejected {raw_price}: "
                        f"{response.context['form'].errors if response.context else 'no form context'}"
                    ),
                )
                self.assertEqual(Invoice.objects.count(), before + 1)
                invoice = Invoice.objects.get(product=product)
                self.assertEqual(
                    response.url, reverse("sale_detail", args=[invoice.pk])
                )
                self.assertEqual(invoice.organization_id, self.org.pk)
                self.assertEqual(invoice.party_id, self.party.pk)
                self.assertEqual(invoice.quantity, 3)
                self.assertEqual(invoice.unit_price, Decimal(expected_price))
                self.assertEqual(invoice.total, Decimal(expected_total))
                self.assertEqual(invoice.status, "draft")
                self.assertIsNone(invoice.price_version_id)
                audit = AuditEvent.objects.get(
                    action="finance.invoice.created", object_id=str(invoice.pk)
                )
                self.assertEqual(audit.actor_id, self.finance.pk)
                self.assertEqual(audit.detail["total"], expected_total)
                self.assertEqual(
                    list(Product.objects.order_by("pk").values()), source_before
                )
                self.assertFalse(Journal.objects.exists())
                self.assertFalse(JournalLine.objects.exists())
                self.assertFalse(DomainEvent.objects.exists())

    @skipUnless(
        connection.vendor == "postgresql",
        "SQLite stores DecimalField values with 15 significant digits; exact high-value cents require PostgreSQL NUMERIC.",
    )
    def test_maximum_supported_unit_price_persists_exact_http_draft_totals(self):
        price = Decimal("99999999999999.99")
        product = Product.objects.create(
            organization=self.org,
            code="ORIG-IDR-MAX",
            name="Synthetic maximum-price product",
            kind="itp",
            default_price=price,
        )
        self.assertEqual(Product.objects.get(pk=product.pk).default_price, price)
        response = self.client.post(
            reverse("sale_create"),
            {
                **self.data,
                "product": product.pk,
                "quantity": "3",
                "unit_price": "99.999.999.999.999,99",
            },
        )
        self.assertEqual(
            response.status_code,
            302,
            msg=(
                "Maximum exact price failed: "
                f"{response.context['form'].errors if response.context else 'no form context'}"
            ),
        )
        invoice = Invoice.objects.get(product=product)
        self.assertEqual(invoice.unit_price, price)
        self.assertEqual(invoice.total, Decimal("299999999999999.97"))
        self.assertEqual(invoice.status, "draft")

    def test_blank_price_keeps_product_fallback_and_active_partner_price_authority(
        self,
    ):
        response = self.client.post(
            reverse("sale_create"), {**self.data, "unit_price": ""}
        )
        self.assertEqual(response.status_code, 302)
        product_fallback = Invoice.objects.get()
        self.assertEqual(product_fallback.unit_price, Decimal("510000.00"))
        self.assertEqual(product_fallback.total, Decimal("1020000.00"))
        self.assertIsNone(product_fallback.price_version_id)

        PriceVersion.objects.create(
            organization=self.org,
            product=self.product,
            kind="selling",
            amount="515000.00",
            effective_from=self.day - timedelta(days=2),
        )
        PriceVersion.objects.create(
            organization=self.org,
            party=self.party,
            product=self.product,
            kind="selling",
            amount="507000.00",
            effective_from=self.day - timedelta(days=2),
            effective_to=self.day - timedelta(days=1),
        )
        partner_price = PriceVersion.objects.create(
            organization=self.org,
            party=self.party,
            product=self.product,
            kind="selling",
            amount="503000.50",
            effective_from=self.day,
            effective_to=self.day,
        )
        prices_before = list(PriceVersion.objects.order_by("pk").values())
        product_before = list(Product.objects.order_by("pk").values())
        response = self.client.post(
            reverse("sale_create"), {**self.data, "unit_price": ""}
        )
        self.assertEqual(response.status_code, 302)
        partner_invoice = Invoice.objects.exclude(pk=product_fallback.pk).get()
        self.assertEqual(partner_invoice.unit_price, Decimal("503000.50"))
        self.assertEqual(partner_invoice.total, Decimal("1006001.00"))
        self.assertEqual(partner_invoice.price_version_id, partner_price.pk)
        self.assertEqual(partner_invoice.status, "draft")
        self.assertEqual(
            list(PriceVersion.objects.order_by("pk").values()), prices_before
        )
        self.assertEqual(list(Product.objects.order_by("pk").values()), product_before)
        self.assertFalse(PriceOverrideUse.objects.exists())
        self.assertEqual(
            AuditEvent.objects.filter(action="finance.invoice.created").count(), 2
        )
        self.assertFalse(Journal.objects.exists())
        self.assertFalse(DomainEvent.objects.exists())

    def test_grouped_special_price_needs_exact_owner_approval_and_can_be_used_only_once(
        self,
    ):
        before = self.financial_source_snapshot()
        response = self.client.post(
            reverse("sale_create"),
            {
                **self.data,
                "unit_price": "505.000,25",
                "override_reason": "Synthetic offer only",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Harga khusus memerlukan persetujuan")
        self.assertEqual(self.financial_source_snapshot(), before)

        approved = self.approved_quote_via_http()
        other_party = Party.objects.create(
            organization=self.org, name="Synthetic other partner", kind="reseller"
        )
        other_product = Product.objects.create(
            organization=self.org,
            code="ORIG-IDR-OTHER",
            name="Synthetic other product",
            kind="itp",
            default_price="510000.00",
        )
        next_day = (self.day + timedelta(days=1)).isoformat()
        special = {
            **self.data,
            "unit_price": "505.000,25",
            "price_override": approved.pk,
        }
        mismatches = (
            {"party": other_party.pk},
            {"product": other_product.pk},
            {"quantity": "3"},
            {"date": next_day, "due_date": next_day, "service_date": next_day},
            {"unit_price": "505.000,26"},
        )
        for changed_fields in mismatches:
            with self.subTest(changed_fields=changed_fields):
                before = self.financial_source_snapshot()
                response = self.client.post(
                    reverse("sale_create"), {**special, **changed_fields}
                )
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Persetujuan harga tidak sesuai")
                self.assertEqual(self.financial_source_snapshot(), before)

        original_request = list(PriceOverrideRequest.objects.order_by("pk").values())
        response = self.client.post(reverse("sale_create"), special)
        self.assertEqual(response.status_code, 302)
        invoice = Invoice.objects.get()
        self.assertEqual(invoice.unit_price, Decimal("505000.25"))
        self.assertEqual(invoice.total, Decimal("1010000.50"))
        self.assertEqual(invoice.quantity, 2)
        self.assertEqual(invoice.date, self.day)
        self.assertEqual(invoice.status, "draft")
        self.assertIsNone(invoice.price_version_id)
        use = PriceOverrideUse.objects.get()
        self.assertEqual(use.request_id, approved.pk)
        self.assertEqual(use.invoice_id, invoice.pk)
        self.assertEqual(use.created_by_id, self.finance.pk)
        self.assertEqual(
            list(PriceOverrideRequest.objects.order_by("pk").values()), original_request
        )
        self.assertEqual(
            AuditEvent.objects.get(action="finance.invoice.created").detail["total"],
            "1010000.50",
        )
        self.assertFalse(Journal.objects.exists())
        self.assertFalse(DomainEvent.objects.exists())

        before = self.financial_source_snapshot()
        repeated = self.client.post(reverse("sale_create"), special)
        self.assertEqual(repeated.status_code, 200)
        self.assertIn("price_override", repeated.context["form"].errors)
        self.assertEqual(self.financial_source_snapshot(), before)

        # Selecting an approved quote with an empty input retains the existing
        # optional-price behavior; the approval, rather than the product, supplies it.
        blank_approved = self.approved_quote_via_http()
        response = self.client.post(
            reverse("sale_create"),
            {
                **self.data,
                "unit_price": "",
                "price_override": blank_approved.pk,
            },
        )
        self.assertEqual(response.status_code, 302)
        blank_invoice = Invoice.objects.exclude(pk=invoice.pk).get()
        self.assertEqual(blank_invoice.unit_price, Decimal("505000.25"))
        self.assertEqual(blank_invoice.total, Decimal("1010000.50"))
        self.assertIsNone(blank_invoice.price_version_id)
        self.assertEqual(
            PriceOverrideUse.objects.get(invoice=blank_invoice).request_id,
            blank_approved.pk,
        )
        self.assertEqual(PriceOverrideUse.objects.count(), 2)

    def test_invalid_price_text_precision_and_bounds_cannot_write_or_consume_approval(
        self,
    ):
        approved = self.approved_quote_via_http()
        # An invalid explicit value must remain invalid even when blank would be
        # filled from the selected, genuine approval by InvoiceForm.clean().
        invalid_prices = (
            "50,000",
            "Rp 505.000,25",
            "505.00.0",
            "50.50,00",
            "505.000.25",
            "505000.250",
            "505.000,250",
            "5.05e5",
            "505_000.25",
            "+505000.25",
            "NaN",
            "Infinity",
            "-Infinity",
            "５０５０００.２５",
            "٥٠٥٠٠٠.٢٥",
            "505\u00a0000,25",
            "505 000,25",
            "0",
            "0,00",
            "-0.01",
            "100000000000000",
            "100000000000000.00",
            "100.000.000.000.000,00",
        )
        for raw_price in invalid_prices:
            with self.subTest(raw_price=raw_price):
                before = self.financial_source_snapshot()
                response = self.client.post(
                    reverse("sale_create"),
                    {
                        **self.data,
                        "unit_price": raw_price,
                        "price_override": approved.pk,
                    },
                )
                self.assertEqual(response.status_code, 200)
                self.assertIn("unit_price", response.context["form"].errors)
                self.assertEqual(self.financial_source_snapshot(), before)
                self.assertFalse(Invoice.objects.exists())
                self.assertFalse(PriceOverrideUse.objects.exists())

    def test_price_entry_keeps_role_company_and_csrf_boundaries(self):
        other_org = Organization.objects.create(
            name="Synthetic foreign invoice company"
        )
        foreign_party = Party.objects.create(
            organization=other_org,
            name="Synthetic private foreign partner",
            kind="reseller",
        )
        foreign_product = Product.objects.create(
            organization=other_org,
            code="FOREIGN-IDR",
            name="Synthetic private foreign product",
            kind="itp",
            default_price="510000.00",
        )
        for field, foreign_pk in (
            ("party", foreign_party.pk),
            ("product", foreign_product.pk),
        ):
            with self.subTest(foreign_field=field):
                before = self.financial_source_snapshot()
                response = self.client.post(
                    reverse("sale_create"),
                    {
                        **self.data,
                        "unit_price": "510.000",
                        field: foreign_pk,
                    },
                )
                self.assertEqual(response.status_code, 200)
                self.assertIn(field, response.context["form"].errors)
                self.assertEqual(self.financial_source_snapshot(), before)

        for role in ("marketing", "auditor"):
            with self.subTest(role=role):
                user = get_user_model().objects.create_user(f"original-idr-{role}")
                Membership.objects.create(organization=self.org, user=user, role=role)
                reader = Client()
                reader.force_login(user)
                before = self.financial_source_snapshot()
                response = reader.post(
                    reverse("sale_create"),
                    {
                        **self.data,
                        "unit_price": "510.000",
                        "actor": self.owner.pk,
                    },
                )
                self.assertEqual(response.status_code, 403)
                self.assertEqual(self.financial_source_snapshot(), before)

        secure = Client(enforce_csrf_checks=True)
        secure.force_login(self.finance)
        before = self.financial_source_snapshot()
        response = secure.post(
            reverse("sale_create"), {**self.data, "unit_price": "510.000"}
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.financial_source_snapshot(), before)

        page = secure.get(reverse("sale_create"))
        self.assertEqual(page.status_code, 200)
        csrf_token = secure.cookies["csrftoken"].value
        data = {
            **self.data,
            "unit_price": "510.000",
            "csrfmiddlewaretoken": csrf_token,
            "organization": other_org.pk,
            "organization_id": other_org.pk,
            "actor": self.owner.pk,
        }
        before = self.financial_source_snapshot()
        response = secure.post(
            reverse("sale_create"), data, HTTP_ORIGIN="https://untrusted.invalid"
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.financial_source_snapshot(), before)
        response = secure.post(reverse("sale_create"), data)
        self.assertEqual(response.status_code, 302)
        invoice = Invoice.objects.get()
        self.assertEqual(invoice.organization_id, self.org.pk)
        self.assertEqual(invoice.unit_price, Decimal("510000.00"))
        self.assertEqual(invoice.total, Decimal("1020000.00"))
        audit = AuditEvent.objects.get(action="finance.invoice.created")
        self.assertEqual(audit.organization_id, self.org.pk)
        self.assertEqual(audit.actor_id, self.finance.pk)
        self.assertFalse(Invoice.objects.filter(organization=other_org).exists())
        self.assertFalse(Journal.objects.exists())
        self.assertFalse(DomainEvent.objects.exists())
