"""Links without DRF: the absolute URL of a named route."""

from django.urls import NoReverseMatch

from scrud_django import reverse


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
    built in its describe mode) points at that API's routes."""
    match = getattr(request, "resolver_match", None)
    namespace = getattr(match, "namespace", "") or namespace
    if namespace and ":" not in name:
        try:
            return reverse(
                f"{namespace}:{name}", args=args or None, kwargs=kwargs, request=request
            )
        except NoReverseMatch:
            pass
    return reverse(name, args=args or None, kwargs=kwargs, request=request)


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
