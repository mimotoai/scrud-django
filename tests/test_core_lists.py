"""A list without DRF (`scrud_django.core.lists`): the resources a requester
may list, filtered on query parameters, and the list's validators and
preconditions, asked with a plain Django request and a neutral requirement,
as the DRF routes answer them."""

import datetime
from uuid import uuid4

import pytest
from django.conf import settings
from django.http import QueryDict
from django.test import RequestFactory, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.http import quote_etag
from rest_framework.test import APIClient
from safetydance_django.test import *  # noqa: F403, F401
from scoped_rbac import access
from scoped_rbac.models import Role, RoleAssignment

from scrud_django.core.requirements import GrantsLeaf
from scrud_django.exceptions import ScrudException
from scrud_django.filters import ui_parameters
from scrud_django.models import Resource, ResourceRemoval
from scrud_django.registration import ResourceRegistration, get_resource_type_for

from .factories import UserFactory
from .rbac_defaults import TYPE_URI_LIST_PARTNER_PROFILES, TYPE_URI_PARTNER_PROFILES
from .steps import *  # noqa: F403, F401

PP = TYPE_URI_PARTNER_PROFILES
PP_READER = {"http.GET": [PP, TYPE_URI_LIST_PARTNER_PROFILES]}
AN_HOUR_AGO = datetime.timedelta(hours=1)


def user_with(*grants):
    """A user holding each ``(role definition, context)`` of `grants`."""
    user = UserFactory()
    for definition, context in grants:
        role = Role(definition=definition, etag=uuid4().hex, modified_at=timezone.now())
        role.save()
        RoleAssignment(
            user=user,
            role=role,
            rbac_context=context,
            etag=uuid4().hex,
            modified_at=timezone.now(),
        ).save()
    return user


def profile(name, context):
    """A partner profile in `context`, stored an hour ago."""
    reverse("partner-profiles-list")
    resource = ResourceRegistration.register(
        {"name": name, "rbac_context": context}, "partner-profiles"
    )
    Resource.objects.filter(pk=resource.pk).update(
        modified_at=timezone.now() - AN_HOUR_AGO
    )
    resource.refresh_from_db()
    return resource


def get_request(user, path="/"):
    request = RequestFactory().get(path)
    request.user = user
    return request


class RefusingLeaf(GrantsLeaf):
    """A leaf whose object check refuses the resource named `name`."""

    overrides_object_checks = True

    def __init__(self, type_iri, name):
        super().__init__(type_iri)
        self.name = name

    def has_object_permission(self, request, obj):
        return obj.get("name") != self.name


@pytest.mark.django_db
@test_context
def test_list_etag_matches_drf():
    from scrud_django.core.lists import list_etag

    for n in range(3):
        profile(f"seen-{n}", "a")
    for n in range(2):
        profile(f"hidden-{n}", "b")
    client = APIClient()
    client.force_authenticate(user_with((PP_READER, "a")))
    response = client.get(reverse("partner-profiles-list") + "?page=2&page_size=1")
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["count"] == 3
    (entry,) = body["content"]
    item = Resource.objects.get(content__name=entry["content"]["name"])
    rtype = get_resource_type_for("partner-profiles")
    expected = list_etag(
        rtype, items=[item], count=3, query=[("page", ["2"]), ("page_size", ["1"])]
    )
    assert response["ETag"] == quote_etag(expected)


