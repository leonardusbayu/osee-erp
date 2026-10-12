"""Manual publication facts; these commands do not publish or fetch remote content."""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .activities import require_work_responsibility
from .activity_models import WorkActivity
from .services import _audit, _org, _scoped, _version
from .workflow import _date, _reason, _time, _workflow_save
from .workflow_content_models import ContentMeasurement, ContentPublication


@transaction.atomic
def record_publication(
    *,
    organization,
    actor,
    activity,
    expected_version,
    expected_latest_id,
    published_at,
    url,
    topic,
    evidence,
    correction_reason="",
    responsibility_reason="",
):
    org = _org(organization, actor)
    work = _scoped(WorkActivity, activity, org)
    _version(work, expected_version)
    responsibility = require_work_responsibility(
        organization=org, actor=actor, work=work, reason=responsibility_reason
    )
    if (
        work.kind not in {"content", "seo"}
        or work.status != "accepted"
        or WorkActivity.objects.filter(replaces=work).exists()
    ):
        raise ValidationError(
            "Publikasi memerlukan pekerjaan Konten/SEO yang diterima pemeriksa dan belum diganti. Selesaikan pemeriksaan pekerjaan terlebih dahulu."
        )
    latest = work.publications.first()
    if (
        expected_latest_id is not None and type(expected_latest_id) is not int
    ) or expected_latest_id != getattr(latest, "pk", None):
        raise ValidationError(
            "Publikasi telah berubah. Muat ulang sebelum mencatat versi baru."
        )
    occurred = _time(published_at)
    if occurred > timezone.now() or occurred.replace(
        microsecond=0
    ) < work.reviewed_at.replace(microsecond=0):
        raise ValidationError(
            "Waktu publikasi harus setelah pengesahan pekerjaan dan tidak boleh di masa depan."
        )
    if latest:
        correction_reason = _reason(correction_reason)
    elif correction_reason:
        raise ValidationError("Alasan koreksi hanya untuk versi pengganti.")
    url = _reason(url, 600)
    if not url.startswith(("https://", "http://")):
        raise ValidationError(
            "Gunakan URL publikasi HTTP atau HTTPS yang dapat diperiksa."
        )
    item = _workflow_save(
        ContentPublication(
            organization=org,
            created_by=actor,
            activity=work,
            version=(latest.version + 1) if latest else 1,
            published_at=occurred,
            url=url,
            topic=_reason(topic, 250),
            evidence=_reason(evidence, 4000),
            supersedes=latest,
            correction_reason=correction_reason,
            responsibility_reason=responsibility,
            approval={
                "activity_version": work.version,
                "reviewed_by_id": work.reviewed_by_id,
                "reviewed_at": work.reviewed_at.isoformat(),
                "review_notes": work.review_notes,
            },
        )
    )
    _audit(
        org,
        actor,
        "content.published" if not latest else "content.publication_corrected",
        item,
        activity_id=work.pk,
        version=item.version,
        supersedes_id=getattr(latest, "pk", None),
        approval=item.approval,
        published_at=occurred.isoformat(),
        url=url,
        reason=correction_reason,
        responsibility_reason=responsibility,
    )
    return item


@transaction.atomic
def record_measurement(
    *,
    organization,
    actor,
    publication,
    period_start,
    period_end,
    source,
    reference,
    evidence,
    impressions=None,
    clicks=None,
    visits=None,
    supersedes=None,
    correction_reason="",
    responsibility_reason="",
):
    org = _org(organization, actor)
    publication = _scoped(ContentPublication, publication, org)
    work = _scoped(WorkActivity, publication.activity_id, org)
    responsibility = require_work_responsibility(
        organization=org, actor=actor, work=work, reason=responsibility_reason
    )
    start, end = _date(period_start), _date(period_end)
    if (
        start > end
        or end > timezone.localdate()
        or start < timezone.localdate(publication.published_at)
        or (end - start).days > 366
    ):
        raise ValidationError(
            "Periode pengukuran harus sejak publikasi hingga hari ini, berurutan, maksimal 366 hari."
        )
    metrics = {"impressions": impressions, "clicks": clicks, "visits": visits}
    if all(value is None for value in metrics.values()) or any(
        value is not None and (type(value) is not int or not 0 <= value <= 2147483647)
        for value in metrics.values()
    ):
        raise ValidationError(
            "Isi minimal satu angka sumber nonnegatif. Kosong berarti belum diketahui, bukan nol."
        )
    previous = (
        None if supersedes is None else _scoped(ContentMeasurement, supersedes, org)
    )
    if previous:
        correction_reason = _reason(correction_reason)
    elif correction_reason:
        raise ValidationError(
            "Pilih pengukuran lama ketika menambahkan alasan koreksi."
        )
    fields = dict(
        publication=publication,
        period_start=start,
        period_end=end,
        source=_reason(source, 180),
        reference=_reason(reference, 180),
        evidence=_reason(evidence, 4000),
        supersedes=previous,
        correction_reason=correction_reason,
        **metrics,
    )
    existing = ContentMeasurement.objects.filter(
        organization=org, reference=fields["reference"]
    ).first()
    if existing:
        if any(getattr(existing, key) != value for key, value in fields.items()):
            raise ValidationError(
                "Referensi sumber sudah mempunyai nilai berbeda. Catat koreksi dengan referensi baru dan alasan."
            )
        return existing
    if previous and ContentMeasurement.objects.filter(supersedes=previous).exists():
        raise ValidationError("Pengukuran telah dikoreksi. Pilih versi paling baru.")
    if ContentPublication.objects.filter(supersedes=publication).exists():
        raise ValidationError(
            "Publikasi ini sudah diganti. Catat pengukuran baru pada versi publikasi terbaru."
        )
    item = _workflow_save(
        ContentMeasurement(
            organization=org,
            created_by=actor,
            responsibility_reason=responsibility,
            **fields,
        )
    )
    _audit(
        org,
        actor,
        "content.measured" if not previous else "content.measurement_corrected",
        item,
        activity_id=work.pk,
        publication_id=publication.pk,
        reference=item.reference,
        source=item.source,
        period_start=str(start),
        period_end=str(end),
        metrics=metrics,
        supersedes_id=getattr(previous, "pk", None),
        reason=correction_reason,
        responsibility_reason=responsibility,
    )
    return item
