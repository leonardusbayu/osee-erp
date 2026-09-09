"""Import supplied PDF observations without turning a recap into accounting."""

import hashlib
import json
import re
from datetime import date
from decimal import Decimal
from pathlib import Path, PurePosixPath

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.db import transaction

from core.audit import record
from core.models import Membership, Organization
from finance.models import Bill, Party
from finance.services import create_bill
from .models import ImportBatch, ImportException, MonthlySnapshot, SourceDocument


MAX_SOURCE_BYTES = 50 * 1024 * 1024
MAX_BUNDLE_BYTES = 25 * 1024 * 1024


def _fail(message):
    raise ValidationError(f"Bundel sumber tidak valid: {message}")


def _text(value, label, limit=10000, *, empty=False):
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        _fail(label)
    return value


def _integer(value, label, *, nullable=False, positive=False):
    if nullable and value is None:
        return None
    if type(value) is not int or value < (1 if positive else 0) or value >= 10**18:
        _fail(label)
    return value


def _iso_date(value, label, *, nullable=False):
    if value is None and nullable:
        return None
    try:
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError
        result = date.fromisoformat(value)
        if not 2000 <= result.year <= 2200:
            raise ValueError
        return result
    except ValueError:
        _fail(label)


def _period(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}", value):
        _fail("periode harus YYYY-MM")
    return _iso_date(value + "-01", "periode")


def _indexed(rows, label):
    if not isinstance(rows, list) or len(rows) > 100000:
        _fail(label)
    result = {}
    for item in rows:
        if not isinstance(item, dict):
            _fail(label)
        key = _text(item.get("id"), f"ID {label}", 160)
        if key in result:
            _fail(f"ID {label} berulang")
        result[key] = item
    return result


def _locator(value, label):
    if not isinstance(value, dict):
        _fail(f"lokator {label}")


def _required(row, keys, label):
    if not set(keys).issubset(row):
        _fail(f"kolom wajib {label} tidak lengkap; nilai kosong harus ditulis null")


def _reference(value, table, label, *, nullable=False):
    if nullable and value is None:
        return
    if not isinstance(value, str) or value not in table:
        _fail(label)


