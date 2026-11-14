"""Per-type hooks declared at registration (`scrud_django.core.types`):
``checks=``, ``object_checks=``, ``content_filter=`` on
``registration.json_resource_type`` and ``compute=`` on
``registration.computed_resource_type``, honoured by the DRF routes.

``hooked-notes`` (``tests.resource_types.hooked_resource_types``) refuses
any write in the context "b" (`deny_context_b`), hides a note marked
``secret`` (`hide_secret`) and filters its list on query parameters;
``summaries`` is computed by its registered ``compute`` (`summarize`). The
hooks only refuse: the users below hold grants that would allow everything
the hooks refuse."""

import datetime
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.test import RequestFactory, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.http import parse_http_date, quote_etag
from rest_framework import status
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django import collection_type_uri_for
from scrud_django.computed import computed_etag
from scrud_django.core.requirements import GrantsLeaf
from scrud_django.core.types import (
    HookLeaf,
    TypeOptions,
    register_type_options,
    requirement_for,
    type_options,
    type_options_for_slug,
)
from scrud_django.filters import ResourceContentFilter, ResourceRbacFilter
from scrud_django.models import Resource
from scrud_django.registration import (
    ResourceRegistration,
    get_resource_type_for,
    workflow_policy_resource_type,
)
from scrud_django.views import ResourceViewSet

from .fixtures import assign_role
from .rbac_defaults import TYPE_URI_LIST_PARTNER_PROFILES, TYPE_URI_PARTNER_PROFILES
from .resource_types import (
    CHECKED_NOTE_TYPE_URI,
    HOOKED_NOTE_TYPE_URI,
    deny_context_b,
    hide_secret,
    hooked_note_type,
    hooked_resource_types,
)
from .steps import *  # noqa: F403, F401

ROOT = Path(__file__).resolve().parent.parent
HOOKED = HOOKED_NOTE_TYPE_URI
HOOKED_LIST = collection_type_uri_for(HOOKED)
ALL_METHODS = ("http.GET", "http.POST", "http.PUT", "http.DELETE")
WRITER = {method: [HOOKED, HOOKED_LIST] for method in ALL_METHODS}
READER = {"http.GET": [HOOKED, HOOKED_LIST]}
CHECKED = CHECKED_NOTE_TYPE_URI
CHECKED_READER = {"http.GET": [CHECKED, collection_type_uri_for(CHECKED)]}
PP_READER = {"http.GET": [TYPE_URI_PARTNER_PROFILES, TYPE_URI_LIST_PARTNER_PROFILES]}
JSON = "application/json"
WORKFLOW_ERROR = "http://api.openteams.com/http-problem/workflow-transition-invalid"

# Any content is a valid initial state, and stays in it.
PERMISSIVE_POLICY = {
    "resource_type": HOOKED,
    "initial_states": {"any": "true"},
    "states": {},
    "transitions": {},
}


@pytest.fixture(autouse=True)
def hooked_types(db):
    """The hooked types registered (rows and hooks) for the test, and their
    hooks as registered again afterwards, whatever the test registered."""
    reverse("hooked-notes-list")  # the URLconf registers the test types
    hooked_resource_types()
    yield
    hooked_resource_types()


def note(text, context, **content):
    return ResourceRegistration.register(
        {"text": text, "rbac_context": context, **content}, "hooked-notes"
    )


def post_note(content):
    When.http.post(
        reverse("hooked-notes-list"), data=json.dumps(content), content_type=JSON
    )


def put_note(resource, content):
    When.http.put(
        reverse("hooked-notes-detail", args=[resource.pk]),
        data=json.dumps(content),
        content_type=JSON,
        HTTP_IF_MATCH=quote_etag(resource.etag),
    )


def listed_texts():
    Then.http.status_code_is(status.HTTP_200_OK)
    return sorted(entry["content"]["text"] for entry in Http.response.json()["content"])


@pytest.mark.django_db
@test_context
def test_check_refuses_write_in_context():
    # The request check: every check in each context the body writes.
    Given.http.force_authenticate(assign_role(WRITER, "a", "b"))
    before = Resource.objects.count()
    post_note({"text": "refused", "rbac_context": "b"})
    Then.http.status_code_is(status.HTTP_403_FORBIDDEN)
    # Refused before the body is validated (an invalid one would be a 400).
    post_note({"text": 5, "rbac_context": "b"})
    Then.http.status_code_is(status.HTTP_403_FORBIDDEN)
    assert Resource.objects.count() == before
    post_note({"text": "allowed", "rbac_context": "a"})
    Then.http.status_code_is(status.HTTP_201_CREATED)
    # A PUT moving a note into "b" is refused too, and changes nothing.
    stored = Resource.objects.order_by("-pk").first()
    put_note(stored, {"text": "moved", "rbac_context": "b"})
    Then.http.status_code_is(status.HTTP_403_FORBIDDEN)
    stored.refresh_from_db()
    assert stored.content == {"text": "allowed", "rbac_context": "a"}


