"""DRF's content negotiation on the ninja front end: each route offers the
media types of the renderers its DRF view negotiates among, in their order
(`Renderer`), and answers DRF's 406 (an ``Accept`` none of them matches) or
404 (a format override, ``?format=``, none of them has) before anything else
of the route, as DRF's ``initial()`` negotiates first. The renderer it
selects (`negotiated`) decides whether a document is served as its own media
type (``routers._as_media_type``), and the media type it accepted (DRF's
``accepted_media_type``) whether JSON is indented (`IndentingJSONRenderer`).
Only the choice is DRF's: a route that negotiates ``text/html`` (the
browsable API's, or ``scrud_django.ui``'s) is answered as it is without one,
never as HTML."""

import contextlib
import functools
import inspect
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from django.conf import settings
from django.utils.http import parse_header_parameters
from django.utils.translation import gettext
from scoped_rbac import access, problems
from scoped_rbac.ninja import DRFStyleJSONRenderer
from scrudful._hsr import is_preflight

from scrud_django import ui
from scrud_django.ninja import entry
from scrud_django.ninja.errors import PROBLEM_JSON, uniform_errors
from scrud_django.registration import DOCUMENT_MEDIA_TYPES

#: The request attribute holding the media type of the renderer negotiated
#: for the request (`negotiated`).
NEGOTIATED_ATTR = "_scrud_ninja_negotiated"

#: The request attribute holding the media type the request accepted, as
#: DRF's ``DefaultContentNegotiation`` gives it (its ``accepted_media_type``),
#: when the renderer negotiated is one of DRF's ``JSONRenderer``s
#: (`JSON_RENDERERS`), whose ``indent`` parameter they read.
ACCEPTED_ATTR = "_scrud_ninja_accepted"

#: The largest indent DRF's ``JSONRenderer.get_indent`` gives.
MAX_INDENT = 8

#: The separators DRF's ``JSONRenderer`` writes indented JSON with.
INDENT_SEPARATORS = (",", ": ")

#: DRF's ``DefaultContentNegotiation`` limits on the ``Accept`` it reads.
MAX_ACCEPT_HEADER_LENGTH = 8192
MAX_ACCEPT_TOKENS = 64
MAX_MEDIA_TYPE_LENGTH = 256

#: The ``detail`` of DRF's ``NotAcceptable``.
NOT_ACCEPTABLE_DETAIL = "Could not satisfy the request Accept header."


@dataclass(frozen=True)
class Renderer:
    """A renderer of a DRF view, as content negotiation sees it: its media
    type and its format (``?format=``)."""

    media_type: str
    format: str


#: DRF's default renderers (``JSONRenderer``, ``BrowsableAPIRenderer``),
#: ``scrud_django.ui``'s HTML renderer, and the documents' renderers
#: (``scrud_django.renderers``).
JSON = Renderer("application/json", "json")
BROWSABLE_API = Renderer("text/html", "api")
SCRUD_HTML = Renderer("text/html", "html")
_DOCUMENT_FORMATS = {
    "application/schema+json": "schema-json",
    "application/ld+json": "ld-json",
}
#: The documents' renderers, by media type: one for each of
#: ``registration.DOCUMENT_MEDIA_TYPES``.
DOCUMENT_RENDERERS = {
    media_type: Renderer(media_type, _DOCUMENT_FORMATS[media_type])
    for media_type in sorted(DOCUMENT_MEDIA_TYPES)
}

#: The renderers that are DRF's ``JSONRenderer`` or a subclass of it
#: (``scrud_django.renderers``), which indent as the accepted media type asks.
JSON_RENDERERS = frozenset({JSON, *DOCUMENT_RENDERERS.values()})

#: The methods whose answer is the document, on a route serving one
#: (``DocumentMediaTypeMixin.serves_document``).
DOCUMENT_METHODS = frozenset({"GET", "HEAD"})  # not routers' (adds PUT)


class NotAcceptable(Exception):
    """No renderer offered matches the request's ``Accept`` (DRF's 406)."""


