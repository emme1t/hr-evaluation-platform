from django.urls import path

from . import views


app_name = "accounts"

urlpatterns = [
    path("wecom/start/", views.wecom_start, name="wecom_start"),
    path("wecom/callback/", views.wecom_callback, name="wecom_callback"),
]
