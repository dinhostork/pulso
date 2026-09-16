"""Request/response shapes for the authentication endpoints (ADR-0009)."""

from django.contrib.auth import authenticate, get_user_model
from rest_framework import serializers


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
