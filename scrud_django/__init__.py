"""
SCRUD-Django - Semantic Create, Read, Update and Delete Django application.
"""

__version__ = "0.1.9"
import base64
import datetime

from django.utils.timezone import datetime as ddt


class ScrudServices:
    last_modified: datetime.datetime
    services: dict
    etag: str

    def __init__(self):
        self.update_last_modified()
        print(F"Initializing services - last modified = {self.last_modified}")
        self.services = {}

    def _set_etag(self):
        self.etag = base64.b64encode(
            self.last_modified.isoformat().encode('utf-8')
        ).decode('utf-8')

    def update_last_modified(self):
        self.last_modified = ddt.now()
        self._set_etag()

    def add_service(self, key, value):
        self.services[key] = value
        self.update_last_modified()

    def get_last_modified(self):
        return self.last_modified

    def get_etag(self):
        return self.etag


services = ScrudServices()


def collection_type_uri_for(type_uri):
    return f"https://api.openteams.com/json-schema/ResourceCollection?contents_type={type_uri}"  # noqa: E501
