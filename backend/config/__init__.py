"""Runtime configuration and composition of the backend."""

from .celery import app as celery_app

__all__ = ("celery_app",)
