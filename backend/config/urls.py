"""Compose HTTP interfaces without introducing domain behavior."""

from django.urls import include, path

urlpatterns = [
    path("api/", include("api.urls")),
    path("health/", include("health.urls")),
]
