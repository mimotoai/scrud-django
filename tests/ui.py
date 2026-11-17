"""What the tests of the UI app (`scrud_django.ui.urls`) share: its URLconf
(`UI_URLCONF`: the API under ``/api/``, the UI at ``/``) and the UI URL of an
API path (`ui_url`). Plain helpers, not steps (`tests/steps.py` holds those)."""

HTML = "text/html"

#: The URLconf serving the API under ``/api/`` and the UI app at ``/``.
UI_URLCONF = "tests.mounted_urls"

API_PREFIX = "/api/"


def ui_url(api_url: str) -> str:
    """The UI URL of `api_url`, a path under `UI_URLCONF`'s API: its leading
    ``/api/`` swapped for ``/`` (``ValueError`` for a path not under it)."""
    if not api_url.startswith(API_PREFIX):
        raise ValueError(f"{api_url!r} is not under {API_PREFIX!r}")
    return "/" + api_url[len(API_PREFIX) :]


def page_request(url: str, **headers) -> tuple[str, dict]:
    """The URL and headers to request the HTML of `url`, which a page of
    the UI app links (so an API path, ``/api/...``) or a published branch's
    prefix serves (``/workbench/...``): an API path becomes its UI URL, which
    needs no ``Accept``; a prefix path stays, asking for HTML."""
    if url.startswith(API_PREFIX):
        return ui_url(url), headers
    return url, {"HTTP_ACCEPT": HTML, **headers}
