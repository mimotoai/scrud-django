"""The UI test URLconf (``tests.mounted_urls``: the API under ``/api/``, the
UI at ``/``) with more types under ``/api/``, each served by
``tests.urls.ContextDenyingResourceViewSet``, whose ``IsAuthorized``
subclass, ``ContextDenyingIsAuthorized``, refuses anything in the context
"denied" (``tests.test_ui_drf_only_authorization``):

- through registration, as a custom ``viewset_class``
  (`tests.resource_types.drf_only_types`): ``denied-notes`` (that
  authorization is DRF's only, so the UI refuses the type),
  ``core-denied-notes`` (``ui_authorization="core"``),
  ``hooked-denied-notes`` (its rule declared to the core as ``checks=`` and
  ``object_checks=`` too), ``checked-denied-notes`` and
  ``object-checked-denied-notes`` (only one of the two: refused);
- routed by hand, not by registration
  (`tests.resource_types.hand_routed_types`): ``hand-routed-notes``
  (refused) and ``core-hand-routed-notes`` (``ui_authorization="core"``).

Their routes are built when this URLconf is imported; their rows, which a
test's rollback removes, are registered again by the tests."""

from django.urls import include, path

from . import mounted_urls
from .resource_types import drf_only_types, hand_routed_types

urlpatterns = [
    path("api/", include([*drf_only_types(), *hand_routed_types()])),
    *mounted_urls.urlpatterns,
]
