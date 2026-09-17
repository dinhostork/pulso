"""Settings for the separate worker process serving the `celery_smoke` tests.

Identical to `config.settings_test` except for one thing: the worker connects
to the database pytest-django actually creates and uses (`test_pulso`) instead
of the base `pulso_tests` database.

Without this, the opt-in News smoke check would be meaningless. pytest-django
creates `test_pulso` for the test session, so a worker started with
`config.settings_test` would read and write `pulso_tests` while the test
asserted against `test_pulso` — two different databases, and a smoke test that
can only pass by accident. The worker's connection is opened lazily and
Celery's Django fixup closes it after every task, so pytest-django can still
drop the test database at the end of the session.

Use it only for that worker process:

    DJANGO_SETTINGS_MODULE=config.settings_smoke_worker \
        uv run --locked celery -A config worker --loglevel=INFO
"""

import copy

from .settings_test import *  # noqa: F403
from .settings_test import DATABASES as _TEST_DATABASES

DATABASES = copy.deepcopy(_TEST_DATABASES)
DATABASES["default"]["NAME"] = DATABASES["default"]["TEST"]["NAME"]
