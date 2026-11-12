"""The studio's views. Every one requires an authenticated user."""

from dataclasses import dataclass
from functools import wraps

from django.core.handlers.exception import response_for_exception
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.clickjacking import xframe_options_sameorigin
from rest_framework.decorators import (
    api_view,
    parser_classes,
    permission_classes,
    renderer_classes,
)
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.parsers import FormParser, JSONParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response

from scrud_django.exceptions import (
    UIBranchUnavailableError,
    UIStudioContextUnavailableError,
    drf_errors,
)
from scrud_django.models import ResourceType
from scrud_django.registration import get_resource_type_for
from scrud_django.rows import by_pk, unsorted
from scrud_django.ui.branches import MAIN
from scrud_django.ui.branches.effective import Selection, using_selection
from scrud_django.ui.branches.model import Branch
from scrud_django.ui.branches.reads import may_read_item
from scrud_django.ui.branches.selection import parse_value, selectable
from scrud_django.ui.branches.store import branches
from scrud_django.ui.cascade import request_memo
from scrud_django.ui.render import node_at
from scrud_django.ui.studio import writes
from scrud_django.ui.studio.dispatch import call_view
from scrud_django.ui.studio.explain import explain as explain_node
from scrud_django.ui.studio.live_preview import (
    live_preview_csp,
    new_nonce,
    parse_request,
    render_live_preview,
)
from scrud_django.ui.studio.new_component import create as create_component
from scrud_django.ui.studio.new_component import options as component_options
from scrud_django.ui.studio.page import (
    may_preview_context,
    page_context,
    read_item,
    strip_context,
)
from scrud_django.ui.studio.preview import (
    SAMPLE_DECISIONS,
    preview_css_urls,
    render_preview,
    sample_of,
)


def registered_types(q: str = "") -> list[dict]:
    """Every registered type with a slug (slug, title, type URI), ordered by
    slug, whose slug, title or URI contains `q` (case-insensitive). A type's
    title is its schema's ``title``, else its slug. Every user sees every
    type: reading a type's items is checked where they are read."""
    rows = by_pk(
        unsorted(
            ResourceType.objects.exclude(slug__isnull=True).values_list(
                "pk", "slug", "type_uri", "schema__content"
            )
        )
    )
    found = []
    needle = q.strip().lower()
    for _, slug, uri, schema in rows:
        title = schema.get("title") if isinstance(schema, dict) else None
        title = title if isinstance(title, str) and title else slug
        if needle and not any(needle in v.lower() for v in (slug, title, uri)):
            continue
        found.append({"slug": slug, "title": title, "type_uri": uri})
    return sorted(found, key=lambda t: t["slug"])


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@drf_errors
def types(request):
    """The type browser, filtered by ``?q=``. An htmx request (``HX-Request``)
    gets the list alone."""
    q = request.GET.get("q", "")
    context = {"types": registered_types(q), "q": q}
    template = "scrud_studio/types.html"
    if request.headers.get("HX-Request"):
        template = "scrud_studio/_type_list.html"
    return render(request._request, template, context)


def _registered_type(slug) -> dict:
    found = [t for t in registered_types() if t["slug"] == slug]
    if not found:
        raise Http404(slug)
    return found[0]


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@drf_errors
def type_page(request, slug):
    """The studio page of type `slug`: the branch picker and preview
    controls (`page.page_context`, from the query), the sandboxed preview,
    the inspector and editor that ``studio.js`` fills, and the branch
    strip."""
    context = {"type": _registered_type(slug), **page_context(request, slug)}
    return render(request._request, "scrud_studio/type.html", context)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@drf_errors
def strip(request, slug):
    """The branch strip of ``?branch=`` (a branch name; default ``main``)
    on the page of type `slug`, alone: the page refreshes it after a save.
    A branch the user may not select is refused as an unknown one
    (`UIBranchUnavailableError`)."""
    found = _registered_type(slug)
    branch = _selected_branch(request)
    all_branches = branches()
    context = {
        "type": found,
        **strip_context(
            request, slug, branch, Selection(branch=branch.name), all_branches
        ),
    }
    return render(request._request, "scrud_studio/_strip.html", context)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@renderer_classes([JSONRenderer])
