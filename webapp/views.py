import calendar
import csv
import io
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings as django_settings
from django.contrib import messages
from django.contrib.auth import login, update_session_auth_hash
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.models import User
from django.contrib.auth.views import LoginView
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q, Sum
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.views.decorators.http import require_POST

from core.access import organization_required, require_role
from core.audit import record
from core.models import Organization, Membership, AuditEvent, DomainEvent
from core.modules import MODULES
from core.numbering import next_number
from core.throttle import allow_attempt
from core.workspaces import real_import_exists, pending_company
from finance.models import Party, Product, PriceVersion, Invoice, Bill, BankAccount, BankTransaction, Allocation, JournalLine
from finance import services
from evidence.forms import AttachmentForm
from evidence.services import attach_document
from .documents import invoice_pdf, monthly_report_pdf
from .forms import SetupForm, InvoiceForm, PartyForm, ProductForm, PriceForm, BillForm, BankImportForm, BankAccountForm, OrganizationForm, RegistrationEvidenceForm, PriceReplacementForm, TeamMemberForm

ZERO = Decimal("0.00")

def _local_request(request):
    return request.META.get("REMOTE_ADDR") in {"127.0.0.1", "::1"}

class FinanceLoginView(LoginView):
    template_name = "registration/login.html"
    redirect_authenticated_user = True

    def get_default_redirect_url(self):
        selected = self.request.session.get("organization_id")
        memberships = Membership.objects.filter(user=self.request.user)
        member = memberships.filter(organization_id=selected).first() if selected else memberships.first()
        if member and member.role == "marketing":
            return reverse("marketing:overview")
        if member and member.role in {"owner", "director"}:
            return reverse("director:overview")
        return super().get_default_redirect_url()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["demo_enabled"] = django_settings.DEMO_MODE and _local_request(self.request) and not real_import_exists()
        context["prepared_company"] = bool(_local_request(self.request) and pending_company())
        return context

    def post(self, request, *args, **kwargs):
        identity = f"login:{request.META.get('REMOTE_ADDR', '')}"
        if not allow_attempt(identity):
            return HttpResponse("Terlalu banyak percobaan masuk. Coba lagi dalam 15 menit.", status=429)
        return super().post(request, *args, **kwargs)

@require_POST
def demo_login(request):
    if not django_settings.DEMO_MODE or not _local_request(request) or real_import_exists():
        raise Http404
    member = Membership.objects.select_related("user", "organization").filter(organization__is_demo=True, user__username="osee_demo").first()
    if not member:
        messages.info(request, "Data contoh belum disiapkan. Jalankan penyiapan demo lokal.")
        return redirect("login")
    login(request, member.user, backend="django.contrib.auth.backends.ModelBackend")
    request.session["organization_id"] = member.organization_id
    return redirect("dashboard")

def setup(request):
    if not django_settings.LOCAL_SETUP_ENABLED or not _local_request(request):
        raise Http404
    if Membership.objects.filter(organization__is_demo=False, role="owner").exists():
        return redirect("login")
    prepared = pending_company()
    form = SetupForm(request.POST or None)
    if prepared:
        form.fields["company_name"].initial = prepared.name
        form.fields["company_name"].disabled = True
    if request.method == "POST" and form.is_valid():
        if not allow_attempt(f"setup:{request.META.get('REMOTE_ADDR', '')}", limit=5):
            return HttpResponse("Coba penyiapan lagi nanti.", status=429)
        with transaction.atomic():
            if Membership.objects.filter(organization__is_demo=False, role="owner").exists():
                raise PermissionDenied("Workspace perusahaan sudah disiapkan.")
            user = form.save()
            if prepared:
                org = Organization.objects.select_for_update().get(pk=prepared.pk, is_demo=False)
                if Membership.objects.filter(organization=org).exists():
                    raise PermissionDenied("Workspace perusahaan sudah memiliki pemilik.")
            else:
                org = Organization.objects.create(name=form.cleaned_data["company_name"], brand_name="OSEE")
            Membership.objects.create(organization=org, user=user, role="owner")
            _initial_products(org)
            record(org, user, "organization.owner_activated" if prepared else "organization.created", obj=org)
        login(request, user)
        request.session["organization_id"] = org.pk
        return redirect("dashboard")
    return render(request, "app/form.html", {"form": form, "title": "Aktifkan akun pemilik" if prepared else "Siapkan workspace perusahaan", "description": "Data perusahaan sudah diimpor. Buat nama pengguna dan kata sandi pribadi untuk membukanya. Kata sandi disimpan oleh aplikasi, tidak perlu dikirim melalui chat." if prepared else "Buat akun pemilik untuk data perusahaan Anda. Workspace ini dimulai kosong, terpisah dari data contoh.", "action_label": "Aktifkan dan buka data perusahaan" if prepared else "Buat workspace", "cancel_url": reverse("login")})

