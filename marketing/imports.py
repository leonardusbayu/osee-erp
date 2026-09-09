"""Bounded, atomic ad-source import. No transformations into financial records."""
import csv
import io
import re
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction

from . import services
from .models import AdDailyObservation, Campaign, ALL_ADS, ALL_AUDIENCES, ALL_REGIONS

CSV_COLUMNS = ("tanggal", "iklan", "audiens", "wilayah", "biaya", "tayangan", "klik", "sumber", "referensi")
MAX_BYTES = 1024 * 1024
MAX_ROWS = 500


def ad_csv_template():
    output = io.StringIO(newline="")
    csv.writer(output, lineterminator="\r\n").writerow(CSV_COLUMNS)
    return output.getvalue().encode("utf-8-sig")


def _count(value, column):
    if not value:
        return None
    if not re.fullmatch(r"[0-9]{1,10}", value) or int(value) > 2147483647:
        raise ValidationError(f"{column} harus bilangan bulat nonnegatif maksimal 2147483647.")
    return int(value)


def parse_ad_csv(content):
    if not isinstance(content, bytes) or not content or len(content) > MAX_BYTES:
        raise ValidationError("Gunakan CSV UTF-8 maksimal 1 MB dengan isi laporan.")
    try:
        decoded = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValidationError("File harus disimpan sebagai CSV UTF-8.") from None
    if "\x00" in decoded:
        raise ValidationError("File mengandung karakter yang tidak valid.")
    reader = csv.reader(io.StringIO(decoded, newline=""), strict=True)
    try:
        if tuple(next(reader, ())) != CSV_COLUMNS:
            raise ValidationError("Kolom tidak sesuai template. Unduh template iklan dan pertahankan urutan kolomnya.")
        rows = []
        for line, cells in enumerate(reader, 2):
            if not cells or all(not cell.strip() for cell in cells):
                continue
            if len(rows) >= MAX_ROWS:
                raise ValidationError("Maksimal 500 baris per impor.")
            if len(cells) != len(CSV_COLUMNS):
                raise ValidationError(f"Baris {line}: jumlah kolom harus {len(CSV_COLUMNS)}.")
            values = dict(zip(CSV_COLUMNS, (cell.strip() for cell in cells)))
            try:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", values["tanggal"]):
                    raise ValidationError("tanggal harus YYYY-MM-DD.")
                try:
                    day = date.fromisoformat(values["tanggal"])
                except ValueError:
                    raise ValidationError("tanggal tidak valid.") from None
                if not 2000 <= day.year <= 2200:
                    raise ValidationError("tanggal harus dalam tahun 2000–2200.")
                amount = values["biaya"]
                if amount and not re.fullmatch(r"[0-9]{1,18}(?:\.[0-9]{1,2})?", amount):
                    raise ValidationError("biaya harus angka tanpa pemisah ribuan, maksimal dua desimal; kosong berarti belum diketahui.")
                if not values["sumber"] or not values["referensi"]:
                    raise ValidationError("sumber dan referensi wajib diisi.")
                rows.append({"date": day, "ad_name": values["iklan"] or ALL_ADS,
                             "audience": values["audiens"] or ALL_AUDIENCES,
                             "region": values["wilayah"] or ALL_REGIONS,
                             "spend": Decimal(amount) if amount else None,
                             "impressions": _count(values["tayangan"], "tayangan"),
                             "clicks": _count(values["klik"], "klik"),
                             "source": values["sumber"], "reference": values["referensi"]})
            except ValidationError as exc:
                raise ValidationError(f"Baris {line}: {' '.join(exc.messages)}") from exc
        if not rows:
            raise ValidationError("Template masih kosong. Tambahkan minimal satu baris laporan.")
        return rows
    except csv.Error:
        raise ValidationError("Format CSV tidak valid. Gunakan pemisah koma dan kutip teks yang mengandung koma.") from None


@transaction.atomic
def import_ad_csv(*, organization, actor, campaign, content):
    org = services._org(organization, actor)
    selected = services._scoped(Campaign, campaign, org)
    rows = parse_ad_csv(content)
    created = duplicates = 0
    for line, values in enumerate(rows, 2):
        exists = AdDailyObservation.objects.filter(organization=org, reference=values["reference"]).exists()
        try:
            services.save_ad_observation(organization=org, actor=actor, campaign=selected, **values)
        except ValidationError as exc:
            raise ValidationError(f"Baris {line}: {' '.join(exc.messages)} Seluruh impor dibatalkan; belum ada perubahan tersimpan.") from exc
        created += not exists
        duplicates += exists
    return {"created": created, "duplicates": duplicates, "rows": len(rows)}
