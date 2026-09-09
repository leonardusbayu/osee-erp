"""Scoped, server-rendered marketing workspace for everyday operational use."""
from decimal import Decimal

from django import forms
from django.contrib import messages
from django.core import signing
from django.core.exceptions import ValidationError
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.db.models import Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods

from core.access import organization_required, require_role
from director.models import CHANNEL_CHOICES
from finance.models import Allocation, Product
from . import services
from .ai import build_recommendations, marketing_advice
from .analytics import GROUP_BY_CHOICES, marketing_snapshot
from .forms import (ActionUpdateForm, AdObservationForm, CampaignForm, InvoiceLinkForm,
                    LeadForm, LeadUpdateForm, MarketingActionForm, MarketingAIPolicyForm,
                    TeamMemberForm)
from .models import AdDailyObservation, Campaign, Lead, MarketingAction, MarketingAIPolicy, STAGES, TeamMember

READ = ("owner", "director", "finance", "marketing", "auditor")
WRITE = ("owner", "director", "finance", "marketing")
FINANCE = ("owner", "director", "finance")
TOKEN_SALT = "marketing.recommendation.v1"


class AnalysisFilterForm(forms.Form):
    start = forms.DateField(label="Dari tanggal", widget=forms.DateInput(attrs={"type": "date"}))
    end = forms.DateField(label="Sampai tanggal", widget=forms.DateInput(attrs={"type": "date"}))
    group_by = forms.ChoiceField(label="Bandingkan menurut", choices=GROUP_BY_CHOICES)
    channel = forms.ChoiceField(label="Channel", required=False, choices=[("", "Semua channel"), *CHANNEL_CHOICES])
    campaign = forms.ModelChoiceField(label="Kampanye", required=False, queryset=Campaign.objects.none())
    member = forms.ModelChoiceField(label="Anggota tim", required=False, queryset=TeamMember.objects.none())
    product = forms.ModelChoiceField(label="Produk", required=False, queryset=Product.objects.none())
    segment = forms.ChoiceField(label="Jenis pembeli", required=False, choices=[("", "Semua jenis"), *Lead._meta.get_field("segment").choices])
    region = forms.CharField(label="Wilayah (persis)", max_length=120, required=False)
    ad = forms.CharField(label="Nama / ID iklan (persis)", max_length=180, required=False)

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        for name, model in (("campaign", Campaign), ("member", TeamMember), ("product", Product)):
            self.fields[name].queryset = model.objects.filter(organization=organization)
        self.fields["ad"].help_text = "Filter iklan menampilkan biaya dan klik; prospek belum dipetakan sampai iklan."

    def clean(self):
        data = super().clean()
        if data.get("start") and data.get("end"):
            if data["start"] > data["end"]:
                raise ValidationError("Tanggal akhir tidak boleh sebelum tanggal awal.")
            if (data["end"] - data["start"]).days > 366:
                raise ValidationError("Pilih periode maksimal 366 hari.")
        return data


class AdImportForm(forms.Form):
    campaign = forms.ModelChoiceField(label="Kampanye", queryset=Campaign.objects.none())
    file = forms.FileField(label="File CSV", help_text="Gunakan template. Maksimal 1 MB; satu kampanye per file.")

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["campaign"].queryset = Campaign.objects.filter(organization=organization)
        self.fields["file"].widget.attrs["accept"] = ".csv,text/csv"

    def clean_file(self):
        upload = self.cleaned_data["file"]
        if not upload.name.lower().endswith(".csv") or upload.size > 1024 * 1024:
            raise ValidationError("Pilih file .csv berukuran maksimal 1 MB.")
        return upload


def _context(request, nav, **values):
    return {"active_nav": "marketing_" + nav, "is_marketing_workspace": True,
            "marketing_write": request.membership.role in WRITE,
            "finance_link": request.membership.role in FINANCE, **values}


def _bad(error):
    return HttpResponse(" ".join(error.messages), status=400, content_type="text/plain; charset=utf-8")


def _form_error(form, error):
    if hasattr(error, "message_dict"):
        for field, errors in error.message_dict.items():
            for message in errors:
                form.add_error(field if field in form.fields else None, message)
    else:
        for message in error.messages:
            form.add_error(None, message)


def _version(request):
    try:
        value = int(request.POST.get("expected_version", ""))
        if value < 1:
            raise ValueError
        return value
    except (TypeError, ValueError) as exc:
        raise ValidationError("Versi data tidak valid. Muat ulang halaman sebelum menyimpan.") from exc