def _initial_products(org):
    Product.objects.get_or_create(organization=org, code="ITP", defaults={"name": "TOEFL ITP Official", "kind": "itp", "default_price": ZERO})
    Product.objects.get_or_create(organization=org, code="IBT", defaults={"name": "TOEFL iBT Official", "kind": "ibt", "default_price": ZERO})
    Product.objects.get_or_create(organization=org, code="COURSE", defaults={"name": "English Course", "kind": "course", "default_price": ZERO})

def _period(request):
    today = timezone.localdate()
    value = request.GET.get("period") or request.POST.get("period") or today.strftime("%Y-%m")
    try:
        parsed = date.fromisoformat(value + "-01")
        if not 2000 <= parsed.year <= 2200:
            raise ValueError
    except ValueError:
        raise ValidationError("Pilih periode dengan format YYYY-MM.")
    return parsed

def _error(request, error):
    values = error.messages if isinstance(error, ValidationError) else [str(error)]
    for value in values[:6]:
        messages.error(request, value)

def _form_page(request, form, title, description, cancel_name, active_nav, action_label="Simpan"):
    return render(request, "app/form.html", {"form": form, "title": title, "description": description, "cancel_url": reverse(cancel_name), "active_nav": active_nav, "action_label": action_label})

def _form_error(form, exc):
    # Model/service errors may name fields absent from this particular form.
    form.add_error(None, ValidationError(exc.messages))

@organization_required
def dashboard(request):
    org = request.organization
    if not org.is_demo and org.import_batches.exists() and not JournalLine.objects.filter(organization=org, journal__status="posted").exists():
        from .import_views import overview_context
        context = overview_context(org, 2026)
        context["active_nav"] = "dashboard"
        return render(request, "imports/overview.html", context)
    try:
        period = _period(request)
    except ValidationError as exc:
        _error(request, exc)
        return redirect("dashboard")
    summary = services.monthly_summary(organization=org, year=period.year, month=period.month)
    balances = summary["balances"]
    bank_lines = list(BankTransaction.objects.filter(organization=org, date__lte=summary["end"]))
    unmatched = sum(1 for line in bank_lines if line.unallocated_amount != ZERO)
    series = []
    for offset in range(5, -1, -1):
        month_number = period.year * 12 + period.month - 1 - offset
        yr, mo = divmod(month_number, 12)
        if yr < 2000:
            continue
        monthly = services.monthly_summary(organization=org, year=yr, month=mo + 1)
        series.append({"label": date_format(date(yr, mo + 1, 1), "M"), "revenue": monthly["revenue"], "costs": monthly["expenses"]})
    maximum = max([row[field] for row in series for field in ["revenue", "costs"]] + [Decimal("1")])
    for row in series:
        row["height"] = max(0, int(row["revenue"] / maximum * 100))
        row["cost_height"] = max(0, int(row["costs"] / maximum * 100))
    draft_bills = Bill.objects.filter(organization=org, status="draft").count()
    review_bills = Bill.objects.filter(organization=org).exclude(tax_status="reviewed").count()
    actions = []
    if unmatched:
        actions.append({"title": f"{unmatched} mutasi belum cocok", "detail": "Hubungkan uang masuk dengan invoice yang tepat.", "url": reverse("bank"), "level": "warning"})
    if draft_bills:
        actions.append({"title": f"{draft_bills} tagihan menunggu pemeriksaan", "detail": "Periksa nilai dan tanggal sebelum masuk pembukuan.", "url": reverse("bills"), "level": "warning"})
    from taxes.services import profile_gates
    _, profile_issues = profile_gates(org, period)
    if profile_issues:
        actions.append({"title": "Lengkapi profil pajak perusahaan", "detail": "Status PT dan non-PKP tersimpan; riwayat pajak masih perlu diperiksa.", "url": reverse("tax_workspace"), "level": "info"})
    if review_bills > draft_bills:
        actions.append({"title": f"{review_bills - draft_bills} tagihan perlu pemeriksaan pajak", "detail": "Pembukuan tersimpan; perlakuan pajak masih perlu ditentukan.", "url": reverse("tax_workspace"), "level": "warning"})
    recent = list(Invoice.objects.filter(organization=org).select_related("party", "product").order_by("-date", "-pk")[:5])
    for invoice in recent:
        invoice.issue_date = invoice.date
    data = {"bank_balance": sum((row.amount for row in bank_lines), ZERO), "receivables": balances["AR"], "payables": -balances["AP"], "revenue": summary["revenue"], "costs": summary["expenses"], "profit": summary["profit"], "unmatched_count": unmatched, "order_count": summary["invoice_count"], "partner_count": Party.objects.filter(organization=org, kind="reseller").count(), "monthly_series": series, "recent_invoices": recent, "actions": actions, "period_label": date_format(period, "F Y"), "review_bills": review_bills}
    return render(request, "app/dashboard.html", {"dashboard": data, "active_nav": "dashboard", "period": period.strftime("%Y-%m")})

