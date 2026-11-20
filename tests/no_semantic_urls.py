"""An app's own scrud API routed without ``scoped_rbac.semantic_urls``
(`tests.test_default_urls`): its role routes cannot reverse the schemas and
contexts they link to, which scoped-rbac's check ``scoped_rbac.E002``
reports."""

from django.urls import path

import scrud_django.ninja

urlpatterns = [
    path(
        "api/",
        scrud_django.ninja.api(
            types=[], urls_namespace="no-semantic", ui_types=False
        ).urls,
    ),
]
