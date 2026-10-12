"""Daily marketing work with traceable responsibility and decisions."""

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods

from core.access import organization_required, require_role

from . import workflow
from .models import Campaign, Lead, TeamMember
from .views import (
    READ,
    WRITE,
    _bad,
    _can_execute_lead,
    _context,
    _form_error,
    _page,
    _pagination_query,
    _version,
)
from .workflow_forms import (
    AssignmentForm,
    CampaignTransitionForm,
    ContactForm,
    DelegationForm,
    MemberAccountForm,
    ReasonForm,
    ResponseCorrectionForm,
    WorkPolicyForm,
)
from .workflow_models import LeadContact, MemberDelegation


def _form(request, form, title, description, nav, back, *, version=None, note=""):
    return render(
        request,
        "marketing/form.html",
        _context(
            request,
            nav,
            form=form,
            page_title=title,
            description=description,
            section_label="ALUR KERJA",
            back_url=back,
            expected_version=version,
            note_title="Riwayat tetap dapat diperiksa",
            note=note,
        ),
    )


@require_http_methods(["GET", "POST"])
@organization_required
def member_detail(request, pk):
    require_role(request, *READ)
    member = get_object_or_404(
        TeamMember.objects.select_related("account"),
        organization=request.organization,
        pk=pk,
    )
    operation = request.POST.get("operation", "") if request.method == "POST" else ""
    account_form = MemberAccountForm(
        request.POST if operation == "configure" else None,
        organization=request.organization,
        initial={"account": member.account, "active": member.active},
    )
    delegation_form = DelegationForm(
        request.POST if operation == "delegate" else None,
        organization=request.organization,
        initial={"starts_on": timezone.localdate(), "ends_on": timezone.localdate()},
    )
    if request.method == "POST":
        require_role(request, "owner")
        if operation not in {"configure", "delegate"}:
            return _bad(ValidationError("Tindakan anggota tidak valid."))
        form = account_form if operation == "configure" else delegation_form
        if form.is_valid():
            try:
                function = (
                    workflow.configure_member
                    if operation == "configure"
                    else workflow.grant_delegation
                )
                function(
                    organization=request.organization,
                    actor=request.user,
                    member=member,
                    expected_version=_version(request),
                    **form.cleaned_data,
                )
                messages.success(
                    request,
                    "Pengaturan tim tersimpan dengan alasan dan identitas pelaku.",
                )
                return redirect("marketing:member_detail", pk=pk)
            except ValidationError as exc:
                _form_error(form, exc)
    return render(
        request,
        "marketing/member_detail.html",
        _context(
            request,
            "members",
            member=member,
            account_form=account_form,
            delegation_form=delegation_form,
            can_configure=request.membership.role == "owner",
            delegations=member.delegations.select_related(
                "delegate", "created_by"
            ).order_by("-created_at"),
        ),
    )


@require_http_methods(["GET", "POST"])
@organization_required
def delegation_revoke(request, pk):
    require_role(request, "owner")
    item = get_object_or_404(MemberDelegation, organization=request.organization, pk=pk)
    form = ReasonForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        try:
            workflow.revoke_delegation(
                organization=request.organization,
                actor=request.user,
                delegation=item,
                expected_version=_version(request),
                **form.cleaned_data,
            )
            messages.success(
                request,
                "Delegasi dicabut. Riwayat pelaksanaan sebelumnya tetap tersimpan.",
            )
            return redirect("marketing:member_detail", pk=item.member_id)
        except ValidationError as exc:
            _form_error(form, exc)
    return _form(
        request,
        form,
        "Cabut delegasi",
        "Akun pengganti tidak lagi bertindak atas PIC melalui delegasi ini.",
        "members",
        reverse("marketing:member_detail", args=[item.member_id]),
        version=item.version,
    )


