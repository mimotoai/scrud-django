"""One request reads its resource type once (``get_resource_type`` is
memoized on the view for the request, per database), not once per caller
(the filter, the serializer, the validators, the Links, the stamps), and
the resource it looks up shares it."""

from uuid import uuid4

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils.http import quote_etag
from django.utils.timezone import now
from rest_framework.test import APIClient
from scoped_rbac.models import Role, RoleAssignment

from scrud_django.models import Resource
from scrud_django.registration import ResourceRegistration

from .factories import UserFactory
from .rbac_defaults import TYPE_URI_LIST_PARTNER_PROFILES, TYPE_URI_PARTNER_PROFILES

PP = TYPE_URI_PARTNER_PROFILES
PP_LIST = TYPE_URI_LIST_PARTNER_PROFILES
TYPE_TABLE = '"scrud_django_resourcetype"'


def own_type_queries(queries, resource_type):
    """The statements of `queries` that read or write `resource_type`'s row
    (by its slug or its key); the Link headers also read the JSON Schema and
    JSON-LD types' rows."""
    found = []
    for query in queries:
        sql = query["sql"]
        head, _, where = sql.partition(" WHERE ")
        if f"FROM {TYPE_TABLE}" not in head and not head.startswith(
            f"UPDATE {TYPE_TABLE}"
        ):
            continue
        if f"\"slug\" = '{resource_type.slug}'" in where or where.startswith(
            f'{TYPE_TABLE}."id" = {resource_type.pk} '
        ):
            found.append(sql.split()[0])
        elif where == f'{TYPE_TABLE}."id" = {resource_type.pk}':
            found.append(sql.split()[0])
    return found


@pytest.fixture
def client_and_profiles():
    reverse("partner-profiles-list")
    profiles = [
        ResourceRegistration.register(
            {"name": f"n{i}", "rbac_context": "a"}, "partner-profiles"
        )
        for i in range(10)
    ]
    user = UserFactory()
    role = Role(
        definition={m: [PP, PP_LIST] for m in ("http.GET", "http.PUT")},
        etag=uuid4().hex,
        modified_at=now(),
    )
    role.save()
    RoleAssignment(
        user=user, role=role, rbac_context="a", etag=uuid4().hex, modified_at=now()
    ).save()
    client = APIClient()
    client.force_authenticate(user)
    return client, profiles


def measured(request):
    with CaptureQueriesContext(connection) as queries:
        response = request()
    assert 200 <= response.status_code < 300, response.content
    return queries.captured_queries


@pytest.mark.django_db
def test_a_list_reads_its_resource_type_once(client_and_profiles):
    client, profiles = client_and_profiles
    queries = measured(lambda: client.get(reverse("partner-profiles-list")))
    assert own_type_queries(queries, profiles[0].resource_type) == ["SELECT"]
    assert len(queries) <= 11, len(queries)


@pytest.mark.django_db
def test_a_detail_get_reads_its_resource_type_once(client_and_profiles):
    client, profiles = client_and_profiles
    url = reverse("partner-profiles-detail", args=[profiles[0].pk])
    queries = measured(lambda: client.get(url))
    assert own_type_queries(queries, profiles[0].resource_type) == ["SELECT"]
    assert len(queries) <= 8, len(queries)


@pytest.mark.django_db
def test_a_put_reads_its_resource_type_once_and_writes_it_once(client_and_profiles):
    client, profiles = client_and_profiles
    resource = profiles[0]
    url = reverse("partner-profiles-detail", args=[resource.pk])
    etag = Resource.objects.get(pk=resource.pk).etag
    queries = measured(
        lambda: client.put(
            url,
            {"name": "z", "rbac_context": "a"},
            format="json",
            HTTP_IF_MATCH=quote_etag(etag),
        )
    )
    found = own_type_queries(queries, resource.resource_type)
    assert found == ["SELECT", "UPDATE"], found
    assert len(queries) <= 25, len(queries)
