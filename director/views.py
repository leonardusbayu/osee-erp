"""Organization-scoped Director UI. Domain writes are delegated to services."""
import csv
import io
import uuid
from decimal import Decimal

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Q, Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from core.access import organization_required, require_role
from core.audit import record
from . import services
from .analytics import dashboard_snapshot
from .calculators import cash_forecast
from .forms import (ObjectiveForm, DecisionForm, BudgetForm, BudgetEntryForm,
                    CashPlanForm, CashLineForm, MarketingForm, ScenarioForm,
                    ActionForm, ReasonForm, ChatForm, AIPolicyForm, MarketingImportForm, CHANNELS)
from .models import (Objective, Decision, Budget, BudgetEntry, CashPlan, CashPlanLine,
                     Scenario, MarketingObservation, DirectorConversation, DirectorMessage, DirectorAIPolicy)

READ = ("owner", "director", "finance", "auditor")
WRITE = ("owner", "director", "finance")
MARKETING_READ = READ + ("marketing",)
MARKETING_WRITE = WRITE + ("marketing",)
MONTHS = ("", "Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des")


def _year(request):
    try:
        value = int(request.GET.get("year", timezone.localdate().year))
        if not 2000 <= value <= 2200:
            raise ValueError
        return value
    except (TypeError, ValueError) as exc:
        raise ValidationError("Tahun harus antara 2000 dan 2200.") from exc


def _context(request, nav, **values):
    return {"active_nav": "director_" + nav,
            "director_write": request.membership.role in WRITE,
            "director_approve": request.membership.role in {"owner", "director"},
            "marketing_write": request.membership.role in MARKETING_WRITE,
            "year_options": range(2024, max(timezone.localdate().year + 3, 2029)), **values}


def _snapshot(request):
    year = _year(request)
    result = dashboard_snapshot(organization=request.organization, year=year)
    amounts = [Decimal(row["reported_amount"]) for row in result["monthly_series"] if row.get("reported_amount") is not None]
    maximum = max(amounts, default=Decimal("0"))
    for row in result["monthly_series"]:
        value = row.get("reported_amount")
        row["bar_height"] = str(round(Decimal(value) / maximum * 100, 2)) if value is not None and maximum > 0 else "0"
        row["bar_value"] = format(Decimal(value) / Decimal("1000000"), ".1f").replace(".", ",") if value is not None else "—"
        row["short_label"] = MONTHS[int(row["period"][-2:])]
    return result


def _form_error(form, error):
    if hasattr(error, "message_dict"):
        for key, errors in error.message_dict.items():
            for message in errors:
                form.add_error(key if key in form.fields else None, message)
    else:
        for message in error.messages:
            form.add_error(None, message)


def _error(request, error):
    messages.error(request, " ".join(error.messages))


def _version(request):
    try:
        version = int(request.POST.get("expected_version", ""))
        if version < 1:
            raise ValueError
        return version
    except (TypeError, ValueError) as exc:
        raise ValidationError("Versi data tidak valid. Muat ulang halaman sebelum menyimpan.") from exc


def _form_page(request, form, title, description, nav, back_url, **extra):
    return render(request, "director/form.html", _context(request, nav, form=form, page_title=title,
                  description=description, section_label={"objectives": "TARGET PERUSAHAAN", "decisions": "KEPUTUSAN", "budgets": "ANGGARAN", "cash": "RENCANA KAS", "marketing": "MARKETING", "scenarios": "SIMULASI", "ai": "AI DIREKTUR"}.get(nav, nav.upper()), back_url=back_url, **extra))


@require_GET
@organization_required
def overview(request):
    if request.membership.role == "marketing":
        return redirect("director:marketing")
    require_role(request, *READ)
    try:
        snapshot = _snapshot(request)
    except ValidationError as exc:
        return HttpResponse(" ".join(exc.messages), status=400)
    org = request.organization
    decisions = Decision.objects.filter(organization=org).exclude(status__in=["completed", "rejected"]).order_by("-created_at")[:4]
    objectives = Objective.objects.filter(organization=org).exclude(status="completed").order_by("period_end")[:4]
    return render(request, "director/overview.html", _context(request, "overview", snapshot=snapshot,
                  year=snapshot["year"], decisions=decisions, objectives=objectives,
                  pending_decisions=Decision.objects.filter(organization=org, status__in=["submitted", "reviewed"]).count(),
                  active_objectives=Objective.objects.filter(organization=org, status="active").count()))


