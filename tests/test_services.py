"""The service list (``services/``): its ``Last-Modified`` is the instant the
catalog last changed, whatever the server's or Django's time zone."""

import datetime

import pytest
from django.test import override_settings
from django.urls import reverse
from django.utils.http import parse_http_date
from rest_framework.test import APIClient

from scrud_django import ScrudServices, services


def test_the_catalog_time_is_aware_utc():
    catalog = ScrudServices()
    assert catalog.last_modified.utcoffset() == datetime.timedelta(0)
    now = datetime.datetime.now(datetime.UTC)
    assert abs((now - catalog.last_modified).total_seconds()) < 5


@pytest.mark.django_db
@pytest.mark.parametrize(
    "time_zone", ["UTC", "Pacific/Kiritimati", "Pacific/Pago_Pago"]
)
def test_the_service_list_last_modified_is_the_instant_it_changed(time_zone):
    # The catalog changes under one time zone (as at import, before Django
    # sets the process's TZ) and is served under another.
    reverse("services")  # the URLconf registers the services
    services.update_last_modified()
    changed = datetime.datetime.now(datetime.UTC).timestamp()
    with override_settings(TIME_ZONE=time_zone):
        response = APIClient().get(reverse("services"))
    assert response.status_code == 200, response.content
    assert abs(parse_http_date(response["Last-Modified"]) - changed) < 5, response[
        "Last-Modified"
    ]
