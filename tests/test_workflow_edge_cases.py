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
from .front_ends import api_url

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
    """A client logged in as `user` through the session (both front ends)."""
    client = APIClient()
    client.force_login(user)
    return client


def admin():
    return UserFactory(is_staff=True, is_superuser=True)


def register(content, slug):
    reverse("todos-list", urlconf="tests.urls")
    return ResourceRegistration.register(content, slug)


def store_policy(content):
    workflow_policy_resource_type()
    return register(content, "workflow-policies")


def put(front_end, user, resource, content, slug):
    return client_for(user).put(
        api_url(front_end, f"{slug}-detail", args=[resource.pk]),
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
def test_put_over_list_content_follows_the_policy(front_end):
    stored = register([dict(NOT_STARTED)], "todos")
    # The default ToDo policy: the stored content is in no state, so no
    # transition leads from it.
    response = put(front_end, admin(), stored, dict(NOT_STARTED), "todos")
    assert response.status_code == 400, response.content
    assert response.json()["type"] == WORKFLOW_ERROR
    assert_unchanged(stored)


@pytest.mark.django_db
def test_put_over_list_content_without_a_policy_is_200(front_end):
    stored = register([{"name": "n"}], "partner-profiles")
    response = put(front_end, admin(), stored, {"name": "n"}, "partner-profiles")
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


@pytest.mark.django_db
def test_serializer_without_a_request_validates_a_type_without_a_workflow():
    reverse("todos-list")
    serializer = ResourceSerializer(
        data={"name": "n", "rbac_context": "q"},
        resource_type=ResourceType.objects.get(type_uri=PP),
    )
    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data == {"name": "n", "rbac_context": "q"}


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
def test_post_save_check_requires_every_context(
    front_end, receiver_adding_other_context
):
    api_url(front_end, "todos-list")
    writer = user_with(({m: [PP] for m in ("http.GET", "http.POST")}, ""))
    before = Resource.objects.count()
    response = client_for(writer).post(
        api_url(front_end, "partner-profiles-list"), {"name": "n"}, format="json"
    )
    assert response.status_code == 403, response.content
    assert Resource.objects.count() == before


@pytest.mark.django_db
def test_post_save_check_passes_with_every_context(
    front_end, receiver_adding_other_context
):
    api_url(front_end, "todos-list")
    writer = user_with(
        ({m: [PP] for m in ("http.GET", "http.POST")}, ""),
        ({"http.POST": [PP]}, "other"),
    )
    response = client_for(writer).post(
        api_url(front_end, "partner-profiles-list"), {"name": "n"}, format="json"
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
def test_conditioned_put_without_a_policy_is_403(front_end, transitions):
    resource = locked_profile()
    user = user_with((dict(pp_reader(), **conditioned("http.PUT", transitions)), "q"))
    response = put(
        front_end,
        user,
        resource,
        {"name": "anything", "state": "open", "rbac_context": "q"},
        "partner-profiles",
    )
    assert response.status_code == 403, response.content
    assert_unchanged(resource)


@pytest.mark.django_db
def test_unconditional_put_without_a_policy_is_200(front_end):
    resource = locked_profile()
    user = user_with(({"http.GET": [PP], "http.PUT": [PP]}, "q"))
    body = {"name": "anything", "state": "open", "rbac_context": "q"}
    response = put(front_end, user, resource, body, "partner-profiles")
    assert response.status_code == 200, response.content
    assert Resource.objects.get(pk=resource.pk).content == body


@pytest.mark.django_db
def test_conditioned_post_without_a_policy_is_403(front_end):
    api_url(front_end, "todos-list")
    user = user_with((conditioned("http.POST", ["__INITIALIZE__"]), "q"))
    before = Resource.objects.count()
    response = client_for(user).post(
        api_url(front_end, "partner-profiles-list"),
        {"name": "n", "rbac_context": "q"},
        format="json",
    )
    assert response.status_code == 403, response.content
    assert Resource.objects.count() == before


@pytest.mark.django_db
def test_cel_conditioned_post_without_a_policy_reads_the_body(front_end):
    api_url(front_end, "todos-list")
    user = user_with(({"http.POST": {PP: {"condition": 'name == "ok"'}}}, "q"))
    client = client_for(user)
    response = client.post(
        api_url(front_end, "partner-profiles-list"),
        {"name": "ok", "rbac_context": "q"},
        format="json",
    )
    assert response.status_code == 201, response.content
    response = client.post(
        api_url(front_end, "partner-profiles-list"),
        {"name": "no", "rbac_context": "q"},
        format="json",
    )
    assert response.status_code == 403, response.content


@pytest.mark.django_db
def test_leaving_a_context_whose_grant_is_conditioned_is_403(front_end):
    # Conditioned (no transitions) in "q", an unconditional writer in "r":
    # the resource may not be moved out of "q".
    resource = locked_profile("q")
    user = user_with(
        (dict(pp_reader(), **conditioned("http.PUT", [])), "q"),
        ({"http.GET": [PP], "http.PUT": [PP]}, "r"),
    )
    body = {"name": "n", "state": "locked", "rbac_context": "r"}
    response = put(front_end, user, resource, body, "partner-profiles")
    assert response.status_code == 403, response.content
    assert_unchanged(resource)


@pytest.mark.django_db
def test_unconditional_writer_of_one_context_of_a_shared_resource_is_unaffected(
    front_end,
):
    # Without a policy, an update is checked by the object check (any of the
    # resource's contexts) and the post-save check, as before: no grant at all
    # in "x" does not block an unconditional writer of "q".
    resource = locked_profile(["q", "x"])
    user = user_with(({"http.GET": [PP], "http.PUT": [PP]}, "q"))
    body = {"name": "n2", "state": "locked", "rbac_context": "q"}
    response = put(front_end, user, resource, body, "partner-profiles")
    assert response.status_code == 200, response.content


@pytest.mark.django_db
def test_cel_conditioned_post_on_a_workflow_type_reads_the_body(front_end):
    # The workflow check authorizes the transition with the body as the fields
    # a CEL condition reads (it once gave CEL an object, not a mapping: a 500).
    api_url(front_end, "todos-list")
    user = user_with(({"http.POST": {TODO: {"condition": 'title == "ok"'}}}, "q"))
    client = client_for(user)
    response = client.post(
        api_url(front_end, "todos-list"),
        dict(NOT_STARTED, title="ok", rbac_context="q"),
        format="json",
    )
    assert response.status_code == 201, response.content
    response = client.post(
        api_url(front_end, "todos-list"),
        dict(NOT_STARTED, title="no", rbac_context="q"),
        format="json",
    )
    assert response.status_code == 403, response.content


# Error bodies: ``prior_state`` is the stored state, ``proposed_state`` the new
# one; a create has no prior state.

LOCKING_PP_POLICY = {
    "resource_type": PP,
    "initial_states": {"open": 'state == "open"'},
    "states": {"locked": 'state == "locked"'},
    "transitions": {"lock": {"from": "open", "to": "locked"}},
}


def states(response):
    body = response.json()
    assert body["type"] == WORKFLOW_ERROR, body
    return body["prior_state"], body["proposed_state"]


@pytest.mark.django_db
def test_invalid_transition_error_names_prior_then_proposed_state(front_end):
    store_policy(dict(LOCKING_PP_POLICY, rbac_context="q"))
    resource = locked_profile()
    response = put(
        front_end,
        admin(),
        resource,
        {"name": "n", "state": "open", "rbac_context": "q"},
        "partner-profiles",
    )
    assert response.status_code == 400, response.content
    assert states(response) == ("locked", "open")


@pytest.mark.django_db
def test_transition_from_no_state_error_names_no_prior_state(front_end):
    store_policy(dict(LOCKING_PP_POLICY, rbac_context=""))
    stored = register([{"name": "n"}], "partner-profiles")
    response = put(
        front_end, admin(), stored, {"name": "n", "state": "open"}, "partner-profiles"
    )
    assert response.status_code == 400, response.content
    assert states(response) == (None, "open")


@pytest.mark.django_db
def test_unauthorized_transition_error_names_prior_then_proposed_state(front_end):
    store_policy(dict(LOCKING_PP_POLICY, rbac_context="q"))
    resource = register(
        {"name": "n", "state": "open", "rbac_context": "q"}, "partner-profiles"
    )
    user = user_with(
        (
            {
                "http.GET": [PP, "http://api.openteams.com/json-ld/WorkflowPolicy"],
                **conditioned("http.PUT", []),
            },
            "q",
        )
    )
    response = put(
        front_end,
        user,
        resource,
        {"name": "n", "state": "locked", "rbac_context": "q"},
        "partner-profiles",
    )
    assert response.status_code == 400, response.content
    assert states(response) == ("open", "locked")


@pytest.mark.django_db
def test_unauthorized_create_error_has_no_prior_state(front_end):
    store_policy(dict(LOCKING_PP_POLICY, rbac_context="q"))
    user = user_with(
        (
            {
                "http.GET": ["http://api.openteams.com/json-ld/WorkflowPolicy"],
                **conditioned("http.POST", []),
            },
            "q",
        )
    )
    response = client_for(user).post(
        api_url(front_end, "partner-profiles-list"),
        {"name": "n", "state": "open", "rbac_context": "q"},
        format="json",
    )
    assert response.status_code == 400, response.content
    assert states(response) == (None, "open")


@pytest.mark.django_db
def test_create_transition_data_has_no_prior_state(reported_data):
    register(dict(NOT_STARTED), "todos")
    assert [(d.prior_state, d.proposed_state, d.transition) for d in reported_data] == [
        (None, "not started", "__INITIALIZE__")
    ]


@pytest.fixture
def reported_data():
    data = []

    def record(sender, instance, transition_data, **kwargs):
        if transition_data is not None:
            data.append(transition_data)

    scrud_post_save.connect(record)
    yield data
    scrud_post_save.disconnect(record)
