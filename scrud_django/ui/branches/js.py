"""JS items: the JavaScript a page runs, per branch, served at versioned URLs
(``<api or prefix>ui-js-files/<name>/<etag>.js``) and linked by the page after ``scrud-ui.js``.

The URL holds the item's ETag, so a URL is immutable (a year's caching) and a
stale one is a 404. Presentation is read with system rights (ruling R12):
what gates a file is the branch the request selects (`choose`, which refuses
a working branch the viewer may not GET) and, under a prefix, `check_mount`.
"""

from django.http import Http404, HttpResponse
from django.urls import reverse
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from scrud_django.ui.branches import UI_JS_TYPE_URI
from scrud_django.ui.branches.effective import Selection, effective_rows
from scrud_django.ui.branches.middleware import check_mount
from scrud_django.ui.branches.selection import choose, in_mount

JS_FILE_URL_NAME = "ui-js-file"
MAX_AGE = 31536000  # a year: the URL holds the ETag


def js_files(selection: Selection | None = None) -> list[tuple[str, str, str]]:
    """``(name, etag, js)`` of the JS items effective on `selection`
    (default: the active one), in order, then by name. An item without a
    string name or text is left out."""
    found = []
    for _pk, etag, content in effective_rows(UI_JS_TYPE_URI, selection):
        name, text = content.get("name"), content.get("js")
        if isinstance(name, str) and isinstance(text, str):
            order = content.get("order")
            found.append((order if isinstance(order, int) else 0, name, etag, text))
    found.sort(key=lambda item: item[:2])
    return [(name, etag, text) for _order, name, etag, text in found]


def js_urls(request) -> list[str]:
    """The URLs of the JS files the page for `request` links, under its
    prefix when it has one."""
    return [
        in_mount(request, reverse(JS_FILE_URL_NAME, args=[name, etag]))
        for name, etag, _text in js_files()
    ]


class UIJsFileView(APIView):
    """GET a JS item's text, by name and ETag. Anyone who can see a page may
    fetch its JS, so there is no permission of its own."""

    permission_classes = [AllowAny]
    http_method_names = ["get", "head", "options"]

    def get(self, request, name: str, etag: str):
        check_mount(request)
        chosen = choose(request)
        text = next(
            (
                text
                for found, found_etag, text in js_files(chosen.selection)
                if found == name and found_etag == etag
            ),
            None,
        )
        if text is None:
            raise Http404()
        response = HttpResponse(text, content_type="text/javascript; charset=utf-8")
        # Never public: a mount's JS is gated by GET on its branch, which a
        # shared cache would not check.
        response["Cache-Control"] = f"private, max-age={MAX_AGE}, immutable"
        response["X-Content-Type-Options"] = "nosniff"
        return response


__all__ = ["UIJsFileView", "js_files", "js_urls"]
