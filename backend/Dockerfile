FROM ghcr.io/astral-sh/uv:0.12.13@sha256:b485bd65cc2cf1c9a93b3554012c9c3778cf7b1b5fd3d3096ce9e1226c97e1e6 AS uv
FROM python:3.14.4-slim-bookworm@sha256:fc74d22ffd0d5ac395a4b7bdda75a4539758862c49ebf3005647084631e63789

COPY --from=uv /uv /usr/local/bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"
WORKDIR /app

# Install only the existing locked dependencies. Keep the venv outside the bind mount.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-dev --no-cache

COPY . .
EXPOSE 8000
# Schema changes remain an explicit `docker compose run ... migrate` step.
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