@pytest.mark.django_db
@test_context
def test_object_check_hides_from_list_and_detail():
    secret = note("hidden", "a", secret=True)
    shown = note("shown", "a")
    Given.http.force_authenticate(assign_role(READER, "a"))
    When.http.get(reverse("hooked-notes-list"))
    assert listed_texts() == ["shown"]
    assert Http.response.json()["count"] == 1
    # The reader's grants allow GET on it: the object check hides it (404,
    # not 403), as the list leaves it out.
    When.http.get(reverse("hooked-notes-detail", args=[secret.pk]))
    Then.http.status_code_is(status.HTTP_404_NOT_FOUND)
    When.http.get(reverse("hooked-notes-detail", args=[shown.pk]))
    Then.http.status_code_is(status.HTTP_200_OK)
    # A writer cannot reach it either.
    Given.http.force_authenticate(assign_role(WRITER, "a"))
    put_note(secret, {"text": "found", "rbac_context": "a"})
    Then.http.status_code_is(status.HTTP_404_NOT_FOUND)
    secret.refresh_from_db()
    assert secret.content["text"] == "hidden"


@pytest.mark.django_db
@test_context
def test_object_check_hides_on_a_view_with_no_neutral_requirement():
    # DRF's ~ over an IsAuthorized: the view keeps its view-based path
    # (scrud_django.filters), which asks the type's hooks too.
    secret = note("hidden", "a", secret=True)
    shown = note("shown", "a")
    Given.http.force_authenticate(assign_role(READER, "a"))
    When.http.get(reverse("not-neutral-hooked-notes-list"))
    assert listed_texts() == ["shown"]
    When.http.get(reverse("not-neutral-hooked-notes-detail", args=[secret.pk]))
    Then.http.status_code_is(status.HTTP_404_NOT_FOUND)
    When.http.get(reverse("not-neutral-hooked-notes-detail", args=[shown.pk]))
    Then.http.status_code_is(status.HTTP_200_OK)


@pytest.mark.django_db
@test_context
def test_object_check_refuses_saving_what_it_hides():
    # The post-save check asks the object checks of the resource as saved:
    # the request check (the type's checks, in "a") lets the POST through.
    Given.http.force_authenticate(assign_role(WRITER, "a"))
    before = Resource.objects.count()
    post_note({"text": "hidden", "rbac_context": "a", "secret": True})
    Then.http.status_code_is(status.HTTP_403_FORBIDDEN)
    assert Resource.objects.count() == before


@pytest.mark.django_db
@test_context
def test_content_filter():
    note("a", "a")
    note("b", "a")
    Given.http.force_authenticate(assign_role(READER, "a"))
    When.http.get(reverse("hooked-notes-list"), {"text": "a"})
    assert listed_texts() == ["a"]
    When.http.get(reverse("hooked-notes-list"))
    assert listed_texts() == ["a", "b"]
    # Per type: the other types' views keep their own backends.
    assert ResourceViewSet.filter_backends == [ResourceRbacFilter]


@pytest.mark.django_db
@test_context
def test_content_filter_leaves_other_types_unfiltered():
    reverse("partner-profiles-list")
    ResourceRegistration.register({"name": "one"}, "partner-profiles")
    ResourceRegistration.register({"name": "two"}, "partner-profiles")
    Given.http.force_authenticate(assign_role(PP_READER, ""))
    When.http.get(reverse("partner-profiles-list"), {"name": "one"})
    Then.http.status_code_is(status.HTTP_200_OK)
    names = sorted(
        entry["content"]["name"] for entry in Http.response.json()["content"]
    )
    assert names == ["one", "two"]


@pytest.mark.django_db
@test_context
def test_compute_hook():
    user = assign_role({}, "a")
    Given.http.force_authenticate(user)
    When.http.get(reverse("summaries-detail", args=["k"]), HTTP_ACCEPT=JSON)
    Then.http.status_code_is(status.HTTP_200_OK)
    body = Http.response.json()
    assert body == {"key": "k", "user": user.get_username()}
    assert Http.response["ETag"] == computed_etag(body)
    assert not Http.response["ETag"].startswith("W/")
    When.http.get(
        reverse("summaries-detail", args=["k"]),
        HTTP_ACCEPT=JSON,
        HTTP_IF_NONE_MATCH=computed_etag(body),
    )
    Then.http.status_code_is(status.HTTP_304_NOT_MODIFIED)


