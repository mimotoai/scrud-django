import json
from uuid import uuid4

import fastjsonschema
import pytest
from django.test import override_settings
from django.urls import reverse
from django.utils.http import quote_etag
from django.utils.timezone import now
from rest_framework.exceptions import ErrorDetail, NotFound
from rest_framework.renderers import JSONRenderer
from rest_framework.request import Request
from rest_framework.test import APIClient, APIRequestFactory
from rest_framework.views import APIView
from scoped_rbac.models import Role, RoleAssignment

from scrud_django.exceptions import (
    JsonValidationError,
    StoredPolicyReference,
    WorkflowInvalidStateError,
    WorkflowPolicyInvalidError,
    scrud_exception_handler,
)
from scrud_django.models import Resource
from scrud_django.registration import (
    ResourceRegistration,
    get_resource_type_for,
    workflow_policy_resource_type,
)

from .factories import UserFactory


@pytest.fixture
def api_context():
    view = APIView()
    request = Request(APIRequestFactory().get("/"))
    request.accepted_renderer = JSONRenderer()
    request.accepted_media_type = "application/json"
    view.request = request
    return {"request": request, "view": view}


def test_scrud_errors_keep_their_body(api_context):
    exc = WorkflowInvalidStateError(policy="todo")
    response = scrud_exception_handler(exc, api_context)
    assert response.status_code == 400
    assert (
        response.data["type"]
        == "http://api.openteams.com/http-problem/workflow-transition-invalid"
    )
    assert response.data["message"] == "The request does not match a valid state"
    assert response["Content-Type"].startswith("application/problem+json")


def test_other_errors_use_scoped_rbac_problem_details(api_context):
    response = scrud_exception_handler(NotFound(), api_context)
    assert response.status_code == 404
    assert set(response.data) >= {"type", "title", "status"}


# Every scrud error answers 400 under any of these exception handlers: DRF's own,
# scoped-rbac's, and scrud's.
HANDLERS = [
    "rest_framework.views.exception_handler",
    "scoped_rbac.rest.exception_handler",
    "scrud_django.exceptions.scrud_exception_handler",
]

TODO = "tests://ToDo"
NOT_STARTED = {
    "title": "Buy milk",
    "status": "not started",
    "priority": "medium",
    "assignee": "http://somewhere.example/someone",
}
# A stored policy with no transitions: any change of state is invalid.
NO_TRANSITIONS_POLICY = {
    "resource_type": TODO,
    "initial_states": {"not started": 'status == "not started"'},
    "states": {"in progress": 'status == "in progress"'},
    "transitions": {},
}
# Lets anyone create a ToDo in "", and PUT one only to "cancel".
CANCEL_ONLY = {
    "http.GET": [TODO],
    "http.POST": [TODO],
    "http.PUT": {
        TODO: {
            "condition": {
                "operator": "scrud_workflow",
                "authorized_transitions": ["cancel"],
            }
        }
    },
}


def with_handler(handler):
    from django.conf import settings

    return override_settings(
        REST_FRAMEWORK=dict(settings.REST_FRAMEWORK, EXCEPTION_HANDLER=handler)
    )


