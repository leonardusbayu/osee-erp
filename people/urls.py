from django.urls import path

from . import views

app_name = "people"
urlpatterns = [
    path("", views.index, name="index"),
    path("people/new/", views.person_create, name="person_create"),
    path("people/<int:pk>/status/", views.person_status, name="person_status"),
    path("runs/new/", views.run_create, name="run_create"),
    path("runs/<int:pk>/", views.run_detail, name="run_detail"),
    path("runs/<int:pk>/lines/new/", views.line_add, name="line_add"),
    path("runs/<int:pk>/lines/<int:line_pk>/edit/", views.line_edit, name="line_edit"),
    path(
        "runs/<int:pk>/lines/<int:line_pk>/remove/",
        views.line_remove,
        name="line_remove",
    ),
    path("runs/<int:pk>/actions/<str:action>/", views.run_action, name="run_action"),
    path("runs/<int:pk>/payments/", views.payment_match, name="payment_match"),
]