@require_GET
@organization_required
def growth(request):
    require_role(request, *READ)
    try:
        snapshot = _snapshot(request)
    except ValidationError as exc:
        return HttpResponse(" ".join(exc.messages), status=400)
    return render(request, "director/growth.html", _context(request, "growth", snapshot=snapshot, year=snapshot["year"]))


@require_GET
@organization_required
def readiness(request):
    require_role(request, *READ)
    try:
        snapshot = _snapshot(request)
    except ValidationError as exc:
        return HttpResponse(" ".join(exc.messages), status=400)
    return render(request, "director/readiness.html", _context(request, "readiness", snapshot=snapshot, year=snapshot["year"]))


@require_GET
@organization_required
def objectives(request):
    require_role(request, *READ)
    rows = Objective.objects.filter(organization=request.organization).order_by("-created_at")
    return render(request, "director/objectives.html", _context(request, "objectives", objectives=rows))


@require_http_methods(["GET", "POST"])
@organization_required
def objective_edit(request, pk=None):
    require_role(request, *WRITE)
    instance = get_object_or_404(Objective, organization=request.organization, pk=pk) if pk else None
    form = ObjectiveForm(request.POST or None, instance=instance, initial={} if instance else {"period_start": timezone.localdate()})
    if request.method == "POST" and form.is_valid():
        try:
            if instance:
                services.update_objective(organization=request.organization, actor=request.user, objective=instance,
                                          expected_version=_version(request), **form.cleaned_data)
            else:
                services.create_objective(organization=request.organization, actor=request.user, **form.cleaned_data)
            messages.success(request, "Sasaran perusahaan disimpan.")
            return redirect("director:objectives")
        except ValidationError as exc:
            _form_error(form, exc)
    return _form_page(request, form, "Perbarui sasaran" if instance else "Buat sasaran perusahaan",
                      "Tentukan hasil yang ingin dicapai, ukuran keberhasilan, dan pemiliknya.", "objectives",
                      reverse("director:objectives"), expected_version=instance.version if instance else None)


@require_GET
@organization_required
def decisions(request):
    require_role(request, *READ)
    query = request.GET.get("q", "").strip()[:100]
    rows = Decision.objects.filter(organization=request.organization).order_by("-created_at")
    if query:
        rows = rows.filter(Q(title__icontains=query) | Q(owner_name__icontains=query))
    status = request.GET.get("status", "")
    if status in {"draft", "submitted", "reviewed", "approved", "active", "completed", "rejected"}:
        rows = rows.filter(status=status)
    return render(request, "director/decisions.html", _context(request, "decisions", page=Paginator(rows, 20).get_page(request.GET.get("page")), query=query, status=status))


@require_http_methods(["GET", "POST"])
@organization_required
def decision_edit(request, pk=None):
    require_role(request, *WRITE)
    instance = get_object_or_404(Decision, organization=request.organization, pk=pk) if pk else None
    form = DecisionForm(request.POST or None, instance=instance)
    if request.method == "POST" and form.is_valid():
        try:
            if instance:
                obj = services.update_decision(organization=request.organization, actor=request.user, decision=instance,
                                               expected_version=_version(request), **form.cleaned_data)
            else:
                obj = services.create_decision(organization=request.organization, actor=request.user, **form.cleaned_data)
            messages.success(request, "Draf keputusan disimpan. Ajukan setelah dasar dan tindakannya siap.")
            return redirect("director:decision_detail", pk=obj.pk)
        except ValidationError as exc:
            _form_error(form, exc)
    return _form_page(request, form, "Edit draf keputusan" if instance else "Usulkan keputusan",
                      "Hubungkan tindakan dengan dasar angka, risiko, dan penanggung jawab.", "decisions",
                      reverse("director:decision_detail", args=[pk]) if pk else reverse("director:decisions"),
                      expected_version=instance.version if instance else None)


