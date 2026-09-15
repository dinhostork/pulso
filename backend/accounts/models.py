from django.contrib.auth.models import AbstractUser


class User(AbstractUser):
    """Stable account identity using Django's standard password behavior."""
