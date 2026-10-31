"""A POST to a list evaluates its preconditions (RFC 9110 §13.2.2:
``If-Match``, else ``If-Unmodified-Since``, then ``If-None-Match``) against
the list's validators as the requester would GET them (their RBAC-filtered
list's ``ETag`` and ``Last-Modified``): a failed one is a 412 and nothing is
created, a malformed one a 400, as on the detail routes. A POST without
preconditions is unchanged. (scrudful never evaluates a create's
preconditions; scrud does it in ``ResourceViewMixin.create``.)"""

from uuid import uuid4

import pytest
from django.conf import settings
from django.test import override_settings
from django.urls import reverse
from django.utils.http import http_date
from django.utils.timezone import now
from rest_framework.test import APIClient
from scoped_rbac.models import Role, RoleAssignment

from scrud_django.models import Resource
from scrud_django.registration import ResourceRegistration

from .factories import UserFactory
from .rbac_defaults import TYPE_URI_LIST_PARTNER_PROFILES, TYPE_URI_PARTNER_PROFILES

PP = TYPE_URI_PARTNER_PROFILES
PP_LIST = TYPE_URI_LIST_PARTNER_PROFILES
WRITER = {"http.GET": [PP, PP_LIST], "http.POST": [PP, PP_LIST]}
BODY = {"name": "new", "rbac_context": "a"}


def client_with(*grants):
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


def url():
    return reverse("partner-profiles-list")


def profile(name, context="a"):
    url()
    return ResourceRegistration.register(
        {"name": name, "rbac_context": context}, "partner-profiles"
    )


def created():
    return Resource.objects.filter(content__name="new").count()


def post(client, **headers):
    return client.post(url(), BODY, format="json", **headers)


@pytest.fixture
def writer():
    profile("existing")
    return client_with((WRITER, "a"))


@pytest.mark.django_db
def test_if_match_with_the_lists_etag_creates(writer):
    etag = writer.get(url())["ETag"]
    response = post(writer, HTTP_IF_MATCH=etag)
    assert response.status_code == 201, response.content
    assert created() == 1


@pytest.mark.django_db
def test_if_match_with_another_etag_is_412_and_creates_nothing(writer):
    response = post(writer, HTTP_IF_MATCH='"nope"')
    assert response.status_code == 412, response.content
    assert response["Content-Type"].startswith("application/problem+json")
    problem = response.json()
    assert (problem["status"], problem["code"]) == (412, "precondition_failed")
    assert "problem-details.schema.json" in response["Link"], response["Link"]
    assert created() == 0


@pytest.mark.django_db
def test_if_match_with_a_stale_etag_is_412(writer):
    etag = writer.get(url())["ETag"]
    profile("another")
    response = post(writer, HTTP_IF_MATCH=etag)
    assert response.status_code == 412, response.content
    assert created() == 0


@pytest.mark.django_db
def test_if_match_any_creates(writer):
    response = post(writer, HTTP_IF_MATCH="*")
    assert response.status_code == 201, response.content


@pytest.mark.django_db
def test_if_unmodified_since_is_evaluated(writer):
    last_modified = writer.get(url())["Last-Modified"]
    earlier = http_date(
        Resource.objects.get(content__name="existing").modified_at.timestamp() - 60
    )
    response = post(writer, HTTP_IF_UNMODIFIED_SINCE=earlier)
    assert response.status_code == 412, response.content
    assert created() == 0
    response = post(writer, HTTP_IF_UNMODIFIED_SINCE=last_modified)
    assert response.status_code == 201, response.content


@pytest.mark.django_db
def test_if_none_match_on_the_existing_list_is_412(writer):
    etag = writer.get(url())["ETag"]
    for value in ("*", etag):
        response = post(writer, HTTP_IF_NONE_MATCH=value)
        assert response.status_code == 412, (value, response.content)
    assert created() == 0
    response = post(writer, HTTP_IF_NONE_MATCH='"other"')
    assert response.status_code == 201, response.content


