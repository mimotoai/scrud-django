"""Contexts match exactly, code point for code point, on every database: a
list shows a resource in context ``"acme"`` only to a requester granted
there, never in ``"ACME"``, ``"acmé"`` or ``"acme "``, which the detail
route tells apart. MySQL and MariaDB compare text under a collation (their
defaults are case- and accent-insensitive, and pad with spaces), so the
``ResourceContext`` and ``ResourceRemoval`` contexts are stored there as
UTF-8 bytes (``VARBINARY``) and compared byte for byte."""

import datetime
from uuid import uuid4

import pytest
from django.urls import reverse
from django.utils import timezone
from django.utils.http import parse_http_date
from rest_framework.test import APIClient
from scoped_rbac.models import Role, RoleAssignment

from scrud_django.models import Resource, ResourceContext, ResourceRemoval
from scrud_django.registration import ResourceRegistration
from scrud_django.workflow import WORKFLOW_POLICY_TYPE_URI, stored_policies_for

from .factories import UserFactory
from .rbac_defaults import TYPE_URI_LIST_PARTNER_PROFILES, TYPE_URI_PARTNER_PROFILES

READER = {"http.GET": [TYPE_URI_PARTNER_PROFILES, TYPE_URI_LIST_PARTNER_PROFILES]}

# Contexts each of which a case- or accent-insensitive collation, or one
# that pads with spaces, takes for "acme" (or for one another).
VARIANTS = [
    "acme",
    "ACME",
    "Acme",
    "acmé",
    "ácme",
    "acme ",
    "acmé",  # "acme" and a combining acute accent
    "ａｃｍｅ",  # "acme" in fullwidth letters
]
AN_HOUR_AGO = datetime.timedelta(hours=1)


def reader_of(context):
    user = UserFactory()
    role = Role(definition=READER, etag=uuid4().hex, modified_at=timezone.now())
    role.save()
    RoleAssignment(
        user=user,
        role=role,
        rbac_context=context,
        etag=uuid4().hex,
        modified_at=timezone.now(),
    ).save()
    client = APIClient()
    client.force_authenticate(user)
    return client


def profile(context):
    reverse("partner-profiles-list")
    return ResourceRegistration.register(
        {"name": f"in {context!r}", "rbac_context": context}, "partner-profiles"
    )


def listed(client):
    response = client.get(reverse("partner-profiles-list"), {"page_size": 100})
    assert response.status_code == 200, response.content
    return sorted(
        item["content"]["rbac_context"] for item in response.json()["content"]
    )


@pytest.mark.django_db
def test_a_list_shows_the_resources_of_the_exact_contexts_only():
    profiles = {context: profile(context) for context in VARIANTS}
    for context in VARIANTS:
        client = reader_of(context)
        assert listed(client) == [context]
        for other, resource in profiles.items():
            response = client.get(
                reverse("partner-profiles-detail", args=[resource.pk])
            )
            assert response.status_code == (200 if other == context else 404), (
                context,
                other,
            )


@pytest.mark.django_db
def test_a_resource_can_be_in_contexts_that_differ_only_in_case_or_accents():
    resource = profile(VARIANTS)
    assert set(
        ResourceContext.objects.filter(resource=resource).values_list(
            "rbac_context", flat=True
        )
    ) == set(VARIANTS)
    for context in VARIANTS:
        assert listed(reader_of(context)) == [VARIANTS]


@pytest.mark.django_db
def test_removals_are_stamped_and_read_per_exact_context():
    kept = profile("acme")
    Resource.objects.filter(pk=kept.pk).update(modified_at=timezone.now() - AN_HOUR_AGO)
    gone = [profile(context) for context in VARIANTS[1:]]
    for resource in gone:
        ResourceRegistration.delete("partner-profiles", resource.pk)

    stamps = set(
        ResourceRemoval.objects.filter(
            resource_type__type_uri=TYPE_URI_PARTNER_PROFILES
        ).values_list("rbac_context", flat=True)
    )
    assert stamps == set(VARIANTS[1:])
    # The removals in the other contexts do not move the list of "acme".
    response = reader_of("acme").get(reverse("partner-profiles-list"))
    assert response.status_code == 200, response.content
    last_modified = parse_http_date(response["Last-Modified"])
    assert last_modified < (timezone.now() - AN_HOUR_AGO / 2).timestamp()


@pytest.mark.django_db
def test_a_stored_policy_governs_its_exact_type_only():
    reverse("partner-profiles-list")
    policy = {
        "resource_type": TYPE_URI_PARTNER_PROFILES + " ",
        "initial_states": {"s": "true"},
        "states": {"s": "true"},
        "transitions": {},
        "rbac_context": "a",
    }
    ResourceRegistration.register(policy, "workflow-policies")
    assert Resource.objects.filter(
        resource_type__type_uri=WORKFLOW_POLICY_TYPE_URI
    ).exists()
    assert stored_policies_for(TYPE_URI_PARTNER_PROFILES, "a") == []
    assert len(stored_policies_for(TYPE_URI_PARTNER_PROFILES + " ", "a")) == 1
