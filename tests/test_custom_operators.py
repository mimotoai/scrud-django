"""Adopters' own operators get plain data, as scoped-rbac's checks give it: the
request body itself (a ``dict``) on the request check and where the workflow
check authorizes a transition (which they read as
``scrud_django.permissions.current_transition()``), and the stored content
(a ``dict``) on object checks. An operator that evaluates CEL itself reads it
as scoped-rbac does. A check must only read what it gets: a change is stored."""

from uuid import uuid4

import pytest
from django.conf import settings
from django.test import override_settings
from django.urls import reverse
from django.utils.http import quote_etag
from django.utils.timezone import now
from rest_framework.test import APIClient
from scoped_rbac.models import Role, RoleAssignment

from scrud_django.models import Resource

from .factories import UserFactory

TODO = "tests://ToDo"
PP = "tests://PartnerProfiles"
PP_LIST = "https://api.openteams.com/json-schema/ResourceCollection?contents_type=" + PP
NOT_STARTED = {
    "title": "ok",
    "status": "not started",
    "priority": "medium",
    "assignee": "http://somewhere.example/someone",
    "rbac_context": "q",
}


def field_is(resource, configuration):
    """An adopter's operator: allows a body whose `field` is `value`."""
    return resource.get(configuration["field"]) == configuration["value"]


# The type of what each call of the `mutator` operator got.
MUTATED = []


def mutator(resource, configuration):
    """An adopter's buggy operator that changes what it is given, at the top
    level and in a nested object and array (the same change each time it is
    called), and allows."""
    MUTATED.append(type(resource))
    if resource is None:
        return True
    resource["title"] = "MUTATED"
    if isinstance(resource.get("nested"), dict):
        resource["nested"]["evil"] = True
    if isinstance(resource.get("tags"), list) and "evil" not in resource["tags"]:
        resource["tags"].append("evil")
    return True


def plain_at_every_level(resource, configuration):
    """An adopter's operator that allows a body whose objects and arrays are
    exact ``dict``s and ``list``s at every level (walked without
    recursion)."""
    stack = [resource]
    while stack:
        node = stack.pop()
        if isinstance(node, (dict, list, tuple)) or hasattr(node, "keys"):
            if type(node) not in (dict, list):
                return False
            stack.extend(node.values() if type(node) is dict else node)
    return True


@pytest.fixture(autouse=True)
def field_is_operator():
    MUTATED.clear()
    with override_settings(
        SCOPED_RBAC=dict(
            settings.SCOPED_RBAC,
            OPERATORS={
                "field_is": field_is,
                "mutator": mutator,
                "plain_at_every_level": plain_at_every_level,
            },
        )
    ):
        yield


def field_is_grant(type_uri, field):
    return {
        type_uri: {"condition": {"operator": "field_is", "field": field, "value": "ok"}}
    }


def user_with(definition, context="q"):
    role = Role(definition=definition, etag=uuid4().hex, modified_at=now())
    role.save()
    user = UserFactory()
    RoleAssignment(
        user=user, role=role, rbac_context=context, etag=uuid4().hex, modified_at=now()
    ).save()
    client = APIClient()
    client.force_authenticate(user)
    return client


@pytest.mark.django_db
def test_body_operator_on_a_type_without_a_workflow():
    reverse("todos-list")
    client = user_with({"http.POST": field_is_grant(PP, "name")})
    before = Resource.objects.count()
    response = client.post(
        reverse("partner-profiles-list"),
        {"name": "no", "rbac_context": "q"},
        format="json",
    )
    assert response.status_code == 403, response.content
    assert Resource.objects.count() == before
    response = client.post(
        reverse("partner-profiles-list"),
        {"name": "ok", "rbac_context": "q"},
        format="json",
    )
    assert response.status_code == 201, response.content


