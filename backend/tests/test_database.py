import pytest
from django.db import connection


@pytest.mark.django_db
def test_database_is_isolated_and_vector_migration_was_applied():
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_database(), current_user")
        assert cursor.fetchone() == ("test_pulso", "pulso_tests")
        cursor.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        assert cursor.fetchone() == ("0.8.6",)
        cursor.execute(
            "SELECT COUNT(*) FROM django_migrations WHERE app = %s AND name = %s",
            ["database", "0001_enable_vector"],
        )
        assert cursor.fetchone() == (1,)


@pytest.mark.django_db
def test_vector_distance_and_ordering_use_postgresql():
    with connection.cursor() as cursor:
        cursor.execute("SELECT '[0,0,0]'::vector <-> '[3,4,0]'::vector")
        assert cursor.fetchone()[0] == pytest.approx(5.0)
        cursor.execute(
            "SELECT label FROM (VALUES ('far', '[3,4,0]'::vector), "
            "('near', '[1,0,0]'::vector)) AS candidates(label, v) "
            "ORDER BY v <-> '[0,0,0]'::vector"
        )
        assert cursor.fetchall() == [("near",), ("far",)]
