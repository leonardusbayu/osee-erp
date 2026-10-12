from django.contrib import messages
from django.core import signing
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from core.access import organization_required, require_role

from . import course_services, forms, intake, services
from .models import (
    IntakeBatch,
    OperationEvent,
    Order,
    OrderLine,
    Registration,
    TestSession,
)
from .view_helpers import _access, _error, _finance_access, _form_page


@organization_required
@require_http_methods(["GET"])
def overview(request):
    _access(request)
    qs = Order.objects.filter(organization=request.organization).select_related("buyer")
    if request.GET.get("q"):
        qs = qs.filter(
            Q(reference__icontains=request.GET["q"][:200])
            | Q(buyer__name__icontains=request.GET["q"][:200])
        )
    return render(
        request,
        "operations/orders.html",
        {
            "orders": Paginator(qs, 30).get_page(request.GET.get("page")),
            "active_nav": "operations",
            "pending_intake": IntakeBatch.objects.filter(
                organization=request.organization, rows__status="pending"
            )
            .distinct()
            .count(),
        },
    )


@organization_required
@require_http_methods(["GET", "POST"])
def order_create(request):
    _access(request)
    form = forms.OrderForm(
        request.POST or None,
        organization=request.organization,
        initial={"date": timezone.localdate()},
    )
    if request.method == "POST" and form.is_valid():
        try:
            obj = services.create_order(
                organization=request.organization,
                actor=request.user,
                **form.cleaned_data,
            )
            return redirect("operations:order", pk=obj.pk)
        except ValidationError as exc:
            _error(form, exc)
    return _form_page(
        request,
        form,
        "Catat pesanan",
        reverse("operations:overview"),
        "Pilih pelanggan atau mitra yang ditagih. Nama peserta dicatat terpisah.",
    )


@organization_required
@require_http_methods(["GET"])
def order_detail(request, pk):
    _access(request)
    order = get_object_or_404(Order, organization=request.organization, pk=pk)
    facts = services.order_summary(organization=request.organization, order=order)
    facts["can_view_financial_details"] = request.membership.role in {
        "owner",
        "finance",
    }
    facts["registrations"] = Registration.objects.filter(
        organization=request.organization, line__order=order
    ).select_related("participant", "line__product", "session")
    facts["active_nav"] = "operations"
    return render(request, "operations/order.html", facts)


@organization_required
@require_http_methods(["GET", "POST"])
def line_create(request, pk):
    _access(request)
    order = get_object_or_404(Order, organization=request.organization, pk=pk)
    form = forms.LineForm(request.POST or None, organization=request.organization)
    if request.method == "POST" and form.is_valid():
        try:
            services.add_order_line(
                organization=request.organization,
                actor=request.user,
                order=order,
                **form.cleaned_data,
            )
            return redirect("operations:order", pk=pk)
        except ValidationError as exc:
            _error(form, exc)
    return _form_page(
        request,
        form,
        "Tambah rincian pesanan",
        reverse("operations:order", args=[pk]),
        "ITP, iBT, dan kursus dicatat sebagai rincian terpisah agar pelaksanaan dan biaya tidak tercampur.",
    )


