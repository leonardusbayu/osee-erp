"""Bounded CSV import for reported marketing observations, never financial posting.

The natural key is (date, channel, source, reference) within one organization.
Only leading/trailing cell whitespace is removed; labels/references keep their
case and internal spelling. Replays compare every supplied observation field.
"""

import csv
import io
import re
from datetime import date
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction

from . import services
from .models import CHANNEL_CHOICES, MarketingObservation


CSV_COLUMNS = ("date", "channel", "spend", "leads", "paid_orders", "reported_value", "source", "reference", "basis")
MAX_CSV_BYTES = 1024 * 1024
MAX_CSV_ROWS = 500
MONEY_COLUMNS = ("spend", "reported_value")
COUNT_COLUMNS = ("leads", "paid_orders")
CHANNELS = frozenset(value for value, _ in CHANNEL_CHOICES)
BASES = frozenset(("reported", "provisional"))
CENT = Decimal("0.01")


def marketing_csv_template():
    """Header-only UTF-8 BOM template; no sample observations can be mistaken for data."""
    output = io.StringIO(newline="")
    csv.writer(output, lineterminator="\r\n").writerow(CSV_COLUMNS)
    return output.getvalue().encode("utf-8-sig")


def _key(row):
    return tuple(row[field] for field in ("date", "channel", "source", "reference"))


def _money(value, column):
    if not value:
        return None
    # Plain decimal notation avoids locale separators, NaN and exponent forms.
    if not re.fullmatch(r"[0-9]{1,18}(?:\.[0-9]{1,2})?", value):
        raise ValidationError(f"Kolom {column} harus angka nonnegatif, tanpa pemisah ribuan, maksimal dua desimal.")
    try:
        amount = Decimal(value)
        if not amount.is_finite() or amount >= Decimal("1000000000000000000"):
            raise InvalidOperation
        return amount.quantize(CENT)
    except InvalidOperation as exc:
        raise ValidationError(f"Jumlah pada kolom {column} tidak valid.") from exc


def _count(value, column):
    if not value:
        return None
    if not re.fullmatch(r"[0-9]{1,10}", value) or int(value) > 2147483647:
        raise ValidationError(f"Kolom {column} harus bilangan bulat antara 0 dan 2147483647.")
    return int(value)


def _validate_row(cells):
    row = dict(zip(CSV_COLUMNS, (cell.strip() for cell in cells)))
    try:
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", row["date"]):
            raise ValueError
        day = date.fromisoformat(row["date"])
        if not 2000 <= day.year <= 2200:
            raise ValueError
        row["date"] = day
    except ValueError as exc:
        raise ValidationError("Tanggal harus YYYY-MM-DD yang valid, dalam tahun 2000–2200.") from exc
    if row["channel"] not in CHANNELS:
        raise ValidationError("Kanal harus mitra, meta_ads, google_ads, whatsapp, seo, atau sales.")
    if row["basis"] not in BASES:
        raise ValidationError("Kolom basis harus reported atau provisional; pengamatan bukan data buku besar.")
    for field in ("source", "reference"):
        value = row[field]
        if not value or len(value) > 200 or any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValidationError(f"Kolom {field} wajib diisi dengan teks satu baris, maksimal 200 karakter.")
    for field in MONEY_COLUMNS:
        row[field] = _money(row[field], field)
    for field in COUNT_COLUMNS:
        row[field] = _count(row[field], field)
    if all(row[field] is None for field in MONEY_COLUMNS + COUNT_COLUMNS):
        raise ValidationError("Isi setidaknya satu angka pengamatan. Kolom kosong berarti belum diketahui, bukan nol.")
    return row


