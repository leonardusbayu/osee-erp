"""Select published tax-source versions applicable to a reporting period."""

from dataclasses import dataclass
from datetime import date, timedelta

from django.core.exceptions import ValidationError
from django.utils import timezone

from .models import TaxSource, TaxSourcePublication, TaxSourceRevision
from .source_versions import snapshot
from .workflow import digest


@dataclass(frozen=True)
class _SourceCandidate:
    reviewed_on: date
    publication_id: int
    revision: TaxSourceRevision
    publication: TaxSourcePublication
    effective_from: date | None
    effective_until: date | None


def _source_candidate(revision, period, end, today):
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
        return None
    publication = (
        TaxSourcePublication.objects.filter(revision=revision).order_by("-pk").first()
    )
    if (
        publication is None
        or digest(data) != revision.content_hash
        or not 0 <= (today - reviewed).days <= 180
        or (start and start > end)
        or (until and until < period)
        or (start and until and start > until)
    ):
        return None
    return _SourceCandidate(
        reviewed_on=reviewed,
        publication_id=publication.pk,
        revision=revision,
        publication=publication,
        effective_from=start,
        effective_until=until,
    )


def _source_revision_candidates(source, slug, today, period, end):
    if not source:
        return []
    head = source.revisions.filter(content_hash=digest(snapshot(source))).first()
    if (
        head is None
        or digest(head.snapshot) != head.content_hash
        or not TaxSourcePublication.objects.filter(revision=head).exists()
        or not 0 <= (today - source.reviewed_on).days <= 180
    ):
        raise ValidationError(
            f"Sumber {slug} berubah tanpa publikasi yang sesuai. Periksa sumber sebelum memakai riwayatnya."
        )
    candidates = []
    for revision in source.revisions.order_by("-pk"):
        candidate = _source_candidate(revision, period, end, today)
        if candidate:
            candidates.append(candidate)
    return candidates


def _source_period_segments(slug, candidates, period, end, partial_slugs):
    segments = []
    day = period
    begun = False
    while day <= end:
        applicable = [
            candidate
            for candidate in candidates
            if (candidate.effective_from is None or candidate.effective_from <= day)
            and (candidate.effective_until is None or candidate.effective_until >= day)
        ]
        if not applicable:
            if slug not in partial_slugs or begun:
                raise ValidationError(
                    f"Sumber {slug} belum mencakup seluruh tanggal yang diperlukan pada masa ini ({day.isoformat()})."
                )
        else:
            begun = True
            selected = max(
                applicable,
                key=lambda candidate: (
                    candidate.reviewed_on,
                    candidate.publication_id,
                ),
            )
            if segments and segments[-1][0].revision.pk == selected.revision.pk:
                segments[-1] = (selected, segments[-1][1], day)
            else:
                segments.append((selected, day, day))
        day += timedelta(days=1)
    return segments


def _source_segment_card(slug, candidate, application_from, application_until):
    revision = candidate.revision
    publication = candidate.publication
    return {
        "id": slug,
        "version_id": revision.pk,
        "content_hash": revision.content_hash,
        "publication_id": publication.pk,
        "publication_reference": publication.review_reference,
        "title": revision.snapshot["title"],
        "url": revision.snapshot["url"],
        "article": revision.snapshot["article"],
        "reviewed_on": revision.snapshot["reviewed_on"],
        "effective_from": (
            candidate.effective_from.isoformat() if candidate.effective_from else None
        ),
        "effective_until": (
            candidate.effective_until.isoformat() if candidate.effective_until else None
        ),
        "application_from": application_from.isoformat(),
        "application_until": application_until.isoformat(),
    }


def reviewed_sources_for_period(period, *, slugs, partial_slugs=()):
    """Published, currently reviewed legal identities applicable to this month.

    A future-effective revision cannot substitute for the historical rule. The
    effective interval is retained, including an amendment during the month;
    the professional's package decision must cover the resulting rule mix.
    """
    if type(period) is not date or period.day != 1:
        raise ValidationError("Pilih tanggal pertama masa pajak.")
    end = date(period.year + (period.month == 12), period.month % 12 + 1, 1)
    end -= timedelta(days=1)
    today = timezone.localdate()
    result = []
    for slug in sorted(set(slugs)):
        source = TaxSource.objects.filter(slug=slug, approved=True).first()
        candidates = _source_revision_candidates(source, slug, today, period, end)
        if not candidates:
            raise ValidationError(
                f"Sumber {slug} belum memiliki versi yang diterbitkan, masih ditinjau, dan berlaku untuk masa ini."
            )
        segments = _source_period_segments(slug, candidates, period, end, partial_slugs)
        result.extend(
            _source_segment_card(slug, candidate, application_from, application_until)
            for candidate, application_from, application_until in segments
        )
    return result
