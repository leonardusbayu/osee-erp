import csv
import io
import json
import zipfile

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods

from core.access import organization_required
from core.audit import record

from . import forms
from . import workflow as service
from .models import AnnualTaxWorkpaper, TaxEvidence, TaxObligation, TaxProfile
from .services import parse_period


def form_page(
    request, form, title, description, *, subject=None, back=None, tax_context=None
):
    return render(
        request,
        "taxes/form.html",
        {
            "form": form,
            "title": title,
            "description": description,
            "subject": subject,
            "back_url": back or reverse("taxes:index"),
            "active_nav": "tax",
            "tax_context": tax_context,
            "pph23_form_fields": forms.PPH23_FORM_FIELDS,
            "pph23_certificate_fields": forms.PPH23_CERTIFICATE_FIELDS,
        },
    )


def review_fields(data):
    return {
        key: data[key]
        for key in (
            "evidence",
            "professional_name",
            "qualification_reference",
            "reviewed_on",
            "statement",
            "report_confirmed",
        )
    }


def staff_tax_errors(error):
    """Keep fiscal errors useful without presenting catalog identifiers to staff."""
    messages = error.messages if hasattr(error, "messages") else error
    from .services import SOURCE_CATALOG

    slugs = {row["slug"] for row in SOURCE_CATALOG}
    result = []
    for message in messages:
        text = (
            (
                "Referensi aturan pajak belum siap untuk tanggal ini. Minta Owner/Direktur menyelesaikan "
                "pemeriksaan dan publikasi referensi bersama pemeriksa pajak. Isian Anda tetap tersimpan."
            )
            if message.startswith("Sumber ") or any(slug in message for slug in slugs)
            else message
        )
        if text not in result:
            result.append(text)
    return result


def pph23_fields(data, *, bill=False):
    if not data.get("rule_code", "").startswith("pph23_"):
        return {}
    from .recipient_identity import pph23_object_decision

    fields = {
        key: data[key]
        for key in (
            "recipient_identity",
            "tax_point_date",
            "tax_point_basis",
            "withholder_reference",
            "withholder_name",
        )
    }
    if not bill:
        fields.update(
            {key: data.get(key) for key in ("recipient_party", "withholder_party")}
        )
    fields["object_decision"] = pph23_object_decision(
        reason=data["object_reason"],
        individual_21_26_excluded=data["individual_21_26_excluded"],
        final_exempt_excluded=data["final_exempt_excluded"],
    )
    return fields


@organization_required
@require_http_methods(["GET", "POST"])
def evidence_upload(request):
    service.authorize(request.organization, request.user)
    form = forms.EvidenceForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            row = service.upload_evidence(
                organization=request.organization,
                actor=request.user,
                file=form.cleaned_data["file"],
            )
        except ValidationError as exc:
            form.add_error(None, exc.messages)
        else:
            messages.success(
                request,
                f"Dokumen privat #{row.pk} tersimpan. Pilih dokumen ini pada alur pemeriksaan.",
            )
            return redirect("taxes:index")
    return form_page(
        request,
        form,
        "Unggah bukti pajak",
        "Unggah dokumen asli yang akan dicocokkan dengan profil, transaksi atau pelaporan. Mengunggah tidak berarti menyetujui atau melaporkan pajak.",
    )


