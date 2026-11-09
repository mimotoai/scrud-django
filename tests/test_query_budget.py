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
# Quoted as the database quotes them ("x" or `x`).
QUOTE = connection.ops.quote_name
TYPE_TABLE = QUOTE("scrud_django_resourcetype")
ID = QUOTE("id")


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
        if f"{QUOTE('slug')} = '{resource_type.slug}'" in where or where.startswith(
            f"{TYPE_TABLE}.{ID} = {resource_type.pk} "
        ):
            found.append(sql.split()[0])
        elif where == f"{TYPE_TABLE}.{ID} = {resource_type.pk}":
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
def test_a_put_reads_its_resource_type_once_and_never_writes_it(client_and_profiles):
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
    # Nothing reads the type's etag: a PUT no longer updates (or locks) the
    # type's row.
    assert found == ["SELECT"], found
    assert len(queries) <= 25, len(queries)


# A write is checked under the stored WorkflowPolicy of each of its contexts.
# With one policy per tenant, the lookup reads the policies of the write's
# contexts, not every tenant's.

TODO = "tests://ToDo"
WORKFLOW_POLICY = "http://api.openteams.com/json-ld/WorkflowPolicy"
# Every tenant's policy allows a create (any content); one that applied
# outside its context would still be seen in the reads counted below.
TENANT_POLICY = {
    "resource_type": TODO,
    "initial_states": {"any": "true"},
    "states": {},
    "transitions": {},
}


def store_tenant_policies(tenants):
    """A policy for each of the contexts ``t<i>`` not stored yet, `tenants`
    in all."""
    from scrud_django.registration import workflow_policy_resource_type

    workflow_policy_resource_type()
    stored = {
        policy.content["rbac_context"]
        for policy in Resource.objects.filter(resource_type__type_uri=WORKFLOW_POLICY)
    }
    for i in range(tenants):
        if f"t{i}" not in stored:
            ResourceRegistration.register(
                dict(TENANT_POLICY, rbac_context=f"t{i}"), "workflow-policies"
            )


@pytest.fixture
def policy_reads():
    """The stored WorkflowPolicy resources loaded (instantiated) while the
    test runs, by pk."""
    from django.db.models.signals import post_init

    from scrud_django.registration import workflow_policy_resource_type

    policy_type = workflow_policy_resource_type()
    loaded = []

    def receiver(sender, instance, **kwargs):
        if instance.resource_type_id == policy_type.pk and instance.pk is not None:
            loaded.append(instance.pk)

    post_init.connect(receiver, sender=Resource)
    yield loaded
    post_init.disconnect(receiver, sender=Resource)


def admin_client():
    client = APIClient()
    client.force_authenticate(UserFactory(is_staff=True, is_superuser=True))
    return client


def measured_write(policy_reads, request):
    """The queries `request` runs, and the policies it loads."""
    del policy_reads[:]
    queries = measured(request)
    return len(queries), list(policy_reads)


@pytest.mark.django_db
def test_a_workflow_write_reads_only_its_contexts_policies(policy_reads):
    client = admin_client()
    todos = reverse("todos-list")
    store_tenant_policies(1)
    own = Resource.objects.get(
        resource_type__type_uri=WORKFLOW_POLICY, content__rbac_context="t0"
    )

    def create():
        return client.post(todos, {"title": "t", "rbac_context": "t0"}, format="json")

    def created():
        assert create().status_code == 201
        return Resource.objects.order_by("pk").last().pk

    def update(pk):
        url = reverse("todos-detail", args=[pk])
        return lambda: client.put(
            url,
            {"title": "u", "rbac_context": "t0"},
            format="json",
            HTTP_IF_MATCH=quote_etag(Resource.objects.get(pk=pk).etag),
        )

    first = created()  # Warms the caches.
    second = created()
    one_tenant = (
        measured_write(policy_reads, create),
        measured_write(policy_reads, update(first)),
    )
    store_tenant_policies(20)
    twenty_tenants = (
        measured_write(policy_reads, create),
        measured_write(policy_reads, update(second)),
    )
    assert twenty_tenants == one_tenant
    for _, read in twenty_tenants:
        # The validator's lookup and the scrud_post_save report's.
        assert set(read) == {own.pk}, read


@pytest.mark.django_db
def test_a_workflow_policy_write_reads_only_its_contexts_policies(policy_reads):
    client = admin_client()
    reverse("todos-list")
    store_tenant_policies(1)
    own = Resource.objects.get(
        resource_type__type_uri=WORKFLOW_POLICY, content__rbac_context="t0"
    )
    url = reverse("workflow-policies-detail", args=[own.pk])

    def update():
        return client.put(
            url,
            dict(TENANT_POLICY, rbac_context="t0"),
            format="json",
            HTTP_IF_MATCH=quote_etag(Resource.objects.get(pk=own.pk).etag),
        )

    update()  # Warms the caches.
    one_tenant = measured_write(policy_reads, update)
    store_tenant_policies(20)
    twenty_tenants = measured_write(policy_reads, update)
    assert twenty_tenants[0] == one_tenant[0]
    # The uniqueness check (before and after the save) reads the policy
    # itself, and no other tenant's.
    assert set(twenty_tenants[1]) <= {own.pk}, twenty_tenants[1]
    assert len(twenty_tenants[1]) == len(one_tenant[1])
