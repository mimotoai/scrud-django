"""A scrud ninja API with ninja's throttling (`tests.test_default_urls`): the
remedy the README gives for ``REST_FRAMEWORK["DEFAULT_THROTTLE_CLASSES"]``,
which ninja does not read. ``UserRateThrottle`` keys a request by Django's
``request.user`` (else the client's address)."""

from django.urls import include, path
from ninja.throttling import UserRateThrottle

import scrud_django.ninja

urlpatterns = [
    path(
        "api/",
        scrud_django.ninja.api(
            types=[],
            urls_namespace="throttled",
            ui_types=False,
            throttle=[UserRateThrottle("2/min")],
        ).urls,
    ),
    path("api/semantic/", include("scoped_rbac.semantic_urls")),
]
