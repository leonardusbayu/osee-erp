import csv
import io
import json
import zipfile

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_GET

from core.access import organization_required
from core.audit import record
from . import forms
from .models import AnnualTaxWorkpaper, TaxEvidence, TaxObligation, TaxProfile
from .services import parse_period
from . import workflow as service


def form_page(request, form, title, description, *, subject=None, back=None):
    return render(request, "taxes/form.html", {"form": form, "title": title, "description": description,
        "subject": subject, "back_url": back or reverse("taxes:index"), "active_nav": "tax"})


def review_fields(data):
    return {key: data[key] for key in ("evidence", "professional_name", "qualification_reference", "reviewed_on", "statement", "report_confirmed")}


@organization_required
@require_http_methods(["GET", "POST"])
def evidence_upload(request):
    service.authorize(request.organization, request.user)
    form = forms.EvidenceForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            row = service.upload_evidence(organization=request.organization, actor=request.user, file=form.cleaned_data["file"])
        except ValidationError as exc:
            form.add_error(None, exc.messages)
        else:
            messages.success(request, f"Dokumen privat #{row.pk} tersimpan. Pilih dokumen ini pada alur pemeriksaan.")
            return redirect("taxes:index")
    return form_page(request, form, "Unggah bukti pajak", "Unggah dokumen asli yang akan dicocokkan dengan profil, transaksi atau pelaporan. Mengunggah tidak berarti menyetujui atau melaporkan pajak.")