@require_GET
@organization_required
def decision_detail(request, pk):
    require_role(request, *READ)
    decision = get_object_or_404(Decision, organization=request.organization, pk=pk)
    actions = []
    role = request.membership.role
    if role in WRITE:
        if decision.status == "draft": actions.append(("submit", "Ajukan untuk ditinjau"))
        if decision.status == "submitted" and role in {"owner", "finance"}: actions.append(("review", "Catat pemeriksaan finance"))
        if decision.status == "reviewed" and role in {"owner", "director"}: actions.append(("approve", "Setujui versi ini"))
        if decision.status in {"submitted", "reviewed"} and role in {"owner", "director"}: actions.append(("reject", "Tolak dengan alasan"))
        if decision.status == "approved" and role in {"owner", "director"}: actions.append(("activate", "Catat mulai pelaksanaan"))
        if decision.status == "active" and role in {"owner", "director"}: actions.append(("complete", "Catat hasil dan selesaikan"))
    from core.models import AuditEvent
    events = AuditEvent.objects.filter(organization=request.organization, object_id=str(pk), action__startswith="director.decision").select_related("actor").order_by("created_at")
    action_forms = [(action, label, ActionForm(initial={"expected_version": decision.version}, auto_id=f"id_{action}_%s")) for action, label in actions]
    for event in events:
        event.display_action = {"director.decision.created": "Draf dibuat", "director.decision.updated": "Draf diperbarui", "director.decision.submit": "Diajukan", "director.decision.review": "Diperiksa finance", "director.decision.approve": "Disetujui", "director.decision.reject": "Ditolak", "director.decision.activate": "Mulai pelaksanaan", "director.decision.complete": "Hasil dicatat"}.get(event.action, "Perubahan keputusan dicatat")
    return render(request, "director/decision_detail.html", _context(request, "decisions", decision=decision,
                  actions=action_forms, events=events))


@require_POST
@organization_required
def decision_action(request, pk, action):
    require_role(request, *WRITE)
    decision = get_object_or_404(Decision, organization=request.organization, pk=pk)
    form = ActionForm(request.POST)
    if form.is_valid():
        try:
            services.transition_decision(organization=request.organization, actor=request.user, decision=decision,
                                         action=action, **form.cleaned_data)
            messages.success(request, "Status keputusan diperbarui dan dicatat dalam jejak aktivitas.")
        except ValidationError as exc:
            _error(request, exc)
    else:
        messages.error(request, "Lengkapi catatan dan versi keputusan yang valid.")
    return redirect("director:decision_detail", pk=pk)


@require_GET
@organization_required
def budgets(request):
    require_role(request, *READ)
    try: year = _year(request)
    except ValidationError as exc: return HttpResponse(" ".join(exc.messages), status=400)
    rows = list(Budget.objects.filter(organization=request.organization, year=year).prefetch_related("entries").order_by("-month", "channel", "title"))
    approved = [b for b in rows if b.status == "approved"]
    totals = {"approved": sum((b.amount for b in approved), Decimal("0")),
              "consumed": sum((b.consumed for b in approved), Decimal("0")),
              "available": sum((b.available for b in approved), Decimal("0")),
              "payments": sum((b.payments for b in approved), Decimal("0"))}
    return render(request, "director/budgets.html", _context(request, "budgets", budgets=rows, totals=totals, year=year))


