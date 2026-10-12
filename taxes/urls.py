from django.urls import path

from . import monthly_source_views as monthly_sources
from . import recipient_identity_views as recipient_identity
from . import source_review_views, views
from . import workspace_views as workspace

app_name = "taxes"
urlpatterns = [
    path("", views.index, name="index"),
    path("recipients/", recipient_identity.index, name="recipient_identities"),
    path("recipients/new/", recipient_identity.prepare, name="recipient_identity_new"),
    path("recipients/<int:pk>/", recipient_identity.detail, name="recipient_identity"),
    path(
        "recipients/<int:pk>/review/",
        recipient_identity.review,
        name="recipient_identity_review",
    ),
    path("monthly/sources/", monthly_sources.index, name="monthly_sources"),
    path("source-reviews/", source_review_views.source_reviews, name="source_reviews"),
    path("monthly/sources/<int:pk>/", monthly_sources.detail, name="monthly_source"),
    path(
        "monthly/sources/<int:pk>/export/",
        monthly_sources.export,
        name="monthly_source_export",
    ),
    path(
        "monthly/sources/<int:pk>/review/",
        monthly_sources.review,
        name="monthly_source_review",
    ),
    path(
        "monthly/sources/<int:pk>/correction/",
        monthly_sources.correction,
        name="monthly_source_correction",
    ),
    path(
        "monthly/sources/<int:pk>/draft/",
        monthly_sources.generate,
        name="monthly_source_generate",
    ),
    path("annual/", views.annual, name="annual"),
    path("chat/", views.chat, name="chat"),
    path("api/chat/", views.chat_api, name="chat_api"),
    path(
        "api/conversations/<uuid:conversation_id>/",
        views.conversation_api,
        name="conversation_api",
    ),
    path("export/", views.export, name="export"),
    path("evidence/upload/", workspace.evidence_upload, name="evidence_upload"),
    path(
        "evidence/<int:pk>/download/",
        workspace.evidence_download,
        name="evidence_download",
    ),
    path("profile/review/", workspace.profile_review, name="profile_review"),
    path("bills/<int:pk>/review/", workspace.bill_review, name="bill_review"),
    path("obligations/new/", workspace.obligation_new, name="obligation_new"),
    path("obligations/<int:pk>/", workspace.obligation_detail, name="obligation"),
    path(
        "obligations/<int:pk>/withdraw/",
        workspace.obligation_withdraw,
        name="obligation_withdraw",
    ),
    path(
        "obligations/<int:pk>/review/",
        workspace.obligation_review,
        name="obligation_review",
    ),
    path(
        "obligations/<int:pk>/verify/",
        workspace.obligation_verify,
        name="obligation_verify",
    ),
    path("annual/prepare/", workspace.annual_prepare, name="annual_prepare"),
    path(
        "annual/workpapers/<int:pk>/", workspace.annual_detail, name="annual_workpaper"
    ),
    path(
        "annual/workpapers/<int:pk>/review/",
        workspace.annual_review,
        name="annual_review",
    ),
    path(
        "annual/workpapers/<int:pk>/verify/",
        workspace.annual_verify,
        name="annual_verify",
    ),
    path(
        "annual/workpapers/<int:pk>/export/",
        workspace.annual_export,
        name="annual_export",
    ),
]
