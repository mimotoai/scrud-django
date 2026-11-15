"""JS items: the JavaScript a page runs, per branch, served at versioned URLs
(``<api or prefix>ui-js-files/<name>/<etag>.js``) and linked by the page after
``scrud-ui.js``. The shared logic is in `scrud_django.ui.branches.files`."""

from scrud_django.ui.branches import UI_JS_TYPE_URI
from scrud_django.ui.branches.effective import Selection
from scrud_django.ui.branches.files import ItemFileView, file_items, file_urls
from scrud_django.ui.branches.middleware import JS_FILE_URL_NAME


def js_files(selection: Selection | None = None) -> list[tuple[str, str, str]]:
    """``(name, etag, js)`` of the JS items effective on `selection`
    (default: the active one), in order, then by name."""
    return file_items(UI_JS_TYPE_URI, "js", selection)


def js_urls(request) -> list[str]:
    """The URLs of the JS files the page for `request` links, under its
    prefix when it has one."""
    return file_urls(request, JS_FILE_URL_NAME, js_files())


class UIJsFileView(ItemFileView):
    """GET a JS item's text, by name and ETag."""

    content_type = "text/javascript; charset=utf-8"

    def files(self, selection):
        return js_files(selection)


__all__ = ["JS_FILE_URL_NAME", "UIJsFileView", "js_files", "js_urls"]
