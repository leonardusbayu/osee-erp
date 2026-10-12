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
from finance.models import (
    Allocation,
    BankAccount,
    BankPosting,
    BankTransaction,
    Bill,
    CustomerAdvance,
    CustomerTaxSettlement,
    Invoice,
    InvoiceCancellation,
    OwnTaxRecognition,
    Party,
)
from taxes.models import AnnualTaxWorkpaper, TaxEvidence, TaxObligation

from .forms import BusinessDateField, LateCustomerTaxCreditForm


class BankChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, value):
        return f"{value.date:%d/%m/%Y} · {value.account.name} · {value.reference} · Rp {value.amount:,.2f}"


OPERATIONS = {
    "customer_tax_credit": {
        "title": "Catat bukti potong setelah pembayaran dicocokkan",
        "source": TaxObligation,
        "argument": "obligation",
        "fields": ["source_allocation", "date", "reference", "evidence"],
        "service": services.recognize_customer_tax_credit,
        "description": "Pilih pembayaran lama yang sudah tercatat pada invoice ini. Bukti potong yang telah diperiksa melunasi sisa tagihan sebagai kredit pajak. Tidak ada tambahan uang bank. Pilih tanggal jurnal pada periode buku yang masih terbuka.",
    },
    "customer_tax_receipt": {
        "title": "Cocokkan pembayaran pelanggan setelah potongan pajak",
        "source": TaxObligation,
        "argument": "obligation",
        "fields": ["transaction", "amount", "reference", "evidence"],
        "service": services.reconcile_customer_tax_receipt,
        "description": "Setelah bukti potong resmi disetujui, pilih uang masuk bersih. Uang bank dan potongan pajak bersama-sama melunasi invoice yang tertaut; potongan tidak dihitung sebagai uang masuk.",
    },
    "own_tax_recognize": {
        "title": "Catat utang pajak final perusahaan",
        "source": TaxObligation,
        "argument": "obligation",
        "fields": ["date", "reference", "evidence"],
        "service": services.recognize_own_tax,
        "description": "Gunakan nominal kertas kerja final yang telah disetujui pemeriksa. Tanggal pembukuan harus masih terbuka. Setelah tersimpan, cocokkan transfer pajak yang sudah terjadi.",
    },
    "annual_tax_recognize": {
        "title": "Catat utang PPh badan tahunan",
        "source": AnnualTaxWorkpaper,
        "argument": "workpaper",
        "fields": [
            "date",
            "instruction_evidence",
            "professional_instruction",
            "late_recognition_confirmed",
            "reference",
            "evidence",
        ],
        "service": services.recognize_own_tax,
        "description": "Tahun asal paket sudah ditutup. Alur ini khusus pengakuan terlambat berdasarkan instruksi jurnal tertulis profesional pada periode yang masih terbuka; tidak memperbaiki laporan tahun lama. Pembukaan kembali tahun lama, PPh 25, pembetulan, dan lebih bayar belum tersedia.",
    },
    "own_tax_payment": {
        "title": "Cocokkan pembayaran pajak perusahaan",
        "source": OwnTaxRecognition,
        "argument": "recognition",
        "fields": ["transaction", "amount", "reference", "evidence"],
        "service": services.reconcile_own_tax_payment,
        "description": "Pilih transfer pajak yang sudah terjadi. Pembayaran boleh sebagian dan dibatasi sisa utang. Pencocokan bank belum membuktikan penerimaan pajak atau pelaporan resmi.",
    },
    "bank_adjustment": {
        "title": "Catat biaya bank / setoran modal",
        "source": BankTransaction,
        "argument": "transaction",
        "fields": ["kind", "amount", "reference", "evidence"],
        "service": services.reconcile_bank_adjustment,
        "description": "Cocokkan sebagian atau seluruh mutasi dengan biaya bank. Setoran modal memerlukan Owner/Direktur.",
    },
    "bank_transfer": {
        "title": "Cocokkan transfer antarrekening",
        "fields": ["outgoing", "incoming", "amount", "reference", "evidence"],
        "service": services.reconcile_bank_transfer,
        "description": "Pilih bukti keluar dan masuk dari dua rekening perusahaan. Biaya transfer dicatat terpisah sebagai biaya bank.",
    },
    "bank_opening": {
        "title": "Tetapkan saldo awal bank",
        "source": BankAccount,
        "argument": "account",
        "owner": True,
        "fields": ["date", "amount", "reference", "evidence"],
        "service": services.record_bank_opening,
        "description": "Owner/Direktur memeriksa saldo pada awal hari dari rekening koran. Saldo ini dibukukan ke Bank dan saldo awal modal; piutang, utang, dan neraca lengkap memerlukan dokumen sumber tersendiri.",
    },
    "prepayment_release": {
        "title": "Akui biaya layanan yang telah diperoleh",
        "source": Bill,
        "argument": "bill",
        "fields": ["date", "amount", "reference", "evidence"],
        "service": services.release_prepayment,
        "description": "Pindahkan jumlah yang sudah menjadi beban dari biaya dibayar di muka setelah bukti penyelesaian layanan diperiksa.",
    },
    "customer_advance": {
        "title": "Catat uang muka pelanggan",
        "fields": ["party", "transaction", "amount", "reference", "evidence"],
        "service": services.record_customer_advance,
        "description": "Tautkan penerimaan bank kepada pelanggan. Dana tetap menjadi uang muka sampai dialokasikan ke invoice atau dikembalikan.",
    },
    "customer_advance_apply": {
        "title": "Gunakan uang muka untuk invoice",
        "source": CustomerAdvance,
        "argument": "advance",
        "fields": ["invoice", "date", "amount", "reference", "evidence"],
        "service": services.apply_customer_advance,
        "description": "Pilih invoice milik pelanggan yang sama. Tanggal alokasi menentukan kapan invoice dianggap telah dibayar.",
    },
    "customer_advance_refund": {
        "title": "Cocokkan pengembalian uang muka",
        "source": CustomerAdvance,
        "argument": "advance",
        "fields": ["transaction", "amount", "reference", "evidence"],
        "service": services.refund_customer_advance,
        "description": "Cocokkan bukti transfer keluar dengan bagian uang muka yang belum digunakan.",
    },
    "sale_cancel": {
        "title": "Batalkan seluruh pesanan",
        "source": Invoice,
        "argument": "invoice",
        "owner": True,
        "fields": ["date", "reason"],
        "service": services.cancel_invoice,
        "description": "Tersedia sebelum layanan selesai. Pembatalan menghapus kewajiban layanan; dana yang sudah diterima menjadi utang refund hingga transfer keluar dicocokkan.",
    },
    "sale_refund": {
        "title": "Cocokkan refund pesanan dibatalkan",
        "source": Invoice,
        "argument": "invoice",
        "fields": ["transaction", "amount", "reference", "evidence"],
        "service": services.refund_cancelled_invoice,
        "description": "Cocokkan bukti transfer keluar dengan sisa utang refund dari pembatalan sebelum layanan selesai.",
    },
    "bill_net_payment": {
        "title": "Cocokkan pelunasan pemasok setelah potongan pajak",
        "source": Bill,
        "argument": "bill",
        "fields": ["transaction"],
        "service": services.reconcile_withheld_bill_payment,
        "description": "Memerlukan keputusan pajak yang ditinjau. Sistem memisahkan pembayaran bersih kepada pemasok dan utang potongan yang akan disetor. Alur ini untuk satu pelunasan penuh pada masa potongan yang sama.",
    },
    "tax_remittance": {
        "title": "Cocokkan setoran potongan pajak",
        "source": TaxObligation,
        "argument": "obligation",
        "fields": ["transaction", "amount", "reference", "evidence"],
        "service": services.reconcile_tax_remittance,
        "description": "Tautkan bukti bank keluar ke utang potongan yang telah dibukukan. Bukti pembayaran resmi dan pelaporan tetap diperiksa terpisah di Pajak.",
    },
}


