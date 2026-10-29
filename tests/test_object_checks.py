"""Object checks read the stored resource as a read-only mapping over its
content (`ResourceCheck`), as the workflow check's `TransitionCheck` is over
the body: a CEL condition reads its fields, an adopter's operator gets the
mapping, on the detail route (GET, PUT, DELETE), in the post-save check and
in a list's per-object check alike, so a list shows what the detail route
shows."""

from collections.abc import Mapping
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
from scrud_django.permissions import ResourceCheck, TransitionCheck
from scrud_django.registration import ResourceRegistration

from .factories import UserFactory
from .rbac_defaults import TYPE_URI_LIST_PARTNER_PROFILES, TYPE_URI_PARTNER_PROFILES

PP = TYPE_URI_PARTNER_PROFILES
PP_LIST = TYPE_URI_LIST_PARTNER_PROFILES

# What every call of the `name_is_ok` operator received.
SEEN = []


def name_is_ok(resource, configuration):
    """An adopter's operator that reads the body: allows ``name == "ok"``.
    A request check without a body (GET, DELETE) gives it ``None``:
    undetermined."""
    SEEN.append(resource)
    if resource is None:
        return None
    return resource.get("name") == "ok"


@pytest.fixture(autouse=True)
def name_is_ok_operator():
    SEEN.clear()
    with override_settings(
        SCOPED_RBAC=dict(settings.SCOPED_RBAC, OPERATORS={"name_is_ok": name_is_ok})
    ):
        yield


OPERATOR = {"condition": {"operator": "name_is_ok"}}
CEL = {"condition": 'name == "ok"'}


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


def conditional_client(condition):
    """GET, PUT and DELETE of partner profiles in "q" only by `condition`,
    and GET and DELETE outright in "o" (scoped-rbac's request check refuses a
    GET or DELETE whose only grant is conditional: it cannot decide it
    without the object)."""
    conditional = {PP: condition}
    return client_with(
        ({"http.GET": [PP, PP_LIST], "http.DELETE": [PP]}, "o"),
        (
            {
                "http.GET": conditional,
                "http.PUT": conditional,
                "http.DELETE": conditional,
            },
            "q",
        ),
    )


def profile(name, context="q"):
    reverse("partner-profiles-list")
    return ResourceRegistration.register(
        {"name": name, "rbac_context": context}, "partner-profiles"
    )


def detail(resource, prefix="partner-profiles"):
    return reverse(f"{prefix}-detail", args=[resource.pk])


def listed(client, path=None):
    response = client.get(path or reverse("partner-profiles-list"))
    assert response.status_code == 200, response.content
    return {entry["content"]["name"] for entry in response.json()["content"]}


@pytest.mark.django_db
@pytest.mark.parametrize("condition", [CEL, OPERATOR], ids=["cel", "operator"])
def test_a_conditional_get_on_the_detail_route(condition):
    ok, no = profile("ok"), profile("no")
    client = conditional_client(condition)
    response = client.get(detail(ok))
    assert response.status_code == 200, response.content
    assert response.json() == ok.content
    # A resource the condition refuses is not readable: hidden, a 404.
    assert client.get(detail(no)).status_code == 404


@pytest.mark.django_db
@pytest.mark.parametrize("condition", [CEL, OPERATOR], ids=["cel", "operator"])
def test_a_conditional_put_on_the_detail_route(condition):
    ok, no = profile("ok"), profile("no")
    client = conditional_client(condition)
    body = {"name": "ok", "display_name": "changed", "rbac_context": "q"}
    response = client.put(
        detail(ok), body, format="json", HTTP_IF_MATCH=quote_etag(ok.etag)
    )
    assert response.status_code == 200, response.content
    assert Resource.objects.get(pk=ok.pk).content == body
    # The object check reads the stored "no": hidden, whatever the body says.
    response = client.put(
        detail(no), body, format="json", HTTP_IF_MATCH=quote_etag(no.etag)
    )
    assert response.status_code == 404, response.content
    assert Resource.objects.get(pk=no.pk).content == no.content


@pytest.mark.django_db
@pytest.mark.parametrize("condition", [CEL, OPERATOR], ids=["cel", "operator"])
def test_a_conditional_delete_on_the_detail_route(condition):
    ok, no = profile("ok"), profile("no")
    client = conditional_client(condition)
    response = client.delete(detail(no), HTTP_IF_MATCH=quote_etag(no.etag))
    assert response.status_code == 404, response.content
    response = client.delete(detail(ok), HTTP_IF_MATCH=quote_etag(ok.etag))
    assert response.status_code == 204, response.content
    assert not Resource.objects.filter(pk=ok.pk).exists()
    assert Resource.objects.filter(pk=no.pk).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("condition", [CEL, OPERATOR], ids=["cel", "operator"])
def test_a_conditional_list_shows_what_the_detail_route_shows(condition):
    resources = [profile("ok"), profile("no"), profile("ok"), profile("x", "o")]
    client = conditional_client(condition)
    assert listed(client) == {"ok", "x"}
    readable = {
        r.content["name"] for r in resources if client.get(detail(r)).status_code == 200
    }
    assert readable == {"ok", "x"}


