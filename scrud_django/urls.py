"""SCRUD DJANGO URL Configuration: scrud's ninja API
(`scrud_django.ninja.api`, in the URL namespace ``"scrud"``: the built-in
types, the UI's and the studio's when their apps are installed, the service
catalog, scoped-rbac's role routes and the collection documents) and
scoped-rbac's semantic documents (``semantic/``: the schemas and contexts its
role routes link to). A project staying on DRF includes
``scrud_django.drf.urls`` instead (with the ``drf`` extra); one building its
own ``api()`` includes that instead of this (two scrud APIs in one namespace
are the system check ``scrud_django.E010``)."""

from django.urls import include, path

import scrud_django.ninja

urlpatterns = [
    path("", scrud_django.ninja.api(types=[]).urls),
    path("semantic/", include("scoped_rbac.semantic_urls")),
]
