"""The studio's views. Every one requires an authenticated user."""

from dataclasses import dataclass

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
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import JSONParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response

from scrud_django.exceptions import (
    UIBranchUnavailableError,
    UIStudioContextUnavailableError,
)
from scrud_django.models import ResourceType
from scrud_django.permissions import may_read_in_context, resource_contexts
from scrud_django.registration import get_resource_type_for
from scrud_django.rows import by_pk, unsorted
from scrud_django.ui.branches import MAIN
from scrud_django.ui.branches.effective import Selection, using_selection
from scrud_django.ui.branches.selection import parse_value, selectable
from scrud_django.ui.cascade import request_memo
from scrud_django.ui.render import node_at
from scrud_django.ui.studio import writes
from scrud_django.ui.studio.dispatch import call_view
from scrud_django.ui.studio.explain import explain as explain_node
from scrud_django.ui.studio.new_component import create as create_component
from scrud_django.ui.studio.new_component import options as component_options
from scrud_django.ui.studio.preview import (
    SAMPLE_DECISIONS,
    preview_css_urls,
    render_preview,
    sample_of,
)
from scrud_django.ui.types import UI_STYLESHEET_TYPE_URI


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
def types(request):
    """The type browser, filtered by ``?q=``. An htmx request (``HX-Request``)
    gets the list alone."""
    q = request.GET.get("q", "")
    context = {"types": registered_types(q), "q": q}
    template = "scrud_studio/types.html"
    if request.headers.get("HX-Request"):
        template = "scrud_studio/_type_list.html"
    return render(request._request, template, context)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def type_page(request, slug):
    """The studio page of one type (a placeholder shell until its editor)."""
    found = [t for t in registered_types() if t["slug"] == slug]
    if not found:
        raise Http404(slug)
    return render(request._request, "scrud_studio/type.html", {"type": found[0]})


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
    if context and not may_read_in_context(
        request, UI_STYLESHEET_TYPE_URI, {"rbac_context": context}, [context]
    ):
        raise UIStudioContextUnavailableError()
    return context


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@renderer_classes([JSONRenderer])
def sample(request, slug):
    """The sample instance of type `slug` as JSON, in the data context
    ``?context=`` (its ``rbac_context``; default ``""``; `_data_context`)."""
    resource_type = get_resource_type_for(slug)
    return Response(sample_of(resource_type, _data_context(request)))


@xframe_options_sameorigin
@api_view(["GET"])
@permission_classes([IsAuthenticated])
@renderer_classes([JSONRenderer])
def preview(request, slug):
    """A page for the studio's preview iframe: type `slug`'s sample, or with
    ``?instance=<pk>`` that instance (read through its resource view with
    the user's rights: a refused read answers as the view does), rendered in
    preview mode (`render_preview`) on the branch ``?branch=`` (a selection
    value; default ``main``), in ``?mode=`` (``view``, the default, or
    ``edit``) and the scope chain ``?scope=`` (space-separated), with the
    branch's CSS. The sample is in the data context ``?context=`` (default
    ``""``; `_data_context`, checked with or without an instance), shown as editable and not deletable; an instance is in its
    own contexts, with the user's decisions on it. A branch that is not
    `selectable` (unknown, unreadable, a broken chain, a missing pinned
    snapshot, not a selection value) is refused alike:
    `UIBranchUnavailableError` (400)."""
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


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@renderer_classes([JSONRenderer])
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
    stylesheet), as the list filter checks a stored one; the default
    stylesheet's are readable."""

    def readable(source):
        if source is None or source.content is None:
            return True
        return may_read_in_context(
            request,
            UI_STYLESHEET_TYPE_URI,
            source.content,
            resource_contexts(source.content),
        )

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
def save_item(request):
    """Save an item on a working branch (`writes.save_item`)."""
    return writes.save_item(request, _body(request))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@parser_classes([JSONParser])
@renderer_classes([JSONRenderer])
def revert_item(request):
    """Drop a working branch's own row of an item (`writes.revert_item`)."""
    return writes.revert_item(request, _body(request))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@parser_classes([JSONParser])
@renderer_classes([JSONRenderer])
def pull(request):
    """Pull a working branch (`writes.pull`)."""
    return writes.pull(request, _body(request))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@parser_classes([JSONParser])
@renderer_classes([JSONRenderer])
def push(request):
    """Push a working branch, to its parent by default (`writes.push`)."""
    return writes.push(request, _body(request))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@renderer_classes([JSONRenderer])
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