@require_http_methods(["GET", "POST"])
@organization_required
def budget_edit(request, pk=None):
    require_role(request, *WRITE)
    instance = get_object_or_404(Budget, organization=request.organization, pk=pk) if pk else None
    today = timezone.localdate()
    form = BudgetForm(request.POST or None, instance=instance, initial={} if instance else {"year": today.year, "month": today.month})
    if request.method == "POST" and form.is_valid():
        try:
            if instance:
                obj = services.update_budget(organization=request.organization, actor=request.user, budget=instance,
                                             expected_version=_version(request), **form.cleaned_data)
            else:
                obj = services.create_budget(organization=request.organization, actor=request.user, **form.cleaned_data)
            messages.success(request, "Draf anggaran disimpan.")
            return redirect("director:budget_detail", pk=obj.pk)
        except ValidationError as exc: _form_error(form, exc)
    return _form_page(request, form, "Edit draf anggaran" if instance else "Buat anggaran", "Tetapkan batas belanja per kanal dan periode.", "budgets",
                      reverse("director:budget_detail", args=[pk]) if pk else reverse("director:budgets"),
                      expected_version=instance.version if instance else None,
                      note="Persetujuan anggaran adalah batas rencana belanja. Saldo kas dan otorisasi pembayaran tetap diperiksa terpisah.")


@require_GET
@organization_required
def budget_detail(request, pk):
    require_role(request, *READ)
    budget = get_object_or_404(Budget, organization=request.organization, pk=pk)
    entries = BudgetEntry.objects.filter(organization=request.organization, budget=budget).order_by("-date", "-pk")
    return render(request, "director/budget_detail.html", _context(request, "budgets", budget=budget, entries=entries,
                  form=ActionForm(initial={"expected_version": budget.version})))


@require_POST
@organization_required
def budget_action(request, pk, action):
    require_role(request, *WRITE)
    budget = get_object_or_404(Budget, organization=request.organization, pk=pk)
    form = ActionForm(request.POST)
    if form.is_valid():
        try:
            services.transition_budget(organization=request.organization, actor=request.user, budget=budget,
                                       action=action, **form.cleaned_data)
            messages.success(request, "Status anggaran diperbarui. Ini bukan otorisasi transfer atau aktivasi iklan.")
        except ValidationError as exc: _error(request, exc)
    else: messages.error(request, "Lengkapi catatan dan versi anggaran yang valid.")
    return redirect("director:budget_detail", pk=pk)


@require_http_methods(["GET", "POST"])
@organization_required
def budget_entry(request, pk, entry_pk=None):
    require_role(request, *WRITE)
    budget = get_object_or_404(Budget, organization=request.organization, pk=pk)
    entry = get_object_or_404(BudgetEntry, organization=request.organization, budget=budget, pk=entry_pk) if entry_pk else None
    initial = {field: getattr(entry, field) for field in BudgetEntryForm.Meta.fields} if entry else {"date": timezone.localdate()}
    form = BudgetEntryForm(request.POST or None, initial=initial)
    if entry:
        form.fields["reason"] = ReasonForm.base_fields["reason"].__deepcopy__({})
    key = request.POST.get("idempotency_key", "") if request.method == "POST" else str(uuid.uuid4())
    if request.method == "POST" and form.is_valid():
        try:
            if not key or len(key) > 100: raise ValidationError("Referensi permintaan tidak valid. Muat ulang halaman.")
            data = dict(form.cleaned_data)
            if entry:
                reason = data.pop("reason")
                services.replace_budget_entry(organization=request.organization, actor=request.user, entry=entry, reason=reason, idempotency_key=key, **data)
            else:
                services.add_budget_entry(organization=request.organization, actor=request.user, budget=budget, idempotency_key=key, **data)
            messages.success(request, "Catatan anggaran disimpan.")
            return redirect("director:budget_detail", pk=pk)
        except ValidationError as exc: _form_error(form, exc)
    return _form_page(request, form, "Ganti catatan anggaran" if entry else "Catat penggunaan anggaran",
                      budget.title, "budgets", reverse("director:budget_detail", args=[pk]), entry_key=key,
                      note="Saat komitmen berubah menjadi biaya, ganti catatan sebelumnya. Pembayaran hanya mencatat penyelesaian kas dan tidak mengurangi anggaran lagi.")


@require_GET
@organization_required
def cash(request):
    require_role(request, *READ)
    plans = CashPlan.objects.filter(organization=request.organization).order_by("-created_at")
    return render(request, "director/cash.html", _context(request, "cash", plans=plans))


