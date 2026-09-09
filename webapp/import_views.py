"""Read-only presentation of imported source facts, separate from posted books."""
import csv
from datetime import date

from django.core.paginator import Paginator
from django.core.exceptions import ValidationError
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils.formats import date_format
from django.views.decorators.http import require_GET

from core.access import organization_required
from core.audit import record
from imports.models import ImportBatch, SourceDocument
from imports.services import import_summary, month_data


def _exception_text(exception):
    payload = exception.payload if hasattr(exception, "payload") else exception
    explanation = payload.get("explanation") or payload.get("message") or payload.get("code", "Perlu pemeriksaan sumber.")
    prefix = []
    if hasattr(exception, "batch"):
        source = exception.batch.payload
        month = next((row for row in source["months"] if row["id"] == payload.get("month_id")), None)
        channel = next((row for row in source["channels"] if row["id"] == payload.get("channel_id")), None)
        if month:
            prefix.append(date_format(date.fromisoformat(month["source_period"] + "-01"), "F Y"))
        if channel:
            prefix.append(channel["source_label"])
    observed, expected = payload.get("observed", {}), payload.get("expected", {})
    facts = ""
    code = payload.get("code")
    from .templatetags.finance_format import money
    if code == "detail_count_mismatch":
        facts = f"Rincian {observed.get('detail_count')}, total tercetak {observed.get('printed_count')}. "
    elif code == "header_price_extension_mismatch":
        facts = f"Subtotal tercetak {money(observed.get('printed_amount'))}; jumlah × harga kolom {money(expected.get('header_price_extension'))}. "
    elif code == "missing_printed_amount":
        facts = f"{observed.get('printed_count')} peserta; nominal belum tercantum. "
    elif code == "date_year_conflict":
        facts = f"Tanggal tertulis {observed.get('raw_date')}, mencakup {observed.get('quantity', 'beberapa')} peserta. "
    elif code == "merged_date_cell_ambiguity":
        facts = f"{observed.get('quantity')} peserta pada sel tanggal {' / '.join(observed.get('source_date_candidates', []))}. "
    elif code == "annual_summary_incomplete_coverage":
        facts = f"Ringkasan tahunan {observed.get('annual_printed_total')} peserta; jumlah seluruh rekap bulanan {observed.get('monthly_printed_total')}. "
    return (" · ".join(prefix) + ": " if prefix else "") + facts + explanation


def _source_links(documents):
    return [{"id": item.pk, "original_filename": item.original_name, "page_count": item.page_count,
             "kind": item.kind, "download_url": reverse("import_source", args=[item.pk])} for item in documents]


def _coverage(value):
    return "Sebagian bulan; kelengkapan belum dikonfirmasi" if value == "partial_source_page" else "Halaman tersedia; kelengkapan bulan perlu dikonfirmasi"


def _locator(value):
    if not isinstance(value, dict):
        return str(value)
    parts = [f"Halaman {value['page']}"] if value.get("page") else []
    if value.get("table_row_index") is not None:
        parts.append(f"baris {value['table_row_index']}")
    if value.get("column_index") is not None:
        parts.append(f"kolom {value['column_index']}")
    return " · ".join(parts)


def overview_context(organization, year=2026):
    data = import_summary(organization=organization, year=year)
    months = []
    for item in data["months"]:
        months.append({"id": item.pk, "source_period": item.source_period, "page": item.source_page,
                       "printed_count": item.printed_count, "printed_amount": item.printed_amount,
                       "detail_count": item.detail_count, "coverage": _coverage(item.coverage),
                       "month_label": date_format(date(item.year, item.month, 1), "F Y"),
                       "detail_url": reverse("imports_month", args=[item.source_period]),
                       "exception_count": sum(1 for e in data["exceptions"] if e.payload.get("month_id") == item.payload.get("id"))})
    batch = data["batches"][0] if data["batches"] else None
    coverage_label = f"{months[0]['month_label']} – {months[-1]['month_label']}" if months else "Belum tersedia"
    partial_months = [date_format(date(item.year, item.month, 1), "F Y") for item in data["months"] if item.coverage == "partial_source_page"]
    return {"summary": data, "months": months, "batch": batch,
            "exceptions": [_exception_text(e) for e in data["exceptions"]],
            "sources": _source_links(data["source_documents"]),
            "supplier_bill": batch.supplier_bill if batch else None,
            "period_year": year, "coverage_label": coverage_label, "partial_months": partial_months, "active_nav": "imports"}


