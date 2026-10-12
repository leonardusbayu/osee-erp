"""Finance attests statement totals after importing, without posting bank entries."""

from calendar import monthrange
from decimal import Decimal

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from core.access import organization_required, require_role
from finance.statement_models import StatementPeriodReview
from finance.statement_services import (
    preview_statement_period,
    review_statement_period,
    statement_coverage,
)

from .models import StatementImport


class PeriodReviewForm(forms.Form):
    period_start = forms.DateField(
        label="Awal periode pada e-statement",
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    period_end = forms.DateField(
        label="Akhir periode pada e-statement",
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    opening_balance = forms.DecimalField(
        label="Saldo awal menurut bank (Rp)", max_digits=20, decimal_places=2
    )
    closing_balance = forms.DecimalField(
        label="Saldo akhir menurut bank (Rp)", max_digits=20, decimal_places=2
    )
    total_in = forms.DecimalField(
        label="Total kredit / uang masuk menurut bank (Rp)",
        min_value=Decimal(0),
        max_digits=20,
        decimal_places=2,
    )
    total_out = forms.DecimalField(
        label="Total debit / uang keluar menurut bank (Rp)",
        min_value=Decimal(0),
        max_digits=20,
        decimal_places=2,
    )
    transaction_count = forms.IntegerField(
        label="Jumlah transaksi menurut bank", min_value=0, max_value=5000
    )
    source_order = forms.ChoiceField(
        label="Urutan transaksi dalam file",
        choices=[
            ("ascending", "Dari lama ke baru"),
            ("descending", "Dari baru ke lama"),
        ],
    )
    evidence = forms.CharField(
        label="Referensi pemeriksaan sumber dan alasan koreksi bila ada",
        max_length=4000,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    supersedes = forms.ModelChoiceField(
        label="Menggantikan pemeriksaan lama (jika koreksi)",
        required=False,
        queryset=StatementPeriodReview.objects.none(),
        help_text="Pilih hanya untuk rekening dan periode yang sama. Riwayat lama tetap tersimpan.",
    )

    def __init__(self, *args, statement, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["supersedes"].queryset = StatementPeriodReview.objects.filter(
            organization=statement.organization,
            account=statement.account,
            replacement__isnull=True,
        ).order_by("-period_end", "-pk")


@require_http_methods(["GET", "POST"])
@organization_required
def review(request, pk):
    require_role(request, "owner", "finance")
    statement = get_object_or_404(
        StatementImport.objects.select_related("account"),
        organization=request.organization,
        pk=pk,
    )
    today = timezone.localdate()
    initial = {
        "period_start": today.replace(day=1),
        "period_end": min(
            today, today.replace(day=monthrange(today.year, today.month)[1])
        ),
        "source_order": "ascending",
    }
    form = PeriodReviewForm(
        request.POST if request.method == "POST" else None,
        statement=statement,
        initial=initial,
    )
    preview = None
    if request.method == "POST" and form.is_valid():
        values = dict(form.cleaned_data)
        supersedes = values.pop("supersedes")
        try:
            if request.POST.get("action") == "confirm":
                review_statement_period(
                    organization=request.organization,
                    actor=request.user,
                    statement=statement,
                    expected_fingerprint=request.POST.get("expected_fingerprint", ""),
                    confirmed=request.POST.get("confirmed") == "yes",
                    supersedes=supersedes,
                    **values,
                )
                messages.success(
                    request,
                    "Cakupan dan saldo periode disahkan oleh Finance. Perubahan sumber akan meminta pemeriksaan ulang.",
                )
                return redirect("statement_period_review", pk=statement.pk)
            preview = preview_statement_period(
                organization=request.organization,
                actor=request.user,
                statement=statement,
                **values,
            )
        except ValidationError as exc:
            form.add_error(None, exc)
    reviews = list(
        statement.period_reviews.select_related("created_by", "supersedes").order_by(
            "-pk"
        )
    )
    for saved in reviews:
        saved.coverage = statement_coverage(
            organization=request.organization,
            account=statement.account,
            start=saved.period_start,
            end=saved.period_end,
        )
        saved.is_current = (
            not hasattr(saved, "replacement") and saved.coverage["verified"]
        )
    return render(
        request,
        "app/statement_period_review.html",
        {
            "statement": statement,
            "form": form,
            "preview": preview,
            "reviews": reviews,
            "active_nav": "bank",
        },
    )
