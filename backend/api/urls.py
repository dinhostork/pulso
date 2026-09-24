"""API route registry."""

from django.urls import include, path

app_name = "api"
urlpatterns = [
    path("auth/", include("accounts.urls")),
    path("", include("reading.urls")),
]