class UnknownFormat(Exception):
    """No renderer offered has the request's format override (DRF's 404)."""


def resource_renderers(document_media_type=None, document_methods=DOCUMENT_METHODS):
    """The renderers of a scrud resource view (``ResourceViewMixin``), a
    function of the request: its document's first for a method in
    `document_methods` (``DocumentResourceViewSet``: its detail route's GET,
    HEAD and PUT), then DRF's JSON, ``scrud_django.ui``'s HTML when it is
    installed (``ResourceViewMixin.get_renderers``), and the browsable API."""

    def renderers(request):
        offered = [JSON, *([SCRUD_HTML] if ui.is_enabled() else []), BROWSABLE_API]
        if document_media_type is not None and request.method in document_methods:
            offered.insert(0, DOCUMENT_RENDERERS[document_media_type])
        return offered

    return renderers


def api_view_renderers(document_media_type=None):
    """The renderers of a plain DRF ``APIView`` (or ``api_view``) with DRF's
    defaults, a function of the request: its document's first for a GET or
    HEAD (``DocumentMediaTypeMixin``), then JSON and the browsable API."""

    def renderers(request):
        offered = [JSON, BROWSABLE_API]
        if document_media_type is not None and request.method in DOCUMENT_METHODS:
            offered.insert(0, DOCUMENT_RENDERERS[document_media_type])
        return offered

    return renderers


def select(request, renderers: Sequence[Renderer]) -> tuple[Renderer, str]:
    """The renderer of `renderers` that DRF's ``DefaultContentNegotiation``
    selects for `request`, and the media type it accepted: of those with the
    format override's format (if the request names one: else
    `UnknownFormat`), the first matching the most specific of the ``Accept``
    media ranges (``*/*`` without one; ``q`` is not weighed, only how
    specific a range is), else `NotAcceptable`. The media type accepted is
    the range matched, or the renderer's own media type with the range's
    parameters when the range is less specific (``*/*; indent=2`` accepts
    ``application/json;indent=2``), as DRF gives it."""
    format_parameter = getattr(settings, "REST_FRAMEWORK", {}).get(
        "URL_FORMAT_OVERRIDE", "format"
    )
    requested = request.GET.get(format_parameter) if format_parameter else None
    if requested:
        renderers = [renderer for renderer in renderers if renderer.format == requested]
        if not renderers:
            raise UnknownFormat(requested)
    accepted = [_MediaRange(token) for token in _accept_tokens(request)]
    for specificity in (3, 2, 1, 0):
        ranges = [media for media in accepted if media.specificity == specificity]
        if not ranges:
            continue
        for renderer in renderers:
            for media in ranges:
                if media.matches(renderer.media_type):
                    return renderer, media.accepted(renderer.media_type)
    raise NotAcceptable()


def _accept_tokens(request):
    """The media ranges of `request`'s ``Accept`` (``*/*`` without one), as
    DRF reads them (``get_accept_list``): cut at a comma past
    `MAX_ACCEPT_HEADER_LENGTH`, empty and over-long ranges skipped, at most
    `MAX_ACCEPT_TOKENS`."""
    header = request.headers.get("accept", "*/*")
    if len(header) > MAX_ACCEPT_HEADER_LENGTH:
        cut = header.rfind(",", 0, MAX_ACCEPT_HEADER_LENGTH)
        header = header[: cut + 1] if cut != -1 else ""
    tokens = []
    for token in header.split(","):
        token = token.strip()
        if not token or len(token) > MAX_MEDIA_TYPE_LENGTH:
            continue
        tokens.append(token)
        if len(tokens) >= MAX_ACCEPT_TOKENS:
            break
    return tokens


