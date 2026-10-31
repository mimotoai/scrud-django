"""Repairing ``ResourceContext`` drift: rows that disagree with a resource's
content, left by a write that bypassed ``Resource.save()`` (a 0.11 process
still writing after ``migrate``, ``QuerySet.update()``, ``bulk_create()``).

``manage.py scrud_sync_contexts`` compares every resource's rows with its
content and re-syncs those that differ (``--check`` only reports them, and
exits non-zero if any do). The ``scrud_django.W004`` database check finds
the cheap part of it: resources with no row at all though their content
names a context."""

import pytest
from django.core import checks
from django.core.management import CommandError, call_command
from django.urls import reverse
from django.utils import timezone

from scrud_django.checks import check_context_drift
from scrud_django.mixins.resource_mixin import rbac_contexts_of
from scrud_django.models import Resource, ResourceContext, ResourceRemoval
from scrud_django.models.resource import context_fits
from scrud_django.registration import ResourceRegistration, get_resource_type_for


def rows_of(pk):
    return set(
        ResourceContext.objects.filter(resource_id=pk).values_list(
            "rbac_context", flat=True
        )
    )


def register(content):
    reverse("partner-profiles-list")
    return ResourceRegistration.register(dict(content), "partner-profiles")


def written_around_save(*contents):
    """Resources stored without ``save()`` (as a 0.11 process or
    ``bulk_create()`` writes them): no ``ResourceContext`` rows."""
    resource_type = get_resource_type_for("partner-profiles")
    return Resource.objects.bulk_create(
        [
            Resource(
                content=content,
                resource_type=resource_type,
                etag="e" * 32,
                modified_at=timezone.now(),
            )
            for content in contents
        ]
    )


@pytest.fixture
def drift():
    """A resource moved around ``save()`` (its row names the context it
    left), one created around it (no rows), and resources in step: one in
    no context, one whose only context is too long for a row, one whose
    rows are right."""
    moved = register({"name": "moved", "rbac_context": "a"})
    Resource.objects.filter(pk=moved.pk).update(
        content={"name": "moved", "rbac_context": "zz"}
    )
    created, list_with_overlong = written_around_save(
        {"name": "created", "rbac_context": ["b", "c"]},
        {"name": "partly long", "rbac_context": ["ok", "x" * 3000]},
    )
    (overlong,) = written_around_save({"name": "long", "rbac_context": "y" * 3000})
    nowhere = ResourceRegistration.register(
        {"name": "nowhere", "rbac_context": []}, "partner-profiles"
    )
    fine = register({"name": "fine", "rbac_context": "d"})
    return {
        "moved": moved.pk,
        "created": created.pk,
        "list_with_overlong": list_with_overlong.pk,
        "overlong": overlong.pk,
        "nowhere": nowhere.pk,
        "fine": fine.pk,
    }


def every_resource_matches_its_rows():
    for resource in Resource.objects.all():
        wanted = {c for c in rbac_contexts_of(resource.content) if context_fits(c)}
        assert rows_of(resource.pk) == wanted, (resource.pk, resource.content)


@pytest.mark.django_db
def test_check_reports_drift_and_changes_nothing(drift, capsys):
    with pytest.raises(CommandError) as raised:
        call_command("scrud_sync_contexts", "--check")
    assert raised.value.returncode == 1
    assert "3 resources" in str(raised.value)
    out = capsys.readouterr().out
    for name in ("moved", "created", "list_with_overlong"):
        assert f"pk {drift[name]}:" in out, out
    for name in ("overlong", "nowhere", "fine"):
        assert f"pk {drift[name]}:" not in out, out
    assert rows_of(drift["moved"]) == {"a"}
    assert rows_of(drift["created"]) == set()


@pytest.mark.django_db
def test_sync_repairs_drift_and_is_idempotent(drift, capsys):
    resource_type_id = get_resource_type_for("partner-profiles").pk
    call_command("scrud_sync_contexts")
    assert "Re-synced 3 resources" in capsys.readouterr().out
    assert rows_of(drift["moved"]) == {"zz"}
    assert rows_of(drift["created"]) == {"b", "c"}
    assert rows_of(drift["list_with_overlong"]) == {"ok"}
    assert rows_of(drift["overlong"]) == set()
    assert rows_of(drift["nowhere"]) == set()
    every_resource_matches_its_rows()
    # Lists that showed the moved resource in "a" are modified.
    assert ResourceRemoval.objects.filter(
        resource_type_id=resource_type_id, rbac_context="a"
    ).exists()

    call_command("scrud_sync_contexts")
    assert "in step" in capsys.readouterr().out
    call_command("scrud_sync_contexts", "--check")
    assert "in step" in capsys.readouterr().out


@pytest.mark.django_db
def test_the_database_check_reports_resources_without_rows(drift):
    found = check_context_drift(None, databases=["default"])
    assert [(m.id, m.level) for m in found] == [("scrud_django.W004", checks.WARNING)]
    # Only resources with no row at all: a moved resource's stale row needs
    # the command's full comparison.
    assert f"[{drift['created']}, {drift['list_with_overlong']}]" in found[0].msg
    assert "scrud_sync_contexts" in found[0].hint
    # A database check: `check --database` and `migrate` run it.
    assert found[0] in checks.run_checks(databases=["default"])
    assert found[0] not in checks.run_checks()

    call_command("scrud_sync_contexts", verbosity=0)
    assert check_context_drift(None, databases=["default"]) == []
