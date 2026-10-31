"""
SCRUD-Django - Semantic Create, Read, Update and Delete Django application.
"""

import base64
import datetime
from importlib.metadata import PackageNotFoundError, version

from rest_framework.reverse import reverse

try:
    __version__ = version("scrud-django")
except PackageNotFoundError:
    # Imported from a source tree that is not installed (e.g. a checkout on
    # PYTHONPATH): there is no distribution metadata to read the version from.
    __version__ = "0+unknown"


class ScrudServices:
    last_modified: datetime.datetime
    services: dict
    etag: str

    def __init__(self):
        self.update_last_modified()
        self.services = {}

    def _set_etag(self):
        self.etag = base64.b64encode(
            self.last_modified.isoformat().encode('utf-8')
        ).decode('utf-8')

    def update_last_modified(self):
        # Aware UTC: a naive time would be read in TIME_ZONE (scrudful, as
        # Django), which need not be the zone it was taken in (the process's
        # TZ changes when Django is configured).
        self.last_modified = datetime.datetime.now(datetime.UTC)
        self._set_etag()

    def add_service(self, type_uri, value):
        if callable(value):
            self.services[type_uri] = value
        else:
            self.services[type_uri] = lambda request=None: reverse(
                f"{value}-list", request=request
            )
        self.update_last_modified()

    def get_last_modified(self):
        return self.last_modified

    def get_etag(self):
        return self.etag


services = ScrudServices()


def collection_type_uri_for(type_uri):
    return f"https://api.openteams.com/json-schema/ResourceCollection?contents_type={type_uri}"  # noqa: E501