def _scope(request, data=None):
    today = timezone.localdate()
    values = {"start": today.replace(day=1).isoformat(), "end": today.isoformat(), "group_by": "channel"}
    source = request.GET if data is None else data
    values.update({key: source.get(key) for key in AnalysisFilterForm.base_fields if key in source})
    form = AnalysisFilterForm(values, organization=request.organization)
    if not form.is_valid():
        errors = [str(message) for errors in form.errors.values() for message in errors]
        raise ValidationError(errors)
    cleaned = form.cleaned_data
    filters = {key: value.pk if hasattr(value, "pk") else value for key, value in cleaned.items()
               if key not in ("start", "end", "group_by") and value not in (None, "")}
    snapshot = marketing_snapshot(organization=request.organization, start=cleaned["start"],
                                  end=cleaned["end"], group_by=cleaned["group_by"], filters=filters)
    return {"snapshot": snapshot, "filter_form": form, "start": cleaned["start"], "end": cleaned["end"],
            "group_by": cleaned["group_by"], "filters": filters,
            "active_filters": len(filters), "dimension_label": dict(GROUP_BY_CHOICES)[cleaned["group_by"]]}


def _page(request, rows):
    try:
        return Paginator(rows, 25).page(request.GET.get("page", "1"))
    except (EmptyPage, PageNotAnInteger) as exc:
        raise ValidationError("Halaman daftar tidak valid.") from exc


def _pagination_query(request):
    query = request.GET.copy()
    query.pop("page", None)
    return query.urlencode()


def _form_page(request, form, title, description, nav, back, **extra):
    return render(request, "marketing/form.html", _context(request, nav, form=form,
                  page_title=title, description=description, section_label=nav.upper(), back_url=reverse(back), **extra))


@require_GET
@organization_required
def overview(request):
    require_role(request, *READ)
    try:
        scope = _scope(request)
    except ValidationError as exc:
        return _bad(exc)
    advice = build_recommendations(scope["snapshot"])
    actions = MarketingAction.objects.filter(organization=request.organization, status__in=("open", "in_progress")).select_related("owner")[:4]
    return render(request, "marketing/overview.html", _context(request, "overview", **scope,
                  recommendations=advice["recommendations"], actions=actions))


@require_GET
@organization_required
def analysis(request):
    require_role(request, *READ)
    try:
        scope = _scope(request)
    except ValidationError as exc:
        return _bad(exc)
    if scope["group_by"] == "member":
        members = {str(member.pk): member for member in TeamMember.objects.filter(organization=request.organization)}
        for row in scope["snapshot"]["rows"]:
            member = members.get(row["key"])
            if member:
                row["member_context"] = f"{member.get_discipline_display()} · {member.team}"
    chart_rows = [dict(row) for row in scope["snapshot"]["rows"] if row["cash_in"] is not None and Decimal(row["cash_in"]) > 0]
    chart_rows.sort(key=lambda row: Decimal(row["cash_in"]), reverse=True)
    maximum = max((Decimal(row["cash_in"]) for row in chart_rows), default=Decimal(0))
    for row in chart_rows:
        row["bar_width"] = format(Decimal(row["cash_in"]) / maximum * 100, ".2f")
    return render(request, "marketing/analysis.html", _context(request, "analysis", **scope, chart_rows=chart_rows[:10]))


@require_GET
@organization_required
def leads(request):
    require_role(request, *READ)
    query, stage, due = request.GET.get("q", "").strip(), request.GET.get("stage", ""), request.GET.get("due", "")
    if len(query) > 100 or stage and stage not in dict(STAGES) or due not in ("", "overdue"):
        return _bad(ValidationError("Filter prospek tidak valid."))
    rows = Lead.objects.filter(organization=request.organization).select_related("campaign", "owner", "product")
    if query:
        rows = rows.filter(Q(name__icontains=query) | Q(reference__icontains=query))
    if stage:
        rows = rows.filter(stage=stage)
    if due:
        today = timezone.localdate()
        rows = rows.filter(next_follow_up__lt=today).exclude(stage="lost").select_related("invoice")
        paid = dict(Allocation.objects.filter(organization=request.organization, transaction__organization=request.organization,
                     invoice__organization=request.organization, transaction__date__lte=today)
                    .values("invoice_id").annotate(total=Sum("amount")).values_list("invoice_id", "total"))
        unpaid_ids = [lead.pk for lead in rows if not lead.invoice_id or paid.get(lead.invoice_id, Decimal(0)) < lead.invoice.total]
        rows = rows.filter(pk__in=unpaid_ids)
    try:
        page = _page(request, rows)
    except ValidationError as exc:
        return _bad(exc)
    return render(request, "marketing/leads.html", _context(request, "leads", page=page, query=query,
                  stage=stage, due=due, stages=STAGES, pagination_query=_pagination_query(request)))


