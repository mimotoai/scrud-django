"""The CSS and JS items' files, served without DRF: `file_response` (the
answer to a GET of one, by name and ETag) and the UI app's
views of them (`css_file`, `js_file`, routed by `scrud_django.ui.urls`).

The URL holds the item's ETag, so a URL is immutable (a year's caching) and a
stale one is a 404. Presentation is read with system rights (ruling R12):
what gates a file is the branch the request selects (`choose`, which refuses
a working branch the viewer may not GET) and, under a prefix, GET on the
prefix's branch."""

from django.http import Http404, HttpResponse

from scrud_django.core.errors import error_answer
from scrud_django.ui.branches.effective import safe_mode_requested, using_safe_mode
from scrud_django.ui.branches.file_urls import css_files, js_files
from scrud_django.ui.branches.selection import check_mount_access, choose
from scrud_django.ui.studio.answers import json_response

MAX_AGE = 31536000  # a year: the URL holds the ETag

CSS_CONTENT_TYPE = "text/css; charset=utf-8"
JS_CONTENT_TYPE = "text/javascript; charset=utf-8"

#: The methods a file's URL serves.
FILE_METHODS = ("GET", "HEAD")


def file_response(request, files, name: str, etag: str, content_type: str):
    """The answer to a GET of the item file `name` at `etag`: its text, as
    `content_type`, privately cacheable for a year, where `files(selection)`
    gives the effective ``(name, etag, text)`` of the branch selection the
    request browses (`choose`), in safe mode when it asks for it (the
    layer's items only). ``Http404`` for no such file (or a stale ETag).
    Anyone who may see a page may fetch its files: the mount's check is the
    caller's."""
    chosen = choose(request)
    with using_safe_mode(safe_mode_requested(request)):
        found = files(chosen.selection)
    text = next(
        (
            text
            for found_name, found_etag, text in found
            if found_name == name and found_etag == etag
        ),
        None,
    )
    if text is None:
        raise Http404()
    response = HttpResponse(text, content_type=content_type)
    # Never public: a mount's files are gated by GET on its branch, which a
    # shared cache would not check.
    response["Cache-Control"] = f"private, max-age={MAX_AGE}, immutable"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def css_file(request, name: str, etag: str):
    """The UI's view of a CSS item's file, by name and ETag."""
    return _served(request, css_files, name, etag, CSS_CONTENT_TYPE)


def js_file(request, name: str, etag: str):
    """The UI's view of a JS item's file, by name and ETag."""
    return _served(request, js_files, name, etag, JS_CONTENT_TYPE)


def _served(request, files, name, etag, content_type):
    """`file_response` for a GET or HEAD, under a prefix only to a user who
    may GET its branch (`check_mount_access`: else refused as the API
    refuses it, 401 for the anonymous user, else 403); any other method is
    a 405 naming the `FILE_METHODS` in ``Allow``, for anyone, as the API's
    file view answers one (it asks no grant). A refusal is answered as the
    API answers it (`error_answer`: problem details, written as the API
    writes JSON: `json_response`)."""
    if request.method not in FILE_METHODS:
        response = HttpResponse(status=405)
        del response["Content-Type"]
        response["Allow"] = ", ".join(FILE_METHODS)
        return response
    try:
        check_mount_access(request)
        return file_response(request, files, name, etag, content_type)
    except Exception as exc:
        answered = error_answer(exc, request)
        if answered is None:
            raise
        status, body, headers = answered
        return json_response(body, status=status, headers=dict(headers))