@require_http_methods(["GET", "POST"])
@organization_required
def lead_assign(request, pk):
    require_role(request, *workflow.MANAGERS)
    lead = get_object_or_404(Lead, organization=request.organization, pk=pk)
    form = AssignmentForm(
        request.POST if request.method == "POST" else None,
        organization=request.organization,
    )
    if request.method == "POST" and form.is_valid():
        try:
            workflow.assign_lead(
                organization=request.organization,
                actor=request.user,
                lead=lead,
                expected_version=_version(request),
                **form.cleaned_data,
            )
            messages.success(
                request,
                "PIC sekarang diperbarui. Atribusi awal dan riwayat tetap dipertahankan.",
            )
            return redirect("marketing:lead_detail", pk=pk)
        except ValidationError as exc:
            _form_error(form, exc)
    return _form(
        request,
        form,
        "Tugaskan / alihkan prospek",
        lead.name,
        "leads",
        reverse("marketing:lead_detail", args=[pk]),
        version=lead.version,
        note="Penugasan berlaku saat disimpan. PIC pada saat prospek pertama dicatat tetap menjadi dasar atribusi KPI lama; alih tugas tidak memindahkan hasil historis diam-diam.",
    )


@require_http_methods(["GET", "POST"])
@organization_required
def contact_create(request, pk):
    return _contact(request, pk)


@require_http_methods(["GET", "POST"])
@organization_required
def contact_correct(request, pk):
    event = get_object_or_404(
        LeadContact.objects.select_related("lead"),
        organization=request.organization,
        pk=pk,
    )
    return _contact(request, event.lead_id, correction=event)


def _contact(request, pk, correction=None):
    require_role(request, *WRITE)
    lead = get_object_or_404(Lead, organization=request.organization, pk=pk)
    initial = {
        "occurred_at": timezone.localtime().replace(microsecond=0),
        "followup_action": "keep",
    }
    if correction:
        initial.update(
            {
                field: getattr(correction, field)
                for field in (
                    "occurred_at",
                    "method",
                    "outcome",
                    "summary",
                    "evidence",
                    "loss_reason",
                )
            }
        )
    form = ContactForm(
        request.POST if request.method == "POST" else None,
        organization=request.organization,
        lead=lead,
        correction=bool(correction),
        initial=initial,
    )
    if request.method == "POST" and form.is_valid():
        try:
            workflow.record_contact(
                organization=request.organization,
                actor=request.user,
                lead=lead,
                expected_version=_version(request),
                corrects=correction,
                **form.cleaned_data,
            )
            messages.success(
                request,
                "Riwayat kontak disimpan. Bukti pembayaran tetap mengikuti Finance.",
            )
            return redirect("marketing:lead_detail", pk=pk)
        except ValidationError as exc:
            _form_error(form, exc)
    return _form(
        request,
        form,
        "Koreksi riwayat kontak" if correction else "Catat kontak & janji berikutnya",
        lead.name,
        "leads",
        reverse("marketing:lead_detail", args=[pk]),
        version=lead.version,
        note="Setiap kontak menyimpan pelaku, waktu, hasil, dan bukti. Koreksi menambah catatan pengganti; catatan asli tidak dihapus. Sepakat membeli belum berarti lunas.",
    )


@require_http_methods(["GET", "POST"])
@organization_required
def response_correct(request, pk):
    require_role(request, *WRITE)
    lead = get_object_or_404(Lead, organization=request.organization, pk=pk)
    form = ResponseCorrectionForm(
        request.POST if request.method == "POST" else None,
        organization=request.organization,
        initial={"first_response_at": lead.first_response_at},
    )
    if request.method == "POST" and form.is_valid():
        try:
            workflow.correct_first_response(
                organization=request.organization,
                actor=request.user,
                lead=lead,
                expected_version=_version(request),
                **form.cleaned_data,
            )
            messages.success(
                request,
                "Koreksi waktu respons dicatat. Evaluasi KPI lama dapat memerlukan pemeriksaan ulang.",
            )
            return redirect("marketing:lead_detail", pk=pk)
        except ValidationError as exc:
            _form_error(form, exc)
    return _form(
        request,
        form,
        "Koreksi waktu respons",
        lead.name,
        "leads",
        reverse("marketing:lead_detail", args=[pk]),
        version=lead.version,
        note="Gunakan waktu pada bukti yang diperiksa. Alasan dan nilai sebelum/sesudah tersimpan; jangan memperbaiki skor dengan menebak waktu respons.",
    )