def _operation_form(request, name, source):
    spec, org = OPERATIONS[name], request.organization
    if name == "customer_tax_credit":
        return LateCustomerTaxCreditForm(
            request.POST if request.method == "POST" else None,
            organization=org,
            obligation=source,
        )
    form = forms.Form(request.POST if request.method == "POST" else None)
    pending = services.pending_bank_transactions(organization=org).select_related(
        "account"
    )

    def source_allocation_field():
        field = forms.ModelChoiceField(
            label="Pembayaran yang sudah dicocokkan",
            queryset=Allocation.objects.filter(
                organization=org,
                invoice_id=source.source_invoice_id,
                invoice__organization=org,
                transaction__organization=org,
            ).select_related("transaction__account"),
        )
        field.label_from_instance = lambda obj: (
            f"{obj.transaction.date:%d/%m/%Y} · {obj.transaction.account.name} · {obj.transaction.reference} · Rp {obj.amount:,.2f}"
        )
        return field

    def instruction_evidence_field():
        field = forms.ModelChoiceField(
            label="PDF instruksi jurnal dari profesional",
            queryset=TaxEvidence.objects.filter(organization=org).defer("content"),
        )
        field.label_from_instance = lambda obj: (
            f"{obj.original_filename} · dokumen #{obj.pk}"
        )
        return field

    def amount_field():
        initial = None
        if isinstance(source, BankTransaction):
            initial = abs(source.unallocated_amount)
        elif isinstance(source, (CustomerAdvance, OwnTaxRecognition)):
            initial = source.remaining_amount
        elif (
            isinstance(source, TaxObligation)
            and name == "customer_tax_receipt"
            and source.source_invoice_id
        ):
            initial = max(
                Decimal(0),
                source.source_invoice.outstanding_amount
                - (source.amount or Decimal(0)),
            )
        elif isinstance(source, Bill) and name == "prepayment_release":
            initial = source.prepaid_remaining
        elif isinstance(source, Invoice) and hasattr(source, "cancellation"):
            initial = source.cancellation.refund_remaining
        return forms.DecimalField(
            label="Jumlah (Rp)",
            max_digits=20,
            decimal_places=2,
            min_value=None if name == "bank_opening" else Decimal("0.01"),
            initial=initial,
            help_text="Saldo negatif hanya untuk overdraft yang dibuktikan rekening koran."
            if name == "bank_opening"
            else "Jumlah yang dicocokkan, bukan perintah transfer.",
        )

    def kind_field():
        choices = [("bank_fee", "Biaya bank")]
        if request.membership.role == "owner":
            choices.append(("owner_funding", "Setoran modal pemilik"))
        return forms.ChoiceField(label="Jenis transaksi", choices=choices)

    def bank_choice_field(field_name):
        positive = field_name == "incoming" or (
            field_name == "transaction"
            and name in ("customer_advance", "customer_tax_receipt")
        )
        queryset = (
            pending.filter(amount__gt=0) if positive else pending.filter(amount__lt=0)
        )
        # A successful first submit removes a row from pending. Preserve only
        # this scoped posted selection so an exact retry reaches idempotency.
        selected = request.POST.get(field_name, "") if request.method == "POST" else ""
        if selected.isascii() and selected.isdigit() and len(selected) <= 18:
            queryset = BankTransaction.objects.filter(organization=org).filter(
                Q(pk__in=queryset.values("pk")) | Q(pk=int(selected))
            )
            queryset = (
                queryset.filter(amount__gt=0)
                if positive
                else queryset.filter(amount__lt=0)
            )
            queryset = queryset.select_related("account")
        return BankChoiceField(
            label={
                "incoming": "Mutasi masuk",
                "outgoing": "Mutasi keluar",
                "transaction": "Bukti mutasi bank",
            }[field_name],
            queryset=queryset,
        )

    field_builders = {
        "date": lambda: BusinessDateField(
            label="Tanggal jurnal sesuai instruksi profesional"
            if name == "annual_tax_recognize"
            else "Tanggal pencatatan kredit pada periode terbuka"
            if name == "customer_tax_credit"
            else "Tanggal pembukuan / penyelesaian",
            initial=None
            if name in ("annual_tax_recognize", "customer_tax_credit")
            else timezone.localdate,
            widget=forms.DateInput(attrs={"type": "date"}),
        ),
        "source_allocation": source_allocation_field,
        "instruction_evidence": instruction_evidence_field,
        "professional_instruction": lambda: forms.CharField(
            label="Instruksi yang diterima",
            min_length=20,
            max_length=2000,
            widget=forms.Textarea(attrs={"rows": 3}),
            help_text="Catat nama profesional, tanggal jurnal yang diperintahkan, dan perlakuan atas tahun yang sudah ditutup.",
        ),
        "late_recognition_confirmed": lambda: forms.BooleanField(
            label="Profesional menginstruksikan tanggal jurnal ini pada periode terbuka. Laporan tahun lama tetap tertutup; koreksi/reopen belum didukung aplikasi."
        ),
        "amount": amount_field,
        "reference": lambda: forms.CharField(
            max_length=120, initial=f"UI-{uuid4().hex}", widget=forms.HiddenInput()
        ),
        "evidence": lambda: forms.CharField(
            label="Bukti dan hasil pemeriksaan",
            max_length=4000,
            widget=forms.Textarea(attrs={"rows": 4}),
            help_text="Tuliskan referensi dokumen dan alasan pencocokan agar dapat diperiksa kembali.",
        ),
        "reason": lambda: forms.CharField(
            label="Alasan pembatalan",
            max_length=4000,
            widget=forms.Textarea(attrs={"rows": 4}),
            help_text="Tuliskan referensi dokumen dan alasan pencocokan agar dapat diperiksa kembali.",
        ),
        "kind": kind_field,
        "party": lambda: forms.ModelChoiceField(
            label="Pelanggan / mitra",
            queryset=Party.objects.filter(
                organization=org, kind__in=["customer", "reseller"]
            ),
        ),
        "invoice": lambda: forms.ModelChoiceField(
            label="Invoice pelanggan ini",
            queryset=Invoice.objects.filter(
                organization=org,
                party_id=source.party_id,
                status__in=["issued", "delivered"],
            ),
        ),
    }
    for field_name in spec["fields"]:
        builder = field_builders.get(field_name)
        field = builder() if builder is not None else bank_choice_field(field_name)
        form.fields[field_name] = field
    form.fields["confirmed"] = forms.BooleanField(
        label="Saya telah memeriksa dokumen, pihak, tanggal, dan jumlah transaksi ini."
    )
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
    source = (
        get_object_or_404(spec["source"], organization=request.organization, pk=pk)
        if spec.get("source")
        else None
    )
    form = _operation_form(request, operation, source)
    if request.method == "POST" and form.is_valid():
        values = dict(form.cleaned_data)
        values.pop("confirmed")
        if source is not None:
            values[spec["argument"]] = source
        try:
            spec["service"](
                organization=request.organization, actor=request.user, **values
            )
        except ValidationError as exc:
            for message in exc.messages:
                form.add_error(None, message)
        else:
            messages.success(
                request,
                "Catatan keuangan tersimpan dan dapat ditelusuri. Tidak ada perintah transfer yang dikirim.",
            )
            return redirect("financial_movements")
    context = {
        "form": form,
        "title": spec["title"],
        "description": spec["description"],
        "source_label": _source_label(source),
        "cancel_url": reverse("financial_movements"),
        "active_nav": "bank",
        "action_label": "Konfirmasi & catat",
    }
    if operation == "bill_net_payment" and source.tax_amount is not None:
        context["settlement_amounts"] = {
            "gross": source.amount,
            "withheld": source.tax_amount,
            "net": source.amount - source.tax_amount,
        }
    if operation == "customer_tax_credit":
        context["credit_only_amount"] = source.amount
        completed = (
            CustomerTaxSettlement.objects.filter(
                organization=request.organization, obligation=source
            )
            .select_related(
                "source_allocation__transaction__account",
                "source_advance_application__advance__bank_posting__transaction__account",
            )
            .first()
        )
        blocker = ""
        if not completed:
            from finance.tax_settlement import _current_obligation
            from taxes.models import TaxVerification
            from taxes.recipient_identity import assert_pph23_credit_verification

            try:
                _current_obligation(source)
                if (
                    source.direction != "receivable"
                    or source.tax_type != "pph23"
                    or not source.source_invoice_id
                    or not source.amount
                    or source.amount <= 0
                ):
                    raise ValidationError(
                        "Gunakan bukti potong pelanggan yang disetujui dan tertaut ke invoice."
                    )
                proof = TaxVerification.objects.filter(
                    organization=request.organization, obligation=source, kind="credit"
                ).first()
                if not proof:
                    raise ValidationError(
                        "Bukti potong pelanggan belum dicocokkan. Minta Owner memeriksa bukti resmi pada halaman pajak terlebih dahulu."
                    )
                assert_pph23_credit_verification(proof, source)
            except ValidationError as exc:
                blocker = " ".join(exc.messages)
        context.update(
            obligation=source,
            completed=completed,
            proof_blocker=blocker,
            payment_sources=form.source_cards,
            invoice=Invoice.objects.filter(
                organization=request.organization, pk=source.source_invoice_id
            )
            .select_related("party")
            .first(),
        )
        return render(request, "app/late_customer_credit_form.html", context)
    return render(request, "app/financial_operation.html", context)


