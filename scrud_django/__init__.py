"""
SCRUD-Django - Semantic Create, Read, Update and Delete Django application.
"""

import hashlib
import json
from importlib.metadata import PackageNotFoundError, version
from urllib import parse

from django.conf import settings
from django.urls import reverse as django_reverse

try:
    __version__ = version("scrud-django")
except PackageNotFoundError:
    # Imported from a source tree that is not installed (e.g. a checkout on
    # PYTHONPATH): there is no distribution metadata to read the version from.
    __version__ = "0+unknown"


def reverse(viewname, args=None, kwargs=None, request=None):
    """``django.urls.reverse``, absolute when given a `request`: what DRF's
    ``reverse`` gives with no versioning scheme, the request's format override
    query parameter (``?format=``) kept."""
    url = django_reverse(viewname, args=args, kwargs=kwargs)
    if not request:
        return url
    url = request.build_absolute_uri(url)
    param = getattr(settings, "REST_FRAMEWORK", {}).get("URL_FORMAT_OVERRIDE", "format")
    if param and param in request.GET:
        scheme, netloc, path, query, fragment = parse.urlsplit(url)
        params = parse.parse_qs(query, keep_blank_values=True)
        params[param] = [request.GET[param]]
        query = parse.urlencode(sorted(params.items()), doseq=True)
        url = parse.urlunsplit((scheme, netloc, path, query, fragment))
    return url


class ScrudServices:
    """The service catalog (``services/``): type URI -> a function of the
    request giving the URL of its list. Its validator is `etag_for`, a hash
    of the catalog as served, the same in every process that registered
    the same services; there is no modification time (entries are added by
    code at startup, which records none that every process shares)."""

    services: dict

    def __init__(self):
        self.services = {}

    def add_service(self, type_uri, value):
        """Register `type_uri`'s list: `value` is a route name (its
        ``-list`` route) or a function ``f(request=None)`` returning a URL."""
        if callable(value):
            self.services[type_uri] = value
        else:
            self.services[type_uri] = lambda request=None: reverse(
                f"{value}-list", request=request
            )

    def catalog(self, request=None):
        """The catalog as served to `request`: type URI -> list URL."""
        return {
            type_uri: url_func(request=request)
            for type_uri, url_func in self.services.items()
        }

    def etag_for(self, request=None):
        """The ETag of `catalog` for `request`: a hash of its content."""
        content = json.dumps(self.catalog(request), sort_keys=True)
        return hashlib.sha256(content.encode("utf-8")).hexdigest()


services = ScrudServices()


def collection_type_uri_for(type_uri):
    return f"https://api.openteams.com/json-schema/ResourceCollection?contents_type={type_uri}"  # noqa: E501
