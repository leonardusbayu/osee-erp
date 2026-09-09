import json
from datetime import date, datetime, timezone as datetime_timezone
from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings

from core.models import Organization
from finance.models import Journal, JournalLine
from finance.services import post_journal
from imports.models import ImportBatch, ImportException, MonthlySnapshot

from .analytics import dashboard_snapshot
from .calculators import budget_position, cash_forecast, price_cost_scenario


class CalculatorTests(SimpleTestCase):
    def test_price_change_and_new_provider_cost_use_explicit_quantity(self):
        result = price_cost_scenario(quantity=100, current_price="530000", current_cost="444000",
                                     variable_cost="10000", new_price="550000", new_cost="460000",
                                     new_variable_cost="15000", new_quantity=120, additional_marketing_spend="1000000")
        self.assertEqual(result["baseline"]["unit_contribution"], "76000.00")
        self.assertEqual(result["baseline"]["net_contribution"], "7600000.00")
        self.assertEqual(result["proposed"]["unit_contribution"], "75000.00")
        self.assertEqual(result["proposed"]["net_contribution"], "8000000.00")
        self.assertEqual(result["delta"]["net_contribution"], "400000.00")
        self.assertEqual(result["break_even_quantity_to_match_baseline"], 115)
        self.assertEqual([row["quantity"] for row in result["sensitivity"]], [96, 120, 144])
        self.assertEqual(result["basis"], "assumed")
        json.dumps(result, allow_nan=False)

    def test_same_scenario_is_not_a_forecast_and_does_not_invent_overhead(self):
        result = price_cost_scenario(quantity=50, current_price="500000", current_cost="444000")
        self.assertEqual(result["baseline"], result["proposed"])
        self.assertEqual(result["delta"]["net_contribution"], "0.00")
        self.assertNotIn("profit", result["baseline"])
        self.assertNotIn("forecast", result)

    def test_zero_margin_cannot_produce_fictional_break_even(self):
        result = price_cost_scenario(quantity=10, current_price=100, current_cost=90, new_cost=100)
        self.assertIsNone(result["break_even_quantity_to_match_baseline"])
        zero = price_cost_scenario(quantity=0, current_price=0, current_cost=0)
        self.assertIsNone(zero["baseline"]["margin_pct"])

    def test_invalid_or_missing_inputs_are_not_silently_zero(self):
        for invalid in (None, True, 1.25, "NaN", "Infinity", "-1", "0.001"):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                price_cost_scenario(quantity=10, current_price=100, current_cost=invalid)
        with self.assertRaises(ValidationError):
            price_cost_scenario(quantity="1.5", current_price=100, current_cost=90)
        with self.assertRaises(ValidationError):
            cash_forecast(None, "2026-01-01", 0, [])

    def test_decimal_cent_precision_survives_large_quantity(self):
        result = price_cost_scenario(quantity=1000000000, current_price="999999999999999999.99",
                                     current_cost="999999999999999999.98", new_price="999999999999999999.98")
        self.assertEqual(result["baseline"]["unit_contribution"], "0.01")
        self.assertEqual(result["delta"]["revenue"], "-10000000.00")

    def test_cash_plan_does_not_recount_receipts_before_opening_or_cancelled_lines(self):
        result = cash_forecast("1000", "2026-09-08", "100", [
            {"id": 1, "date": "2026-09-07", "direction": "inflow", "amount": "500"},
            {"id": 2, "date": "2026-09-08", "direction": "outflow", "amount": "200"},
            {"id": 3, "date": "2026-09-09", "direction": "inflow", "amount": "900", "status": "void"},
            {"id": 4, "date": "2026-12-08", "direction": "inflow", "amount": "100"},
        ])
        self.assertEqual(result["closing_balance"], "800.00")
        self.assertEqual(result["total_inflows"], "0.00")
        self.assertEqual(result["excluded_line_count"], 3)
        self.assertEqual(len(result["weeks"]), 13)
        self.assertEqual(len(result["daily"]), 14)
        self.assertEqual(result["horizon_end"], "2026-12-07")
        self.assertFalse(result["verified"])
        self.assertIsNone(result["safe_to_spend"])
        self.assertEqual(result["basis"], "assumed")
        json.dumps(result, allow_nan=False)

    def test_cash_minimum_catches_intraweek_shortfall_even_after_recovery(self):
        result = cash_forecast("1000", "2026-09-08", "200", [
            {"date": "2026-09-23", "direction": "outflow", "amount": "900"},
            {"date": "2026-09-25", "direction": "inflow", "amount": "1200"},
        ])
        self.assertEqual(result["minimum_balance"], "100.00")
        self.assertEqual(result["first_shortfall_date"], "2026-09-23")
        self.assertEqual(result["weeks"][2]["closing_balance"], "1300.00")
        self.assertTrue(result["weeks"][2]["below_reserve"])
        self.assertEqual(result["weeks"][2]["minimum_balance"], "100.00")

    def test_cash_empty_lines_are_explicit_assumption_and_include_initial_shortfall(self):
        result = cash_forecast(0, date(2026, 9, 8), 100, [])
        self.assertEqual(result["closing_balance"], "0.00")
        self.assertEqual(result["first_shortfall_date"], "2026-09-08")
        self.assertFalse(result["verified"])

    def test_cash_duplicate_ids_rejected_but_separate_installments_retained(self):
        line = {"id": 1, "date": "2026-09-08", "direction": "inflow", "amount": "100", "source_reference": "INV-A"}
        with self.assertRaises(ValidationError):
            cash_forecast(0, "2026-09-08", 0, [line, dict(line)])
        result = cash_forecast(0, "2026-09-08", 0, [line, {**line, "id": 2}])
        self.assertEqual(result["total_inflows"], "200.00")

    @override_settings(TIME_ZONE="America/New_York")
    def test_cash_dates_are_wib_and_date_only_inputs_do_not_drift(self):
        # 17:30 UTC is already the next day in Indonesia.
        instant = datetime(2026, 9, 7, 17, 30, tzinfo=datetime_timezone.utc)
        result = cash_forecast(0, instant, 0, [{"date": date(2026, 9, 8), "direction": "inflow", "amount": "10"}])
        self.assertEqual(result["as_of"], "2026-09-08")
        self.assertEqual(result["daily"][0]["closing_balance"], "10.00")
        with self.assertRaises(ValidationError):
            cash_forecast(0, datetime(2026, 9, 8), 0, [])
        with self.assertRaises(ValidationError):
            cash_forecast(0, "20260908", 0, [])

    def test_actual_receipt_is_not_accepted_as_a_planned_line(self):
        with self.assertRaises(ValidationError):
            cash_forecast(0, "2026-09-08", 0, [{"date": "2026-09-08", "direction": "inflow", "amount": "10", "status": "settled"}])

    def test_budget_payment_does_not_consume_again_and_voided_commitment_is_excluded(self):
        result = budget_position({"amount": "1000", "status": "approved"}, [
            {"id": 1, "kind": "open_commitment", "amount": "400", "voided_at": "2026-09-08"},
            {"id": 2, "kind": "incurred", "amount": "400"},
            {"id": 3, "kind": "payment", "amount": "400"},
            {"id": 4, "kind": "open_commitment", "amount": "200"},
            {"id": 5, "kind": "reservation", "amount": "100"},
        ])
        self.assertEqual(result["consumed"], "700.00")
        self.assertEqual(result["payments"], "400.00")
        self.assertEqual(result["available"], "300.00")
        self.assertEqual(result["utilization_pct"], "70.00")
        self.assertFalse(result["over_budget"])

    def test_over_budget_is_negative_not_clamped_to_zero_and_zero_budget_ratio_unknown(self):
        result = budget_position(100, [{"kind": "incurred", "amount": 110}])
        self.assertEqual(result["available"], "-10.00")
        self.assertTrue(result["over_budget"])
        self.assertIsNone(budget_position(0, [])["utilization_pct"])
        with self.assertRaises(ValidationError):
            budget_position(None, [])


class DirectorAnalyticsTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Synthetic Director Company")
        self.other = Organization.objects.create(name="Other Director Company")

    def archive_record(self, model, **kwargs):
        item = model(organization=self.org, **kwargs)
        item._import_creation = True
        item.save()
        return item

    def source(self):
        rows = [("2026-01", 100, "50000000", 100, "source_page_only_completeness_unconfirmed"),
                ("2026-02", 120, "61000000", 121, "source_page_only_completeness_unconfirmed"),
                ("2026-03", 3, "1500000", 3, "partial_source_page")]
        payload = {"channels": [
            {"id": "c1", "month_id": "2026-01", "source_label": "Partner A", "printed_count": 100, "printed_amount": 50000000, "detail_count": 100},
            {"id": "c2", "month_id": "2026-02", "source_label": "PARTNER A", "printed_count": 120, "printed_amount": None, "detail_count": 121},
            {"id": "c3", "month_id": "2026-03", "source_label": "Partner A", "printed_count": 3, "printed_amount": 1500000, "detail_count": 3},
        ], "details": [
            {"id": "d1", "month_id": "2026-01", "channel_id": "c1", "quantity": 100,
             "original_date_text": "05-Jan-25", "parsed_source_date": "2025-01-05", "year_conflict": True},
            {"id": "d2", "month_id": "2026-02", "channel_id": "c2", "quantity": 121,
             "merged_date_cell": True, "parsed_source_date": None, "source_date_candidates": ["2026-02-01", "2026-02-02"]},
            {"id": "d3", "month_id": "2026-03", "channel_id": "c3", "quantity": 3},
        ], "annual_summary": {"page": 4, "printed_total": 220, "covered_periods": ["2026-01", "2026-02"], "coverage": "partial"}}
        batch = self.archive_record(ImportBatch, source_hash="a" * 64, bundle_hash="b" * 64, payload=payload)
        for index, (period, count, amount, detail, coverage) in enumerate(rows, 1):
            self.archive_record(MonthlySnapshot, batch=batch, source_id=period, source_period=period,
                                year=2026, month=index, source_page=index, printed_count=count,
                                printed_amount=amount, detail_count=detail, coverage=coverage, payload={"id": period})
        self.archive_record(ImportException, batch=batch, exception_id="qty-1", code="detail_count_mismatch",
                            severity="review_required", message="Total tercetak 120; rincian 121.", month_source_id="2026-02", payload={"month_id": "2026-02"})
        return batch

    def post(self, org, key, entries, posting_date="2026-01-15"):
        return post_journal(organization=org, date=posting_date, source_key=key, description="Synthetic evidence", entries=entries)

    def test_empty_workspace_preserves_unknown_financial_values(self):
        result = dashboard_snapshot(organization=self.org)
        self.assertFalse(result["source"]["available"])
        self.assertIsNone(result["source"]["reported_amount"])
        self.assertIsNone(result["source"]["printed_count"])
        self.assertEqual(result["source"]["month_count"], 0)
        self.assertFalse(result["finance"]["posted_available"])
        for key in ("revenue", "expenses", "profit", "bank_balance"):
            self.assertIsNone(result["finance"][key])
        self.assertEqual(len(result["monthly_series"]), 12)
        self.assertTrue(all(row["printed_count"] is None for row in result["monthly_series"]))
        self.assertTrue(all(row["change_pct"] is None for row in result["monthly_series"]))
        json.dumps(result, allow_nan=False)

    def test_source_only_totals_are_not_financial_and_annual_is_not_added(self):
        self.source()
        result = dashboard_snapshot(organization=self.org)
        self.assertEqual(result["source"]["reported_amount"], "112500000.00")
        self.assertEqual(result["source"]["printed_count"], 223)
        self.assertEqual(result["source"]["detail_count"], 224)
        self.assertEqual(result["source"]["annual_printed_count"], 220)
        self.assertEqual(result["source"]["detail_cell_count"], 3)
        self.assertEqual(result["source"]["raw_label_count"], 2)
        self.assertEqual(result["source"]["month_count"], 3)
        self.assertEqual(result["source"]["partial_months"], ["2026-03"])
        self.assertEqual(len(result["source"]["missing_months"]), 9)
        self.assertEqual(result["source"]["exception_count"], 1)
        self.assertFalse(result["finance"]["posted_available"])
        self.assertIsNone(result["finance"]["revenue"])
        self.assertIsNone(result["finance"]["bank_balance"])
        self.assertFalse(Journal.objects.exists())

    def test_raw_aliases_unknown_subtotals_and_ambiguous_date_counts_are_preserved(self):
        batch = self.source()
        original_payload = batch.payload
        result = dashboard_snapshot(organization=self.org)
        partners = {row["source_label"]: row for row in result["partners"]}
        self.assertEqual(set(partners), {"Partner A", "PARTNER A"})
        self.assertEqual(partners["Partner A"]["printed_count"], 103)
        self.assertEqual(partners["PARTNER A"]["detail_count"], 121)
        self.assertIsNone(partners["PARTNER A"]["reported_amount"])
        self.assertEqual(partners["PARTNER A"]["unknown_amount_count"], 1)
        batch.refresh_from_db()
        self.assertEqual(batch.payload, original_payload)
        self.assertEqual(result["monthly_series"][0]["detail_count"], 100)
        self.assertEqual(result["monthly_series"][1]["detail_count"], 121)

    def test_monthly_change_only_compares_adjacent_nonpartial_source_pages(self):
        self.source()
        result = dashboard_snapshot(organization=self.org)
        self.assertEqual(result["monthly_series"][1]["change_pct"], "20.00")
        self.assertIsNone(result["monthly_series"][2]["change_pct"])
        self.assertIn("sebagian", result["monthly_series"][2]["change_reason"])
        self.assertIsNone(result["monthly_series"][3]["printed_count"])
        self.assertIsNone(result["monthly_series"][3]["change_pct"])
        self.assertNotIn("daily", result)

    def test_finance_uses_posted_year_and_org_only_and_bank_movement_is_not_balance(self):
        self.post(self.org, "revenue", [("AR", 100, 0), ("REVENUE", 0, 100)])
        self.post(self.org, "expense", [("EXPENSE", 40, 0), ("AP", 0, 40)])
        self.post(self.org, "receipt", [("BANK", 100, 0), ("AR", 0, 100)])
        self.post(self.org, "other-year", [("AR", 900, 0), ("REVENUE", 0, 900)], "2025-12-31")
        self.post(self.other, "other-org", [("AR", 999, 0), ("REVENUE", 0, 999)])
        draft = Journal.objects.create(organization=self.org, date="2026-01-15", source_key="draft", description="Unposted")
        JournalLine.objects.create(organization=self.org, journal=draft, account="REVENUE", credit=10000)
        result = dashboard_snapshot(organization=self.org)
        self.assertEqual(result["finance"]["posting_count"], 3)
        self.assertEqual(result["finance"]["revenue"], "100.00")
        self.assertEqual(result["finance"]["expenses"], "40.00")
        self.assertEqual(result["finance"]["profit"], "60.00")
        self.assertIsNone(result["finance"]["bank_balance"])
        self.assertFalse(result["source"]["available"])

    def test_unobserved_expenses_are_unknown_even_when_revenue_exists(self):
        self.post(self.org, "revenue", [("AR", 100, 0), ("REVENUE", 0, 100)])
        result = dashboard_snapshot(organization=self.org)
        self.assertEqual(result["finance"]["revenue"], "100.00")
        self.assertIsNone(result["finance"]["expenses"])
        self.assertIsNone(result["finance"]["profit"])

    def test_observed_zero_net_revenue_is_zero_not_unknown(self):
        self.post(self.org, "revenue", [("AR", 100, 0), ("REVENUE", 0, 100)])
        self.post(self.org, "reversal", [("REVENUE", 100, 0), ("AR", 0, 100)])
        result = dashboard_snapshot(organization=self.org)
        self.assertEqual(result["finance"]["revenue"], "0.00")
        self.assertIsNone(result["finance"]["expenses"])

    def test_source_is_organization_and_year_scoped(self):
        self.source()
        self.assertFalse(dashboard_snapshot(organization=self.other)["source"]["available"])
        self.assertFalse(dashboard_snapshot(organization=self.org, year=2025)["source"]["available"])

    @override_settings(TIME_ZONE="America/New_York")
    def test_fact_registry_is_json_safe_has_local_provenance_no_raw_names_and_wib_as_of(self):
        self.source()
        with patch("django.utils.timezone.now", return_value=datetime(2026, 9, 7, 17, 30, tzinfo=datetime_timezone.utc)):
            result = dashboard_snapshot(organization=self.org)
        self.assertEqual(result["as_of"], "2026-09-08")
        facts = {row["fact_id"]: row for row in result["facts"]}
        self.assertEqual(len(facts), len(result["facts"]))
        self.assertEqual(facts["source.printed_count"]["value"], 223)
        self.assertIsNone(facts["finance.bank_balance"]["value"])
        self.assertIsNone(facts["source.month.2026-12.reported_amount"]["value"])
        self.assertIn("2026-01", facts["source.annual_printed_count"]["coverage"])
        for item in facts.values():
            self.assertTrue(item["source_url"].startswith("/"))
            self.assertIn("basis", item)
            self.assertIn("coverage", item)
        encoded = json.dumps(result["facts"], allow_nan=False)
        self.assertNotIn("Partner A", encoded)
        self.assertNotIn(self.org.name, encoded)
        self.assertNotIn("2025-01-05", encoded)

    def test_year_and_unsaved_organization_validation(self):
        for invalid in (True, "2026", 2026.0, 1999, 2201):
            with self.subTest(year=invalid), self.assertRaises(ValidationError):
                dashboard_snapshot(organization=self.org, year=invalid)
        with self.assertRaises(ValidationError):
            dashboard_snapshot(organization=Organization(name="Not saved"))
