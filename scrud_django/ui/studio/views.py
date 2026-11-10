"""The studio's views. Every one requires an authenticated user."""

from django.http import Http404
from django.shortcuts import render
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from scrud_django.models import ResourceType
from scrud_django.rows import by_pk, unsorted


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
