from django.urls import path

from . import ad_recovery_views

urlpatterns = [
    path("ads/<int:pk>/", ad_recovery_views.ad_detail, name="ad_detail"),
    path("ads/<int:pk>/withdraw/", ad_recovery_views.ad_withdraw, name="ad_withdraw"),
    path("ads/<int:pk>/replace/", ad_recovery_views.ad_replace, name="ad_replace"),
]
