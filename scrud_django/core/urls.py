"""Links without DRF: the absolute URL of a named route, and where each
type's API routes are (`note_api_routes`, `api_route_name`, `api_reverse`)."""

import threading
from collections.abc import Iterable
from typing import Literal

from django.urls import NoReverseMatch
from django.urls import reverse as path_reverse

from scrud_django import reverse

# The slugs of the types whose API routes a front end built, by the URL
# namespace it named them in (``None``: none, as DRF's registration names
# them), in the order they were noted.
_api_routes: dict[str | None, dict[str, None]] = {}
_api_routes_lock = threading.Lock()

#: The kinds of a type's API routes: its list (``<slug>-list``) and its
#: items' (``<slug>-detail``).
ROUTE_KINDS = ("list", "detail")


def note_api_routes(slugs: Iterable[str], *, namespace: str | None) -> None:
    """Note that a front end built the API routes of the types `slugs`,
    named ``<slug>-list`` and ``<slug>-detail`` in the URL namespace
    `namespace` (``None``: outside any, as DRF's registration names them;
    scrud's ninja ``api()``: its ``urls_namespace``), so that
    `api_route_name` looks for a type's routes there too. The URLconf
    serving a request decides which of them it routes."""
    slugs = list(slugs)
    with _api_routes_lock:
        noted = _api_routes.setdefault(namespace, {})
        for slug in slugs:
            noted[slug] = None


def api_namespaces() -> tuple[str | None, ...]:
    """The URL namespaces a front end named a type's API routes in
    (`note_api_routes`), in the order they were first noted (``None``:
    outside any)."""
    with _api_routes_lock:
        return tuple(_api_routes)


#: `api_slugs`' default: the slugs noted in any namespace.
_ANY_NAMESPACE = object()


def api_slugs(*, namespace=_ANY_NAMESPACE) -> tuple[str, ...]:
    """The slugs of the types whose API routes a front end built
    (`note_api_routes`), each once, in the order they were first noted: in
    any namespace, or, given a `namespace`, in that one alone (``None``:
    outside any, as DRF's registration names them)."""
    with _api_routes_lock:
        if namespace is not _ANY_NAMESPACE:
            return tuple(_api_routes.get(namespace, ()))
        found = {}
        for noted in _api_routes.values():
            found.update(noted)
        return tuple(found)


def api_route_name(
    slug: str, kind: Literal["list", "detail"], *, request=None
) -> str | None:
    """The name of the API route of the type `slug` of `kind` (``"list"``:
    ``<slug>-list``; ``"detail"``: ``<slug>-detail``, reversed with one
    key) in the URLconf serving the request: the bare name when it
    reverses (DRF's registration, or a route named so by hand), else the
    first noted namespace's (`api_namespaces`: scrud's ninja ``api()``'s
    ``"<namespace>:<slug>-<kind>"``) that does; ``None`` when none does.
    Given a `request` an API serves (`serving_api_namespace`: resolved in a
    noted namespace, DRF's included, outside any), that API's route alone is
    looked for (R5): an answer it computes links to its own routes, never
    another API's (``None`` when it has none). A request no API serves (a
    UI page's, the studio's, or one not resolved) looks as without one.
    A name found is only where the route is: whether its view is one scrud
    built is its mark's to say (`scrud_django.core.types.route_authorization`).
    A namespace an outer ``include`` nests the API's in is not looked in.
    ``ValueError`` for another `kind`."""
    if kind not in ROUTE_KINDS:
        raise ValueError(f"api_route_name: {kind!r} is not one of {ROUTE_KINDS}.")
    name = f"{slug}-{kind}"
    args = ("key",) if kind == "detail" else ()
    serving = serving_api_namespace(request)
    if serving is not _NO_API:
        qualified = name if serving is None else f"{serving}:{name}"
        return qualified if _reverses(qualified, args) else None
    if _reverses(name, args):
        return name
    # Reversing imported the URLconf, which builds (and notes) its APIs:
    # the namespaces are read after it.
    for namespace in api_namespaces():
        if namespace and _reverses(f"{namespace}:{name}", args):
            return f"{namespace}:{name}"
    return None


#: What `serving_api_namespace` answers for a request no API serves.
_NO_API = object()


def serving_api_namespace(request):
    """The noted API namespace (`api_namespaces`) `request` was resolved in:
    a ninja ``api()``'s ``urls_namespace``, or ``None`` for a route outside
    any namespace once DRF's registration noted that (``None``) one;
    `_NO_API` for a request resolved elsewhere (the UI's ``scrud_ui``, the
    studio's ``scrud-studio``, a namespace no API noted) or not resolved."""
    match = getattr(request, "resolver_match", None)
    if match is None:
        return _NO_API
    namespace = getattr(match, "namespace", "") or None
    return namespace if namespace in api_namespaces() else _NO_API