@pytest.mark.django_db
def test_body_operator_on_a_workflow_type():
    reverse("todos-list")
    definition = {
        "http.GET": [TODO],
        "http.POST": field_is_grant(TODO, "title"),
        "http.PUT": {
            TODO: {
                "condition": {
                    "operator": "scrud_workflow",
                    "authorized_transitions": ["cancel"],
                }
            }
        },
    }
    client = user_with(definition)
    response = client.post(
        reverse("todos-list"), dict(NOT_STARTED, title="no"), format="json"
    )
    assert response.status_code == 403, response.content
    response = client.post(reverse("todos-list"), NOT_STARTED, format="json")
    assert response.status_code == 201, response.content

    # The transitions are still those of the scrud_workflow grant.
    todo = Resource.objects.latest("pk")
    detail = reverse("todos-detail", args=[todo.pk])
    response = client.put(
        detail,
        dict(NOT_STARTED, status="done"),
        format="json",
        HTTP_IF_MATCH=quote_etag(todo.etag),
    )
    assert response.status_code == 400, response.content
    canceled = dict(NOT_STARTED, status="canceled")
    response = client.put(
        detail, canceled, format="json", HTTP_IF_MATCH=quote_etag(todo.etag)
    )
    assert response.status_code == 200, response.content
    assert Resource.objects.get(pk=todo.pk).content == canceled


MUTATOR = {"condition": {"operator": "mutator"}}


@pytest.mark.django_db
@pytest.mark.parametrize(
    "type_uri,route,body",
    [
        (PP, "partner-profiles-list", {"name": "x", "rbac_context": "q"}),
        (TODO, "todos-list", NOT_STARTED),
    ],
    ids=["request-check", "workflow-check"],
)
def test_an_operator_gets_the_body_itself_and_its_changes_are_stored(
    type_uri, route, body
):
    # The request check (scoped-rbac's) and the workflow check give operators
    # the body itself, a plain dict, not a copy or a read-only view (as
    # scoped-rbac decided, issue #55): an operator that changes it changes
    # what is stored, a bug in that operator which nothing guards.
    reverse("todos-list")
    client = user_with({"http.GET": [type_uri], "http.POST": {type_uri: MUTATOR}})
    sent = dict(body, nested={"a": 1}, tags=["t"])
    response = client.post(reverse(route), sent, format="json")
    assert response.status_code == 201, response.content
    assert Resource.objects.latest("pk").content == dict(
        sent, title="MUTATED", nested={"a": 1, "evil": True}, tags=["t", "evil"]
    )
    assert MUTATED, "the operator was not asked"
    assert set(MUTATED) == {dict}, MUTATED


def test_scoped_rbacs_body_contexts_reads_the_bodys_array_of_contexts():
    # The body a contexts_from gets is the parsed body itself: its array of
    # contexts is a list, which access.body_contexts (scoped-rbac's default
    # contexts_from) reads as several contexts.
    from scoped_rbac import access

    body = {"rbac_context": ["a", "b"]}
    assert access.body_contexts(body) == ["a", "b"]


LD = "http://www.w3.org/ns/json-ld#context"


@pytest.mark.django_db
def test_a_contexts_from_delegating_to_scoped_rbac_reads_several_contexts():
    reverse("todos-list")
    role = Role(definition={"http.POST": [LD]}, etag=uuid4().hex, modified_at=now())
    role.save()
    user = UserFactory()
    for context in ("a", "b"):
        RoleAssignment(
            user=user,
            role=role,
            rbac_context=context,
            etag=uuid4().hex,
            modified_at=now(),
        ).save()
    client = APIClient()
    client.force_authenticate(user)
    response = client.post(
        reverse("body-contexts-json-ld-list"),
        {"@context": {}, "rbac_context": ["a", "b"]},
        format="json",
    )
    assert response.status_code == 201, response.content
    response = client.post(
        reverse("body-contexts-json-ld-list"),
        {"@context": {}, "rbac_context": ["a", "c"]},
        format="json",
    )
    assert response.status_code == 403, response.content


def nested(depth):
    """A JSON text of `depth` nested arrays."""
    return "[" * depth + "]" * depth


