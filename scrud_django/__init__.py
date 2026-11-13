"""
SCRUD-Django - Semantic Create, Read, Update and Delete Django application.
"""

import functools
import hashlib
import json
from importlib.metadata import PackageNotFoundError, version
from urllib import parse

from django.conf import settings
from django.urls import NoReverseMatch
from django.urls import reverse as django_reverse
from django.utils.encoding import force_str

try:
    __version__ = version("scrud-django")
except PackageNotFoundError:
    # Imported from a source tree that is not installed (e.g. a checkout on
    # PYTHONPATH): there is no distribution metadata to read the version from.
    __version__ = "0+unknown"


def reverse(viewname, args=None, kwargs=None, request=None, format=None, **extra):
    """DRF's ``rest_framework.reverse.reverse``, without importing DRF:
    ``django.urls.reverse``, absolute when given a `request`; through the
    request's versioning scheme when it has one (``request.versioning_scheme``,
    which DRF's views set from ``DEFAULT_VERSIONING_CLASS``), falling back
    to the plain route when the scheme cannot reverse it; and the request's
    format override query parameter (``?format=``) kept."""
    scheme = getattr(request, "versioning_scheme", None)
    if scheme is not None:
        try:
            url = scheme.reverse(viewname, args, kwargs, request, format, **extra)
        except NoReverseMatch:
            # As DRF: a scheme that cannot reverse it falls back to the plain
            # route.
            url = _reverse(viewname, args, kwargs, request, format, **extra)
    else:
        url = _reverse(viewname, args, kwargs, request, format, **extra)
    return _preserve_builtin_query_params(url, request)


def _reverse(viewname, args=None, kwargs=None, request=None, format=None, **extra):
    """DRF's ``_reverse``: ``django.urls.reverse`` (`format` as the route's
    ``format`` keyword), absolute when given a `request`."""
    if format is not None:
        kwargs = kwargs or {}
        kwargs["format"] = format
    url = django_reverse(viewname, args=args, kwargs=kwargs, **extra)
    if request:
        return request.build_absolute_uri(url)
    return url


def _preserve_builtin_query_params(url, request=None):
    """DRF's ``preserve_builtin_query_params``: `url` with `request`'s format
    override query parameter (``REST_FRAMEWORK["URL_FORMAT_OVERRIDE"]``,
    ``format`` by default) set to the request's value, the query's
    parameters then sorted (DRF's ``replace_query_param``)."""
    if request is None:
        return url
    param = getattr(settings, "REST_FRAMEWORK", {}).get("URL_FORMAT_OVERRIDE", "format")
    if param and param in request.GET:
        scheme, netloc, path, query, fragment = parse.urlsplit(force_str(url))
        params = parse.parse_qs(query, keep_blank_values=True)
        params[force_str(param)] = [force_str(request.GET[param])]
        query = parse.urlencode(sorted(params.items()), doseq=True)
        url = parse.urlunsplit((scheme, netloc, path, query, fragment))
    return url


class ScrudServices:
    """The service catalog (``services/``): type URI -> a function of the
    request giving the URL of its list, within the URL namespace the request
    was resolved under first (``core.urls.absolute_reverse``: on a ninja
    API, its own routes). Its validator is `etag_for`, a hash of the
    catalog as served, the same in every process that registered the same
    services; there is no modification time (entries are added by code at
    startup, which records none that every process shares)."""

    services: dict

    def __init__(self):
        self.services = {}

    def add_service(self, type_uri, value):
        """Register `type_uri`'s list: `value` is a route name prefix (its
        ``-list`` route), a sequence of them (the first the serving API
        names, `core.urls.first_absolute_reverse`: one per front end
        naming the route differently), or a function ``f(request=None)``
        returning a URL."""
        if callable(value):
            self.services[type_uri] = value
        elif isinstance(value, str):
            self.services[type_uri] = functools.partial(_list_url, (value,))
        else:
            self.services[type_uri] = functools.partial(_list_url, tuple(value))

    def catalog(self, request=None):
        """The catalog as served to `request`: type URI -> list URL."""
        return {
            type_uri: url_func(request=request)
            for type_uri, url_func in self.services.items()
        }

    def etag_for(self, request=None):
        """The ETag of `catalog` for `request` (`etag_of`)."""
        return self.etag_of(self.catalog(request))

    @staticmethod
    def etag_of(catalog):
        """The ETag of the served `catalog`: a hash of its content."""
        content = json.dumps(catalog, sort_keys=True)
        return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _list_url(prefixes, request=None):
    """The URL of the list route of the first of `prefixes` (route name
    prefixes) the serving API names, else of the first reversible."""
    from scrud_django.core.urls import first_absolute_reverse

    return first_absolute_reverse(
        [f"{prefix}-list" for prefix in prefixes], request=request
    )


services = ScrudServices()


def collection_type_uri_for(type_uri):
    return f"https://api.openteams.com/json-schema/ResourceCollection?contents_type={type_uri}"  # noqa: E501
