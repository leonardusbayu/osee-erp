from datetime import date
from unittest.mock import patch
from django.test import TestCase
from django.core.exceptions import ValidationError
from taxes.models import TaxSource, TaxSourceRevision, ChatMessage
from taxes.services import seed_tax_sources, monthly_tax_context, annual_readiness
from taxes.knowledge import current_sources, activate_source_revision, historical_cards, source_cards
from core.models import Organization


class KnowledgeVersionTests(TestCase):
    def test_catalog_stages_without_automatically_approving_sources(self):
        seed_tax_sources()
        self.assertTrue(TaxSourceRevision.objects.exists())
        self.assertFalse(TaxSource.objects.filter(approved=True).exists())
        self.assertEqual(current_sources(), [])

    def test_explicit_review_is_recorded_and_immutable(self):
        seed_tax_sources()
        revision = TaxSourceRevision.objects.filter(source__slug="pp20-2026").first()
        with self.assertRaises(ValidationError):
            activate_source_revision(revision=revision, reviewer_name="", review_reference="", confirm_reviewed=True)
        publication = activate_source_revision(revision=revision, reviewer_name="Synthetic reviewer", review_reference="Synthetic legal review", confirm_reviewed=True)
        self.assertEqual([source.slug for source in current_sources()], ["pp20-2026"])
        with self.assertRaises(ValidationError):
            publication.save()
        revision.snapshot = {}
        with self.assertRaises(ValidationError):
            revision.save()

    def test_staging_cannot_publish_edits_to_an_approved_head(self):
        seed_tax_sources(reviewed_by="Synthetic reviewer", review_reference="Synthetic review")
        TaxSource.objects.filter(slug="pmk81-2024").update(summary="Unreviewed changed legal summary")
        self.assertEqual(current_sources(slugs=["pmk81-2024"]), [])
        seed_tax_sources()
        self.assertEqual(current_sources(slugs=["pmk81-2024"]), [])
        self.assertTrue(TaxSource.objects.get(slug="pmk81-2024").approved)

    def test_stale_sources_are_excluded_everywhere_and_historical_cards_do_not_change(self):
        seed_tax_sources(reviewed_by="Synthetic reviewer", review_reference="Synthetic review")
        saved = source_cards(current_sources(slugs=["pmk81-2024"]))
        message = ChatMessage(source_ids=["pmk81-2024"], metadata={"source_cards": saved})
        old_title = saved[0]["title"]
        TaxSource.objects.filter(slug="pmk81-2024").update(title="Changed unpublished title")
        card = historical_cards(message)[0]
        self.assertEqual(card["title"], old_title)
        self.assertFalse(card["current"])
        org = Organization.objects.create(name="Synthetic knowledge org")
        with patch("django.utils.timezone.localdate", return_value=date(2027, 9, 9)):
            self.assertEqual(current_sources(), [])
            self.assertEqual(monthly_tax_context(org, "2026-09")["sources"], [])
            self.assertEqual(annual_readiness(org, 2026)["sources"], [])
