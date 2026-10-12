from django.urls import path

from . import workflow_content_views
from . import workflow_views as views

urlpatterns = [
    path(
        "activities/<int:pk>/publication/",
        workflow_content_views.content_delivery,
        name="content_delivery",
    ),
    path("today/", views.daily, name="daily"),
    path("work-policy/", views.workflow_policy, name="workflow_policy"),
    path("members/<int:pk>/", views.member_detail, name="member_detail"),
    path(
        "delegations/<int:pk>/revoke/",
        views.delegation_revoke,
        name="delegation_revoke",
    ),
    path("leads/<int:pk>/assign/", views.lead_assign, name="lead_assign"),
    path("leads/<int:pk>/contacts/new/", views.contact_create, name="contact_create"),
    path("contacts/<int:pk>/correct/", views.contact_correct, name="contact_correct"),
    path("leads/<int:pk>/history/", views.lead_history, name="lead_history"),
    path(
        "leads/<int:pk>/response/correct/",
        views.response_correct,
        name="response_correct",
    ),
    path("campaigns/<int:pk>/", views.campaign_detail, name="campaign_detail"),
]
