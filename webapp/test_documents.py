"""Export integrity and isolation checks without an extra PDF-reader dependency."""

import base64
import re
import zlib
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from django.test import SimpleTestCase

from core.models import Organization
from finance.models import Party, Product

from .documents import invoice_pdf, monthly_report_pdf


def pdf_text_commands(data):
    """Decode ReportLab page streams for assertions; visual QA remains separate."""
    decoded = []
    for match in re.finditer(rb"stream\r?\n(.*?)endstream", data, re.DOTALL):
        stream = match.group(1).strip()
        try:
            stream = base64.a85decode(stream, adobe=True)
            stream = zlib.decompress(stream)
        except (ValueError, zlib.error):
            continue
        decoded.append(stream.decode("latin-1"))
    commands = "\n".join(decoded)
    # Entity-safe Paragraph fragments may become adjacent Tj commands.
    literal_text = "".join(re.findall(r"\(((?:\\.|[^\\()])*)\)\s*Tj", commands))
    return commands + "\n" + literal_text


class DocumentExportTests(SimpleTestCase):
    def setUp(self):
        self.organization = SimpleNamespace(pk=71, name="PT Langkah Pintar Nusantara", brand_name="OSEE", vat_status="non_pkp", is_demo=False)
        self.invoice = SimpleNamespace(
            organization=self.organization,
            party=SimpleNamespace(organization_id=71, name="Mitra <b>Utama</b> & Rekan"),
            product=SimpleNamespace(organization_id=71, name="TOEFL ITP"),
            number="INV-2026-0017", quantity=2, unit_price=Decimal("510000.00"),
            date=date(2026, 9, 1), due_date=date(2026, 9, 15), service_date=date(2026, 9, 10),
            status="issued", total=Decimal("1020000.00"), paid_amount=Decimal("500000.00"), outstanding_amount=Decimal("520000.00"),
        )
        self.summary = {
            "organization_id": 71, "year": 2026, "month": 9,
            "start": date(2026, 9, 1), "end": date(2026, 9, 30),
            "revenue": Decimal("1020000.00"), "expenses": Decimal("430000.00"), "profit": Decimal("590000.00"),
            "balances": {"BANK": Decimal("590000.00"), "REVENUE": Decimal("-1020000.00"), "EXPENSE": Decimal("430000.00")},
            "closed": False, "blockers": ["2 mutasi belum dicocokkan"],
        }

    def assert_valid_pdf(self, content):
        self.assertIsInstance(content, bytes)
        self.assertTrue(content.startswith(b"%PDF-"))
        self.assertTrue(content.rstrip().endswith(b"%%EOF"))
        self.assertNotIn(b"/JavaScript", content)
        return pdf_text_commands(content)

    def test_invoice_has_snapshot_amounts_and_escaped_names(self):
        text = self.assert_valid_pdf(invoice_pdf(self.invoice))
        for value in ("INV-2026-0017", "Rp1.020.000,00", "Rp500.000,00", "Rp520.000,00", "Mitra <b>Utama</b> & Rekan", "bukan Faktur Pajak", "bukan penetapan status historis"):
            self.assertIn(value, text)
        self.assertNotIn("DEMO - DATA UJI", text)

    def test_invoice_rejects_cross_organization_references(self):
        for related in (self.invoice.party, self.invoice.product):
            related.organization_id = 99
            with self.assertRaises(ValueError):
                invoice_pdf(self.invoice)
            related.organization_id = 71

    def test_invoice_draft_and_demo_are_visible(self):
        self.organization.is_demo = True
        self.invoice.status = "draft"
        text = self.assert_valid_pdf(invoice_pdf(self.invoice))
        self.assertIn("DEMO - DATA UJI", text)
        self.assertIn("invoice belum diterbitkan", text)

    def test_monthly_draft_totals_and_blockers_are_explicit(self):
        text = self.assert_valid_pdf(monthly_report_pdf(self.organization, self.summary))
        for value in ("DRAF - PERIODE BELUM DITUTUP", "Rp590.000,00", "Rp1.020.000,00", "2 mutasi belum dicocokkan", "Total debit dan kredit seimbang", "bukan perhitungan dasar pajak", "30 September 2026"):
            self.assertIn(value, text)

    def test_monthly_scope_mismatch_is_rejected(self):
        self.summary["organization_id"] = 99
        with self.assertRaises(ValueError):
            monthly_report_pdf(self.organization, self.summary)

    def test_unbalanced_trial_balance_is_never_labelled_balanced(self):
        self.summary["balances"]["BANK"] += Decimal("0.01")
        text = self.assert_valid_pdf(monthly_report_pdf(self.organization, self.summary))
        self.assertIn("neraca saldo belum seimbang", text)
        self.assertIn("Rp0,01", text)
        self.assertNotIn("Total debit dan kredit seimbang", text)

    def test_closed_report_and_long_blockers_paginate(self):
        self.summary["closed"] = True
        self.summary["blockers"] = [f"Tinjau dokumen sumber nomor {i}: <img src='private'> & tanggal pencatatan." for i in range(75)]
        text = self.assert_valid_pdf(monthly_report_pdf(self.organization, self.summary))
        self.assertIn("PERIODE DITUTUP", text)
        self.assertNotIn("DRAF - PERIODE BELUM DITUTUP", text)
        self.assertIn("sumber nomor 74", text)
        self.assertIn("Halaman 2", text)

    def test_non_finite_financial_values_are_rejected(self):
        self.summary["revenue"] = Decimal("NaN")
        with self.assertRaises(ValueError):
            monthly_report_pdf(self.organization, self.summary)

    def test_accepted_multiline_names_do_not_overflow_invoice_cells(self):
        for related, model in ((self.invoice.party, Party), (self.invoice.product, Product)):
            with self.subTest(model=model.__name__):
                field = model._meta.get_field("name")
                multiline_name = ("X\n" * (field.max_length // 2)).rstrip()
                field.clean(multiline_name, None)
                original = related.name
                related.name = multiline_name
                try:
                    text = self.assert_valid_pdf(invoice_pdf(self.invoice))
                finally:
                    related.name = original
                self.assertIn("X X X X", text)
                self.assertIn("Rp520.000,00", text)
                self.assertNotIn("Halaman 2", text)

    def test_multiline_organization_and_long_tax_note_remain_readable(self):
        field = Organization._meta.get_field("name")
        self.organization.name = ("X\n" * (field.max_length // 2)).rstrip()
        field.clean(self.organization.name, None)
        self.summary["tax_profile_gates"] = [{
            "title": "Dokumen perlu ditinjau",
            "detail": "Periksa bukti sumber\r\n\t" * 250 + "AKHIR CATATAN",
        }]
        text = self.assert_valid_pdf(monthly_report_pdf(self.organization, self.summary))
        self.assertIn("X X X X", text)
        self.assertIn("AKHIR CATATAN", text)
        self.assertIn("Halaman 2", text)
