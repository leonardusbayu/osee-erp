from collections import defaultdict

from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from core.access import organization_required

from . import course_services, forms
from .models import (
    CourseAttendance,
    CourseMeeting,
    CoursePlan,
    OperationEvent,
    OrderLine,
    Registration,
)
from .view_helpers import _access, _error, _form_page


def _course_attendance_history(organization, registrations, meetings):
    if not registrations or not meetings:
        return {}
    events = (
        OperationEvent.objects.filter(
            organization=organization,
            object_type="registration",
            object_id__in=[registration.pk for registration in registrations],
            action__in=["course.attendance_recorded", "course.attendance_corrected"],
        )
        .select_related("created_by")
        .order_by("created_at", "pk")
    )
    history = defaultdict(list)
    for event in events:
        meeting_id = event.after.get("meeting_id")
        if type(meeting_id) is int:
            history[(event.object_id, meeting_id)].append(event)
    return history


def _course_meeting_history(organization, meetings):
    if not meetings:
        return {}
    events = (
        OperationEvent.objects.filter(
            organization=organization,
            object_type="coursemeeting",
            object_id__in=[meeting.pk for meeting in meetings],
            action="course.meeting_rescheduled",
        )
        .select_related("created_by")
        .order_by("created_at", "pk")
    )
    history = defaultdict(list)
    for event in events:
        history[event.object_id].append(event)
    return history


def _course_registration_rows(registrations, meetings, attendance_map, plan):
    rows = []
    for registration in registrations:
        statuses = [
            attendance_map.get((registration.pk, meeting.pk)) for meeting in meetings
        ]
        attended = sum(
            bool(item and item.status == CourseAttendance.Status.ATTENDED)
            for item in statuses
        )
        rows.append(
            {
                "registration": registration,
                "attended": attended,
                "remaining": max((plan.total_sessions if plan else 0) - attended, 0),
                "meeting_rows": [
                    {
                        "meeting": meeting,
                        "attendance": attendance_map.get((registration.pk, meeting.pk)),
                    }
                    for meeting in meetings
                ],
            }
        )
    return rows


def _course_plan_display_data(organization, line, plan):
    registrations = list(
        Registration.objects.filter(organization=organization, line=line)
        .exclude(status=Registration.Status.CANCELLED)
        .select_related("participant")
        .order_by("pk")
    )
    meetings = list(plan.meetings.order_by("number") if plan else [])
    attendance_map = (
        {
            (record.registration_id, record.meeting_id): record
            for record in CourseAttendance.objects.filter(
                organization=organization,
                registration__in=registrations,
                meeting__in=meetings,
            )
        }
        if registrations and meetings
        else {}
    )
    attendance_history = _course_attendance_history(
        organization, registrations, meetings
    )
    meeting_history = _course_meeting_history(organization, meetings)
    for meeting in meetings:
        meeting.can_reschedule = (
            meeting.starts_at > timezone.now()
            and not meeting.attendance_records.exists()
        )
        meeting.participant_rows = [
            {
                "registration": registration,
                "attendance": attendance_map.get((registration.pk, meeting.pk)),
                "history": attendance_history.get((registration.pk, meeting.pk), []),
            }
            for registration in registrations
        ]
        meeting.history = meeting_history.get(meeting.pk, [])
    return (
        _course_registration_rows(registrations, meetings, attendance_map, plan),
        meetings,
    )


def _submit_course_plan(request, line, plan, page_forms):
    action = request.POST.get("action")
    if action == "configure" and not plan:
        name, form_class = "configure", forms.CoursePlanForm
    elif action == "schedule" and plan:
        name, form_class = "meeting", forms.CourseMeetingForm
    elif action == "revise" and plan:
        name, form_class = "revision", forms.CoursePlanRevisionForm
    else:
        fallback = page_forms["configure"] if not plan else page_forms["meeting"]
        fallback.add_error(None, "Perbarui halaman lalu pilih tindakan yang tersedia.")
        return None

    form = form_class(request.POST)
    page_forms[name] = form
    if not form.is_valid():
        return None
    try:
        if name == "configure":
            course_services.create_course_plan(
                organization=request.organization,
                actor=request.user,
                line=line,
                **form.cleaned_data,
            )
        elif name == "meeting":
            course_services.schedule_course_meeting(
                organization=request.organization,
                actor=request.user,
                plan=plan,
                **form.cleaned_data,
            )
        else:
            course_services.revise_course_plan(
                organization=request.organization,
                actor=request.user,
                plan=plan,
                **form.cleaned_data,
            )
    except ValidationError as exc:
        _error(form, exc)
        return None
    return redirect("operations:course_plan", line_pk=line.pk)


