from django.urls import path

from . import opening_balance_review_views as views

app_name = "opening_balance_tax"
urlpatterns = [path("<int:pk>/review/", views.review, name="review")]