@require_GET
@organization_required
def overview(request):
    try:
        year = int(request.GET.get("year", "2026"))
        if not 2000 <= year <= 2200:
            raise ValueError
    except ValueError:
        return HttpResponse("Tahun tidak valid.", status=400)
    return render(request, "imports/overview.html", overview_context(request.organization, year))


@require_GET
@organization_required
def month(request, period):
    try:
        parsed = date.fromisoformat(period + "-01")
        if not 2000 <= parsed.year <= 2200:
            raise ValueError
    except ValueError:
        raise Http404
    try:
        data = month_data(organization=request.organization, source_period=period)
    except ValidationError as exc:
        raise Http404 from exc
    item = data["month"]
    channels = [dict(row) for row in data["channels"]]
    by_id = {row["id"]: row for row in channels}
    for channel in channels:
        channel["detail_count"] = sum(row["quantity"] for row in data["details"] if row["channel_id"] == channel["id"])
        channel["locator"] = _locator(channel["locator"])
    rows = []
    query = request.GET.get("q", "").strip()[:100]
    for row in data["details"]:
        channel = by_id[row["channel_id"]]
        if query and query.casefold() not in (channel["source_label"] + " " + row["original_date_text"]).casefold():
            continue
        rows.append({**row, "source_label": channel["source_label"], "source_page": item.source_page, "locator": _locator(row["locator"])})
    page = Paginator(rows, 75).get_page(request.GET.get("page"))
    sources = _source_links(data["source_documents"])
    recap_source = next((source for source in sources if source["kind"] == "operating_recap"), sources[0] if sources else None)
    context = {"month": {"source_period": period, "month_label": date_format(parsed, "F Y"), "page": item.source_page,
                         "printed_count": item.printed_count, "detail_count": item.detail_count,
                         "printed_amount": item.printed_amount, "coverage": _coverage(item.coverage), "exception_count": data["exceptions"].count()},
               "batch": data["batch"], "channels": channels, "details": page.object_list, "page_obj": page, "q": query,
               "exceptions": [_exception_text(e) for e in data["exceptions"]],
               "source_pdf_url": recap_source["download_url"] if recap_source else "", "active_nav": "imports"}
    return render(request, "imports/month.html", context)


@require_GET
@organization_required
def source_download(request, pk):
    source = get_object_or_404(SourceDocument, organization=request.organization, pk=pk)
    try:
        handle = source.file.open("rb")
    except FileNotFoundError as exc:
        raise Http404("Dokumen sumber tidak ditemukan.") from exc
    try:
        record(request.organization, request.user, "imports.source.downloaded", obj=source)
    except Exception:
        handle.close()
        raise
    response = FileResponse(handle, as_attachment=True, filename=source.original_name, content_type="application/pdf")
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@require_GET
@organization_required
def export(request):
    from .views import _safe_csv
    data = overview_context(request.organization, 2026)
    response = HttpResponse(content_type="text/csv; charset=utf-8-sig")
    response["Content-Disposition"] = 'attachment; filename="osee-rekap-sumber-2026.csv"'
    response.write("\ufeff")
    writer = csv.writer(response)
    writer.writerow(["Periode sumber", "Peserta pada total tercetak", "Peserta pada rincian", "Nominal rekap tercetak IDR", "Cakupan", "Halaman PDF", "Dasar"])
    for row in data["months"]:
        writer.writerow([row["source_period"], row["printed_count"] if row["printed_count"] is not None else "", row["detail_count"],
                         row["printed_amount"] if row["printed_amount"] is not None else "", _safe_csv(row["coverage"]), row["page"],
                         "Rekap sumber; belum menjadi jurnal, saldo bank, atau dasar pajak terverifikasi"])
    record(request.organization, request.user, "imports.recap.exported", detail={"year": 2026})
    return response
