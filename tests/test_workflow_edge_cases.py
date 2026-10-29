"""Workflow edge cases: content that is not an object, a serializer without a
request, the post-save check of every context, which policy ``scrud_post_save``
reports, and ``scrud_workflow``-conditioned grants where no policy applies."""

from uuid import uuid4

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.urls import reverse
from django.utils.http import quote_etag
from django.utils.timezone import now
from rest_framework.test import APIClient
from scoped_rbac.models import Role, RoleAssignment

from scrud_django.models import Resource, ResourceType
from scrud_django.registration import (
    ResourceRegistration,
    workflow_policy_resource_type,
)
from scrud_django.scrud_signals import scrud_post_save
from scrud_django.serializers import ResourceSerializer
from scrud_django.workflow import WorkflowPolicy, workflow_policy_for

from .factories import UserFactory

TODO = "tests://ToDo"
PP = "tests://PartnerProfiles"
WORKFLOW_ERROR = "http://api.openteams.com/http-problem/workflow-transition-invalid"
NOT_STARTED = {
    "title": "Buy milk",
    "status": "not started",
    "priority": "medium",
    "assignee": "http://somewhere.example/someone",
}


def user_with(*grants):
    user = UserFactory()
    for definition, context in grants:
        role = Role(definition=definition, etag=uuid4().hex, modified_at=now())
        role.save()
        RoleAssignment(
            user=user,
            role=role,
            rbac_context=context,
            etag=uuid4().hex,
            modified_at=now(),
        ).save()
    return user


def client_for(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


def admin():
    return UserFactory(is_staff=True, is_superuser=True)


def register(content, slug):
    reverse("todos-list")
    return ResourceRegistration.register(content, slug)


def store_policy(content):
    workflow_policy_resource_type()
    return register(content, "workflow-policies")


def put(user, resource, content, slug):
    return client_for(user).put(
        reverse(f"{slug}-detail", args=[resource.pk]),
        content,
        format="json",
        HTTP_IF_MATCH=quote_etag(resource.etag),
    )


def assert_unchanged(resource):
    stored = Resource.objects.get(pk=resource.pk)
    assert stored.content == resource.content
    assert stored.etag == resource.etag


# M-3: a stored resource whose content is not an object (pre-0.12.0).


def test_determine_state_of_content_that_is_not_an_object_is_no_match():
    policy = WorkflowPolicy(
        {"initial_states": {"any": "true"}, "states": {}, "transitions": {}}
    )
    for data in ([NOT_STARTED], "x", 5, None):
        assert policy.determine_state(data, policy.state_expressions) is None


@pytest.mark.django_db
def test_put_over_list_content_follows_the_policy():
    stored = register([dict(NOT_STARTED)], "todos")
    # The default ToDo policy: the stored content is in no state, so no
    # transition leads from it.
    response = put(admin(), stored, dict(NOT_STARTED), "todos")
    assert response.status_code == 400, response.content
    assert response.json()["type"] == WORKFLOW_ERROR
    assert_unchanged(stored)


@pytest.mark.django_db
def test_put_over_list_content_without_a_policy_is_200():
    stored = register([{"name": "n"}], "partner-profiles")
    response = put(admin(), stored, {"name": "n"}, "partner-profiles")
    assert response.status_code == 200, response.content
    assert Resource.objects.get(pk=stored.pk).content == {"name": "n"}


# M-4: the workflow check needs the request.


@pytest.mark.django_db
def test_workflow_validator_without_a_request_names_it():
    reverse("todos-list")
    serializer = ResourceSerializer(
        data=dict(NOT_STARTED), resource_type=ResourceType.objects.get(type_uri=TODO)
    )
    with pytest.raises(ImproperlyConfigured, match="request"):
        serializer.is_valid()


# M-1: the post-save check requires every context the stored resource is in.


@pytest.fixture
def receiver_adding_other_context():
    def add_other_context(sender, instance, **kwargs):
        content = dict(instance.content, rbac_context=["", "other"])
        Resource.objects.filter(pk=instance.pk).update(content=content)

    scrud_post_save.connect(add_other_context)
    yield
    scrud_post_save.disconnect(add_other_context)


@pytest.mark.django_db
def test_post_save_check_requires_every_context(receiver_adding_other_context):
    reverse("todos-list")
    writer = user_with(({m: [PP] for m in ("http.GET", "http.POST")}, ""))
    before = Resource.objects.count()
    response = client_for(writer).post(
        reverse("partner-profiles-list"), {"name": "n"}, format="json"
    )
    assert response.status_code == 403, response.content
    assert Resource.objects.count() == before


@pytest.mark.django_db
def test_post_save_check_passes_with_every_context(receiver_adding_other_context):
    reverse("todos-list")
    writer = user_with(
        ({m: [PP] for m in ("http.GET", "http.POST")}, ""),
        ({"http.POST": [PP]}, "other"),
    )
    response = client_for(writer).post(
        reverse("partner-profiles-list"), {"name": "n"}, format="json"
    )
    assert response.status_code == 201, response.content


# M-2: which context's policy ``scrud_post_save`` reports.


def start_policy(context, transition):
    return {
        "resource_type": TODO,
        "rbac_context": context,
        "initial_states": {"not started": 'status == "not started"'},
        "states": {"in progress": 'status == "in progress"'},
        "transitions": {transition: {"from": "not started", "to": "in progress"}},
    }


@pytest.fixture
def reported():
    transitions = []

    def record(sender, instance, transition_data, **kwargs):
        transitions.append(transition_data and transition_data.transition)

    scrud_post_save.connect(record)
    yield transitions
    scrud_post_save.disconnect(record)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "contexts, expected", [(["b", "a"], "start-b"), (["a", "b"], "start-a")]
)
def test_post_save_reports_the_first_named_contexts_policy(
    contexts, expected, reported
):
    policies = {
        "a": store_policy(start_policy("a", "start-a")),
        "b": store_policy(start_policy("b", "start-b")),
    }
    resource = register(dict(NOT_STARTED, rbac_context=contexts), "todos")
    assert resource.get_workflow_policy().resource.pk == policies[contexts[0]].pk

    del reported[:]
    # (The ToDo schema allows only a string rbac_context through the API.)
    progressing = dict(NOT_STARTED, status="in progress", rbac_context=contexts)
    ResourceRegistration.update(
        content=progressing, register_type="todos", slug=str(resource.pk)
    )
    assert reported == [expected]


