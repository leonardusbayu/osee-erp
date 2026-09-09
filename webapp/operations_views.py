"""Scoped forms for recorded financial evidence; these never initiate payments."""
from decimal import Decimal
from uuid import uuid4

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path, reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from core.access import organization_required, require_role
from finance import services
from finance.models import (BankAccount, BankPosting, BankTransaction, Bill, CustomerAdvance,
                            Invoice, InvoiceCancellation, Party)
from taxes.models import TaxObligation
from .forms import BusinessDateField


class BankChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, value):
        return f"{value.date:%d/%m/%Y} · {value.account.name} · {value.reference} · Rp {value.amount:,.2f}"


OPERATIONS = {
    "bank_adjustment": {"title": "Catat biaya bank / setoran modal", "source": BankTransaction, "argument": "transaction",
        "fields": ["kind", "amount", "reference", "evidence"], "service": services.reconcile_bank_adjustment,
        "description": "Cocokkan sebagian atau seluruh mutasi dengan biaya bank. Setoran modal memerlukan Owner/Direktur."},
    "bank_transfer": {"title": "Cocokkan transfer antarrekening", "fields": ["outgoing", "incoming", "amount", "reference", "evidence"],
        "service": services.reconcile_bank_transfer, "description": "Pilih bukti keluar dan masuk dari dua rekening perusahaan. Biaya transfer dicatat terpisah sebagai biaya bank."},
    "bank_opening": {"title": "Tetapkan saldo awal bank", "source": BankAccount, "argument": "account", "owner": True,
        "fields": ["date", "amount", "reference", "evidence"], "service": services.record_bank_opening,
        "description": "Owner/Direktur memeriksa saldo pada awal hari dari rekening koran. Saldo ini dibukukan ke Bank dan saldo awal modal; piutang, utang, dan neraca lengkap memerlukan dokumen sumber tersendiri."},
    "prepayment_release": {"title": "Akui biaya layanan yang telah diperoleh", "source": Bill, "argument": "bill",
        "fields": ["date", "amount", "reference", "evidence"], "service": services.release_prepayment,
        "description": "Pindahkan jumlah yang sudah menjadi beban dari biaya dibayar di muka setelah bukti penyelesaian layanan diperiksa."},
    "customer_advance": {"title": "Catat uang muka pelanggan", "fields": ["party", "transaction", "amount", "reference", "evidence"],
        "service": services.record_customer_advance, "description": "Tautkan penerimaan bank kepada pelanggan. Dana tetap menjadi uang muka sampai dialokasikan ke invoice atau dikembalikan."},
    "customer_advance_apply": {"title": "Gunakan uang muka untuk invoice", "source": CustomerAdvance, "argument": "advance",
        "fields": ["invoice", "date", "amount", "reference", "evidence"], "service": services.apply_customer_advance,
        "description": "Pilih invoice milik pelanggan yang sama. Tanggal alokasi menentukan kapan invoice dianggap telah dibayar."},
    "customer_advance_refund": {"title": "Cocokkan pengembalian uang muka", "source": CustomerAdvance, "argument": "advance",
        "fields": ["transaction", "amount", "reference", "evidence"], "service": services.refund_customer_advance,
        "description": "Cocokkan bukti transfer keluar dengan bagian uang muka yang belum digunakan."},
    "sale_cancel": {"title": "Batalkan seluruh pesanan", "source": Invoice, "argument": "invoice", "owner": True,
        "fields": ["date", "reason"], "service": services.cancel_invoice,
        "description": "Tersedia sebelum layanan selesai. Pembatalan menghapus kewajiban layanan; dana yang sudah diterima menjadi utang refund hingga transfer keluar dicocokkan."},
    "sale_refund": {"title": "Cocokkan refund pesanan dibatalkan", "source": Invoice, "argument": "invoice",
        "fields": ["transaction", "amount", "reference", "evidence"], "service": services.refund_cancelled_invoice,
        "description": "Cocokkan bukti transfer keluar dengan sisa utang refund dari pembatalan sebelum layanan selesai."},
    "bill_net_payment": {"title": "Cocokkan pelunasan pemasok setelah potongan pajak", "source": Bill, "argument": "bill",
        "fields": ["transaction"], "service": services.reconcile_withheld_bill_payment,
        "description": "Memerlukan keputusan pajak yang ditinjau. Sistem memisahkan pembayaran bersih kepada pemasok dan utang potongan yang akan disetor. Alur ini untuk satu pelunasan penuh pada masa potongan yang sama."},
    "tax_remittance": {"title": "Cocokkan setoran potongan pajak", "source": TaxObligation, "argument": "obligation",
        "fields": ["transaction", "amount", "reference", "evidence"], "service": services.reconcile_tax_remittance,
        "description": "Tautkan bukti bank keluar ke utang potongan yang telah dibukukan. Bukti pembayaran resmi dan pelaporan tetap diperiksa terpisah di Pajak."},
}


