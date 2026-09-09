import csv
import io
import json
import uuid

from django.core.exceptions import ValidationError
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from core.access import organization_required
from core.audit import record
from .chat import RateLimited, STARTER_QUESTIONS, answer_question, external_chat_enabled, reserve_chat_request
from .models import ChatConversation, ChatMessage, TaxSource
from .services import annual_readiness, monthly_tax_context, source_cards


@organization_required
@require_GET
def index(request):
    try:
        context = monthly_tax_context(request.organization, request.GET.get("period"))
    except ValidationError:
        return HttpResponse("Pilih masa dengan format YYYY-MM.", status=400)
    return render(request, "app/tax_workspace.html", context)


@organization_required
@require_GET
def annual(request):
    try:
        context = annual_readiness(request.organization, request.GET.get("year"))
    except (ValidationError, TypeError, ValueError):
        return HttpResponse("Pilih tahun yang valid.", status=400)
    from .models import AnnualTaxWorkpaper
    context["workpapers"] = AnnualTaxWorkpaper.objects.filter(organization=request.organization, year=context["year"]).order_by("-pk")
    return render(request, "app/annual_tax.html", context)


@organization_required
@require_GET
def chat(request):
    return render(request, "app/chat.html", {
        "conversations": ChatConversation.objects.filter(organization=request.organization, created_by=request.user)[:30],
        "chat_available": request.membership.role in ("owner", "finance", "reviewer"),
        "external_chat_enabled": external_chat_enabled(),
        "privacy_notice": "Chat bersifat pribadi untuk akun Anda dalam perusahaan ini. Pertanyaan asli, riwayat, dan data perusahaan tetap di ERP; hanya topik proses umum yang dapat dikirim dari panduan publik yang disetujui bila AI diaktifkan.",
        "starter_questions": STARTER_QUESTIONS,
    })


def error(message, code, status):
    response = JsonResponse({"error": message, "code": code}, status=status)
    response["Cache-Control"] = "no-store"
    return response


@organization_required
@require_POST
def chat_api(request):
    if request.membership.role not in ("owner", "finance", "reviewer"):
        return error("Peran pembaca tidak dapat mengirim chat.", "forbidden", 403)
    if request.content_type != "application/json":
        return error("Gunakan permintaan JSON.", "invalid_request", 400)
    try:
        if int(request.META.get("CONTENT_LENGTH") or "0") > 12000 or len(request.body) > 12000:
            raise ValueError
        payload = json.loads(request.body)
        if not isinstance(payload, dict) or set(payload) - {"message", "conversation_id"}:
            raise ValueError
        question = payload.get("message")
        if not isinstance(question, str) or not 1 <= len(question.strip()) <= 2000:
            raise ValueError
        question = question.strip()
        raw_id = payload.get("conversation_id")
        conversation_id = uuid.UUID(str(raw_id)) if raw_id else None
    except (ValueError, TypeError, UnicodeError):
        return error("Tulis pertanyaan hingga 2.000 karakter dan gunakan percakapan yang valid.", "invalid_request", 400)
    conversation = None
    if conversation_id:
        conversation = ChatConversation.objects.filter(pk=conversation_id, organization=request.organization, created_by=request.user).first()
        if not conversation:
            return error("Percakapan tidak tersedia untuk akun ini.", "not_found", 404)
        if conversation.messages.count() >= 100:
            return error("Percakapan ini sudah penuh. Mulai percakapan baru.", "conversation_limit", 400)
    try:
        reserve_chat_request(request.organization, request.user)
    except RateLimited:
        response = error("Batas chat tercapai. Coba lagi nanti; ruang keuangan tetap dapat digunakan.", "rate_limited", 429)
        response["Retry-After"] = "60"
        return response
    if not conversation:
        conversation = ChatConversation.objects.create(organization=request.organization, created_by=request.user, title=question[:100])
    previous = conversation.messages.filter(role="assistant").last()
    previous_topic = previous.metadata.get("topic") if previous else None
    ChatMessage.objects.create(conversation=conversation, role="user", content=question)
    answer = answer_question(request.organization, question, previous_topic)
    message = ChatMessage.objects.create(
        conversation=conversation, role="assistant", content=answer["answer"], mode=answer["mode"],
        source_ids=[source["id"] for source in answer["sources"]],
        metadata={"topic": answer["topic"], "status": answer["status"], "review_required": answer["review_required"], "followups": answer["followups"], "notice": answer["notice"], "source_review_dates": {source["id"]: source["reviewed_on"] for source in answer["sources"]}, "source_cards": answer["sources"], "public_topic_only": True, "prompt_version": "osee-public-process-v2"},
    )
    conversation.updated_at = timezone.now()
    conversation.save(update_fields=["updated_at"])
    record(request.organization, request.user, "tax.chat.answered", conversation, {"message_id": message.pk, "mode": answer["mode"], "source_ids": message.source_ids})
    answer.pop("topic", None)
    response = JsonResponse({**answer, "conversation_id": str(conversation.pk), "message_id": message.pk})
    response["Cache-Control"] = "no-store"
    return response