class _MediaRange:
    """A media range of an ``Accept``, as DRF's ``_MediaType`` reads it."""

    def __init__(self, token):
        self.orig = token
        try:
            full_type, self.params = parse_header_parameters(token)
        except ValueError:
            full_type, self.params = "", {}
        self.main_type, _, self.sub_type = full_type.partition("/")

    @property
    def specificity(self):
        """DRF's precedence: 3 for a type with parameters (``q`` aside), 2
        without, 1 for ``type/*``, 0 for ``*/*``."""
        if self.main_type == "*":
            return 0
        if self.sub_type == "*":
            return 1
        if not self.params or list(self.params) == ["q"]:
            return 2
        return 3

    def matches(self, media_type):
        """Whether the range takes `media_type` (a renderer's, which has no
        parameters)."""
        main_type, _, sub_type = media_type.partition("/")
        return self.sub_type in ("*", sub_type) and self.main_type in ("*", main_type)

    def accepted(self, media_type):
        """The media type accepted when the range takes `media_type` (a
        renderer's, as specific as a range without parameters): the range as
        sent, or `media_type` with its parameters if the range is less
        specific."""
        if self.specificity < 2:
            return ";".join(
                (media_type, *(f"{key}={value}" for key, value in self.params.items()))
            )
        return self.orig


def accepted_indent(request):
    """The indent of JSON written for `request`, as DRF's
    ``JSONRenderer.get_indent`` reads it from the media type accepted
    (`ACCEPTED_ATTR`): its ``indent`` parameter, clamped to 0 to
    `MAX_INDENT`, with 0, a missing or a non-integer one ``None`` (not
    indented)."""
    accepted = getattr(request, ACCEPTED_ATTR, None)
    if not accepted:
        return None
    _, params = parse_header_parameters(accepted)
    with contextlib.suppress(KeyError, ValueError, TypeError):
        return max(min(int(params["indent"]), MAX_INDENT), 0) or None
    return None


class IndentingJSONRenderer(DRFStyleJSONRenderer):
    """scoped-rbac's ``DRFStyleJSONRenderer`` (JSON as DRF's ``JSONRenderer``
    writes it), indented as DRF's indents it when the media type the
    request accepted asks (`accepted_indent`): with DRF's indented
    separators (`INDENT_SEPARATORS`). Not an error answered under a
    document's renderer, which DRF writes with ``JSONRenderer`` and its own
    media type (``DocumentMediaTypeMixin.finalize_response``)."""

    def render(self, request, data, *, response_status):
        indent = accepted_indent(request)
        if not 200 <= response_status < 300 and (
            negotiated(request) in DOCUMENT_RENDERERS
        ):
            indent = None
        if indent is None:
            return super().render(request, data, response_status=response_status)
        text = json.dumps(
            data,
            cls=self.encoder_class,
            **{
                **self.json_dumps_params,
                "indent": indent,
                "separators": INDENT_SEPARATORS,
            },
        )
        return text.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def indent_as_drf(api) -> None:
    """Have `api` indent its JSON as DRF does for an accepted media type
    asking it (`IndentingJSONRenderer`), if its renderer writes JSON as DRF
    does (scoped-rbac's ``DRFStyleJSONRenderer``, which
    ``scrud_django.ninja.errors.install`` sets for ninja's own); a renderer
    of the API's own is left as it is. Call it after ``errors.install``."""
    if type(api.renderer) is DRFStyleJSONRenderer:
        api.renderer = IndentingJSONRenderer()


def negotiated(request):
    """The media type of the renderer negotiated for `request`
    (`negotiate_as_drf`), ``None`` for a request no negotiation ran for."""
    return getattr(request, NEGOTIATED_ATTR, None)