def _operation_form(request, name, source):
    spec, org = OPERATIONS[name], request.organization
    form = forms.Form(request.POST if request.method == "POST" else None)
    pending = services.pending_bank_transactions(organization=org).select_related("account")
    for field_name in spec["fields"]:
        if field_name == "date":
            field = BusinessDateField(label="Tanggal pembukuan / penyelesaian", initial=timezone.localdate,
                widget=forms.DateInput(attrs={"type": "date"}))
        elif field_name == "amount":
            initial = None
            if isinstance(source, BankTransaction):
                initial = abs(source.unallocated_amount)
            elif isinstance(source, CustomerAdvance):
                initial = source.remaining_amount
            elif isinstance(source, Bill) and name == "prepayment_release":
                initial = source.prepaid_remaining
            elif isinstance(source, Invoice) and hasattr(source, "cancellation"):
                initial = source.cancellation.refund_remaining
            field = forms.DecimalField(label="Jumlah (Rp)", max_digits=20, decimal_places=2,
                min_value=None if name == "bank_opening" else Decimal("0.01"), initial=initial,
                help_text="Saldo negatif hanya untuk overdraft yang dibuktikan rekening koran." if name == "bank_opening" else "Jumlah yang dicocokkan, bukan perintah transfer.")
        elif field_name == "reference":
            field = forms.CharField(max_length=120, initial=f"UI-{uuid4().hex}", widget=forms.HiddenInput())
        elif field_name in ("evidence", "reason"):
            field = forms.CharField(label="Alasan pembatalan" if field_name == "reason" else "Bukti dan hasil pemeriksaan",
                max_length=4000, widget=forms.Textarea(attrs={"rows": 4}),
                help_text="Tuliskan referensi dokumen dan alasan pencocokan agar dapat diperiksa kembali.")
        elif field_name == "kind":
            choices = [("bank_fee", "Biaya bank")]
            if request.membership.role == "owner":
                choices.append(("owner_funding", "Setoran modal pemilik"))
            field = forms.ChoiceField(label="Jenis transaksi", choices=choices)
        elif field_name == "party":
            field = forms.ModelChoiceField(label="Pelanggan / mitra", queryset=Party.objects.filter(organization=org, kind__in=["customer", "reseller"]))
        elif field_name == "invoice":
            field = forms.ModelChoiceField(label="Invoice pelanggan ini", queryset=Invoice.objects.filter(organization=org,
                party_id=source.party_id, status__in=["issued", "delivered"]))
        else:
            positive = field_name == "incoming" or (field_name == "transaction" and name == "customer_advance")
            queryset = pending.filter(amount__gt=0) if positive else pending.filter(amount__lt=0)
            # A successful first submit removes a row from pending. Preserve only
            # this scoped posted selection so an exact retry reaches idempotency.
            selected = request.POST.get(field_name, "") if request.method == "POST" else ""
            if selected.isascii() and selected.isdigit() and len(selected) <= 18:
                queryset = BankTransaction.objects.filter(organization=org).filter(
                    Q(pk__in=queryset.values("pk")) | Q(pk=int(selected)))
                queryset = queryset.filter(amount__gt=0) if positive else queryset.filter(amount__lt=0)
                queryset = queryset.select_related("account")
            field = BankChoiceField(label={"incoming": "Mutasi masuk", "outgoing": "Mutasi keluar", "transaction": "Bukti mutasi bank"}[field_name], queryset=queryset)
        form.fields[field_name] = field
    form.fields["confirmed"] = forms.BooleanField(label="Saya telah memeriksa dokumen, pihak, tanggal, dan jumlah transaksi ini.")
    return form


