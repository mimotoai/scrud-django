import base64
import binascii

from django.contrib.auth import authenticate
from ninja.errors import AuthenticationError


def basic_user(request):
    header = request.headers.get("Authorization", "")
    scheme, _, credentials = header.partition(" ")
    if scheme.lower() != "basic":
        return getattr(request, "user", None)
    try:
        username, _, password = base64.b64decode(credentials).decode().partition(":")
    except (binascii.Error, UnicodeDecodeError):
        raise AuthenticationError() from None
    user = authenticate(request, username=username, password=password)
    if user is None:
        raise AuthenticationError() from None
    return user