@drf_errors
def item(request):
    """The item ``{kind, name, applies_to?}`` (query parameters) as the
    branch ``?branch=`` (a branch name; default ``main``) shows it live, for
    the editor (`page.read_item`), with its ETag. 404 when the branch shows
    none or one the user may not read, alike; a branch they may not select
    is refused as an unknown one (`UIBranchUnavailableError`)."""
    kind, name, target = writes._item(request.GET)
    branch = _selected_branch(request)
    found = read_item(request, branch, kind, name, target)
    if found is None:
        raise NotFound(f"Branch {branch.name!r} shows no such item you may read.")
    headers = {"ETag": found["etag"]} if found["etag"] else None
    return Response(found, headers=headers)


def _selected_branch(request) -> Branch:
    """The branch ``?branch=`` names (default ``main``), which the user may
    select; else `UIBranchUnavailableError`."""
    name = request.GET.get("branch") or MAIN
    all_branches = branches()
    branch = all_branches.get(name)
    if branch is None or not selectable(
        request, Selection(branch=name), all_branches=all_branches
    ):
        raise UIBranchUnavailableError(name)
    return branch


def _schema_title(resource_type) -> str:
    schema = getattr(resource_type.schema, "content", None)
    title = schema.get("title") if isinstance(schema, dict) else None
    return title if isinstance(title, str) and title else resource_type.slug


def _data_context(request, params=None) -> str:
    """The data context ``?context=`` names (default ``""``; `params`
    instead of the query when given). Any other than ``""`` needs GET on UI
    stylesheets in it, as seeing a context's presentation needs reading its
    styling (ruling S7): else `UIStudioContextUnavailableError`, the same
    for every context."""
    params = request.GET if params is None else params
    context = params.get("context", "")
    if not may_preview_context(request, context):
        raise UIStudioContextUnavailableError()
    return context


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@renderer_classes([JSONRenderer])
@drf_errors
def sample(request, slug):
    """The sample instance of type `slug` as JSON, in the data context
    ``?context=`` (its ``rbac_context``; default ``""``; `_data_context`)."""
    resource_type = get_resource_type_for(slug)
    return Response(sample_of(resource_type, _data_context(request)))


# The CSP of every preview response (ruling S15): opened outside the
# studio's iframe, a preview is still sandboxed without scripts. Its
# document stays same-origin, so the studio page can listen in it.
PREVIEW_CSP = "sandbox allow-same-origin"


def _sandboxed(view):
    """`view` with `PREVIEW_CSP` on every response it gives, errors
    included."""

    @wraps(view)
    def sandboxed(*args, **kwargs):
        response = view(*args, **kwargs)
        response["Content-Security-Policy"] = PREVIEW_CSP
        return response

    return sandboxed


@_sandboxed
@xframe_options_sameorigin
@api_view(["GET"])
@permission_classes([IsAuthenticated])
@renderer_classes([JSONRenderer])
@drf_errors
def preview(request, slug):
    """A page for the studio's preview iframe: type `slug`'s sample, or with
    ``?instance=<pk>`` that instance (read through its resource view with
    the user's rights: a refused read answers as the view does), rendered in
    preview mode (`render_preview`) on the branch ``?branch=`` (a selection
    value; default ``main``), in ``?mode=`` (``view``, the default, or
    ``edit``) and the scope chain ``?scope=`` (space-separated), with the
    branch's CSS. The sample is in the data context ``?context=`` (default
    ``""``; `_data_context`, checked with or without an instance), shown as
    editable and not deletable; an instance is in its own contexts, with
    the user's decisions on it. A branch that is not `selectable` (unknown,
    unreadable, a broken chain, a missing pinned snapshot, not a selection
    value) is refused alike: `UIBranchUnavailableError` (400). Every
    response, a refusal too, is framable by the same origin only and
    carries `PREVIEW_CSP`."""
    resource_type = get_resource_type_for(slug)
    shown = _shown(request, slug, resource_type)
    if isinstance(shown, HttpResponse):
        return shown
    frame = render_preview(
        request,
        resource_type,
        shown.content,
        selection=shown.selection,
        mode=shown.mode,
        scope_chain=shown.scope_chain,
        contexts=shown.contexts,
        decisions=shown.decisions,
        href=shown.href,
    )
    return render(
        request._request,
        "scrud_studio/preview.html",
        {
            "title": _schema_title(resource_type),
            "css": preview_css_urls(shown.selection),
            "frame": frame,
        },
    )


