"""Login, logout and current-user endpoints (issue #6, ADR-0009)."""

from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from .serializers import CurrentUserSerializer, LoginSerializer, LogoutSerializer


class LoginView(APIView):
    """Exchange a username/password for an access/refresh token pair."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = LoginSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        refresh = RefreshToken.for_user(serializer.validated_data["user"])
        return Response(
            {"access": str(refresh.access_token), "refresh": str(refresh)},
            status=status.HTTP_200_OK,
        )


class LogoutView(APIView):
    """Blacklist a refresh token so it can no longer mint access tokens.

    Requires only the refresh token, not a valid access token: possession
    of the refresh token is itself sufficient authority to invalidate it,
    the same trust model /auth/refresh/ already relies on. See ADR-0009 for
    the resulting residual-validity window on the paired access token.
    """

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = LogoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            RefreshToken(serializer.validated_data["refresh"]).blacklist()
        except TokenError:
            return Response(
                {"detail": "Invalid or already invalidated refresh token."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(status=status.HTTP_205_RESET_CONTENT)


class CurrentUserView(APIView):
    """Return the minimal identity of the authenticated account."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(CurrentUserSerializer(request.user).data)