@pytest.mark.django_db
def test_a_malformed_if_match_is_400(writer):
    response = post(writer, HTTP_IF_MATCH="nope")
    assert response.status_code == 400, response.content
    assert response.json()["code"] == "malformed_precondition"
    assert created() == 0


@pytest.mark.django_db
def test_the_validators_are_the_requesters_list(writer):
    # A write the requester cannot see does not change their list's ETag:
    # their If-Match still passes.
    other = profile("elsewhere", context="b")
    etag = writer.get(url())["ETag"]
    ResourceRegistration.update(
        {"name": "elsewhere, changed", "rbac_context": "b"},
        "partner-profiles",
        other.pk,
    )
    response = post(writer, HTTP_IF_MATCH=etag)
    assert response.status_code == 201, response.content


@pytest.mark.django_db
def test_without_uniform_errors_a_412_is_scrudfuls_native_body(writer):
    with override_settings(
        SCOPED_RBAC=dict(settings.SCOPED_RBAC, UNIFORM_ERRORS=False)
    ):
        response = post(writer, HTTP_IF_MATCH='"nope"')
    assert response.status_code == 412, response.content
    assert response["Content-Type"].startswith("application/json")
    assert response.json() == {"detail": "Precondition failed."}


@pytest.mark.django_db
def test_a_create_without_preconditions_is_unchanged(writer):
    response = post(writer)
    assert response.status_code == 201, response.content
    assert response.has_header("ETag") and response.has_header("Location")


@pytest.mark.django_db
def test_authorization_comes_before_preconditions():
    profile("existing")
    reader = client_with(({"http.GET": [PP, PP_LIST]}, "a"))
    response = post(reader, HTTP_IF_MATCH='"nope"')
    assert response.status_code == 403, response.content


# Past SCOPED_RBAC["CONDITIONAL_LIST_MAX_ROWS"] the list's validators cannot be
# computed (a GET of the list is a 400 conditional_list_too_large). A
# precondition that needs them gets that 400 too; one that does not (an
# ``If-Match: *`` or ``If-None-Match: *``, decided by the list existing) is
# evaluated without them.

CONDITIONAL_WRITER = {
    "http.GET": {PP: {"condition": 'name != "zzz"'}, PP_LIST: True},
    "http.POST": [PP, PP_LIST],
}


@pytest.fixture
def capped_writer():
    for name in ("one", "two", "three"):
        profile(name)
    capped = dict(settings.SCOPED_RBAC, CONDITIONAL_LIST_MAX_ROWS=2)
    with override_settings(SCOPED_RBAC=capped):
        client = client_with((CONDITIONAL_WRITER, "a"))
        response = client.get(url())
        assert response.status_code == 400, response.content
        assert response.json()["code"] == "conditional_list_too_large"
        yield client


@pytest.mark.django_db
def test_if_match_any_past_the_cap_creates_as_an_unconditional_post(capped_writer):
    assert post(capped_writer).status_code == 201
    response = post(capped_writer, HTTP_IF_MATCH="*")
    assert response.status_code == 201, response.content
    assert created() == 2


@pytest.mark.django_db
def test_if_none_match_any_past_the_cap_is_412(capped_writer):
    response = post(capped_writer, HTTP_IF_NONE_MATCH="*")
    assert response.status_code == 412, response.content
    assert created() == 0


@pytest.mark.django_db
def test_a_precondition_that_needs_the_validators_past_the_cap_is_400(capped_writer):
    for headers in (
        {"HTTP_IF_MATCH": '"x"'},
        {"HTTP_IF_NONE_MATCH": '"x"'},
        {"HTTP_IF_UNMODIFIED_SINCE": "Thu, 01 Jan 2099 00:00:00 GMT"},
        {"HTTP_IF_MATCH": "*", "HTTP_IF_NONE_MATCH": '"x"'},
    ):
        response = post(capped_writer, **headers)
        assert response.status_code == 400, (headers, response.content)
        assert response.json()["code"] == "conditional_list_too_large", headers
    assert created() == 0
