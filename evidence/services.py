"""Validate uploads locally; never send source documents to an external service."""

import hashlib
import warnings
from io import BytesIO
from pathlib import PurePosixPath

from django.core.exceptions import PermissionDenied, SuspiciousFileOperation, ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils.text import get_valid_filename
from PIL import Image
from pypdf import PdfReader
from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject

from core.audit import record
from core.models import Membership, Organization
from finance.models import Bill, Invoice
from .models import Attachment, MAX_UPLOAD_BYTES


ALLOWED_EXTENSIONS = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


def _validate_structure(data, detected):
    """Structural validation, not a malware certificate; originals stay private."""
    try:
        if detected == "application/pdf":
            reader = PdfReader(BytesIO(data), strict=True)
            if reader.is_encrypted or not 1 <= len(reader.pages) <= 2000:
                raise ValueError("PDF must have readable bounded pages")
            # Walk reachable objects once without decompressing page streams.
            pending, seen, visited = [reader.trailer], set(), 0
            forbidden_keys = {"/JS", "/JavaScript", "/OpenAction", "/AA", "/EmbeddedFiles", "/XFA", "/RichMediaContent"}
            forbidden_actions = {"/JavaScript", "/Launch", "/GoToR", "/GoToE", "/SubmitForm", "/ImportData", "/Rendition"}
            while pending:
                item = pending.pop()
                if isinstance(item, IndirectObject):
                    identity = (item.idnum, item.generation)
                    if identity in seen:
                        continue
                    seen.add(identity)
                    item = item.get_object()
                visited += 1
                if visited > 100000:
                    raise ValueError("PDF object limit")
                if isinstance(item, DictionaryObject):
                    if forbidden_keys.intersection(item) or str(item.get("/S", "")) in forbidden_actions or str(item.get("/Type", "")) in {"/EmbeddedFile", "/Filespec"}:
                        raise ValueError("Active or embedded PDF content")
                    pending.extend(item.values())
                elif isinstance(item, ArrayObject):
                    pending.extend(item)
        else:
            expected = "PNG" if detected == "image/png" else "JPEG"
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(BytesIO(data)) as image:
                    if image.format != expected or image.width * image.height > 20_000_000 or getattr(image, "n_frames", 1) != 1:
                        raise ValueError("Image format, dimensions or frames")
                    image.verify()
                with Image.open(BytesIO(data)) as image:
                    image.load()
    except Exception as exc:
        raise ValidationError("Berkas tidak dapat divalidasi. Gunakan PDF biasa tanpa sandi, skrip, atau lampiran, atau gambar PNG/JPEG yang utuh (maksimal 20 megapiksel).") from exc


def inspect_upload(upload):
    """Read at most 10 MB + 1 byte; reported size and browser MIME are untrusted."""
    name = PurePosixPath(str(getattr(upload, "name", "")).replace("\\", "/")).name
    suffix = PurePosixPath(name).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise ValidationError("Gunakan dokumen PDF, PNG, atau JPEG.")
    if not hasattr(upload, "read"):
        raise ValidationError("Berkas unggahan tidak tersedia.")
    try:
        upload.seek(0)
        data = bytearray()
        while len(data) <= MAX_UPLOAD_BYTES:
            chunk = upload.read(min(65536, MAX_UPLOAD_BYTES + 1 - len(data)))
            if not chunk:
                break
            if not isinstance(chunk, bytes):
                raise ValidationError("Berkas harus berupa data biner.")
            data.extend(chunk)
        upload.seek(0)
    except (OSError, ValueError, AttributeError) as exc:
        raise ValidationError("Berkas unggahan tidak dapat dibaca.") from exc
    if not data or len(data) > MAX_UPLOAD_BYTES:
        raise ValidationError("Berkas harus berisi data dan berukuran maksimal 10 MB.")
    detected = None
    if data.startswith(b"%PDF-"):
        detected = "application/pdf"
    elif data.startswith(b"\x89PNG\r\n\x1a\n"):
        detected = "image/png"
    elif data.startswith(b"\xff\xd8\xff"):
        detected = "image/jpeg"
    if detected != ALLOWED_EXTENSIONS[suffix]:
        raise ValidationError("Isi berkas tidak sesuai dengan format PDF, PNG, atau JPEG yang dipilih.")
    _validate_structure(bytes(data), detected)
    # Keep a display basename only. Storage names are independent random UUIDs.
    name = "".join(char for char in name if char.isprintable()).strip()
    try:
        name = get_valid_filename(name)
    except SuspiciousFileOperation as exc:
        raise ValidationError("Nama berkas tidak valid.") from exc
    if len(name) > 240:
        name = name[:240 - len(suffix)] + suffix
    return {"data": bytes(data), "original_name": name, "content_type": detected,
            "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def _authorize(organization, actor):
    if not getattr(actor, "is_authenticated", False) or not Membership.objects.filter(
        organization=organization, user=actor, user__is_active=True, role__in=["owner", "finance"]
    ).exists():
        raise PermissionDenied("Hanya pemilik dan staf keuangan yang dapat menambahkan dokumen.")


def attach_document(*, organization, actor, upload, invoice=None, bill=None):
    """Own the commit boundary so failed database writes can remove the new file."""
    _authorize(organization, actor)
    if (invoice is None) == (bill is None):
        raise ValidationError("Pilih tepat satu invoice atau tagihan pemasok.")
    inspected = inspect_upload(upload)
    stored_name, storage = None, None
    try:
        # Do not wrap this service in an outer atomic block: the file and row
        # must finish together before returning to the caller.
        with transaction.atomic(durable=True):
            organization = Organization.objects.select_for_update().get(pk=organization.pk)
            _authorize(organization, actor)
            model, source = (Invoice, invoice) if invoice is not None else (Bill, bill)
            try:
                source = model.objects.select_for_update().get(organization=organization, pk=getattr(source, "pk", source))
            except (model.DoesNotExist, ValueError, TypeError) as exc:
                raise ValidationError("Dokumen transaksi tidak tersedia untuk organisasi ini.") from exc
            source_fields = {"invoice": source} if model is Invoice else {"bill": source}
            existing = Attachment.objects.filter(organization=organization, sha256=inspected["sha256"], **source_fields).first()
            if existing:
                return existing
            data = inspected.pop("data")
            attachment = Attachment(organization=organization, uploaded_by=actor, **source_fields, **inspected)
            attachment._service_creation = True
            storage = attachment.file.storage
            attachment.file.save(inspected["original_name"], ContentFile(data), save=False)
            stored_name = attachment.file.name
            attachment.save()
            record(organization, actor, "evidence.document.attached", obj=attachment,
                   detail={"invoice_id": attachment.invoice_id, "bill_id": attachment.bill_id,
                           "sha256": attachment.sha256, "size": attachment.size})
        return attachment
    except Exception:
        if stored_name and storage:
            # Only this attempt's UUID path can be removed. Previous evidence
            # is never deleted by duplicate checks or a failed transaction.
            storage.delete(stored_name)
        raise