@pytest.mark.django_db
def test_a_body_nested_past_the_limit_is_400_and_one_within_it_is_stored():
    # A body nested about 1000 levels (one the JSON parser reads) is a 400
    # body-too-deep, before any check: Python code that walks it recursively
    # (reversion's serializer, an operator) would fail with RecursionError, a
    # 500. One within MAX_BODY_DEPTH is checked (its operator gets plain
    # dicts and lists at every level), stored and read back.
    from scrud_django.mixins.view_mixins import MAX_BODY_DEPTH

    reverse("todos-list")
    plain = {"condition": {"operator": "plain_at_every_level"}}
    client = user_with({"http.GET": [PP], "http.POST": {PP: plain}})
    before = Resource.objects.count()
    for depth in (990, MAX_BODY_DEPTH):
        body = '{"name": "deep", "rbac_context": "q", "x": ' + nested(depth) + "}"
        response = client.generic(
            "POST",
            reverse("partner-profiles-list"),
            body,
            content_type="application/json",
        )
        assert response.status_code == 400, response.content[:200]
        problem = response.json()
        if depth == MAX_BODY_DEPTH or problem.get("code") != "parse_error":
            # (Python 3.11's JSON parser already refuses 990 levels: a 400
            # parse_error.)
            assert problem["type"] == (
                "https://api.openteams.com/http-problem/body-too-deep"
            )
    assert Resource.objects.count() == before
    # The object holding the arrays is one level: MAX_BODY_DEPTH in all.
    body = (
        '{"name": "deep", "rbac_context": "q", "x": ' + nested(MAX_BODY_DEPTH - 1) + "}"
    )
    response = client.generic(
        "POST", reverse("partner-profiles-list"), body, content_type="application/json"
    )
    assert response.status_code == 201, response.content[:200]
    stored = Resource.objects.latest("pk")
    reader = user_with({"http.GET": [PP, PP_LIST]})
    assert (
        reader.get(reverse("partner-profiles-detail", args=[stored.pk])).status_code
        == 200
    )
    assert reader.get(reverse("partner-profiles-list")).status_code == 200


# An adopter's operator that evaluates CEL itself, the pattern scoped-rbac's
# docs describe: it refuses a resource whose `field` is "secret" with
# ``not cel.is_true(...)``, so a value CEL cannot read (anything but plain
# dicts and lists: an undetermined result) would ALLOW it.
RECEIVED = []


def deny_secret(resource, configuration):
    from scoped_rbac import cel

    RECEIVED.append(resource)
    expression = cel.compile_expression(
        f"resource.{configuration['field']} == 'secret'"
    )
    return not cel.is_true(expression, {"resource": resource})


@pytest.fixture
def deny_secret_operator():
    RECEIVED.clear()
    with override_settings(
        SCOPED_RBAC=dict(settings.SCOPED_RBAC, OPERATORS={"deny_secret": deny_secret})
    ):
        yield RECEIVED


def deny_secret_on(field="name"):
    return {"condition": {"operator": "deny_secret", "field": field}}


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


def assert_plain_dicts(received):
    """Every object the operator received (``None`` from a request check
    without a body aside) is an exact ``dict``, its arrays exact lists."""
    objects = [obj for obj in received if obj is not None]
    assert objects, received
    for obj in objects:
        assert type(obj) is dict, type(obj)
        for value in obj.values():
            if isinstance(value, (list, tuple)):
                assert type(value) is list, type(value)


def a_profile(name, context="q"):
    from scrud_django.registration import ResourceRegistration

    reverse("partner-profiles-list")
    return ResourceRegistration.register(
        {"name": name, "rbac_context": context}, "partner-profiles"
    )


@pytest.mark.django_db
def test_a_cel_operator_refuses_on_the_request_check(deny_secret_operator):
    reverse("todos-list")
    client = client_with(({"http.GET": [PP], "http.POST": {PP: deny_secret_on()}}, "q"))
    before = Resource.objects.count()
    response = client.post(
        reverse("partner-profiles-list"),
        {"name": "secret", "rbac_context": "q"},
        format="json",
    )
    assert response.status_code == 403, response.content
    assert Resource.objects.count() == before
    response = client.post(
        reverse("partner-profiles-list"),
        {"name": "ok", "rbac_context": "q"},
        format="json",
    )
    assert response.status_code == 201, response.content
    assert_plain_dicts(deny_secret_operator)