@pytest.mark.django_db
@test_context
def test_reregistering_replaces_options(caplog):
    with caplog.at_level(logging.WARNING, logger="scrud_django.core.types"):
        hooked_note_type()  # registered again, with no hooks
    assert [r.name for r in caplog.records] == ["scrud_django.core.types"]
    assert type_options_for_slug("hooked-notes") == TypeOptions()
    Given.http.force_authenticate(assign_role(WRITER, "a", "b"))
    post_note({"text": "allowed now", "rbac_context": "b"})
    Then.http.status_code_is(status.HTTP_201_CREATED)
    post_note({"text": "secret", "rbac_context": "a", "secret": True})
    Then.http.status_code_is(status.HTTP_201_CREATED)


def test_register_type_options_replaces_never_merges():
    iri = "tests://Replaced"
    register_type_options(iri, TypeOptions(checks=(deny_context_b,)), slug="replaced")
    register_type_options(
        iri, TypeOptions(object_checks=(hide_secret,)), slug="replaced"
    )
    assert type_options(iri) == TypeOptions(object_checks=(hide_secret,))
    assert type_options_for_slug("replaced") == type_options(iri)
    register_type_options(iri, TypeOptions(), slug="replaced")
    assert type_options(iri) == TypeOptions()
    assert type_options("tests://never-registered") == TypeOptions()
    assert type_options_for_slug("never-registered") == TypeOptions()


def test_replacing_different_hooks_warns(caplog):
    iri = "tests://Warned"
    register_type_options(iri, TypeOptions(checks=(deny_context_b,)), slug="warned")
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="scrud_django.core.types"):
        register_type_options(iri, TypeOptions(checks=(deny_context_b,)), slug="warned")
        assert caplog.records == []
        register_type_options(
            iri, TypeOptions(object_checks=(hide_secret,)), slug="warned"
        )
    (record,) = caplog.records
    assert record.levelno == logging.WARNING
    assert record.name == "scrud_django.core.types"
    assert iri in record.getMessage()


def deny(label):
    def check(request, resource_type, body, context):
        return label == "never"

    return check


def test_two_slugs_one_iri_need_the_same_hooks():
    iri = "tests://Shared"
    register_type_options(iri, TypeOptions(checks=(deny_context_b,)), slug="one")
    with pytest.raises(ImproperlyConfigured, match="one.*two.*tests://Shared"):
        register_type_options(iri, TypeOptions(), slug="two")
    assert type_options(iri) == TypeOptions(checks=(deny_context_b,))
    register_type_options(iri, TypeOptions(checks=(deny_context_b,)), slug="two")
    assert type_options_for_slug("two") == type_options_for_slug("one")


def test_two_slugs_one_iri_refuse_closures_of_different_values():
    iri = "tests://SharedClosures"
    register_type_options(iri, TypeOptions(checks=(deny("a"),)), slug="one")
    with pytest.raises(ImproperlyConfigured):
        register_type_options(iri, TypeOptions(checks=(deny("a"),)), slug="two")
    with pytest.raises(ImproperlyConfigured):
        register_type_options(iri, TypeOptions(checks=(deny("b"),)), slug="two")


def test_same_slug_reimport_is_silent_only_for_equal_closures(caplog):
    iri = "tests://Reimported"
    with caplog.at_level(logging.WARNING, logger="scrud_django.core.types"):
        register_type_options(iri, TypeOptions(checks=(deny("a"),)), slug="reimported")
        register_type_options(iri, TypeOptions(checks=(deny("a"),)), slug="reimported")
        assert caplog.records == []
        register_type_options(iri, TypeOptions(checks=(deny("b"),)), slug="reimported")
    assert len(caplog.records) == 1


def test_a_slug_moving_iri_releases_the_old_one():
    register_type_options("tests://Old", TypeOptions(checks=(deny("a"),)), slug="mover")
    register_type_options("tests://New", TypeOptions(), slug="mover")
    assert type_options("tests://Old") == TypeOptions()
    register_type_options("tests://Old", TypeOptions(), slug="other")  # not shared


@pytest.fixture
def fresh_loader(monkeypatch):
    from scrud_django.core import types

    monkeypatch.setattr(types, "_loaded", False)
    return types


def test_ensure_hooks_loaded_waits_for_the_first_import(fresh_loader, monkeypatch):
    import importlib
    import threading

    started, release = threading.Event(), threading.Event()
    imports = []

    def slow_import(name):
        imports.append(name)
        started.set()
        release.wait(5)

    monkeypatch.setattr(importlib, "import_module", slow_import)
    done = []
    first = threading.Thread(
        target=lambda: (fresh_loader.ensure_hooks_loaded(), done.append(1))
    )
    second = threading.Thread(
        target=lambda: (fresh_loader.ensure_hooks_loaded(), done.append(2))
    )
    first.start()
    assert started.wait(5)
    second.start()
    second.join(0.3)
    assert second.is_alive() and done == []
    release.set()
    first.join(5)
    second.join(5)
    assert sorted(done) == [1, 2]
    assert imports == [settings.ROOT_URLCONF]


