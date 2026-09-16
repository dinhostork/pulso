import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction


@pytest.mark.django_db
def test_account_roundtrip_preserves_identity_and_hashed_password():
    user_model = get_user_model()
    user = user_model.objects.create_user(username="roundtrip", password="test-password-123!")
    loaded = user_model.objects.get(pk=user.pk)

    assert loaded.pk == user.pk
    assert loaded.username == "roundtrip"
    assert loaded.password != "test-password-123!"
    assert loaded.check_password("test-password-123!")
    assert not loaded.check_password("incorrect")


@pytest.mark.django_db
def test_duplicate_username_is_rejected_by_database():
    user_model = get_user_model()
    user_model.objects.create_user(username="unique-user")
    with pytest.raises(IntegrityError), transaction.atomic():
        user_model.objects.create_user(username="unique-user")
    assert user_model.objects.filter(username="unique-user").count() == 1


@pytest.mark.django_db
def test_account_without_password_has_no_usable_password():
    user = get_user_model().objects.create_user(username="no-password")
    user.refresh_from_db()
    assert not user.has_usable_password()