@organization_required
@require_http_methods(["GET", "POST"])
def line_invoice(request, pk):
    _access(request)
    _finance_access(request)
    line = get_object_or_404(OrderLine, organization=request.organization, pk=pk)
    form = forms.InvoiceLineForm(
        request.POST or None,
        initial={"date": timezone.localdate(), "due_date": timezone.localdate()},
    )
    quote = None
    if request.method == "POST" and form.is_valid():
        try:
            data = dict(form.cleaned_data)
            confirmed, token = data.pop("confirm"), data.pop("quote_token")
            quote = services.invoice_quote(
                organization=request.organization,
                actor=request.user,
                line=line,
                date=data["date"],
            )
            if request.POST.get("action") == "issue":
                try:
                    expected = signing.loads(
                        token, salt="order-price-preview", max_age=1800
                    )
                except signing.BadSignature:
                    raise ValidationError(
                        "Pratinjau kedaluwarsa atau tidak valid; tampilkan ulang harga."
                    )
                if not confirmed or expected != {
                    "org": request.organization.pk,
                    "user": request.user.pk,
                    "fingerprint": quote["fingerprint"],
                    "due_date": str(data["due_date"]),
                }:
                    raise ValidationError(
                        "Konfirmasi pratinjau yang sesuai tanggal, harga, dan jumlah terkini."
                    )
                invoice = services.invoice_line(
                    organization=request.organization,
                    actor=request.user,
                    line=line,
                    expected_price_fingerprint=quote["fingerprint"],
                    **data,
                )
                return redirect("sale_detail", pk=invoice.pk)
            mutable = request.POST.copy()
            mutable["quote_token"] = signing.dumps(
                {
                    "org": request.organization.pk,
                    "user": request.user.pk,
                    "fingerprint": quote["fingerprint"],
                    "due_date": str(data["due_date"]),
                },
                salt="order-price-preview",
            )
            mutable["confirm"] = ""
            form = forms.InvoiceLineForm(mutable)
        except ValidationError as exc:
            _error(form, exc)
    return render(
        request,
        "operations/invoice_preview.html",
        {"form": form, "line": line, "quote": quote, "active_nav": "operations"},
    )


@organization_required
@require_http_methods(["GET", "POST"])
def order_lead(request, pk):
    _access(request)
    _finance_access(request)
    order = get_object_or_404(Order, organization=request.organization, pk=pk)
    form = forms.OrderLeadForm(
        request.POST or None,
        organization=request.organization,
        initial={"lead": order.lead_id, "expected_version": order.version},
    )
    if request.method == "POST" and form.is_valid():
        try:
            services.link_order_lead(
                organization=request.organization,
                actor=request.user,
                order=order,
                **form.cleaned_data,
            )
            return redirect("operations:order", pk=pk)
        except ValidationError as exc:
            _error(form, exc)
    return _form_page(
        request,
        form,
        "Hubungkan prospek ke pesanan",
        reverse("operations:order", args=[pk]),
        "Semua invoice dalam pesanan mengikuti sumber prospek ini. Perubahan disimpan sebagai koreksi dan akan meminta pemeriksaan ulang KPI terkait.",
    )


@organization_required
@require_http_methods(["GET", "POST"])
def existing_invoice(request, pk):
    _access(request)
    _finance_access(request)
    line = get_object_or_404(OrderLine, organization=request.organization, pk=pk)
    form = forms.ExistingInvoiceForm(
        request.POST or None, organization=request.organization
    )
    form.fields["invoice"].queryset = form.fields["invoice"].queryset.filter(
        party=line.order.buyer, product=line.product, quantity=line.quantity
    )
    if request.method == "POST" and form.is_valid():
        try:
            services.link_order_invoice(
                organization=request.organization,
                actor=request.user,
                line=line,
                **form.cleaned_data,
            )
            return redirect("operations:order", pk=line.order_id)
        except ValidationError as exc:
            _error(form, exc)
    return _form_page(
        request,
        form,
        "Gunakan invoice yang sudah ada",
        reverse("operations:order", args=[line.order_id]),
        "Tidak menerbitkan tagihan baru. Pemesan, produk, jumlah, dan kondisi pelaksanaan harus sama.",
    )