def _validate(bundle):
    required = {"schema_version", "sources", "months", "channels", "details", "exceptions", "annual_summary", "supplier_invoice"}
    if not isinstance(bundle, dict) or not required.issubset(bundle) or bundle["schema_version"] != "1.0":
        _fail("versi atau struktur utama")
    sources = _indexed(bundle["sources"], "sumber")
    months = _indexed(bundle["months"], "bulan")
    channels = _indexed(bundle["channels"], "kolom sumber")
    details = _indexed(bundle["details"], "rincian")
    exceptions = _indexed(bundle["exceptions"], "pengecualian")
    if not sources or not months:
        _fail("sumber dan bulan harus tersedia")
    recaps = [row for row in sources.values() if row.get("kind") == "operating_recap"]
    if len(recaps) != 1:
        _fail("tepat satu sumber rekap diperlukan")
    for source in sources.values():
        _required(source, ("kind", "relative_path", "original_filename", "sha256", "page_count"), "sumber")
        if source.get("kind") not in {"operating_recap", "supplier_invoice"}:
            _fail("jenis sumber")
        sha = source.get("sha256")
        if not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{64}", sha):
            _fail("SHA-256 sumber")
        _integer(source.get("page_count"), "jumlah halaman", positive=True)
        name = _text(source.get("original_filename"), "nama asli berkas", 240)
        if PurePosixPath(name.replace("\\", "/")).name != name or any(not char.isprintable() for char in name):
            _fail("nama asli harus basename tanpa karakter kontrol")
    periods = set()
    for month in months.values():
        _required(month, ("source_id", "source_period", "page", "printed_count", "printed_amount", "detail_count", "format_controls", "coverage", "locator"), "bulan")
        period = _period(month.get("source_period"))
        _reference(month.get("source_id"), sources, "sumber bulan tidak tersedia")
        if period in periods:
            _fail("periode berulang")
        periods.add(period)
        _integer(month.get("page"), "halaman bulan", positive=True)
        if month["page"] > sources[month["source_id"]]["page_count"]:
            _fail("halaman bulan di luar sumber")
        for key in ("printed_count", "printed_amount", "detail_count"):
            _integer(month.get(key), f"bulan {key}", nullable=True)
        _text(month.get("coverage"), "cakupan bulan", 100)
        if not isinstance(month.get("format_controls"), list):
            _fail("kontrol format bulan")
        _locator(month.get("locator"), "bulan")
    for channel in channels.values():
        _required(channel, ("month_id", "source_label", "raw_header", "header_price", "printed_count", "printed_amount", "legacy_contributions", "locator"), "kolom sumber")
        _reference(channel.get("month_id"), months, "bulan kolom tidak tersedia")
        _text(channel.get("source_label"), "label kolom", 500)
        _text(channel.get("raw_header"), "header sumber", 2000, empty=True)
        for key in ("header_price", "printed_count", "printed_amount"):
            _integer(channel.get(key), f"kolom {key}", nullable=True)
        if not isinstance(channel.get("legacy_contributions"), list):
            _fail("kontribusi historis kolom")
        _locator(channel.get("locator"), "kolom")
    for detail in details.values():
        _required(detail, ("month_id", "channel_id", "original_date_text", "parsed_source_date", "proposed_context_date", "quantity", "year_conflict", "locator"), "rincian")
        _reference(detail.get("month_id"), months, "bulan rincian tidak tersedia")
        _reference(detail.get("channel_id"), channels, "kolom rincian tidak tersedia")
        if channels[detail["channel_id"]]["month_id"] != detail["month_id"]:
            _fail("bulan rincian berbeda dari kolom")
        _text(detail.get("original_date_text"), "tanggal asli", 100)
        _iso_date(detail.get("parsed_source_date"), "tanggal sumber", nullable=True)
        _iso_date(detail.get("proposed_context_date"), "usulan tanggal konteks", nullable=True)
        _integer(detail.get("quantity"), "kuantitas rincian")
        if type(detail.get("year_conflict")) is not bool:
            _fail("penanda konflik tahun")
        for key in ("merged_date_cell", "date_ambiguous"):
            if key in detail and type(detail[key]) is not bool:
                _fail(f"penanda {key}")
        if "source_date_candidates" in detail:
            if not isinstance(detail["source_date_candidates"], list):
                _fail("kandidat tanggal sumber")
            for candidate in detail["source_date_candidates"]:
                _text(candidate, "kandidat tanggal sumber", 100)
        if "date_status" in detail:
            _text(detail["date_status"], "status tanggal", 100)
        if detail.get("date_ambiguous") and (detail["parsed_source_date"] is not None or detail["proposed_context_date"] is not None):
            _fail("tanggal ambigu harus tetap null sampai ada bukti penyelesaian")
        _locator(detail.get("locator"), "rincian")
    for exception in exceptions.values():
        for key, limit in (("code", 100), ("severity", 40), ("status", 40), ("explanation", 10000)):
            _text(exception.get(key), f"pengecualian {key}", limit)
        for field, table in (("month_id", months), ("channel_id", channels)):
            _reference(exception.get(field), table, f"referensi pengecualian {field}", nullable=True)
        ids = exception.get("detail_ids")
        if not isinstance(ids, list) or any(not isinstance(key, str) or key not in details for key in ids):
            _fail("referensi rincian pengecualian")
        if not isinstance(exception.get("observed"), dict) or not isinstance(exception.get("expected"), dict):
            _fail("pengamatan pengecualian")
    for month_id, month in months.items():
        counted = sum(detail["quantity"] for detail in details.values() if detail["month_id"] == month_id)
        if month["detail_count"] is not None and month["detail_count"] != counted:
            _fail("detail_count tidak sama dengan jumlah kuantitas rincian sumber")
    annual = bundle["annual_summary"]
    if not isinstance(annual, dict):
        _fail("ringkasan tahunan")
    for key in ("covered_periods", "missing_periods"):
        if not isinstance(annual.get(key), list):
            _fail(f"ringkasan tahunan {key}")
        for value in annual[key]:
            _period(value)
    supplier = bundle["supplier_invoice"]
    if supplier is not None:
        if not isinstance(supplier, dict):
            _fail("referensi invoice pemasok")
        _reference(supplier.get("source_id"), sources, "referensi invoice pemasok")
        if sources[supplier["source_id"]]["kind"] != "supplier_invoice":
            _fail("jenis sumber invoice pemasok")
        facts = supplier.get("facts")
        if not isinstance(facts, dict) or facts.get("currency") != "IDR":
            _fail("fakta invoice pemasok IDR")
        for key, limit in (("invoice_number", 80), ("supplier_name", 200)):
            _text(facts.get(key), key, limit)
        _iso_date(facts.get("invoice_date"), "tanggal invoice pemasok")
        _iso_date(facts.get("service_date"), "tanggal layanan pemasok")
        for key in ("quantity", "unit_price", "base_amount", "total_amount"):
            _integer(facts.get(key), key, positive=True)
        _integer(facts.get("vat_amount"), "vat_amount")
        if facts["quantity"] * facts["unit_price"] != facts["base_amount"] or facts["base_amount"] + facts["vat_amount"] != facts["total_amount"]:
            _fail("total invoice pemasok tidak cocok dengan komponen tertulis")
    return sources, months, channels, details, exceptions, recaps[0]["sha256"]


