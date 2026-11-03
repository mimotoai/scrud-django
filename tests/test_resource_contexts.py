"""A resource's RBAC contexts are also stored as rows (``ResourceContext``),
kept in step with its content by ``Resource.save()``, so that lists can be
filtered by context in SQL."""

import importlib
import json
from copy import deepcopy

import pytest
from django.apps import apps as global_apps
from django.db import connection
from django.urls import reverse
from rest_framework import status
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django.models import Resource, ResourceContext, ResourceType
from scrud_django.registration import ResourceRegistration, get_resource_type_for

from .fixtures import *  # noqa: F401, F403
from .fixtures import RESOURCE_ENDPOINT_DETAIL_NAME, RESOURCE_ENDPOINT_LIST_NAME
from .steps import *  # noqa: F403, F401

CONTEXT_ROWS = [
    ({}, {""}),
    ({"rbac_context": None}, {""}),
    ({"rbac_context": ""}, {""}),
    ({"rbac_context": "a"}, {"a"}),
    ({"rbac_context": ["a", "b", "a"]}, {"a", "b"}),
    ({"rbac_context": []}, set()),
    ({"rbac_context": {"@id": "openteams:RBACContext"}}, {""}),
    ({"rbac_context": ["a", 1]}, {""}),
    ({"rbac_context": "__x"}, {"__x"}),
]


def rows_of(resource):
    return set(
        ResourceContext.objects.filter(resource=resource).values_list(
            "rbac_context", flat=True
        )
    )


def register(content):
    reverse(RESOURCE_ENDPOINT_LIST_NAME)
    return ResourceRegistration.register(
        dict({"name": "n", "slug": "n"}, **content), "partner-profiles"
    )


@pytest.mark.django_db
@pytest.mark.parametrize("content, expected", CONTEXT_ROWS)
def test_save_stores_one_row_per_context(content, expected):
    resource = register(content)
    assert rows_of(resource) == expected


@pytest.mark.django_db
def test_save_keeps_rows_in_step_with_the_content():
    resource = register({"rbac_context": ["a", "b"]})
    ResourceRegistration.update(
        {"name": "n", "rbac_context": ["b", "c"]}, "partner-profiles", resource.pk
    )
    assert rows_of(resource) == {"b", "c"}
    # A plain save (server code, the admin) keeps them in step too.
    resource = Resource.objects.get(pk=resource.pk)
    resource.content = {"name": "n"}
    resource.save()
    assert rows_of(resource) == {""}
    resource.delete()
    assert not ResourceContext.objects.filter(resource_id=resource.pk).exists()


@pytest.mark.django_db
def test_save_with_update_fields_without_content_leaves_rows():
    resource = register({"rbac_context": "a"})
    resource.etag = "x" * 32
    resource.save(update_fields=["etag"])
    assert rows_of(resource) == {"a"}


def migration_0008():
    return importlib.import_module("scrud_django.migrations.0008_resourcecontext")


class FakeSchemaEditor:
    connection = connection


@pytest.mark.django_db
def test_migration_backfills_rows_for_resources_stored_before_it():
    resources = [register(deepcopy(content)) for content, _ in CONTEXT_ROWS]
    # As stored before the table existed: no rows.
    ResourceContext.objects.all().delete()
    migration_0008().backfill_resource_contexts(global_apps, FakeSchemaEditor())
    for resource, (_, expected) in zip(resources, CONTEXT_ROWS, strict=True):
        assert rows_of(resource) == expected, resource.content
    # Every other resource (schemas, contexts, policies) has its rows too.
    without_rows = Resource.objects.exclude(
        pk__in=ResourceContext.objects.values("resource_id")
    ).exclude(content__rbac_context=[])
    assert not without_rows.exists()


@pytest.mark.django_db
def test_migration_contexts_agree_with_the_model():
    from scrud_django.mixins.resource_mixin import rbac_contexts_of

    contexts_of = migration_0008().contexts_of
    for content, _ in CONTEXT_ROWS:
        assert set(contexts_of(content)) == set(rbac_contexts_of(content))
    for content in (None, "text", 3, ["a"]):
        assert set(contexts_of(content)) == set(rbac_contexts_of(content))


