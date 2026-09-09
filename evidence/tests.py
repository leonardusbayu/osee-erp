from datetime import date
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image
from pypdf import PdfWriter

from core.models import AuditEvent, Membership, Organization
from finance.models import Bill, Invoice, Party, Product
from .forms import AttachmentForm
from .models import Attachment, MAX_UPLOAD_BYTES
from .services import attach_document


def pdf_bytes(*, title="Evidence", javascript=False, encrypted=False):
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_metadata({"/Title": title})
    if javascript:
        writer.add_js("app.alert('test')")
    if encrypted:
        writer.encrypt("synthetic-only")
    stream = BytesIO()
    writer.write(stream)
    return stream.getvalue()


def image_bytes(format):
    stream = BytesIO()
    Image.new("RGB", (10, 10), "white").save(stream, format=format)
    return stream.getvalue()


PDF = pdf_bytes()
DAY = date(2026, 5, 4)


class PrivateEvidenceTests(TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory(prefix="osee-evidence-test-")
        self.settings_override = override_settings(MEDIA_ROOT=self.directory.name)
        self.settings_override.enable()
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.settings_override.disable)
        self.org = Organization.objects.create(name="Evidence owner")
        self.other = Organization.objects.create(name="Other company")
        self.owner = get_user_model().objects.create_user(username="evidence-owner")
        self.auditor = get_user_model().objects.create_user(username="evidence-auditor")
        self.foreign_user = get_user_model().objects.create_user(username="evidence-foreign")
        Membership.objects.create(organization=self.org, user=self.owner, role="owner")
        Membership.objects.create(organization=self.org, user=self.auditor, role="auditor")
        Membership.objects.create(organization=self.other, user=self.foreign_user, role="owner")
        self.invoice = self.make_invoice(self.org, "INV-1")
        self.foreign_invoice = self.make_invoice(self.other, "INV-2")
        supplier = Party.objects.create(organization=self.org, name="Supplier", kind="supplier")
        self.bill = Bill.objects.create(organization=self.org, supplier=supplier, number="B-1",
                                        amount=Decimal("444000"), date=DAY, service_date=DAY)

    def make_invoice(self, org, number):
        party = Party.objects.create(organization=org, name="Customer", kind="customer")
        product = Product.objects.create(organization=org, code="ITP", name="ITP", kind="itp", default_price=Decimal("500000"))
        return Invoice.objects.create(organization=org, party=party, product=product, number=number,
                                      quantity=1, unit_price=Decimal("500000"), date=DAY, due_date=DAY, service_date=DAY)

    def attach(self, *, upload=None, **kwargs):
        return attach_document(organization=self.org, actor=self.owner, invoice=self.invoice,
                               upload=upload or SimpleUploadedFile("invoice.pdf", PDF), **kwargs)

    def files(self):
        return [path for path in Path(self.directory.name).rglob("*") if path.is_file()]

    def test_duplicate_bytes_on_same_source_return_existing_without_duplicate_file(self):
        first = self.attach()
        second = self.attach(upload=SimpleUploadedFile("different-name.pdf", PDF))
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Attachment.objects.count(), 1)
        self.assertEqual(len(self.files()), 1)
        self.assertEqual(AuditEvent.objects.filter(action="evidence.document.attached").count(), 1)
        bill_attachment = attach_document(organization=self.org, actor=self.owner, bill=self.bill,
                                          upload=SimpleUploadedFile("supplier.pdf", PDF))
        self.assertNotEqual(first.pk, bill_attachment.pk)
        self.assertEqual(len(self.files()), 2)

    def test_traversal_filename_is_basename_only_and_storage_path_uses_uuid(self):
        upload = SimpleUploadedFile("..\\..\\supplier invoice.pdf", PDF)
        attached = self.attach(upload=upload)
        self.assertEqual(attached.original_name, "supplier_invoice.pdf")
        self.assertRegex(attached.file.name, rf"\Aorganizations/{self.org.pk}/evidence/[a-f0-9]{{32}}\.pdf\Z")
        self.assertNotIn("supplier", attached.file.name)
        self.assertEqual(attached.size, len(PDF))
        self.assertEqual(len(attached.sha256), 64)
        self.assertEqual(attached.uploaded_by, self.owner)
        self.assertTrue(Path(attached.file.path).resolve().is_relative_to(Path(self.directory.name).resolve()))
        with self.assertRaises(NotImplementedError):
            _ = attached.file.url

    def test_invalid_extension_magic_and_actual_size_are_rejected(self):
        cases = [("fake.pdf", b"not a PDF"), ("png.pdf", b"\x89PNG\r\n\x1a\n"),
                 ("file.exe", PDF), ("empty.pdf", b"")]
        for filename, data in cases:
            with self.subTest(filename=filename), self.assertRaises(ValidationError):
                self.attach(upload=SimpleUploadedFile(filename, data))
        oversized = SimpleUploadedFile("large.pdf", b"%PDF-" + b"x" * (MAX_UPLOAD_BYTES - 4))
        oversized.size = 1  # The service must count the bytes rather than trust metadata.
        with self.assertRaisesMessage(ValidationError, "10 MB"):
            self.attach(upload=oversized)
        self.assertFalse(Attachment.objects.exists())
        self.assertEqual(self.files(), [])

    def test_allowed_images_are_detected_from_bytes_instead_of_browser_mime(self):
        for name, data, content_type in [
            ("receipt.PNG", image_bytes("PNG"), "image/png"),
            ("receipt.jpeg", image_bytes("JPEG"), "image/jpeg"),
        ]:
            with self.subTest(name=name):
                attached = self.attach(upload=SimpleUploadedFile(name, data, content_type="text/html"))
                self.assertEqual(attached.content_type, content_type)

    def test_source_scope_exactly_one_target_and_upload_role_are_enforced(self):
        common = {"organization": self.org, "actor": self.owner, "upload": SimpleUploadedFile("a.pdf", PDF)}
        for source in [{}, {"invoice": self.invoice, "bill": self.bill}, {"invoice": self.foreign_invoice}]:
            with self.subTest(source=source), self.assertRaises(ValidationError):
                attach_document(**common, **source)
        with self.assertRaises(PermissionDenied):
            attach_document(**(common | {"actor": self.auditor}), invoice=self.invoice)
        with self.assertRaises(PermissionDenied):
            attach_document(**(common | {"actor": None}), invoice=self.invoice)
        self.assertEqual(self.files(), [])

    def test_database_failure_cleans_only_new_file_and_keeps_existing_evidence(self):
        original = self.attach()
        original_path = Path(original.file.path)
        with patch("evidence.services.record", side_effect=IntegrityError("simulated audit failure")):
            with self.assertRaises(IntegrityError):
                self.attach(upload=SimpleUploadedFile("new.pdf", pdf_bytes(title="Different evidence")))
        self.assertEqual(Attachment.objects.count(), 1)
        self.assertEqual(self.files(), [original_path])
        self.assertEqual(original_path.read_bytes(), PDF)

    def test_members_can_download_privately_but_other_organizations_cannot(self):
        attached = self.attach()
        url = reverse("evidence:download", args=[attached.pk])
        self.assertEqual(self.client.get(url).status_code, 302)
        self.client.force_login(self.auditor)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response["Content-Disposition"].startswith("attachment;"))
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(b"".join(response.streaming_content), PDF)
        self.client.force_login(self.foreign_user)
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_download_rechecks_source_organization_and_reports_missing_file(self):
        attached = self.attach()
        url = reverse("evidence:download", args=[attached.pk])
        self.client.force_login(self.owner)
        with connection.cursor() as cursor:
            # Simulate an invalid legacy/manual DB write that bypassed the model.
            cursor.execute("UPDATE evidence_attachment SET invoice_id = %s WHERE id = %s", [self.foreign_invoice.pk, attached.pk])
        self.assertEqual(self.client.get(url).status_code, 404)
        with connection.cursor() as cursor:
            cursor.execute("UPDATE evidence_attachment SET invoice_id = %s WHERE id = %s", [self.invoice.pk, attached.pk])
        Path(attached.file.path).unlink()
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_evidence_is_immutable_and_form_accepts_no_source_fields(self):
        attached = self.attach()
        attached.original_name = "changed.pdf"
        with self.assertRaises(ValidationError):
            attached.save()
        with self.assertRaises(ValidationError):
            attached.delete()
        self.assertEqual(set(AttachmentForm.base_fields), {"file"})

    def test_deactivated_actor_is_rejected_even_when_passed_a_stale_user_instance(self):
        get_user_model().objects.filter(pk=self.owner.pk).update(is_active=False)
        self.assertTrue(self.owner.is_active)
        with self.assertRaises(PermissionDenied):
            self.attach()
        self.assertEqual(self.files(), [])

    def test_spoofed_corrupt_active_and_encrypted_documents_are_rejected(self):
        cases = [("fake.pdf", b"%PDF-not-a-document"),
                 ("active.pdf", pdf_bytes(javascript=True)),
                 ("secret.pdf", pdf_bytes(encrypted=True)),
                 ("truncated.pdf", PDF[:100]),
                 ("fake.png", b"\x89PNG\r\n\x1a\nfixture"),
                 ("fake.jpg", b"\xff\xd8\xfffixture"),
                 ("truncated.jpg", image_bytes("JPEG")[:-30])]
        for name, data in cases:
            with self.subTest(name=name), self.assertRaises(ValidationError):
                self.attach(upload=SimpleUploadedFile(name, data))
        self.assertFalse(Attachment.objects.exists())
        self.assertEqual(self.files(), [])