def _source_bytes(root, source):
    relative = _text(source.get("relative_path"), "lokasi relatif sumber", 300)
    normalized = PurePosixPath(relative.replace("\\", "/"))
    if normalized.is_absolute() or ".." in normalized.parts or Path(relative).is_absolute():
        _fail("lokasi sumber harus berada di direktori bundel")
    path = (root / Path(*normalized.parts)).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        _fail("berkas sumber tidak tersedia di direktori bundel")
    with path.open("rb") as handle:
        data = handle.read(MAX_SOURCE_BYTES + 1)
    if not data.startswith(b"%PDF-") or len(data) > MAX_SOURCE_BYTES:
        _fail("sumber harus PDF maksimal 50 MB")
    if hashlib.sha256(data).hexdigest() != source["sha256"]:
        _fail("hash berkas sumber tidak sesuai manifest")
    return data


def _authorize(org, actor):
    if actor is not None and (not getattr(actor, "is_authenticated", False) or not Membership.objects.filter(organization=org, user=actor, role__in=["owner", "finance"]).exists()):
        raise PermissionDenied("Peran akun tidak dapat mengimpor arsip sumber.")


def _draft_supplier_bill(org, supplier_invoice, actor):
    if supplier_invoice is None:
        return None
    facts = supplier_invoice["facts"]
    suppliers = Party.objects.filter(organization=org, kind="supplier", name=facts["supplier_name"])
    if suppliers.count() > 1:
        _fail("nama pemasok ambigu; tinjau kontak sebelum impor")
    supplier = suppliers.first() or Party.objects.create(organization=org, kind="supplier", name=facts["supplier_name"])
    existing = Bill.objects.filter(organization=org, supplier=supplier, number=facts["invoice_number"]).first()
    fields = {"amount": Decimal(facts["total_amount"]), "supplier_vat": Decimal(facts["vat_amount"]),
              "date": _iso_date(facts["invoice_date"], "tanggal invoice"),
              "service_date": _iso_date(facts["service_date"], "tanggal layanan")}
    if existing:
        if any(getattr(existing, key) != value for key, value in fields.items()):
            _fail("nomor tagihan pemasok sudah ada dengan fakta berbeda")
        return existing
    return create_bill(organization=org, supplier=supplier, number=facts["invoice_number"],
                       category="provider", actor=actor, **fields)


