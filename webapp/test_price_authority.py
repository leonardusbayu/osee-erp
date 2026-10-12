"""Finance cannot evade quote approval by changing the pricing baseline."""

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import AuditEvent, Membership, Organization
from finance.models import Party, PriceVersion, Product


@override_settings(MFA_REQUIRED=False)
class PriceAuthorityTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Synthetic pricing authority")
        self.owner = get_user_model().objects.create_user("price-policy-owner")
        self.finance = get_user_model().objects.create_user("price-policy-finance")
        Membership.objects.create(organization=self.org, user=self.owner, role="owner")
        Membership.objects.create(
            organization=self.org, user=self.finance, role="finance"
        )
        self.party = Party.objects.create(
            organization=self.org, name="Synthetic partner", kind="reseller"
        )
        self.product = Product.objects.create(
            organization=self.org,
            code="ITP",
            name="Synthetic ITP",
            kind="itp",
            default_price="510000",
        )
        self.price_data = {
            "party": self.party.pk,
            "product": self.product.pk,
            "kind": "selling",
            "amount": "500000",
            "effective_from": "2026-01-01",
        }
        self.product_data = {
            "code": "IBT",
            "name": "Synthetic iBT",
            "kind": "ibt",
            "default_price": "3500000",
        }

    def test_finance_can_read_rates_and_request_exceptions_but_cannot_create_baseline_prices(
        self,
    ):
        self.client.force_login(self.finance)
        page = self.client.get(reverse("prices"))
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, reverse("price_create"))
        self.assertNotContains(page, reverse("product_create"))
        self.assertEqual(
            self.client.get(reverse("price_request_create")).status_code, 200
        )
        for route, data in (
            ("price_create", self.price_data),
            ("product_create", self.product_data),
        ):
            self.assertEqual(self.client.get(reverse(route)).status_code, 403)
            self.assertEqual(self.client.post(reverse(route), data).status_code, 403)
        self.assertFalse(PriceVersion.objects.exists())
        self.assertEqual(Product.objects.count(), 1)
        self.assertFalse(
            AuditEvent.objects.filter(
                action__in=["finance.price.created", "finance.product.created"]
            ).exists()
        )

    def test_director_creates_product_and_rate_with_audited_scoped_forms(self):
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse("prices")), reverse("price_create"))
        self.assertEqual(
            self.client.post(reverse("product_create"), self.product_data).status_code,
            302,
        )
        self.assertEqual(
            self.client.post(reverse("price_create"), self.price_data).status_code, 302
        )
        self.assertEqual(Product.objects.filter(organization=self.org).count(), 2)
        self.assertEqual(PriceVersion.objects.get().party_id, self.party.pk)
        self.assertEqual(
            AuditEvent.objects.filter(
                action__in=["finance.price.created", "finance.product.created"],
                actor=self.owner,
            ).count(),
            2,
        )
