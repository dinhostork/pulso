"""Request/response shapes for the authentication endpoints (ADR-0009)."""

from django.contrib.auth import authenticate, get_user_model
from rest_framework import serializers
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.serializers import TokenRefreshSerializer


class LoginSerializer(serializers.Serializer):
    username = serializers.CharField()
    password = serializers.CharField(trim_whitespace=False, write_only=True)

    def validate(self, attrs):
        # authenticate() returns None for a wrong password, an unknown
        # username or an inactive account alike (ModelBackend checks
        # is_active): one generic error for all three avoids confirming
        # which case applies, and keeps inactive accounts unauthenticatable.
        user = authenticate(
            request=self.context.get("request"),
            username=attrs["username"],
            password=attrs["password"],
        )
        if user is None:
            raise serializers.ValidationError("Invalid credentials.", code="authorization")
        attrs["user"] = user
        return attrs


class LogoutSerializer(serializers.Serializer):
    refresh = serializers.CharField(write_only=True)


class CurrentUserSerializer(serializers.ModelSerializer):
    class Meta:
        model = get_user_model()
        # Deliberately minimal: no email, name or permission fields yet.
        fields = ["id", "username"]
        read_only_fields = fields


class RefreshSerializer(TokenRefreshSerializer):
    """SimpleJWT's refresh, with a deleted account rejected like an inactive one.

    The installed serializer loads the token's user with `objects.get()` and
    lets a missing row escape as a 500. A refresh token whose account no
    longer exists is lost authentication, so it gets the same 401
    `no_active_account` response as an inactive account. Only the User
    model's own `DoesNotExist` is translated; every other failure is left as
    SimpleJWT or Django raise it.
    """

    def validate(self, attrs):
        try:
            return super().validate(attrs)
        except get_user_model().DoesNotExist:
            raise AuthenticationFailed(
                self.error_messages["no_active_account"], "no_active_account"
            ) from None
