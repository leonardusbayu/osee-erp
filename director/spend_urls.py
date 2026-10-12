from django.urls import path

from . import spend_views as views

urlpatterns = [
    path(
        "budgets/<int:pk>/entries/<int:entry_pk>/manage/",
        views.spend_adopt,
        name="spend_adopt",
    ),
    path("spends/<int:pk>/", views.spend_detail, name="spend_detail"),
    path(
        "spends/<int:pk>/convert/",
        views.spend_operation,
        {"operation": "convert"},
        name="spend_convert",
    ),
    path(
        "spends/<int:pk>/release/",
        views.spend_operation,
        {"operation": "release"},
        name="spend_release",
    ),
    path(
        "spends/<int:pk>/bills/new/",
        views.spend_operation,
        {"operation": "link_bill"},
        name="spend_link_bill",
    ),
    path(
        "spends/<int:pk>/payments/new/",
        views.spend_operation,
        {"operation": "report_payment"},
        name="spend_report_payment",
    ),
    path(
        "spends/<int:pk>/payments/match/",
        views.spend_operation,
        {"operation": "match_payment"},
        name="spend_match_payment",
    ),
    path(
        "spends/<int:pk>/records/<str:record_kind>/<int:record_pk>/<str:action>/",
        views.spend_correction,
        name="spend_correction",
    ),
    path("bills/<int:pk>/budget/", views.bill_spend_entry, name="bill_spend_entry"),
]