@require_http_methods(["GET"])
@organization_required
def financial_movements(request):
    org = request.organization
    require_role(request, "owner", "finance", "reviewer", "auditor", "director")
    rows = (
        BankPosting.objects.filter(organization=org)
        .select_related("transaction__account")
        .order_by("-date", "-pk")
    )
    page = Paginator(rows, 50).get_page(request.GET.get("page"))
    advance_page = Paginator(
        CustomerAdvance.objects.filter(organization=org)
        .select_related("party")
        .order_by("-date", "-pk"),
        50,
    ).get_page(request.GET.get("advances_page"))
    cancellation_page = Paginator(
        InvoiceCancellation.objects.filter(organization=org)
        .select_related("invoice__party")
        .order_by("-date", "-pk"),
        50,
    ).get_page(request.GET.get("cancellations_page"))
    tax_page = Paginator(
        OwnTaxRecognition.objects.filter(organization=org)
        .select_related("obligation", "workpaper")
        .order_by("-date", "-pk"),
        50,
    ).get_page(request.GET.get("tax_page"))
    late_credit_page = Paginator(
        CustomerTaxSettlement.objects.filter(organization=org)
        .filter(
            Q(source_allocation__isnull=False)
            | Q(source_advance_application__isnull=False)
        )
        .select_related(
            "invoice__party",
            "source_allocation__transaction__account",
            "source_advance_application__advance__bank_posting__transaction__account",
        )
        .order_by("-date", "-pk"),
        50,
    ).get_page(request.GET.get("credits_page"))
    return render(
        request,
        "app/financial_movements.html",
        {
            "postings": page.object_list,
            "page_obj": page,
            "advances": advance_page.object_list,
            "cancellations": cancellation_page.object_list,
            "advance_page": advance_page,
            "cancellation_page": cancellation_page,
            "own_taxes": tax_page.object_list,
            "tax_page": tax_page,
            "late_credits": late_credit_page.object_list,
            "late_credit_page": late_credit_page,
            "can_write": request.membership.role in {"owner", "finance"},
            "active_nav": "bank",
        },
    )


