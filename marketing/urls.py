from django.urls import include, path

from . import audit_views, performance_views, views

app_name = "marketing"
urlpatterns = [
    path("", include("marketing.identity_urls")),
    path("", include("marketing.ad_recovery_urls")),
    path("", include("marketing.workflow_urls")),
    path("", include("marketing.performance_workflow_urls")),
    path("kpis/", performance_views.kpis, name="kpis"),
    path("kpis/new/", performance_views.kpi_create, name="kpi_create"),
    path("kpis/<int:pk>/", performance_views.kpi_detail, name="kpi_detail"),
    path("activities/", performance_views.activities, name="activities"),
    path("activities/new/", performance_views.activity_create, name="activity_create"),
    path(
        "activities/<int:pk>/",
        performance_views.activity_detail,
        name="activity_detail",
    ),
    path("audit/", audit_views.audit, name="audit"),
    path("audit/export/", audit_views.audit_export, name="audit_export"),
    path("", views.overview, name="overview"),
    path("analysis/", views.analysis, name="analysis"),
    path("leads/", views.leads, name="leads"),
    path("leads/new/", views.lead_create, name="lead_create"),
    path("leads/<int:pk>/", views.lead_detail, name="lead_detail"),
    path("campaigns/", views.campaigns, name="campaigns"),
    path("campaigns/new/", views.campaign_create, name="campaign_create"),
    path("ads/new/", views.ad_create, name="ad_create"),
    path("ads/<int:pk>/correct/", views.ad_correct, name="ad_correct"),
    path("ads/import/", views.ad_import, name="ad_import"),
    path("ads/template/", views.ad_template, name="ad_template"),
    path("coverage/", views.coverage, name="coverage"),
    path("coverage/new/", views.coverage_create, name="coverage_create"),
    path(
        "coverage/<int:pk>/withdraw/", views.coverage_withdraw, name="coverage_withdraw"
    ),
    path("members/", views.members, name="members"),
    path("members/new/", views.member_create, name="member_create"),
    path("advisor/", views.advisor, name="advisor"),
    path("actions/new/", views.action_create, name="action_create"),
    path("actions/<int:pk>/", views.action_detail, name="action_detail"),
    path("ai-policy/", views.ai_policy, name="ai_policy"),
]