def negotiate_as_drf(
    api,
    offers: Mapping[str, Callable[[object], Sequence[Renderer]]],
    *,
    described: frozenset[str] = frozenset(),
) -> None:
    """Have `api`'s URL patterns named in `offers` negotiate among the
    renderers ``offers[name](request)`` gives, as their DRF views do, first,
    every method alike (`select`): the renderer selected is noted on the
    request (`negotiated`), and the media type accepted when it is a JSON
    renderer (`ACCEPTED_ATTR`, for `IndentingJSONRenderer`, which
    `indent_as_drf` gives `api`); no renderer answers DRF's 406 (``not_acceptable``)
    and an unknown format override DRF's 404 (`_refusal`). Not for a request
    rewritten from an OPTIONS, which the path refuses first, as scrud's DRF
    views do, nor for a CORS preflight of a path named in `described` (a
    scrudful router's, which scrudful answers before DRF negotiates). Their
    HTTP Schema Resource routes are left alone. Call it after
    ``scrud_django.ninja.errors.answer_natively_when_off`` and before
    ``routers.vary_on_accept`` and the ``Allow`` wrappers
    (``routers.as_drf_api_views``, ``services.as_drf_api_view_function``),
    which give its refusals their headers, before ``api.urls`` is read."""
    indent_as_drf(api)
    get_urls = api._get_urls

    @functools.wraps(get_urls)
    def _get_urls():
        patterns = get_urls()
        for pattern in patterns:
            name = getattr(pattern, "name", None)
            if name in offers and entry.HSR_SEGMENT not in str(pattern.pattern).split(
                "/"
            ):
                pattern.callback = _negotiating(
                    api, pattern.callback, offers[name], name in described
                )
        return patterns

    api._get_urls = _get_urls


def _negotiating(api, view, renderers_of, described):
    @functools.wraps(view)
    def negotiating(request, *args, **kwargs):
        if access.checks_allow(request) or (
            described and is_preflight(request.method, request.headers.get)
        ):
            return view(request, *args, **kwargs)
        renderers = renderers_of(request)
        try:
            renderer, accepted = select(request, renderers)
        except NotAcceptable:
            refused = _refusal(
                api,
                request,
                406,
                "not_acceptable",
                gettext(NOT_ACCEPTABLE_DETAIL),
                renderers,
            )
        except UnknownFormat:
            refused = _refusal(
                api,
                request,
                404,
                None,
                access.ObjectNotFound.default_message,
                renderers,
            )
        else:
            setattr(request, NEGOTIATED_ATTR, renderer.media_type)
            if renderer in JSON_RENDERERS:
                setattr(request, ACCEPTED_ATTR, accepted)
            return _problem_as_drf(request, view(request, *args, **kwargs))
        if not described:
            return refused
        raw = inspect.unwrap(view)
        return entry._described_as(
            refused, entry._hsr_link_of(raw, request, args, kwargs)
        )

    return negotiating


def _problem_as_drf(request, response):
    """`response`, written for `request`, not indented if it is problem
    details with the problem-details Links (``problems.PROBLEM_LINKS``), as
    DRF writes those: scoped-rbac's handler gives them, and renders them
    with the view's JSON renderer and its own media type, so with no
    ``indent``. scrud's own errors, which have no such Link
    (``errors.install``), DRF writes with the media type accepted, so
    indented as it asks."""
    if (
        accepted_indent(request) is None
        or response.get("Content-Type", "").split(";")[0].strip() != PROBLEM_JSON
        or f"<{problems.PROBLEM_SCHEMA}>" not in response.get("Link", "")
    ):
        return response
    try:
        body = json.loads(response.content)
    except ValueError:
        return response
    response.content = DRFStyleJSONRenderer().render(
        request, body, response_status=response.status_code
    )
    return response


def _refusal(api, request, status, code, detail, renderers):
    """DRF's answer to a negotiation that failed with `status`: problem
    details (``problems.error_problem``, with their Links) while uniform
    errors are on, else DRF's native ``{"detail": ...}`` as its first
    renderer's media type (DRF renders the refusal with it)."""
    if uniform_errors():
        body = problems.error_problem(status, code=code, detail=detail)
        response = api.create_response(request, body, status=status)
        response["Content-Type"] = PROBLEM_JSON
        response["Link"] = problems.PROBLEM_LINKS
    else:
        response = api.create_response(request, {"detail": detail}, status=status)
        response["Content-Type"] = renderers[0].media_type
    return response