@organization_required
def sales(request):
    queryset = Invoice.objects.filter(organization=request.organization).select_related("party", "product").order_by("-date", "-pk")
    query = request.GET.get("q", "").strip()[:100]
    if query:
        queryset = queryset.filter(Q(number__icontains=query) | Q(party__name__icontains=query))
    status = request.GET.get("status", "")
    if status in {"draft", "issued", "delivered", "cancelled"}:
        queryset = queryset.filter(status=status)
    page = Paginator(queryset, 50).get_page(request.GET.get("page"))
    invoices = list(page.object_list)
    return render(request, "app/sales.html", {"invoices": invoices, "page_obj": page, "q": query, "selected_status": status, "active_nav": "sales", "can_write": request.membership.role in {"owner", "finance"}})

@organization_required
def sale_create(request):
    require_role(request, "owner", "finance")
    form = InvoiceForm(request.POST or None, organization=request.organization)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                data = dict(form.cleaned_data)
                if data.get("unit_price") is None:
                    data.pop("unit_price", None)
                data["number"] = next_number(request.organization, "INV", data["date"].year)
                invoice = services.create_invoice(organization=request.organization, actor=request.user, **data)
            messages.success(request, "Draf invoice tersimpan. Periksa lalu terbitkan untuk mencatat piutang.")
            return redirect("sale_detail", pk=invoice.pk)
        except ValidationError as exc:
            _form_error(form, exc)
    return _form_page(request, form, "Buat penjualan", "Harga tersimpan per pesanan. Pembayaran dan penyelesaian layanan dicatat terpisah.", "sales", "sales", "Simpan draf invoice")

@organization_required
def sale_detail(request, pk):
    invoice = get_object_or_404(Invoice.objects.select_related("party", "product"), organization=request.organization, pk=pk)
    can_write = request.membership.role in {"owner", "finance"}
    return render(request, "app/sale_detail.html", {"invoice": invoice, "allocations": Allocation.objects.filter(organization=request.organization, invoice=invoice).select_related("transaction"), "can_issue": can_write and invoice.status == "draft", "can_deliver": can_write and invoice.status == "issued", "active_nav": "sales", "attachments": invoice.attachments.filter(organization=request.organization), "attachment_form": AttachmentForm(), "attachment_url": reverse("sale_attachment", args=[pk])})