@pytest.mark.django_db
@test_context
def test_empty_context_list_post_is_400(admin_login, partner_profile_post_data):
    before = Resource.objects.count()
    Given.http.force_authenticate(admin_login)
    When.http.post(
        reverse(RESOURCE_ENDPOINT_LIST_NAME),
        dict(partner_profile_post_data, rbac_context=[]),
        format="json",
    )
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)
    And.http.content_type_is("application/problem+json")
    And.http.response_json_matches(
        {
            "type": "https://api.openteams.com/http-problem/rbac-context-invalid",
            "title": "Invalid rbac_context",
            "status": 400,
        }
    )
    assert "at least one context" in Http.response.json()["detail"]
    assert Resource.objects.count() == before


@pytest.mark.django_db
@test_context
def test_empty_context_list_put_is_400(admin_login, partner_profiles):
    url = reverse(RESOURCE_ENDPOINT_DETAIL_NAME, kwargs={"slug": partner_profiles.id})
    stored = deepcopy(partner_profiles.content)
    Given.http.force_authenticate(admin_login)
    When.http.put(
        url,
        dict(stored, rbac_context=[]),
        format="json",
        HTTP_IF_MATCH="*",
    )
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)
    And.http.response_json_matches(
        {"type": "https://api.openteams.com/http-problem/rbac-context-invalid"}
    )
    partner_profiles.refresh_from_db()
    assert partner_profiles.content == stored
    assert rows_of(partner_profiles) == {""}


@pytest.mark.django_db
@test_context
def test_overlong_context_is_400(admin_login, partner_profile_post_data):
    before = Resource.objects.count()
    Given.http.force_authenticate(admin_login)
    When.http.post(
        reverse(RESOURCE_ENDPOINT_LIST_NAME),
        dict(partner_profile_post_data, rbac_context="x" * 2049),
        format="json",
    )
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)
    And.http.response_json_matches(
        {"type": "https://api.openteams.com/http-problem/rbac-context-invalid"}
    )
    assert Resource.objects.count() == before


@pytest.mark.django_db
def test_overlong_context_is_refused_server_side():
    before = Resource.objects.count()
    with pytest.raises(ValueError, match="2048"):
        register({"rbac_context": "x" * 2049})
    assert Resource.objects.count() == before


@pytest.mark.django_db
@test_context
def test_server_side_empty_context_list_is_stored_superuser_only(
    admin_login, regular_login
):
    resource = register({"rbac_context": []})
    assert rows_of(resource) == set()
    url = reverse(RESOURCE_ENDPOINT_DETAIL_NAME, kwargs={"slug": resource.id})
    Given.http.force_authenticate(regular_login)
    When.http.get(url)
    Then.http.status_code_is(status.HTTP_404_NOT_FOUND)
    Given.http.force_authenticate(admin_login)
    When.http.get(url)
    Then.http.status_code_is(status.HTTP_200_OK)


@pytest.mark.django_db
def test_writes_leave_the_resource_types_etag_alone():
    # Nothing reads a type's etag or modified_at since lists compute their own
    # validators: a write of a resource no longer updates (or locks) its
    # type's row.
    resource = register({"rbac_context": "a"})
    before = ResourceType.objects.get(pk=resource.resource_type_id)
    updated = ResourceRegistration.update(
        {"name": "changed"}, "partner-profiles", resource.pk
    )
    ResourceRegistration.delete("partner-profiles", str(updated.pk))
    after = ResourceType.objects.get(pk=resource.resource_type_id)
    assert updated.etag != resource.etag
    assert (after.etag, after.modified_at) == (before.etag, before.modified_at)
    assert get_resource_type_for("partner-profiles").etag == before.etag


def test_resource_content_reads_as_cel_fields():
    resource = Resource(content={"name": "x"})
    assert resource.to_cel() == {"name": "x"}
    assert Resource(content=json.loads('"text"')).to_cel() == {}