def _reverses(name, args) -> bool:
    try:
        path_reverse(name, args=args)
    except NoReverseMatch:
        return False
    return True


def api_reverse(
    slug: str, kind: Literal["list", "detail"], *, args=(), kwargs=None, request=None
) -> str:
    """The URL path of the API route of the type `slug` of `kind`
    (`api_route_name`, the API serving `request` first), reversed with
    `args` or `kwargs`, as ``reverse`` gives it. ``NoReverseMatch`` when the
    URLconf routes none."""
    name = api_route_name(slug, kind, request=request)
    if name is None:
        raise NoReverseMatch(
            f"The URLconf routes no {kind} of the type {slug!r} "
            f"({slug}-{kind}, in no namespace or a noted API's)."
        )
    return path_reverse(name, args=args or None, kwargs=kwargs)


def absolute_reverse(name, *, args=(), kwargs=None, request=None, namespace=None):
    """The URL of the route `name`: absolute when given a `request`, as DRF's
    ``reverse`` gives it (`scrud_django.reverse`, which mirrors it): through
    the request's versioning scheme when it has one (a DRF request of a
    versioned view), the request's ``?format=`` override kept.

    A request resolved under a URL namespace (a ninja API's
    ``urls_namespace``) reverses `name` within it first, so links point at the
    API that served the request; a name the namespace lacks, or a request
    without a namespace, reverses globally. `namespace` is the namespace to
    reverse within first when there is no request resolved under one: a link
    an API describes without a request (scrudful's HTTP Schema Resource,
    built in its describe mode) points at that API's routes.

    A name neither reverses within is reversed within each namespace an
    API's routes are named in (`api_namespaces`), in turn, unless an API
    serves the request (`serving_api_namespace`, DRF's routes outside any
    namespace included) or the `namespace` given is a noted one
    (``NoReverseMatch`` then: a link always points at the API that served
    the request, never into another): a UI page (resolved in the UI's
    namespace) or a request with no route links to the API that routes the
    name, a ninja ``api()``'s in a ninja-only project. A request resolved
    outside any namespace counts as DRF's API once DRF's registration noted
    it (``None``), so an app's own un-namespaced view that is no API gets no
    fallback either: a name only a ninja API routes raises there."""
    match = getattr(request, "resolver_match", None)
    namespace = getattr(match, "namespace", "") or namespace
    if namespace and ":" not in name:
        try:
            return reverse(
                f"{namespace}:{name}", args=args or None, kwargs=kwargs, request=request
            )
        except NoReverseMatch:
            pass
    try:
        return reverse(name, args=args or None, kwargs=kwargs, request=request)
    except NoReverseMatch as error:
        namespaces = api_namespaces()
        if (
            ":" in name
            or (namespace and namespace in namespaces)
            or serving_api_namespace(request) is not _NO_API
        ):
            raise
        for api_namespace in namespaces:
            if not api_namespace:
                continue
            try:
                return reverse(
                    f"{api_namespace}:{name}",
                    args=args or None,
                    kwargs=kwargs,
                    request=request,
                )
            except NoReverseMatch:
                pass
        raise error


def first_absolute_reverse(
    names, *, args=(), kwargs=None, request=None, namespace=None
):
    """`absolute_reverse` of the first of `names` the serving namespace (the
    request's, else `namespace`) has, else of the first reversible globally:
    a route two front ends name differently (DRF's ``role-list``,
    scoped-rbac's ninja ``scoped-rbac-role-list``) points at the API that
    served the request. ``NoReverseMatch`` when none reverses (or there is
    none); a bare string (one name, not a sequence of them) is a
    ``TypeError``."""
    if isinstance(names, str):
        raise TypeError(
            f"first_absolute_reverse takes a sequence of names, not {names!r}."
        )
    names = tuple(names)
    match = getattr(request, "resolver_match", None)
    serving = getattr(match, "namespace", "") or namespace
    candidates = [f"{serving}:{name}" for name in names] if serving else []
    candidates.extend(names)
    error = NoReverseMatch(f"None of {names!r} is a route.")
    for candidate in candidates:
        try:
            return reverse(candidate, args=args or None, kwargs=kwargs, request=request)
        except NoReverseMatch as exc:
            error = exc
    raise error
