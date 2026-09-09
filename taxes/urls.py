from django.urls import path

from . import views

app_name = "taxes"
urlpatterns = [
    path("", views.index, name="index"),
    path("annual/", views.annual, name="annual"),
    path("chat/", views.chat, name="chat"),
    path("api/chat/", views.chat_api, name="chat_api"),
    path("api/conversations/<uuid:conversation_id>/", views.conversation_api, name="conversation_api"),
    path("export/", views.export, name="export"),
]