@organization_required
@require_GET
def evidence_download(request, pk):
    row = get_object_or_404(TaxEvidence, organization=request.organization, pk=pk)
    response = HttpResponse(bytes(row.content), content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="bukti-pajak-{row.pk}.pdf"'
    response["Cache-Control"], response["X-Content-Type-Options"] = "no-store", "nosniff"
    record(request.organization, request.user, "tax.evidence.downloaded", row)
    return response


@organization_required
@require_http_methods(["GET", "POST"])
def profile_review(request):
    service.authorize(request.organization, request.user, approve=True)
    profile = TaxProfile.objects.filter(organization=request.organization).first()
    form = forms.ProfileReviewForm(request.POST or None, organization=request.organization,
        initial={"taxpayer_reference": profile.taxpayer_reference if profile else ""})
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                service._lock(request.organization, request.user, approve=True)
                profile = TaxProfile.objects.select_for_update().filter(organization=request.organization).first()
                if not profile or not profile.registration_date or not profile.registration_evidence or not profile.vat_status_evidence or not profile.vat_status_effective_from:
                    raise ValidationError("Lengkapi dokumen pendaftaran dan tanggal/status PPN melalui pengaturan profil terlebih dahulu.")
                data = form.cleaned_data
                if profile.taxpayer_reference != data["taxpayer_reference"]:
                    if profile.reviewed_at:
                        raise ValidationError("Identitas pada profil ditinjau tidak ditimpa; perlukan koreksi dokumen dan versi profil tersendiri.")
                    profile.taxpayer_reference = data["taxpayer_reference"]
                    profile.save()
                decision = service.profile_decision(**data)
                review = service.record_external_review(organization=request.organization, actor=request.user,
                    subject=profile, decision=decision, **review_fields(data))
                service.approve_tax_profile(profile, request.user, **{key: data[key] for key in decision},
                    review_note=data["statement"], external_review=review)
        except ValidationError as exc:
            form.add_error(None, exc.messages)
        else:
            messages.success(request, "Laporan profesional dan persetujuan bisnis tercatat. Perhitungan tetap mengikuti masa dan bukti tiap kewajiban.")
            return redirect("taxes:index")
    return form_page(request, form, "Catat pemeriksaan profesional atas profil", "Pemilik mencatat laporan yang sudah diterima dari profesional kompeten. Referensi kompetensi dicatat, belum diverifikasi aplikasi; omzet kecil tidak otomatis menetapkan pajak final.")


@organization_required
@require_http_methods(["GET", "POST"])
def obligation_new(request):
    service.authorize(request.organization, request.user)
    form = forms.ObligationForm(request.POST or None, organization=request.organization,
        initial={"period": request.GET.get("period", "")})
    if request.method == "POST" and form.is_valid():
        data = dict(form.cleaned_data)
        data["source_evidence"] = data.pop("evidence")
        try:
            row = service.prepare_obligation(organization=request.organization, actor=request.user, **data)
        except ValidationError as exc:
            form.add_error(None, exc.messages)
        else:
            return redirect("taxes:obligation", pk=row.pk)
    return form_page(request, form, "Siapkan kertas kerja masa", "Angka dihitung aplikasi berdasarkan klasifikasi terdokumentasi. Kertas kerja belum disetujui, belum menjadi XML DJP, dan belum dilaporkan. Untuk tagihan pemasok, gunakan tombol Tinjau pajak pada tagihan agar jurnal pemotongan tertaut.")


@organization_required
@require_GET
def obligation_detail(request, pk):
    row = get_object_or_404(TaxObligation.objects.select_related("external_review", "source_evidence"), organization=request.organization, pk=pk)
    return render(request, "taxes/obligation.html", {"obligation": row, "verifications": row.verifications.select_related("evidence", "verifier"),
        "can_approve": request.membership.role in ("owner", "reviewer"), "active_nav": "tax"})


@organization_required
@require_http_methods(["GET", "POST"])
def obligation_review(request, pk):
    service.authorize(request.organization, request.user, approve=True)
    row = get_object_or_404(TaxObligation, organization=request.organization, pk=pk)
    form = forms.ProfessionalReviewForm(request.POST or None, organization=request.organization)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                review = service.record_external_review(organization=request.organization, actor=request.user, subject=row,
                    decision={"action": "approve_obligation"}, **review_fields(form.cleaned_data))
                service.approve_tax_obligation(row, request.user, external_review=review)
        except ValidationError as exc:
            form.add_error(None, exc.messages)
        else:
            return redirect("taxes:obligation", pk=pk)
    return form_page(request, form, "Catat persetujuan kertas kerja", "Cocokkan laporan profesional dengan klasifikasi, masa, dasar, tarif, dan nominal yang ditampilkan. Catatan disetujui tidak dapat ditimpa.", subject=row, back=reverse("taxes:obligation", args=[pk]))


@organization_required
@require_http_methods(["GET", "POST"])
def bill_review(request, pk):
    from finance.models import Bill
    service.authorize(request.organization, request.user, approve=True)
    bill = get_object_or_404(Bill, organization=request.organization, pk=pk)
    form = forms.BillReviewForm(request.POST or None, organization=request.organization, initial={"base": bill.amount - bill.supplier_vat, "period": bill.date.strftime("%Y-%m")})
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                data = form.cleaned_data
                decision_args = {key: data[key] for key in ("rule_code", "base", "period", "reason", "object_code")}
                decision_args["period"] = parse_period(decision_args["period"])
                review = service.record_external_review(organization=request.organization, actor=request.user, subject=bill,
                    decision=service.bill_decision(**decision_args), **review_fields(data))
                row = service.review_bill_tax(organization=request.organization, actor=request.user, bill=bill, external_review=review, **decision_args)
        except ValidationError as exc:
            form.add_error(None, exc.messages)
        else:
            messages.success(request, "Keputusan pajak tagihan tercatat. Pembayaran pemasok dan penyetoran pajak tetap dicocokkan terpisah.")
            return redirect("taxes:obligation", pk=row.obligation_id) if row.obligation_id else redirect("taxes:index")
    return form_page(request, form, "Tinjau pajak tagihan " + bill.number, "Dasar awal dari bruto dikurangi PPN pemasok hanya membantu pencocokan, bukan kesimpulan pajak. Salin klasifikasi, waktu terutang dan dasar dari laporan profesional. Tutor orang pribadi, penerima luar negeri, fasilitas khusus atau gross-up memerlukan perlakuan tersendiri.", subject=bill)


@organization_required
@require_http_methods(["GET", "POST"])
def obligation_verify(request, pk):
    service.authorize(request.organization, request.user, approve=True)
    row = get_object_or_404(TaxObligation, organization=request.organization, pk=pk)
    form = forms.VerificationForm(request.POST or None, organization=request.organization,
        initial={"period": row.period.strftime("%Y-%m"), "amount": row.amount})
    if request.method == "POST" and form.is_valid():
        try:
            service.verify_obligation_evidence(organization=request.organization, actor=request.user, obligation=row, **form.cleaned_data)
        except ValidationError as exc:
            form.add_error(None, exc.messages)
        else:
            return redirect("taxes:obligation", pk=pk)
    return form_page(request, form, "Cocokkan bukti resmi", "Pencocokan dokumen dilakukan oleh akun yang berwenang; tidak ada panggilan verifikasi otomatis ke DJP. Untuk final setor sendiri yang memenuhi syarat, pembayaran tervalidasi memenuhi pelaporan masa; SPT Tahunan tetap terpisah.", subject=row, back=reverse("taxes:obligation", args=[pk]))


@organization_required
@require_http_methods(["GET", "POST"])
def annual_prepare(request):
    service.authorize(request.organization, request.user)
    form = forms.AnnualWorkpaperForm(request.POST or None, organization=request.organization, initial={"year": request.GET.get("year", 2026)})
    if request.method == "POST" and form.is_valid():
        data = dict(form.cleaned_data)
        data["support_evidence"] = data.pop("evidence")
        try:
            row = service.prepare_annual_workpaper(organization=request.organization, actor=request.user, **data)
        except ValidationError as exc:
            form.add_error(None, exc.messages)
        else:
            return redirect("taxes:annual_workpaper", pk=row.pk)
    return form_page(request, form, "Siapkan rekonsiliasi fiskal tahunan", "Laba buku diambil dari jurnal. Koreksi fiskal, kompensasi kerugian dan angsuran berasal dari lampiran profesional yang disimpan; kredit pelanggan hanya dari bukti diverifikasi. Hasil merupakan paket pemeriksaan, belum formulir/XML resmi DJP.", back=reverse("taxes:annual"))


@organization_required
@require_GET
def annual_detail(request, pk):
    row = get_object_or_404(AnnualTaxWorkpaper, organization=request.organization, pk=pk)
    return render(request, "taxes/annual_workpaper.html", {"workpaper": row, "can_approve": request.membership.role in ("owner", "reviewer"), "active_nav": "tax"})


@organization_required
@require_http_methods(["GET", "POST"])
def annual_review(request, pk):
    service.authorize(request.organization, request.user, approve=True)
    row = get_object_or_404(AnnualTaxWorkpaper, organization=request.organization, pk=pk)
    form = forms.ProfessionalReviewForm(request.POST or None, organization=request.organization)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                review = service.record_external_review(organization=request.organization, actor=request.user,
                    subject=row, decision={"action": "approve_annual"}, **review_fields(form.cleaned_data))
                service.approve_annual_workpaper(organization=request.organization, actor=request.user, workpaper=row, external_review=review)
        except ValidationError as exc:
            form.add_error(None, exc.messages)
        else:
            return redirect("taxes:annual_workpaper", pk=pk)
    return form_page(request, form, "Persetujuan paket tahunan", "Pemeriksaan harus mencakup rekonsiliasi fiskal, kelayakan aturan, bukti kredit, pembayaran, dan kelengkapan lampiran. Semua masa harus selesai dan ditutup. Persetujuan ini bukan penerimaan DJP.")


@organization_required
@require_http_methods(["GET", "POST"])
def annual_verify(request, pk):
    service.authorize(request.organization, request.user, approve=True)
    row = get_object_or_404(AnnualTaxWorkpaper, organization=request.organization, pk=pk)
    form = forms.AnnualFilingForm(request.POST or None, organization=request.organization)
    if request.method == "POST" and form.is_valid():
        try:
            service.verify_annual_filing(organization=request.organization, actor=request.user, workpaper=row, **form.cleaned_data)
        except ValidationError as exc:
            form.add_error(None, exc.messages)
        else:
            return redirect("taxes:annual_workpaper", pk=pk)
    return form_page(request, form, "Catat penerimaan SPT Tahunan", "Hanya setelah pelaporan di saluran DJP dan BPE resmi diterima. Cocokkan versi SPT, tahun, dan identitas; tidak ada pelaporan otomatis dari tombol ini.")


@organization_required
@require_GET
def annual_export(request, pk):
    from .views import csv_cell
    row = get_object_or_404(AnnualTaxWorkpaper, organization=request.organization, pk=pk)
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        manifest = {"status": "DRAFT_REVIEW_PACKAGE_NOT_DJP_XML", "official_schema_validated": False,
            "workpaper_id": row.pk, "year": row.year, "calculation_version": row.calculation_version,
            "approval_recorded": hasattr(row, "approval"), "filing_evidence_recorded": hasattr(row, "filing"),
            "inputs": row.inputs, "result": row.result, "book_snapshot": row.book_snapshot}
        archive.writestr("DRAF-manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["DRAF REKONSILIASI FISKAL — BUKAN SPT / XML DJP"])
        for key, value in row.result.items():
            writer.writerow([csv_cell(key), csv_cell(value)])
        archive.writestr("DRAF-rekonsiliasi.csv", "\ufeff" + output.getvalue())
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["Bulan", "Pendapatan buku", "Biaya buku", "Hasil buku", "Ditutup"])
        for month in row.book_snapshot["ledger_months"]:
            writer.writerow([csv_cell(month[key]) for key in ("period", "revenue", "expenses", "book_profit", "closed")])
        archive.writestr("DRAF-buku-bulanan.csv", "\ufeff" + output.getvalue())
        archive.writestr("BACA-DAHULU.txt", "Paket pemeriksaan internal. Bukan formulir atau XML DJP. Cocokkan lampiran, versi aturan, pembetulan, pembayaran dan penerimaan resmi pada saluran DJP. Persetujuan profesional tidak membuktikan sudah dilaporkan.")
    response = HttpResponse(target.getvalue(), content_type="application/zip")
    response["Content-Disposition"] = f'attachment; filename="DRAF-pajak-tahunan-{row.year}-versi-{row.pk}.zip"'
    response["Cache-Control"] = "no-store"
    record(request.organization, request.user, "tax.annual.exported", row, {"official_schema_validated": False})
    return response
