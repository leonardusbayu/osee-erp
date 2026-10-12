"""Course scheduling, attendance, and progress commands."""

from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from finance import services as finance

from .models import CourseAttendance, CourseMeeting, CoursePlan, OrderLine, Registration
from .services import _event, _org, _save, _text, _version


@dataclass(frozen=True)
class CourseAttendanceCommand:
    expected_version: int
    attended: bool
    evidence: str
    reason: str = ""


@transaction.atomic
def create_course_plan(*, organization, actor, line, total_sessions, reason):
    org = _org(organization, actor)
    line = finance._scoped(OrderLine, line, org, lock=True)
    if line.product.kind != "course":
        raise ValidationError("Jadwal beberapa pertemuan hanya untuk produk kursus.")
    if type(total_sessions) is not int or not 1 <= total_sessions <= 200:
        raise ValidationError("Jumlah pertemuan harus antara 1 dan 200.")
    reason = _text(reason, "Bukti jumlah pertemuan", 2000)
    old = CoursePlan.objects.filter(organization=org, line=line).first()
    if old:
        if old.total_sessions != total_sessions:
            raise ValidationError(
                "Rencana kursus sudah dibuat dengan jumlah pertemuan berbeda. Minta Owner memperbaiki rencana."
            )
        return old
    plan = _save(
        CoursePlan(
            organization=org,
            created_by=actor,
            line=line,
            total_sessions=total_sessions,
        )
    )
    _event(
        org,
        actor,
        plan,
        "course.plan_created",
        reason,
        after={"line_id": line.pk, "total_sessions": total_sessions},
    )
    return plan


@transaction.atomic
def revise_course_plan(
    *, organization, actor, plan, total_sessions, expected_version, reason
):
    org = _org(organization, actor)
    finance._owner(org, actor)
    plan = finance._scoped(CoursePlan, plan, org, lock=True)
    _version(plan, expected_version)
    if type(total_sessions) is not int or not 1 <= total_sessions <= 200:
        raise ValidationError("Jumlah pertemuan harus antara 1 dan 200.")
    reason = _text(reason, "Alasan perubahan jumlah pertemuan", 2000)
    scheduled = plan.meetings.count()
    attended = CourseAttendance.objects.filter(
        organization=org,
        meeting__plan=plan,
        status=CourseAttendance.Status.ATTENDED,
    ).count()
    if total_sessions < max(scheduled, attended):
        raise ValidationError(
            "Jumlah pertemuan tidak boleh lebih kecil dari jadwal atau sesi yang sudah dihadiri."
        )
    if total_sessions == plan.total_sessions:
        raise ValidationError("Jumlah pertemuan tidak berubah.")
    before = {"total_sessions": plan.total_sessions, "version": plan.version}
    plan.total_sessions = total_sessions
    plan.version += 1
    _save(plan)
    _event(
        org,
        actor,
        plan,
        "course.plan_revised",
        reason,
        before,
        {"total_sessions": total_sessions, "version": plan.version},
    )
    return plan


@transaction.atomic
def schedule_course_meeting(
    *, organization, actor, plan, starts_at, expected_version, reason
):
    from datetime import datetime

    org = _org(organization, actor)
    plan = finance._scoped(CoursePlan, plan, org, lock=True)
    _version(plan, expected_version)
    if not isinstance(starts_at, datetime) or timezone.is_naive(starts_at):
        raise ValidationError("Waktu pertemuan harus valid dan menyertakan zona waktu.")
    reason = _text(reason, "Konfirmasi jadwal", 2000)
    meetings = list(plan.meetings.order_by("number"))
    if len(meetings) >= plan.total_sessions:
        raise ValidationError("Semua pertemuan dalam paket sudah dijadwalkan.")
    if meetings and starts_at <= meetings[-1].starts_at:
        raise ValidationError(
            "Jadwalkan pertemuan setelah tanggal dan waktu pertemuan sebelumnya."
        )
    meeting = _save(
        CourseMeeting(
            organization=org,
            created_by=actor,
            plan=plan,
            number=len(meetings) + 1,
            starts_at=starts_at,
        )
    )
    before = {"version": plan.version, "scheduled_sessions": len(meetings)}
    plan.version += 1
    _save(plan)
    _event(
        org,
        actor,
        plan,
        "course.meeting_scheduled",
        reason,
        before,
        {
            "meeting_id": meeting.pk,
            "number": meeting.number,
            "starts_at": meeting.starts_at.isoformat(),
            "version": plan.version,
        },
    )
    return meeting