@require_http_methods(["GET", "POST"])
@organization_required
def lead_create(request):
    require_role(request, *WRITE)
    form = LeadForm(request.POST or None, organization=request.organization,
                    initial={"received_at": timezone.localtime().replace(second=0, microsecond=0)})
    if request.method == "POST" and form.is_valid():
        try:
            lead = services.create_lead(organization=request.organization, actor=request.user, **form.cleaned_data)
            messages.success(request, "Prospek dicatat. Lanjutkan dengan respons dan jadwal tindak lanjut.")
            return redirect("marketing:lead_detail", pk=lead.pk)
        except ValidationError as exc:
            _form_error(form, exc)
    return _form_page(request, form, "Catat prospek", "Mulai dengan asal prospek, kebutuhan, dan penanggung jawabnya.", "leads", "marketing:leads")


@require_http_methods(["GET", "POST"])
@organization_required
def lead_detail(request, pk):
    require_role(request, *READ)
    lead = get_object_or_404(Lead.objects.select_related("campaign", "owner", "product", "party", "invoice"), organization=request.organization, pk=pk)
    operation = request.POST.get("operation", "update")
    if request.method == "POST":
        require_role(request, *WRITE)
        if operation not in ("update", "link_invoice"):
            return _bad(ValidationError("Tindakan prospek tidak valid."))
        if operation == "link_invoice":
            require_role(request, *FINANCE)
    update_form = LeadUpdateForm(request.POST if request.method == "POST" and operation == "update" else None,
                                 organization=request.organization, instance=lead)
    invoice_form = InvoiceLinkForm(request.POST if request.method == "POST" and operation == "link_invoice" else None,
                                   organization=request.organization, lead=lead) if request.membership.role in FINANCE else None
    if request.method == "POST":
        form = invoice_form if operation == "link_invoice" else update_form
        if form.is_valid():
            try:
                if operation == "link_invoice":
                    services.link_invoice(organization=request.organization, actor=request.user, lead=lead,
                                          expected_version=_version(request), **form.cleaned_data)
                else:
                    services.update_lead(organization=request.organization, actor=request.user, lead=lead,
                                         expected_version=_version(request), **form.cleaned_data)
                messages.success(request, "Invoice terhubung." if operation == "link_invoice" else "Tindak lanjut disimpan.")
                return redirect("marketing:lead_detail", pk=lead.pk)
            except ValidationError as exc:
                _form_error(form, exc)
    return render(request, "marketing/lead_detail.html", _context(request, "leads", lead=lead,
                  update_form=update_form, invoice_form=invoice_form))


@require_GET
@organization_required
def campaigns(request):
    require_role(request, *READ)
    try:
        page = _page(request, AdDailyObservation.objects.filter(organization=request.organization).select_related("campaign"))
    except ValidationError as exc:
        return _bad(exc)
    return render(request, "marketing/campaigns.html", _context(request, "campaigns", page=page,
                  campaigns=Campaign.objects.filter(organization=request.organization).select_related("owner", "product"),
                  pagination_query=_pagination_query(request)))


