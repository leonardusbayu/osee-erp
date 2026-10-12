"""Finance assigns tested-unit costs; Directors approve exceptional quotes."""

from decimal import Decimal
from uuid import uuid4

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods

from core.access import organization_required, require_role
from finance import costing
from finance.costing_models import CostAllocation, PriceOverrideRequest
from finance.models import Bill, Invoice, Party, Product

from .forms import BusinessDateField
from .views import _form_page


class ReviewNotesForm(forms.Form):
    reason = forms.CharField(
        label="Alasan", max_length=4000, widget=forms.Textarea(attrs={"rows": 2})
    )
    evidence = forms.CharField(
        label="Referensi bukti",
        max_length=4000,
        widget=forms.Textarea(attrs={"rows": 2}),
    )


class CostForm(ReviewNotesForm):
    bill = forms.ModelChoiceField(
        label="Tagihan penyedia yang sudah disahkan", queryset=Bill.objects.none()
    )
    quantity = forms.IntegerField(
        label="Jumlah unit tes yang diberi biaya", min_value=1, max_value=100000
    )
    amount = forms.DecimalField(
        label="Biaya bruto untuk unit ini (Rp)",
        max_digits=20,
        decimal_places=2,
        min_value=Decimal("0.01"),
        help_text="Masukkan bagian biaya dari tagihan penyedia, termasuk pajak pemasok. Biaya tidak dijurnal ulang.",
    )
    date = BusinessDateField(
        label="Tanggal alokasi biaya",
        initial=timezone.localdate,
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    reference = forms.UUIDField(initial=uuid4, widget=forms.HiddenInput())

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["bill"].queryset = Bill.objects.filter(
            organization=organization, category="provider", status="approved"
        ).select_related("supplier")
        self.fields["bill"].label_from_instance = lambda bill: (
            f"{bill.supplier.name} · {bill.supplier_reference or bill.number} · Rp{bill.amount}"
        )

    def clean_reference(self):
        return f"cost:{self.cleaned_data['reference']}"


class ReverseCostForm(ReviewNotesForm):
    date = BusinessDateField(
        label="Tanggal pembalikan",
        initial=timezone.localdate,
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    reference = forms.UUIDField(initial=uuid4, widget=forms.HiddenInput())

    def clean_reference(self):
        return f"cost-reversal:{self.cleaned_data['reference']}"


class PriceRequestForm(ReviewNotesForm):
    party = forms.ModelChoiceField(
        label="Pelanggan / mitra", queryset=Party.objects.none()
    )
    product = forms.ModelChoiceField(label="Produk", queryset=Product.objects.none())
    quantity = forms.IntegerField(
        label="Jumlah peserta / unit", min_value=1, max_value=100000
    )
    date = BusinessDateField(
        label="Tanggal invoice yang akan dibuat",
        initial=timezone.localdate,
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    unit_price = forms.DecimalField(
        label="Harga khusus per unit yang diminta (Rp)",
        max_digits=20,
        decimal_places=2,
        min_value=Decimal("0.01"),
    )

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["party"].queryset = Party.objects.filter(
            organization=organization, kind__in=["customer", "reseller"]
        )
        self.fields["product"].queryset = Product.objects.filter(
            organization=organization
        )


class PriceDecisionForm(ReviewNotesForm):
    expected_fingerprint = forms.CharField(widget=forms.HiddenInput(), max_length=64)
    decision = forms.ChoiceField(
        label="Keputusan Direktur",
        choices=[("approved", "Setujui harga"), ("rejected", "Tolak harga")],
    )


class BindPriceForm(forms.Form):
    invoice = forms.ModelChoiceField(
        label="Draf invoice yang cocok dengan persetujuan ini",
        queryset=Invoice.objects.none(),
    )

    def __init__(self, *args, price_request, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["invoice"].queryset = Invoice.objects.filter(
            organization=price_request.organization,
            party=price_request.party,
            product=price_request.product,
            quantity=price_request.quantity,
            date=price_request.date,
            unit_price=price_request.unit_price,
            status="draft",
            price_override_use__isnull=True,
        )


@require_http_methods(["GET", "POST"])
@organization_required
def invoice_costs(request, pk):
    require_role(request, "owner", "finance", "auditor")
    invoice = get_object_or_404(
        Invoice.objects.select_related("party", "product"),
        organization=request.organization,
        pk=pk,
    )
    form = CostForm(
        request.POST if request.method == "POST" else None,
        organization=request.organization,
    )
    if request.method == "POST":
        require_role(request, "owner", "finance")
        if form.is_valid():
            try:
                costing.allocate_cost(
                    organization=request.organization,
                    actor=request.user,
                    invoice=invoice,
                    **form.cleaned_data,
                )
                messages.success(
                    request,
                    "Biaya ditautkan ke unit yang sudah dilaksanakan. Tagihan penyedia tidak dijurnal ulang.",
                )
                return redirect("invoice_costs", pk=invoice.pk)
            except ValidationError as exc:
                form.add_error(None, exc)
    return render(
        request,
        "app/invoice_costs.html",
        {
            "invoice": invoice,
            "form": form,
            "facts": costing.cost_facts(
                organization=request.organization, invoice=invoice
            ),
            "allocations": invoice.cost_allocations.select_related(
                "bill", "created_by", "reversal"
            ),
            "can_write": request.membership.role in {"owner", "finance"},
            "active_nav": "sales",
        },
    )


@require_http_methods(["GET", "POST"])
@organization_required
def reverse_cost(request, pk):
    require_role(request, "owner", "finance")
    allocation = get_object_or_404(
        CostAllocation.objects.select_related("invoice"),
        organization=request.organization,
        pk=pk,
    )
    form = ReverseCostForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        try:
            costing.reverse_cost(
                organization=request.organization,
                actor=request.user,
                allocation=allocation,
                **form.cleaned_data,
            )
            messages.success(
                request,
                "Alokasi dibalik dengan riwayat utuh. Petakan kembali biaya yang benar agar margin bisa dinilai ulang.",
            )
            return redirect("invoice_costs", pk=allocation.invoice_id)
        except ValidationError as exc:
            form.add_error(None, exc)
    return _form_page(
        request,
        form,
        "Koreksi alokasi biaya",
        f"{allocation.invoice.number}: pembalikan alokasi {allocation.quantity} unit. Nilai tagihan penyedia tetap utuh.",
        "sales",
        "sales",
        "Simpan pembalikan",
    )


@require_GET
@organization_required
def price_requests(request):
    require_role(request, "owner", "finance", "auditor")
    rows = (
        PriceOverrideRequest.objects.filter(organization=request.organization)
        .select_related("party", "product", "decision_record", "use")
        .order_by("-pk")
    )
    page = Paginator(rows, 50).get_page(request.GET.get("page"))
    return render(
        request,
        "app/price_requests.html",
        {
            "rows": page.object_list,
            "page_obj": page,
            "can_write": request.membership.role in {"owner", "finance"},
            "active_nav": "sales",
        },
    )


@require_http_methods(["GET", "POST"])
@organization_required
def price_request_create(request):
    require_role(request, "owner", "finance")
    form = PriceRequestForm(
        request.POST if request.method == "POST" else None,
        organization=request.organization,
    )
    if request.method == "POST" and form.is_valid():
        try:
            row = costing.request_price_override(
                organization=request.organization,
                actor=request.user,
                **form.cleaned_data,
            )
            messages.success(
                request,
                "Permintaan tersimpan untuk keputusan Direktur. Harga khusus belum boleh dipakai sebelum disetujui.",
            )
            return redirect("price_request_detail", pk=row.pk)
        except ValidationError as exc:
            form.add_error(None, exc)
    return _form_page(
        request,
        form,
        "Minta persetujuan harga khusus",
        "Persetujuan hanya berlaku untuk satu invoice dengan pelanggan, produk, jumlah, tanggal, dan harga yang sama.",
        "sales",
        "price_requests",
        "Kirim permintaan ke Direktur",
    )


@require_http_methods(["GET", "POST"])
@organization_required
def price_request_detail(request, pk):
    require_role(request, "owner", "finance", "auditor")
    row = get_object_or_404(
        PriceOverrideRequest.objects.select_related(
            "party", "product", "decision_record", "use"
        ),
        organization=request.organization,
        pk=pk,
    )
    action = request.POST.get("action") if request.method == "POST" else None
    decision_form = PriceDecisionForm(
        request.POST if action == "decision" else None,
        initial={"expected_fingerprint": row.fingerprint},
    )
    bind_form = BindPriceForm(
        request.POST if action == "bind" else None, price_request=row
    )
    if request.method == "POST":
        require_role(request, "owner", "finance")
        if action == "decision":
            require_role(request, "owner")
            if decision_form.is_valid():
                try:
                    costing.decide_price_override(
                        organization=request.organization,
                        actor=request.user,
                        request=row,
                        **decision_form.cleaned_data,
                    )
                    messages.success(
                        request, "Keputusan Direktur tersimpan bersama dasar harganya."
                    )
                    return redirect("price_request_detail", pk=row.pk)
                except ValidationError as exc:
                    decision_form.add_error(None, exc)
        elif action == "bind" and bind_form.is_valid():
            try:
                costing.bind_price_override(
                    organization=request.organization,
                    actor=request.user,
                    request=row,
                    invoice=bind_form.cleaned_data["invoice"],
                )
                return redirect("sale_detail", pk=bind_form.cleaned_data["invoice"].pk)
            except ValidationError as exc:
                bind_form.add_error(None, exc)
    return render(
        request,
        "app/price_request_detail.html",
        {
            "item": row,
            "decision_form": decision_form,
            "bind_form": bind_form,
            "can_decide": request.membership.role == "owner"
            and not hasattr(row, "decision_record"),
            "can_bind": request.membership.role in {"owner", "finance"}
            and hasattr(row, "decision_record")
            and row.decision_record.decision == "approved"
            and not hasattr(row, "use"),
            "active_nav": "sales",
        },
    )


urlpatterns = [
    path("sales/<int:pk>/costs/", invoice_costs, name="invoice_costs"),
    path("sales/costs/<int:pk>/reverse/", reverse_cost, name="cost_reverse"),
    path("sales/price-requests/", price_requests, name="price_requests"),
    path(
        "sales/price-requests/new/", price_request_create, name="price_request_create"
    ),
    path(
        "sales/price-requests/<int:pk>/",
        price_request_detail,
        name="price_request_detail",
    ),
]