def test_ensure_hooks_loaded_is_reentrant(fresh_loader, monkeypatch):
    import importlib

    calls = []

    def reentrant_import(name):
        fresh_loader.ensure_hooks_loaded()  # the URLconf asks for a requirement
        calls.append(name)

    monkeypatch.setattr(importlib, "import_module", reentrant_import)
    fresh_loader.ensure_hooks_loaded()
    fresh_loader.ensure_hooks_loaded()
    assert calls == [settings.ROOT_URLCONF]


def test_ensure_hooks_loaded_retries_a_failed_import(fresh_loader, monkeypatch):
    import importlib

    attempts = []

    def flaky_import(name):
        attempts.append(name)
        if len(attempts) == 1:
            raise RuntimeError("database down")

    monkeypatch.setattr(importlib, "import_module", flaky_import)
    with pytest.raises(RuntimeError):
        fresh_loader.ensure_hooks_loaded()
    fresh_loader.ensure_hooks_loaded()
    assert len(attempts) == 2


@pytest.mark.django_db
def test_requirement_for_refuses_a_mismatched_slug():
    hooked = get_resource_type_for("hooked-notes")
    with pytest.raises(ImproperlyConfigured):
        requirement_for(hooked, "todos")


@pytest.mark.django_db
def test_requirement_for_defaults_the_slug():
    (_, hooks) = requirement_for(get_resource_type_for("hooked-notes"))
    assert isinstance(hooks, HookLeaf)


