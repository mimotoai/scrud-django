"""Object checks read the stored resource's content, the ``dict`` itself
(`content_of`), as the workflow check reads the body: a CEL condition reads
its fields, an adopter's operator and the permission class get the
``dict``, on the detail route (GET, PUT, DELETE), in the post-save check and
in a list's per-object check alike, so a list shows what the detail route
shows."""

from uuid import uuid4

import pytest
from django.conf import settings
from django.test import override_settings
from django.urls import reverse
from django.utils.http import quote_etag
from django.utils.timezone import now
from rest_framework.test import APIClient
from scoped_rbac.models import Role, RoleAssignment

from scrud_django.mixins.resource_mixin import rbac_contexts_of
from scrud_django.models import Resource
from scrud_django.permissions import content_of, resource_contexts
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
def test_operators_get_the_content_as_a_plain_dict():
    ok = profile("ok")
    client = conditional_client(OPERATOR)

    def received():
        """The objects the operator got since the last call (``None``, from
        a request check without a body, aside)."""
        objects = [obj for obj in SEEN if obj is not None]
        SEEN.clear()
        for obj in objects:
            assert type(obj) is dict, type(obj)
            assert obj == ok.content
        return objects

    # A GET's and a list's request checks have no body (None): every object
    # received comes from the detail route's object check, then from the
    # list's per-object check.
    assert client.get(detail(ok)).status_code == 200
    assert len(received()) >= 1
    assert listed(client) == {"ok"}
    assert len(received()) >= 1
    # A PUT: the request check's body (one call, in "q"), then the object
    # check's and the post-save check's content (one call, then two).
    body = {"name": "ok", "rbac_context": "q"}
    response = client.put(
        detail(ok), body, format="json", HTTP_IF_MATCH=quote_etag(ok.etag)
    )
    assert response.status_code == 200, response.content
    assert len(received()) >= 4


def test_an_object_check_gets_the_stored_content_itself():
    content = {
        "name": "ok",
        "nested": {"k": 1, "list": [1, {"x": 2}]},
        "rbac_context": ["a", "b"],
    }
    resource = Resource(pk=7, content=content)
    checked = content_of(resource)
    # The dict the resource holds, not a copy or a view: plain dicts and
    # lists at every level, which CEL reads.
    assert checked is resource.content
    assert type(checked["nested"]) is dict and type(checked["nested"]["list"]) is list
    # Its contexts are the resource's (scoped-rbac's default contexts_for
    # reads them so on the scrud views).
    assert rbac_contexts_of(checked) == resource.rbac_contexts == ["a", "b"]
    # Content that is not a JSON object has no fields.
    assert content_of(Resource(content=["x"])) == {}
    assert content_of(Resource(content="x")) == {}


@pytest.mark.django_db
def test_every_object_check_gives_the_permission_class_one_kind_of_object():
    # tests/urls.py: RecordingIsAuthorized records the obj each of its
    # has_object_permission and has_context_permission calls is given. The
    # detail route's object check and the post-save check give the same
    # kind: the resource's content, a dict. (tests.urls registers the
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
    # Every check gives a plain dict: the object checks the content, the
    # workflow check (none for this type) the body.
    seen = RecordingIsAuthorized.SEEN
    assert {method for method, _ in seen} == {
        "has_object_permission",
        "has_context_permission",
    }
    for method, obj in seen:
        assert type(obj) is dict, (method, obj)
    # The post-save checks saw what was stored ("x", then "y"), the PUT's
    # object check the stored "x".
    assert {obj["name"] for _, obj in seen} == {"x", "y"}
    assert {obj["rbac_context"] for _, obj in seen} == {"q"}


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


