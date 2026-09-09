import csv
import io
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import SimpleTestCase, TestCase

from core.models import AuditEvent, Membership, Organization
from finance.models import BankTransaction, Bill, Invoice, Journal

from . import services
from .marketing_import import (CSV_COLUMNS, MAX_CSV_BYTES, import_marketing_csv,
                               marketing_csv_template, parse_marketing_csv)
from .models import MarketingObservation


def csv_bytes(rows, header=CSV_COLUMNS, *, bom=True):
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow([row.get(field, "") for field in header] if isinstance(row, dict) else row)
    return output.getvalue().encode("utf-8-sig" if bom else "utf-8")


def observation(**changes):
    return {"date": "2026-09-08", "channel": "meta_ads", "spend": "125000.50", "leads": "10",
            "paid_orders": "", "reported_value": "", "source": "Synthetic source", "reference": "campaign-day-001",
            "basis": "reported", **changes}


class MarketingCSVParserTests(SimpleTestCase):
    def test_template_has_bom_exact_header_and_no_fake_data(self):
        result = marketing_csv_template()
        self.assertTrue(result.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(list(csv.reader(io.StringIO(result.decode("utf-8-sig")))), [list(CSV_COLUMNS)])
        with self.assertRaisesMessage(ValidationError, "belum berisi"):
            parse_marketing_csv(result)

    def test_all_six_channels_and_optional_numeric_unknown_vs_zero(self):
        rows = [observation(channel=channel, reference=channel, spend="", leads="0")
                for channel in ("mitra", "meta_ads", "google_ads", "whatsapp", "seo", "sales")]
        parsed = parse_marketing_csv(csv_bytes(rows))
        self.assertEqual(len(parsed), 6)
        self.assertIsNone(parsed[0]["spend"])
        self.assertEqual(parsed[0]["leads"], 0)
        self.assertIsNone(parsed[0]["paid_orders"])
        self.assertEqual(parsed[0]["date"], date(2026, 9, 8))

    def test_utf8_without_bom_and_quoted_commas_preserve_source(self):
        result = parse_marketing_csv(csv_bytes([observation(source='Tim "OSEE", wilayah Jawa', reference=" uji-001 ")], bom=False))
        self.assertEqual(result[0]["source"], 'Tim "OSEE", wilayah Jawa')
        self.assertEqual(result[0]["reference"], "uji-001")
        self.assertEqual(result[0]["spend"], Decimal("125000.50"))

    def test_strict_header_rejects_reordering_duplicates_missing_extra_or_semicolon(self):
        for header in (tuple(reversed(CSV_COLUMNS)), CSV_COLUMNS[:-1], CSV_COLUMNS + ("extra",),
                       CSV_COLUMNS[:-1] + ("source",), ("Date",) + CSV_COLUMNS[1:]):
            with self.subTest(header=header), self.assertRaises(ValidationError):
                parse_marketing_csv(csv_bytes([observation()], header=header))
        with self.assertRaises(ValidationError):
            parse_marketing_csv(";".join(CSV_COLUMNS).encode())

    def test_invalid_dates_amounts_counts_basis_and_required_references(self):
        cases = [("date", "2026-02-30"), ("date", "1999-12-31"), ("date", "2201-01-01"),
                 ("date", "20260908"), ("date", "2026-09-08T00:00:00Z"),
                 ("spend", "NaN"), ("spend", "Infinity"), ("spend", "1e3"), ("spend", "-1"),
                 ("spend", "12.345"), ("spend", "1,000"), ("spend", "1000000000000000000"),
                 ("leads", "1.0"), ("leads", "-1"), ("paid_orders", "2147483648"),
                 ("channel", "Meta Ads"), ("basis", "ledger"), ("basis", ""),
                 ("source", " "), ("reference", ""), ("reference", "x" * 201), ("reference", "A\nB")]
        for field, value in cases:
            with self.subTest(field=field, value=value), self.assertRaises(ValidationError):
                parse_marketing_csv(csv_bytes([observation(**{field: value})]))
        with self.assertRaisesMessage(ValidationError, "setidaknya satu"):
            parse_marketing_csv(csv_bytes([observation(spend="", leads="")]))

    def test_limit_500_rows_and_one_megabyte(self):
        self.assertEqual(len(parse_marketing_csv(csv_bytes([observation(reference=str(index)) for index in range(500)]))), 500)
        with self.assertRaisesMessage(ValidationError, "500"):
            parse_marketing_csv(csv_bytes([observation(reference=str(index)) for index in range(501)]))
        with self.assertRaisesMessage(ValidationError, "1 MB"):
            parse_marketing_csv(b"x" * (MAX_CSV_BYTES + 1))

    def test_invalid_utf8_bad_quotes_control_bytes_and_extra_cells_are_rejected(self):
        for content in (b"\xff", marketing_csv_template() + b'"unterminated', marketing_csv_template() + b"\x00"):
            with self.subTest(content=content[:10]), self.assertRaises(ValidationError):
                parse_marketing_csv(content)
        with self.assertRaisesMessage(ValidationError, "jumlah kolom"):
            parse_marketing_csv(csv_bytes([["2026-09-08", "meta_ads"]]))
        with self.assertRaises(ValidationError):
            parse_marketing_csv("text is not uploaded bytes")

    def test_same_file_conflicts_rejected_but_identical_normalized_rows_retained(self):
        original = observation()
        with self.assertRaisesMessage(ValidationError, "dalam CSV"):
            parse_marketing_csv(csv_bytes([original, {**original, "spend": "125001"}]))
        rows = parse_marketing_csv(csv_bytes([original, {**original, "spend": "125000.5"}]))
        self.assertEqual(rows[0], rows[1])


class MarketingCSVImportTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Synthetic marketing import")
        self.other = Organization.objects.create(name="Other marketing import")
        self.users = {}
        for role in ("owner", "director", "finance", "marketing", "reviewer", "auditor"):
            actor = get_user_model().objects.create_user(username=f"marketing-import-{role}")
            Membership.objects.create(organization=self.org, user=actor, role=role)
            self.users[role] = actor
        self.actor = self.users["marketing"]

    def run_import(self, rows, *, actor=None, organization=None):
        return import_marketing_csv(organization=organization or self.org, actor=actor or self.actor, content=csv_bytes(rows))

    def test_import_calls_audited_service_and_never_creates_financial_records(self):
        with patch("director.services.create_marketing_observation", wraps=services.create_marketing_observation) as create:
            result = self.run_import([observation(), observation(channel="sales", reference="sales-day-001", basis="provisional")])
        self.assertEqual(result, {"created": 2, "skipped": 0})
        self.assertEqual(create.call_count, 2)
        self.assertEqual(MarketingObservation.objects.filter(organization=self.org).count(), 2)
        self.assertEqual(AuditEvent.objects.filter(organization=self.org, action="director.marketing.observation_created").count(), 2)
        item = MarketingObservation.objects.get(organization=self.org, channel="meta_ads")
        self.assertEqual(item.created_by, self.actor)
        self.assertIsNone(item.paid_orders)
        self.assertEqual(item.basis, "reported")
        for model in (Invoice, Bill, Journal, BankTransaction):
            self.assertFalse(model.objects.exists())

    def test_exact_upload_and_same_file_duplicates_skip_without_new_audits(self):
        original = observation()
        self.assertEqual(self.run_import([original, original]), {"created": 1, "skipped": 1})
        audit_count = AuditEvent.objects.count()
        self.assertEqual(self.run_import([original, original]), {"created": 0, "skipped": 2})
        self.assertEqual(MarketingObservation.objects.count(), 1)
        self.assertEqual(AuditEvent.objects.count(), audit_count)

    def test_conflict_against_existing_record_preflights_before_any_create(self):
        original = observation()
        self.run_import([original])
        audit_count = AuditEvent.objects.count()
        with patch("director.services.create_marketing_observation", wraps=services.create_marketing_observation) as create:
            with self.assertRaisesMessage(ValidationError, "Ganti pengamatan"):
                self.run_import([observation(reference="new-row"), {**original, "leads": "11"}])
        create.assert_not_called()
        self.assertEqual(MarketingObservation.objects.count(), 1)
        self.assertEqual(AuditEvent.objects.count(), audit_count)

    def test_null_to_zero_and_basis_change_are_conflicts_not_replays(self):
        original = observation()
        self.run_import([original])
        for change in ({"reported_value": "0"}, {"basis": "provisional"}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                self.run_import([{**original, **change}])

    def test_all_file_validation_happens_before_writes(self):
        for invalid in (observation(reference="bad", leads="NaN"), observation(reference="bad", source="x" * 201)):
            with patch("director.services.create_marketing_observation", wraps=services.create_marketing_observation) as create:
                with self.assertRaises(ValidationError):
                    self.run_import([observation(), invalid])
                create.assert_not_called()
        self.assertFalse(MarketingObservation.objects.exists())
        self.assertFalse(AuditEvent.objects.exists())

    def test_mid_creation_failure_rolls_back_records_and_audits(self):
        actual = services.create_marketing_observation
        call_count = 0

        def fail_second(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise ValidationError("Synthetic failure after preflight")
            return actual(**kwargs)

        with patch("director.services.create_marketing_observation", side_effect=fail_second):
            with self.assertRaises(ValidationError):
                self.run_import([observation(), observation(reference="second")])
        self.assertFalse(MarketingObservation.objects.exists())
        self.assertFalse(AuditEvent.objects.exists())

    def test_write_roles_match_marketing_service_including_skip_only_uploads(self):
        for role in ("owner", "director", "finance", "marketing"):
            self.assertEqual(self.run_import([observation(reference=role)], actor=self.users[role])["created"], 1)
        for role in ("reviewer", "auditor"):
            with self.subTest(role=role), self.assertRaises(PermissionDenied):
                self.run_import([observation(reference="owner")], actor=self.users[role])
        with self.assertRaises(PermissionDenied):
            import_marketing_csv(organization=self.org, actor=AnonymousUser(), content=b"invalid")
        self.actor.is_active = False
        self.actor.save(update_fields=["is_active"])
        with self.assertRaises(PermissionDenied):
            self.run_import([observation(reference="owner")])

    def test_organization_scope_and_raw_key_case_are_preserved(self):
        self.run_import([observation()])
        with self.assertRaises(PermissionDenied):
            self.run_import([observation()], organization=self.other)
        Membership.objects.create(organization=self.other, user=self.actor, role="marketing")
        self.assertEqual(self.run_import([observation()], organization=self.other), {"created": 1, "skipped": 0})
        self.assertEqual(self.run_import([observation(source="SYNTHETIC SOURCE")]), {"created": 1, "skipped": 0})
        self.assertEqual(MarketingObservation.objects.filter(organization=self.org).count(), 2)
        self.assertEqual(MarketingObservation.objects.filter(organization=self.other).count(), 1)

    def test_voided_reference_is_not_resurrected_and_corrected_active_record_can_repeat(self):
        original = observation()
        self.run_import([original])
        item = MarketingObservation.objects.get()
        services.void_marketing_observation(organization=self.org, actor=self.actor, observation=item, reason="Wrong source")
        with self.assertRaisesMessage(ValidationError, "dibatalkan"):
            self.run_import([original])
        replacement_source = observation(reference="correctable")
        self.run_import([replacement_source])
        item = MarketingObservation.objects.get(reference="correctable")
        corrected = {**replacement_source, "leads": "12"}
        typed = parse_marketing_csv(csv_bytes([corrected]))[0]
        services.replace_marketing_observation(organization=self.org, actor=self.actor, observation=item,
                                               reason="Source count corrected", **typed)
        self.assertEqual(self.run_import([corrected]), {"created": 0, "skipped": 1})
        with self.assertRaises(ValidationError):
            self.run_import([replacement_source])