def test_hooks_load_without_the_urlconf_imported():
    """A process that never imported the URLconf still has a type's hooks
    once they are asked for (`ensure_hooks_loaded`)."""
    script = """
import sys
import django
from django.conf import settings
from tests.settings import SETTINGS

settings.configure(**SETTINGS)
django.setup()
from scrud_django.core import types

assert "tests.urls" not in sys.modules
assert not types.hooks_loaded()
types.ensure_hooks_loaded()
assert types.hooks_loaded()
assert types.type_options_for_slug("hooked-notes").checks
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    assert result.returncode == 0, result.stderr


def refuses_leaving_b(request, resource_type, body, context):
    """A check that reads the body: in the context "b", a body must stay in
    "b"."""
    return context != "b" or body.get("rbac_context") == "b"


@pytest.mark.django_db
@test_context
def test_check_narrows_workflow_transition():
    workflow_policy_resource_type()
    ResourceRegistration.register(
        dict(PERMISSIVE_POLICY, rbac_context="b"), "workflow-policies"
    )
    in_b, other = note("in b", "b"), note("other", "b")
    Given.http.force_authenticate(assign_role(WRITER, "a", "b"))
    # Moving a note out of "b" acts in "b", where it is stored: the object
    # check refuses it there (deny_context_b) before the body is validated.
    put_note(in_b, {"text": "out", "rbac_context": "a"})
    Then.http.status_code_is(status.HTTP_403_FORBIDDEN)
    in_b.refresh_from_db()
    assert in_b.content == {"text": "in b", "rbac_context": "b"}
    # A check reading the body allows the note as stored in "b" (the object
    # check) and the body in "a" (the request check), but the transition is
    # authorized in "b" too, under its policy: the check narrows it there.
    hooked_note_type(checks=(refuses_leaving_b,))
    put_note(in_b, {"text": "out", "rbac_context": "a"})
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)
    assert Http.response.json()["type"] == WORKFLOW_ERROR
    in_b.refresh_from_db()
    assert in_b.content == {"text": "in b", "rbac_context": "b"}
    # Registered without the check, the same transition is allowed.
    hooked_note_type()
    put_note(other, {"text": "out", "rbac_context": "a"})
    Then.http.status_code_is(status.HTTP_200_OK)


@pytest.mark.django_db
def test_requirement_for():
    hooked = get_resource_type_for("hooked-notes")
    (grants, hooks) = requirement_for(hooked, "hooked-notes")
    assert isinstance(grants, GrantsLeaf) and grants.type_iri == HOOKED
    assert isinstance(hooks, HookLeaf) and hooks.type_iri == HOOKED
    assert hooks.overrides_object_checks
    assert hooks.has_object_permission(None, {"secret": False})
    assert not hooks.has_object_permission(None, {"secret": True})
    assert hooks.has_context_permission(None, {}, "a")
    assert not hooks.has_context_permission(None, {}, "b")
    (narrow,) = hooks.narrow
    assert narrow(None, {}, "a") and not narrow(None, {}, "b")
    todos = get_resource_type_for("todos")
    (only,) = requirement_for(todos, "todos")
    assert isinstance(only, GrantsLeaf)
    assert ResourceContentFilter not in ResourceViewSet.filter_backends


@pytest.mark.django_db
@test_context
def test_checks_alone_leave_lists_to_the_grants():
    # checks= govern writes: a type registered with them alone lists as a
    # hookless type does (by SQL, not object by object: no cap) and keeps
    # its removal stamps in the list's Last-Modified.
    an_hour_ago = timezone.now() - datetime.timedelta(hours=1)
    notes = [
        ResourceRegistration.register(
            {"text": text, "rbac_context": "a"}, "checked-notes"
        )
        for text in ("one", "two", "three")
    ]
    Resource.objects.filter(pk__in=[n.pk for n in notes]).update(
        modified_at=an_hour_ago
    )
    capped = dict(settings.SCOPED_RBAC, CONDITIONAL_LIST_MAX_ROWS=1)
    with override_settings(SCOPED_RBAC=capped):
        Given.http.force_authenticate(assign_role(CHECKED_READER, "a"))
        When.http.get(reverse("checked-notes-list"))
        assert listed_texts() == ["one", "three", "two"]
        before = Http.response["Last-Modified"]
        ResourceRegistration.delete("checked-notes", str(notes[0].pk))
        When.http.get(reverse("checked-notes-list"), HTTP_IF_MODIFIED_SINCE=before)
        assert listed_texts() == ["three", "two"]
        newest = parse_http_date(Http.response["Last-Modified"])
        assert newest > parse_http_date(before)


@pytest.mark.django_db
@test_context
def test_checks_still_refuse_saving_in_a_context():
    # The post-save check asks the type's checks in each context the saved
    # resource is in (has_context_permission), whatever the object checks.
    from rest_framework.test import APIRequestFactory
    from scoped_rbac import access

    from scrud_django.core.writes import check_post_save

    checked = get_resource_type_for("checked-notes")
    (_, hooks) = requirement_for(checked, "checked-notes")
    assert not hooks.overrides_object_checks
    request = APIRequestFactory().post("/")
    request.user = assign_role({"http.POST": [CHECKED]}, "a", "b")
    in_b = Resource(resource_type=checked, content={"text": "x", "rbac_context": "b"})
    in_a = Resource(resource_type=checked, content={"text": "x", "rbac_context": "a"})
    check_post_save(request, checked, in_a, requirement_for(checked, "checked-notes"))
    with pytest.raises(access.AccessDenied):
        check_post_save(
            request, checked, in_b, requirement_for(checked, "checked-notes")
        )


@pytest.mark.django_db
@test_context
def test_an_array_body_is_answered_as_for_a_hookless_type():
    Given.http.force_authenticate(assign_role(WRITER, "a", "b"))
    post_note([{"text": "listed", "rbac_context": "a"}])
    hooked = Http.response.status_code
    hooked_note_type()  # registered again, with no hooks
    post_note([{"text": "listed", "rbac_context": "a"}])
    assert Http.response.status_code == hooked != 500, Http.response.content


@pytest.mark.django_db
@test_context
def test_object_check_hides_from_delete():
    secret = note("hidden", "a", secret=True)
    Given.http.force_authenticate(assign_role(WRITER, "a"))
    When.http.delete(
        reverse("hooked-notes-detail", args=[secret.pk]),
        HTTP_IF_MATCH=quote_etag(secret.etag),
    )
    Then.http.status_code_is(status.HTTP_404_NOT_FOUND)
    assert Resource.objects.filter(pk=secret.pk).exists()


def reads_text(request, resource_type, body, context):
    """A check that reads the body's fields."""
    return body.get("text") != "refused"


@pytest.mark.django_db
def test_hook_leaf_request_check_and_grants():
    from django.test import RequestFactory
    from scoped_rbac.policy import Permission

    from scrud_django.core.requirements import grants_allow

    hooked = get_resource_type_for("hooked-notes")
    hooks = HookLeaf(hooked, TypeOptions(checks=(reads_text, deny_context_b)))
    factory = RequestFactory()
    post, patch = factory.post("/"), factory.patch("/")
    for request in (post, patch):
        request.user = assign_role(WRITER, "a", "b")
        assert hooks.has_permission(request, {"text": "x", "rbac_context": "a"})
        assert not hooks.has_permission(request, {"text": "x", "rbac_context": "b"})
        assert not hooks.has_permission(request, {"text": "refused"})
        # A body that is no object is left to the body's validation.
        assert hooks.has_permission(request, [{"text": "refused"}])
    # It grants nothing, and asks no grants: a user with none is refused
    # by the grants leaf beside it alone.
    nobody = factory.get("/")
    nobody.user = assign_role({}, "a")
    content = {"text": "x", "rbac_context": "a"}
    permission = Permission("http.GET", HOOKED)
    assert grants_allow([hooks], nobody, content, permission)
    assert not grants_allow([GrantsLeaf(HOOKED), hooks], nobody, content, permission)


