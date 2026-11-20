"""A URLconf serving an app's own model (``tests.Article``) through scrud's
ninja API with its workflow (`tests.own_models`): the router added to
``scrud_django.ninja.api`` before its URL patterns are read, which happens
once (ninja refuses a second read of a namespace)."""

from django.urls import include, path

import scrud_django.ninja

from .own_models import router

API = scrud_django.ninja.api(types=[], urls_namespace="own-models")
API.add_router("articles/", router)

urlpatterns = [
    path("api/", API.urls),
    path("semantic/", include("scoped_rbac.semantic_urls")),
]
