"""Bounded-timeout dependency probes for the health endpoints.

Each probe opens its own short-lived connection instead of reusing Django's
request-scoped database connection or a shared Redis client, so a slow or
unreachable dependency cannot block ordinary request handling and cannot
leave a half-open connection behind.
"""

import psycopg
import redis
from django.conf import settings


def check_database() -> bool:
    """Probe PostgreSQL with a bounded connect and statement timeout."""
    db = settings.DATABASES["default"]
    timeout = settings.HEALTH_CHECK_TIMEOUT_SECONDS
    try:
        with psycopg.connect(
            host=db["HOST"],
            port=db["PORT"],
            dbname=db["NAME"],
            user=db["USER"],
            password=db["PASSWORD"],
            connect_timeout=timeout,
            options=f"-c statement_timeout={int(timeout * 1000)}",
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
        return True
    except psycopg.Error:
        return False


def check_redis() -> bool:
    """Probe Redis (the Celery broker) with a bounded socket timeout."""
    timeout = settings.HEALTH_CHECK_TIMEOUT_SECONDS
    client = redis.Redis.from_url(
        settings.CELERY_BROKER_URL,
        socket_connect_timeout=timeout,
        socket_timeout=timeout,
    )
    try:
        return bool(client.ping())
    except redis.RedisError:
        return False
    finally:
        client.close()