@pytest.mark.django_db
@test_context
def test_options_allow_is_the_grants():
    # scrudful's body-less copies of an OPTIONS (access.checks_allow) are not
    # given to the hooks: Allow names what the grants allow.
    Given.http.force_authenticate(assign_role(WRITER, "a"))
    When.http.options(reverse("hooked-notes-list"))
    Then.http.status_code_is(status.HTTP_204_NO_CONTENT)
    assert "POST" in Http.response["Allow"]


def delete_note(resource):
    When.http.delete(
        reverse("hooked-notes-detail", args=[resource.pk]),
        HTTP_IF_MATCH=quote_etag(resource.etag),
    )


def allow_of_note(resource):
    When.http.options(reverse("hooked-notes-detail", args=[resource.pk]))
    Then.http.status_code_is(status.HTTP_204_NO_CONTENT)
    return [method.strip() for method in Http.response["Allow"].split(",")]


@pytest.mark.django_db
@test_context
def test_check_refuses_deleting_from_a_stored_context():
    # The object check of a DELETE asks the checks in each context the note
    # is stored in: the writer's grants allow it in "b", the check refuses.
    in_b = note("in b", "b")
    Given.http.force_authenticate(assign_role(WRITER, "a", "b"))
    delete_note(in_b)
    Then.http.status_code_is(status.HTTP_403_FORBIDDEN)
    assert Resource.objects.filter(pk=in_b.pk).exists()
    in_a = note("in a", "a")
    delete_note(in_a)
    Then.http.status_code_is(status.HTTP_204_NO_CONTENT)
    assert not Resource.objects.filter(pk=in_a.pk).exists()


@pytest.mark.django_db
@test_context
def test_check_refuses_moving_out_of_a_stored_context():
    # The body writes only "a" (the request check allows it), but the note
    # is stored in "b": the object check of the PUT refuses it there.
    in_b = note("in b", "b")
    Given.http.force_authenticate(assign_role(WRITER, "a", "b"))
    put_note(in_b, {"text": "moved", "rbac_context": "a"})
    Then.http.status_code_is(status.HTTP_403_FORBIDDEN)
    in_b.refresh_from_db()
    assert in_b.content == {"text": "in b", "rbac_context": "b"}
    # A PUT staying in "a" of a note stored in "a" is allowed.
    in_a = note("in a", "a")
    put_note(in_a, {"text": "edited", "rbac_context": "a"})
    Then.http.status_code_is(status.HTTP_200_OK)
    in_a.refresh_from_db()
    assert in_a.content == {"text": "edited", "rbac_context": "a"}
    # Reading the note in "b" is governed by the object checks alone.
    When.http.get(reverse("hooked-notes-detail", args=[in_b.pk]))
    Then.http.status_code_is(status.HTTP_200_OK)


@pytest.mark.django_db
@test_context
def test_options_of_an_item_gives_the_hooks_its_content():
    # scrudful's OPTIONS checks the object as get_object() returns it (the
    # Resource): the hooks get its content, as on the method's own request.
    in_a, secret = note("in a", "a"), note("hidden", "a", secret=True)
    Given.http.force_authenticate(assign_role(WRITER, "a"))
    assert allow_of_note(in_a) == ["GET", "PUT", "DELETE", "OPTIONS", "HEAD"]
    When.http.options(reverse("hooked-notes-detail", args=[secret.pk]))
    Then.http.status_code_is(status.HTTP_404_NOT_FOUND)


@pytest.mark.django_db
@test_context
def test_options_allow_drops_the_writes_the_checks_refuse():
    # Allow names what the object check allows: no PUT or DELETE of a note
    # stored in "b", consistent with their 403.
    in_a, in_b = note("in a", "a"), note("in b", "b")
    Given.http.force_authenticate(assign_role(WRITER, "a", "b"))
    assert allow_of_note(in_a) == ["GET", "PUT", "DELETE", "OPTIONS", "HEAD"]
    assert allow_of_note(in_b) == ["GET", "OPTIONS", "HEAD"]


