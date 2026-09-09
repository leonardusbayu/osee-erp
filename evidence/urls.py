from django.urls import path

from . import views


app_name = "evidence"
urlpatterns = [path("<int:pk>/download/", views.download, name="download")]