@pytest.mark.django_db
def test_operators_get_a_read_only_mapping_over_the_content():
    ok = profile("ok")
    client = conditional_client(OPERATOR)
    assert client.get(detail(ok)).status_code == 200
    assert listed(client) == {"ok"}
    body = {"name": "ok", "rbac_context": "q"}
    response = client.put(
        detail(ok), body, format="json", HTTP_IF_MATCH=quote_etag(ok.etag)
    )
    assert response.status_code == 200, response.content
    assert SEEN
    for seen in filter(None, SEEN):
        assert isinstance(seen, Mapping), seen
        assert seen.get("name") == "ok"
    # The detail and list object checks give a ResourceCheck (scoped-rbac's
    # request check gives the parsed body itself).
    checks = [seen for seen in SEEN if isinstance(seen, ResourceCheck)]
    assert len(checks) >= 3  # the GET, the list and the PUT
    for check in checks:
        assert isinstance(check.resource, Resource)
        assert check.resource.pk == ok.pk
        with pytest.raises(TypeError):
            check["name"] = "changed"
        with pytest.raises(AttributeError):
            check.name = "changed"


def test_a_resource_check_reads_the_content_and_the_resource():
    resource = Resource(pk=7, content={"name": "ok", "rbac_context": ["a", "b"]})
    check = ResourceCheck(resource)
    assert dict(check) == {"name": "ok", "rbac_context": ["a", "b"]}
    assert check.to_cel() == {"name": "ok", "rbac_context": ["a", "b"]}
    assert check.resource is resource
    # Attribute reads are the resource's: an override written against the
    # Resource (obj.content, obj.rbac_contexts, obj.pk) keeps working.
    assert check.content == resource.content
    assert check.rbac_contexts == ["a", "b"]
    assert check.pk == 7
    with pytest.raises(AttributeError):
        check.content = {}
    # Content that is not a JSON object has no fields.
    assert dict(ResourceCheck(Resource(content=["x"]))) == {}
    assert ResourceCheck(Resource(content="x")).to_cel() == {}


@pytest.mark.django_db
def test_every_object_check_gives_the_permission_class_one_kind_of_object():
    # tests/urls.py: RecordingIsAuthorized records the obj each of its
    # has_object_permission and has_context_permission calls is given. The
    # detail route's object check and the post-save check give the same
    # kind: a ResourceCheck over the resource. (tests.urls registers the
    # resource types: imported here, with database access.)
    from .urls import RecordingIsAuthorized

    RecordingIsAuthorized.SEEN.clear()
    reverse("partner-profiles-list")
    writer = client_with(
        (
            {
                method: [PP, PP_LIST]
                for method in ("http.GET", "http.POST", "http.PUT", "http.DELETE")
            },
            "q",
        )
    )
    response = writer.post(
        reverse("recording-partner-profiles-list"),
        {"name": "x", "rbac_context": "q"},
        format="json",
    )
    assert response.status_code == 201, response.content
    created = Resource.objects.latest("pk")
    response = writer.put(
        reverse("recording-partner-profiles-detail", args=[created.pk]),
        {"name": "y", "rbac_context": "q"},
        format="json",
        HTTP_IF_MATCH=quote_etag(created.etag),
    )
    assert response.status_code == 200, response.content
    # The workflow check gives TransitionChecks (the transition a write
    # makes); every other object check a ResourceCheck.
    seen = [
        (method, obj)
        for method, obj in RecordingIsAuthorized.SEEN
        if not isinstance(obj, TransitionCheck)
    ]
    assert {method for method, _ in seen} == {
        "has_object_permission",
        "has_context_permission",
    }
    for method, obj in seen:
        assert isinstance(obj, ResourceCheck), (method, obj)
        assert obj.resource.pk == created.pk
    # The post-save checks saw what was stored ("x", then "y"), the PUT's
    # object check the stored "x".
    assert {obj["name"] for _, obj in seen} == {"x", "y"}


@pytest.mark.django_db
@pytest.mark.parametrize("condition", [CEL, OPERATOR], ids=["cel", "operator"])
def test_a_list_by_overridden_contexts_shows_what_the_detail_route_shows(condition):
    # tests/urls.py: TeamIsAuthorized's contexts are a resource's "team".
    # GET outright in "red", only by `condition` in "blue".
    reverse("partner-profiles-list")

    def member(name, team):
        return ResourceRegistration.register(
            {"name": name, "team": team}, "partner-profiles"
        )

    resources = [
        member("ok", "red"),
        member("no", "red"),
        member("ok", "blue"),
        member("no", "blue"),
        member("ok", "green"),
    ]
    client = client_with(
        ({"http.GET": [PP, PP_LIST]}, "red"), ({"http.GET": {PP: condition}}, "blue")
    )
    response = client.get(reverse("teams-partner-profiles-list"))
    assert response.status_code == 200, response.content
    listed_ = {
        (entry["content"]["name"], entry["content"]["team"])
        for entry in response.json()["content"]
    }
    expected = {("ok", "red"), ("no", "red"), ("ok", "blue")}
    assert listed_ == expected
    readable = {
        (r.content["name"], r.content["team"])
        for r in resources
        if client.get(detail(r, "teams-partner-profiles")).status_code == 200
    }
    assert readable == expected
