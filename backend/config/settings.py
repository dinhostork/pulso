"""Shared settings. Local development must explicitly opt into DEBUG."""

from django.core.exceptions import ImproperlyConfigured

from .common import *  # noqa: F403
from .environment import boolean, port, required

SECRET_KEY = required("SECRET_KEY")
DEBUG = boolean("DEBUG")
ALLOWED_HOSTS = [host.strip() for host in required("ALLOWED_HOSTS").split(",")]
if any(not host for host in ALLOWED_HOSTS) or "*" in ALLOWED_HOSTS:
    raise ImproperlyConfigured("ALLOWED_HOSTS must list explicit nonempty hosts")
if not DEBUG and (
    len(SECRET_KEY) < 50
    or len(set(SECRET_KEY)) < 5
    or SECRET_KEY.startswith(("local-only-", "django-insecure-"))
):
    raise ImproperlyConfigured("SECRET_KEY must be a strong non-example key when DEBUG=false")

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": required("POSTGRES_DB"),
        "USER": required("POSTGRES_USER"),
        "PASSWORD": required("POSTGRES_PASSWORD"),
        "HOST": required("POSTGRES_HOST"),
        "PORT": port("POSTGRES_PORT"),
        "OPTIONS": {"connect_timeout": 5},
    }
}
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