@pytest.mark.django_db
def test_a_resource_stored_in_a_context_reserved_once_normalised_is_missing():
    # Its content, which the object check gets, has no rbac_context attribute
    # for scoped-rbac's check_object_permissions to read: get_object answers
    # 404 for it first, to every method, superusers included.
    hidden = profile("hidden", "​__x")
    admin = UserFactory(is_superuser=True, is_staff=True)
    client = APIClient()
    client.force_authenticate(admin)
    assert client.get(detail(hidden)).status_code == 404
    response = client.put(
        detail(hidden),
        {"name": "changed", "rbac_context": "q"},
        format="json",
        HTTP_IF_MATCH=quote_etag(hidden.etag),
    )
    assert response.status_code == 404, response.content
    response = client.delete(detail(hidden), HTTP_IF_MATCH=quote_etag(hidden.etag))
    assert response.status_code == 404, response.content
    assert Resource.objects.get(pk=hidden.pk).content == hidden.content


# The user of the get_permissions pattern tests: GET (and list) in "a" and
# "b", PUT and POST in "a" only.
PATTERN_GRANTS = (
    ({"http.GET": [PP, PP_LIST], "http.PUT": [PP], "http.POST": [PP]}, "a"),
    ({"http.GET": [PP, PP_LIST]}, "b"),
)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "prefix", ["no-super", "per-action", "appending", "shared", "partner-profiles"]
)
def test_every_get_permissions_pattern_reads_the_contents_contexts(prefix):
    # tests/urls.py: a get_permissions that does not call super(), one with a
    # branch per action, one that adds a composed IsAuthorized after super()'s,
    # one that reuses one instance, and the plain view. Each reads a
    # resource's contexts from the content its checks get (the detail
    # route's object and readability checks, the list, the post-save check of
    # each 200 PUT and 201 POST): 200 or 403 as the grants say, never an
    # error.
    from .urls import NoSuperResourceViewSet  # noqa: F401  (registers the routes)

    def path(resource=None):
        if prefix == "partner-profiles":
            name = "partner-profiles"
        else:
            name = f"{prefix}-partner-profiles"
        if resource is None:
            return reverse(f"{name}-list")
        return reverse(f"{name}-detail", args=[resource.pk])

    in_a, in_b = profile("in a", "a"), profile("in b", "b")
    client = client_with(*PATTERN_GRANTS)
    for resource in (in_a, in_b):
        response = client.get(path(resource))
        assert response.status_code == 200, response.content
    assert listed(client, path()) == {"in a", "in b"}
    response = client.put(
        path(in_a),
        {"name": "changed", "rbac_context": "a"},
        format="json",
        HTTP_IF_MATCH=quote_etag(in_a.etag),
    )
    assert response.status_code == 200, response.content
    # Readable in "b", not writable there: the object check refuses, 403.
    response = client.put(
        path(in_b),
        {"name": "changed", "rbac_context": "b"},
        format="json",
        HTTP_IF_MATCH=quote_etag(in_b.etag),
    )
    assert response.status_code == 403, response.content
    # A create: the request check, then the post-save check of the content.
    response = client.post(path(), {"name": "new", "rbac_context": "a"}, format="json")
    assert response.status_code == 201, response.content
    response = client.post(path(), {"name": "new", "rbac_context": "b"}, format="json")
    assert response.status_code == 403, response.content


@pytest.mark.django_db
def test_a_reused_permission_instance_is_not_wrapped_again():
    from .urls import SHARED_IS_AUTHORIZED

    in_a = profile("in a", "a")
    client = client_with(*PATTERN_GRANTS)
    url = reverse("shared-partner-profiles-detail", args=[in_a.pk])
    seen = []
    for _ in range(5):
        assert client.get(url).status_code == 200
        seen.append(vars(SHARED_IS_AUTHORIZED).get("contexts_for"))
    # One contexts_for, the same every time: no wrapper around the last one.
    assert all(contexts_for is resource_contexts for contexts_for in seen), seen


def test_resource_contexts_reads_the_content_or_the_object():
    # A contexts_for of your own calls it where it would call super()'s.
    assert resource_contexts({"rbac_context": ["a", "b"]}) == ["a", "b"]
    assert resource_contexts({}) == [""]
    resource = Resource(content={"rbac_context": "a"})
    assert resource_contexts(resource) == ["a"]
