"""Do not present an incomplete amended-return calculation as supported."""

import io

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from pypdf import PdfWriter

from core.models import Membership, Organization
from taxes import workflow
from taxes.forms import AnnualWorkpaperForm
from taxes.models import AnnualTaxWorkpaper


class AnnualAmendmentGateTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(
            name="Synthetic amendment gate", is_demo=True
        )
        self.owner = get_user_model().objects.create_user(
            username="amendment-gate-owner"
        )
        Membership.objects.create(organization=self.org, user=self.owner, role="owner")
        output = io.BytesIO()
        pdf = PdfWriter()
        pdf.add_blank_page(width=100, height=100)
        pdf.write(output)
        self.evidence = workflow.upload_evidence(
            organization=self.org,
            actor=self.owner,
            file=SimpleUploadedFile("synthetic-reconciliation.pdf", output.getvalue()),
        )
        self.values = {
            "organization": self.org,
            "actor": self.owner,
            "year": 2026,
            "positive_adjustments": "0",
            "negative_adjustments": "0",
            "loss_compensation": "0",
            "turnover": "0",
            "regime_code": "corporate_22",
            "instalments": "0",
            "support_evidence": self.evidence,
            "reconciliation_note": "Synthetic normal reconciliation",
        }

    def legacy_amendment(self):
        normal = workflow.prepare_annual_workpaper(**self.values)
        return workflow._save(
            AnnualTaxWorkpaper(
                organization=self.org,
                year=normal.year,
                book_snapshot=normal.book_snapshot,
                inputs={**normal.inputs, "return_version": 1},
                result=normal.result,
                calculation_version=normal.calculation_version,
                prepared_by=self.owner,
            )
        )

    def test_preparation_rejects_amendments_without_creating_workpaper(self):
        for version in (1, 2, 999, -1, True, "0"):
            with self.subTest(version=version), self.assertRaises(ValidationError):
                workflow.prepare_annual_workpaper(**self.values, return_version=version)
        self.assertFalse(AnnualTaxWorkpaper.objects.exists())
        self.assertEqual(
            workflow.prepare_annual_workpaper(**self.values).inputs["return_version"], 0
        )

    def test_form_offers_only_normal_return(self):
        field = AnnualWorkpaperForm(organization=self.org).fields["return_version"]
        self.assertEqual(field.clean("0"), 0)
        with self.assertRaises(ValidationError):
            field.clean("1")

    def test_annual_workpaper_form_rejects_years_without_a_supported_calculator(self):
        field = AnnualWorkpaperForm(organization=self.org).fields["year"]
        self.assertEqual(field.clean("2025"), 2025)
        self.assertEqual(field.clean("2026"), 2026)
        with self.assertRaises(ValidationError):
            field.clean("2027")

    def test_annual_workpaper_service_rejects_unsupported_year_before_readiness(self):
        with self.assertRaisesMessage(ValidationError, "2025 sampai 2026"):
            workflow.prepare_annual_workpaper(**{**self.values, "year": 2027})

    def test_legacy_amendment_cannot_be_approved_or_verified(self):
        row = self.legacy_amendment()
        with self.assertRaisesMessage(ValidationError, "Pembetulan"):
            workflow.approve_annual_workpaper(
                organization=self.org,
                actor=self.owner,
                workpaper=row,
                external_review=None,
            )
        with self.assertRaisesMessage(ValidationError, "Pembetulan"):
            workflow.verify_annual_filing(
                organization=self.org,
                actor=self.owner,
                workpaper=row,
                evidence=self.evidence,
                reference="SYNTHETIC-BPE",
                taxpayer_reference="1234567890123456",
                year=2026,
                return_version=1,
                reported_tax="0",
                reported_balance_due="0",
                matches_confirmed=True,
            )
        self.assertFalse(hasattr(row, "approval"))
        self.assertFalse(hasattr(row, "filing"))

    def test_legacy_detail_retains_history_and_explains_limit_without_approval_action(
        self,
    ):
        row = self.legacy_amendment()
        self.client.force_login(self.owner)
        response = self.client.get(reverse("taxes:annual_workpaper", args=[row.pk]))
        self.assertContains(response, "Pembetulan SPT perlu pemeriksaan terpisah")
        self.assertNotContains(response, reverse("taxes:annual_review", args=[row.pk]))
        self.assertContains(response, reverse("taxes:annual_export", args=[row.pk]))