@pytest.mark.django_db
def test_post_save_reports_the_default_policy_where_none_is_stored(reported):
    store_policy(start_policy("b", "start-b"))
    resource = register(dict(NOT_STARTED, rbac_context=["a", "b"]), "todos")
    assert (
        resource.get_workflow_policy().policy == workflow_policy_for(TODO, "a").policy
    )
    assert resource.get_workflow_policy().resource is None


# D6: a grant conditioned on ``scrud_workflow`` allows nothing where no
# workflow policy applies.


def conditioned(method, transitions):
    return {
        method: {
            PP: {
                "condition": {
                    "operator": "scrud_workflow",
                    "authorized_transitions": transitions,
                }
            }
        }
    }


def pp_reader():
    return {"http.GET": [PP]}


def locked_profile(context="q"):
    return register(
        {"name": "n", "state": "locked", "rbac_context": context}, "partner-profiles"
    )


@pytest.mark.django_db
@pytest.mark.parametrize("transitions", [[], ["__SAME_STATE__", "unlock"]])
def test_conditioned_put_without_a_policy_is_403(transitions):
    resource = locked_profile()
    user = user_with((dict(pp_reader(), **conditioned("http.PUT", transitions)), "q"))
    response = put(
        user,
        resource,
        {"name": "anything", "state": "open", "rbac_context": "q"},
        "partner-profiles",
    )
    assert response.status_code == 403, response.content
    assert_unchanged(resource)


@pytest.mark.django_db
def test_unconditional_put_without_a_policy_is_200():
    resource = locked_profile()
    user = user_with(({"http.GET": [PP], "http.PUT": [PP]}, "q"))
    body = {"name": "anything", "state": "open", "rbac_context": "q"}
    response = put(user, resource, body, "partner-profiles")
    assert response.status_code == 200, response.content
    assert Resource.objects.get(pk=resource.pk).content == body


@pytest.mark.django_db
def test_conditioned_post_without_a_policy_is_403():
    reverse("todos-list")
    user = user_with((conditioned("http.POST", ["__INITIALIZE__"]), "q"))
    before = Resource.objects.count()
    response = client_for(user).post(
        reverse("partner-profiles-list"),
        {"name": "n", "rbac_context": "q"},
        format="json",
    )
    assert response.status_code == 403, response.content
    assert Resource.objects.count() == before


@pytest.mark.django_db
def test_cel_conditioned_post_without_a_policy_reads_the_body():
    reverse("todos-list")
    user = user_with(({"http.POST": {PP: {"condition": 'name == "ok"'}}}, "q"))
    client = client_for(user)
    response = client.post(
        reverse("partner-profiles-list"),
        {"name": "ok", "rbac_context": "q"},
        format="json",
    )
    assert response.status_code == 201, response.content
    response = client.post(
        reverse("partner-profiles-list"),
        {"name": "no", "rbac_context": "q"},
        format="json",
    )
    assert response.status_code == 403, response.content


@pytest.mark.django_db
def test_leaving_a_context_whose_grant_is_conditioned_is_403():
    # Conditioned (no transitions) in "q", an unconditional writer in "r":
    # the resource may not be moved out of "q".
    resource = locked_profile("q")
    user = user_with(
        (dict(pp_reader(), **conditioned("http.PUT", [])), "q"),
        ({"http.GET": [PP], "http.PUT": [PP]}, "r"),
    )
    body = {"name": "n", "state": "locked", "rbac_context": "r"}
    response = put(user, resource, body, "partner-profiles")
    assert response.status_code == 403, response.content
    assert_unchanged(resource)


@pytest.mark.django_db
def test_unconditional_writer_of_one_context_of_a_shared_resource_is_unaffected():
    # Without a policy, an update is checked by the object check (any of the
    # resource's contexts) and the post-save check, as before: no grant at all
    # in "x" does not block an unconditional writer of "q".
    resource = locked_profile(["q", "x"])
    user = user_with(({"http.GET": [PP], "http.PUT": [PP]}, "q"))
    body = {"name": "n2", "state": "locked", "rbac_context": "q"}
    response = put(user, resource, body, "partner-profiles")
    assert response.status_code == 200, response.content


@pytest.mark.django_db
def test_cel_conditioned_post_on_a_workflow_type_reads_the_body():
    # The workflow check authorizes the transition with the body as the fields
    # a CEL condition reads (it was a TransitionCheck, not a mapping: a 500).
    reverse("todos-list")
    user = user_with(({"http.POST": {TODO: {"condition": 'title == "ok"'}}}, "q"))
    client = client_for(user)
    response = client.post(
        reverse("todos-list"),
        dict(NOT_STARTED, title="ok", rbac_context="q"),
        format="json",
    )
    assert response.status_code == 201, response.content
    response = client.post(
        reverse("todos-list"),
        dict(NOT_STARTED, title="no", rbac_context="q"),
        format="json",
    )
    assert response.status_code == 403, response.content
