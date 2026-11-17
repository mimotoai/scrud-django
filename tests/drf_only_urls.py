"""The UI test URLconf (``tests.mounted_urls``: the API under ``/api/``, the
UI at ``/``) with three more types under ``/api/``, each served by a custom
``viewset_class`` whose ``IsAuthorized`` subclass,
``ContextDenyingIsAuthorized``, refuses anything in the context "denied"
(``tests.test_ui_drf_only_authorization``):

- ``denied-notes``: that authorization is DRF's only, so the UI refuses the
  type;
- ``core-denied-notes``: registered with ``ui_authorization="core"``,
  declaring the core's checks complete for it;
- ``hooked-denied-notes``: its rule declared to the core as ``checks=`` and
  ``object_checks=`` too.

The types are `tests.resource_types.drf_only_types`. Their routes are built
when this URLconf is imported; their rows, which a test's rollback removes,
are registered again by the tests."""

from django.urls import include, path

from . import mounted_urls
from .resource_types import drf_only_types

urlpatterns = [
    path("api/", include(drf_only_types())),
    *mounted_urls.urlpatterns,
]