urlpatterns = [
    path("finance/movements/", financial_movements, name="financial_movements")
]
for name, route in {
    "bank_adjustment": "finance/bank/<int:pk>/adjust/",
    "bank_transfer": "finance/transfers/new/",
    "bank_opening": "finance/bank/accounts/<int:pk>/opening/",
    "prepayment_release": "finance/bills/<int:pk>/release/",
    "customer_advance": "finance/advances/new/",
    "customer_advance_apply": "finance/advances/<int:pk>/apply/",
    "customer_advance_refund": "finance/advances/<int:pk>/refund/",
    "sale_cancel": "finance/sales/<int:pk>/cancel/",
    "sale_refund": "finance/sales/<int:pk>/refund/",
    "bill_net_payment": "finance/bills/<int:pk>/net-payment/",
    "tax_remittance": "finance/tax/<int:pk>/remittance/",
    "customer_tax_receipt": "finance/tax/<int:pk>/customer-receipt/",
    "customer_tax_credit": "finance/tax/<int:pk>/credit-after-payment/",
    "own_tax_recognize": "finance/tax/<int:pk>/recognize/",
    "annual_tax_recognize": "finance/tax/annual/<int:pk>/recognize/",
    "own_tax_payment": "finance/tax/recognized/<int:pk>/pay/",
}.items():
    urlpatterns.append(path(route, financial_operation, {"operation": name}, name=name))
