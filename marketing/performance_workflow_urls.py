from django.urls import path

from . import performance_workflow_views as views

urlpatterns = [
    path("management/", views.management, name="management"),
    path(
        "kpis/<int:pk>/sources/new/",
        views.source_period_create,
        name="source_period_create",
    ),
    path(
        "source-periods/<int:pk>/",
        views.source_period_detail,
        name="source_period_detail",
    ),
    path(
        "activities/<int:pk>/evidence/",
        views.evidence_upload,
        name="work_evidence_upload",
    ),
    path("activities/<int:pk>/correct/", views.work_correction, name="work_correction"),
    path("activities/<int:pk>/handoff/", views.work_handoff, name="work_handoff"),
    path(
        "work-evidence/<int:pk>/download/",
        views.evidence_download,
        name="work_evidence_download",
    ),
]
