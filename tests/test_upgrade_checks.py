"""Upgrading a 0.11 database (scrud_django at migration 0007) whose stored
``WorkflowPolicy`` resources fail the database check: a plain ``migrate``
goes through (the check of stored policies waits for scrud_django's
migrations, whose tables the fix needs), then ``check --database`` reports
them (``scrud_django.E002``, ``E005``), and fixing or deleting them clears
it.

It runs on the "other" database, migrated back to 0007 and forward again in
the test (a transactional test, which flushes only that database)."""

from uuid import uuid4

import pytest
from django.core.management import call_command
from django.db import connections, transaction
from django.db.migrations.executor import MigrationExecutor
from django.urls import get_resolver
from django.utils.timezone import now

from scrud_django.checks import check_stored_workflow_policies
from scrud_django.models import Resource
from scrud_django.workflow import WORKFLOW_POLICY_TYPE_URI

ALIAS = "other"
BEFORE_0_12 = ("scrud_django", "0007_auto_20220221_0551")
THING = "tests://UpgradedThing"
POLICY = {
    "resource_type": THING,
    "initial_states": {"s": "true"},
    "states": {"s": "true"},
    "transitions": {},
}


@pytest.fixture(scope="module")
def urls_loaded(django_db_setup, django_db_blocker):
    """The URLconf imported (``migrate``'s URL checks read it), its resource
    type registrations rolled back on the default database, before the test
    starts: the test may only use "other"."""
    with django_db_blocker.unblock(), transaction.atomic():
        assert get_resolver().url_patterns
        transaction.set_rollback(True)


def migrate_to(target):
    connection = connections[ALIAS]
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate([target])
    return executor.loader.project_state([target]).apps


def ids(found):
    return sorted(error.id for error in found)


@pytest.mark.django_db(transaction=True, databases=[ALIAS])
def test_a_0_11_database_with_bad_policies_migrates_then_check_reports_them(
    urls_loaded,
):
    apps = migrate_to(BEFORE_0_12)
    ResourceType = apps.get_model("scrud_django", "ResourceType")
    OldResource = apps.get_model("scrud_django", "Resource")

    def resource_type(type_uri, slug):
        return ResourceType.objects.using(ALIAS).create(
            type_uri=type_uri,
            slug=slug,
            revision="1",
            etag=uuid4().hex,
            modified_at=now(),
        )

    policies = resource_type(WORKFLOW_POLICY_TYPE_URI, "workflow-policies")
    resource_type(THING, "upgraded-things")

    def stored(content):
        return OldResource.objects.using(ALIAS).create(
            resource_type=policies,
            content=content,
            etag=uuid4().hex,
            modified_at=now(),
        )

    kept = stored(dict(POLICY, rbac_context="a"))
    duplicate = stored(dict(POLICY, rbac_context="a"))
    invalid = stored(
        dict(POLICY, rbac_context="e", transitions={"t": {"from": "s", "to": "x"}})
    )

    # Before scrud_django's migrations are applied the stored policies are
    # not checked: neither the API nor ResourceRegistration could fix them.
    assert check_stored_workflow_policies(None, databases=[ALIAS]) == []
    call_command("migrate", database=ALIAS, verbosity=0)

    found = check_stored_workflow_policies(None, databases=[ALIAS])
    assert ids(found) == ["scrud_django.E002", "scrud_django.E005"]
    messages = " ".join(error.msg for error in found)
    assert f"pks [{kept.pk}, {duplicate.pk}]" in messages
    assert f"pk {invalid.pk} " in messages

    # Deleted and fixed with the 0.12 models, whose tables now exist. (The
    # fix is an update() of the content, in the same context: a save would
    # read the workflow policies through the router, on "default".)
    Resource.objects.using(ALIAS).get(pk=duplicate.pk).delete()
    Resource.objects.using(ALIAS).filter(pk=invalid.pk).update(
        content=dict(POLICY, rbac_context="e")
    )
    assert check_stored_workflow_policies(None, databases=[ALIAS]) == []


@pytest.mark.django_db
def test_a_migrated_database_is_checked():
    # The default database is migrated: its stored policies are checked (a
    # duplicate stored server-side is reported). This test also makes
    # pytest-django set up "default" when this module runs alone, which
    # tests/conftest.py registers the test resource types in.
    from scrud_django.registration import ResourceRegistration

    for _ in range(2):
        ResourceRegistration.register(
            dict(POLICY, rbac_context="a"), "workflow-policies"
        )
    found = check_stored_workflow_policies(None, databases=["default"])
    assert ids(found) == ["scrud_django.E002"]