@organization_required
@require_http_methods(["GET", "POST"])
def course_plan(request, line_pk):
    _access(request)
    line = get_object_or_404(
        OrderLine.objects.select_related("product", "order__buyer"),
        organization=request.organization,
        pk=line_pk,
    )
    if line.product.kind != "course":
        raise Http404
    plan = CoursePlan.objects.filter(
        organization=request.organization, line=line
    ).first()
    configure_form = forms.CoursePlanForm()
    meeting_form = forms.CourseMeetingForm(
        initial={"expected_version": plan.version if plan else 0}
    )
    revision_form = forms.CoursePlanRevisionForm(
        initial={
            "total_sessions": plan.total_sessions if plan else "",
            "expected_version": plan.version if plan else 0,
        }
    )

    if request.method == "POST":
        page_forms = {
            "configure": configure_form,
            "meeting": meeting_form,
            "revision": revision_form,
        }
        response = _submit_course_plan(request, line, plan, page_forms)
        if response:
            return response
        configure_form = page_forms["configure"]
        meeting_form = page_forms["meeting"]
        revision_form = page_forms["revision"]

    registration_rows, meetings = _course_plan_display_data(
        request.organization, line, plan
    )
    return render(
        request,
        "operations/course_plan.html",
        {
            "line": line,
            "plan": plan,
            "registrations": registration_rows,
            "meetings": meetings,
            "configure_form": configure_form,
            "meeting_form": meeting_form,
            "revision_form": revision_form,
            "can_revise": request.membership.role == "owner",
            "active_nav": "operations",
        },
    )


@organization_required
@require_http_methods(["GET", "POST"])
def course_meeting_reschedule(request, meeting_pk):
    _access(request)
    meeting = get_object_or_404(
        CourseMeeting.objects.select_related("plan__line__order"),
        organization=request.organization,
        pk=meeting_pk,
    )
    if meeting.starts_at <= timezone.now() or meeting.attendance_records.exists():
        raise Http404
    course_plan_url = reverse("operations:course_plan", args=[meeting.plan.line_id])
    form = forms.CourseMeetingForm(
        request.POST or None,
        initial={
            "expected_version": meeting.version,
            "starts_at": timezone.localtime(meeting.starts_at).strftime(
                "%Y-%m-%dT%H:%M"
            ),
        },
    )
    if request.method == "POST" and form.is_valid():
        try:
            course_services.reschedule_course_meeting(
                organization=request.organization,
                actor=request.user,
                meeting=meeting,
                **form.cleaned_data,
            )
            return redirect(course_plan_url)
        except ValidationError as exc:
            _error(form, exc)
    return _form_page(
        request,
        form,
        "Ubah jadwal pertemuan kursus",
        course_plan_url,
        "Perubahan hanya tersedia sebelum pertemuan dimulai dan sebelum kehadiran dicatat. Simpan alasan perubahan agar riwayat dapat diperiksa.",
        action="Simpan jadwal baru",
    )


@organization_required
@require_http_methods(["GET", "POST"])
def course_attendance(request, registration_pk, meeting_pk):
    _access(request)
    registration = get_object_or_404(
        Registration.objects.select_related("line__product", "line__order"),
        organization=request.organization,
        pk=registration_pk,
    )
    meeting = get_object_or_404(
        CourseMeeting.objects.select_related("plan__line__product"),
        organization=request.organization,
        pk=meeting_pk,
    )
    if (
        registration.line.product.kind != "course"
        or meeting.plan.line_id != registration.line_id
    ):
        raise Http404
    attendance = CourseAttendance.objects.filter(
        organization=request.organization,
        registration=registration,
        meeting=meeting,
    ).first()
    correction = attendance is not None
    form_class = (
        forms.CourseAttendanceCorrectionForm
        if correction
        else forms.CourseAttendanceForm
    )
    initial = {
        "expected_version": attendance.version if attendance else 0,
        "attended": "yes" if attendance and attendance.status == "attended" else "no",
        "evidence": attendance.evidence if attendance else "",
    }
    if correction:
        initial["reason"] = ""
    form = form_class(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            data = dict(form.cleaned_data)
            data.pop("confirm", None)
            command = course_services.CourseAttendanceCommand(**data)
            if correction:
                course_services.correct_course_attendance(
                    organization=request.organization,
                    actor=request.user,
                    attendance=attendance,
                    command=command,
                )
            else:
                course_services.record_course_attendance(
                    organization=request.organization,
                    actor=request.user,
                    registration=registration,
                    meeting=meeting,
                    command=command,
                )
            return redirect("operations:course_plan", line_pk=registration.line_id)
        except ValidationError as exc:
            _error(form, exc)
    return _form_page(
        request,
        form,
        "Koreksi kehadiran pertemuan" if correction else "Catat kehadiran pertemuan",
        reverse("operations:course_plan", args=[registration.line_id]),
        "Catatan ini membuktikan pelaksanaan kursus. Pencatatan kehadiran tidak membuat jurnal pendapatan atau menyatakan kursus selesai.",
        action="Simpan koreksi" if correction else "Simpan kehadiran",
    )
