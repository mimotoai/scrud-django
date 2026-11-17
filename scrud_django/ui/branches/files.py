"""What CSS and JS items share at the API's URLs: the text of a page's
stylesheet or script, per branch, served at versioned URLs
(``<api>ui-<kind>-files/<name>/<etag>.<kind>``) and linked by the
page. The answer is `scrud_django.ui.branches.file_views.file_response`, which
the UI app's views give too.

The URL holds the item's ETag, so a URL is immutable (a year's caching) and a
stale one is a 404. Presentation is read with system rights (ruling R12):
what gates a file is the branch the request selects (`choose`, which refuses
a working branch the viewer may not GET). The API's URLs are never under a
prefix: a prefix's files are the UI app's (`file_views`, which ask GET on
its branch).
"""

from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from scrud_django.ui.branches.file_urls import file_items, file_urls  # noqa: F401
from scrud_django.ui.branches.file_views import (  # noqa: F401
    MAX_AGE,
    file_response,
)


class ItemFileView(APIView):
    """GET an item's text, by name and ETag (`file_response`), at the API's
    URLs. Anyone who can see a page may fetch its files, so there is no
    permission of its own. A subclass sets `files` (the effective
    ``(name, etag, text)`` of a selection) and `content_type`."""

    permission_classes = [AllowAny]
    http_method_names = ["get", "head", "options"]
    content_type: str

    def files(self, selection) -> list[tuple[str, str, str]]:
        raise NotImplementedError

    def get(self, request, name: str, etag: str):
        return file_response(request, self.files, name, etag, self.content_type)