@require_http_methods(["GET", "POST"])
@organization_required
def cash_edit(request, pk=None):
    require_role(request, *WRITE)
    instance = get_object_or_404(CashPlan, organization=request.organization, pk=pk) if pk else None
    form = CashPlanForm(request.POST or None, instance=instance, initial={} if instance else {"as_of": timezone.localdate(), "reserve": 0})
    if request.method == "POST" and form.is_valid():
        try:
            if instance:
                obj = services.update_cash_plan(organization=request.organization, actor=request.user, plan=instance,
                                                expected_version=_version(request), **form.cleaned_data)
            else: obj = services.create_cash_plan(organization=request.organization, actor=request.user, **form.cleaned_data)
            messages.success(request, "Rencana kas disimpan sebagai asumsi perencanaan.")
            return redirect("director:cash_detail", pk=obj.pk)
        except ValidationError as exc: _form_error(form, exc)
    return _form_page(request, form, "Perbarui rencana kas" if instance else "Buat rencana kas 13 minggu",
                      "Uji kecukupan dana dengan saldo dan jadwal yang Anda masukkan.", "cash",
                      reverse("director:cash_detail", args=[pk]) if pk else reverse("director:cash"),
                      expected_version=instance.version if instance else None,
                      note="Rencana tetap berlabel asumsi. Kewajiban belum lengkap atau setelah 13 minggu dapat membuat saldo terlihat lebih longgar dari keadaan sebenarnya.")


@require_GET
@organization_required
def cash_detail(request, pk):
    require_role(request, *READ)
    plan = get_object_or_404(CashPlan, organization=request.organization, pk=pk)
    lines = list(CashPlanLine.objects.filter(organization=request.organization, plan=plan).order_by("date", "pk"))
    forecast = cash_forecast(opening_balance=plan.opening_balance, as_of=plan.as_of, reserve=plan.reserve, lines=lines)
    return render(request, "director/cash_detail.html", _context(request, "cash", plan=plan, lines=lines, forecast=forecast))


@require_http_methods(["GET", "POST"])
@organization_required
def cash_line(request, pk, line_pk=None):
    require_role(request, *WRITE)
    plan = get_object_or_404(CashPlan, organization=request.organization, pk=pk)
    line = get_object_or_404(CashPlanLine, organization=request.organization, plan=plan, pk=line_pk) if line_pk else None
    initial = {field: getattr(line, field) for field in CashLineForm.Meta.fields} if line else {"date": plan.as_of}
    form = CashLineForm(request.POST or None, initial=initial)
    if line: form.fields["reason"] = ReasonForm.base_fields["reason"].__deepcopy__({})
    if request.method == "POST" and form.is_valid():
        try:
            data = dict(form.cleaned_data)
            if line:
                reason = data.pop("reason")
                services.replace_cash_line(organization=request.organization, actor=request.user, line=line, reason=reason, **data)
            else: services.add_cash_line(organization=request.organization, actor=request.user, plan=plan, **data)
            messages.success(request, "Jadwal arus dana disimpan.")
            return redirect("director:cash_detail", pk=pk)
        except ValidationError as exc: _form_error(form, exc)
    return _form_page(request, form, "Ganti jadwal arus dana" if line else "Tambah jadwal arus dana", plan.title, "cash",
                      reverse("director:cash_detail", args=[pk]), note="Masukkan sisa penerimaan atau pembayaran yang belum terjadi. Uang yang sudah termasuk saldo awal tidak dicatat masuk lagi.")


@require_GET
@organization_required
def marketing(request):
    require_role(request, *MARKETING_READ)
    try: year = _year(request)
    except ValidationError as exc: return HttpResponse(" ".join(exc.messages), status=400)
    rows = MarketingObservation.objects.filter(organization=request.organization, date__year=year).order_by("-date", "-pk")
    summaries = []
    for code, label in CHANNELS:
        active = rows.filter(channel=code, status="active")
        summary = active.aggregate(spend=Sum("spend"), leads=Sum("leads"), paid_orders=Sum("paid_orders"), reported_value=Sum("reported_value"))
        summary.update(code=code, label=label, count=active.count(), missing_spend=active.filter(spend__isnull=True).count())
        summaries.append(summary)
    channel = request.GET.get("channel", "")
    if channel in dict(CHANNELS): rows = rows.filter(channel=channel)
    return render(request, "director/marketing.html", _context(request, "marketing", summaries=summaries, year=year, selected_channel=channel,
                  page=Paginator(rows, 25).get_page(request.GET.get("page"))))


