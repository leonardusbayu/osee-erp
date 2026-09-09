import hashlib
import json
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError
from django.test import TestCase, override_settings

from core.models import AuditEvent, Membership, Organization
from finance.models import BankTransaction, Bill, Invoice, Journal, Party, PriceVersion
from .models import ImportBatch, ImportException, MonthlySnapshot, SourceDocument
from .services import import_bundle, import_summary, month_data


class SourceImportTests(TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory(prefix="osee-source-import-test-")
        self.base = Path(self.directory.name)
        self.root = self.base / "bundle"
        self.root.mkdir()
        self.storage = self.base / "private"
        self.settings_override = override_settings(MEDIA_ROOT=self.storage)
        self.settings_override.enable()
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.settings_override.disable)
        self.org = Organization.objects.create(name="Synthetic source company")
        self.other = Organization.objects.create(name="Other source company")
        self.owner = get_user_model().objects.create_user(username="source-owner")
        Membership.objects.create(organization=self.org, user=self.owner, role="owner")
        source = b"%PDF-1.7\nSynthetic recap only\n%%EOF"
        (self.root / "recap.pdf").write_bytes(source)
        self.bundle = {
            "schema_version": "1.0",
            "sources": [{"id": "recap", "kind": "operating_recap", "relative_path": "recap.pdf",
                         "original_filename": "synthetic recap.pdf", "sha256": hashlib.sha256(source).hexdigest(), "page_count": 2}],
            "months": [{"id": "jan", "source_id": "recap", "source_period": "2026-01", "page": 1,
                        "printed_count": 2, "printed_amount": 100, "detail_count": 3, "format_controls": [],
                        "coverage": "source_page_only_completeness_unconfirmed", "locator": {"page": 1}}],
            "channels": [{"id": "col-1", "month_id": "jan", "source_label": "RAW LABEL", "raw_header": "RAW LABEL",
                          "header_price": None, "printed_count": 2, "printed_amount": None,
                          "legacy_contributions": [], "locator": {"page": 1, "column_index": 1, "x0": 40.5}}],
            "details": [{"id": "row-1", "month_id": "jan", "channel_id": "col-1", "original_date_text": "05-Jan-25",
                         "parsed_source_date": "2025-01-05", "proposed_context_date": "2026-01-05", "quantity": 3,
                         "year_conflict": True, "locator": {"page": 1, "row_index": 3, "column_index": 1}}],
            "exceptions": [{"id": "year-1", "code": "date_year_conflict", "severity": "review_required", "status": "unresolved",
                            "month_id": "jan", "channel_id": "col-1", "detail_ids": ["row-1"],
                            "observed": {"raw_date": "05-Jan-25"}, "expected": {"context_year": 2026},
                            "explanation": "Synthetic year conflict; original value retained."}],
            "annual_summary": {"page": 2, "covered_periods": ["2026-01"], "missing_periods": [f"2026-{month:02d}" for month in range(2, 13)],
                               "rows": [], "monthly_controls": [], "printed_total": 2, "coverage": "partial"},
            "supplier_invoice": None,
        }

    def write_bundle(self, bundle=None):
        path = self.root / "bundle.json"
        path.write_text(json.dumps(self.bundle if bundle is None else bundle), encoding="utf-8")
        return path

    def run_import(self, bundle=None, **kwargs):
        return import_bundle(organization=self.org, actor=self.owner, bundle_path=self.write_bundle(bundle), **kwargs)

    def stored_files(self):
        return [path for path in self.storage.rglob("*") if path.is_file()]

    def add_supplier(self):
        data = b"%PDF-1.7\nSynthetic supplier only\n%%EOF"
        (self.root / "supplier.pdf").write_bytes(data)
        self.bundle["sources"].append({"id": "supplier", "kind": "supplier_invoice", "relative_path": "supplier.pdf",
                                       "original_filename": "synthetic supplier.pdf", "sha256": hashlib.sha256(data).hexdigest(), "page_count": 1})
        self.bundle["supplier_invoice"] = {"source_id": "supplier", "facts": {
            "invoice_number": "SYNTHETIC-1", "supplier_name": "Synthetic provider", "invoice_date": "2026-05-04",
            "service_date": "2026-05-02", "quantity": 1, "unit_price": 100, "base_amount": 100,
            "vat_amount": 11, "total_amount": 111, "currency": "IDR", "payment_status": "unconfirmed",
        }, "approval_status": "source_evidence_only", "candidate_match": {}}

    def test_source_archive_preserves_nulls_dates_and_exceptions_without_posting(self):
        batch = self.run_import()
        self.assertEqual(batch.payload, self.bundle)
        detail = month_data(organization=self.org, source_period="2026-01")
        self.assertIsNone(detail["channels"][0]["header_price"])
        self.assertIsNone(detail["channels"][0]["printed_amount"])
        self.assertEqual(detail["details"][0]["parsed_source_date"], "2025-01-05")
        self.assertEqual(detail["details"][0]["proposed_context_date"], "2026-01-05")
        self.assertEqual(detail["details"][0]["original_date_text"], "05-Jan-25")
        exception = detail["exceptions"].get()
        self.assertEqual((exception.code, exception.status), ("date_year_conflict", "open"))
        self.assertEqual(exception.payload["status"], "unresolved")
        for model in (Invoice, Bill, BankTransaction, Journal, PriceVersion, Party):
            self.assertFalse(model.objects.exists(), model.__name__)
        self.assertEqual(len(self.stored_files()), 1)

    def test_same_bundle_is_idempotent_and_conflicting_source_hash_is_rejected(self):
        first = self.run_import()
        repeated = self.run_import()
        self.assertEqual(first.pk, repeated.pk)
        changed = deepcopy(self.bundle)
        changed["months"][0]["printed_amount"] = 101
        with self.assertRaisesMessage(ValidationError, "berbeda"):
            self.run_import(changed)
        self.assertEqual(ImportBatch.objects.count(), 1)
        self.assertEqual(MonthlySnapshot.objects.get().printed_amount, 100)
        self.assertEqual(len(self.stored_files()), 1)
        self.assertEqual(AuditEvent.objects.filter(action="imports.source_bundle.imported").count(), 1)

    def test_summary_distinguishes_printed_controls_from_detail_and_missing_months(self):
        self.run_import()
        summary = import_summary(organization=self.org)
        self.assertEqual(summary["reported_amount"], 100)
        self.assertEqual(summary["printed_count"], 2)
        self.assertEqual(summary["detail_count"], 3)
        self.assertEqual(summary["detail_rows"], 1)
        self.assertEqual(summary["channel_count"], 1)
        self.assertEqual(summary["annual_printed_count"], 2)
        self.assertEqual(summary["covered_months"], ["2026-01"])
        self.assertEqual(len(summary["missing_months"]), 11)
        self.assertEqual(summary["exception_count"], 1)

    def test_import_scope_is_required_and_read_helpers_cannot_cross_organizations(self):
        self.run_import()
        with self.assertRaises(PermissionDenied):
            import_bundle(organization=self.other, actor=self.owner, bundle_path=self.write_bundle())
        with self.assertRaises(ValidationError):
            month_data(organization=self.other, source_period="2026-01")
        self.assertEqual(import_summary(organization=self.other)["covered_months"], [])
        other_batch = import_bundle(organization=self.other, bundle_path=self.write_bundle())
        self.assertEqual(other_batch.organization_id, self.other.pk)
        self.assertEqual(self.org.import_batches.count(), 1)
        self.assertEqual(import_summary(organization=self.other)["reported_amount"], 100)
        source = SourceDocument.objects.filter(batch=other_batch).get()
        self.assertTrue(source.file.name.startswith(f"organizations/{self.other.pk}/"))

    def test_invalid_types_references_and_source_files_are_rejected_atomically(self):
        cases = []
        altered = deepcopy(self.bundle); altered["details"][0]["quantity"] = True; cases.append(altered)
        altered = deepcopy(self.bundle); altered["channels"][0]["header_price"] = 1.5; cases.append(altered)
        altered = deepcopy(self.bundle); altered["details"][0]["channel_id"] = "missing"; cases.append(altered)
        altered = deepcopy(self.bundle); altered["sources"][0]["sha256"] = "0" * 64; cases.append(altered)
        altered = deepcopy(self.bundle); altered["sources"][0]["relative_path"] = "../outside.pdf"; cases.append(altered)
        altered = deepcopy(self.bundle); altered["months"].append(deepcopy(altered["months"][0])); cases.append(altered)
        for bundle in cases:
            with self.subTest(bundle=bundle), self.assertRaises(ValidationError):
                self.run_import(bundle)
        self.assertFalse(ImportBatch.objects.exists())
        self.assertEqual(self.stored_files(), [])

    def test_supplier_source_creates_only_exact_gross_unreviewed_draft(self):
        self.add_supplier()
        batch = self.run_import()
        bill = batch.supplier_bill
        self.assertEqual(bill.number, "SYNTHETIC-1")
        self.assertEqual((bill.amount, bill.supplier_vat), (111, 11))
        self.assertEqual((bill.status, bill.tax_status, bill.tax_amount), ("draft", "review", None))
        self.assertEqual(bill.service_date.isoformat(), "2026-05-02")
        self.assertFalse(Journal.objects.exists())
        self.assertFalse(Invoice.objects.exists())
        self.assertFalse(BankTransaction.objects.exists())
        self.assertEqual(self.run_import().supplier_bill_id, bill.pk)
        self.assertEqual(Bill.objects.count(), 1)

    def test_database_failure_removes_new_copies_and_rolls_back_draft_bill(self):
        self.add_supplier()
        with patch("imports.services.record", side_effect=IntegrityError("synthetic commit failure")):
            with self.assertRaises(IntegrityError):
                self.run_import()
        for model in (ImportBatch, SourceDocument, MonthlySnapshot, ImportException, Bill, Party):
            self.assertFalse(model.objects.exists(), model.__name__)
        self.assertEqual(self.stored_files(), [])

    def test_archive_cannot_be_changed_deleted_or_overlapped_by_new_source(self):
        batch = self.run_import()
        batch.payload = {"changed": True}
        with self.assertRaises(ValidationError):
            batch.save()
        with self.assertRaises(ValidationError):
            batch.delete()
        modified_source = b"%PDF-1.7\nA different recap for the same period\n%%EOF"
        (self.root / "recap.pdf").write_bytes(modified_source)
        self.bundle["sources"][0]["sha256"] = hashlib.sha256(modified_source).hexdigest()
        with self.assertRaisesMessage(ValidationError, "tumpang tindih"):
            self.run_import()
        self.assertEqual(ImportBatch.objects.count(), 1)
        self.assertEqual(len(self.stored_files()), 1)

    def test_merged_date_cell_remains_ambiguous_without_an_assigned_service_date(self):
        row = self.bundle["details"][0]
        row.update({"original_date_text": "05-Jan-26 / 06-Jan-26", "parsed_source_date": None,
                    "proposed_context_date": None, "year_conflict": False, "merged_date_cell": True,
                    "date_ambiguous": True, "source_date_candidates": ["05-Jan-26", "06-Jan-26"],
                    "date_status": "merged_date_cell_unresolved"})
        self.run_import()
        imported = month_data(organization=self.org, source_period="2026-01")["details"][0]
        self.assertEqual(imported["source_date_candidates"], ["05-Jan-26", "06-Jan-26"])
        self.assertIsNone(imported["parsed_source_date"])
        self.assertIsNone(imported["proposed_context_date"])
        self.assertEqual(imported["quantity"], 3)

    def test_summary_unknown_values_remain_null_instead_of_becoming_zero(self):
        month = self.bundle["months"][0]
        month.update({"printed_amount": None, "printed_count": None, "detail_count": None})
        self.bundle["annual_summary"]["printed_total"] = None
        self.run_import()
        summary = import_summary(organization=self.org)
        for field in ("reported_amount", "printed_count", "detail_count", "annual_printed_count"):
            self.assertIsNone(summary[field], field)
        empty = import_summary(organization=self.other)
        self.assertIsNone(empty["reported_amount"])
        self.assertIsNone(empty["printed_count"])
