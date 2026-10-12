from django.urls import path

from . import supplier_advance_review_views as views

app_name = "supplier_advance_tax"
urlpatterns = [path("<int:pk>/review/", views.review, name="review")]