@organization_required
def sale_pdf(request, pk):
    invoice = get_object_or_404(Invoice.objects.select_related("organization", "party", "product"), organization=request.organization, pk=pk)
    content = invoice_pdf(invoice)
    record(request.organization, request.user, "finance.invoice.exported", obj=invoice, detail={"format": "pdf"})
    response = HttpResponse(content, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="osee-invoice-{invoice.pk}.pdf"'
    response["Cache-Control"] = "private, no-store"
    return response

def _attach_source(request, source, target_name, source_key):
    require_role(request, "owner", "finance")
    form = AttachmentForm(request.POST, request.FILES)
    if form.is_valid():
        try:
            attach_document(organization=request.organization, actor=request.user, upload=form.cleaned_data["file"], **{source_key: source})
            messages.success(request, "Dokumen pendukung tersimpan privat. Berkas yang sama tidak digandakan.")
        except ValidationError as exc:
            _error(request, exc)
    else:
        for errors in form.errors.values():
            for error in errors:
                messages.error(request, error)
    return redirect(target_name, pk=source.pk)

@require_POST
@organization_required
def sale_attachment(request, pk):
    source = get_object_or_404(Invoice, organization=request.organization, pk=pk)
    return _attach_source(request, source, "sale_detail", "invoice")

@require_POST
@organization_required
def sale_issue(request, pk):
    require_role(request, "owner", "finance")
    invoice = get_object_or_404(Invoice, organization=request.organization, pk=pk)
    try:
        services.issue_invoice(organization=request.organization, invoice=invoice, actor=request.user)
        messages.success(request, "Invoice diterbitkan. Piutang dan kewajiban layanan sudah tercatat.")
    except ValidationError as exc:
        _error(request, exc)
    return redirect("sale_detail", pk=pk)

@require_POST
@organization_required
def sale_deliver(request, pk):
    require_role(request, "owner", "finance")
    invoice = get_object_or_404(Invoice, organization=request.organization, pk=pk)
    try:
        services.record_delivery(organization=request.organization, invoice=invoice, date=timezone.localdate(), actor=request.user)
        messages.success(request, "Seluruh layanan dikonfirmasi selesai. Pendapatan tercatat satu kali.")
    except ValidationError as exc:
        _error(request, exc)
    return redirect("sale_detail", pk=pk)

@organization_required
def bills(request):
    queryset = Bill.objects.filter(organization=request.organization).select_related("supplier").order_by("-date", "-pk")
    page = Paginator(queryset, 50).get_page(request.GET.get("page"))
    return render(request, "app/bills.html", {"bills": page.object_list, "page_obj": page, "active_nav": "bills", "can_write": request.membership.role in {"owner", "finance"}})

@organization_required
def bill_detail(request, pk):
    bill = get_object_or_404(Bill.objects.select_related("supplier"), organization=request.organization, pk=pk)
    from imports.models import SourceDocument
    import_sources = SourceDocument.objects.filter(organization=request.organization, batch__supplier_bill=bill, kind="supplier_invoice")
    return render(request, "app/bill_detail.html", {"bill": bill, "active_nav": "bills", "attachments": bill.attachments.filter(organization=request.organization), "import_sources": import_sources, "attachment_form": AttachmentForm(), "attachment_url": reverse("bill_attachment", args=[pk])})

@require_POST
@organization_required
def bill_attachment(request, pk):
    source = get_object_or_404(Bill, organization=request.organization, pk=pk)
    return _attach_source(request, source, "bill_detail", "bill")

@organization_required
def bill_create(request):
    require_role(request, "owner", "finance")
    form = BillForm(request.POST or None, organization=request.organization)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                data = dict(form.cleaned_data)
                data["number"] = next_number(request.organization, "BILL", data["date"].year)
                bill = services.create_bill(organization=request.organization, actor=request.user, **data)
            messages.success(request, "Draf tagihan tersimpan. Pemeriksaan pajak tetap dilakukan terpisah.")
            return redirect("bills")
        except ValidationError as exc:
            _form_error(form, exc)
    return _form_page(request, form, "Catat tagihan supplier", "Masukkan nilai sesuai dokumen supplier. Pajak yang tercantum tidak otomatis menjadi kredit pajak.", "bills", "bills", "Simpan tagihan")

@require_POST
@organization_required
def bill_approve(request, pk):
    require_role(request, "owner", "finance")
    bill = get_object_or_404(Bill, organization=request.organization, pk=pk)
    try:
        services.approve_bill(organization=request.organization, bill=bill, actor=request.user)
        messages.success(request, "Tagihan masuk pembukuan. Ini bukan persetujuan atau pelaksanaan transfer bank.")
    except ValidationError as exc:
        _error(request, exc)
    return redirect("bills")

@organization_required
def bank(request):
    org = request.organization
    all_lines = BankTransaction.objects.filter(organization=org).select_related("account").order_by("-date", "-pk")
    pending = services.pending_bank_transactions(organization=org)
    filtered = all_lines.filter(pk__in=pending.values("pk")) if request.GET.get("status") == "unmatched" else all_lines
    page = Paginator(filtered, 50).get_page(request.GET.get("page"))
    lines = list(page.object_list)
    for line in lines:
        line.status_label = "Sudah cocok" if line.unallocated_amount == ZERO else "Belum cocok"
        line.can_match = line.amount > ZERO and line.unallocated_amount > ZERO and request.membership.role in {"owner", "finance"}
        line.can_match_bill = line.amount < ZERO and line.unallocated_amount != ZERO and request.membership.role in {"owner", "finance"}
    invoices = [i for i in Invoice.objects.filter(organization=org, status__in=["issued", "delivered"]).select_related("party") if i.outstanding_amount > ZERO]
    open_bills = [bill for bill in Bill.objects.filter(organization=org, status="approved", tax_status="reviewed", tax_amount=0).select_related("supplier") if bill.outstanding_amount > ZERO]
    return render(request, "app/bank.html", {"transactions": lines, "page_obj": page, "selected_status": request.GET.get("status", ""), "accounts": BankAccount.objects.filter(organization=org), "unmatched_count": pending.count(), "open_invoices": invoices, "open_bills": open_bills, "active_nav": "bank", "can_write": request.membership.role in {"owner", "finance"}, "bank_balance": BankTransaction.objects.filter(organization=org).aggregate(total=Sum("amount"))["total"] or ZERO})

@organization_required
def bank_import(request):
    require_role(request, "owner", "finance")
    form = BankImportForm(request.POST or None, request.FILES or None, organization=request.organization)
    if request.method == "POST" and form.is_valid():
        try:
            result = services.import_bank_csv(organization=request.organization, account=form.cleaned_data["account"], content=form.cleaned_data["file"].read(), actor=request.user)
            messages.success(request, f"{result['created']} mutasi baru diimpor; {result['duplicates']} baris sudah ada. Cocokkan dengan dokumen transaksi.")
            return redirect("bank")
        except (ValidationError, UnicodeDecodeError) as exc:
            if isinstance(exc, ValidationError):
                _form_error(form, exc)
            else:
                form.add_error(None, "CSV harus memakai encoding UTF-8.")
    return _form_page(request, form, "Impor mutasi bank", "Impor file CSV terstruktur sambil menunggu aktivasi API BNIdirect. Referensi yang sama tidak dicatat dua kali.", "bank", "bank", "Periksa & impor CSV")

@require_POST
@organization_required
def bank_match(request, pk):
    require_role(request, "owner", "finance")
    bank_line = get_object_or_404(BankTransaction, organization=request.organization, pk=pk)
    try:
        amount = Decimal(request.POST.get("amount", ""))
        if not amount.is_finite():
            raise InvalidOperation
        if bank_line.amount > 0:
            invoice = get_object_or_404(Invoice, organization=request.organization, pk=request.POST.get("invoice") or 0)
            services.reconcile_receipt(organization=request.organization, transaction=bank_line, invoice=invoice, amount=amount, actor=request.user)
        else:
            bill = get_object_or_404(Bill, organization=request.organization, pk=request.POST.get("bill") or 0)
            services.reconcile_bill_payment(organization=request.organization, transaction=bank_line, bill=bill, amount=amount, actor=request.user)
        messages.success(request, "Alokasi tersimpan dan pembukuan diperbarui. Mutasi asli tetap tersimpan.")
    except (InvalidOperation, ValueError, ValidationError) as exc:
        _error(request, exc if isinstance(exc, ValidationError) else ValidationError("Masukkan jumlah alokasi yang valid."))
    return redirect("bank")

@organization_required
def bank_template(request):
    response = HttpResponse("date,reference,description,amount\n", content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="template-mutasi-osee.csv"'
    return response

@organization_required
def bank_account_create(request):
    require_role(request, "owner")
    form = BankAccountForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        obj.organization = request.organization
        try:
            obj.save()
            record(request.organization, request.user, "finance.bank_account.created", obj=obj)
            messages.success(request, "Rekening ditambahkan. Koneksi API belum diaktifkan.")
            return redirect("bank")
        except ValidationError as exc:
            _form_error(form, exc)
    return _form_page(request, form, "Tambah rekening", "Rekening digunakan untuk mengelompokkan mutasi. Penambahan ini tidak memberi akses transfer.", "bank", "bank")

@organization_required
def partners(request):
    from imports.models import ImportBatch
    from .templatetags.finance_format import money
    observed = {}
    for batch in ImportBatch.objects.filter(organization=request.organization):
        for channel in batch.payload["channels"]:
            row = observed.setdefault(channel["source_label"], {"name": channel["source_label"], "periods": set(), "counts": [], "prices_seen": set()})
            row["periods"].add(channel["month_id"])
            if channel["printed_count"] is not None:
                row["counts"].append(channel["printed_count"])
            if channel["header_price"] is not None:
                row["prices_seen"].add(channel["header_price"])
    source_channels = [{"name": row["name"], "months": len(row["periods"]), "count": sum(row["counts"]) if row["counts"] else None,
                        "prices": ", ".join(money(price) for price in sorted(row["prices_seen"]))} for row in sorted(observed.values(), key=lambda row: row["name"])]
    return render(request, "app/partners.html", {"parties": Party.objects.filter(organization=request.organization).order_by("name"), "prices": PriceVersion.objects.filter(organization=request.organization).select_related("party", "product").order_by("-effective_from"), "source_channels": source_channels, "active_nav": "partners"})

@organization_required
def partner_create(request):
    require_role(request, "owner", "finance")
    form = PartyForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        obj.organization = request.organization
        try:
            obj.save()
            record(request.organization, request.user, "finance.party.created", obj=obj)
            messages.success(request, "Kontak tersimpan. Harga khusus dapat ditambahkan di daftar harga.")
            return redirect("partners")
        except ValidationError as exc:
            _form_error(form, exc)
    return _form_page(request, form, "Tambah mitra atau kontak", "Mitra adalah reseller independen. Penjualan kembali mitra tidak menjadi omzet OSEE.", "partners", "partners")

@organization_required
def prices(request):
    return render(request, "app/prices.html", {"prices": PriceVersion.objects.filter(organization=request.organization).select_related("party", "product").order_by("-effective_from", "-pk"), "products": Product.objects.filter(organization=request.organization), "active_nav": "partners"})

@organization_required
def price_create(request):
    require_role(request, "owner", "finance")
    form = PriceForm(request.POST or None, organization=request.organization)
    if request.method == "POST" and form.is_valid():
        try:
            obj = form.save()
            record(request.organization, request.user, "finance.price.created", obj=obj)
            messages.success(request, "Versi harga tersimpan. Invoice yang sudah diterbitkan tidak berubah.")
            return redirect("prices")
        except ValidationError as exc:
            _form_error(form, exc)
    return _form_page(request, form, "Tambah versi harga", "Pisahkan harga jual mitra dan biaya supplier. Tetapkan masa berlaku agar perubahan harga dapat ditelusuri.", "prices", "partners")

@organization_required
def price_replace(request, pk):
    require_role(request, "owner")
    old = get_object_or_404(PriceVersion.objects.select_related("party", "product"), organization=request.organization, pk=pk)
    if not 2000 <= old.effective_from.year <= 2200:
        messages.error(request, "Tanggal harga ini di luar rentang yang didukung. Perlu pemeriksaan data.")
        return redirect("prices")
    form = PriceReplacementForm(request.POST or None, initial={"amount": old.amount, "effective_from": max(timezone.localdate(), old.effective_from + timedelta(days=1))})
    if request.method == "POST" and form.is_valid():
        try:
            services.replace_price_version(organization=request.organization, price_version=old, actor=request.user, **form.cleaned_data)
            messages.success(request, "Versi harga baru aktif sesuai tanggal. Harga lama dan pesanan yang telah diterbitkan tetap dapat ditelusuri.")
            return redirect("prices")
        except ValidationError as exc:
            _form_error(form, exc)
    return _form_page(request, form, "Perbarui harga", f"{old.product.name} · {old.party.name if old.party else 'Harga umum'}. Versi lama diakhiri sehari sebelum versi baru berlaku.", "prices", "partners", "Simpan versi pengganti")

@organization_required
def product_create(request):
    require_role(request, "owner", "finance")
    form = ProductForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        obj.organization = request.organization
        try:
            obj.save()
            record(request.organization, request.user, "finance.product.created", obj=obj)
            messages.success(request, "Produk tersimpan.")
            return redirect("prices")
        except ValidationError as exc:
            _form_error(form, exc)
    return _form_page(request, form, "Tambah produk", "Harga TOEFL ITP, iBT, dan kursus dikelola terpisah.", "prices", "partners")

@organization_required
def reports(request):
    try:
        period = _period(request)
    except ValidationError as exc:
        _error(request, exc)
        return redirect("reports")
    summary = services.monthly_summary(organization=request.organization, year=period.year, month=period.month)
    from taxes.services import profile_gates
    _, tax_gates = profile_gates(request.organization, period)
    summary["blockers"] += [gate["title"] for gate in tax_gates]
    if summary["end"] >= timezone.localdate():
        summary["blockers"].append("Penutupan tersedia setelah bulan ini berakhir.")
    labels = dict(JournalLine.Account.choices)
    trial = [{"account": {"code": code, "name": labels[code]}, "debit": max(value, ZERO), "credit": max(-value, ZERO)} for code, value in summary["balances"].items()]
    context = {"period_label": date_format(period, "F Y"), "revenue": summary["revenue"], "costs": summary["expenses"], "profit": summary["profit"], "receivables": summary["balances"]["AR"], "payables": -summary["balances"]["AP"], "trial_balance": trial, "exports": [{"label": "Neraca saldo CSV", "url": reverse("report_export") + f"?period={period:%Y-%m}&kind=trial"}, {"label": "Jurnal transaksi CSV", "url": reverse("report_export") + f"?period={period:%Y-%m}&kind=journal"}], "blockers": summary["blockers"], "closed": summary["closed"], "debit_total": sum((row["debit"] for row in trial), ZERO), "credit_total": sum((row["credit"] for row in trial), ZERO)}
    context["exports"].insert(0, {"label": "Laporan bulanan PDF", "url": reverse("report_pdf") + f"?period={period:%Y-%m}"})
    return render(request, "app/reports.html", {"reports": context, "period": period.strftime("%Y-%m"), "active_nav": "reports", "can_close": request.membership.role in {"owner", "reviewer"}})

@organization_required
def report_pdf(request):
    try:
        period = _period(request)
    except ValidationError:
        return HttpResponse("Periode tidak valid.", status=400)
    org = request.organization
    summary = services.monthly_summary(organization=org, year=period.year, month=period.month)
    from taxes.services import profile_gates
    _, gates = profile_gates(org, period)
    summary["organization_id"] = org.pk
    summary["tax_profile_gates"] = gates
    if summary["end"] >= timezone.localdate():
        summary["blockers"].append("Penutupan tersedia setelah bulan ini berakhir.")
    content = monthly_report_pdf(org, summary)
    record(org, request.user, "finance.report.exported", detail={"format": "pdf", "period": period.strftime("%Y-%m")})
    response = HttpResponse(content, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="osee-laporan-{period:%Y-%m}.pdf"'
    response["Cache-Control"] = "private, no-store"
    return response

def _safe_csv(value):
    value = str(value)
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else value

@organization_required
def report_export(request):
    try:
        period = _period(request)
    except ValidationError:
        return HttpResponse("Periode tidak valid.", status=400)
    kind = request.GET.get("kind", "trial")
    if kind not in {"trial", "journal"}:
        return HttpResponse("Jenis laporan tidak valid.", status=400)
    org = request.organization
    summary = services.monthly_summary(organization=org, year=period.year, month=period.month)
    response = HttpResponse(content_type="text/csv; charset=utf-8-sig")
    response["Content-Disposition"] = f'attachment; filename="osee-{kind}-{period:%Y-%m}.csv"'
    response.write("\ufeff")
    writer = csv.writer(response)
    if kind == "trial":
        writer.writerow(["Akun", "Nama", "Debit", "Kredit", "Status", "Periode"])
        labels = dict(JournalLine.Account.choices)
        for code, value in summary["balances"].items():
            writer.writerow([code, labels[code], f"{max(value, ZERO):.2f}", f"{max(-value, ZERO):.2f}", "Ditutup" if summary["closed"] else "Draf komersial", period.strftime("%Y-%m")])
    else:
        writer.writerow(["Tanggal", "Referensi", "Keterangan", "Akun", "Debit", "Kredit"])
        for line in JournalLine.objects.filter(organization=org, journal__status="posted", journal__date__range=(summary["start"], summary["end"])).select_related("journal").order_by("journal__date", "journal_id", "pk"):
            writer.writerow([line.journal.date, _safe_csv(line.journal.source_key), _safe_csv(line.journal.description), line.account, f"{line.debit:.2f}", f"{line.credit:.2f}"])
    record(org, request.user, "finance.report.exported", detail={"kind": kind, "period": period.strftime("%Y-%m")})
    return response

@require_POST
@organization_required
def close_month(request):
    require_role(request, "owner", "reviewer")
    from taxes.services import profile_gates
    try:
        period = _period(request)
        # Commercial closing is available only after the tax profile/setup gates too.
        _, gates = profile_gates(request.organization, period)
        if gates:
            raise ValidationError("Profil pajak masih membutuhkan pemeriksaan. Buka Pajak sebelum menutup bulan.")
        services.close_month(organization=request.organization, year=period.year, month=period.month, actor=request.user)
        messages.success(request, "Bulan ditutup. Transaksi baru tidak dapat diposting ke periode ini.")
    except ValidationError as exc:
        _error(request, exc)
    return redirect("reports")

@organization_required
def settings_view(request):
    form = OrganizationForm(request.POST or None, instance=request.organization)
    if request.method == "POST":
        require_role(request, "owner")
        if form.is_valid():
            org = form.save()
            record(org, request.user, "organization.updated", obj=org)
            messages.success(request, "Informasi perusahaan disimpan.")
            return redirect("settings")
    from taxes.models import TaxProfile
    profile = TaxProfile.objects.filter(organization=request.organization).first()
    return render(request, "app/settings.html", {"form": form, "profile": profile, "members": Membership.objects.filter(organization=request.organization).select_related("user"), "audit_events": AuditEvent.objects.filter(organization=request.organization).select_related("actor")[:15], "openrouter_configured": bool(django_settings.OPENROUTER_API_KEY and django_settings.OPENROUTER_MODEL and django_settings.OPENROUTER_PROVIDER), "active_nav": "settings", "can_write": request.membership.role == "owner"})

@organization_required
def member_create(request):
    require_role(request, "owner")
    form = TeamMemberForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            user = form.save()
            membership = Membership.objects.create(organization=request.organization, user=user, role=form.cleaned_data["role"])
            record(request.organization, request.user, "organization.member.created", obj=membership, detail={"role": membership.role})
        messages.success(request, "Akun tim dibuat. Bagikan akses melalui saluran internal Anda; sistem tidak mengirim email otomatis.")
        return redirect("settings")
    return _form_page(request, form, "Tambah anggota tim", "Pilih salah satu dari tiga peran tim OSEE. Setiap orang masuk dengan akun sendiri; hanya Owner/Direktur yang dapat membuat akun anggota.", "settings", "settings", "Buat akun")

@organization_required
def password_change(request):
    if request.organization.is_demo:
        messages.info(request, "Akun demo tidak memakai kata sandi. Buat workspace perusahaan untuk akun pribadi.")
        return redirect("settings")
    form = PasswordChangeForm(request.user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        update_session_auth_hash(request, user)
        record(request.organization, request.user, "account.password_changed")
        messages.success(request, "Kata sandi diperbarui.")
        return redirect("director:marketing" if request.membership.role == "marketing" else "settings")
    return _form_page(request, form, "Ganti kata sandi", "Gunakan kata sandi unik setidaknya 12 karakter.", "settings", "settings", "Perbarui kata sandi")

@organization_required
def tax_profile_setup(request):
    require_role(request, "owner", "finance")
    from taxes.models import TaxProfile
    profile, _ = TaxProfile.objects.get_or_create(organization=request.organization)
    initial = {key: getattr(profile, key) for key in RegistrationEvidenceForm.base_fields}
    form = RegistrationEvidenceForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        if profile.reviewed_at:
            form.add_error(None, "Profil sudah diperiksa. Perubahan harus melalui peninjau pajak agar riwayat tidak tertimpa.")
        else:
            for key, value in form.cleaned_data.items():
                setattr(profile, key, value)
            try:
                profile.save()
                record(request.organization, request.user, "tax.profile.evidence_updated", obj=profile)
                messages.success(request, "Informasi pendukung tersimpan. Tarif tetap menunggu pemeriksaan pajak.")
                return redirect("tax_workspace")
            except ValidationError as exc:
                _form_error(form, exc)
    return _form_page(request, form, "Lengkapi informasi pajak", "Masukkan fakta dari dokumen yang Anda miliki. Penetapan aturan pajak dilakukan oleh peninjau yang berwenang.", "tax_workspace", "tax")

@organization_required
def modules(request):
    return render(request, "app/modules.html", {"active_nav": "modules", "events_count": DomainEvent.objects.filter(organization=request.organization).count()})

def health(request):
    return JsonResponse({"status": "ok"})
