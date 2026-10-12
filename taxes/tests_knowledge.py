from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase

from core.models import Organization
from taxes.knowledge import (
    activate_source_revision,
    current_sources,
    historical_cards,
    source_cards,
)
from taxes.models import (
    ChatMessage,
    TaxSource,
    TaxSourcePublication,
    TaxSourceRevision,
)
from taxes.services import annual_readiness, monthly_tax_context, seed_tax_sources


class KnowledgeVersionTests(TestCase):
    def test_recipient_identity_sources_are_staged_with_separate_dates(self):
        seed_tax_sources()
        expected = {
            "pph23-identity-law": None,
            "pmk81-identity-2024": date(2025, 1, 1),
            "per7-2025": date(2025, 5, 21),
            "peng6-2024": None,
        }
        for slug, start in expected.items():
            with self.subTest(slug=slug):
                row = TaxSource.objects.get(slug=slug)
                self.assertEqual(row.reviewed_on, date(2026, 10, 9))
                self.assertEqual(row.effective_from, start)
                self.assertFalse(row.approved)
                self.assertEqual(current_sources(slugs=[slug]), [])
        self.assertEqual(
            TaxSource.objects.get(slug="pph23-law").reviewed_on, date(2026, 9, 9)
        )
        self.assertEqual(
            TaxSource.objects.get(slug="pmk81-2024").reviewed_on, date(2026, 9, 9)
        )

    def test_monthly_gross_basis_source_has_its_own_date_and_requires_review(self):
        seed_tax_sources()
        source = TaxSource.objects.get(slug="pmk164-2023")
        self.assertEqual(source.reviewed_on, date(2026, 10, 9))
        self.assertEqual(source.effective_from, date(2023, 12, 29))
        self.assertFalse(source.approved)
        self.assertEqual(current_sources(slugs=[source.slug]), [])
        self.assertIn("Pasal 6", source.article)
        self.assertIn("sebelum potongan", source.summary)
        self.assertIn("orang pribadi", source.summary)
        self.assertEqual(
            TaxSource.objects.get(slug="per11-2025").reviewed_on, date(2026, 9, 9)
        )

    def test_new_reporting_source_has_its_own_date_and_needs_publication_review(self):
        seed_tax_sources()
        source = TaxSource.objects.get(slug="per12-2026")
        self.assertEqual(source.reviewed_on, date(2026, 10, 9))
        self.assertEqual(source.effective_from, date(2026, 10, 1))
        self.assertFalse(source.approved)
        self.assertEqual(current_sources(slugs=[source.slug]), [])
        old = TaxSource.objects.get(slug="per11-2025")
        self.assertEqual(old.reviewed_on, date(2026, 9, 9))

    def test_catalog_stages_without_automatically_approving_sources(self):
        seed_tax_sources()
        self.assertTrue(TaxSourceRevision.objects.exists())
        self.assertFalse(TaxSource.objects.filter(approved=True).exists())
        self.assertEqual(current_sources(), [])

    def test_reseeding_an_unchanged_reviewed_catalog_is_idempotent(self):
        seed_tax_sources(
            reviewed_by="Synthetic reviewer",
            review_reference="Synthetic review record",
        )
        first_publication_count = TaxSourcePublication.objects.count()
        seed_tax_sources(
            reviewed_by="Synthetic reviewer",
            review_reference="Synthetic review record",
        )
        self.assertEqual(TaxSourcePublication.objects.count(), first_publication_count)

    def test_explicit_review_is_recorded_and_immutable(self):
        seed_tax_sources()
        revision = TaxSourceRevision.objects.filter(source__slug="pp20-2026").first()
        with self.assertRaises(ValidationError):
            activate_source_revision(
                revision=revision,
                reviewer_name="",
                review_reference="",
                confirm_reviewed=True,
            )
        publication = activate_source_revision(
            revision=revision,
            reviewer_name="Synthetic reviewer",
            review_reference="Synthetic legal review",
            confirm_reviewed=True,
        )
        self.assertEqual([source.slug for source in current_sources()], ["pp20-2026"])
        with self.assertRaises(ValidationError):
            publication.save()
        revision.snapshot = {}
        with self.assertRaises(ValidationError):
            revision.save()

    def test_publication_is_attributed_and_the_same_revision_cannot_be_republished(
        self,
    ):
        seed_tax_sources()
        revision = TaxSourceRevision.objects.filter(source__slug="pp20-2026").first()
        actor = get_user_model().objects.create_user(username="source-steward")
        publication = activate_source_revision(
            revision=revision,
            reviewer_name="Synthetic reviewer",
            review_reference="Synthetic review record",
            confirm_reviewed=True,
            actor=actor,
        )
        self.assertEqual(publication.published_by, actor)
        with self.assertRaisesMessage(ValidationError, "sudah pernah diterbitkan"):
            activate_source_revision(
                revision=revision,
                reviewer_name="Synthetic reviewer",
                review_reference="Synthetic review record",
                confirm_reviewed=True,
                actor=actor,
            )
        self.assertEqual(TaxSourcePublication.objects.count(), 1)

    def test_staging_cannot_publish_edits_to_an_approved_head(self):
        seed_tax_sources(
            reviewed_by="Synthetic reviewer", review_reference="Synthetic review"
        )
        TaxSource.objects.filter(slug="pmk81-2024").update(
            summary="Unreviewed changed legal summary"
        )
        self.assertEqual(current_sources(slugs=["pmk81-2024"]), [])
        seed_tax_sources()
        self.assertEqual(current_sources(slugs=["pmk81-2024"]), [])
        self.assertTrue(TaxSource.objects.get(slug="pmk81-2024").approved)

    def test_stale_sources_are_excluded_everywhere_and_historical_cards_do_not_change(
        self,
    ):
        seed_tax_sources(
            reviewed_by="Synthetic reviewer", review_reference="Synthetic review"
        )
        saved = source_cards(current_sources(slugs=["pmk81-2024"]))
        message = ChatMessage(
            source_ids=["pmk81-2024"], metadata={"source_cards": saved}
        )
        old_title = saved[0]["title"]
        TaxSource.objects.filter(slug="pmk81-2024").update(
            title="Changed unpublished title"
        )
        card = historical_cards(message)[0]
        self.assertEqual(card["title"], old_title)
        self.assertFalse(card["current"])
        org = Organization.objects.create(name="Synthetic knowledge org")
        with patch("django.utils.timezone.localdate", return_value=date(2027, 9, 9)):
            self.assertEqual(current_sources(), [])
            self.assertEqual(monthly_tax_context(org, "2026-09")["sources"], [])
            self.assertEqual(annual_readiness(org, 2026)["sources"], [])

    def test_malformed_historical_metadata_shows_unavailable_review_date(self):
        for metadata in (
            None,
            [],
            {"source_review_dates": None},
            {"source_review_dates": []},
        ):
            with self.subTest(metadata=metadata):
                message = ChatMessage(source_ids=["pph23-law"], metadata=metadata)
                card = historical_cards(message)[0]
                self.assertEqual(card["reviewed_on"], "Tidak tersedia")
                self.assertFalse(card["current"])
