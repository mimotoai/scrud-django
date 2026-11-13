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
