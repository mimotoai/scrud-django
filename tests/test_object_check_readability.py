"""Whether a refused object is readable (404 or 403 on its detail route, and
whether a workflow error names its stored state) follows what the view's
``IsAuthorized`` permissions decide for a read, their object checks
included, as its list does: a permission class that refuses a read of one
object (``tests.urls.ReadRefusingIsAuthorized``) hides it (404) for every
method, and its workflow errors name no state. The object checks only
narrow: one that allows what the grants refuse does not make an object
readable."""

from uuid import uuid4

import pytest
from django.urls import reverse
from django.utils.http import quote_etag
from django.utils.timezone import now
from rest_framework.test import APIClient
from scoped_rbac.models import Role, RoleAssignment

from scrud_django.models import Resource
from scrud_django.registration import (
    ResourceRegistration,
    workflow_policy_resource_type,
)

from .factories import UserFactory
from .models import Note
from .rbac_defaults import TYPE_URI_LIST_PARTNER_PROFILES, TYPE_URI_PARTNER_PROFILES

PP = TYPE_URI_PARTNER_PROFILES
PP_LIST = TYPE_URI_LIST_PARTNER_PROFILES
TODO = "tests://ToDo"
TODO_LIST = (
    "https://api.openteams.com/json-schema/ResourceCollection?contents_type=" + TODO
)
WFP = "http://api.openteams.com/json-ld/WorkflowPolicy"
NOTE = "tests://Note"
NOTE_LIST = f"{NOTE}/list"


def client_with(*grants):
    """A client whose user holds each ``(definition, context)`` of `grants`."""
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
    client = APIClient()
    client.force_authenticate(user)
    return client


def profile(name, context="a"):
    reverse("partner-profiles-list")
    return ResourceRegistration.register(
        {"name": name, "rbac_context": context}, "partner-profiles"
    )


def detail(resource):
    return reverse("read-refusing-partner-profiles-detail", args=[resource.pk])


def if_match(resource):
    return {"HTTP_IF_MATCH": quote_etag(Resource.objects.get(pk=resource.pk).etag)}


READER = {"http.GET": [PP, PP_LIST]}


@pytest.mark.django_db
def test_a_read_the_permission_class_refuses_is_a_404_on_the_detail_route():
    unreadable, shown = profile("unreadable"), profile("shown")
    client = client_with((READER, "a"))
    assert client.get(detail(unreadable)).status_code == 404
    assert client.head(detail(unreadable)).status_code == 404
    assert client.get(detail(shown)).status_code == 200
    # Its list leaves out the same resource.
    response = client.get(reverse("read-refusing-partner-profiles-list"))
    assert response.status_code == 200, response.content
    assert [e["content"]["name"] for e in response.json()["content"]] == ["shown"]


@pytest.mark.django_db
def test_a_write_refused_on_an_object_the_permission_class_hides_is_a_404():
    unreadable, shown = profile("unreadable", "b"), profile("shown", "b")
    # The request check passes (http.PUT where the body goes, "a"); the
    # object check refuses (no http.PUT in "b", where they are stored): 403
    # where the writer may read the resource, 404 where the permission class
    # refuses them a read.
    client = client_with((READER, "b"), ({"http.PUT": [PP]}, "a"))
    body = {"name": "changed", "rbac_context": "a"}
    response = client.put(
        detail(unreadable), body, format="json", **if_match(unreadable)
    )
    assert response.status_code == 404, response.content
    response = client.put(detail(shown), body, format="json", **if_match(shown))
    assert response.status_code == 403, response.content
    assert Resource.objects.get(pk=unreadable.pk).content == unreadable.content


@pytest.mark.django_db
def test_a_write_the_permission_class_allows_is_not_refused_by_its_read_refusal():
    # The class refuses only reads: a writer of the resource may write it.
    unreadable = profile("unreadable")
    client = client_with(({"http.GET": [PP], "http.PUT": [PP]}, "a"))
    body = {"name": "unreadable", "rbac_context": "a", "note": "changed"}
    response = client.put(
        detail(unreadable), body, format="json", **if_match(unreadable)
    )
    assert response.status_code == 200, response.content


def a_note(text, rbac_context="a"):
    reverse("generic-notes-list")  # tests.urls registers the routes
    return Note.objects.create(
        text=text, rbac_context=rbac_context, etag=uuid4().hex, modified_at=now()
    )


