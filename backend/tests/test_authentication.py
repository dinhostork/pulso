from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

LOGIN_URL = "/api/auth/login"
LOGOUT_URL = "/api/auth/logout"
REFRESH_URL = "/api/auth/refresh"
ME_URL = "/api/auth/me"

PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def user(db):
    return get_user_model().objects.create_user(username="alice", password=PASSWORD)


@pytest.fixture
def inactive_user(db):
    return get_user_model().objects.create_user(
        username="inactive-bob", password=PASSWORD, is_active=False
    )


def login(client, username, password):
    return client.post(
        LOGIN_URL, {"username": username, "password": password}, content_type="application/json"
    )


def bearer(access):
    return {"HTTP_AUTHORIZATION": f"Bearer {access}"}


@pytest.mark.django_db
def test_login_with_valid_credentials_returns_access_and_refresh_tokens(user):
    response = login(Client(), "alice", PASSWORD)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"access", "refresh"}
    assert body["access"] and body["refresh"]


@pytest.mark.django_db
def test_login_with_wrong_password_is_rejected_generically(user):
    response = login(Client(), "alice", "wrong-password")

    assert response.status_code == 400
    assert "token" not in response.content.decode().lower()


@pytest.mark.django_db
def test_login_with_unknown_username_is_rejected_with_the_same_generic_error(user):
    known_user_response = login(Client(), "alice", "wrong-password")
    unknown_user_response = login(Client(), "someone-who-does-not-exist", "wrong-password")

    assert unknown_user_response.status_code == known_user_response.status_code == 400
    assert unknown_user_response.json() == known_user_response.json()


@pytest.mark.django_db
def test_login_rejects_inactive_accounts_with_the_same_generic_error(inactive_user):
    active_failure = login(Client(), "someone-who-does-not-exist", "x")
    inactive_failure = login(Client(), "inactive-bob", PASSWORD)

    assert inactive_failure.status_code == active_failure.status_code == 400
    assert inactive_failure.json() == active_failure.json()


@pytest.mark.django_db
def test_password_never_appears_in_the_login_response(user):
    response = login(Client(), "alice", PASSWORD)
    assert PASSWORD not in response.content.decode()
    assert user.password not in response.content.decode()


@pytest.mark.django_db
def test_current_user_requires_authentication():
    response = Client().get(ME_URL)
    assert response.status_code == 401


@pytest.mark.django_db
def test_current_user_rejects_a_malformed_token():
    response = Client().get(ME_URL, **bearer("not-a-real-token"))
    assert response.status_code == 401


@pytest.mark.django_db
def test_current_user_returns_only_minimal_fields_for_a_valid_access_token(user):
    access = login(Client(), "alice", PASSWORD).json()["access"]

    response = Client().get(ME_URL, **bearer(access))

    assert response.status_code == 200
    assert response.json() == {"id": user.id, "username": "alice"}
    assert user.password not in response.content.decode()


@pytest.mark.django_db
def test_current_user_rejects_an_expired_access_token(user):
    token = AccessToken.for_user(user)
    token.set_exp(lifetime=timedelta(seconds=-1))  # already expired

    response = Client().get(ME_URL, **bearer(str(token)))

    assert response.status_code == 401


@pytest.mark.django_db
def test_refresh_token_yields_a_new_access_token(user):
    refresh = login(Client(), "alice", PASSWORD).json()["refresh"]

    response = Client().post(REFRESH_URL, {"refresh": refresh}, content_type="application/json")

    assert response.status_code == 200
    assert "access" in response.json()


@pytest.mark.django_db
def test_an_access_token_cannot_be_used_at_the_refresh_endpoint(user):
    access = login(Client(), "alice", PASSWORD).json()["access"]

    response = Client().post(REFRESH_URL, {"refresh": access}, content_type="application/json")

    assert response.status_code == 401


@pytest.mark.django_db
def test_logout_requires_no_access_token_only_the_refresh_token(user):
    refresh = login(Client(), "alice", PASSWORD).json()["refresh"]

    # No Authorization header at all: possessing the refresh token is the
    # only authority logout needs (ADR-0009).
    response = Client().post(LOGOUT_URL, {"refresh": refresh}, content_type="application/json")

    assert response.status_code == 205


@pytest.mark.django_db
def test_logout_rejects_a_missing_refresh_field():
    response = Client().post(LOGOUT_URL, {}, content_type="application/json")
    assert response.status_code == 400


@pytest.mark.django_db
def test_logout_rejects_an_already_invalidated_refresh_token(user):
    refresh = login(Client(), "alice", PASSWORD).json()["refresh"]
    client = Client()
    client.post(LOGOUT_URL, {"refresh": refresh}, content_type="application/json")

    second_attempt = client.post(LOGOUT_URL, {"refresh": refresh}, content_type="application/json")

    assert second_attempt.status_code == 400


@pytest.mark.django_db
def test_logout_blocks_further_refresh_with_the_same_token(user):
    refresh = login(Client(), "alice", PASSWORD).json()["refresh"]
    client = Client()
    client.post(LOGOUT_URL, {"refresh": refresh}, content_type="application/json")

    response = client.post(REFRESH_URL, {"refresh": refresh}, content_type="application/json")

    assert response.status_code == 401


@pytest.mark.django_db
def test_logout_does_not_revoke_an_already_issued_unexpired_access_token(user):
    """Documents the recorded residual-validity contract (ADR-0009): logout
    blacklists the refresh token, but a not-yet-expired access token issued
    before logout keeps authenticating until it naturally expires, since
    access tokens are verified statelessly (signature + exp), not looked up
    per request."""
    tokens = login(Client(), "alice", PASSWORD).json()
    client = Client()
    client.post(LOGOUT_URL, {"refresh": tokens["refresh"]}, content_type="application/json")

    response = client.get(ME_URL, **bearer(tokens["access"]))

    assert response.status_code == 200
    assert response.json()["username"] == "alice"


@pytest.mark.django_db
def test_login_and_logout_require_no_csrf_token(user):
    """Login/logout use stateless JWT bearer auth, not cookies/sessions, so
    CSRF protection does not apply (ADR-0009); enforce=True would reject an
    unsafe-method request without a CSRF token if this view relied on it."""
    client = Client(enforce_csrf_checks=True)

    login_response = login(client, "alice", PASSWORD)
    assert login_response.status_code == 200

    refresh = login_response.json()["refresh"]
    logout_response = client.post(LOGOUT_URL, {"refresh": refresh}, content_type="application/json")
    assert logout_response.status_code == 205


@pytest.mark.django_db
def test_blacklisting_a_refresh_token_does_not_create_or_touch_domain_users(user):
    user_model = get_user_model()
    before = set(user_model.objects.values_list("id", flat=True))

    refresh = RefreshToken.for_user(user)
    refresh.blacklist()

    after = set(user_model.objects.values_list("id", flat=True))
    assert before == after
