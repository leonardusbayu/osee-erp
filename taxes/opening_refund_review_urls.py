from django.urls import path

from . import opening_refund_review_views as views

app_name = "opening_refund_tax"
urlpatterns = [path("<int:pk>/review/", views.review, name="review")]
