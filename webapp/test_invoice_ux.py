"""The ordinary invoice journey keeps special pricing optional and controlled."""

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import Membership, Organization
from finance.models import Invoice, Party, Product


@override_settings(MFA_REQUIRED=False)
class SimpleInvoiceJourneyTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Synthetic simple invoice")
        self.user = get_user_model().objects.create_user("invoice-ux-finance")
        Membership.objects.create(organization=self.org, user=self.user, role="finance")
        self.client.force_login(self.user)
        self.party = Party.objects.create(
            organization=self.org, name="Synthetic buyer", kind="reseller"
        )
        self.product = Product.objects.create(
            organization=self.org,
            code="UXITP",
            name="Synthetic ITP",
            kind="itp",
            default_price="510000",
        )
        day = timezone.localdate().isoformat()
        self.data = {
            "party": self.party.pk,
            "product": self.product.pk,
            "quantity": 2,
            "date": day,
            "due_date": day,
            "service_date": day,
        }

    def test_standard_invoice_starts_with_order_fields_and_optional_special_price(self):
        response = self.client.get(reverse("sale_create"))
        self.assertContains(
            response, "<summary>Gunakan harga khusus</summary>", html=True
        )
        self.assertContains(response, 'id="special-price"')
        self.assertNotContains(response, 'id="special-price" open')
        content = response.content.decode()
        self.assertLess(
            content.index('id="id_service_date"'), content.index('id="special-price"')
        )
        for name in (
            "party",
            "product",
            "quantity",
            "date",
            "due_date",
            "service_date",
        ):
            self.assertIn('id="id_' + name + '"', content)

    def test_standard_price_can_be_used_without_opening_special_price_options(self):
        response = self.client.post(reverse("sale_create"), self.data)
        self.assertEqual(response.status_code, 302)
        invoice = Invoice.objects.get(organization=self.org)
        self.assertEqual(invoice.unit_price, self.product.default_price)
        self.assertEqual(invoice.status, "draft")

    def test_rejected_special_price_remains_visible_and_does_not_create_invoice(self):
        response = self.client.post(
            reverse("sale_create"),
            {
                **self.data,
                "unit_price": "505000",
                "override_reason": "Synthetic request, no approval",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="special-price" open')
        self.assertContains(response, 'value="505000"')
        self.assertContains(response, "Harga khusus memerlukan persetujuan")
        self.assertFalse(Invoice.objects.filter(organization=self.org).exists())
