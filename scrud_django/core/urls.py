"""Links without DRF: the absolute URL of a named route."""

from scrud_django import reverse


def absolute_reverse(name, *, args=(), kwargs=None, request=None):
    """The URL of the route `name`: absolute when given a `request`, as DRF's
    ``reverse`` gives it with no versioning scheme (scrud configures none),
    the request's ``?format=`` override kept (`scrud_django.reverse`)."""
    return reverse(name, args=args or None, kwargs=kwargs, request=request)
