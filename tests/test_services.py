"""The service list (``services/``): its ``ETag`` is a hash of the catalog it
serves, so every process and worker that registered the same services sends
the same one, whenever it started; it changes when the catalog does. It sends
no ``Last-Modified``: nothing records when a catalog entry, which code adds
at startup, last changed (a process's start time differed between workers)."""

import time

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from scrud_django import ScrudServices, services


def a_catalog():
    """A catalog as a process start builds it."""
    catalog = ScrudServices()
    catalog.add_service("tests://A", "partner-profiles")
    catalog.add_service("tests://B", lambda request=None: "http://example.com/b/")
    return catalog


def test_two_processes_with_the_same_services_have_the_same_etag():
    first = a_catalog()
    time.sleep(1.1)  # another process, started later
    second = a_catalog()
    assert first.etag_for(None) == second.etag_for(None)
    # Registered again, as each start does, it is unchanged.
    second.add_service("tests://A", "partner-profiles")
    assert first.etag_for(None) == second.etag_for(None)
    # A catalog with another entry differs.
    second.add_service("tests://C", lambda request=None: "http://example.com/c/")
    assert first.etag_for(None) != second.etag_for(None)


@pytest.mark.django_db
def test_the_service_list_etag_follows_its_catalog():
    reverse("services")  # the URLconf registers the services
    client = APIClient()
    response = client.get(reverse("services"))
    assert response.status_code == 200, response.content
    assert "Last-Modified" not in response, response["Last-Modified"]
    etag = response["ETag"]
    assert client.get(reverse("services"), HTTP_IF_NONE_MATCH=etag).status_code == 304
    # Registering the same services again (a new process) keeps it.
    for type_uri, url_func in list(services.services.items()):
        services.add_service(type_uri, url_func)
    time.sleep(1.1)
    assert client.get(reverse("services"))["ETag"] == etag
    # A new service changes it.
    services.add_service("tests://K5M1", lambda request=None: "http://example.com/")
    try:
        assert client.get(reverse("services"))["ETag"] != etag
    finally:
        del services.services["tests://K5M1"]
