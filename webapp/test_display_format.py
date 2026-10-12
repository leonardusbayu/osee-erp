from decimal import Decimal

from django.test import SimpleTestCase

from webapp.templatetags.finance_format import tax_rate_percent


class TaxRateDisplayTests(SimpleTestCase):
    def test_fractional_rates_are_displayed_as_percentages_and_unknown_is_not_zero(
        self,
    ):
        for value, expected in (
            (Decimal("0.005"), "0,5%"),
            (Decimal("0.02"), "2%"),
            (Decimal("0.025"), "2,5%"),
            (Decimal(0), "0%"),
            (Decimal(1), "100%"),
            (None, "—"),
            ("", "—"),
            ("NaN", "—"),
            ("Infinity", "—"),
            ("-0.01", "—"),
            ("2", "—"),
        ):
            with self.subTest(value=value):
                self.assertEqual(tax_rate_percent(value), expected)