@pytest.mark.django_db
def test_facade_check_refuses_writes_from_a_stored_context():
    from django.test import RequestFactory
    from scoped_rbac import access

    from scrud_django.core import resources

    hooked = get_resource_type_for("hooked-notes")
    requirement = requirement_for(hooked, "hooked-notes")
    in_a, in_b = note("in a", "a"), note("in b", "b")
    writer = assign_role(WRITER, "a", "b")

    def request_of(method):
        request = getattr(RequestFactory(), method.lower())("/")
        request.user = writer
        return request

    hooks = requirement[1]
    for method in ("PUT", "PATCH", "DELETE"):
        assert not hooks.has_object_permission(request_of(method), in_b.content)
        assert hooks.has_object_permission(request_of(method), in_a.content)
    for method in ("GET", "HEAD", "OPTIONS"):
        assert hooks.has_object_permission(request_of(method), in_b.content)
    for method in ("PUT", "DELETE"):
        # Readable (a GET is allowed): a 403, not a 404.
        with pytest.raises(access.AccessDenied):
            resources.check_object(
                request_of(method), hooked, in_b, requirement, covered=False
            )
        resources.check_object(
            request_of(method), hooked, in_a, requirement, covered=False
        )
    for method in ("PUT", "DELETE"):
        with pytest.raises(access.AccessDenied):
            resources.load_for_read(
                request_of(method),
                hooked,
                in_b.pk,
                requirement=requirement,
                method=method,
            )
        assert (
            resources.load_for_read(
                request_of(method),
                hooked,
                in_a.pk,
                requirement=requirement,
                method=method,
            )
            == in_a
        )
    assert (
        resources.load_for_read(
            request_of("GET"), hooked, in_b.pk, requirement=requirement, method="GET"
        )
        == in_b
    )
    assert resources.object_readable(request_of("DELETE"), hooked, in_b, requirement)
    options = request_of("OPTIONS")
    assert resources.allowed_methods(options, hooked, in_b, requirement) == [
        "GET",
        "OPTIONS",
        "HEAD",
    ]
    assert resources.allowed_methods(options, hooked, in_a, requirement) == [
        "GET",
        "PUT",
        "DELETE",
        "OPTIONS",
        "HEAD",
    ]


@pytest.mark.django_db
def test_facade_honours_a_types_hooks():
    from django.http import Http404
    from django.test import RequestFactory
    from scoped_rbac import access

    from scrud_django.core import resources

    hooked = get_resource_type_for("hooked-notes")
    requirement = requirement_for(hooked, "hooked-notes")
    secret, shown = note("hidden", "a", secret=True), note("shown", "a")
    writer = assign_role(WRITER, "a", "b")

    def request_of(method):
        request = getattr(RequestFactory(), method.lower())("/")
        request.user = writer
        return request

    # The object check hides a secret note, whatever the grants allow.
    for method in ("GET", "PUT", "DELETE"):
        with pytest.raises(Http404):
            resources.load_for_read(
                request_of(method),
                hooked,
                secret.pk,
                requirement=requirement,
                method=method,
            )
    listed = resources.readable_queryset(request_of("GET"), hooked, requirement)
    assert list(listed.values_list("pk", flat=True)) == [shown.pk]
    # The post-save check asks the checks where the note landed: "b" is
    # refused, and the create rolled back.
    before = Resource.objects.count()
    with pytest.raises(access.AccessDenied):
        resources.create(
            request_of("POST"),
            hooked,
            {"text": "refused", "rbac_context": "b"},
            requirement=requirement,
            using="default",
        )
    assert Resource.objects.count() == before
    created = resources.create(
        request_of("POST"),
        hooked,
        {"text": "allowed", "rbac_context": "a"},
        requirement=requirement,
        using="default",
    )
    assert Resource.objects.get(pk=created.pk).content["text"] == "allowed"


@pytest.mark.django_db
@test_context
def test_check_must_allow_in_every_stored_context():
    # Unlike an IsAuthorized subclass (any stored context allowing
    # suffices), checks= must allow in each one: a note in ["a", "b"] can be
    # neither moved to "a" nor deleted.
    both = note("in both", ["a", "b"])
    Given.http.force_authenticate(assign_role(WRITER, "a", "b"))
    put_note(both, {"text": "moved", "rbac_context": "a"})
    Then.http.status_code_is(status.HTTP_403_FORBIDDEN)
    row = Resource.objects.get(pk=both.pk)
    assert (row.content, row.etag) == (
        {"text": "in both", "rbac_context": ["a", "b"]},
        both.etag,
    )
    delete_note(both)
    Then.http.status_code_is(status.HTTP_403_FORBIDDEN)
    assert Resource.objects.filter(pk=both.pk).exists()


def says_no(request, resource_type, body, context):
    return "no"


@pytest.mark.django_db
@test_context
def test_a_truthy_non_bool_hook_refuses():
    # A hook allows by returning True: "no" (truthy) refuses, in a check
    # and in an object check.
    hooked_note_type(checks=(says_no,))
    Given.http.force_authenticate(assign_role(WRITER, "a"))
    before = Resource.objects.count()
    post_note({"text": "x", "rbac_context": "a"})
    Then.http.status_code_is(status.HTTP_403_FORBIDDEN)
    assert Resource.objects.count() == before
    hooked_note_type(object_checks=(lambda request, resource_type, obj: "yes",))
    stored = note("n", "a")
    When.http.get(reverse("hooked-notes-detail", args=[stored.pk]))
    Then.http.status_code_is(status.HTTP_404_NOT_FOUND)