@require_http_methods(["GET", "POST"])
@organization_required
def marketing_edit(request, pk=None):
    require_role(request, *MARKETING_WRITE)
    instance = get_object_or_404(MarketingObservation, organization=request.organization, pk=pk) if pk else None
    initial = {field: getattr(instance, field) for field in MarketingForm.Meta.fields} if instance else {"date": timezone.localdate()}
    form = MarketingForm(request.POST or None, initial=initial)
    if instance: form.fields["reason"] = ReasonForm.base_fields["reason"].__deepcopy__({})
    if request.method == "POST" and form.is_valid():
        try:
            data = dict(form.cleaned_data)
            if instance:
                reason = data.pop("reason")
                services.replace_marketing_observation(organization=request.organization, actor=request.user, observation=instance, reason=reason, **data)
            else: services.create_marketing_observation(organization=request.organization, actor=request.user, **data)
            messages.success(request, "Catatan sumber marketing disimpan. Angka ini belum menjadi transaksi pembukuan.")
            return redirect("director:marketing")
        except ValidationError as exc: _form_error(form, exc)
    return _form_page(request, form, "Koreksi catatan marketing" if instance else "Catat hasil marketing",
                      "Simpan angka dari laporan kanal beserta referensinya. Biarkan kosong bila belum diketahui.", "marketing", reverse("director:marketing"),
                      note="Jangan menjumlahkan klaim konversi semua kanal menjadi omzet. Satu pelanggan dapat melewati Google, WA, dan sales sebelum membeli.")


@require_http_methods(["GET", "POST"])
@organization_required
def void_record(request, kind, pk):
    if kind == "marketing":
        require_role(request, *MARKETING_WRITE)
        obj = get_object_or_404(MarketingObservation, organization=request.organization, pk=pk)
        back = reverse("director:marketing"); nav = "marketing"
        call, key = services.void_marketing_observation, "observation"
    elif kind == "budget":
        require_role(request, *WRITE)
        obj = get_object_or_404(BudgetEntry, organization=request.organization, pk=pk)
        back = reverse("director:budget_detail", args=[obj.budget_id]); nav = "budgets"
        call, key = services.void_budget_entry, "entry"
    elif kind == "cash":
        require_role(request, *WRITE)
        obj = get_object_or_404(CashPlanLine, organization=request.organization, pk=pk)
        back = reverse("director:cash_detail", args=[obj.plan_id]); nav = "cash"
        call, key = services.void_cash_line, "line"
    else:
        return HttpResponse("Jenis catatan tidak valid.", status=404)
    form = ReasonForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            call(organization=request.organization, actor=request.user, **{key: obj}, **form.cleaned_data)
            messages.success(request, "Catatan dibatalkan; riwayatnya tetap tersimpan.")
            return redirect(back)
        except ValidationError as exc: _form_error(form, exc)
    return _form_page(request, form, "Batalkan catatan", "Catatan lama tetap tersedia untuk pemeriksaan.", nav, back, submit_label="Batalkan catatan")


@require_GET
@organization_required
def scenarios(request):
    require_role(request, *READ)
    rows = Scenario.objects.filter(organization=request.organization).order_by("-created_at")
    return render(request, "director/scenarios.html", _context(request, "scenarios", scenarios=rows))