@pytest.mark.django_db
def test_raw_saves_keep_rows_in_step():
    # reversion's revert and loaddata save with Model.save_base(raw=True),
    # past Resource.save_base: the post_save receiver keeps the rows.
    from django.core import serializers
    from reversion.models import Version

    resource = register({"rbac_context": "a"})
    ResourceRegistration.update(
        {"name": "n", "rbac_context": "b"}, "partner-profiles", resource.pk
    )
    assert rows_of(resource) == {"b"}
    oldest = Version.objects.get_for_object(resource).order_by("pk").first()
    assert oldest.field_dict["content"]["rbac_context"] == "a"
    oldest.revert()
    assert rows_of(resource) == {"a"}

    data = json.loads(
        serializers.serialize("json", [Resource.objects.get(pk=resource.pk)])
    )
    data[0]["fields"]["content"]["rbac_context"] = ["c", "d"]
    (loaded,) = serializers.deserialize("json", json.dumps(data))
    assert loaded.object.content["rbac_context"] == ["c", "d"]
    loaded.save()
    assert rows_of(resource) == {"c", "d"}


def a_reader_lists(resource):
    """Whether a user who reads partner profiles only in "a" lists `resource`."""
    from .test_list_rbac import READER, user_with

    Given.http.force_authenticate(user_with((READER, "a")))
    When.http.get(reverse(RESOURCE_ENDPOINT_LIST_NAME))
    Then.http.status_code_is(status.HTTP_200_OK)
    hrefs = {entry["href"] for entry in Http.response.json()["content"]}
    url = reverse(RESOURCE_ENDPOINT_DETAIL_NAME, kwargs={"slug": resource.pk})
    return "http://testserver" + url in hrefs


def replacement_for(resource, contexts):
    """A new instance with `resource`'s pk (Django UPDATEs its row)."""
    return Resource(
        pk=resource.pk,
        resource_type=get_resource_type_for("partner-profiles"),
        content={"name": "moved", "slug": "m", "rbac_context": contexts},
        etag="e" * 32,
        modified_at=resource.modified_at,
    )


@pytest.mark.django_db
@test_context
def test_new_instance_saved_over_an_existing_pk_leaves_its_old_contexts():
    resource = register({"rbac_context": "a"})
    assert a_reader_lists(resource)
    replacement_for(resource, "b").save()
    assert rows_of(resource) == {"b"}
    assert not a_reader_lists(resource)


@pytest.mark.django_db
@test_context
def test_register_instance_over_an_existing_pk_leaves_its_old_contexts():
    resource = register({"rbac_context": "a"})
    ResourceRegistration.register_instance(replacement_for(resource, ["b", "c"]))
    assert rows_of(resource) == {"b", "c"}
    assert not a_reader_lists(resource)


def flush():
    """``manage.py flush`` inside the test's transaction. PostgreSQL refuses to
    TRUNCATE a table with deferred constraint checks pending: run them first.
    On MySQL and MariaDB a TRUNCATE commits the transaction: delete the rows
    instead (``reset_sequences=False``)."""
    from django.core.management import call_command

    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
    call_command(
        "flush",
        interactive=False,
        inhibit_post_migrate=True,
        reset_sequences=connection.vendor != "mysql",
        verbosity=0,
    )
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL DEFERRED")


def every_resource_matches_its_rows():
    from scrud_django.mixins.resource_mixin import rbac_contexts_of

    for resource in Resource.objects.all():
        assert rows_of(resource) == set(rbac_contexts_of(resource.content)), (
            resource.pk,
            resource.content,
        )


@pytest.mark.django_db
def test_dumpdata_flush_loaddata_round_trips(tmp_path):
    from django.core.management import call_command

    first = register({"rbac_context": "a"})
    ResourceRegistration.update(
        {"name": "n", "rbac_context": ["b", "c"]}, "partner-profiles", first.pk
    )
    register({"rbac_context": "a"})
    register({"rbac_context": []})
    # Row keys no longer follow resource order once rows were re-synced.
    ResourceRegistration.update(
        {"name": "n", "rbac_context": "a"}, "partner-profiles", first.pk
    )
    before = {r.pk: rows_of(r) for r in Resource.objects.all()}
    dump = tmp_path / "scrud.json"
    call_command("dumpdata", "scrud_django", output=str(dump), verbosity=0)
    # A dump whose rows disagree with the content (taken while out of step):
    # the content decides.
    data = json.loads(dump.read_text())
    for item in data:
        if item["model"] == "scrud_django.resourcecontext" and (
            item["fields"]["resource"] == first.pk
        ):
            item["fields"]["rbac_context"] = "stale"
    dump.write_text(json.dumps(data))

    flush()
    assert not Resource.objects.exists()
    call_command("loaddata", str(dump), verbosity=0)
    assert {r.pk: rows_of(r) for r in Resource.objects.all()} == before
    every_resource_matches_its_rows()
    # Loading it again changes nothing.
    call_command("loaddata", str(dump), verbosity=0)
    assert {r.pk: rows_of(r) for r in Resource.objects.all()} == before
    every_resource_matches_its_rows()


