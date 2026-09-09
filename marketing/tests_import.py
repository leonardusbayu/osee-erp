import csv
import io
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import SimpleTestCase, TestCase

from core.models import AuditEvent, Membership, Organization
from .imports import CSV_COLUMNS, ad_csv_template, import_ad_csv, parse_ad_csv
from .models import AdDailyObservation
from .services import create_campaign


def csv_file(*rows):
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(CSV_COLUMNS)
    writer.writerows(rows)
    return output.getvalue().encode("utf-8-sig")


ROW = ["2026-09-01", "ITP-A", "", "", "150000.50", "5000", "125", "Laporan uji", "ADS-01"]


class AdCSVParserTests(SimpleTestCase):
    def test_unknown_and_zero_remain_distinct(self):
        data = ROW.copy()
        data[4:7] = ["", "0", ""]
        parsed = parse_ad_csv(csv_file(data))[0]
        self.assertIsNone(parsed["spend"])
        self.assertEqual(parsed["impressions"], 0)
        self.assertIsNone(parsed["clicks"])
        self.assertEqual(parsed["audience"], "Semua audiens")

    def test_empty_template_and_malformed_values_rejected(self):
        for body in (ad_csv_template(), b"bad,header\n", b"\xff", b"x" * (1024 * 1024 + 1)):
            with self.subTest(body=body[:20]), self.assertRaises(ValidationError):
                parse_ad_csv(body)
        for column, value in ((0, "2026-02-31"), (4, "NaN"), (4, "-1"), (4, "1e6"), (5, "2.5"), (6, "2147483648")):
            data = ROW.copy()
            data[column] = value
            with self.subTest(column=column, value=value), self.assertRaises(ValidationError):
                parse_ad_csv(csv_file(data))

    def test_blank_export_rows_are_ignored_and_data_row_limit_is_enforced(self):
        data = csv_file([], ROW, [""] * len(CSV_COLUMNS), [])
        self.assertEqual(len(parse_ad_csv(data)), 1)
        with self.assertRaises(ValidationError):
            parse_ad_csv(csv_file(*([ROW] * 501)))


class AdCSVImportTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Marketing CSV test")
        self.actor = get_user_model().objects.create_user(username="csv-marketer")
        Membership.objects.create(organization=self.org, user=self.actor, role="marketing")
        self.campaign = create_campaign(organization=self.org, actor=self.actor, name="Kampanye uji", channel="meta_ads")

    def ingest(self, *rows, **kwargs):
        return import_ad_csv(organization=self.org, actor=self.actor, campaign=kwargs.get("campaign", self.campaign), content=csv_file(*rows))

    def test_replay_is_idempotent_and_changed_replay_is_rejected(self):
        self.assertEqual(self.ingest(ROW), {"created": 1, "duplicates": 0, "rows": 1})
        self.assertEqual(self.ingest(ROW), {"created": 0, "duplicates": 1, "rows": 1})
        changed = ROW.copy()
        changed[4] = "200000"
        with self.assertRaises(ValidationError):
            self.ingest(changed)
        self.assertEqual(AdDailyObservation.objects.get().spend, Decimal("150000.50"))

    def test_conflicting_second_row_rolls_back_records_and_audit(self):
        before_audits = AuditEvent.objects.count()
        conflicting = ROW.copy()
        conflicting[4] = "90000"
        with self.assertRaises(ValidationError):
            self.ingest(ROW, conflicting)
        self.assertFalse(AdDailyObservation.objects.exists())
        self.assertEqual(AuditEvent.objects.count(), before_audits)

    def test_overlapping_total_cannot_double_spend(self):
        total = ROW.copy()
        total[1], total[8] = "", "ADS-TOTAL"
        with self.assertRaises(ValidationError):
            self.ingest(ROW, total)
        self.assertFalse(AdDailyObservation.objects.exists())

    def test_foreign_campaign_and_auditor_cannot_import(self):
        other = Organization.objects.create(name="Foreign marketing")
        owner = get_user_model().objects.create_user(username="foreign-csv-owner")
        Membership.objects.create(organization=other, user=owner, role="owner")
        campaign = create_campaign(organization=other, actor=owner, name="Foreign", channel="seo")
        with self.assertRaises(ValidationError):
            self.ingest(ROW, campaign=campaign)
        membership = Membership.objects.get(organization=self.org, user=self.actor)
        membership.role = "auditor"
        membership.save()
        with self.assertRaises(PermissionDenied):
            self.ingest(ROW)
