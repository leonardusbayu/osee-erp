"""Explicit Finance screens for unit delivery, owner credits and recorded refunds."""

from decimal import Decimal

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path, reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from core.access import organization_required, require_role
from finance.fulfillment import (
    credit_units,
    deliver_units,
    fulfillment_summary,
    refund_credit,
)
from finance.fulfillment_models import InvoiceCredit
from finance.models import BankTransaction, Invoice


class UnitForm(forms.Form):
    action = forms.ChoiceField(
        label="Tindakan", choices=[("deliver", "Konfirmasi unit layanan selesai")]
    )
    quantity = forms.IntegerField(label="Jumlah unit", min_value=1, max_value=100000)
    date = forms.DateField(
        label="Tanggal aktual / koreksi", widget=forms.DateInput(attrs={"type": "date"})
    )
    reference = forms.CharField(label="Referensi unik", max_length=120)
    evidence = forms.CharField(
        label="Bukti dan alasan pemeriksaan", max_length=4000, widget=forms.Textarea
    )
    confirm = forms.BooleanField(
        label="Saya sudah memeriksa rincian, tanggal, dan dampak pencatatannya"
    )


class RefundForm(forms.Form):
    transaction = forms.ModelChoiceField(
        label="Mutasi keluar yang sudah terjadi",
        queryset=BankTransaction.objects.none(),
    )
    amount = forms.DecimalField(
        label="Jumlah refund",
        min_value=Decimal("0.01"),
        max_digits=20,
        decimal_places=2,
    )
    reference = forms.CharField(
        label="Referensi unik pencocokan refund", max_length=120
    )
    evidence = forms.CharField(
        label="Bukti pemeriksaan mutasi", max_length=4000, widget=forms.Textarea
    )


@organization_required
@require_http_methods(["GET", "POST"])
def invoice_units(request, pk):
    require_role(request, "owner", "finance")
    invoice = get_object_or_404(Invoice, organization=request.organization, pk=pk)
    form = UnitForm(request.POST or None, initial={"date": timezone.localdate()})
    if request.membership.role == "owner":
        form.fields["action"].choices += [
            ("unfulfilled", "Koreksi: batalkan unit belum dilaksanakan"),
            ("service", "Koreksi: pengurangan layanan yang sudah dilaksanakan"),
        ]
    if request.method == "POST" and form.is_valid():
        try:
            values = dict(form.cleaned_data)
            action = values.pop("action")
            values.pop("confirm")
            if action == "deliver":
                deliver_units(
                    organization=request.organization,
                    actor=request.user,
                    invoice=invoice,
                    **values,
                )
            else:
                credit_units(
                    organization=request.organization,
                    actor=request.user,
                    invoice=invoice,
                    kind=action,
                    **values,
                )
            messages.success(
                request,
                "Pencatatan tersimpan. Halaman ini tidak mengirim uang; refund dicocokkan ke mutasi keluar tersendiri.",
            )
            return redirect("invoice_units", pk=pk)
        except ValidationError as exc:
            for message in exc.messages:
                form.add_error(None, message)
    return render(
        request,
        "app/invoice_units.html",
        {
            "form": form,
            "invoice": invoice,
            "facts": fulfillment_summary(
                organization=request.organization, invoice=invoice
            ),
            "deliveries": invoice.unit_deliveries.order_by("date", "pk"),
            "credits": invoice.unit_credits.order_by("date", "pk"),
            "active_nav": "sales",
        },
    )


@organization_required
@require_http_methods(["GET", "POST"])
def credit_refund(request, pk):
    require_role(request, "owner", "finance")
    credit = get_object_or_404(InvoiceCredit, organization=request.organization, pk=pk)
    form = RefundForm(request.POST or None)
    form.fields["transaction"].queryset = BankTransaction.objects.filter(
        organization=request.organization, amount__lt=0, date__gte=credit.date
    )
    if request.method == "POST" and form.is_valid():
        try:
            refund_credit(
                organization=request.organization,
                actor=request.user,
                credit=credit,
                **form.cleaned_data,
            )
            return redirect("invoice_units", pk=credit.invoice_id)
        except ValidationError as exc:
            for message in exc.messages:
                form.add_error(None, message)
    return render(
        request,
        "app/form.html",
        {
            "form": form,
            "title": "Cocokkan refund koreksi",
            "description": f"Sisa kewajiban refund {credit.refund_remaining:,.2f}. Pilih mutasi yang sudah terjadi; aplikasi tidak mengirim uang.",
            "cancel_url": reverse("invoice_units", args=[credit.invoice_id]),
            "active_nav": "sales",
        },
    )


urlpatterns = [
    path("sales/<int:pk>/units/", invoice_units, name="invoice_units"),
    path("sales/credits/<int:pk>/refund/", credit_refund, name="credit_refund"),
]