@pytest.mark.django_db
def test_raw_save_outside_a_transaction_is_refused(monkeypatch):
    # A raw save (loaddata, reversion's Version.revert()) syncs the rows in
    # post_save: outside a transaction the row would commit without them.
    from django.core import serializers
    from django.db.transaction import TransactionManagementError

    resource = register({"rbac_context": "a"})
    data = json.loads(serializers.serialize("json", [resource]))
    data[0]["fields"]["content"]["rbac_context"] = "b"
    (loaded,) = serializers.deserialize("json", json.dumps(data))
    from django.db import connections

    monkeypatch.setattr(connections["default"], "in_atomic_block", False)
    with pytest.raises(TransactionManagementError):
        loaded.save()
    monkeypatch.undo()
    resource.refresh_from_db()
    assert resource.content["rbac_context"] == "a"
    assert rows_of(resource) == {"a"}


@pytest.mark.django_db
def test_raw_save_of_an_overlong_context_is_refused():
    from django.core import serializers

    resource = register({"rbac_context": "a"})
    data = json.loads(serializers.serialize("json", [resource]))
    data[0]["fields"]["content"]["rbac_context"] = "x" * 2049
    (loaded,) = serializers.deserialize("json", json.dumps(data))
    with pytest.raises(ValueError, match="2048"):
        loaded.save()
    resource.refresh_from_db()
    assert resource.content["rbac_context"] == "a"
    assert rows_of(resource) == {"a"}


@pytest.mark.django_db
def test_dump_without_context_rows_loads_them_from_the_content(tmp_path):
    from django.core.management import call_command

    register({"rbac_context": ["a", "b"]})
    register({})
    before = {r.pk: rows_of(r) for r in Resource.objects.all()}
    dump = tmp_path / "scrud.json"
    call_command(
        "dumpdata",
        "scrud_django",
        exclude=["scrud_django.ResourceContext"],
        output=str(dump),
        verbosity=0,
    )
    assert "scrud_django.resourcecontext" not in dump.read_text()
    flush()
    call_command("loaddata", str(dump), verbosity=0)
    assert {r.pk: rows_of(r) for r in Resource.objects.all()} == before
    every_resource_matches_its_rows()


JSON_LD = "http://www.w3.org/ns/json-ld#context"


def json_ld_writer(context):
    from uuid import uuid4

    from django.utils.timezone import now
    from scoped_rbac.models import Role, RoleAssignment

    from .factories import UserFactory

    role = Role(
        definition={m: [JSON_LD] for m in ("http.GET", "http.POST", "http.PUT")},
        etag=uuid4().hex,
        modified_at=now(),
    )
    role.save()
    user = UserFactory()
    RoleAssignment(
        user=user, role=role, rbac_context=context, etag=uuid4().hex, modified_at=now()
    ).save()
    return user


# Bodies whose rbac_context names no context: the resource is stored in the
# default context "" (rbac_contexts_of), and the write is authorized there.
DEFAULT_CONTEXT_BODIES = [
    pytest.param(
        {"@id": "x", "rbac_context": {"@id": "openteams:RBACContext"}},
        id="json-ld-term-definition",
    ),
    pytest.param({"name": "n", "rbac_context": None}, id="null"),
    pytest.param({"name": "n"}, id="absent"),
]
# A list with a member that is not a string is stored in "" too, but
# scoped-rbac's request check also needs the method in each member of a list
# rbac_context (access.authorize_request), so no role allows it (it fails
# closed): refused below.
LIST_WITH_A_NON_STRING = pytest.param(
    {"name": "n", "rbac_context": ["a", 1]}, id="list-with-a-non-string"
)


