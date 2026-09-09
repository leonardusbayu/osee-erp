from django.urls import path
from . import views

app_name = "marketing"
urlpatterns = [
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
    path("members/", views.members, name="members"),
    path("members/new/", views.member_create, name="member_create"),
    path("advisor/", views.advisor, name="advisor"),
    path("actions/new/", views.action_create, name="action_create"),
    path("actions/<int:pk>/", views.action_detail, name="action_detail"),
    path("ai-policy/", views.ai_policy, name="ai_policy"),
]