@pytest.mark.django_db
def test_a_cel_operator_refuses_on_the_detail_routes_object_check(
    deny_secret_operator,
):
    secret, ok = a_profile("secret"), a_profile("ok")
    grant = {PP: deny_secret_on()}
    client = client_with(({"http.GET": grant, "http.DELETE": grant}, "q"))
    # A DELETE's lookup is not filtered by the condition: the object check
    # refuses it (and, the resource being unreadable, hides it).
    response = client.delete(
        reverse("partner-profiles-detail", args=[secret.pk]),
        HTTP_IF_MATCH=quote_etag(secret.etag),
    )
    assert response.status_code == 404, response.content
    assert Resource.objects.filter(pk=secret.pk).exists()
    response = client.get(reverse("partner-profiles-detail", args=[ok.pk]))
    assert response.status_code == 200, response.content
    response = client.get(reverse("partner-profiles-detail", args=[secret.pk]))
    assert response.status_code == 404, response.content
    response = client.delete(
        reverse("partner-profiles-detail", args=[ok.pk]),
        HTTP_IF_MATCH=quote_etag(ok.etag),
    )
    assert response.status_code == 204, response.content
    assert_plain_dicts(deny_secret_operator)


@pytest.mark.django_db
def test_a_cel_operator_refuses_on_the_lists_per_object_check(deny_secret_operator):
    a_profile("secret"), a_profile("ok")
    client = client_with(
        ({"http.GET": [PP_LIST]}, "q"), ({"http.GET": {PP: deny_secret_on()}}, "q")
    )
    response = client.get(reverse("partner-profiles-list"))
    assert response.status_code == 200, response.content
    assert [e["content"]["name"] for e in response.json()["content"]] == ["ok"]
    assert_plain_dicts(deny_secret_operator)


@pytest.mark.django_db
def test_a_cel_operator_refuses_on_the_post_save_check(deny_secret_operator):
    # Any one context of the body suffices for the request check, and of the
    # stored resource for the object check: "open" allows both outright. The
    # post-save check needs every context of the saved resource, "q" too,
    # where only the operator grants a PUT. (A JSON-LD context document may
    # name several contexts.)
    from scrud_django.registration import ResourceRegistration

    reverse("todos-list")
    stored = ResourceRegistration.register(
        {"@context": {}, "name": "ok", "rbac_context": ["open", "q"]}, "json-ld"
    )
    client = client_with(
        ({"http.GET": [LD], "http.PUT": [LD]}, "open"),
        ({"http.PUT": {LD: deny_secret_on()}}, "q"),
    )
    url = reverse("json-ld-detail", args=[stored.pk])
    secret = {"@context": {}, "name": "secret", "rbac_context": ["open", "q"]}
    response = client.put(
        url, secret, format="json", HTTP_IF_MATCH=quote_etag(stored.etag)
    )
    assert response.status_code == 403, response.content
    assert Resource.objects.get(pk=stored.pk).content == stored.content
    fine = dict(secret, name="fine")
    response = client.put(
        url, fine, format="json", HTTP_IF_MATCH=quote_etag(stored.etag)
    )
    assert response.status_code == 200, response.content
    assert Resource.objects.get(pk=stored.pk).content == fine
    assert_plain_dicts(deny_secret_operator)


@pytest.mark.django_db
def test_a_cel_operator_refuses_on_the_workflow_check(deny_secret_operator):
    # A ToDo stored in "q" is moved to "open". The request check reads the
    # body's context ("open", where scrud_workflow allows outside the
    # workflow check), the object check the stored one ("q", where the
    # operator reads the stored "ok"). The workflow check authorizes the
    # transition in both, "q" too, where only the operator grants it on the
    # body: refused there, a 400, before the save.
    from scrud_django.registration import ResourceRegistration

    reverse("todos-list")
    stored = ResourceRegistration.register(dict(NOT_STARTED, title="ok"), "todos")
    client = client_with(
        (
            {
                "http.GET": [TODO],
                "http.PUT": {
                    TODO: {
                        "condition": {
                            "operator": "scrud_workflow",
                            "authorized_transitions": ["__INITIALIZE__"],
                        }
                    }
                },
            },
            "open",
        ),
        ({"http.GET": [TODO], "http.PUT": {TODO: deny_secret_on("title")}}, "q"),
    )
    url = reverse("todos-detail", args=[stored.pk])
    moved = dict(NOT_STARTED, rbac_context="open")
    response = client.put(
        url,
        dict(moved, title="secret"),
        format="json",
        HTTP_IF_MATCH=quote_etag(stored.etag),
    )
    assert response.status_code == 400, response.content
    problem = response.json()
    assert problem["type"] == (
        "http://api.openteams.com/http-problem/workflow-transition-invalid"
    ), problem
    assert Resource.objects.get(pk=stored.pk).content == stored.content
    response = client.put(
        url, moved, format="json", HTTP_IF_MATCH=quote_etag(stored.etag)
    )
    assert response.status_code == 200, response.content
    assert Resource.objects.get(pk=stored.pk).content == moved
    assert_plain_dicts(deny_secret_operator)