def _source_label(source):
    if isinstance(source, BankTransaction):
        return f"{source.account.name} · {source.reference} · {source.date:%d/%m/%Y} · Rp {source.amount:,.2f}"
    if isinstance(source, CustomerAdvance):
        return f"{source.party.name} · sisa uang muka Rp {source.remaining_amount:,.2f}"
    if isinstance(source, TaxObligation):
        return f"{source.get_tax_type_display()} · {source.period:%m/%Y} · {source.source_reference}"
    return str(source) if source else ""


@require_http_methods(["GET", "POST"])
@organization_required
def financial_operation(request, operation, pk=None):
    spec = OPERATIONS[operation]
    require_role(request, "owner", *(() if spec.get("owner") else ("finance",)))
    source = get_object_or_404(spec["source"], organization=request.organization, pk=pk) if spec.get("source") else None
    form = _operation_form(request, operation, source)
    if request.method == "POST" and form.is_valid():
        values = dict(form.cleaned_data)
        values.pop("confirmed")
        if source is not None:
            values[spec["argument"]] = source
        try:
            spec["service"](organization=request.organization, actor=request.user, **values)
        except ValidationError as exc:
            for message in exc.messages:
                form.add_error(None, message)
        else:
            messages.success(request, "Catatan keuangan tersimpan dan dapat ditelusuri. Tidak ada perintah transfer yang dikirim.")
            return redirect("financial_movements")
    context = {"form": form, "title": spec["title"], "description": spec["description"], "source_label": _source_label(source),
               "cancel_url": reverse("financial_movements"), "active_nav": "bank", "action_label": "Konfirmasi & catat"}
    if operation == "bill_net_payment" and source.tax_amount is not None:
        context["settlement_amounts"] = {"gross": source.amount, "withheld": source.tax_amount, "net": source.amount - source.tax_amount}
    return render(request, "app/financial_operation.html", context)


@require_http_methods(["GET"])
@organization_required
def financial_movements(request):
    org = request.organization
    require_role(request, "owner", "finance", "reviewer", "auditor", "director")
    rows = BankPosting.objects.filter(organization=org).select_related("transaction__account").order_by("-date", "-pk")
    page = Paginator(rows, 50).get_page(request.GET.get("page"))
    advance_page = Paginator(CustomerAdvance.objects.filter(organization=org).select_related("party").order_by("-date", "-pk"), 50).get_page(request.GET.get("advances_page"))
    cancellation_page = Paginator(InvoiceCancellation.objects.filter(organization=org).select_related("invoice__party").order_by("-date", "-pk"), 50).get_page(request.GET.get("cancellations_page"))
    return render(request, "app/financial_movements.html", {"postings": page.object_list, "page_obj": page,
        "advances": advance_page.object_list, "cancellations": cancellation_page.object_list,
        "advance_page": advance_page, "cancellation_page": cancellation_page,
        "can_write": request.membership.role in {"owner", "finance"}, "active_nav": "bank"})


urlpatterns = [path("finance/movements/", financial_movements, name="financial_movements")]
for name, route in {
    "bank_adjustment": "finance/bank/<int:pk>/adjust/", "bank_transfer": "finance/transfers/new/",
    "bank_opening": "finance/bank/accounts/<int:pk>/opening/", "prepayment_release": "finance/bills/<int:pk>/release/",
    "customer_advance": "finance/advances/new/", "customer_advance_apply": "finance/advances/<int:pk>/apply/",
    "customer_advance_refund": "finance/advances/<int:pk>/refund/", "sale_cancel": "finance/sales/<int:pk>/cancel/",
    "sale_refund": "finance/sales/<int:pk>/refund/", "bill_net_payment": "finance/bills/<int:pk>/net-payment/",
    "tax_remittance": "finance/tax/<int:pk>/remittance/",
}.items():
    urlpatterns.append(path(route, financial_operation, {"operation": name}, name=name))
