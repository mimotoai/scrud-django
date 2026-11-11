"""CSS items: the text of a page's CSS, per branch, served at versioned URLs
(``<api or prefix>ui-css-files/<name>/<etag>.css``) and linked by the page.
The shared logic is in `scrud_django.ui.branches.files`."""

from scrud_django.ui.branches import UI_CSS_TYPE_URI
from scrud_django.ui.branches.effective import Selection
from scrud_django.ui.branches.files import ItemFileView, file_items, file_urls
from scrud_django.ui.branches.middleware import CSS_FILE_URL_NAME


def css_files(selection: Selection | None = None) -> list[tuple[str, str, str]]:
    """``(name, etag, css)`` of the CSS items effective on `selection`
    (default: the active one), in order, then by name."""
    return file_items(UI_CSS_TYPE_URI, "css", selection)


def css_urls(request) -> list[str]:
    """The URLs of the CSS files the page for `request` links, under its
    prefix when it has one."""
    return file_urls(request, CSS_FILE_URL_NAME, css_files())


class UICssFileView(ItemFileView):
    """GET a CSS item's text, by name and ETag."""

    content_type = "text/css; charset=utf-8"

    def files(self, selection):
        return css_files(selection)


__all__ = ["CSS_FILE_URL_NAME", "UICssFileView", "css_files", "css_urls"]