@organization_required
@require_http_methods(["GET", "POST"])
def attribution_correction(request, pk):
    from marketing.identity_recovery import identity_recovery_context
    from marketing.identity_views import legacy_review_rows
    from marketing.models import Lead

    from .attribution import correct_lead_invoice

    _access(request)
    _finance_access(request)
    lead = get_object_or_404(Lead, organization=request.organization, pk=pk)
    recovery = identity_recovery_context(organization=request.organization, lead=lead)
    lead = recovery["lead"]
    data = request.POST if request.method == "POST" else None
    rechecked = False
    if data is not None and data.get("review_action") == "recheck":
        if not any(
            len(data.getlist(name)) > 1
            for name in (*forms.AttributionCorrectionForm.base_fields, "review_action")
        ):
            data = data.copy()
            data["expected_version"] = lead.version
            data["expected_fingerprint"] = recovery["fingerprint"]
            data.pop("confirm_legacy_source", None)
            rechecked = True
    form = forms.AttributionCorrectionForm(
        data,
        organization=request.organization,
        recovery=recovery,
        initial={
            "invoice": lead.invoice_id,
            "expected_version": lead.version,
            "expected_fingerprint": recovery["fingerprint"],
        },
    )
    form.fields["invoice"].queryset = form.fields["invoice"].queryset.filter(
        party_id=lead.party_id, product_id=lead.product_id
    )
    if request.method == "POST" and not rechecked and form.is_valid():
        try:
            correct_lead_invoice(
                organization=request.organization,
                actor=request.user,
                lead=lead,
                **form.cleaned_data,
            )
            return redirect("marketing:lead_detail", pk=pk)
        except ValidationError as exc:
            _error(form, exc)
            recovery = identity_recovery_context(
                organization=request.organization, lead=lead
            )
            lead = recovery["lead"]
    stale = bool(
        data is not None
        and (
            str(data.get("expected_version", "")) != str(lead.version)
            or recovery["legacy_review_required"]
            and data.get("expected_fingerprint") != recovery["fingerprint"]
        )
    )
    return render(
        request,
        "operations/attribution_correction.html",
        {
            "form": form,
            "lead": lead,
            "recovery": recovery,
            "stale": stale,
            "rechecked": rechecked,
            "active_nav": "operations",
            "legacy_history": legacy_review_rows(
                organization=request.organization, recovery=recovery
            ),
        },
    )


@organization_required
@require_http_methods(["GET", "POST"])
def registration_create(request, pk):
    _access(request)
    line = get_object_or_404(OrderLine, organization=request.organization, pk=pk)
    form = forms.RegistrationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            obj = services.register_participant(
                organization=request.organization,
                actor=request.user,
                line=line,
                **form.cleaned_data,
            )
            return redirect("operations:registration", pk=obj.pk)
        except ValidationError as exc:
            _error(form, exc)
    return _form_page(
        request,
        form,
        "Daftarkan peserta",
        reverse("operations:order", args=[line.order_id]),
        "Pendaftaran tidak membuat invoice, pembayaran, atau pendapatan.",
    )


@organization_required
@require_http_methods(["GET"])
def registration_detail(request, pk):
    _access(request)
    reg = get_object_or_404(
        Registration.objects.select_related(
            "participant", "session", "line__order", "line__invoice"
        ),
        organization=request.organization,
        pk=pk,
    )
    history = (
        OperationEvent.objects.filter(
            organization=request.organization, object_type="registration", object_id=pk
        )
        .select_related("created_by")
        .order_by("-created_at", "-pk")
    )
    for event in history:
        if event.action in {
            "registration.attendance",
            "registration.attendance_corrected",
        }:
            old = {"attended": "Hadir", "absent": "Tidak hadir"}.get(
                event.before.get("status"), "Belum dicatat"
            )
            new = {"attended": "Hadir", "absent": "Tidak hadir"}.get(
                event.after.get("status"), "Belum dicatat"
            )
            event.timeline_label = (
                "Kehadiran dikoreksi"
                if event.action.endswith("_corrected")
                else "Kehadiran dicatat"
            )
            event.timeline_detail = (
                f"{old} → {new}" if event.action.endswith("_corrected") else new
            )
        elif event.action == "registration.identity_corrected":
            event.timeline_label = "Data peserta dikoreksi"
            event.timeline_detail = (
                f"{event.before.get('name', '—')} → {event.after.get('name', '—')}"
            )
            event.identity_before = event.before
            event.identity_after = event.after
        else:
            event.timeline_label = event.reason
            event.timeline_detail = ""
    context = {"registration": reg, "history": history, "active_nav": "operations"}
    context["can_view_financial_details"] = request.membership.role in {
        "owner",
        "finance",
    }
    if reg.line.product.kind == "course":
        context["course_progress"] = course_services.course_progress(
            organization=request.organization, registration=reg
        )
        context["course_plan_url"] = reverse(
            "operations:course_plan", args=[reg.line_id]
        )
    return render(
        request,
        "operations/registration.html",
        context,
    )