@require_GET
@organization_required
def lead_history(request, pk):
    require_role(request, *READ)
    lead = get_object_or_404(Lead, organization=request.organization, pk=pk)
    try:
        page = _page(
            request,
            lead.contact_events.select_related(
                "created_by", "corrects", "completes"
            ).order_by("-created_at", "-pk"),
        )
    except ValidationError as exc:
        return _bad(exc)
    return render(
        request,
        "marketing/lead_history.html",
        _context(
            request,
            "leads",
            lead=lead,
            page=page,
            pagination_query=_pagination_query(request),
            can_execute=_can_execute_lead(request, lead),
            assignments=lead.assignment_events.select_related(
                "previous_member", "member", "created_by"
            ),
        ),
    )


@require_http_methods(["GET", "POST"])
@organization_required
def campaign_detail(request, pk):
    require_role(request, *READ)
    campaign = get_object_or_404(
        Campaign.objects.select_related("owner", "current_owner", "product"),
        organization=request.organization,
        pk=pk,
    )
    form = CampaignTransitionForm(
        request.POST if request.method == "POST" else None,
        organization=request.organization,
        initial={"effective_on": timezone.localdate()},
    )
    if request.method == "POST":
        require_role(request, *workflow.MANAGERS)
        if form.is_valid():
            try:
                workflow.transition_campaign(
                    organization=request.organization,
                    actor=request.user,
                    campaign=campaign,
                    expected_version=_version(request),
                    **form.cleaned_data,
                )
                messages.success(
                    request,
                    "Keputusan kampanye disimpan. Tanggal rencana dan riwayat tetap tersedia.",
                )
                return redirect("marketing:campaign_detail", pk=pk)
            except ValidationError as exc:
                _form_error(form, exc)
    return render(
        request,
        "marketing/campaign_detail.html",
        _context(
            request,
            "campaigns",
            campaign=campaign,
            form=form,
            can_manage=request.membership.role in workflow.MANAGERS,
            events=campaign.lifecycle_events.select_related("created_by", "member"),
        ),
    )


@require_GET
@organization_required
def daily(request):
    require_role(request, *READ)
    try:
        result = workflow.daily_work(
            organization=request.organization,
            actor=request.user,
            scope=request.GET.get(
                "scope",
                "all" if request.membership.role in workflow.MANAGERS else "mine",
            ),
            queue=request.GET.get("queue", "today"),
        )
        page = _page(request, result.pop("rows"))
    except ValidationError as exc:
        return _bad(exc)
    attention_queues = [
        item
        for item in result["counts"]
        if item["count"]
        and item["key"] != result["queue"]
        and item["key"]
        in {
            "overdue",
            "unanswered",
            "unassigned",
            "revision",
            "review",
            "response_sla",
            "review_sla",
        }
    ]
    return render(
        request,
        "marketing/daily.html",
        _context(
            request,
            "daily",
            page=page,
            pagination_query=_pagination_query(request),
            attention_queues=attention_queues,
            can_configure_policy=request.membership.role == "owner",
            **result,
        ),
    )


@require_http_methods(["GET", "POST"])
@organization_required
def workflow_policy(request):
    require_role(request, "owner")
    current = workflow.current_work_policy(request.organization)
    initial = {"expected_version": current.version if current else 0}
    if current:
        initial.update(
            {
                key: getattr(current, key)
                for key in (
                    "response_limit_minutes",
                    "review_limit_minutes",
                    "workdays",
                    "work_start",
                    "work_end",
                )
            }
        )
        initial["holidays"] = "\n".join(current.holidays)
    form = WorkPolicyForm(
        request.POST if request.method == "POST" else None, initial=initial
    )
    if request.method == "POST" and form.is_valid():
        try:
            workflow.configure_work_policy(
                organization=request.organization,
                actor=request.user,
                **form.cleaned_data,
            )
            messages.success(
                request, "Aturan jam kerja dan batas waktu disahkan sebagai versi baru."
            )
            return redirect("marketing:daily")
        except ValidationError as exc:
            _form_error(form, exc)
    return _form(
        request,
        form,
        "Jam kerja & batas tindak lanjut",
        "Aturan disahkan Owner; tidak ada target waktu yang diasumsikan otomatis.",
        "daily",
        reverse("marketing:daily"),
        note="Antrean SLA memakai aturan versi terbaru dalam WIB dan mengecualikan hari libur. Ini pengingat kerja saat ini, bukan perubahan skor historis. Peringatan tampil dalam aplikasi; tidak mengirim pesan WhatsApp/email otomatis.",
    )
