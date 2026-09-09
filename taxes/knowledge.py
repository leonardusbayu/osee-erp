"""Immutable source snapshots, one freshness policy and historical citations."""
from datetime import date
from types import SimpleNamespace

from django.db import transaction
from django.core.exceptions import ValidationError
from django.utils import timezone
from .models import TaxSource, TaxSourceRevision, TaxSourcePublication
from .workflow import digest, json_value


def snapshot(source):
    return json_value({name: getattr(source, name) for name in ("slug", "title", "url", "issuer", "article", "summary", "topics", "reviewed_on", "effective_from", "effective_until")})


def _revision(source, data=None):
    data = data or snapshot(source)
    content_hash = digest(data)
    row = source.revisions.filter(content_hash=content_hash).first()
    if row is None:
        row = TaxSourceRevision(source=source, snapshot=data, content_hash=content_hash)
        row._knowledge_write = True
        row.save()
    return row


@transaction.atomic
def publish_catalog(catalog, reviewed_on, *, reviewed_by=None, review_reference=None):
    """Stage revisions. Activation requires explicit recorded operator review."""
    for item in catalog:
        values = dict(item)
        slug = values.pop("slug")
        source, created = TaxSource.objects.select_for_update().get_or_create(slug=slug,
            defaults={**values, "reviewed_on": reviewed_on, "approved": False})
        if not created:
            _revision(source)
        candidate = {**snapshot(source), **json_value(values), "reviewed_on": reviewed_on.isoformat()}
        revision = _revision(source, candidate)
        if reviewed_by and review_reference and (created or source.approved):
            activate_source_revision(revision=revision, reviewer_name=reviewed_by, review_reference=review_reference, confirm_reviewed=True)


@transaction.atomic
def activate_source_revision(*, revision, reviewer_name, review_reference, confirm_reviewed=False):
    if confirm_reviewed is not True or any(not isinstance(value, str) or len(value.strip()) < 3 for value in (reviewer_name, review_reference)):
        raise ValidationError("Catat pemeriksa dan referensi pemeriksaan sumber; jangan mengaktifkan otomatis.")
    revision = TaxSourceRevision.objects.get(pk=revision.pk)
    source = TaxSource.objects.select_for_update().get(pk=revision.source_id)
    data = dict(revision.snapshot)
    reviewed_on = date.fromisoformat(data["reviewed_on"])
    if not 0 <= (timezone.localdate() - reviewed_on).days <= 180:
        raise ValidationError("Versi sumber di luar masa tinjauan; siapkan versi tinjauan terbaru.")
    for name, value in data.items():
        if name in ("reviewed_on", "effective_from", "effective_until"):
            value = date.fromisoformat(value) if value else None
        setattr(source, name, value)
    source.approved = True
    source.save()
    publication = TaxSourcePublication(revision=revision, reviewer_name=reviewer_name.strip(), review_reference=review_reference.strip())
    publication._knowledge_write = True
    publication.save()
    return publication


def current_sources(topic=None, slugs=None):
    today = timezone.localdate()
    query = TaxSource.objects.filter(approved=True)
    if slugs is not None:
        query = query.filter(slug__in=slugs)
    rows = []
    for source in query:
        if not 0 <= (today - source.reviewed_on).days <= 180 or (source.effective_from and source.effective_from > today) or (source.effective_until and source.effective_until < today) or (topic and topic not in source.topics):
            continue
        data = snapshot(source)
        revision = source.revisions.filter(content_hash=digest(data)).first()
        if revision is None or not TaxSourcePublication.objects.filter(revision=revision).exists():
            continue  # Staging or retaining a legacy flag is not a publication review.
        source.version_id, source.content_hash = revision.pk, revision.content_hash
        rows.append(source)
    return rows


def source_cards(sources):
    return [{"id": row.slug, "title": row.title, "url": row.url, "article": row.article,
             "reviewed_on": row.reviewed_on.isoformat(), "version_id": getattr(row, "version_id", None),
             "content_hash": getattr(row, "content_hash", None)} for row in sources]


def historical_cards(message):
    saved = message.metadata.get("source_cards")
    if not isinstance(saved, list):
        return [{"id": slug, "title": slug + " — versi historis tidak tersimpan", "url": "", "article": "Jangan gunakan jawaban lama untuk keputusan baru.",
                 "reviewed_on": message.metadata.get("source_review_dates", {}).get(slug, "Tidak tersedia"), "historical": True, "current": False} for slug in message.source_ids]
    current = {row.slug: getattr(row, "content_hash", None) for row in current_sources()}
    return [{**row, "historical": True, "current": bool(row.get("content_hash") and current.get(row.get("id")) == row.get("content_hash"))} for row in saved if isinstance(row, dict)]