@organization_required
@require_GET
def conversation_api(request, conversation_id):
    conversation = ChatConversation.objects.filter(pk=conversation_id, organization=request.organization, created_by=request.user).first()
    if not conversation:
        return error("Percakapan tidak tersedia untuk akun ini.", "not_found", 404)
    from .knowledge import historical_cards
    messages = [{
        "id": item.pk, "role": item.role, "content": item.content, "mode": item.mode,
        "sources": historical_cards(item),
        "followups": item.metadata.get("followups", []), "status": item.metadata.get("status", ""),
        "review_required": item.metadata.get("review_required", False), "notice": item.metadata.get("notice", ""),
        "created_at": item.created_at.isoformat(),
    } for item in conversation.messages.all()[:100]]
    response = JsonResponse({"conversation_id": str(conversation.pk), "messages": messages})
    response["Cache-Control"] = "no-store"
    return response


def csv_cell(value):
    text = str(value) if value is not None else "BELUM DITENTUKAN"
    # CSV files may be opened in a spreadsheet; do not execute imported formulas.
    if text.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + text
    return text


@organization_required
@require_GET
def export(request):
    try:
        context = monthly_tax_context(request.organization, request.GET.get("period"))
    except ValidationError:
        return HttpResponse("Pilih masa dengan format YYYY-MM.", status=400)
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["DRAF KERTAS KERJA — BUKAN SPT / BUKAN XML DJP / BUKAN BUKTI BAYAR ATAU LAPOR"])
    writer.writerow(["Perusahaan", csv_cell(request.organization.name), "Masa", context["period_value"]])
    for gate in context["gates"]:
        writer.writerow(["PERLU PEMERIKSAAN", gate["title"], gate["detail"]])
    writer.writerow(["Tipe baris", "Masa", "Jenis pajak", "Arah", "Bruto dokumen", "Dasar pajak", "Tarif pecahan", "Nominal pajak", "Persiapan", "Pembayaran", "Pelaporan", "Referensi sumber", "Referensi aturan", "Pemasok", "Alasan pemeriksaan", "Kode perhitungan", "Versi kalkulator", "Kode objek dari pemeriksa", "ID dokumen", "ID laporan profesional"])
    for item in context["obligations"]:
        writer.writerow([csv_cell(value) for value in ["obligation_draft", item.period.strftime("%Y-%m"), item.get_tax_type_display(), item.get_direction_display(), "", item.base, item.rate, item.amount, item.get_status_display(), item.payment_status, item.filing_status, item.source_reference, item.rule_reference, "", "", item.rule_code, item.calculation_version, item.object_code, item.source_evidence_id, item.external_review_id]])
    for candidate in context["tax_candidates"]:
        writer.writerow([csv_cell(value) for value in ["bill_tax_review_candidate", context["period_value"], "", "", candidate["gross_amount"], "", "", "", "needs_review", "", "", candidate["number"], "", candidate["supplier_name"], candidate["reason"], "", "", "", "", ""]])
    if not context["obligations"] and not context["tax_candidates"]:
        writer.writerow(["Belum ada kertas kerja. Tidak berarti kewajiban pajak nihil."])
    response = HttpResponse("\ufeff" + output.getvalue(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="DRAF-pajak-{context["period_value"]}.csv"'
    response["Cache-Control"] = "no-store"
    record(request.organization, request.user, "tax.workpaper.exported", detail={"period": context["period_value"], "format": "draft_csv"})
    return response
