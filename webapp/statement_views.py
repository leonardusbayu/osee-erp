"""Three-step private statement intake: upload, preview, explicit confirmation."""
import hashlib
from decimal import Decimal
from pathlib import Path

from django import forms
from django.contrib import messages
from django.core import signing
from django.core.exceptions import ValidationError, PermissionDenied
from django.core.files.base import ContentFile
from django.db import transaction
from django.http import FileResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_GET

from core.access import organization_required, require_role
from core.audit import record
from core.models import Membership, Organization
from finance.models import BankAccount
from finance.services import import_bank_csv
from .models import StatementImport
from .statement_import import MAX_BYTES, ALIASES, read_statement, suggest_mapping, normalize_statement, canonical_csv
from .views import _form_page, _form_error


class UploadForm(forms.Form):
    account = forms.ModelChoiceField(label="Rekening tujuan", queryset=BankAccount.objects.none())
    file = forms.FileField(label="E-statement Excel / CSV", help_text="Unduhan asli .xlsx, .xls, atau .csv, maksimal 5 MB dan 5.000 transaksi. Tahap ini belum mencatat mutasi.")

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["account"].queryset = BankAccount.objects.filter(organization=organization)

    def clean_file(self):
        upload = self.cleaned_data["file"]
        if upload.size > MAX_BYTES or Path(upload.name).suffix.lower() not in {".csv", ".xlsx", ".xls"}:
            raise ValidationError("Pilih Excel / CSV berukuran maksimal 5 MB.")
        return upload


class MappingForm(forms.Form):
    number_format = forms.ChoiceField(label="Format angka dalam file", choices=[("id", "Indonesia: 1.234.567,89"), ("en", "Internasional / template OSEE: 1,234,567.89")])
    reference_mode = forms.ChoiceField(label="Identitas transaksi", choices=[("bank_reference", "Nomor referensi unik dari bank"), ("balance_fingerprint", "File tanpa nomor unik: tanggal, keterangan, nominal dan saldo")], help_text="Pilihan kedua memerlukan saldo berjalan. Jika bank memberi nomor unik, gunakan nomor itu secara konsisten pada setiap impor.")
    first_row = forms.IntegerField(label="Baris data pertama", min_value=1, initial=1)
    last_row = forms.IntegerField(label="Baris data terakhir", min_value=1)

    def __init__(self, *args, statement, **kwargs):
        super().__init__(*args, **kwargs)
        choices = [("", "Tidak digunakan")] + [(str(i), f"{i + 1}. {name}") for i, name in enumerate(statement.headers)]
        labels = {"date": "Tanggal transaksi", "reference": "Referensi unik", "description": "Keterangan", "amount": "Nominal bertanda (+ masuk, − keluar)", "debit": "Debit / uang keluar", "credit": "Kredit / uang masuk", "balance": "Saldo berjalan", "currency": "Mata uang"}
        for key in ALIASES:
            self.fields[key] = forms.ChoiceField(label=labels[key], choices=choices, required=key == "date")
        self.fields["first_row"].max_value = self.fields["last_row"].max_value = len(statement.rows)

    def clean(self):
        data = super().clean()
        columns = [data.get(key) for key in ALIASES if data.get(key)]
        if len(columns) != len(set(columns)):
            raise ValidationError("Setiap kolom hanya boleh digunakan untuk satu informasi.")
        if data.get("amount") and (data.get("debit") or data.get("credit")):
            raise ValidationError("Pilih kolom nominal bertanda ATAU kolom debit/kredit.")
        if not (data.get("amount") or data.get("debit") or data.get("credit")):
            raise ValidationError("Pilih kolom nominal, atau debit dan/atau kredit.")
        if data.get("reference_mode") == "bank_reference" and not data.get("reference"):
            raise ValidationError("Pilih kolom referensi unik bank.")
        if data.get("reference_mode") == "balance_fingerprint" and (not data.get("balance") or not data.get("description")):
            raise ValidationError("Identitas dari saldo memerlukan kolom keterangan dan saldo berjalan.")
        return data


def _authorize(request):
    require_role(request, "owner", "finance")
    # Recheck persisted authorization after obtaining the organization lock.
    if not Membership.objects.filter(organization=request.organization, user_id=request.user.pk, user__is_active=True, role__in=["owner", "finance"]).exists():
        raise PermissionDenied


@require_http_methods(["GET", "POST"])
@organization_required
def upload(request):
    _authorize(request)
    form = UploadForm(request.POST or None, request.FILES or None, organization=request.organization)
    if request.method == "POST" and form.is_valid():
        stored_name = None
        try:
            source = form.cleaned_data["file"]
            content = source.read(MAX_BYTES + 1)
            headers, rows = read_statement(content, source.name)
            digest = hashlib.sha256(content).hexdigest()
            with transaction.atomic():
                Organization.objects.select_for_update().get(pk=request.organization.pk)
                _authorize(request)
                account = get_object_or_404(BankAccount, pk=form.cleaned_data["account"].pk, organization=request.organization)
                statement = StatementImport.objects.filter(organization=request.organization, account=account, sha256=digest).first()
                if statement is None:
                    filename = Path(source.name).name
                    suffix = Path(filename).suffix.lower()
                    filename = filename[:240 - len(suffix)] + suffix if len(filename) > 240 else filename
                    statement = StatementImport(organization=request.organization, account=account, created_by=request.user,
                                                original_name=filename, sha256=digest, headers=headers, rows=rows)
                    statement.file.save(source.name, ContentFile(content), save=False)
                    stored_name = statement.file.name
                    statement.save()
                    record(request.organization, request.user, "finance.statement.uploaded", obj=statement, detail={"sha256": digest, "rows": len(rows), "account_id": account.pk})
            return redirect("statement_preview", pk=statement.pk)
        except (ValidationError, UnicodeError, ValueError) as exc:
            if stored_name:
                StatementImport._meta.get_field("file").storage.delete(stored_name)
            _form_error(form, exc if isinstance(exc, ValidationError) else ValidationError("File tidak dapat dibaca. Periksa format unduhan bank."))
    return _form_page(request, form, "1. Unggah e-statement BNI", "Pilih rekening dan file. Selanjutnya cocokkan kolom, periksa pratinjau, lalu konfirmasi impor.", "bank", "bank", "Lanjut ke pemetaan kolom")


