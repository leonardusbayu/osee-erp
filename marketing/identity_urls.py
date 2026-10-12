from django.urls import path

from . import identity_views

urlpatterns = [
    path(
        "leads/<int:pk>/identity/correct/",
        identity_views.invoice_identity_correction,
        name="invoice_identity_correction",
    ),
    path(
        "leads/<int:pk>/invoice-identity/",
        identity_views.invoice_identity_correction,
        name="invoice_identity_alias",
    ),
]