@pytest.mark.django_db
def test_a_truthy_non_bool_hook_refuses_a_workflow_transition():
    hooked = get_resource_type_for("hooked-notes")
    leaf = HookLeaf(hooked, TypeOptions(checks=(says_no,)))
    assert [narrow(None, {}, "") for narrow in leaf.narrow] == [False]
    for result in (None, 1, "no", [True]):
        leaf = HookLeaf(
            hooked,
            TypeOptions(checks=(lambda request, rt, body, ctx, r=result: r,)),
        )
        assert leaf.narrow[0](None, {}, "") is False


@pytest.mark.django_db
@test_context
def test_a_body_with_no_context_is_asked_in_the_default_one():
    # rbac_context [] names no context; the checks are asked in "" (never
    # in none, which would allow it whatever they say).
    asked = []

    def deny_everything(request, resource_type, body, context):
        asked.append(context)
        return False

    hooked = get_resource_type_for("hooked-notes")
    leaf = HookLeaf(hooked, TypeOptions(checks=(deny_everything,)))
    request = RequestFactory().post("/")
    request.user = assign_role(WRITER, "a")
    assert not leaf.has_permission(request, {"rbac_context": []})
    assert asked == [""]
    # (Over HTTP scoped-rbac answers such a body 400 before any hook.)


@pytest.mark.django_db
def test_a_hook_is_given_a_mapping_or_a_resource_only():
    class Plain:
        rbac_context = "b"

    hooked = get_resource_type_for("hooked-notes")
    leaf = HookLeaf(hooked, TypeOptions(checks=(deny_context_b,)))
    request = RequestFactory().delete("/")
    request.user = assign_role(WRITER, "a", "b")
    with pytest.raises(TypeError, match="Plain"):
        leaf.has_object_permission(request, Plain())
    with pytest.raises(TypeError, match="Plain"):
        leaf.has_context_permission(request, Plain(), "b")
    # A Resource and a mapping are given as their content.
    stored = note("in b", "b")
    assert not leaf.has_object_permission(request, stored)
    assert not leaf.has_object_permission(request, dict(stored.content))


EXTRA = "tests://Extra"


def asks_extra_grant(request, resource_type, body, context):
    """A check that asks a grant of its own: the request's method on
    `EXTRA` in `context`."""
    from scoped_rbac import access
    from scoped_rbac.policy import Permission

    permission = Permission(access.action_iri_for(request.method), EXTRA)
    return access.policy_for(request).should_allow(permission, context)


def extra_grant(conditioned):
    """A role writing `EXTRA`, only through the workflow check when
    `conditioned` (a grant ``scrud_workflow`` conditions)."""
    grant = (
        {
            EXTRA: {
                "condition": {
                    "operator": "scrud_workflow",
                    "authorized_transitions": ["__INITIALIZE__", "__SAME_STATE__"],
                }
            }
        }
        if conditioned
        else [EXTRA]
    )
    return {"http.POST": grant, "http.PUT": grant}


@pytest.mark.django_db
@pytest.mark.parametrize("conditioned", [True, False], ids=["workflow", "plain"])
@test_context
def test_hooks_request_check_runs_outside_the_write_checks(conditioned):
    """DRF asks a type's hooks' request check (``check_request_hooks``)
    after its own, outside ``write_checks``: there ``scrud_workflow``
    refuses, so a hook that asks a grant conditioned on it is refused, a
    403. The facade's `check_request` asks them outside
    ``authorizing_write`` too, for a POST to the list and an item's PUT.
    With an unconditioned grant both allow."""
    from scoped_rbac import access

    from scrud_django.core.resources import check_request

    from .test_core_resources import user_with

    hooked_note_type(checks=(asks_extra_grant,))
    hooked = get_resource_type_for("hooked-notes")
    requirement = requirement_for(hooked, "hooked-notes")
    user = user_with((WRITER, "a"), (extra_grant(conditioned), "a"))
    stored = note("n", "a")
    body = {"text": "x", "rbac_context": "a"}
    expected = status.HTTP_403_FORBIDDEN if conditioned else None
    Given.http.force_authenticate(user)
    post_note(body)
    Then.http.status_code_is(expected or status.HTTP_201_CREATED)
    put_note(stored, body)
    Then.http.status_code_is(expected or status.HTTP_200_OK)
    for method, list_route in (("post", True), ("put", False)):
        request = getattr(RequestFactory(), method)("/")
        request.user = user

        def asked(request=request, list_route=list_route):
            check_request(
                request, hooked, requirement, list_route=list_route, body=body
            )

        if conditioned:
            with pytest.raises(access.AccessDenied):
                asked()
        else:
            asked()