@transaction.atomic
def reschedule_course_meeting(
    *, organization, actor, meeting, starts_at, expected_version, reason
):
    from datetime import datetime

    org = _org(organization, actor)
    meeting = finance._scoped(CourseMeeting, meeting, org, lock=True)
    _version(meeting, expected_version)
    if not isinstance(starts_at, datetime) or timezone.is_naive(starts_at):
        raise ValidationError("Waktu pertemuan harus valid dan menyertakan zona waktu.")
    reason = _text(reason, "Alasan perubahan jadwal", 2000)
    if meeting.starts_at <= timezone.now() or meeting.attendance_records.exists():
        raise ValidationError(
            "Jadwal hanya dapat diubah sebelum pertemuan dimulai dan sebelum kehadiran dicatat."
        )
    previous = (
        meeting.plan.meetings.filter(number__lt=meeting.number)
        .order_by("-number")
        .first()
    )
    following = (
        meeting.plan.meetings.filter(number__gt=meeting.number)
        .order_by("number")
        .first()
    )
    if (previous and starts_at <= previous.starts_at) or (
        following and starts_at >= following.starts_at
    ):
        raise ValidationError("Pertahankan urutan tanggal dan waktu pertemuan kursus.")
    before = {"starts_at": meeting.starts_at.isoformat(), "version": meeting.version}
    meeting.starts_at, meeting.version = starts_at, meeting.version + 1
    _save(meeting)
    _event(
        org,
        actor,
        meeting,
        "course.meeting_rescheduled",
        reason,
        before,
        {"starts_at": starts_at.isoformat(), "version": meeting.version},
    )
    return meeting


@transaction.atomic
def record_course_attendance(
    *,
    organization,
    actor,
    registration,
    meeting,
    command,
):
    if not isinstance(command, CourseAttendanceCommand):
        raise ValidationError("Data kehadiran kursus tidak valid.")
    org = _org(organization, actor)
    reg = finance._scoped(Registration, registration, org, lock=True)
    meeting = finance._scoped(CourseMeeting, meeting, org, lock=True)
    evidence = _text(command.evidence, "Referensi bukti kehadiran", 2000)
    attended = command.attended
    expected_version = command.expected_version
    if type(attended) is not bool:
        raise ValidationError("Pilih hadir atau tidak hadir.")
    if (
        reg.line.product.kind != "course"
        or meeting.plan.line_id != reg.line_id
        or reg.status == Registration.Status.CANCELLED
    ):
        raise ValidationError("Pertemuan kursus tidak sesuai dengan pendaftaran ini.")
    if meeting.starts_at > timezone.now():
        raise ValidationError("Kehadiran baru dapat dicatat setelah pertemuan dimulai.")
    old = (
        CourseAttendance.objects.select_for_update()
        .filter(organization=org, registration=reg, meeting=meeting)
        .first()
    )
    status = (
        CourseAttendance.Status.ATTENDED if attended else CourseAttendance.Status.ABSENT
    )
    if old:
        if old.status == status and old.evidence == evidence:
            return old
        if type(expected_version) is not int or old.version != expected_version:
            raise ValidationError("Catatan berubah. Muat ulang sebelum mencoba lagi.")
        raise ValidationError(
            "Kehadiran sudah dicatat. Gunakan koreksi dengan alasan untuk mengubahnya."
        )
    if type(expected_version) is not int or expected_version != 0:
        raise ValidationError("Catatan berubah. Muat ulang sebelum menyimpan.")
    attendance = _save(
        CourseAttendance(
            organization=org,
            created_by=actor,
            registration=reg,
            meeting=meeting,
            status=status,
            evidence=evidence,
        )
    )
    _event(
        org,
        actor,
        reg,
        "course.attendance_recorded",
        "Kehadiran pertemuan kursus dicatat",
        {"meeting_id": meeting.pk, "status": "not_recorded"},
        {"meeting_id": meeting.pk, "status": status},
    )
    return attendance


