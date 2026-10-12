from django.test import SimpleTestCase

from taxes import services


class TaxServiceFacadeTests(SimpleTestCase):
    def test_workflow_actions_remain_available_from_the_public_service_module(self):
        public_actions = (
            "approve_tax_profile",
            "approve_tax_obligation",
            "prepare_obligation",
            "review_bill_tax",
            "record_external_review",
            "upload_evidence",
            "verify_obligation_evidence",
            "prepare_annual_workpaper",
            "approve_annual_workpaper",
            "verify_annual_filing",
            "withdraw_obligation",
        )

        self.assertTrue(
            all(callable(getattr(services, name, None)) for name in public_actions)
        )