@organization_required
@require_GET
def evidence_download(request, pk):
    row = get_object_or_404(TaxEvidence, organization=request.organization, pk=pk)
    response = HttpResponse(bytes(row.content), content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="bukti-pajak-{row.pk}.pdf"'
    response["Cache-Control"], response["X-Content-Type-Options"] = (
        "no-store",
        "nosniff",
    )
    record(request.organization, request.user, "tax.evidence.downloaded", row)
    return response


@organization_required
@require_http_methods(["GET", "POST"])
def profile_review(request):
    service.authorize(request.organization, request.user, approve=True)
    profile = TaxProfile.objects.filter(organization=request.organization).first()
    form = forms.ProfileReviewForm(
        request.POST or None,
        organization=request.organization,
        initial={"taxpayer_reference": profile.taxpayer_reference if profile else ""},
    )
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                service._lock(request.organization, request.user, approve=True)
                profile = (
                    TaxProfile.objects.select_for_update()
                    .filter(organization=request.organization)
                    .first()
                )
                if (
                    not profile
                    or not profile.registration_date
                    or not profile.registration_evidence
                    or not profile.vat_status_evidence
                    or not profile.vat_status_effective_from
                ):
                    raise ValidationError(
                        "Lengkapi dokumen pendaftaran dan tanggal/status PPN melalui pengaturan profil terlebih dahulu."
                    )
                data = form.cleaned_data
                if profile.taxpayer_reference != data["taxpayer_reference"]:
                    if profile.reviewed_at:
                        raise ValidationError(
                            "Identitas pada profil ditinjau tidak ditimpa; perlukan koreksi dokumen dan versi profil tersendiri."
                        )
                    profile.taxpayer_reference = data["taxpayer_reference"]
                    profile.save()
                decision = service.profile_decision(**data)
                review = service.record_external_review(
                    organization=request.organization,
                    actor=request.user,
                    subject=profile,
                    decision=decision,
                    **review_fields(data),
                )
                service.approve_tax_profile(
                    profile,
                    request.user,
                    **{key: data[key] for key in decision},
                    review_note=data["statement"],
                    external_review=review,
                )
        except ValidationError as exc:
            form.add_error(None, exc.messages)
        else:
            messages.success(
                request,
                "Laporan profesional dan persetujuan bisnis tercatat. Perhitungan tetap mengikuti masa dan bukti tiap kewajiban.",
            )
            return redirect("taxes:index")
    return form_page(
        request,
        form,
        "Catat pemeriksaan profesional atas profil",
        "Pemilik mencatat laporan yang sudah diterima dari profesional kompeten. Referensi kompetensi dicatat, belum diverifikasi aplikasi; omzet kecil tidak otomatis menetapkan pajak final.",
    )


@organization_required
@require_http_methods(["GET", "POST"])
def obligation_new(request):
    service.authorize(request.organization, request.user)
    initial = {"period": request.GET.get("period", "")}
    if request.GET.get("invoice"):
        from finance.models import Invoice

        invoice = get_object_or_404(
            Invoice,
            organization=request.organization,
            pk=request.GET["invoice"],
            status__in=["issued", "delivered"],
        )
        initial.update(
            source_invoice=invoice,
            direction="receivable",
            source_reference=invoice.number,
            base=invoice.total,
        )
    form = forms.ObligationForm(
        request.POST or None, organization=request.organization, initial=initial
    )
    if request.method == "POST" and form.is_valid():
        data = dict(form.cleaned_data)
        data["source_evidence"] = data.pop("evidence")
        try:
            context = pph23_fields(data)
            for key in forms.PPH23_FORM_FIELDS:
                data.pop(key, None)
            data.update(context)
            row = service.prepare_obligation(
                organization=request.organization, actor=request.user, **data
            )
        except ValidationError as exc:
            form.add_error(None, staff_tax_errors(exc))
        else:
            return redirect("taxes:obligation", pk=row.pk)
    return form_page(
        request,
        form,
        "Siapkan kertas kerja masa",
        "Angka dihitung aplikasi berdasarkan klasifikasi terdokumentasi. Untuk PPh 23, pilih identitas yang sudah diperiksa dan tanggal terutang sesuai laporan. Untuk tagihan pemasok, gunakan Tinjau pajak pada tagihan agar pembayaran tertaut.",
        tax_context="obligation",
    )


@organization_required
@require_GET
def obligation_detail(request, pk):
    row = get_object_or_404(
        TaxObligation.objects.select_related("external_review", "source_evidence"),
        organization=request.organization,
        pk=pk,
    )
    identity_blocker, credit_blocker = "", ""
    credit_verified, credit_present = False, False
    if row.tax_type == "pph23":
        from .recipient_identity import (
            assert_pph23_credit_verification,
            assert_pph23_obligation,
        )

        try:
            assert_pph23_obligation(row)
        except ValidationError as exc:
            identity_blocker = " ".join(staff_tax_errors(exc))
        if (
            not identity_blocker
            and row.direction == "receivable"
            and row.status == "approved"
        ):
            credit = row.verifications.filter(kind="credit").first()
            credit_present = credit is not None
            if not credit:
                credit_blocker = "Owner/Direktur perlu mencocokkan bukti potong resmi sebelum Finance mencatat pelunasan atau kredit pajak."
            else:
                try:
                    assert_pph23_credit_verification(credit, row)
                    credit_verified = True
                except ValidationError as exc:
                    credit_blocker = " ".join(exc.messages)
    snapshot = row.identity_snapshot if isinstance(row.identity_snapshot, dict) else {}
    parties = []
    for key, label in (
        ("recipient", "Penerima penghasilan"),
        ("withholder", "Pihak pemotong"),
    ):
        party = snapshot.get(key) if isinstance(snapshot.get(key), dict) else {}
        name, reference = party.get("name"), party.get("reference")
        parties.append(
            {
                "label": label,
                "name": name
                if isinstance(name, str) and name.strip()
                else "Belum terikat",
                "masked": "•••• " + reference[-4:]
                if isinstance(reference, str) and reference
                else "Belum ada nomor",
            }
        )
    return render(
        request,
        "taxes/obligation.html",
        {
            "obligation": row,
            "verifications": row.verifications.select_related("evidence", "verifier"),
            "can_approve": (
                request.membership.role == "owner"
                if row.tax_type == "pph23"
                else request.membership.role in ("owner", "reviewer")
            )
            and not identity_blocker,
            "can_prepare": request.membership.role in ("owner", "finance", "reviewer"),
            "can_money": request.membership.role in ("owner", "finance")
            and not identity_blocker
            and not credit_blocker,
            "can_verify": (
                request.membership.role == "owner"
                if row.tax_type == "pph23"
                else request.membership.role in ("owner", "reviewer")
            )
            and not identity_blocker
            and not (
                row.tax_type == "pph23"
                and row.direction == "receivable"
                and credit_present
            ),
            "credit_verified": credit_verified,
            "customer_settled": hasattr(row, "customer_settlement"),
            "identity_blocker": identity_blocker,
            "credit_blocker": credit_blocker,
            "tax_parties": parties,
            "withdrawal_blocker": service.withdrawal_blocker(row),
            "active_nav": "tax",
        },
    )


@organization_required
@require_http_methods(["GET", "POST"])
def obligation_withdraw(request, pk):
    service.authorize(request.organization, request.user)
    row = get_object_or_404(TaxObligation, organization=request.organization, pk=pk)
    form = forms.WithdrawalForm(
        request.POST or None, initial={"expected_version": row.updated_at.isoformat()}
    )
    if request.method == "POST" and form.is_valid():
        try:
            service.withdraw_obligation(
                organization=request.organization,
                actor=request.user,
                obligation=row,
                reason=form.cleaned_data["reason"],
                expected_version=form.cleaned_data["expected_version"],
            )
        except ValidationError as exc:
            form.add_error(None, exc.messages)
        else:
            messages.success(
                request,
                "Draf ditarik dan riwayatnya tersimpan. Siapkan kertas kerja yang benar bila diperlukan. Paket tahunan yang memuat draf lama perlu disiapkan dan ditinjau kembali.",
            )
            return redirect("taxes:obligation", pk=pk)
    return render(
        request,
        "taxes/withdrawal.html",
        {
            "obligation": row,
            "form": form,
            "withdrawal_blocker": service.withdrawal_blocker(row),
            "active_nav": "tax",
        },
    )


@organization_required
@require_http_methods(["GET", "POST"])
def obligation_review(request, pk):
    service.authorize(request.organization, request.user, approve=True)
    row = get_object_or_404(TaxObligation, organization=request.organization, pk=pk)
    if row.tax_type == "pph23" and request.membership.role != "owner":
        raise PermissionDenied(
            "Owner/Direktur mencatat laporan profesional untuk PPh 23."
        )
    form = forms.ProfessionalReviewForm(
        request.POST or None, organization=request.organization
    )
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                review = service.record_external_review(
                    organization=request.organization,
                    actor=request.user,
                    subject=row,
                    decision={"action": "approve_obligation"},
                    **review_fields(form.cleaned_data),
                )
                service.approve_tax_obligation(
                    row, request.user, external_review=review
                )
        except ValidationError as exc:
            form.add_error(None, staff_tax_errors(exc))
        else:
            return redirect("taxes:obligation", pk=pk)
    return form_page(
        request,
        form,
        "Catat persetujuan kertas kerja",
        "Cocokkan laporan profesional dengan klasifikasi, masa, dasar, tarif, dan nominal yang ditampilkan. Catatan disetujui tidak dapat ditimpa.",
        subject=row,
        back=reverse("taxes:obligation", args=[pk]),
    )


@organization_required
@require_http_methods(["GET", "POST"])
def bill_review(request, pk):
    from finance.models import Bill

    service.authorize(request.organization, request.user, approve=True)
    bill = get_object_or_404(Bill, organization=request.organization, pk=pk)
    form = forms.BillReviewForm(
        request.POST or None,
        organization=request.organization,
        bill=bill,
        initial={
            "base": bill.amount - bill.supplier_vat,
            "period": bill.date.strftime("%Y-%m"),
        },
    )
    if request.membership.role != "owner":
        form.fields["rule_code"].choices = [
            (value, label)
            for value, label in form.fields["rule_code"].choices
            if value == "no_withholding"
        ]
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                data = form.cleaned_data
                decision_args = {
                    key: data[key]
                    for key in ("rule_code", "base", "period", "reason", "object_code")
                }
                decision_args["period"] = parse_period(decision_args["period"])
                decision_args.update(pph23_fields(data, bill=True))
                review = service.record_external_review(
                    organization=request.organization,
                    actor=request.user,
                    subject=bill,
                    decision=service.bill_decision(**decision_args),
                    **review_fields(data),
                )
                row = service.review_bill_tax(
                    organization=request.organization,
                    actor=request.user,
                    bill=bill,
                    external_review=review,
                    **decision_args,
                )
        except ValidationError as exc:
            form.add_error(None, staff_tax_errors(exc))
        else:
            messages.success(
                request,
                "Keputusan pajak tagihan tercatat. Pembayaran pemasok dan penyetoran pajak tetap dicocokkan terpisah.",
            )
            return (
                redirect("taxes:obligation", pk=row.obligation_id)
                if row.obligation_id
                else redirect("taxes:index")
            )
    return form_page(
        request,
        form,
        "Tinjau pajak tagihan " + bill.number,
        "Dasar awal dari bruto dikurangi PPN pemasok membantu pencocokan. Salin klasifikasi, waktu terutang dan dasar dari laporan profesional. PPh 23 membutuhkan identitas pemasok dan OSEE yang berlaku pada tanggal itu.",
        subject=bill,
        tax_context="bill",
    )


@organization_required
@require_http_methods(["GET", "POST"])
def obligation_verify(request, pk):
    service.authorize(request.organization, request.user, approve=True)
    row = get_object_or_404(TaxObligation, organization=request.organization, pk=pk)
    if row.tax_type == "pph23" and request.membership.role != "owner":
        raise PermissionDenied(
            "Owner/Direktur mencocokkan bukti resmi PPh 23 dengan versi yang disetujui."
        )
    form = forms.VerificationForm(
        request.POST or None,
        organization=request.organization,
        obligation=row,
        initial={"period": row.period.strftime("%Y-%m"), "amount": row.amount},
    )
    if request.method == "POST" and form.is_valid():
        try:
            data = dict(form.cleaned_data)
            if row.tax_type != "pph23" or data.get("kind") != "credit":
                for key in forms.PPH23_CERTIFICATE_FIELDS:
                    data.pop(key, None)
            service.verify_obligation_evidence(
                organization=request.organization,
                actor=request.user,
                obligation=row,
                **data,
            )
        except ValidationError as exc:
            form.add_error(None, staff_tax_errors(exc))
        else:
            return redirect("taxes:obligation", pk=pk)
    return form_page(
        request,
        form,
        "Cocokkan bukti resmi",
        "Cocokkan dokumen asli dengan versi kewajiban yang disetujui. Bukti potong pelanggan harus cocok dengan identitas kedua pihak, invoice, objek, dasar, tanggal dan potongannya. Aplikasi tidak memverifikasi ke DJP otomatis.",
        subject=row,
        back=reverse("taxes:obligation", args=[pk]),
        tax_context="credit" if row.tax_type == "pph23" else "other_verification",
    )


@organization_required
@require_http_methods(["GET", "POST"])
def annual_prepare(request):
    service.authorize(request.organization, request.user)
    form = forms.AnnualWorkpaperForm(
        request.POST or None,
        organization=request.organization,
        initial={"year": request.GET.get("year", 2026)},
    )
    if request.method == "POST" and form.is_valid():
        data = dict(form.cleaned_data)
        data["support_evidence"] = data.pop("evidence")
        try:
            row = service.prepare_annual_workpaper(
                organization=request.organization, actor=request.user, **data
            )
        except ValidationError as exc:
            form.add_error(None, exc.messages)
        else:
            return redirect("taxes:annual_workpaper", pk=row.pk)
    return form_page(
        request,
        form,
        "Siapkan rekonsiliasi fiskal tahunan",
        "Laba buku diambil dari jurnal. Koreksi fiskal, kompensasi kerugian dan angsuran berasal dari lampiran profesional yang disimpan; kredit pelanggan hanya dari bukti diverifikasi. Hasil merupakan paket pemeriksaan, belum formulir/XML resmi DJP.",
        back=reverse("taxes:annual"),
    )


@organization_required
@require_GET
def annual_detail(request, pk):
    row = get_object_or_404(
        AnnualTaxWorkpaper, organization=request.organization, pk=pk
    )
    snapshot = service.annual_snapshot_status(request.organization, row)
    return render(
        request,
        "taxes/annual_workpaper.html",
        {
            "workpaper": row,
            **snapshot,
            "can_money": snapshot["snapshot_current"]
            and snapshot["preparation_supported"]
            and request.membership.role in ("owner", "finance"),
            "can_approve": snapshot["snapshot_current"]
            and snapshot["preparation_supported"]
            and request.membership.role in ("owner", "reviewer"),
            "active_nav": "tax",
        },
    )


@organization_required
@require_http_methods(["GET", "POST"])
def annual_review(request, pk):
    service.authorize(request.organization, request.user, approve=True)
    row = get_object_or_404(
        AnnualTaxWorkpaper, organization=request.organization, pk=pk
    )
    form = forms.ProfessionalReviewForm(
        request.POST or None, organization=request.organization
    )
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                review = service.record_external_review(
                    organization=request.organization,
                    actor=request.user,
                    subject=row,
                    decision={"action": "approve_annual"},
                    **review_fields(form.cleaned_data),
                )
                service.approve_annual_workpaper(
                    organization=request.organization,
                    actor=request.user,
                    workpaper=row,
                    external_review=review,
                )
        except ValidationError as exc:
            form.add_error(None, exc.messages)
        else:
            return redirect("taxes:annual_workpaper", pk=pk)
    return form_page(
        request,
        form,
        "Persetujuan paket tahunan",
        "Pemeriksaan harus mencakup rekonsiliasi fiskal, kelayakan aturan, bukti kredit, pembayaran, dan kelengkapan lampiran. Semua masa harus selesai dan ditutup. Persetujuan ini bukan penerimaan DJP.",
    )


@organization_required
@require_http_methods(["GET", "POST"])
def annual_verify(request, pk):
    service.authorize(request.organization, request.user, approve=True)
    row = get_object_or_404(
        AnnualTaxWorkpaper, organization=request.organization, pk=pk
    )
    form = forms.AnnualFilingForm(
        request.POST or None, organization=request.organization
    )
    if request.method == "POST" and form.is_valid():
        try:
            service.verify_annual_filing(
                organization=request.organization,
                actor=request.user,
                workpaper=row,
                **form.cleaned_data,
            )
        except ValidationError as exc:
            form.add_error(None, exc.messages)
        else:
            return redirect("taxes:annual_workpaper", pk=pk)
    return form_page(
        request,
        form,
        "Catat penerimaan SPT Tahunan",
        "Hanya setelah pelaporan di saluran DJP dan BPE resmi diterima. Cocokkan versi SPT, tahun, dan identitas; tidak ada pelaporan otomatis dari tombol ini.",
    )


@organization_required
@require_GET
def annual_export(request, pk):
    from .views import csv_cell

    row = get_object_or_404(
        AnnualTaxWorkpaper, organization=request.organization, pk=pk
    )
    snapshot = service.annual_snapshot_status(request.organization, row)
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        manifest = {
            "status": "DRAFT_REVIEW_PACKAGE_NOT_DJP_XML",
            "official_schema_validated": False,
            **snapshot,
            "workpaper_id": row.pk,
            "year": row.year,
            "calculation_version": row.calculation_version,
            "approval_recorded": hasattr(row, "approval"),
            "filing_evidence_recorded": hasattr(row, "filing"),
            "inputs": row.inputs,
            "result": row.result,
            "book_snapshot": row.book_snapshot,
        }
        archive.writestr(
            "DRAF-manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2)
        )
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["DRAF REKONSILIASI FISKAL — BUKAN SPT / XML DJP"])
        for key, value in row.result.items():
            writer.writerow([csv_cell(key), csv_cell(value)])
        archive.writestr("DRAF-rekonsiliasi.csv", "\ufeff" + output.getvalue())
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(
            ["Bulan", "Pendapatan buku", "Biaya buku", "Hasil buku", "Ditutup"]
        )
        for month in row.book_snapshot["ledger_months"]:
            writer.writerow(
                [
                    csv_cell(month[key])
                    for key in (
                        "period",
                        "revenue",
                        "expenses",
                        "book_profit",
                        "closed",
                    )
                ]
            )
        archive.writestr("DRAF-buku-bulanan.csv", "\ufeff" + output.getvalue())
        warning = (
            "Paket lama: buku atau bukti/keputusan pajak telah berubah. Siapkan versi baru dan tinjau kembali. Angka historis ini bukan keadaan terkini.\n\n"
            if not snapshot["snapshot_current"]
            else ""
        )
        archive.writestr(
            "BACA-DAHULU.txt",
            warning
            + "Paket pemeriksaan internal. Bukan formulir atau XML DJP. Cocokkan lampiran, versi aturan, pembetulan, pembayaran dan penerimaan resmi pada saluran DJP. Persetujuan profesional tidak membuktikan sudah dilaporkan.",
        )
    response = HttpResponse(target.getvalue(), content_type="application/zip")
    response["Content-Disposition"] = (
        f'attachment; filename="DRAF-pajak-tahunan-{row.year}-versi-{row.pk}.zip"'
    )
    response["Cache-Control"] = "no-store"
    record(
        request.organization,
        request.user,
        "tax.annual.exported",
        row,
        {"official_schema_validated": False},
    )
    return response