# What `current_transition()` was in each call of the `recording_transition`
# operator.
TRANSITIONS = []


def recording_transition(resource, configuration):
    from scrud_django.permissions import current_transition

    TRANSITIONS.append(current_transition())
    return True


@pytest.mark.django_db
def test_current_transition_is_the_workflow_checks_and_none_outside_it():
    from scrud_django.permissions import current_transition

    reverse("todos-list")
    TRANSITIONS.clear()
    operators = dict(
        settings.SCOPED_RBAC, OPERATORS={"recording_transition": recording_transition}
    )
    with override_settings(SCOPED_RBAC=operators):
        client = client_with(
            (
                {
                    "http.GET": [TODO],
                    "http.POST": {
                        TODO: {"condition": {"operator": "recording_transition"}}
                    },
                },
                "q",
            )
        )
        response = client.post(reverse("todos-list"), NOT_STARTED, format="json")
    assert response.status_code == 201, response.content
    # The request check (first) and the post-save check (last) are outside
    # the workflow check; the workflow check authorizes the creation.
    assert TRANSITIONS[0] is None and TRANSITIONS[-1] is None, TRANSITIONS
    assert set(TRANSITIONS) == {None, "__INITIALIZE__"}, TRANSITIONS
    assert current_transition() is None


def test_scrud_workflow_allows_outside_the_workflow_check_only_what_it_lists():
    from scrud_django.permissions import authorized_transitions, checking_transition

    configuration = {"authorized_transitions": ["start"]}
    body = {"title": "x"}
    # Outside the workflow check (the request and object checks): allowed,
    # the workflow check decides the transition.
    assert authorized_transitions(body, configuration) is True
    with checking_transition("start"):
        assert authorized_transitions(body, configuration) is True
    with checking_transition("finish"):
        assert authorized_transitions(body, configuration) is False
    # Where no workflow policy applies there is no transition: refused.
    with checking_transition(None):
        assert authorized_transitions(body, configuration) is False
        assert authorized_transitions(body, {}) is False
    assert authorized_transitions(body, configuration) is True


class TransitionLeaf:
    """An ``IsAuthorized`` of a view, as the workflow check asks it: records
    `current_transition()` and the object it gets, then answers `outcome`
    (or raises ``PermissionDenied`` for ``"raise"``)."""

    def __init__(self, outcome):
        self.outcome = outcome
        self.seen = []

    def has_context_permission(self, request, view, obj, context):
        from rest_framework.exceptions import PermissionDenied

        from scrud_django.permissions import current_transition

        self.seen.append((current_transition(), type(obj)))
        if self.outcome == "raise":
            raise PermissionDenied()
        return self.outcome


@pytest.mark.django_db
@pytest.mark.parametrize("outcome", [False, "raise"], ids=["refused", "raised"])
def test_the_transition_is_forgotten_after_a_refused_or_raising_check(outcome):
    from types import SimpleNamespace

    from rest_framework.exceptions import PermissionDenied
    from rest_framework.test import APIRequestFactory

    from scrud_django.permissions import current_transition
    from scrud_django.registration import get_resource_type_for
    from scrud_django.validators import WorkflowValidator

    reverse("todos-list")
    request = APIRequestFactory().put("/")
    request.user = UserFactory(is_superuser=True, is_staff=True)
    leaf = TransitionLeaf(outcome)
    view = SimpleNamespace(get_rbac_permissions=lambda: [leaf])
    context = SimpleNamespace(
        context={"request": request, "view": view},
        resource_type=get_resource_type_for("todos"),
    )
    validator = WorkflowValidator()
    if outcome == "raise":
        with pytest.raises(PermissionDenied):
            validator.allowed(context, "q", "start", {"title": "x"})
    else:
        assert validator.allowed(context, "q", "start", {"title": "x"}) is False
    assert leaf.seen == [("start", dict)]
    assert current_transition() is None
