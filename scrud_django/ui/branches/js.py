"""JS items: the JavaScript a page runs, per branch, served at versioned URLs
(``<api or prefix>ui-js-files/<name>/<etag>.js``) and linked by the page after
``scrud-ui.js``. The shared logic is in `scrud_django.ui.branches.files`."""

from scrud_django.ui.branches.file_urls import (  # noqa: F401
    JS_FILE_URL_NAME,
    js_files,
    js_urls,
)
from scrud_django.ui.branches.files import ItemFileView


class UIJsFileView(ItemFileView):
    """GET a JS item's text, by name and ETag."""

    content_type = "text/javascript; charset=utf-8"

    def files(self, selection):
        return js_files(selection)


__all__ = ["JS_FILE_URL_NAME", "UIJsFileView", "js_files", "js_urls"]
