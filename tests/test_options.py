"""OPTIONS under scrudful 0.10 (Semantic HTTP 0.1 at ``37ec1ce``): a 204
without content, linking the path's HTTP Schema Resource (HSR), served by GET,
and an ``Allow`` of the methods this requester may use.

scrudful decides ``Allow`` by checking each method on a body-less copy of the
OPTIONS (``scoped_rbac.access.checks_allow``): scrud's body checks, which
refuse an empty POST or PUT with 400, must leave those copies alone, or the
OPTIONS of anyone who may write is that 400."""

from uuid import uuid4

import pytest
from django.urls import reverse
from django.utils.timezone import now
from rest_framework import status
from rest_framework.test import APIClient
from safetydance_django.test import *  # noqa: F403, F401
from scoped_rbac.models import Role, RoleAssignment
from scoped_rbac.rbac_contexts import DEFAULT_CONTEXT

from scrud_django.registration import ResourceRegistration

from .factories import UserFactory
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


def user_granted(*methods, iris=(JSON_LD, JSON_LD_LIST)):
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
    client.force_authenticate(user)
    return client


def assert_options_204(response):
    assert response.status_code == status.HTTP_204_NO_CONTENT, response.content
    assert response.content == b""
    assert "Content-Type" not in response


@pytest.mark.django_db
def test_options_of_a_list_an_anonymous_user_may_post_to_is_204():
    # tests.rbac_defaults lets the anonymous POST partner profiles (and not
    # GET them): the POST copy has no body, and is not the empty body's 400.
    response = APIClient().options(reverse("partner-profiles-list"))
    assert_options_204(response)
    assert allowed(response) == {"OPTIONS", "POST"}
    assert hsr_link(response) == "http://testserver/_hsr/partner-profiles-list"


@pytest.mark.django_db
def test_options_lists_the_writes_of_a_writer():
    reverse("json-ld-list")  # importing tests.urls registers the types
    stored = ResourceRegistration.register({"name": "kept"}, "json-ld")
    client = user_granted("http.GET", "http.POST", "http.PUT", "http.DELETE")

    response = client.options(reverse("json-ld-list"))
    assert_options_204(response)
    assert {"GET", "HEAD", "POST", "OPTIONS"} <= allowed(response)

    response = client.options(reverse("json-ld-detail", args=[stored.pk]))
    assert_options_204(response)
    assert {"GET", "HEAD", "PUT", "DELETE", "OPTIONS"} <= allowed(response)
    assert "POST" not in allowed(response)


@pytest.mark.django_db
def test_options_of_a_reader_lists_no_write():
    reverse("json-ld-list")
    stored = ResourceRegistration.register({"name": "kept"}, "json-ld")
    client = user_granted("http.GET")

    response = client.options(reverse("json-ld-list"))
    assert_options_204(response)
    assert allowed(response) == {"GET", "HEAD", "OPTIONS"}

    response = client.options(reverse("json-ld-detail", args=[stored.pk]))
    assert_options_204(response)
    assert allowed(response) == {"GET", "HEAD", "OPTIONS"}


@pytest.mark.django_db
def test_a_real_empty_post_is_still_400():
    # Only scrudful's copy of an OPTIONS skips the body checks.
    reverse("json-ld-list")
    client = user_granted("http.POST")
    response = client.post(reverse("json-ld-list"), content_type="application/json")
    assert response.status_code == status.HTTP_400_BAD_REQUEST, response.content


@pytest.mark.django_db
def test_the_hsr_is_served_by_get_and_is_the_same_for_everyone():
    url = hsr_link(APIClient().options(reverse("partner-profiles-list")))
    anonymous = APIClient().get(url)
    assert anonymous.status_code == status.HTTP_200_OK, anonymous.content
    assert anonymous["Content-Type"] == "application/json"
    superuser = APIClient()
    superuser.force_authenticate(UserFactory(is_superuser=True, is_staff=True))
    assert superuser.get(url).content == anonymous.content
    hsr = anonymous.json()
    assert hsr["options"] == {"responses": {"204": {"description": "No Content"}}}
    for method in ("get", "post"):
        assert method in hsr, sorted(hsr)
