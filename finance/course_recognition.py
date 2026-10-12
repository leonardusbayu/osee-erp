"""Keep course service obligations out of period close until their treatment is settled."""

from django.db.models import Count, Q

from operations.models import CourseAttendance, CoursePlan, Registration


def course_period_close_blocker(*, organization, invoice, cutoff, remaining_quantity):
    """Return the finance close message for an issued course invoice, if any."""
    if invoice.product.kind != "course":
        return None

    plan = CoursePlan.objects.filter(
        organization=organization,
        line__invoice=invoice,
    ).first()
    past_meetings = (
        plan.meetings.filter(starts_at__date__lte=cutoff).count() if plan else 0
    )
    if (
        past_meetings
        and Registration.objects.filter(
            organization=organization,
            line__invoice=invoice,
        )
        .exclude(status="cancelled")
        .annotate(
            attended_sessions=Count(
                "course_attendances",
                filter=Q(
                    course_attendances__meeting__plan=plan,
                    course_attendances__meeting__starts_at__date__lte=cutoff,
                    course_attendances__status=CourseAttendance.Status.ATTENDED,
                ),
            )
        )
        .filter(attended_sessions__lt=past_meetings)
        .exists()
    ):
        return (
            "Ada pertemuan kursus yang lewat tetapi belum tercatat dihadiri oleh semua peserta. "
            "Periksa daftar hadir atau jadwal pengganti sebelum tutup bulan."
        )

    attended_sessions = CourseAttendance.objects.filter(
        organization=organization,
        registration__line__invoice=invoice,
        status=CourseAttendance.Status.ATTENDED,
        meeting__starts_at__date__lte=cutoff,
    ).exists()
    if attended_sessions:
        return (
            "Ada pertemuan kursus yang sudah dihadiri. Tutup bulan setelah Finance "
            "menetapkan dan menerapkan kebijakan pendapatan kursus yang disetujui."
        )

    has_future_meeting = bool(
        plan and plan.meetings.filter(starts_at__date__gt=cutoff).exists()
    )
    if remaining_quantity > 0 and not has_future_meeting:
        return (
            "Kursus yang sudah ditagih belum memiliki jadwal pertemuan berikutnya. "
            "Periksa jadwal peserta sebelum menutup bulan."
        )
    return None
