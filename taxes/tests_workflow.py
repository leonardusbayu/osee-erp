from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings

from core.models import Membership, Organization
from taxes.models import TaxObligation, TaxProfile
from taxes.services import approve_tax_obligation, annual_readiness


@override_settings(OPENROUTER_API_KEY="")
class TaxReleaseWorkflowTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Synthetic tax workflow", is_demo=True)
        self.owner = get_user_model().objects.create_user(username="tax-release-owner")
        self.reviewer = get_user_model().objects.create_user(username="tax-release-reviewer")
        Membership.objects.create(organization=self.org, user=self.owner, role="owner")
        Membership.objects.create(organization=self.org, user=self.reviewer, role="reviewer")

    def test_inconsistent_workpaper_cannot_receive_approval(self):
        row = TaxObligation.objects.create(organization=self.org, period=date(2026, 9, 1),
            tax_type="pph23", direction="payable", base=Decimal("1000000"), rate=Decimal("0.02"),
            amount=Decimal("1"), source_reference="Synthetic invoice", rule_reference="Synthetic rule")
        with self.assertRaises(ValidationError):
            approve_tax_obligation(row, self.reviewer)

    def test_inactive_reviewer_cannot_approve(self):
        self.reviewer.is_active = False
        self.reviewer.save(update_fields=["is_active"])
        row = TaxObligation.objects.create(organization=self.org, period=date(2026, 9, 1), tax_type="pph23", direction="payable")
        with self.assertRaises(PermissionDenied):
            approve_tax_obligation(row, self.reviewer)