def import_bundle(*, organization, bundle_path, actor=None):
    """Import a local validated bundle; this service owns its commit boundary."""
    _authorize(organization, actor)
    path = Path(bundle_path).resolve()
    try:
        if not path.is_file() or path.stat().st_size > MAX_BUNDLE_BYTES:
            _fail("berkas bundel JSON tidak tersedia atau terlalu besar")
        bundle = json.loads(path.read_text(encoding="utf-8-sig"), parse_constant=lambda value: _fail("angka JSON tidak terbatas"))
        canonical = json.dumps(bundle, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (UnicodeError, ValueError, TypeError, OSError) as exc:
        raise ValidationError("Bundel JSON tidak dapat dibaca.") from exc
    sources, months, channels, details, exceptions, source_hash = _validate(bundle)
    source_data = {key: _source_bytes(path.parent, source) for key, source in sources.items()}
    bundle_hash = hashlib.sha256(canonical).hexdigest()
    stored = []
    try:
        with transaction.atomic(durable=True):
            org = Organization.objects.select_for_update().get(pk=organization.pk)
            _authorize(org, actor)
            old = ImportBatch.objects.filter(organization=org, source_hash=source_hash).first()
            if old:
                if old.bundle_hash != bundle_hash:
                    raise ValidationError("Sumber yang sama sudah diimpor dengan isi bundel berbeda. Arsip lama tidak ditimpa.")
                return old
            for month in months.values():
                if MonthlySnapshot.objects.filter(organization=org, source_period=month["source_period"]).exists():
                    raise ValidationError("Periode sumber sudah diarsipkan; impor tumpang tindih memerlukan peninjauan.")
            bill = _draft_supplier_bill(org, bundle["supplier_invoice"], actor)
            batch = ImportBatch(organization=org, source_hash=source_hash, bundle_hash=bundle_hash, payload=bundle,
                                supplier_bill=bill, metadata={"source_count": len(sources), "month_count": len(months),
                                                            "channel_rows": len(channels), "detail_rows": len(details), "exception_count": len(exceptions)})
            batch._import_creation = True
            batch.save()
            for key, source in sources.items():
                doc = SourceDocument(organization=org, batch=batch, source_id=key, kind=source["kind"],
                                     original_name=source["original_filename"], sha256=source["sha256"],
                                     size=len(source_data[key]), page_count=source["page_count"])
                doc._import_creation = True
                doc.file.save(source["original_filename"], ContentFile(source_data[key]), save=False)
                stored.append((doc.file.storage, doc.file.name))
                doc.save()
            for key, month in months.items():
                period = _period(month["source_period"])
                row = MonthlySnapshot(organization=org, batch=batch, source_id=key, source_period=month["source_period"],
                                      year=period.year, month=period.month, source_page=month["page"], printed_count=month["printed_count"],
                                      printed_amount=None if month["printed_amount"] is None else Decimal(month["printed_amount"]),
                                      detail_count=month["detail_count"], coverage=month["coverage"], payload=month)
                row._import_creation = True
                row.save()
            for key, exception in exceptions.items():
                month_id = exception.get("month_id") or ""
                locator = exception.get("locator") or (months[month_id]["locator"] if month_id else {})
                row = ImportException(organization=org, batch=batch, exception_id=key, code=exception["code"], severity=exception["severity"],
                                      message=exception["explanation"], month_source_id=month_id, source_locator=locator, payload=exception)
                row._import_creation = True
                row.save()
            record(org, actor, "imports.source_bundle.imported", obj=batch,
                   detail={"source_hash": source_hash, "bundle_hash": bundle_hash, **batch.metadata})
        return batch
    except Exception:
        for storage, name in stored:
            storage.delete(name)
        raise


def month_data(*, organization, source_period):
    _period(source_period)
    try:
        month = MonthlySnapshot.objects.select_related("batch").get(organization=organization, source_period=source_period)
    except MonthlySnapshot.DoesNotExist as exc:
        raise ValidationError("Periode sumber tidak tersedia untuk organisasi ini.") from exc
    payload = month.batch.payload
    return {"month": month, "batch": month.batch,
            "channels": [row for row in payload["channels"] if row["month_id"] == month.source_id],
            "details": [row for row in payload["details"] if row["month_id"] == month.source_id],
            "exceptions": month.batch.exceptions.filter(organization=organization, month_source_id=month.source_id),
            "source_documents": month.batch.source_documents.filter(organization=organization)}


def import_summary(*, organization, year=2026):
    _period(f"{year}-01")
    months = list(MonthlySnapshot.objects.filter(organization=organization, year=year).select_related("batch"))
    batch_ids = {month.batch_id for month in months}
    batches = list(ImportBatch.objects.filter(organization=organization, pk__in=batch_ids))
    labels = set()
    detail_rows = 0
    annual_counts = []
    for batch in batches:
        source_ids = {month.source_id for month in months if month.batch_id == batch.pk}
        labels.update(row["source_label"] for row in batch.payload["channels"] if row["month_id"] in source_ids)
        detail_rows += sum(row["month_id"] in source_ids for row in batch.payload["details"])
        printed = batch.payload["annual_summary"].get("printed_total")
        if isinstance(printed, dict):
            printed = printed.get("count", printed.get("printed_count"))
        if type(printed) is int:
            annual_counts.append(printed)
    covered = [month.source_period for month in months]
    exceptions = ImportException.objects.filter(organization=organization, batch_id__in=batch_ids)
    observed_amounts = [month.printed_amount for month in months if month.printed_amount is not None]
    observed_counts = [month.printed_count for month in months if month.printed_count is not None]
    detail_counts = [month.detail_count for month in months if month.detail_count is not None]
    return {"year": year, "months": months, "batches": batches,
            "reported_amount": sum(observed_amounts, Decimal("0.00")) if observed_amounts else None,
            "printed_count": sum(observed_counts) if observed_counts else None,
            "detail_count": sum(detail_counts) if detail_counts else None,
            "channel_count": len(labels), "detail_rows": detail_rows, "exception_count": exceptions.count(),
            "covered_months": covered, "missing_months": [f"{year}-{month:02d}" for month in range(1, 13) if f"{year}-{month:02d}" not in covered],
            "annual_printed_count": sum(annual_counts) if annual_counts else None,
            "source_documents": SourceDocument.objects.filter(organization=organization, batch_id__in=batch_ids), "exceptions": exceptions,
            "coverage": "Arsip halaman sumber, bukan laporan tahun lengkap, kas, atau buku besar."}
