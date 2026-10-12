"""Immutable tax source versions and publication history."""

from datetime import date

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max, Subquery
from django.utils import timezone

from .models import TaxSource, TaxSourcePublication, TaxSourceRevision
from .workflow import digest, json_value


def snapshot(source):
    return json_value(
        {
            name: getattr(source, name)
            for name in (
                "slug",
                "title",
                "url",
                "issuer",
                "article",
                "summary",
                "topics",
                "reviewed_on",
                "effective_from",
                "effective_until",
            )
        }
    )


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
    for item in sorted(catalog, key=lambda value: value["slug"]):
        values = dict(item)
        slug = values.pop("slug")
        item_reviewed_on = values.pop("reviewed_on", reviewed_on)
        source, created = TaxSource.objects.select_for_update().get_or_create(
            slug=slug,
            defaults={**values, "reviewed_on": item_reviewed_on, "approved": False},
        )
        if not created:
            _revision(source)
        candidate = {
            **snapshot(source),
            **json_value(values),
            "reviewed_on": item_reviewed_on.isoformat(),
        }
        revision = _revision(source, candidate)
        if (
            reviewed_by
            and review_reference
            and (created or source.approved)
            and not TaxSourcePublication.objects.filter(revision=revision).exists()
        ):
            activate_source_revision(
                revision=revision,
                reviewer_name=reviewed_by,
                review_reference=review_reference,
                confirm_reviewed=True,
            )


@transaction.atomic
def activate_source_revision(
    *, revision, reviewer_name, review_reference, confirm_reviewed=False, actor=None
):
    if confirm_reviewed is not True or any(
        not isinstance(value, str) or len(value.strip()) < 3
        for value in (reviewer_name, review_reference)
    ):
        raise ValidationError(
            "Catat pemeriksa dan referensi pemeriksaan sumber; jangan mengaktifkan otomatis."
        )
    revision = TaxSourceRevision.objects.select_for_update().get(pk=revision.pk)
    if TaxSourcePublication.objects.filter(revision=revision).exists():
        raise ValidationError("Versi ini sudah pernah diterbitkan.")
    if (
        not isinstance(revision.snapshot, dict)
        or digest(revision.snapshot) != revision.content_hash
    ):
        raise ValidationError("Isi versi sumber tidak cocok dengan rekam jejaknya.")
    source = TaxSource.objects.select_for_update().get(pk=revision.source_id)
    data = dict(revision.snapshot)
    if set(data) != set(snapshot(source)):
        raise ValidationError("Versi sumber tidak lengkap untuk diperiksa.")
    try:
        reviewed_on = date.fromisoformat(data["reviewed_on"])
        effective_from = (
            date.fromisoformat(data["effective_from"])
            if data.get("effective_from")
            else None
        )
        effective_until = (
            date.fromisoformat(data["effective_until"])
            if data.get("effective_until")
            else None
        )
    except (KeyError, TypeError, ValueError):
        raise ValidationError("Tanggal pada versi sumber tidak valid.") from None
    if data.get("slug") != source.slug:
        raise ValidationError("Versi ini tidak cocok dengan sumber yang dipilih.")
    if effective_from and effective_until and effective_from > effective_until:
        raise ValidationError("Tanggal berlaku sumber tidak berurutan.")
    if not 0 <= (timezone.localdate() - reviewed_on).days <= 180:
        raise ValidationError(
            "Versi sumber di luar masa tinjauan; siapkan versi tinjauan terbaru."
        )
    for name, value in data.items():
        if name in ("reviewed_on", "effective_from", "effective_until"):
            value = date.fromisoformat(value) if value else None
        setattr(source, name, value)
    source.approved = True
    source.full_clean()
    source.save()
    publication = TaxSourcePublication(
        revision=revision,
        reviewer_name=reviewer_name.strip(),
        review_reference=review_reference.strip(),
        published_by=actor,
    )
    publication._knowledge_write = True
    publication.save()
    return publication


def pending_source_revisions():
    """Return only each source's latest version that has not been published."""
    latest_revision_ids = (
        TaxSourceRevision.objects.order_by()
        .values("source_id")
        .annotate(latest_id=Max("pk"))
        .values("latest_id")
    )
    published_revision_ids = TaxSourcePublication.objects.order_by().values(
        "revision_id"
    )
    return (
        TaxSourceRevision.objects.filter(pk__in=Subquery(latest_revision_ids))
        .exclude(pk__in=Subquery(published_revision_ids))
        .select_related("source")
        .order_by("source__title", "pk")
    )


