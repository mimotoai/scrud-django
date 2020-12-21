import json
from copy import copy
from datetime import timezone

import pytest
from django.urls import reverse
from django.utils.http import http_date, quote_etag
from rest_framework import status
from safetydance_django.test import *  # noqa: F403, F401
from safetydance_django.test import http_response
from safetydance_test import And, Given, Then, scripted_test

from scrud_django.registration import ResourceRegistration

from .fixtures import *  # noqa: F403, F401
from .steps import *  # noqa: F403, F401

# RESOURCE

RESOURCE_ENDPOINT_PREFIX = "partner-profiles"
RESOURCE_ENDPOINT_DETAIL_NAME = f"{RESOURCE_ENDPOINT_PREFIX}-detail"
RESOURCE_ENDPOINT_LIST_NAME = f"{RESOURCE_ENDPOINT_PREFIX}-list"


def last_modified_for(resource):
    return http_date(resource.modified_at.replace(tzinfo=timezone.utc).timestamp())


def serialize_page(resources):
    return {
        'count': 1,
        'page_count': 1,
        'next': None,
        'previous': None,
        'content': [serialize_resource_envelope(resource) for resource in resources],
    }


def serialize_resource_envelope(resource):
    return {
        'content': resource.content,
        'last_modified': last_modified_for(resource),
        'etag': quote_etag(resource.etag),
        'href': "http://testserver"
        + reverse(RESOURCE_ENDPOINT_DETAIL_NAME, args=[resource.id]),
    }


def serialize_resource(resource):
    return resource.content


# TESTS


@pytest.mark.django_db
@scripted_test
def test_resource_get_list(regular_login, partner_profile_post_data):
    # NOTE: run url reverse first for resource type registrations
    url = reverse(RESOURCE_ENDPOINT_LIST_NAME)

    resource = ResourceRegistration.register(
        content=partner_profile_post_data, register_type='partner-profiles'
    )

    assert url == '/partner-profiles/'

    Given.http.get(url)
    Then.http.status_code_is(status.HTTP_200_OK)
    And.http.content_type_is('application/json')
    And.http.response_json_is(serialize_page([resource]))
    http_response.get('last-modified') == last_modified_for(resource)


@pytest.mark.django_db
@scripted_test
def test_resource_get_detail(regular_login, partner_profile_post_data):
    # NOTE: run url reverse first for resource type registrations
    reverse(RESOURCE_ENDPOINT_LIST_NAME)

    # generate a data for searching
    resource = ResourceRegistration.register(
        content=partner_profile_post_data, register_type='partner-profiles'
    )

    url = reverse(RESOURCE_ENDPOINT_DETAIL_NAME, kwargs={'slug': resource.id},)

    assert url == f'/partner-profiles/{resource.id}/'

    Given.http.get(url)
    Then.http.status_code_is(status.HTTP_200_OK)
    And.http.content_type_is('application/json')
    And.http.response_json_is(serialize_resource(resource))
    http_response.get('last-modified') == last_modified_for(resource)


@pytest.mark.django_db
@scripted_test
def test_resource_post(admin_login, partner_profile_post_data):
    # NOTE: run url reverse first for resource type registrations
    url = reverse(RESOURCE_ENDPOINT_LIST_NAME)
    assert url == '/partner-profiles/'

    serialized_data = partner_profile_post_data

    Given.http.force_authenticate(admin_login)
    And.http.post(
        url,
        data=json.dumps(partner_profile_post_data),
        content_type='application/json',
    )
    Then.http.status_code_is(status.HTTP_201_CREATED)
    And.http.content_type_is('application/json')
    And.http.resource_json_is(serialized_data)


@pytest.mark.django_db
@scripted_test
def test_resource_put(admin_login, partner_profile_post_data):
    # NOTE: run url reverse first for resource type registrations
    reverse(RESOURCE_ENDPOINT_LIST_NAME)

    resource = ResourceRegistration.register(
        content=partner_profile_post_data, register_type='partner-profiles'
    )

    partner_profile_put_data = copy(partner_profile_post_data)
    partner_profile_put_data['name'] = 'test put'
    partner_profile_put_data['slug'] = 'test-put'
    partner_profile_put_data['display_name'] = 'Test PUT'

    serialized_data = partner_profile_put_data

    url = reverse(RESOURCE_ENDPOINT_DETAIL_NAME, kwargs={'slug': resource.id})
    assert url == f'/partner-profiles/{resource.id}/'

    Given.http.force_authenticate(admin_login)
    And.http.get(url)
    response = http_response
    And.http.put(
        url,
        data=json.dumps(partner_profile_put_data),
        content_type='application/json',
        HTTP_IF_UNMODIFIED_SINCE=response["Last-Modified"],
        HTTP_IF_MATCH=response["ETag"],
    )
    Then.http.status_code_is(status.HTTP_200_OK)
    And.http.content_type_is('application/json')
    And.http.resource_json_is(serialized_data)


@pytest.mark.django_db
@scripted_test
def test_resource_delete(admin_login, partner_profile_post_data):
    # NOTE: run url reverse first for resource type registrations
    reverse(RESOURCE_ENDPOINT_LIST_NAME)

    resource = ResourceRegistration.register(
        content=partner_profile_post_data, register_type='partner-profiles'
    )

    url = reverse(RESOURCE_ENDPOINT_DETAIL_NAME, kwargs={'slug': resource.id})
    assert url == f'/partner-profiles/{resource.id}/'

    Given.http.force_authenticate(admin_login)
    And.http.get(url)
    response = http_response
    And.http.delete(
        url,
        HTTP_IF_UNMODIFIED_SINCE=response["Last-Modified"],
        HTTP_IF_MATCH=response["ETag"],
    )
    Then.http.status_code_is(status.HTTP_204_NO_CONTENT)


# JSON SCHEMA

JSON_SCHEMA_ENDPOINT_PREFIX = "json-schema"
JSON_SCHEMA_ENDPOINT_DETAIL_NAME = f"{JSON_SCHEMA_ENDPOINT_PREFIX}-detail"
JSON_SCHEMA_ENDPOINT_LIST_NAME = f"{JSON_SCHEMA_ENDPOINT_PREFIX}-list"


@pytest.mark.django_db
@scripted_test
def test_js_schema_get_list(regular_login):
    # NOTE: not sure if this endpoint would be useful
    url = reverse(JSON_SCHEMA_ENDPOINT_LIST_NAME)
    assert url == '/json-schema/'

    Given.http.force_authenticate(regular_login)
    And.http.get(url)
    Then.http.status_code_is(status.HTTP_200_OK)
    And.http.content_type_is('application/json')
    And.http.response_json_is({})


# JSON LD

JSON_LD_ENDPOINT_PREFIX = "json-ld"
JSON_LD_ENDPOINT_DETAIL_NAME = f"{JSON_LD_ENDPOINT_PREFIX}-detail"
JSON_LD_ENDPOINT_LIST_NAME = f"{JSON_LD_ENDPOINT_PREFIX}-list"


@pytest.mark.django_db
@scripted_test
def test_js_ld_get_list(regular_login):
    url = reverse(JSON_LD_ENDPOINT_LIST_NAME)
    assert url == '/json-ld/'

    Given.http.force_authenticate(regular_login)
    And.http.get(url)
    Then.http.status_code_is(status.HTTP_200_OK)
    And.http.content_type_is('application/json')
    And.http.response_json_is({})
