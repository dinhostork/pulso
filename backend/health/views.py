"""Liveness/readiness views (issue #5). See the health contract in README.md."""

from django.http import JsonResponse
from django.views.decorators.http import require_GET

from .checks import check_database, check_redis


@require_GET
def liveness(request):
    """Always 200 while the process can handle requests.

    Independent of every external service by construction: it calls no
    dependency probe at all.
    """
    return JsonResponse({"status": "ok"})


@require_GET
def readiness(request):
    """Report the dependencies strictly required to serve traffic.

    PostgreSQL is required: its failure returns 503. Redis is not required
    today, because no HTTP endpoint dispatches a task synchronously; its
    failure is reported as "degraded" with HTTP 200, since ordinary
    request traffic can still be served and only background/async
    processing is affected. Worker/Beat liveness is outside this contract;
    only broker (Redis) reachability is probed.
    """
    database_healthy = check_database()
    redis_healthy = check_redis()

    if not database_healthy:
        status, http_status = "unavailable", 503
    elif not redis_healthy:
        status, http_status = "degraded", 200
    else:
        status, http_status = "ready", 200

    return JsonResponse(
        {
            "status": status,
            "dependencies": {
                "database": {
                    "status": "healthy" if database_healthy else "unhealthy",
                    "required": True,
                },
                "redis": {
                    "status": "healthy" if redis_healthy else "unhealthy",
                    "required": False,
                },
            },
        },
        status=http_status,
    )