def current_sources(topic=None, slugs=None):
    today = timezone.localdate()
    query = TaxSource.objects.filter(approved=True)
    if slugs is not None:
        query = query.filter(slug__in=slugs)
    rows = []
    for source in query:
        if (
            not 0 <= (today - source.reviewed_on).days <= 180
            or (source.effective_from and source.effective_from > today)
            or (source.effective_until and source.effective_until < today)
            or (topic and topic not in source.topics)
        ):
            continue
        data = snapshot(source)
        revision = source.revisions.filter(content_hash=digest(data)).first()
        if (
            revision is None
            or not TaxSourcePublication.objects.filter(revision=revision).exists()
        ):
            continue  # Staging or retaining a legacy flag is not a publication review.
        source.version_id, source.content_hash = revision.pk, revision.content_hash
        rows.append(source)
    return rows


def source_cards(sources):
    return [
        {
            "id": row.slug,
            "title": row.title,
            "url": row.url,
            "article": row.article,
            "reviewed_on": row.reviewed_on.isoformat(),
            "version_id": getattr(row, "version_id", None),
            "content_hash": getattr(row, "content_hash", None),
        }
        for row in sources
    ]


def historical_cards(message):
    metadata = message.metadata if isinstance(message.metadata, dict) else {}
    saved = metadata.get("source_cards")
    review_dates = metadata.get("source_review_dates")
    if not isinstance(review_dates, dict):
        review_dates = {}
    if not isinstance(saved, list):
        return [
            {
                "id": slug,
                "title": slug + " — versi historis tidak tersimpan",
                "url": "",
                "article": "Jangan gunakan jawaban lama untuk keputusan baru.",
                "reviewed_on": review_dates.get(slug, "Tidak tersedia"),
                "historical": True,
                "current": False,
            }
            for slug in message.source_ids
        ]
    current = {
        row.slug: getattr(row, "content_hash", None) for row in current_sources()
    }
    return [
        {
            **row,
            "historical": True,
            "current": bool(
                row.get("content_hash")
                and current.get(row.get("id")) == row.get("content_hash")
            ),
        }
        for row in saved
        if isinstance(row, dict)
    ]


def reviewed_sources_for_date(point, *, slugs):
    """Exact tax-point selection; a later-month amendment is not backdated."""
    if type(point) is not date:
        raise ValidationError(
            "Tanggal terutang harus berupa tanggal kalender, bukan waktu atau bulan perkiraan."
        )
    today = timezone.localdate()
    result = []
    for slug in sorted(set(slugs)):
        source = TaxSource.objects.filter(slug=slug, approved=True).first()
        if source is None:
            raise ValidationError(
                f"Sumber {slug} belum diterbitkan setelah pemeriksaan."
            )
        head = source.revisions.filter(content_hash=digest(snapshot(source))).first()
        if (
            head is None
            or digest(head.snapshot) != head.content_hash
            or not TaxSourcePublication.objects.filter(revision=head).exists()
            or not 0 <= (today - source.reviewed_on).days <= 180
        ):
            raise ValidationError(
                f"Sumber {slug} berubah atau perlu pemeriksaan terbaru."
            )
        candidates = []
        for revision in source.revisions.all():
            data = revision.snapshot
            try:
                reviewed = date.fromisoformat(data["reviewed_on"])
                start = (
                    date.fromisoformat(data["effective_from"])
                    if data.get("effective_from")
                    else None
                )
                until = (
                    date.fromisoformat(data["effective_until"])
                    if data.get("effective_until")
                    else None
                )
            except (KeyError, ValueError, TypeError):
                continue
            publication = (
                TaxSourcePublication.objects.filter(revision=revision)
                .order_by("-pk")
                .first()
            )
            if (
                publication
                and digest(data) == revision.content_hash
                and 0 <= (today - reviewed).days <= 180
                and (start is None or start <= point)
                and (until is None or point <= until)
                and not (start and until and start > until)
            ):
                candidates.append((reviewed, publication.pk, revision, publication))
        if not candidates:
            raise ValidationError(
                f"Sumber {slug} belum mempunyai versi yang ditinjau dan berlaku pada tanggal terutang."
            )
        _, _, revision, publication = max(
            candidates, key=lambda item: (item[0], item[1])
        )
        data = revision.snapshot
        result.append(
            {
                "id": slug,
                "version_id": revision.pk,
                "content_hash": revision.content_hash,
                "publication_id": publication.pk,
                "publication_reference": publication.review_reference,
                "title": data["title"],
                "url": data["url"],
                "article": data["article"],
                "reviewed_on": data["reviewed_on"],
                "effective_from": data.get("effective_from"),
                "effective_until": data.get("effective_until"),
                "application_from": point.isoformat(),
                "application_until": point.isoformat(),
            }
        )
    return result