@organization_required
@require_http_methods(["GET", "POST"])
def registration_action(request, pk, action):
    _access(request)
    reg = get_object_or_404(Registration, organization=request.organization, pk=pk)
    definitions = {
        "schedule": (
            forms.ScheduleForm,
            services.schedule_registration,
            "Jadwalkan / pindahkan sesi",
        ),
        "attendance": (
            forms.AttendanceForm,
            services.record_attendance,
            "Catat kehadiran",
        ),
        "correct-attendance": (
            forms.AttendanceCorrectionForm,
            services.correct_attendance,
            "Koreksi kehadiran",
        ),
        "correct-identity": (
            forms.IdentityCorrectionForm,
            services.correct_registration_identity,
            "Koreksi data peserta",
        ),
        "complete": (
            forms.CompleteForm,
            services.complete_registration,
            "Konfirmasikan layanan selesai",
        ),
        "cancel": (
            forms.CancelForm,
            services.cancel_registration,
            "Batalkan satu peserta",
        ),
    }
    if action not in definitions:
        raise Http404
    if action == "cancel":
        require_role(request, "owner")
    elif action == "complete":
        _finance_access(request)
    form_class, command, title = definitions[action]
    kwargs = {
        "initial": {"expected_version": reg.version, "date": timezone.localdate()}
    }
    if action == "correct-attendance":
        kwargs["initial"]["attended"] = "yes" if reg.status == "attended" else "no"
    if action == "correct-identity":
        kwargs["initial"].update(
            name=reg.participant.name,
            email=reg.participant.email,
            phone=reg.participant.phone,
        )
    if action == "schedule":
        kwargs["organization"] = request.organization
    form = form_class(request.POST or None, **kwargs)
    if action == "schedule":
        form.fields["session"].queryset = form.fields["session"].queryset.filter(
            product=reg.line.product
        )
    if request.method == "POST" and form.is_valid():
        try:
            data = dict(form.cleaned_data)
            data.pop("confirm", None)
            command(
                organization=request.organization,
                actor=request.user,
                registration=reg,
                **data,
            )
            return redirect("operations:registration", pk=pk)
        except ValidationError as exc:
            _error(form, exc)
    description = "Pelunasan mitra diperiksa pada tanggal sesi. Penyelesaian mengakui satu unit layanan; pembatalan invoice membuat koreksi, bukan mengirim uang."
    if action == "correct-attendance":
        description = (
            "Catatan saat ini: "
            + reg.get_status_display()
            + ". Periksa bukti kehadiran kembali. Koreksi hanya mengubah status, disimpan di riwayat, dan tidak mengubah jurnal atau pembayaran."
        )
    elif action == "correct-identity":
        description = (
            "Cocokkan nama dan kontak dengan bukti asli. Data lama tetap tersimpan di riwayat. "
            "Koreksi tidak mengubah pemesan, invoice, sumber pendaftaran, atribusi, atau pembukuan."
        )
    return _form_page(
        request, form, title, reverse("operations:registration", args=[pk]), description
    )


