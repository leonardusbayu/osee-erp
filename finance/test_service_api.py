from django.test import SimpleTestCase

from finance import services


class FinanceServiceFacadeTests(SimpleTestCase):
    def test_tax_settlement_actions_remain_available_to_finance_callers(self):
        public_actions = (
            "reconcile_customer_tax_receipt",
            "recognize_customer_tax_credit",
            "recognize_own_tax",
            "reconcile_own_tax_payment",
        )

        self.assertTrue(
            all(callable(getattr(services, name, None)) for name in public_actions)
        )