@require_http_methods(["GET", "POST"])
@organization_required
def preview(request, pk):
    _authorize(request)
    statement = get_object_or_404(StatementImport.objects.select_related("account"), pk=pk, organization=request.organization)
    initial = {**suggest_mapping(statement.headers), "number_format": "id", "reference_mode": "bank_reference", "first_row": 1, "last_row": len(statement.rows)}
    # The supplied OSEE template has a documented dot decimal convention.
    if statement.headers == ["date", "reference", "description", "amount"]:
        initial["number_format"] = "en"
    form = MappingForm(request.POST if request.method == "POST" and request.POST.get("action") != "confirm" else None, statement=statement, initial=initial)
    normalized, errors, token = [], [], ""
    if request.method == "POST" and not statement.confirmed_at:
        if request.POST.get("action") == "confirm":
            try:
                payload = signing.loads(request.POST.get("preview_token", ""), salt="bank-statement-preview", max_age=1800)
                if payload["statement"] != statement.pk or payload["sha256"] != statement.sha256 or payload["user"] != request.user.pk or request.POST.get("confirmed") != "yes":
                    raise ValidationError("Periksa pratinjau dan centang persetujuan sebelum mengimpor.")
                with transaction.atomic():
                    Organization.objects.select_for_update().get(pk=request.organization.pk)
                    _authorize(request)
                    locked = StatementImport.objects.select_for_update().get(pk=statement.pk, organization=request.organization)
                    if not locked.confirmed_at:
                        with locked.file.open("rb") as source:
                            content = source.read(MAX_BYTES + 1)
                        if hashlib.sha256(content).hexdigest() != locked.sha256:
                            raise ValidationError("File sumber berubah. Impor dihentikan; unggah ulang sumber yang benar.")
                        headers, rows = read_statement(content, locked.original_name)
                        normalized, errors = normalize_statement(rows, payload["mapping"])
                        if errors or not normalized:
                            raise ValidationError("Pratinjau memuat kesalahan. Periksa kembali pemetaan.")
                        result = import_bank_csv(organization=request.organization, account=locked.account, content=canonical_csv(normalized), actor=request.user)
                        locked.mapping = payload["mapping"]
                        locked.result = result
                        locked.confirmed_at, locked.confirmed_by = timezone.now(), request.user
                        locked.save(update_fields=["mapping", "result", "confirmed_at", "confirmed_by"])
                        record(request.organization, request.user, "finance.statement.confirmed", obj=locked, detail={**result, "sha256": locked.sha256, "mapping": locked.mapping})
                    messages.success(request, f"{locked.result['created']} mutasi baru; {locked.result['duplicates']} sudah ada. Lanjutkan pencocokan dokumen.")
                return redirect("statement_preview", pk=pk)
            except (signing.BadSignature, KeyError):
                messages.error(request, "Pratinjau kedaluwarsa atau tidak valid. Tampilkan pratinjau lagi.")
            except (ValidationError, OSError) as exc:
                messages.error(request, " ".join(exc.messages) if isinstance(exc, ValidationError) else "File sumber belum dapat diakses. Impor belum disimpan.")
        elif form.is_valid():
            try:
                normalized, errors = normalize_statement(statement.rows, form.cleaned_data)
                if not errors:
                    token = signing.dumps({"statement": statement.pk, "sha256": statement.sha256, "user": request.user.pk, "mapping": form.cleaned_data}, salt="bank-statement-preview", compress=True)
            except ValidationError as exc:
                _form_error(form, exc)
    total_in = sum((Decimal(row["amount"]) for row in normalized if Decimal(row["amount"]) > 0), Decimal(0))
    total_out = -sum((Decimal(row["amount"]) for row in normalized if Decimal(row["amount"]) < 0), Decimal(0))
    return render(request, "app/statement_preview.html", {"statement": statement, "form": form, "sample": [[cell.get("number", "") if isinstance(cell, dict) else cell for cell in row] for row in statement.rows[:8]],
                  "normalized": normalized[:100], "row_count": len(normalized), "errors": errors[:100], "error_count": len(errors), "token": token, "total_in": total_in, "total_out": total_out, "net": total_in - total_out, "active_nav": "bank"})


@require_GET
@organization_required
def source(request, pk):
    require_role(request, "owner", "finance", "auditor")
    statement = get_object_or_404(StatementImport, pk=pk, organization=request.organization)
    response = FileResponse(statement.file.open("rb"), as_attachment=True, filename=statement.original_name, content_type="application/octet-stream")
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


urlpatterns = [
    path("bank/import/", upload, name="bank_import"),
    path("bank/statements/<int:pk>/", preview, name="statement_preview"),
    path("bank/statements/<int:pk>/source/", source, name="statement_source"),
]