@organization_required
@require_http_methods(["GET", "POST"])
def sessions(request):
    _access(request)
    form = forms.SessionForm(request.POST or None, organization=request.organization)
    if request.method == "POST" and form.is_valid():
        try:
            services.create_session(
                organization=request.organization,
                actor=request.user,
                **form.cleaned_data,
            )
            return redirect("operations:sessions")
        except ValidationError as exc:
            _error(form, exc)
    return render(
        request,
        "operations/sessions.html",
        {
            "form": form,
            "sessions": TestSession.objects.filter(
                organization=request.organization
            ).select_related("product")[:100],
            "active_nav": "operations",
        },
    )


@organization_required
@require_http_methods(["GET", "POST"])
def intake_upload(request):
    _access(request)
    form = forms.UploadForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            file = form.cleaned_data["file"]
            batch = intake.stage_upload(
                organization=request.organization,
                actor=request.user,
                source=form.cleaned_data["source"],
                content=file.read(),
                filename=file.name,
            )
            return redirect("operations:intake_map", pk=batch.pk)
        except ValidationError as exc:
            _error(form, exc)
    return render(
        request,
        "operations/intake_upload.html",
        {
            "form": form,
            "batches": IntakeBatch.objects.filter(
                organization=request.organization
            ).order_by("-pk")[:50],
            "active_nav": "operations",
        },
    )


@organization_required
@require_http_methods(["GET", "POST"])
def intake_map(request, pk):
    _access(request)
    batch = get_object_or_404(IntakeBatch, organization=request.organization, pk=pk)
    form = forms.MappingForm(
        request.POST or None, batch=batch, initial={"expected_version": batch.version}
    )
    if request.method == "POST" and form.is_valid():
        try:
            mapping = dict(form.cleaned_data)
            version = mapping.pop("expected_version")
            intake.map_batch(
                organization=request.organization,
                actor=request.user,
                batch=batch,
                mapping=mapping,
                expected_version=version,
            )
            return redirect("operations:intake_review", pk=pk)
        except ValidationError as exc:
            _error(form, exc)
    return _form_page(
        request,
        form,
        "Cocokkan kolom pendaftaran",
        reverse("operations:intake_review", args=[pk]),
        "Tanpa ID respons, identitas sementara mengikuti snapshot file. Periksa duplikat lintas file sebelum menerima. Kolom KTP/bukti transfer tetap sumber privat; tidak diambil otomatis.",
    )


@organization_required
@require_http_methods(["GET", "POST"])
def intake_review(request, pk):
    _access(request)
    batch = get_object_or_404(IntakeBatch, organization=request.organization, pk=pk)
    rows = Paginator(batch.rows.all(), 100).get_page(request.GET.get("page"))
    form = forms.IntakeReviewForm(
        request.POST or None,
        organization=request.organization,
        batch=batch,
        page_rows=rows,
        initial={"expected_version": batch.version},
    )
    if request.method == "POST" and form.is_valid():
        try:
            data = dict(form.cleaned_data)
            data["row_ids"] = list(data["row_ids"].values_list("pk", flat=True))
            if request.POST.get("operation") == "reject":
                intake.reject_rows(
                    organization=request.organization,
                    actor=request.user,
                    batch=batch,
                    **{
                        key: data[key]
                        for key in ("row_ids", "expected_version", "reason")
                    },
                )
            elif request.POST.get("operation") == "accept":
                if not data["order"] or not data["product"]:
                    raise ValidationError(
                        "Pilih pesanan/pemesan dan produk sebelum menerima pendaftaran."
                    )
                intake.accept_rows(
                    organization=request.organization,
                    actor=request.user,
                    batch=batch,
                    **data,
                )
            else:
                raise ValidationError("Tindakan pemeriksaan tidak dikenal.")
            messages.success(
                request, "Keputusan tersimpan bersama riwayat pemeriksaan."
            )
            return redirect("operations:intake_review", pk=pk)
        except ValidationError as exc:
            _error(form, exc)
    return render(
        request,
        "operations/intake_review.html",
        {"batch": batch, "rows": rows, "form": form, "active_nav": "operations"},
    )
