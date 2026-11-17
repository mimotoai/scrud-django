"""What a page needs to link its CSS and JS items: the text of each item per
branch (`file_items`, `css_files`, `js_files`) and their versioned URLs
(`css_urls`, `js_urls`). No DRF: the views that serve the files are in
`scrud_django.ui.branches.files` (and `css`, `js`), which re-export these."""

from django.urls import reverse

from scrud_django.ui.branches import UI_CSS_TYPE_URI, UI_JS_TYPE_URI
from scrud_django.ui.branches.effective import (
    SAFE_PARAMETER,
    Selection,
    effective_rows,
    safe_mode,
)
from scrud_django.ui.branches.selection import in_mount

CSS_FILE_URL_NAME = "ui-css-file"
JS_FILE_URL_NAME = "ui-js-file"


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


def css_files(selection: Selection | None = None) -> list[tuple[str, str, str]]:
    """``(name, etag, css)`` of the CSS items effective on `selection`
    (default: the active one), in order, then by name."""
    return file_items(UI_CSS_TYPE_URI, "css", selection)


def css_urls(request) -> list[str]:
    """The URLs of the CSS files the page for `request` links, under its
    prefix when it has one."""
    return file_urls(request, CSS_FILE_URL_NAME, css_files())


def js_files(selection: Selection | None = None) -> list[tuple[str, str, str]]:
    """``(name, etag, js)`` of the JS items effective on `selection`
    (default: the active one), in order, then by name."""
    return file_items(UI_JS_TYPE_URI, "js", selection)


def js_urls(request) -> list[str]:
    """The URLs of the JS files the page for `request` links, under its
    prefix when it has one."""
    return file_urls(request, JS_FILE_URL_NAME, js_files())
