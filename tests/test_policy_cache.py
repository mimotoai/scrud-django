"""A stored ``WorkflowPolicy`` is compiled once per version, not on every write:
`workflow_policy_for` keeps the compiled policy in a bounded LRU cache keyed by
the stored resource's pk, ETag and modification time."""

import threading
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from django.urls import reverse
from django.utils.http import quote_etag
from django.utils.timezone import now
from rest_framework.test import APIClient
from scoped_rbac.models import Role, RoleAssignment

from scrud_django import workflow
from scrud_django.models import Resource
from scrud_django.registration import (
    ResourceRegistration,
    workflow_policy_resource_type,
)
from scrud_django.workflow import (
    CompiledPolicyCache,
    WorkflowPolicy,
    workflow_policy_for,
)

from .factories import UserFactory

TODO = "tests://ToDo"
ALL_METHODS = ("http.GET", "http.POST", "http.PUT", "http.DELETE")
PERMISSIVE_POLICY = {
    "resource_type": TODO,
    "initial_states": {"any": "true"},
    "states": {},
    "transitions": {},
}


@pytest.fixture
def compiled(monkeypatch):
    """The content of each policy compiled, in order."""
    compiled = []
    original = WorkflowPolicy.compile_state_expressions

    def spy(self):
        compiled.append(self.policy)
        return original(self)

    monkeypatch.setattr(WorkflowPolicy, "compile_state_expressions", spy)
    return compiled


def store_policy(content):
    workflow_policy_resource_type()
    return ResourceRegistration.register(content, "workflow-policies")


def todo_writer(context):
    role = Role(
        definition={m: [TODO] for m in ALL_METHODS},
        etag=uuid4().hex,
        modified_at=now(),
    )
    role.save()
    user = UserFactory()
    RoleAssignment(
        user=user, role=role, rbac_context=context, etag=uuid4().hex, modified_at=now()
    ).save()
    client = APIClient()
    client.force_authenticate(user)
    return client


@pytest.mark.django_db
def test_two_writes_compile_once_and_an_update_recompiles(compiled):
    reverse("todos-list")
    policy = dict(PERMISSIVE_POLICY, rbac_context="p")
    stored = store_policy(policy)
    client = todo_writer("p")
    compiled.clear()

    for title in ("one", "two"):
        response = client.post(
            reverse("todos-list"), {"title": title, "rbac_context": "p"}, format="json"
        )
        assert response.status_code == 201, response.content
    todo = Resource.objects.filter(resource_type__type_uri=TODO).latest("pk")
    response = client.put(
        reverse("todos-detail", args=[todo.pk]),
        {"title": "three", "rbac_context": "p"},
        format="json",
        HTTP_IF_MATCH=quote_etag(todo.etag),
    )
    assert response.status_code == 200, response.content
    assert compiled == [policy]

    updated = dict(policy, initial_states={"titled": "title != ''"})
    ResourceRegistration.update(
        content=updated, register_type="workflow-policies", slug=str(stored.pk)
    )
    compiled.clear()
    for title in ("four", "five"):
        response = client.post(
            reverse("todos-list"), {"title": title, "rbac_context": "p"}, format="json"
        )
        assert response.status_code == 201, response.content
    assert compiled == [updated]
    assert workflow_policy_for(TODO, "p").policy == updated
    assert compiled == [updated]


@pytest.mark.django_db
def test_the_cached_policy_names_the_resource_it_was_read_from():
    reverse("todos-list")
    stored = store_policy(dict(PERMISSIVE_POLICY, rbac_context="p"))
    first = workflow_policy_for(TODO, "p")
    second = workflow_policy_for(TODO, "p")
    assert first.resource == stored and second.resource == stored
    assert first.resource is not second.resource  # each lookup's own instance
    assert first.state_expressions is second.state_expressions  # compiled once


def a_resource(pk, content, etag="e"):
    return SimpleNamespace(
        pk=pk,
        etag=etag,
        modified_at=datetime(2026, 1, 1, tzinfo=UTC),
        content=content,
    )


def policy_content(name):
    return dict(PERMISSIVE_POLICY, initial_states={name: "true"})


def test_the_cache_is_bounded_and_least_recently_used_goes_first(compiled):
    cache = CompiledPolicyCache(maxsize=2)
    a, b, c = (a_resource(pk, policy_content(n)) for pk, n in enumerate("abc"))
    for resource in (a, b, a, c):  # a is used again before c: b goes
        assert cache.get(resource).resource is resource
    assert compiled == [a.content, b.content, c.content]
    assert len(cache) == 2
    compiled.clear()
    cache.get(a)
    cache.get(c)
    assert compiled == []
    cache.get(b)
    assert compiled == [b.content]


def test_a_new_etag_or_content_recompiles(compiled):
    cache = CompiledPolicyCache(maxsize=4)
    resource = a_resource(1, policy_content("a"))
    cache.get(resource)
    cache.get(resource)
    assert len(compiled) == 1
    cache.get(a_resource(1, policy_content("a"), etag="f"))
    assert len(compiled) == 2
    # Same pk, ETag and time but other content (a save that kept the ETag):
    # not the compiled policy of the old content.
    changed = a_resource(1, policy_content("b"), etag="f")
    assert list(cache.get(changed).initial_state_expressions) == ["b"]
    assert len(compiled) == 3


def test_an_invalid_stored_policy_is_not_cached(compiled):
    from django.core.exceptions import ImproperlyConfigured

    cache = CompiledPolicyCache(maxsize=4)
    invalid = a_resource(7, dict(PERMISSIVE_POLICY, states={"x": "status == "}))
    for _ in range(2):
        with pytest.raises(ImproperlyConfigured, match="WorkflowPolicy pk 7"):
            cache.get(invalid)
    assert len(cache) == 0


def test_the_cache_is_safe_across_threads():
    cache = CompiledPolicyCache(maxsize=3)
    resources = [a_resource(pk, policy_content(f"s{pk}")) for pk in range(6)]
    errors = []
    start = threading.Barrier(8)

    def work(offset):
        try:
            start.wait()
            for i in range(200):
                resource = resources[(i + offset) % len(resources)]
                policy = cache.get(resource)
                assert policy.resource is resource
                assert list(policy.initial_state_expressions) == [f"s{resource.pk}"]
                assert len(cache) <= 3
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(cache) <= 3


def test_workflow_policy_for_uses_the_module_cache():
    assert isinstance(workflow.STORED_POLICY_CACHE, CompiledPolicyCache)
    assert workflow.STORED_POLICY_CACHE.maxsize == workflow.STORED_POLICY_CACHE_SIZE
