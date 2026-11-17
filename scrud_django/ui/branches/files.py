"""What CSS and JS items share: the text of a page's stylesheet or script,
per branch, served at versioned URLs
(``<api or prefix>ui-<kind>-files/<name>/<etag>.<kind>``) and linked by the
page.

The URL holds the item's ETag, so a URL is immutable (a year's caching) and a
stale one is a 404. Presentation is read with system rights (ruling R12):
what gates a file is the branch the request selects (`choose`, which refuses
a working branch the viewer may not GET) and, under a prefix, `check_mount`.
"""

from django.http import Http404, HttpResponse
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from scrud_django.ui.branches.effective import safe_mode_requested, using_safe_mode
from scrud_django.ui.branches.file_urls import file_items, file_urls  # noqa: F401
from scrud_django.ui.branches.middleware import check_mount
from scrud_django.ui.branches.selection import choose

MAX_AGE = 31536000  # a year: the URL holds the ETag


class ItemFileView(APIView):
    """GET an item's text, by name and ETag. Anyone who can see a page may
    fetch its files, so there is no permission of its own. A subclass sets
    `files` (the effective ``(name, etag, text)`` of a selection) and
    `content_type`."""

    permission_classes = [AllowAny]
    http_method_names = ["get", "head", "options"]
    content_type: str

    def files(self, selection) -> list[tuple[str, str, str]]:
        raise NotImplementedError

    def get(self, request, name: str, etag: str):
        check_mount(request)
        chosen = choose(request)
        with using_safe_mode(safe_mode_requested(request)):
            files = self.files(chosen.selection)
        text = next(
            (
                text
                for found, found_etag, text in files
                if found == name and found_etag == etag
            ),
            None,
        )
        if text is None:
            raise Http404()
        response = HttpResponse(text, content_type=self.content_type)
        # Never public: a mount's files are gated by GET on its branch, which
        # a shared cache would not check.
        response["Cache-Control"] = f"private, max-age={MAX_AGE}, immutable"
        response["X-Content-Type-Options"] = "nosniff"
        return response