@pytest.mark.django_db
@test_context
def test_readable_queryset_object_checks():
    from scrud_django.core.lists import readable_queryset

    refused, shown = profile("refused", "a"), profile("shown", "a")
    profile("elsewhere", "b")
    rtype = get_resource_type_for("partner-profiles")
    request = get_request(user_with((PP_READER, "a")))
    # The grants alone: what the RBAC filter lets through.
    plain = readable_queryset(request, rtype, [GrantsLeaf(PP)])
    assert sorted(plain.values_list("pk", flat=True)) == sorted([refused.pk, shown.pk])
    requirement = [GrantsLeaf(PP), RefusingLeaf(PP, "refused")]
    listed = readable_queryset(request, rtype, requirement)
    assert list(listed.values_list("pk", flat=True)) == [shown.pk]
    # The refused resource was examined: it counts against the cap.
    capped = dict(settings.SCOPED_RBAC, CONDITIONAL_LIST_MAX_ROWS=1)
    with override_settings(SCOPED_RBAC=capped):
        with pytest.raises(access.ConditionalListTooLarge):
            readable_queryset(request, rtype, requirement)
        # Without object checks the outright grants list in SQL, uncapped.
        assert readable_queryset(request, rtype, [GrantsLeaf(PP)]).count() == 2


@pytest.mark.django_db
@test_context
def test_content_filtered_ignores_paging_and_ui():
    from scrud_django.core.lists import content_filtered

    reverse("json-ld-list")
    a = ResourceRegistration.register({"text": "a", "rbac_context": "a"}, "json-ld")
    ResourceRegistration.register({"text": "b", "rbac_context": "a"}, "json-ld")
    queryset = Resource.objects.filter(resource_type=a.resource_type)
    ignored = frozenset({"page", "page_size", "format"}) | ui_parameters()
    assert "ui-mode" in ignored
    params = QueryDict("page=1&ui-mode=edit&text=a")
    filtered = content_filtered(queryset, params, ignored=ignored)
    assert list(filtered.values_list("pk", flat=True)) == [a.pk]
    # An integer key is a 400, as the DRF filter answers it.
    with pytest.raises(ScrudException) as raised:
        content_filtered(queryset, QueryDict("1=x"), ignored=ignored)
    assert raised.value.status_code == 400
    assert raised.value.errors == {
        "1": ["An integer is not a content key a list can be filtered on."]
    }


@pytest.mark.django_db
@test_context
def test_list_last_modified_includes_removal():
    from scrud_django.core.lists import list_last_modified, readable_queryset

    older, gone = profile("older", "a"), profile("gone", "a")
    Resource.objects.filter(pk=older.pk).update(
        modified_at=timezone.now() - 2 * AN_HOUR_AGO
    )
    rtype = get_resource_type_for("partner-profiles")
    request = get_request(user_with((PP_READER, "a")))
    requirement = [GrantsLeaf(PP)]

    def last_modified():
        queryset = readable_queryset(request, rtype, requirement)
        return list_last_modified(request, rtype, queryset, requirement)

    before = last_modified()
    gone.refresh_from_db()
    assert before == gone.modified_at
    ResourceRegistration.delete("partner-profiles", gone.pk)
    after = last_modified()
    removal = ResourceRemoval.objects.filter(resource_type=rtype).get()
    assert after == removal.removed_at > before
    # A removal in a context the requester does not read does not move it.
    other = get_request(user_with((PP_READER, "b")))
    assert (
        list_last_modified(
            other, rtype, readable_queryset(other, rtype, requirement), requirement
        )
        is None
    )


@pytest.mark.django_db
@test_context
def test_evaluate_list_preconditions_reads_validators_only_when_needed():
    from scrud_django.core.lists import evaluate_list_preconditions

    calls = []

    def validators():
        calls.append(1)
        return "abc", None

    # No list precondition: nothing to evaluate.
    assert evaluate_list_preconditions("POST", {}, validators) is None
    # "*" is decided by the list existing, without its validators.
    assert evaluate_list_preconditions("POST", {"if-match": "*"}, validators) is None
    failed = evaluate_list_preconditions("POST", {"if-none-match": "*"}, validators)
    assert failed.status == 412
    assert calls == []
    # An entity-tag needs them.
    assert (
        evaluate_list_preconditions("POST", {"if-match": '"abc"'}, validators) is None
    )
    stale = evaluate_list_preconditions("POST", {"if-match": '"old"'}, validators)
    assert stale.status == 412
    assert calls == [1, 1]
