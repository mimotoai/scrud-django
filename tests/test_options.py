"""OPTIONS under scrudful 0.10 (Semantic HTTP 0.1 at ``37ec1ce``): a 204
without content, linking the path's HTTP Schema Resource (HSR), served by GET,
and an ``Allow`` of the methods this requester may use.

scrudful decides ``Allow`` by checking each method on a body-less copy of the
OPTIONS (``scoped_rbac.access.checks_allow``): scrud's body checks, which
refuse an empty POST or PUT with 400, must leave those copies alone, or
every OPTIONS of a route serving POST or PUT is that 400. A request that
arrives looking like such a copy (an OPTIONS whose ``request.method``
middleware rewrote) is refused."""

from uuid import uuid4

import pytest
from django.test import RequestFactory, override_settings
from django.urls import reverse
from django.utils.timezone import now
from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from rest_framework.request import Request
from rest_framework.test import APIClient
from rest_framework.views import APIView
from safetydance_django.test import *  # noqa: F403, F401
from scoped_rbac.models import Role, RoleAssignment
from scoped_rbac.rbac_contexts import DEFAULT_CONTEXT

from scrud_django.mixins.view_mixins import LockedWriteMixin
from scrud_django.models import Resource
from scrud_django.registration import ResourceRegistration

from .factories import UserFactory
from .front_ends import api_url, hsr_url
from .models import Note
from .steps import *  # noqa: F403, F401

HSR_REL = "https://w3id.org/semantic-http/ns#http-schema-resource-document"
JSON_LD = "http://www.w3.org/ns/json-ld#context"
JSON_LD_LIST = (
    f"https://api.openteams.com/json-schema/ResourceCollection?contents_type={JSON_LD}"
)


def allowed(response):
    return {m.strip() for m in response["Allow"].split(",")}


def hsr_link(response):
    """The HSR link's target: the one Link with the HSR's rel."""
    links = [
        link for link in response["Link"].split(", ") if f'rel="{HSR_REL}"' in link
    ]
    assert len(links) == 1, response["Link"]
    return links[0].split(";")[0].strip("<>")


def user_granted(*methods, iris=(JSON_LD, JSON_LD_LIST), session=True):
    """A client of a user granted `methods` on `iris` in the default
    context, logged in through the session (served by both front ends), or
    with DRF's ``force_authenticate`` unless `session`."""
    role = Role(
        definition={m: list(iris) for m in methods},
        etag=uuid4().hex,
        modified_at=now(),
    )
    role.save()
    user = UserFactory()
    RoleAssignment(
        user=user,
        role=role,
        rbac_context=DEFAULT_CONTEXT,
        etag=uuid4().hex,
        modified_at=now(),
    ).save()
    client = APIClient()
    if session:
        client.force_login(user)
    else:
        client.force_authenticate(user)
    return client


def assert_options_204(response):
    assert response.status_code == status.HTTP_204_NO_CONTENT, response.content
    assert response.content == b""
    assert "Content-Type" not in response


@pytest.mark.django_db
def test_options_of_a_list_an_anonymous_user_may_post_to_is_204(front_end):
    # tests.rbac_defaults lets the anonymous POST partner profiles (and not
    # GET them): the POST copy has no body, and is not the empty body's 400.
    response = APIClient().options(api_url(front_end, "partner-profiles-list"))
    assert_options_204(response)
    assert allowed(response) == {"OPTIONS", "POST"}
    assert hsr_link(response) == hsr_url(front_end, "partner-profiles-list")


@pytest.mark.django_db
def test_options_lists_the_writes_of_a_writer(front_end):
    # Its URLconf registers the types (tests.urls on import, serve_ninja).
    api_url(front_end, "json-ld-list")
    stored = ResourceRegistration.register({"name": "kept"}, "json-ld")
    client = user_granted("http.GET", "http.POST", "http.PUT", "http.DELETE")

    response = client.options(api_url(front_end, "json-ld-list"))
    assert_options_204(response)
    assert {"GET", "HEAD", "POST", "OPTIONS"} <= allowed(response)

    response = client.options(api_url(front_end, "json-ld-detail", args=[stored.pk]))
    assert_options_204(response)
    assert {"GET", "HEAD", "PUT", "DELETE", "OPTIONS"} <= allowed(response)
    assert "POST" not in allowed(response)


@pytest.mark.django_db
def test_options_of_a_reader_lists_no_write(front_end):
    api_url(front_end, "json-ld-list")
    stored = ResourceRegistration.register({"name": "kept"}, "json-ld")
    client = user_granted("http.GET")

    response = client.options(api_url(front_end, "json-ld-list"))
    assert_options_204(response)
    assert allowed(response) == {"GET", "HEAD", "OPTIONS"}

    response = client.options(api_url(front_end, "json-ld-detail", args=[stored.pk]))
    assert_options_204(response)
    assert allowed(response) == {"GET", "HEAD", "OPTIONS"}