def parse_marketing_csv(content):
    """Validate the complete byte payload and return typed rows without database writes.

    Comma delimiter and the exact ordered header are required. Optional numeric
    blanks become None; explicit zero stays zero. Identical duplicate rows remain
    in the result so the importer can include them in its skipped count.
    """
    if not isinstance(content, (bytes, bytearray)):
        raise ValidationError("Isi CSV harus berupa berkas UTF-8.")
    if len(content) > MAX_CSV_BYTES:
        raise ValidationError("Ukuran CSV maksimal 1 MB.")
    try:
        decoded = bytes(content).decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValidationError("CSV harus disimpan sebagai UTF-8 (BOM diperbolehkan).") from exc
    if "\x00" in decoded:
        raise ValidationError("CSV mengandung karakter kontrol yang tidak didukung.")
    reader = csv.reader(io.StringIO(decoded, newline=""), strict=True)
    rows, seen = [], {}
    try:
        header = next(reader, None)
        if header != list(CSV_COLUMNS):
            raise ValidationError("Header CSV harus tepat dan berurutan: " + ",".join(CSV_COLUMNS) + ".")
        for cells in reader:
            if not cells or all(not cell.strip() for cell in cells):
                continue
            line_number = reader.line_num
            if len(rows) >= MAX_CSV_ROWS:
                raise ValidationError("CSV maksimal berisi 500 baris pengamatan.")
            if len(cells) != len(CSV_COLUMNS):
                raise ValidationError(f"Baris {line_number}: jumlah kolom harus {len(CSV_COLUMNS)}.")
            try:
                row = _validate_row(cells)
            except ValidationError as exc:
                raise ValidationError(f"Baris {line_number}: " + " ".join(exc.messages)) from exc
            key = _key(row)
            if key in seen and seen[key] != row:
                raise ValidationError(f"Baris {line_number}: referensi yang sama memiliki angka atau basis berbeda dalam CSV. Gunakan satu versi yang sudah ditinjau.")
            seen[key] = row
            rows.append(row)
    except csv.Error as exc:
        raise ValidationError("Struktur CSV tidak valid. Periksa pemisah koma, tanda kutip, dan panjang isian.") from exc
    if not rows:
        raise ValidationError("CSV belum berisi baris pengamatan. Isi template sebelum mengunggah.")
    return rows


@transaction.atomic
def import_marketing_csv(*, organization, actor, content):
    """Create all validated new observations atomically, or leave everything unchanged.

    Reuse the workflow's role check and organization lock, including for uploads
    containing only skipped rows. Every creation uses the audited public service.
    Active replacements can be replayed exactly; void-only keys cannot be revived
    by re-uploading an old file. Conflicts must use the explicit correction flow.
    """
    org = services._org(organization, actor, services.MARKETERS)
    rows = parse_marketing_csv(content)
    unique = {_key(row): row for row in rows}
    candidates = MarketingObservation.objects.filter(
        organization=org, date__in={row["date"] for row in rows}, channel__in={row["channel"] for row in rows},
    ).only(*CSV_COLUMNS, "status", "replaces_id").order_by("pk")
    existing = {}
    for item in candidates:
        key = tuple(getattr(item, field) for field in ("date", "channel", "source", "reference"))
        if key in unique:
            existing.setdefault(key, []).append(item)
    new_rows = []
    skipped = len(rows) - len(unique)
    for key, row in unique.items():
        matches = existing.get(key, [])
        active = [item for item in matches if item.status == "active"]
        if len(active) > 1:
            raise ValidationError("Satu referensi memiliki beberapa pengamatan aktif. Tinjau catatan sebelum mengimpor.")
        if active:
            if any(getattr(active[0], field) != row[field] for field in CSV_COLUMNS):
                raise ValidationError("Referensi sumber sudah memiliki angka atau basis berbeda. Gunakan tindakan Ganti pengamatan agar riwayat koreksi tersimpan.")
            skipped += 1
        elif matches:
            raise ValidationError("Referensi sumber ini sudah dibatalkan. Unggahan tidak mengaktifkannya kembali; tinjau koreksi atau gunakan referensi baru yang sah.")
        else:
            # Preflight model validation for every row before the first creation.
            MarketingObservation(organization=org, created_by=actor, **row).full_clean()
            new_rows.append(row)
    for row in new_rows:
        services.create_marketing_observation(organization=org, actor=actor, **row)
    return {"created": len(new_rows), "skipped": skipped}
