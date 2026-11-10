"""The studio's views. Every one requires an authenticated user."""

from dataclasses import dataclass

from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.clickjacking import xframe_options_sameorigin
from rest_framework.decorators import api_view, permission_classes, renderer_classes
from rest_framework.exceptions import ValidationError
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
from scrud_django.ui.studio.dispatch import call_view
from scrud_django.ui.studio.explain import explain as explain_node
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


def _data_context(request) -> str:
    """The data context ``?context=`` names (default ``""``). Any other than
    ``""`` needs GET on UI stylesheets in it, as seeing a context's
    presentation needs reading its styling (ruling S7): else
    `UIStudioContextUnavailableError`, the same for every context."""
    context = request.GET.get("context", "")
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

    def readable(source):
        if source is None or source.content is None:
            return True
        return may_read_in_context(
            request,
            UI_STYLESHEET_TYPE_URI,
            source.content,
            resource_contexts(source.content),
        )

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
        return Response(explain_node(info, shown.mode, readable=readable))


@dataclass(frozen=True)
class _Shown:
    """What a preview (or its explanation) shows: `content` at `href` on
    `selection`'s branch in `mode` and `scope_chain`, with the stylesheets
    of `contexts` and the user's `decisions` (``None``: the content's own)."""

    content: object
    href: str
    selection: Selection
    mode: str
    scope_chain: tuple[str, ...]
    contexts: list[str] | None
    decisions: dict | None


def _shown(request, slug, resource_type) -> "_Shown | HttpResponse":
    """The `_Shown` of a preview's parameters (see `preview`), or the
    resource view's response refusing to read ``?instance=``."""
    context = _data_context(request)
    value = request.GET.get("branch") or MAIN
    selection = parse_value(value)
    if selection is None or not selectable(request, selection):
        raise UIBranchUnavailableError(value)
    if selection.branch == MAIN:
        selection = Selection()
    mode = request.GET.get("mode") or "view"
    if mode not in ("view", "edit"):
        raise ValidationError({"mode": ["Must be 'view' or 'edit'."]})
    scope_chain = tuple(request.GET.get("scope", "").split())
    instance = request.GET.get("instance") or None
    if instance is None:
        return _Shown(
            content=sample_of(resource_type, context),
            href=reverse("scrud-studio:sample", args=[slug]),
            selection=selection,
            mode=mode,
            scope_chain=scope_chain,
            contexts=[context],
            decisions=SAMPLE_DECISIONS,
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
    )
