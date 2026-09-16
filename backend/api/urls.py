"""API route registry. Product endpoints arrive in later issues."""

from django.urls import include, path

app_name = "api"
urlpatterns = [path("auth/", include("accounts.urls"))]
