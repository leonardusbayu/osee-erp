"""Same-selection duplicate review uses the public staged intake commands."""

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase

from core.models import Membership, Organization
from finance.models import Party, Product

from .intake import accept_rows, map_batch, stage_upload
from .models import Registration
from .services import create_order


class IntakeSelectionDuplicateTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Synthetic intake duplicate review")
        self.actor = get_user_model().objects.create_user(
            username="intake-duplicate-owner"
        )
        Membership.objects.create(organization=self.org, user=self.actor, role="owner")
        buyer = Party.objects.create(
            organization=self.org, name="Synthetic buyer", kind="reseller"
        )
        self.product = Product.objects.create(
            organization=self.org,
            code="DUP-ITP",
            name="ITP",
            kind="itp",
            default_price="510000",
        )
        self.order = create_order(
            organization=self.org,
            actor=self.actor,
            reference="DUP-ORDER",
            buyer=buyer,
            date="2026-01-05",
        )

    def staged(self, records):
        content = ("id,name,date\n" + records).encode("utf-8")
        batch = stage_upload(
            organization=self.org,
            actor=self.actor,
            content=content,
            filename="synthetic.csv",
            source="synthetic registrations",
        )
        return map_batch(
            organization=self.org,
            actor=self.actor,
            batch=batch,
            mapping={"external_id": "0", "name": "1", "planned_date": "2"},
            expected_version=batch.version,
        )

    def accept(self, batch, reviewed=False):
        return accept_rows(
            organization=self.org,
            actor=self.actor,
            batch=batch,
            row_ids=list(batch.rows.values_list("pk", flat=True)),
            order=self.order,
            product=self.product,
            expected_version=batch.version,
            reason="Identitas dan sumber diperiksa",
            confirm_identity=True,
            reviewed_duplicates=reviewed,
        )

    def test_same_selection_name_and_date_require_review_before_any_rows_are_accepted(
        self,
    ):
        batch = self.staged(
            "A,Synthetic participant,2026-01-05\nB,  SYNTHETIC PARTICIPANT  ,2026-01-05\n"
        )
        with self.assertRaises(ValidationError):
            self.accept(batch)
        self.assertFalse(Registration.objects.exists())
        self.assertFalse(self.order.lines.exists())
        self.assertEqual(batch.rows.filter(status="pending").count(), 2)
        batch.refresh_from_db()
        self.assertEqual(batch.version, 2)

    def test_explicit_review_keeps_distinct_source_ids_and_never_merges_names(self):
        batch = self.staged(
            "A,Synthetic participant,2026-01-05\nB,Synthetic participant,2026-01-05\n"
        )
        result = self.accept(batch, reviewed=True)
        self.assertEqual(result["created"], 2)
        self.assertEqual(
            set(Registration.objects.values_list("external_id", flat=True)), {"A", "B"}
        )
        self.assertEqual(batch.rows.filter(status="accepted").count(), 2)

    def test_same_name_known_different_dates_are_not_automatically_duplicates(self):
        batch = self.staged(
            "A,Synthetic participant,2026-01-05\nB,Synthetic participant,2026-01-06\n"
        )
        self.assertEqual(self.accept(batch)["created"], 2)

    def test_missing_date_cannot_disambiguate_same_selection_names_in_either_order(
        self,
    ):
        for dates in (("", "2026-01-05"), ("2026-01-05", "")):
            with self.subTest(dates=dates):
                batch = self.staged(
                    f"A,Synthetic participant,{dates[0]}\nB,Synthetic participant,{dates[1]}\n"
                )
                with self.assertRaises(ValidationError):
                    self.accept(batch)
        self.assertFalse(Registration.objects.exists())