@transaction.atomic
def correct_course_attendance(*, organization, actor, attendance, command):
    if not isinstance(command, CourseAttendanceCommand):
        raise ValidationError("Data koreksi kehadiran kursus tidak valid.")
    org = _org(organization, actor)
    attendance = finance._scoped(CourseAttendance, attendance, org, lock=True)
    _version(attendance, command.expected_version)
    reason = _text(command.reason, "Alasan koreksi kehadiran", 2000)
    evidence = _text(command.evidence, "Bukti pemeriksaan ulang", 2000)
    attended = command.attended
    if type(attended) is not bool or attendance.meeting.starts_at > timezone.now():
        raise ValidationError(
            "Kehadiran hanya dapat dikoreksi setelah pertemuan dimulai."
        )
    status = (
        CourseAttendance.Status.ATTENDED if attended else CourseAttendance.Status.ABSENT
    )
    if attendance.status == status:
        raise ValidationError("Hasil koreksi sama dengan catatan saat ini.")
    before = {
        "meeting_id": attendance.meeting_id,
        "status": attendance.status,
        "evidence": attendance.evidence,
    }
    attendance.status = status
    attendance.evidence = evidence
    attendance.version += 1
    _save(attendance)
    _event(
        org,
        actor,
        attendance.registration,
        "course.attendance_corrected",
        reason,
        before,
        {
            "meeting_id": attendance.meeting_id,
            "status": status,
            "evidence": attendance.evidence,
        },
    )
    return attendance


def course_progress(*, organization, registration):
    org = finance._lock_org(organization)
    reg = finance._scoped(Registration, registration, org)
    if reg.line.product.kind != "course":
        raise ValidationError("Progres pertemuan hanya tersedia untuk produk kursus.")
    plan = CoursePlan.objects.filter(organization=org, line=reg.line).first()
    if not plan:
        return {
            "configured": False,
            "total_sessions": 0,
            "scheduled_sessions": 0,
            "attended_sessions": 0,
            "absent_sessions": 0,
            "remaining_sessions": 0,
            "all_sessions_attended": False,
            "meetings": [],
        }
    meetings = list(plan.meetings.order_by("number"))
    attendance = {
        item.meeting_id: item
        for item in CourseAttendance.objects.filter(
            organization=org, registration=reg, meeting__plan=plan
        )
    }
    attended = sum(
        item.status == CourseAttendance.Status.ATTENDED for item in attendance.values()
    )
    absent = sum(
        item.status == CourseAttendance.Status.ABSENT for item in attendance.values()
    )
    details = [
        {
            "meeting": meeting,
            "attendance": attendance.get(meeting.pk),
            "status": (
                attendance[meeting.pk].status
                if meeting.pk in attendance
                else "not_recorded"
            ),
        }
        for meeting in meetings
    ]
    return {
        "configured": True,
        "plan": plan,
        "total_sessions": plan.total_sessions,
        "scheduled_sessions": len(meetings),
        "attended_sessions": attended,
        "absent_sessions": absent,
        "remaining_sessions": plan.total_sessions - attended,
        "all_sessions_attended": attended == plan.total_sessions,
        "meetings": details,
    }
