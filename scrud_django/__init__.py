"""
SCRUD-Django - Semantic Create, Read, Update and Delete Django application.
"""

import hashlib
import json
from importlib.metadata import PackageNotFoundError, version

from rest_framework.reverse import reverse

try:
    __version__ = version("scrud-django")
except PackageNotFoundError:
    # Imported from a source tree that is not installed (e.g. a checkout on
    # PYTHONPATH): there is no distribution metadata to read the version from.
    __version__ = "0+unknown"


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
