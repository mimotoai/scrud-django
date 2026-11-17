"""CSS items: the text of a page's CSS, per branch, served at versioned URLs
(``<api or prefix>ui-css-files/<name>/<etag>.css``) and linked by the page.
The shared logic is in `scrud_django.ui.branches.files`."""

from scrud_django.ui.branches.file_urls import (  # noqa: F401
    CSS_FILE_URL_NAME,
    css_files,
    css_urls,
)
from scrud_django.ui.branches.files import ItemFileView


class UICssFileView(ItemFileView):
    """GET a CSS item's text, by name and ETag."""

    content_type = "text/css; charset=utf-8"

    def files(self, selection):
        return css_files(selection)


__all__ = ["CSS_FILE_URL_NAME", "UICssFileView", "css_files", "css_urls"]