@pytest.mark.django_db
@pytest.mark.parametrize("body", DEFAULT_CONTEXT_BODIES)
@test_context
def test_a_body_naming_no_context_is_written_in_the_default_context(body):
    reverse("json-ld-list")
    Given.http.force_authenticate(json_ld_writer(""))
    When.http.post(reverse("json-ld-list"), body, format="json")
    Then.http.status_code_is(status.HTTP_201_CREATED)
    created = Resource.objects.filter(resource_type__type_uri=JSON_LD).latest("pk")
    assert created.content == body
    assert created.rbac_contexts == [""]
    assert rows_of(created) == {""}

    When.http.put(
        reverse("json-ld-detail", args=[created.pk]),
        dict(body, extra=1),
        format="json",
        HTTP_IF_MATCH=f'"{created.etag}"',
    )
    Then.http.status_code_is(status.HTTP_200_OK)
    assert Resource.objects.get(pk=created.pk).content == dict(body, extra=1)


@pytest.mark.django_db
@pytest.mark.parametrize("body", [*DEFAULT_CONTEXT_BODIES, LIST_WITH_A_NON_STRING])
@test_context
def test_a_body_naming_no_context_needs_the_default_context(body):
    # A writer in "a" only: the body would be stored in "", where they may not
    # write (a list naming "a" next to a non-string included).
    reverse("json-ld-list")
    before = Resource.objects.count()
    Given.http.force_authenticate(json_ld_writer("a"))
    When.http.post(reverse("json-ld-list"), body, format="json")
    Then.http.status_code_is(status.HTTP_403_FORBIDDEN)
    assert Resource.objects.count() == before


# A list rbac_context holding an array or an object names no context. Since
# scoped-rbac 0.11.2 its request check refuses it with a 400 naming the
# field, before any grant is evaluated (access.InvalidContext): a superuser
# gets it too. Before, scrud stored such a resource in "" (rbac_contexts_of).
NESTED_IN_A_LIST = [
    pytest.param(["a", ["b"]], id="array-in-a-list"),
    pytest.param(["a", {"@id": "x"}], id="object-in-a-list"),
]
INVALID_CONTEXT_PROBLEM = {
    "type": "about:blank",
    "title": "Bad Request",
    "status": 400,
    "detail": "Invalid input.",
    "code": "invalid",
    "errors": {
        "rbac_context": [
            {
                "code": "invalid",
                "message": "A context is a string, not an array or object.",
            }
        ]
    },
}


@pytest.mark.django_db
@pytest.mark.parametrize("rbac_context", NESTED_IN_A_LIST)
@pytest.mark.parametrize("as_superuser", [False, True], ids=["writer", "superuser"])
@test_context
def test_a_list_context_holding_an_array_or_object_is_400(
    rbac_context, as_superuser, admin_login
):
    reverse("json-ld-list")
    before = Resource.objects.count()
    Given.http.force_authenticate(admin_login if as_superuser else json_ld_writer(""))
    When.http.post(
        reverse("json-ld-list"),
        {"@id": "x", "rbac_context": rbac_context},
        format="json",
    )
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)
    And.http.content_type_is("application/problem+json")
    And.http.response_json_matches(INVALID_CONTEXT_PROBLEM)
    assert Resource.objects.count() == before


@pytest.mark.django_db
@pytest.mark.parametrize("rbac_context", NESTED_IN_A_LIST)
@pytest.mark.parametrize("as_superuser", [False, True], ids=["writer", "superuser"])
@test_context
def test_a_put_of_a_list_context_holding_an_array_or_object_is_400(
    rbac_context, as_superuser, admin_login
):
    reverse("json-ld-list")
    stored = ResourceRegistration.register({"@id": "kept"}, "json-ld")
    Given.http.force_authenticate(admin_login if as_superuser else json_ld_writer(""))
    When.http.put(
        reverse("json-ld-detail", args=[stored.pk]),
        {"@id": "x", "rbac_context": rbac_context},
        format="json",
        HTTP_IF_MATCH=f'"{stored.etag}"',
    )
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)
    And.http.content_type_is("application/problem+json")
    And.http.response_json_matches(INVALID_CONTEXT_PROBLEM)
    assert Resource.objects.get(pk=stored.pk).content == {"@id": "kept"}
    assert rows_of(stored) == {""}


