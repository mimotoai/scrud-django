"""Links without DRF: the absolute URL of a named route."""

from scrud_django import reverse


def absolute_reverse(name, *, args=(), kwargs=None, request=None):
    """The URL of the route `name`: absolute when given a `request`, as DRF's
    ``reverse`` gives it (`scrud_django.reverse`, which mirrors it): through
    the request's versioning scheme when it has one (a DRF request of a
    versioned view), the request's ``?format=`` override kept."""
    return reverse(name, args=args or None, kwargs=kwargs, request=request)