@pytest.mark.django_db
def test_a_real_empty_post_is_still_400(front_end):
    # Only scrudful's copy of an OPTIONS skips the body checks.
    api_url(front_end, "json-ld-list")
    client = user_granted("http.POST")
    response = client.post(
        api_url(front_end, "json-ld-list"), content_type="application/json"
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST, response.content


@pytest.mark.django_db
def test_the_hsr_is_served_by_get_and_is_the_same_for_everyone(front_end):
    url = hsr_link(APIClient().options(api_url(front_end, "partner-profiles-list")))
    anonymous = APIClient().get(url)
    assert anonymous.status_code == status.HTTP_200_OK, anonymous.content
    assert anonymous["Content-Type"] == "application/json"
    superuser = APIClient()
    superuser.force_login(UserFactory(is_superuser=True, is_staff=True))
    assert superuser.get(url).content == anonymous.content
    hsr = anonymous.json()
    assert hsr["options"] == {"responses": {"204": {"description": "No Content"}}}
    for method in ("get", "post"):
        assert method in hsr, sorted(hsr)


@pytest.mark.django_db
def test_options_of_a_write_only_user_lists_only_their_write(front_end):
    api_url(front_end, "json-ld-list")
    stored = ResourceRegistration.register({"name": "kept"}, "json-ld")
    response = user_granted("http.PUT").options(
        api_url(front_end, "json-ld-detail", args=[stored.pk])
    )
    assert_options_204(response)
    assert allowed(response) == {"PUT", "OPTIONS"}


@pytest.mark.django_db
def test_the_hsr_of_a_detail_route_describes_its_methods(front_end):
    api_url(front_end, "json-ld-list")
    stored = ResourceRegistration.register({"name": "kept"}, "json-ld")
    url = hsr_link(
        APIClient().options(api_url(front_end, "json-ld-detail", args=[stored.pk]))
    )
    assert url == hsr_url(front_end, "json-ld-detail")
    response = APIClient().get(url)
    assert response.status_code == status.HTTP_200_OK, response.content
    assert response["Content-Type"] == "application/json"
    hsr = response.json()
    assert hsr["options"] == {"responses": {"204": {"description": "No Content"}}}
    for method in ("get", "put", "delete"):
        assert method in hsr, sorted(hsr)
    assert "post" not in hsr


# DRF-only: generic-notes/ is a DRF GenericViewMixin viewset of tests/urls.py.
@pytest.mark.django_db
def test_options_and_the_hsr_of_a_generic_view():
    # tests.urls.NoteViewSet, a GenericViewMixin view of a model of its own.
    reverse("generic-notes-list")
    note = Note.objects.create(text="x", etag=uuid4().hex, modified_at=now())
    client = APIClient()
    client.force_authenticate(UserFactory(is_superuser=True, is_staff=True))
    for route, methods in (
        ("generic-notes-list", {"get", "head", "post"}),
        ("generic-notes-detail", {"get", "put", "delete"}),
    ):
        args = [] if route.endswith("list") else [note.pk]
        response = client.options(reverse(route, args=args))
        assert_options_204(response)
        assert {m.upper() for m in methods} | {"OPTIONS"} <= allowed(response)
        url = hsr_link(response)
        assert url == f"http://testserver/_hsr/{route}"
        hsr = APIClient().get(url)
        assert hsr.status_code == status.HTTP_200_OK, hsr.content
        assert methods | {"options"} <= set(hsr.json()), sorted(hsr.json())


# An OPTIONS whose ``request.method`` middleware rewrote (tests.middleware)
# arrives looking like scrudful's body-less copy of an OPTIONS, on which the
# body checks are skipped: it is refused (403, 401 for the anonymous) and
# writes nothing.
REWRITING = override_settings(
    MIDDLEWARE=(
        "django.contrib.sessions.middleware.SessionMiddleware",
        "django.contrib.auth.middleware.AuthenticationMiddleware",
        "tests.middleware.MethodOverrideMiddleware",
    )
)


# DRF-only: the rewriting tests run DRF's views under a middleware of their
# own (ninja's refusal is pinned by test_ninja_api).
@pytest.mark.django_db
@REWRITING
def test_an_options_rewritten_to_a_post_is_refused():
    reverse("json-ld-list")
    before = Resource.objects.count()
    response = user_granted("http.POST", session=False).options(
        reverse("json-ld-list"), HTTP_X_HTTP_METHOD_OVERRIDE="POST"
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN, response.content
    assert Resource.objects.count() == before


# DRF-only: as above.
@pytest.mark.django_db
@REWRITING
def test_an_anonymous_options_rewritten_to_a_post_is_401():
    # tests.rbac_defaults lets the anonymous POST partner profiles.
    before = Resource.objects.count()
    response = APIClient().options(
        reverse("partner-profiles-list"), HTTP_X_HTTP_METHOD_OVERRIDE="POST"
    )
    assert response.status_code == status.HTTP_401_UNAUTHORIZED, response.content
    assert Resource.objects.count() == before


# DRF-only: a DRF view class's initial().
@pytest.mark.django_db
def test_scrud_refuses_a_rewritten_options_without_scoped_rbacs_view():
    # LockedWriteMixin.initial refuses it itself, so that a view whose bases
    # leave out (or put after it) scoped-rbac's AccessControlledAPIView, which
    # refuses it too, never runs the body checks' skip on it.
    class View(LockedWriteMixin, APIView):
        pass

    django_request = RequestFactory().options("/")
    django_request.method = "POST"
    request = Request(django_request)  # no authenticator: no user
    with pytest.raises(PermissionDenied, match="request line's OPTIONS"):
        View().initial(request)