def test_contexts_are_read_as_exact_strs():
    # As scoped-rbac 0.11.2 reads a context (access.context_name):
    # rbac_contexts_of gives a str subclass (a StrEnum member, from Python
    # code) as the exact str of its characters, and tells a value by its
    # type, so an object only passing as a str is not one (nor evaluated).
    from enum import StrEnum

    from django.utils.functional import SimpleLazyObject

    from scrud_django.mixins.resource_mixin import rbac_contexts_of

    class Team(StrEnum):
        RED = "red"
        BLUE = "blue"

    for content in ({"rbac_context": Team.RED}, {"rbac_context": [Team.RED, "blue"]}):
        contexts = rbac_contexts_of(content)
        assert [type(c) for c in contexts] == [str] * len(contexts)
    assert rbac_contexts_of({"rbac_context": Team.RED}) == ["red"]
    assert rbac_contexts_of({"rbac_context": [Team.RED, Team.BLUE]}) == ["red", "blue"]

    def evaluated():
        raise AssertionError("a lazy context must not be evaluated")

    lazy = SimpleLazyObject(evaluated)
    assert rbac_contexts_of({"rbac_context": lazy}) == [""]
    assert rbac_contexts_of({"rbac_context": ["a", lazy]}) == [""]


def removal_stamps():
    from scrud_django.models import ResourceRemoval

    return {
        (row.resource_type_id, row.rbac_context): row.removed_at
        for row in ResourceRemoval.objects.all()
    }


@pytest.mark.django_db
def test_loaddata_into_a_database_holding_the_resource_elsewhere(tmp_path):
    # A staging copy or a partial restore: the dumped resource is already
    # stored, in another context. Loading it stamps the context it leaves,
    # and the dump's own stamp of that context must not collide with it.
    from django.core.management import call_command
    from django.utils import timezone

    from scrud_django.models import ResourceRemoval
    from scrud_django.models.resource import sync_resource_contexts

    resource = register({"rbac_context": "a"})
    type_id = resource.resource_type_id
    ResourceRegistration.update({"rbac_context": "b"}, "partner-profiles", resource.pk)
    ResourceRegistration.update({"rbac_context": "c"}, "partner-profiles", resource.pk)
    dumped = removal_stamps()
    assert set(dumped) == {(type_id, "a"), (type_id, "b")}
    a_pk = ResourceRemoval.objects.get(rbac_context="a").pk
    dump = tmp_path / "scrud.json"
    call_command("dumpdata", "scrud_django", output=str(dump), verbosity=0)

    # The target: the resource in "b", no stamps but one of "zz" under the
    # dump's key for (type, "a").
    Resource.objects.filter(pk=resource.pk).update(content={"rbac_context": "b"})
    sync_resource_contexts(Resource.objects.get(pk=resource.pk), "default")
    ResourceRemoval.objects.all().delete()
    zz_at = timezone.now() - timezone.timedelta(days=1)
    ResourceRemoval.objects.create(
        pk=a_pk, resource_type_id=type_id, rbac_context="zz", removed_at=zz_at
    )
    before_load = timezone.now()

    call_command("loaddata", str(dump), verbosity=0)

    stamps = removal_stamps()
    assert set(stamps) == {(type_id, "a"), (type_id, "b"), (type_id, "zz")}
    # Django's JSON serializer keeps milliseconds.
    dumped_a = dumped[(type_id, "a")]
    dumped_a = dumped_a.replace(microsecond=dumped_a.microsecond // 1000 * 1000)
    assert stamps[(type_id, "a")] == dumped_a
    # Leaving "b" now is later than the dump's stamp of it: the later wins.
    assert stamps[(type_id, "b")] >= before_load > dumped[(type_id, "b")]
    # The target's stamp the dump's row displaced is kept.
    assert stamps[(type_id, "zz")] == zz_at
    assert rows_of(resource) == {"c"}
    every_resource_matches_its_rows()