def _create(request, form_class, service, title, description, nav, back, initial=None):
    require_role(request, *WRITE)
    form = form_class(request.POST or None, organization=request.organization, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            service(organization=request.organization, actor=request.user, **form.cleaned_data)
            messages.success(request, "Catatan marketing disimpan.")
            return redirect(back)
        except ValidationError as exc:
            _form_error(form, exc)
    return _form_page(request, form, title, description, nav, back)


@require_http_methods(["GET", "POST"])
@organization_required
def campaign_create(request):
    return _create(request, CampaignForm, services.create_campaign, "Buat kampanye", "Hubungkan aktivitas iklan dan prospek melalui satu kampanye.", "campaigns", "marketing:campaigns")


@require_http_methods(["GET", "POST"])
@organization_required
def ad_create(request):
    return _create(request, AdObservationForm, services.save_ad_observation, "Catat hasil iklan", "Isi angka sesuai laporan platform. Kosong berarti belum diketahui.", "campaigns", "marketing:campaigns", {"date": timezone.localdate()})


@require_http_methods(["GET", "POST"])
@organization_required
def ad_correct(request, pk):
    require_role(request, *WRITE)
    observation = get_object_or_404(AdDailyObservation, organization=request.organization, pk=pk)
    form = AdObservationForm(request.POST or None, organization=request.organization, instance=observation)
    for name in ("campaign", "date", "ad_name", "audience", "region", "reference"):
        form.fields[name].disabled = True
    form.fields["reason"] = forms.CharField(label="Alasan koreksi", max_length=500, widget=forms.Textarea(attrs={"rows": 3}),
                                           help_text="Jelaskan perubahan pada laporan sumber. Angka sebelum dan sesudah disimpan dalam riwayat audit.")
    if request.method == "POST" and form.is_valid():
        try:
            services.save_ad_observation(organization=request.organization, actor=request.user, observation=observation,
                                         expected_version=_version(request), **form.cleaned_data)
            messages.success(request, "Koreksi hasil iklan disimpan beserta alasannya.")
            return redirect("marketing:campaigns")
        except ValidationError as exc:
            _form_error(form, exc)
    return _form_page(request, form, "Koreksi hasil iklan", "Perbarui angka sesuai sumber dan jelaskan alasannya.", "campaigns", "marketing:campaigns",
                      expected_version=observation.version, note_title="Koreksi yang dapat ditelusuri",
                      note="Kampanye, tanggal, rincian iklan, dan referensi dipertahankan sebagai identitas sumber. Koreksi hanya mengubah angka laporan serta sumber, dengan alasan dan versi data.")


@require_GET
@organization_required
def members(request):
    require_role(request, *READ)
    return render(request, "marketing/members.html", _context(request, "members", members=TeamMember.objects.filter(organization=request.organization)))


@require_http_methods(["GET", "POST"])
@organization_required
def member_create(request):
    return _create(request, TeamMemberForm, services.create_member, "Tambah anggota tim", "Tentukan peran kerja untuk membagi tindak lanjut dan membaca kontribusi secara adil.", "members", "marketing:members")


def _recommendation_token(request, scope, recommendation):
    return signing.dumps({"organization": request.organization.pk, "id": recommendation["id"],
                          "start": scope["start"].isoformat(), "end": scope["end"].isoformat(),
                          "group_by": scope["group_by"], **scope["filters"]}, salt=TOKEN_SALT, compress=True)


@require_http_methods(["GET", "POST"])
@organization_required
def advisor(request):
    require_role(request, *READ)
    if request.method == "POST":
        require_role(request, *WRITE)
    try:
        scope = _scope(request, request.POST if request.method == "POST" else None)
        page = _page(request, MarketingAction.objects.filter(organization=request.organization).select_related("owner"))
    except ValidationError as exc:
        return _bad(exc)
    question = request.POST.get("question", "").strip() if request.method == "POST" else ""
    question_error = ""
    advice = build_recommendations(scope["snapshot"])
    if request.method == "POST":
        if not question or len(question) > 1000:
            question_error = "Tulis pertanyaan antara 1 dan 1.000 karakter."
        else:
            try:
                advice = marketing_advice(organization=request.organization, actor=request.user,
                                          snapshot=scope["snapshot"], question=question)
            except ValidationError as exc:
                question_error = " ".join(exc.messages)
    for recommendation in advice["recommendations"]:
        recommendation["action_token"] = _recommendation_token(request, scope, recommendation)
        recommendation["priority_label"] = "Prioritas tinggi" if recommendation["priority"] == "high" else "Tinjau bersama tim"
    return render(request, "marketing/advisor.html", _context(request, "advisor", **scope, advice=advice,
                  question=question, question_error=question_error, page=page, pagination_query=_pagination_query(request)))


def _verified_recommendation(request, token):
    try:
        data = signing.loads(token, salt=TOKEN_SALT, max_age=3600)
        if data["organization"] != request.organization.pk:
            raise signing.BadSignature
        scope = _scope(request, data)
        recommendation = next((item for item in build_recommendations(scope["snapshot"])["recommendations"] if item["id"] == data["id"]), None)
        if recommendation is None:
            raise ValidationError("Data telah berubah dan saran ini tidak lagi berlaku. Buka Saran Manager untuk memperbaruinya.")
        return recommendation
    except (signing.BadSignature, KeyError, TypeError) as exc:
        raise ValidationError("Saran tidak valid atau telah kedaluwarsa. Pilih kembali dari Saran Manager.") from exc


@require_http_methods(["GET", "POST"])
@organization_required
def action_create(request):
    require_role(request, *WRITE)
    token = request.POST.get("recommendation_token", "") if request.method == "POST" else request.GET.get("recommendation_token", "")
    recommendation, initial = None, {}
    if token:
        try:
            recommendation = _verified_recommendation(request, token)
        except ValidationError as exc:
            return _bad(exc)
        initial = {"title": recommendation["title"], "description": recommendation["action"],
                   "evidence": recommendation["evidence"], "metric_key": recommendation["metric_key"]}
    form = MarketingActionForm(request.POST or None, organization=request.organization, initial=initial)
    form.fields["owner"].required = True
    form.fields["due_date"].required = True
    if recommendation:
        for field in ("title", "description", "evidence", "metric_key"):
            form.fields[field].disabled = True
    if request.method == "POST" and form.is_valid():
        try:
            action = services.create_action(organization=request.organization, actor=request.user, **form.cleaned_data)
            messages.success(request, "Tindakan ditetapkan. Evaluasi hasilnya setelah dikerjakan.")
            return redirect("marketing:action_detail", pk=action.pk)
        except ValidationError as exc:
            _form_error(form, exc)
    return _form_page(request, form, "Tetapkan tindakan", "Pilih penanggung jawab dan tenggat untuk mengubah temuan menjadi perbaikan.", "advisor", "marketing:advisor",
                      recommendation=recommendation, recommendation_token=token, note_title="Tindakan dengan dasar yang jelas")


@require_http_methods(["GET", "POST"])
@organization_required
def action_detail(request, pk):
    require_role(request, *READ)
    action = get_object_or_404(MarketingAction.objects.select_related("owner"), organization=request.organization, pk=pk)
    if request.method == "POST":
        require_role(request, *WRITE)
    form = ActionUpdateForm(request.POST or None, organization=request.organization, instance=action)
    if request.method == "POST" and form.is_valid():
        try:
            services.update_action(organization=request.organization, actor=request.user, action=action,
                                   expected_version=_version(request), **form.cleaned_data)
            messages.success(request, "Hasil tindakan disimpan.")
            return redirect("marketing:action_detail", pk=action.pk)
        except ValidationError as exc:
            _form_error(form, exc)
    return render(request, "marketing/action_detail.html", _context(request, "advisor", action=action, form=form))


@require_http_methods(["GET", "POST"])
@organization_required
def ai_policy(request):
    require_role(request, "owner")
    policy = MarketingAIPolicy.objects.filter(organization=request.organization).first()
    form = MarketingAIPolicyForm(request.POST or None, initial={"enabled": policy.enabled if policy else False,
                                "monthly_budget_usd": policy.monthly_budget_usd if policy else 0,
                                "request_cap_usd": policy.request_cap_usd if policy else 0})
    if request.method == "POST" and form.is_valid():
        try:
            services.configure_marketing_ai_policy(organization=request.organization, actor=request.user, **form.cleaned_data)
            messages.success(request, "Kebijakan AI marketing disimpan.")
            return redirect("marketing:advisor")
        except ValidationError as exc:
            _form_error(form, exc)
    return _form_page(request, form, "Pengaturan AI marketing", "Persetujuan pemilik untuk pemrosesan agregat oleh penyedia model AI.", "advisor", "marketing:advisor",
                      note_title="Data yang dikirim ke penyedia AI",
                      note="Jika diaktifkan dan koneksi tersedia, OpenRouter menerima angka agregat marketing serta kode saran untuk mengurutkan prioritas. Nama prospek, identitas anggota, nama kampanye, dan pertanyaan Anda tetap lokal. Ada biaya pemakaian dalam USD sesuai batas ini. Angka dan saran tetap dihitung aplikasi; model tidak mengubah anggaran atau menghubungi prospek. Pengaturan default memakai aturan aplikasi tanpa penyedia eksternal.")


@require_http_methods(["GET", "POST"])
@organization_required
def ad_import(request):
    require_role(request, *WRITE)
    from .imports import import_ad_csv
    form = AdImportForm(request.POST or None, request.FILES or None, organization=request.organization)
    if request.method == "POST" and form.is_valid():
        try:
            result = import_ad_csv(organization=request.organization, actor=request.user,
                                   campaign=form.cleaned_data["campaign"], content=form.cleaned_data["file"].read())
            messages.success(request, f"Impor selesai: {result['created']} catatan baru, {result['duplicates']} catatan yang sudah ada.")
            return redirect("marketing:campaigns")
        except ValidationError as exc:
            _form_error(form, exc)
    return _form_page(request, form, "Impor hasil iklan", "Unggah catatan harian satu kampanye menggunakan template CSV.", "campaigns", "marketing:campaigns",
                      template_url=reverse("marketing:ad_template"), note_title="Satu sumber, satu catatan",
                      note="Gunakan referensi unik dari laporan untuk mencegah pencatatan ganda. Biarkan angka kosong bila sumber belum tersedia. Jangan mencampurkan total harian dengan rincian iklan pada tanggal yang sama.")


@require_GET
@organization_required
def ad_template(request):
    require_role(request, *READ)
    from .imports import ad_csv_template
    response = HttpResponse(ad_csv_template(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="template-hasil-iklan.csv"'
    return response