def client_for(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


def cancel_only_user():
    role = Role(definition=CANCEL_ONLY, etag=uuid4().hex, modified_at=now())
    role.save()
    user = UserFactory()
    RoleAssignment(
        user=user, role=role, rbac_context="", etag=uuid4().hex, modified_at=now()
    ).save()
    return user


def a_todo():
    reverse("todos-list")
    return ResourceRegistration.register(dict(NOT_STARTED), "todos")


def store_policy(content):
    workflow_policy_resource_type()
    return ResourceRegistration.register(content, "workflow-policies")


def put(user, resource, content):
    return client_for(user).put(
        reverse("todos-detail", args=[resource.pk]),
        content,
        format="json",
        HTTP_IF_MATCH=quote_etag(resource.etag),
    )


@pytest.mark.django_db
@pytest.mark.parametrize("handler", HANDLERS)
def test_json_validation_error_is_400_under_any_handler(handler):
    reverse("todos-list")
    admin = UserFactory(is_staff=True, is_superuser=True)
    with with_handler(handler):
        response = client_for(admin).post(
            reverse("todos-list"), dict(NOT_STARTED, title=5), format="json"
        )
    assert response.status_code == 400, response.content
    json.loads(response.content)


@pytest.mark.django_db
@pytest.mark.parametrize("handler", HANDLERS)
def test_invalid_transition_under_stored_policy_is_400_under_any_handler(handler):
    # The error's detail used to be the policy itself, here a stored one.
    store_policy(NO_TRANSITIONS_POLICY)
    resource = a_todo()
    admin = UserFactory(is_staff=True, is_superuser=True)
    with with_handler(handler):
        response = put(admin, resource, dict(NOT_STARTED, status="in progress"))
    assert response.status_code == 400, response.content
    json.loads(response.content)


@pytest.mark.django_db
@pytest.mark.parametrize("handler", HANDLERS)
def test_unauthorized_transition_is_400_under_any_handler(handler):
    resource = a_todo()
    with with_handler(handler):
        response = put(cancel_only_user(), resource, dict(NOT_STARTED, status="done"))
    assert response.status_code == 400, response.content
    json.loads(response.content)


INVALID_CONDITION_POLICY = dict(
    NO_TRANSITIONS_POLICY, states={"in progress": "status == "}
)


@pytest.mark.django_db
@pytest.mark.parametrize("handler", HANDLERS)
def test_invalid_workflow_policy_is_400_under_any_handler(handler):
    reverse("todos-list")
    workflow_policy_resource_type()
    admin = UserFactory(is_staff=True, is_superuser=True)
    with with_handler(handler):
        response = client_for(admin).post(
            reverse("workflow-policies-list"), INVALID_CONDITION_POLICY, format="json"
        )
    assert response.status_code == 400, response.content
    json.loads(response.content)


@pytest.mark.django_db
@pytest.mark.parametrize("handler", HANDLERS)
def test_an_empty_body_is_400_under_any_handler(handler):
    reverse("todos-list")
    admin = UserFactory(is_staff=True, is_superuser=True)
    with with_handler(handler):
        response = client_for(admin).post(
            reverse("todos-list"), content_type="application/json"
        )
    assert response.status_code == 400, response.content
    json.loads(response.content)


@pytest.mark.django_db
def test_scrud_handler_body_for_an_invalid_workflow_policy():
    reverse("todos-list")
    workflow_policy_resource_type()
    admin = UserFactory(is_staff=True, is_superuser=True)
    response = client_for(admin).post(
        reverse("workflow-policies-list"), INVALID_CONDITION_POLICY, format="json"
    )
    assert response.status_code == 400
    body = response.json()
    assert set(body) == {"type", "title", "detail", "status"}, body
    assert body["type"] == (
        "https://api.openteams.com/http-problem/workflow-policy-invalid"
    )
    assert body["title"] == "Invalid workflow policy"
    assert body["detail"].startswith("Invalid workflow state condition: "), body
    assert body["status"] == 400
    assert response["Content-Type"].startswith("application/problem+json")


@pytest.mark.django_db
def test_scrud_handler_body_for_a_stored_policy_error():
    stored = store_policy(NO_TRANSITIONS_POLICY)
    resource = a_todo()
    admin = UserFactory(is_staff=True, is_superuser=True)
    response = put(admin, resource, dict(NOT_STARTED, status="in progress"))
    assert response.status_code == 400
    url = "http://testserver" + reverse("workflow-policies-detail", args=[stored.pk])
    assert response.json() == {
        "type": "http://api.openteams.com/http-problem/workflow-transition-invalid",
        "title": "Invalid workflow transition",
        "detail": url,
        "policy": url,
        "prior_state": "not started",
        "proposed_state": "in progress",
        "message": "Invalid workflow transition",
        "status": 400,
    }


@pytest.mark.django_db
def test_scrud_handler_body_for_a_json_validation_error():
    reverse("todos-list")
    admin = UserFactory(is_staff=True, is_superuser=True)
    response = client_for(admin).post(
        reverse("todos-list"), dict(NOT_STARTED, title=5), format="json"
    )
    assert response.status_code == 400
    body = response.json()
    assert body["type"] == "https://api.openteams.com/http-problem/json-invalid"
    assert body["title"] == "JSON Validation Error"
    assert body["detail"] == "data.title must be string"
    assert body["rule"] == "type"
    assert body["status"] == 400


def test_scrud_errors_carry_an_error_detail_with_a_code():
    exc = WorkflowInvalidStateError(policy=StoredPolicyReference(None))
    assert isinstance(exc.detail, ErrorDetail)
    assert exc.detail.code == "workflow_transition_invalid"
    assert exc.get_codes() == "workflow_transition_invalid"
    json_exc = JsonValidationError(
        fastjsonschema.JsonSchemaValueException(
            "data must be object", value=5, name="data", rule="type"
        )
    )
    assert isinstance(json_exc.detail, ErrorDetail)
    assert json_exc.detail.code == "json_invalid"
    assert str(json_exc.detail) == "data must be object"
    policy_exc = WorkflowPolicyInvalidError("Invalid workflow state condition: x")
    assert isinstance(policy_exc.detail, ErrorDetail)
    assert policy_exc.detail.code == "workflow_policy_invalid"
    assert str(policy_exc.detail) == "Invalid workflow state condition: x"


# A resource a type uses (its schema, JSON-LD context or indexing policy) is
# protected (on_delete=PROTECT): deleting it is a 409 problem naming the type,
# under any handler, and leaves it stored.

RESOURCE_IN_USE = "https://api.openteams.com/http-problem/resource-in-use"


def delete_schema_of(slug):
    reverse(f"{slug}-list")
    schema = get_resource_type_for(slug).schema
    admin = UserFactory(is_staff=True, is_superuser=True)
    response = client_for(admin).delete(
        reverse("json-schema-detail", args=[schema.pk]),
        HTTP_IF_MATCH=quote_etag(schema.etag),
    )
    return schema, response


@pytest.mark.django_db
@pytest.mark.parametrize("handler", HANDLERS)
def test_deleting_a_resource_a_type_uses_is_409_under_any_handler(handler):
    with with_handler(handler):
        schema, response = delete_schema_of("todos")
    assert response.status_code == 409, response.content
    assert "tests://ToDo" in json.loads(response.content)["detail"]
    assert Resource.objects.filter(pk=schema.pk).exists()


@pytest.mark.django_db
def test_scrud_handler_body_for_a_resource_a_type_uses():
    schema, response = delete_schema_of("todos")
    assert response.status_code == 409, response.content
    assert response["Content-Type"] == "application/problem+json"
    body = json.loads(response.content)
    assert body["type"] == RESOURCE_IN_USE
    assert body["title"] == "Resource in use"
    assert body["status"] == 409
    assert body["used_by"] == [{"resource_type": "tests://ToDo", "as": "schema"}]
    assert "tests://ToDo" in body["detail"]
    assert Resource.objects.filter(pk=schema.pk).exists()


@pytest.mark.django_db
def test_scrud_error_under_drf_default_handler():
    # An adopter's own DRF handler renders a scrud error: the body is the one
    # recorded before scrud errors stopped being ``APIException``s.
    reverse("todos-list")
    admin = UserFactory(is_staff=True, is_superuser=True)
    with with_handler("rest_framework.views.exception_handler"):
        response = client_for(admin).post(
            reverse("todos-list"), dict(NOT_STARTED, title=5), format="json"
        )
    assert response.status_code == 400
    assert response.content == b'{"detail":"data.title must be string"}'


def test_scrud_error_is_not_api_exception():
    from rest_framework.exceptions import APIException

    from scrud_django.exceptions import as_drf

    error = JsonValidationError(
        fastjsonschema.JsonSchemaValueException(
            "data must be object", value=5, name="data", rule="type"
        )
    )
    assert not isinstance(error, APIException)
    wrapped = as_drf(error)
    assert isinstance(wrapped, APIException)
    assert wrapped.detail.code == "json_invalid"
    assert wrapped.status_code == 400
    assert wrapped.scrud is error


def test_scrud_error_full_details_are_drfs():
    # A scrud error answers ``get_full_details`` as it did as an
    # ``APIException``: its message and its code.
    from scrud_django.exceptions import as_drf

    error = JsonValidationError(
        fastjsonschema.JsonSchemaValueException(
            "data must be object", value=5, name="data", rule="type"
        )
    )
    details = error.get_full_details()
    assert details == {"message": "data must be object", "code": "json_invalid"}
    assert details == as_drf(error).get_full_details()
    assert isinstance(details["message"], ErrorDetail)
    assert details["message"].code == "json_invalid"


def test_drf_wrapper_class_is_defined_once_at_import():
    # ``as_drf`` wraps every scrud error in the one ``ScrudAPIException``,
    # defined when the module is imported, not on first use.
    from rest_framework.exceptions import APIException

    from scrud_django import exceptions

    assert issubclass(exceptions.ScrudAPIException, APIException)
    first = exceptions.as_drf(WorkflowPolicyInvalidError("first"))
    second = exceptions.as_drf(
        JsonValidationError(fastjsonschema.JsonSchemaValueException("x"))
    )
    assert type(first) is type(second) is exceptions.ScrudAPIException
    assert not hasattr(exceptions, "_drf_scrud_class")


def test_as_drf_without_drf_is_improperly_configured():
    from unittest import mock

    from django.core.exceptions import ImproperlyConfigured

    from scrud_django import exceptions

    with mock.patch.object(exceptions, "ScrudAPIException", None):
        with pytest.raises(ImproperlyConfigured, match="Django REST framework"):
            exceptions.as_drf(WorkflowPolicyInvalidError("x"))


IMPORT_WITH = """
import importlib.abc, sys

class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "rest_framework" or name.startswith("rest_framework."):
            raise {error}
        return None

sys.meta_path.insert(0, Blocker())
import scrud_django.exceptions as exceptions
print(exceptions.ScrudAPIException is None)
"""


@pytest.mark.parametrize(
    "error, falls_back",
    [
        ('ModuleNotFoundError("absent", name="rest_framework")', True),
        ('ModuleNotFoundError("absent", name=name)', True),
        ('ModuleNotFoundError("absent", name="rest_framework_extra")', False),
        ('ModuleNotFoundError("absent", name="yaml")', False),
        ('ImportError("broken", name=name)', False),
    ],
)
def test_only_drf_being_absent_selects_the_no_drf_branch(error, falls_back):
    # Another import error (a DRF that fails to import, or a dependency of
    # it missing) is raised, not taken for DRF being absent.
    import subprocess
    import sys
    from pathlib import Path

    result = subprocess.run(
        [sys.executable, "-c", IMPORT_WITH.format(error=error)],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
    )
    if falls_back:
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "True"
    else:
        assert result.returncode != 0
        assert "absent" in result.stderr or "broken" in result.stderr


def test_a_bare_scrud_exception_renders_only_the_members_it_sets():
    from scrud_django.exceptions import ScrudException

    class Bare(ScrudException):
        status_code = 418

    class Titled(Bare):
        title = "Titled"

    assert Bare("nope").response_data({}) == {"detail": "nope"}
    assert Titled("nope").response_data({}) == {"title": "Titled", "detail": "nope"}
    assert ScrudException.type is None
    assert ScrudException.title is None
    assert ScrudException.instance is None


@pytest.mark.django_db
def test_a_stored_policys_url_is_the_serving_apis():
    # A workflow error names a stored policy by the URL of the API serving
    # the request: within the namespace the request was resolved under (a
    # ninja API's), else by the route's global name (the DRF routes').
    from django.test import RequestFactory
    from django.urls import resolve

    from .front_ends import serve_both

    with override_settings(ROOT_URLCONF="tests.parity_urls"):
        serve_both()
        policy = ResourceRegistration.register(
            {
                "resource_type": "tests://ToDo",
                "rbac_context": "p",
                "initial_states": {"any": "true"},
                "states": {},
                "transitions": {},
            },
            "workflow-policies",
        )
        reference = StoredPolicyReference(policy)
        urls = {}
        for mount in ("drf", "ninja"):
            request = RequestFactory().get(f"/{mount}/workflow-policies/")
            request.resolver_match = resolve(request.path)
            urls[mount] = reference.url(request)
        assert urls == {
            mount: f"http://testserver/{mount}/workflow-policies/{policy.pk}/"
            for mount in ("drf", "ninja")
        }
        assert reference.url() == f"/drf/workflow-policies/{policy.pk}/"