@require_http_methods(["GET", "POST"])
@organization_required
def scenario_create(request):
    require_role(request, *WRITE)
    form = ScenarioForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            inputs = {key: str(value) if isinstance(value, Decimal) else value for key, value in form.cleaned_data.items() if key != "title" and value is not None}
            obj = services.save_scenario(organization=request.organization, actor=request.user,
                                         title=form.cleaned_data["title"], kind="price_cost", inputs=inputs)
            return redirect("director:scenario_detail", pk=obj.pk)
        except ValidationError as exc: _form_error(form, exc)
    return _form_page(request, form, "Simulasikan harga, biaya, dan volume", "Bandingkan kontribusi sebelum dan sesudah perubahan; harga pada invoice tetap sama.",
                      "scenarios", reverse("director:scenarios"), submit_label="Hitung & simpan simulasi",
                      note="Gunakan biaya dan harga untuk produk yang sama. Hasil adalah skenario berdasarkan input, bukan laba aktual atau prediksi permintaan.")


@require_GET
@organization_required
def scenario_detail(request, pk):
    require_role(request, *READ)
    scenario = get_object_or_404(Scenario, organization=request.organization, pk=pk)
    return render(request, "director/scenario_detail.html", _context(request, "scenarios", scenario=scenario, result=scenario.results))


@require_http_methods(["GET", "POST"])
@organization_required
def assistant(request):
    require_role(request, *WRITE)
    from .ai import ask_director, DirectorRateLimited
    try: year = _year(request)
    except ValidationError as exc: return HttpResponse(" ".join(exc.messages), status=400)
    conversation = None
    conversation_id = request.GET.get("conversation")
    if conversation_id:
        try: conversation_id = int(conversation_id)
        except ValueError: return HttpResponse("Percakapan tidak valid.", status=400)
        conversation = get_object_or_404(DirectorConversation, organization=request.organization, created_by=request.user, pk=conversation_id)
    form = ChatForm(request.POST or None, initial={"year": year, "conversation_id": conversation.pk if conversation else None})
    if request.method == "POST" and form.is_valid():
        try:
            if form.cleaned_data.get("conversation_id"):
                get_object_or_404(DirectorConversation, organization=request.organization, created_by=request.user,
                                  pk=form.cleaned_data["conversation_id"])
            result = ask_director(organization=request.organization, actor=request.user, **form.cleaned_data)
            return redirect(reverse("director:assistant") + f'?year={form.cleaned_data["year"]}&conversation={result["conversation_id"]}')
        except ValidationError as exc: _form_error(form, exc)
        except DirectorRateLimited:
            form.add_error(None, "Batas pertanyaan sementara tercapai. Coba kembali nanti; data dan simulasi tetap dapat dibuka.")
    conversations = DirectorConversation.objects.filter(organization=request.organization, created_by=request.user).order_by("-created_at")[:20]
    history = DirectorMessage.objects.filter(organization=request.organization, conversation=conversation).order_by("created_at", "pk") if conversation else []
    policy = DirectorAIPolicy.objects.filter(organization=request.organization).first()
    return render(request, "director/assistant.html", _context(request, "ai", form=form, conversations=conversations,
                  conversation=conversation, history=history, year=year, policy=policy))


@require_http_methods(["GET", "POST"])
@organization_required
def ai_policy(request):
    require_role(request, "owner")
    from .ai import configure_ai_policy, DISCLOSURE_TEXT
    policy = DirectorAIPolicy.objects.filter(organization=request.organization).first()
    form = AIPolicyForm(request.POST or None, initial={"enabled": policy.enabled if policy else False,
                       "monthly_budget_usd": policy.monthly_budget_usd if policy else 0,
                       "request_cap_usd": policy.request_cap_usd if policy else 0})
    if request.method == "POST" and form.is_valid():
        try:
            configure_ai_policy(organization=request.organization, actor=request.user, **form.cleaned_data)
            messages.success(request, "Kebijakan AI Direktur disimpan. Pengiriman hanya aktif jika seluruh konfigurasi endpoint dan batas biaya siap.")
            return redirect("director:assistant")
        except ValidationError as exc: _form_error(form, exc)
    return _form_page(request, form, "Pengaturan AI Direktur", "Kendalikan izin pemrosesan agregat dan biaya AI perusahaan.", "ai", reverse("director:assistant"),
                      note_title="Informasi yang diproses",
                      note=DISCLOSURE_TEXT + " Key, model, dan endpoint dikonfigurasi di server.")


