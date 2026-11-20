"""``tests.drf_only_urls`` again, but for ``denied-notes``, whose routes are
registration's default ``ResourceViewSet``'s here: the same slug built
differently by two URLconfs of one process
(``tests.test_ui_drf_only_authorization``)."""

from django.urls import include, path

from scrud_django import registration

from . import mounted_urls
from .resource_types import DENIED_NOTE_TYPE_URI, note_type

urlpatterns = [
    path(
        "api/",
        include(
            registration.json_resource_type(
                **note_type(DENIED_NOTE_TYPE_URI, "denied-notes", "Denied note")
            )
        ),
    ),
    *mounted_urls.urlpatterns,
]
