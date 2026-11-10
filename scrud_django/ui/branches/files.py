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
from django.urls import reverse
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from scrud_django.ui.branches.effective import (
    SAFE_PARAMETER,
    Selection,
    effective_rows,
    safe_mode,
    safe_mode_requested,
    using_safe_mode,
)
from scrud_django.ui.branches.middleware import check_mount
from scrud_django.ui.branches.selection import choose, in_mount

MAX_AGE = 31536000  # a year: the URL holds the ETag


def file_items(
    type_uri: str, field: str, selection: Selection | None = None
) -> list[tuple[str, str, str]]:
    """``(name, etag, text)`` of the items of `type_uri` effective on
    `selection` (default: the active one), whose text is in `field`, in
    order, then by name. An item without a string name or text is left
    out."""
    found = []
    for _pk, etag, content in effective_rows(type_uri, selection):
        name, text = content.get("name"), content.get(field)
        if isinstance(name, str) and isinstance(text, str):
            order = content.get("order")
            found.append((order if isinstance(order, int) else 0, name, etag, text))
    found.sort(key=lambda item: item[:2])
    return [(name, etag, text) for _order, name, etag, text in found]


def file_urls(request, url_name: str, files: list[tuple[str, str, str]]) -> list[str]:
    """The URLs of `files` (route `url_name`), under the prefix of `request`'s
    page when it has one. In safe mode they carry ``?ui-safe=1``, which the
    file views honour (the layer's items only)."""
    suffix = f"?{SAFE_PARAMETER}=1" if safe_mode() else ""
    return [
        in_mount(request, reverse(url_name, args=[name, etag])) + suffix
        for name, etag, _text in files
    ]


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
