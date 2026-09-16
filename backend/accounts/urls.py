"""Authentication route registry (issue #6, ADR-0009)."""

from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

from . import views

app_name = "accounts"
urlpatterns = [
    path("login", views.LoginView.as_view(), name="login"),
    path("logout", views.LogoutView.as_view(), name="logout"),
    path("refresh", TokenRefreshView.as_view(), name="refresh"),
    path("me", views.CurrentUserView.as_view(), name="me"),
]
