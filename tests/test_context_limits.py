"""A context is at most 2048 bytes long in UTF-8, so that its
``ResourceContext`` and ``ResourceRemoval`` rows fit PostgreSQL's btree
indexes (about 2700 bytes an entry): a longer one is refused (400 through the
API, ``ValueError`` from ``Resource.save()``), never a database error (a
500). A resource stored before 0.12.0 with a longer context is kept as it
is: migration 0008 gives it no row for that context (lists leave it out but
for superusers), its deletion stamps ``SOME_CONTEXT`` for it, and the
``scrud_django.W003`` database check reports it."""

import importlib
import json
from uuid import uuid4

import pytest
from django.apps import apps as global_apps
from django.core import checks
from django.db import connection
from django.urls import reverse
from django.utils.timezone import now
from rest_framework.test import APIClient
from scoped_rbac.rbac_contexts import SOME_CONTEXT

from scrud_django.models import Resource, ResourceContext, ResourceRemoval
from scrud_django.registration import ResourceRegistration, get_resource_type_for

from .factories import UserFactory


def cjk(count):
    """`count` CJK ideographs (3 bytes each in UTF-8) that do not repeat in
    a pattern PostgreSQL's index compression would shrink."""
    return "".join(chr(0x4E00 + (i * 7919) % 20000) for i in range(count))


# 1000 characters, 3000 bytes in UTF-8: within the old 2048-character limit.
WIDE = cjk(1000)
# 2048 bytes exactly.
WIDEST = cjk(682) + "ab"


def rows_of(resource):
    return set(
        ResourceContext.objects.filter(resource=resource).values_list(
            "rbac_context", flat=True
        )
    )


def register(context):
    reverse("partner-profiles-list")
    return ResourceRegistration.register(
        {"name": "n", "rbac_context": context}, "partner-profiles"
    )


def legacy(context):
    """A resource stored before 0.12.0: no checks, no context rows."""
    reverse("partner-profiles-list")
    (resource,) = Resource.objects.bulk_create(
        [
            Resource(
                resource_type=get_resource_type_for("partner-profiles"),
                content={"name": "legacy", "rbac_context": context},
                etag=uuid4().hex,
                modified_at=now(),
            )
        ]
    )
    return resource


@pytest.mark.django_db
def test_a_context_over_2048_bytes_is_400():
    before = Resource.objects.count()
    client = APIClient()
    client.force_authenticate(UserFactory(is_staff=True, is_superuser=True))
    response = client.post(
        reverse("partner-profiles-list"),
        {"name": "n", "rbac_context": WIDE},
        format="json",
    )
    assert response.status_code == 400, response.content
    body = response.json()
    assert body["type"] == "https://api.openteams.com/http-problem/rbac-context-invalid"
    assert "2048 bytes" in json.dumps(body)
    assert Resource.objects.count() == before


@pytest.mark.django_db
def test_a_context_over_2048_bytes_is_refused_server_side():
    before = Resource.objects.count()
    with pytest.raises(ValueError, match="2048 bytes"):
        register(WIDE)
    with pytest.raises(ValueError, match="2048 bytes"):
        register(["a", WIDE])
    assert Resource.objects.count() == before


@pytest.mark.django_db
def test_a_context_of_2048_bytes_is_stored_and_removed():
    assert len(WIDEST.encode("utf-8")) == 2048
    resource = register(WIDEST)
    assert rows_of(resource) == {WIDEST}
    ResourceRegistration.delete("partner-profiles", resource.pk)
    stamps = ResourceRemoval.objects.filter(resource_type=resource.resource_type)
    assert WIDEST in set(stamps.values_list("rbac_context", flat=True))


def migration_0008():
    return importlib.import_module("scrud_django.migrations.0008_resourcecontext")


class FakeSchemaEditor:
    connection = connection


@pytest.mark.django_db
def test_migration_0008_gives_a_legacy_context_too_long_for_a_row_none():
    wide = legacy([WIDE, "a"])
    too_many_characters = legacy("x" * 3000)
    fits = legacy(WIDEST)
    migration_0008().backfill_resource_contexts(global_apps, FakeSchemaEditor())
    assert rows_of(wide) == {"a"}
    assert rows_of(too_many_characters) == set()
    assert rows_of(fits) == {WIDEST}


@pytest.mark.django_db
def test_a_legacy_resource_with_a_context_too_long_for_a_row_can_be_deleted():
    resource = legacy([WIDE, "a"])
    migration_0008().backfill_resource_contexts(global_apps, FakeSchemaEditor())
    ResourceRegistration.delete("partner-profiles", resource.pk)
    assert not Resource.objects.filter(pk=resource.pk).exists()
    stamps = set(
        ResourceRemoval.objects.filter(
            resource_type=resource.resource_type
        ).values_list("rbac_context", flat=True)
    )
    # The long context is stamped as SOME_CONTEXT (superusers' lists).
    assert {"a", SOME_CONTEXT} <= stamps
    assert WIDE not in stamps


@pytest.mark.django_db
def test_the_database_check_reports_a_context_too_long_for_a_row():
    from scrud_django.checks import check_overlong_contexts

    assert check_overlong_contexts(None, databases=["default"]) == []
    wide = legacy([WIDE, "a"])
    legacy("x" * 3000)
    legacy(WIDEST)  # fits
    found = check_overlong_contexts(None, databases=["default"])
    assert [(m.id, m.level) for m in found] == [("scrud_django.W003", checks.WARNING)]
    assert f"[{wide.pk}, {wide.pk + 1}]" in found[0].msg
    # A database check: `check --database` and `migrate` run it.
    assert found[0] in checks.run_checks(databases=["default"])
    assert found[0] not in checks.run_checks()


@pytest.mark.django_db
def test_loading_a_dumped_resource_whose_context_is_over_2048_bytes_is_refused():
    # A 0.11 dump can hold one (within the old 2048-character limit): the
    # release notes' Upgrading 12 says to shorten it before dumping.
    from django.core import serializers
    from django.db import transaction

    resource = register("a")
    data = json.loads(serializers.serialize("json", [resource]))
    data[0]["fields"]["content"]["rbac_context"] = WIDE
    (loaded,) = serializers.deserialize("json", json.dumps(data))
    with pytest.raises(ValueError, match="2048 bytes"), transaction.atomic():
        loaded.save()
    resource.refresh_from_db()
    assert resource.content["rbac_context"] == "a"
