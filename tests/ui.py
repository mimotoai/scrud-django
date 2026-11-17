"""What the tests of the UI app (`scrud_django.ui.urls`) share: its URLconf
(`UI_URLCONF`: the API under ``/api/``, the UI at ``/``) and the UI URL of an
API path (`ui_url`). Plain helpers, not steps (`tests/steps.py` holds those)."""

import re

HTML = "text/html"

#: A page's CSRF token, masked anew for every response.
CSRF_META = re.compile(r'(<meta name="csrf-token" content=")[^"]*(")')

#: The URLconf serving the API under ``/api/`` and the UI app at ``/``.
UI_URLCONF = "tests.mounted_urls"

API_PREFIX = "/api/"


def ui_url(api_url: str) -> str:
    """The UI URL of `api_url`, a path under `UI_URLCONF`'s API: its leading
    ``/api/`` swapped for ``/`` (``ValueError`` for a path not under it)."""
    if not api_url.startswith(API_PREFIX):
        raise ValueError(f"{api_url!r} is not under {API_PREFIX!r}")
    return "/" + api_url[len(API_PREFIX) :]


def comparable(page: str) -> str:
    """`page` with its per-request values normalised (ruling R3): the CSRF
    token the page carries is masked anew for every response."""
    return CSRF_META.sub(r"\1\2", page)


def page_request(url: str, **headers) -> tuple[str, dict]:
    """The URL and headers to request the HTML of `url`, which a page links:
    an API path (``/api/...``) becomes its UI URL; a UI path, or a path
    under a published branch's prefix (``/workbench/...``), which the UI
    app serves, stays. The UI does not negotiate, so no ``Accept`` is
    needed; one given for an API path is refused (``ValueError``): the API
    path is requested at its UI URL, which answers HTML only."""
    if url.startswith(API_PREFIX):
        if "HTTP_ACCEPT" in headers:
            raise ValueError(
                f"page_request({url!r}): an Accept header for an API path; "
                "its UI URL answers HTML only"
            )
        return ui_url(url), headers
    return url, headers