@require_GET
@organization_required
def reports(request):
    require_role(request, *READ)
    try: snapshot = _snapshot(request)
    except ValidationError as exc: return HttpResponse(" ".join(exc.messages), status=400)
    return render(request, "director/reports.html", _context(request, "reports", snapshot=snapshot, year=snapshot["year"],
                  objectives=Objective.objects.filter(organization=request.organization).order_by("period_end")[:30],
                  decisions=Decision.objects.filter(organization=request.organization).order_by("-created_at")[:30]))


def _safe_cell(value):
    text = "" if value is None else str(value)
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else text


@require_GET
@organization_required
def export(request):
    require_role(request, *READ)
    try: snapshot = _snapshot(request)
    except ValidationError as exc: return HttpResponse(" ".join(exc.messages), status=400)
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="osee-direktur-{snapshot["year"]}.csv"'
    response.write("\ufeff")
    writer = csv.writer(response)
    writer.writerow(["Metrik", "Nilai", "Satuan", "Basis", "Cakupan", "Waktu snapshot", "Sumber"])
    for fact in snapshot["facts"]:
        writer.writerow([_safe_cell(fact.get(key)) for key in ["label", "value", "unit", "basis", "coverage"]] + [snapshot["as_of"], _safe_cell(fact.get("source_url"))])
    record(request.organization, request.user, "director.report.exported", detail={"year": snapshot["year"], "format": "csv", "as_of": snapshot["as_of"]})
    return response


@require_GET
@organization_required
def snapshot_api(request):
    require_role(request, *READ)
    try: snapshot = dashboard_snapshot(organization=request.organization, year=_year(request))
    except ValidationError as exc: return JsonResponse({"error": " ".join(exc.messages)}, status=400)
    return JsonResponse(snapshot)


@require_GET
@organization_required
def marketing_template(request):
    require_role(request, *MARKETING_WRITE)
    from .marketing_import import marketing_csv_template
    response = HttpResponse(marketing_csv_template(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="osee-template-marketing.csv"'
    return response


@require_http_methods(["GET", "POST"])
@organization_required
def marketing_import(request):
    require_role(request, *MARKETING_WRITE)
    from .marketing_import import import_marketing_csv
    form = MarketingImportForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            content = form.cleaned_data["file"].read(1024 * 1024 + 1)
            result = import_marketing_csv(organization=request.organization, actor=request.user, content=content)
            messages.success(request, f'{result["created"]} catatan diimpor; {result["skipped"]} catatan identik dilewati. Tidak ada jurnal keuangan yang dibuat.')
            return redirect("director:marketing")
        except ValidationError as exc: _form_error(form, exc)
    return _form_page(request, form, "Impor laporan marketing", "Gunakan template aplikasi. Semua baris divalidasi sebelum disimpan.",
                      "marketing", reverse("director:marketing"), submit_label="Validasi & impor",
                      note="Isi channel dengan mitra, meta_ads, google_ads, whatsapp, seo, atau sales; basis reported atau provisional. Source dan reference wajib. Tanggal YYYY-MM-DD; rupiah tanpa pemisah ribuan. Catatan identik dilewati; konflik memerlukan koreksi eksplisit.")


@require_GET
@organization_required
def report_pdf(request):
    require_role(request, *READ)
    from .documents import management_report_pdf
    try: snapshot = _snapshot(request)
    except ValidationError as exc: return HttpResponse(" ".join(exc.messages), status=400)
    objectives = list(Objective.objects.filter(organization=request.organization).order_by("period_end")[:30])
    decisions = list(Decision.objects.filter(organization=request.organization).order_by("-created_at")[:30])
    content = management_report_pdf(request.organization, snapshot, objectives, decisions)
    response = HttpResponse(content, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="osee-laporan-direktur-{snapshot["year"]}.pdf"'
    record(request.organization, request.user, "director.report.exported", detail={"year": snapshot["year"], "format": "pdf", "as_of": snapshot["as_of"]})
    return response
