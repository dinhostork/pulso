"""Health endpoint route registry."""

from django.urls import path

from . import views

app_name = "health"
urlpatterns = [
    path("live", views.liveness, name="live"),
    path("ready", views.readiness, name="ready"),
]