@pytest.mark.django_db
def test_a_generic_view_hides_what_the_permission_class_refuses_to_read():
    unreadable, shown = a_note("unreadable"), a_note("shown")
    # The request check passes (http.PUT in the default context, where a
    # body without rbac_context goes); the object check refuses (no http.PUT
    # in "a", where the notes are).
    client = client_with(
        ({"http.GET": [NOTE, NOTE_LIST]}, "a"), ({"http.PUT": [NOTE]}, "")
    )

    def note_detail(note):
        return reverse("generic-read-refusing-notes-detail", args=[note.pk])

    assert client.get(note_detail(unreadable)).status_code == 404
    assert client.get(note_detail(shown)).status_code == 200
    response = client.put(
        note_detail(unreadable),
        {"text": "x"},
        format="json",
        HTTP_IF_MATCH=quote_etag(unreadable.etag),
    )
    assert response.status_code == 404, response.content
    response = client.put(
        note_detail(shown),
        {"text": "x"},
        format="json",
        HTTP_IF_MATCH=quote_etag(shown.etag),
    )
    assert response.status_code == 403, response.content
    response = client.get(reverse("generic-read-refusing-notes-list"))
    assert response.status_code == 200, response.content
    assert [note["text"] for note in response.json()] == ["shown"]


# Workflow errors name the stored state only to a writer who may read the
# resource, as the view's permissions decide a read.

PLAN = {
    "title": "plan",
    "status": "done",
    "priority": "high",
    "assignee": "http://somewhere.example/someone",
    "rbac_context": "team",
}
POLICY = {
    "resource_type": TODO,
    "rbac_context": "team",
    "initial_states": {"not started": 'status == "not started"'},
    "states": {
        "in progress": 'status == "in progress"',
        "canceled": 'status == "canceled"',
        "done": 'status == "done"',
    },
    "transitions": {
        "start": {"from": "not started", "to": "in progress"},
        "cancel": {"from": ["not started", "in progress"], "to": "canceled"},
    },
    "restrict_update": ["done"],
}
# Reads the ToDos and the policy, and may cancel.
CANCELLING_READER = {
    "http.GET": [TODO, TODO_LIST, WFP],
    "http.PUT": {
        TODO: {
            "condition": {
                "operator": "scrud_workflow",
                "authorized_transitions": ["cancel"],
            }
        }
    },
}


def stored_todo(title):
    workflow_policy_resource_type()
    reverse("todos-list")
    ResourceRegistration.register(dict(POLICY), "workflow-policies")
    return ResourceRegistration.register(dict(PLAN, title=title), "todos")


def cancel(client, todo):
    return client.put(
        reverse("read-refusing-todos-detail", args=[todo.pk]),
        dict(PLAN, status="canceled", title=todo.content["title"]),
        format="json",
        HTTP_IF_MATCH="*",
    )


@pytest.mark.django_db
def test_a_workflow_error_hides_the_state_of_what_the_permission_class_hides():
    todo = stored_todo("unreadable")
    client = client_with((CANCELLING_READER, "team"))
    url = reverse("read-refusing-todos-detail", args=[todo.pk])
    assert client.get(url).status_code == 404
    response = cancel(client, todo)
    assert response.status_code == 400, response.content
    problem = response.json()
    assert problem["prior_state"] is None, problem
    assert problem["proposed_state"] is None, problem
    assert problem["policy"] is None, problem
    assert '"done"' not in response.content.decode(), problem


@pytest.mark.django_db
def test_a_workflow_error_names_the_state_of_what_the_permission_class_shows():
    todo = stored_todo("plan")
    client = client_with((CANCELLING_READER, "team"))
    url = reverse("read-refusing-todos-detail", args=[todo.pk])
    assert client.get(url).status_code == 200
    response = cancel(client, todo)
    assert response.status_code == 400, response.content
    assert response.json()["prior_state"] == "done", response.content


@pytest.mark.django_db
def test_an_object_check_that_allows_does_not_make_an_object_readable():
    # OwnerIsAuthorized allows its owner anything in the object check; the
    # owner holds no http.GET grant, so a workflow error still hides the
    # stored state (the object checks may refuse a read, never grant one).
    workflow_policy_resource_type()
    reverse("todos-list")
    ResourceRegistration.register(dict(POLICY), "workflow-policies")
    user = UserFactory()
    todo = ResourceRegistration.register(dict(PLAN, owner=user.username), "todos")
    role = Role(
        definition={
            "http.GET": [WFP],
            "http.PUT": CANCELLING_READER["http.PUT"],
        },
        etag=uuid4().hex,
        modified_at=now(),
    )
    role.save()
    RoleAssignment(
        user=user, role=role, rbac_context="team", etag=uuid4().hex, modified_at=now()
    ).save()
    client = APIClient()
    client.force_authenticate(user)
    response = client.put(
        reverse("owner-todos-detail", args=[todo.pk]),
        dict(PLAN, status="canceled", owner=user.username),
        format="json",
        HTTP_IF_MATCH="*",
    )
    assert response.status_code == 400, response.content
    assert response.json()["prior_state"] is None, response.content
