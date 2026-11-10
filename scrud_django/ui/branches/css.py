"""CSS items: the text of a page's CSS, per branch, served at versioned URLs
(``<api or prefix>ui-css-files/<name>/<etag>.css``) and linked by the page.

The URL holds the item's ETag, so a URL is immutable (a year's caching) and a
stale one is a 404. Presentation is read with system rights (ruling R12):
what gates a file is the branch the request selects (`choose`, which refuses
a working branch the viewer may not GET) and, under a prefix, `check_mount`.
"""

from django.http import Http404, HttpResponse
from django.urls import reverse
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from scrud_django.ui.branches import UI_CSS_TYPE_URI
from scrud_django.ui.branches.effective import Selection, effective_rows
from scrud_django.ui.branches.middleware import check_mount
from scrud_django.ui.branches.selection import choose, in_mount, is_published

CSS_FILE_URL_NAME = "ui-css-file"
MAX_AGE = 31536000  # a year: the URL holds the ETag


def css_files(selection: Selection | None = None) -> list[tuple[str, str, str]]:
    """``(name, etag, css)`` of the CSS items effective on `selection`
    (default: the active one), in order, then by name. An item without a
    string name or text is left out."""
    found = []
    for _pk, etag, content in effective_rows(UI_CSS_TYPE_URI, selection):
        name, text = content.get("name"), content.get("css")
        if isinstance(name, str) and isinstance(text, str):
            order = content.get("order")
            found.append((order if isinstance(order, int) else 0, name, etag, text))
    found.sort(key=lambda item: item[:2])
    return [(name, etag, text) for _order, name, etag, text in found]


def css_urls(request) -> list[str]:
    """The URLs of the CSS files the page for `request` links, under its
    prefix when it has one."""
    return [
        in_mount(request, reverse(CSS_FILE_URL_NAME, args=[name, etag]))
        for name, etag, _text in css_files()
    ]


class UICssFileView(APIView):
    """GET a CSS item's text, by name and ETag. Anyone who can see a page may
    fetch its CSS, so there is no permission of its own."""

    permission_classes = [AllowAny]
    http_method_names = ["get", "head", "options"]

    def get(self, request, name: str, etag: str):
        check_mount(request)
        chosen = choose(request)
        text = next(
            (
                text
                for found, found_etag, text in css_files(chosen.selection)
                if found == name and found_etag == etag
            ),
            None,
        )
        if text is None:
            raise Http404()
        response = HttpResponse(text, content_type="text/css; charset=utf-8")
        scope = "public" if is_published(chosen, request) else "private"
        response["Cache-Control"] = f"{scope}, max-age={MAX_AGE}, immutable"
        response["X-Content-Type-Options"] = "nosniff"
        return response


__all__ = ["UICssFileView", "css_files", "css_urls"]