# The request attribute holding a live preview response's CSP nonce.
_NONCE_ATTR = "scrud_studio_preview_nonce"


def _live_sandboxed(view):
    """`view` with `live_preview_csp` and ``X-Frame-Options: SAMEORIGIN`` on
    every response it gives, its nonce new with each (on the request as
    `_NONCE_ATTR`). An exception DRF does not answer (a body over
    ``DATA_UPLOAD_MAX_MEMORY_SIZE``, too many form fields, any error) is
    answered here as Django would answer it (``response_for_exception``:
    400 for a suspicious request, else 500, logged and signalled as usual),
    so that its response is sandboxed too."""

    @wraps(view)
    def sandboxed(request, *args, **kwargs):
        nonce = new_nonce()
        setattr(request, _NONCE_ATTR, nonce)
        try:
            response = view(request, *args, **kwargs)
        except Exception as error:
            response = response_for_exception(request, error)
        response["Content-Security-Policy"] = live_preview_csp(nonce)
        response["X-Frame-Options"] = "SAMEORIGIN"
        return response

    return sandboxed


@_live_sandboxed
@xframe_options_sameorigin
@api_view(["POST"])
@permission_classes([IsAuthenticated])
@parser_classes([FormParser])
@renderer_classes([JSONRenderer])
@drf_errors
def live_preview(request):
    """The live preview (`studio.live_preview`): a form-encoded POST, so
    that a ``<form target=...>`` can submit it into a frame with the CSRF
    token, whose one field ``request`` holds the preview's JSON
    (`live_preview.parse_request`: a refused one is
    `UIStudioPreviewInvalidError`). What it shows is read as `preview`
    reads its parameters (`_shown`: the branch must be selectable, the data
    context readable, an instance readable through its view), then
    `render_live_preview` renders it. It writes nothing. Every response,
    errors included, is framable by the same origin only and carries
    `live_preview_csp` with its own nonce."""
    live = parse_request(request.data.get("request"))
    resource_type = get_resource_type_for(live.slug)
    shown = _shown(request, live.slug, resource_type, live.params())
    if isinstance(shown, HttpResponse):
        return shown
    nonce = getattr(request._request, _NONCE_ATTR)
    return render_live_preview(
        request, resource_type, shown, live, nonce, _readable(request)
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@renderer_classes([JSONRenderer])
@drf_errors
def explain(request, slug):
    """The inspector's explanation (`studio.explain.explain`) of the node at
    ``?pointer=`` (a JSON Pointer; default ``""``, the resource) of what
    `preview` shows with the same parameters, as JSON. A pointer to no node
    the preview renders is refused (`UIPointerNotFoundError`, 400). A rule
    from a stylesheet the user may not GET (in its contexts, as the list
    filter checks a stored one) shows its rank parts only; the default
    stylesheet's are readable."""
    resource_type = get_resource_type_for(slug)
    shown = _shown(request, slug, resource_type)
    if isinstance(shown, HttpResponse):
        return shown
    with using_selection(shown.selection), request_memo():
        info = node_at(
            shown.content,
            resource_type=resource_type,
            request=request,
            pointer=request.GET.get("pointer", ""),
            scope_chain=shown.scope_chain,
            mode=shown.mode,
            contexts=shown.contexts,
        )
        return Response(explain_node(info, shown.mode, readable=_readable(request)))


def _readable(request):
    """Whether the user of `request` may read a rule's `RuleSource` (a
    stylesheet), as the list filter checks a stored one (`may_read_item`:
    a packaged layer's is public); the default stylesheet's are readable."""

    def readable(source):
        if source is None or source.content is None:
            return True
        return may_read_item(request, "stylesheet", source.pk, source.content)

    return readable


@dataclass(frozen=True)
class _Shown:
    """What a preview (or its explanation) shows: `content` at `href` on
    `selection`'s branch in `mode` and `scope_chain`, with the stylesheets
    of `contexts` and the user's `decisions` (``None``: the content's own);
    `data_context` is the ``?context=`` it was asked for."""

    content: object
    href: str
    selection: Selection
    mode: str
    scope_chain: tuple[str, ...]
    contexts: list[str] | None
    decisions: dict | None
    data_context: str = ""


def _shown(request, slug, resource_type, params=None) -> "_Shown | HttpResponse":
    """The `_Shown` of a preview's parameters (see `preview`; `params`
    instead of the query when given), or the resource view's response
    refusing to read ``?instance=``."""
    params = request.GET if params is None else params
    context = _data_context(request, params)
    value = params.get("branch") or MAIN
    selection = parse_value(value)
    if selection is None or not selectable(request, selection):
        raise UIBranchUnavailableError(value)
    if selection.branch == MAIN:
        selection = Selection()
    mode = params.get("mode") or "view"
    if mode not in ("view", "edit"):
        raise ValidationError({"mode": ["Must be 'view' or 'edit'."]})
    scope_chain = tuple(params.get("scope", "").split())
    instance = params.get("instance") or None
    if instance is None:
        return _Shown(
            content=sample_of(resource_type, context),
            href=reverse("scrud-studio:sample", args=[slug]),
            selection=selection,
            mode=mode,
            scope_chain=scope_chain,
            contexts=[context],
            decisions=SAMPLE_DECISIONS,
            data_context=context,
        )
    read = call_view(request, slug, "GET", pk=instance)
    if read.status_code != 200:
        return HttpResponse(
            read.content,
            status=read.status_code,
            content_type=read["Content-Type"],
        )
    return _Shown(
        content=read.data,
        href=reverse(f"{slug}-detail", args=[instance]),
        selection=selection,
        mode=mode,
        scope_chain=scope_chain,
        contexts=None,
        decisions=None,
        data_context=context,
    )


def _body(request) -> dict:
    """The JSON object a studio write is sent with."""
    if not isinstance(request.data, dict):
        raise ValidationError({"body": ["A JSON object is required."]})
    return request.data


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@parser_classes([JSONParser])
@renderer_classes([JSONRenderer])
@drf_errors
def save_item(request):
    """Save an item on a working branch (`writes.save_item`)."""
    return writes.save_item(request, _body(request))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@parser_classes([JSONParser])
@renderer_classes([JSONRenderer])
@drf_errors
def revert_item(request):
    """Drop a branch's own row of an item, a published branch's too
    (`writes.revert_item`)."""
    return writes.revert_item(request, _body(request))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@parser_classes([JSONParser])
@renderer_classes([JSONRenderer])
@drf_errors
def pull(request):
    """Pull a working branch (`writes.pull`)."""
    return writes.pull(request, _body(request))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@parser_classes([JSONParser])
@renderer_classes([JSONRenderer])
@drf_errors
def push(request):
    """Push a working branch, to its parent by default (`writes.push`)."""
    return writes.push(request, _body(request))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@renderer_classes([JSONRenderer])
@drf_errors
def new_component_options(request, slug):
    """The options of "new component for this node"
    (`studio.new_component.options`) for the node at ``?pointer=`` (default
    ``""``) of what `preview` shows with the same parameters, as JSON."""
    resource_type = get_resource_type_for(slug)
    shown = _shown(request, slug, resource_type)
    if isinstance(shown, HttpResponse):
        return shown
    return Response(
        component_options(
            request, slug, resource_type, shown, request.GET.get("pointer", "")
        )
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@parser_classes([JSONParser])
@renderer_classes([JSONRenderer])
@drf_errors
def new_component(request):
    """ "New component for this node" (`studio.new_component.create`): the
    body names the working ``branch`` (refused before anything is
    dispatched when it is unknown, not selectable or published), the type
    ``slug``, and what the preview shows (``mode``, ``scope``, ``context``,
    ``instance``, as `preview`'s parameters, on that branch, live)."""
    body = _body(request)
    branch = writes._working_branch(request, body.get("branch"))
    slug = body.get("slug")
    if not isinstance(slug, str) or not slug:
        raise ValidationError({"slug": ["A resource type slug is required."]})
    params = {"branch": branch.name}
    for key in ("mode", "scope", "context", "instance"):
        value = body.get(key)
        if value is None:
            continue
        if key == "instance" and type(value) is int:
            value = str(value)
        if not isinstance(value, str):
            raise ValidationError({key: ["Must be a string."]})
        params[key] = value
    resource_type = get_resource_type_for(slug)
    shown = _shown(request, slug, resource_type, params)
    if isinstance(shown, HttpResponse):
        return shown
    return create_component(
        request, slug, resource_type, branch, shown, body, _readable(request)
    )
