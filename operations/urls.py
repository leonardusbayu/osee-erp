from django.urls import path

from . import course_views, views

app_name = "operations"
urlpatterns = [
    path("", views.overview, name="overview"),
    path("orders/new/", views.order_create, name="order_create"),
    path("orders/<int:pk>/", views.order_detail, name="order"),
    path("orders/<int:pk>/lead/", views.order_lead, name="order_lead"),
    path(
        "leads/<int:pk>/invoice-correction/",
        views.attribution_correction,
        name="attribution_correction",
    ),
    path(
        "lines/<int:pk>/existing-invoice/",
        views.existing_invoice,
        name="existing_invoice",
    ),
    path("orders/<int:pk>/lines/new/", views.line_create, name="line_create"),
    path("lines/<int:pk>/invoice/", views.line_invoice, name="line_invoice"),
    path(
        "lines/<int:pk>/register/",
        views.registration_create,
        name="registration_create",
    ),
    path("registrations/<int:pk>/", views.registration_detail, name="registration"),
    path("lines/<int:line_pk>/course/", course_views.course_plan, name="course_plan"),
    path(
        "course-meetings/<int:meeting_pk>/reschedule/",
        course_views.course_meeting_reschedule,
        name="course_meeting_reschedule",
    ),
    path(
        "registrations/<int:registration_pk>/course-meetings/<int:meeting_pk>/attendance/",
        course_views.course_attendance,
        name="course_attendance",
    ),
    path(
        "registrations/<int:pk>/<str:action>/",
        views.registration_action,
        name="registration_action",
    ),
    path("sessions/", views.sessions, name="sessions"),
    path("intake/", views.intake_upload, name="intake_upload"),
    path("intake/<int:pk>/", views.intake_review, name="intake_review"),
    path("intake/<int:pk>/map/", views.intake_map, name="intake_map"),
]
