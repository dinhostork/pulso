import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from news.models import Story
from reading.application.bookmarks import save_bookmark


@pytest.fixture
def users(db):
    model = get_user_model()
    return model.objects.create_user(username="api-reader-a"), model.objects.create_user(
        username="api-reader-b"
    )


def authenticated(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


@pytest.mark.django_db
def test_all_bookmark_endpoints_reject_anonymous_requests():
    client = APIClient()
    for method, path in (
        (client.get, "/api/bookmarks"),
        (lambda target: client.put(target, {}, format="json"), "/api/bookmarks/1"),
        (lambda target: client.delete(target, {}, format="json"), "/api/bookmarks/1"),
    ):
        response = method(path)
        assert response.status_code == 401
        assert response.json()["code"] == "not_authenticated"
        assert response["Cache-Control"] == "private, no-store"


@pytest.mark.django_db
def test_put_is_idempotent_owner_derived_and_rejects_client_user_id(users, make_story):
    first_user, second_user = users
    story, _ = make_story(50)
    first = authenticated(first_user)
    rejected = first.put(f"/api/bookmarks/{story.pk}", {"user_id": second_user.pk}, format="json")
    assert rejected.status_code == 400
    assert rejected.json()["code"] == "validation_error"

    response = first.put(f"/api/bookmarks/{story.pk}", {}, format="json")
    repeated = first.put(f"/api/bookmarks/{story.pk}", {}, format="json")
    assert response.status_code == repeated.status_code == 200
    assert response.json() == repeated.json()
    assert response.json()["story_id"] == str(story.pk)
    assert response["Cache-Control"] == "private, no-store"
    assert authenticated(second_user).get("/api/bookmarks").json()["results"] == []


@pytest.mark.django_db(transaction=True)
def test_list_returns_available_card_and_archived_tombstone_only_to_owner(users, make_story):
    user, other = users
    available, _ = make_story(51)
    archived, _ = make_story(52)
    save_bookmark(user=user, story_id=available.pk)
    save_bookmark(user=user, story_id=archived.pk)
    Story.objects.filter(pk=archived.pk).update(status=Story.Status.ARCHIVED)

    body = authenticated(user).get("/api/bookmarks?limit=20").json()
    entries = {entry["story_id"]: entry for entry in body["results"]}
    assert entries[str(archived.pk)] == {
        "story_id": str(archived.pk),
        "saved_at": entries[str(archived.pk)]["saved_at"],
        "availability": "UNAVAILABLE",
    }
    assert entries[str(available.pk)]["availability"] == "AVAILABLE"
    assert entries[str(available.pk)]["story"]["viewer"] == {"bookmarked": True}
    assert authenticated(other).get("/api/bookmarks").json()["results"] == []


@pytest.mark.django_db(transaction=True)
def test_delete_is_idempotent_scoped_and_works_after_archival(users, make_story):
    user, other = users
    story, _ = make_story(53)
    save_bookmark(user=user, story_id=story.pk)
    save_bookmark(user=other, story_id=story.pk)
    Story.objects.filter(pk=story.pk).update(status=Story.Status.ARCHIVED)

    client = authenticated(user)
    assert client.delete(f"/api/bookmarks/{story.pk}", {}, format="json").status_code == 204
    assert client.delete(f"/api/bookmarks/{story.pk}", {}, format="json").status_code == 204
    assert len(authenticated(other).get("/api/bookmarks").json()["results"]) == 1


@pytest.mark.django_db
def test_api_preserves_missing_unavailable_cursor_and_input_errors(users, make_story):
    user, other = users
    archived, _ = make_story(54, status=Story.Status.ARCHIVED)
    client = authenticated(user)
    missing = client.put("/api/bookmarks/999999999", {}, format="json")
    unavailable = client.put(f"/api/bookmarks/{archived.pk}", {}, format="json")
    invalid = client.get("/api/bookmarks?limit=51&user_id=2")
    cursor = client.get("/api/bookmarks?cursor=not-signed").json()

    assert (missing.status_code, missing.json()["code"]) == (404, "story_not_found")
    assert (unavailable.status_code, unavailable.json()["code"]) == (
        410,
        "story_unavailable",
    )
    assert invalid.status_code == 400 and "user_id" in invalid.json()["fields"]
    assert cursor["code"] == "invalid_cursor"
    assert (
        authenticated(other).delete(f"/api/bookmarks/{archived.pk}", {}, format="json").status_code
        == 204
    )


@pytest.mark.django_db
def test_malformed_mutation_body_uses_the_stable_validation_error(users, make_story):
    user, _ = users
    story, _ = make_story(55)
    response = authenticated(user).put(
        f"/api/bookmarks/{story.pk}", data='{"broken":', content_type="application/json"
    )
    assert response.status_code == 400
    assert response.json() == {
        "code": "validation_error",
        "detail": "The request is invalid.",
        "fields": {"body": ["Malformed request body."]},
    }
