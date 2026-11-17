"""The UI resource types written through the core alone (`scrud_django.core
.writes` with a plain Django request, no front end): their write behaviour
is their `TypeOptions` hooks (`scrud_django.ui.hooks`), and answers as the
DRF routes do."""

import pytest
from django.db import transaction
from django.test import RequestFactory
from django.urls import reverse
from safetydance_django.test import *  # noqa: F403, F401
from scoped_rbac import access

from scrud_django.core import writes
from scrud_django.core.requirements import requirement_leaves
from scrud_django.core.resources import allowed_methods_for, served_methods
from scrud_django.core.types import (
    HookLeaf,
    TypeOptions,
    has_hooks,
    requirement_for,
    type_options_for_slug,
)
from scrud_django.exceptions import (
    MethodNotAllowedError,
    UIBranchInUseError,
    UIBranchInvalidError,
    UIComponentNameTakenError,
    UIPushAppliedError,
)
from scrud_django.models import Resource
from scrud_django.registration import get_resource_type_for, write_db
from scrud_django.ui.branches import (
    MAIN,
    UI_BRANCH_SLUG,
    UI_BRANCH_TYPE_URI,
    UI_BRANCH_VERSION_SLUG,
    UI_BRANCH_VERSION_TYPE_URI,
    UI_CSS_SLUG,
    UI_JS_SLUG,
    UI_PUSH_SLUG,
)
from scrud_django.ui.branches.model import MAIN_BRANCH, applies_to
from scrud_django.ui.types import (
    UI_COMPONENT_SLUG,
    UI_COMPONENT_TYPE_URI,
    UI_STYLESHEET_SLUG,
)

from .fixtures import *  # noqa
from .steps import *  # noqa: F403, F401
from .test_ui_branch_pull import conflicted
from .test_ui_branch_push import (
    WIP,
    created,
    make_wip,
    own_rows,
    stored_branch,
    wip_overrides_row,
)
from .test_ui_branch_types import component, make_branch, stylesheet
from .test_ui_branch_versions import versions_of

WRITTEN_SLUGS = (
    UI_COMPONENT_SLUG,
    UI_STYLESHEET_SLUG,
    UI_CSS_SLUG,
    UI_JS_SLUG,
    UI_BRANCH_SLUG,
    UI_PUSH_SLUG,
)
UI_SLUGS = (*WRITTEN_SLUGS, UI_BRANCH_VERSION_SLUG)


def rtype(slug):
    """The UI type `slug`, its routes (and hooks) registered first."""
    reverse(f"{slug}-list")
    return get_resource_type_for(slug)


def request_by(method, user):
    """A plain Django request (no front end) of `method`, by `user`."""
    request = getattr(RequestFactory(), method)("/")
    request.user = user
    return request


def core_create(user, slug, body, *, request=None):
    """`writes.create` of `body` as `slug` by `user` (or on `request`), then
    its answer (`writes.answered_content`), in one transaction; returns
    both."""
    resource_type = rtype(slug)
    if request is None:
        request = request_by("post", user)
    using = write_db()
    with transaction.atomic(using=using):
        saved = writes.create(
            request,
            resource_type,
            body,
            requirement=requirement_for(resource_type),
            using=using,
        )
        answered = writes.answered_content(
            request, resource_type, saved, created=True, using=using
        )
    return saved, answered


def core_update(user, slug, instance, body):
    """`writes.update` of `instance` with `body` by `user`, then its answer,
    in one transaction; returns both."""
    resource_type = rtype(slug)
    request = request_by("put", user)
    using = write_db()
    with transaction.atomic(using=using):
        saved = writes.update(
            request,
            resource_type,
            instance,
            body,
            requirement=requirement_for(resource_type),
            using=using,
        )
        answered = writes.answered_content(
            request, resource_type, saved, created=False, using=using
        )
    return saved, answered


def core_destroy(user, slug, instance):
    resource_type = rtype(slug)
    using = write_db()
    with transaction.atomic(using=using):
        writes.destroy(
            request_by("delete", user),
            resource_type,
            instance,
            requirement=requirement_for(resource_type),
            using=using,
        )


def drf_delete(user, slug, instance):
    Given.http.force_authenticate(user)
    When.http.delete(
        reverse(f"{slug}-detail", args=[instance.pk]),
        HTTP_IF_MATCH=f'"{instance.etag}"',
    )
    return Http.response.status_code


def count(type_uri):
    return Resource.objects.filter(resource_type__type_uri=type_uri).count()


def versions():
    return count(UI_BRANCH_VERSION_TYPE_URI)


@pytest.mark.django_db
@test_context
def test_core_create_of_a_component_sets_its_branch_and_applies_to(admin_login):
    assert not Resource.objects.filter(
        resource_type__type_uri=UI_BRANCH_TYPE_URI, content__name=MAIN
    ).exists()
    saved, answered = core_create(admin_login, UI_COMPONENT_SLUG, component())
    assert saved.content["branch"] == MAIN
    assert answered == saved.content
    # ensure_main ran in the save's transaction: main is stored now.
    assert stored_branch(MAIN).content["name"] == MAIN
    body = stylesheet()
    saved, _ = core_create(admin_login, UI_STYLESHEET_SLUG, body)
    assert saved.content["branch"] == MAIN
    assert saved.content["applies_to"] == applies_to(body, MAIN_BRANCH)
    # The body given is the caller's: the hook completed a copy.
    assert "branch" not in body


@pytest.mark.django_db
@test_context
def test_core_create_refuses_a_taken_component_name(admin_login):
    core_create(admin_login, UI_COMPONENT_SLUG, component())
    before = count(rtype(UI_COMPONENT_SLUG).type_uri)
    with pytest.raises(UIComponentNameTakenError):
        core_create(admin_login, UI_COMPONENT_SLUG, component())
    assert count(rtype(UI_COMPONENT_SLUG).type_uri) == before


@pytest.mark.django_db
@test_context
def test_core_update_of_a_pull_records_a_version(admin_login):
    make_wip(admin_login)
    body = {"name": "wip", "rbac_context": "alice", "parent": "main", "pull": True}
    # As the DRF PUT does: one more version.
    wip = stored_branch("wip")
    before = versions()
    When.http.put(
        reverse("ui-branches-detail", args=[wip.pk]),
        data=body,
        format="json",
        HTTP_IF_MATCH=f'"{wip.etag}"',
    )
    Then.http.status_code_is(200)
    assert versions() == before + 1
    before = versions()
    saved, answered = core_update(
        admin_login, UI_BRANCH_SLUG, stored_branch("wip"), dict(body)
    )
    assert versions() == before + 1
    assert "pull" not in saved.content
    assert answered == saved.content


@pytest.mark.django_db
@test_context
def test_core_push_answers_the_applied_result(admin_login):
    wip_overrides_row(admin_login)
    body = {
        "source": "wip",
        "target": "main",
        "restore": None,
        "state": "applied",
        "rbac_context": "",
    }
    using = write_db()
    # The core's push, rolled back: the DRF push then sees the same setup.
    with transaction.atomic(using=using):
        _saved, core_answer = core_create(admin_login, UI_PUSH_SLUG, dict(body))
        transaction.set_rollback(True, using=using)
    When.http.post(reverse("ui-pushes-list"), data=body, format="json")
    Then.http.status_code_is(201)
    drf_answer = Http.response.json()
    assert "result" in core_answer
    assert core_answer == drf_answer
    # Stored as answered.
    assert created().content == drf_answer


@pytest.mark.django_db
@test_context
def test_core_destroy_refuses_main(admin_login):
    core_create(admin_login, UI_COMPONENT_SLUG, component())
    main = stored_branch(MAIN)
    with pytest.raises(UIBranchInvalidError) as refused:
        core_destroy(admin_login, UI_BRANCH_SLUG, main)
    assert refused.value.status_code == 400
    assert drf_delete(admin_login, UI_BRANCH_SLUG, main) == 400
    assert stored_branch(MAIN).pk == main.pk


@pytest.mark.django_db
@test_context
def test_core_destroy_refuses_a_branch_with_children(admin_login):
    make_wip(admin_login)
    make_wip(admin_login, name="child", parent="wip")
    wip = stored_branch("wip")
    with pytest.raises(UIBranchInUseError) as refused:
        core_destroy(admin_login, UI_BRANCH_SLUG, wip)
    assert refused.value.status_code == 409
    assert drf_delete(admin_login, UI_BRANCH_SLUG, wip) == 409
    assert stored_branch("wip").pk == wip.pk


@pytest.mark.django_db
@test_context
def test_core_destroy_refuses_an_applied_push(admin_login):
    wip_overrides_row(admin_login)
    When.http.post(
        reverse("ui-pushes-list"),
        data={
            "source": "wip",
            "target": "main",
            "restore": None,
            "state": "applied",
            "rbac_context": "",
        },
        format="json",
    )
    Then.http.status_code_is(201)
    push = created()
    with pytest.raises(UIPushAppliedError) as refused:
        core_destroy(admin_login, UI_PUSH_SLUG, push)
    assert refused.value.status_code == 409
    assert drf_delete(admin_login, UI_PUSH_SLUG, push) == 409
    assert Resource.objects.filter(pk=push.pk).exists()


@pytest.mark.django_db
@test_context
def test_core_served_methods_of_versions_are_read_only():
    versions_type = rtype(UI_BRANCH_VERSION_SLUG)
    assert served_methods(versions_type, item=True) == ("GET", "HEAD", "OPTIONS")
    assert served_methods(versions_type, item=False) == ("GET", "HEAD", "OPTIONS")
    assert served_methods(rtype(UI_BRANCH_SLUG), item=True) == ("GET", "PUT", "DELETE")


@pytest.mark.django_db
@test_context
def test_core_allow_of_a_version_is_the_drf_routes(admin_login):
    make_wip(admin_login)  # records main's first version
    version = Resource.objects.filter(
        resource_type__type_uri=UI_BRANCH_VERSION_TYPE_URI
    ).first()
    versions_type = rtype(UI_BRANCH_VERSION_SLUG)
    for pk in (version.pk, None):
        url = (
            reverse(f"{UI_BRANCH_VERSION_SLUG}-list")
            if pk is None
            else reverse(f"{UI_BRANCH_VERSION_SLUG}-detail", args=[pk])
        )
        When.http.options(url)
        Then.http.status_code_is(204)
        allowed = allowed_methods_for(
            request_by("options", admin_login),
            versions_type,
            pk if pk is None else str(pk),
            requirement_for(versions_type),
        )
        assert ", ".join(allowed) == Http.response["Allow"]


@pytest.mark.django_db
@test_context
def test_ui_type_options_are_registered_for_every_ui_type():
    for slug in WRITTEN_SLUGS:
        rtype(slug)
        assert type_options_for_slug(slug).prepare_write is not None, slug
    rtype(UI_BRANCH_VERSION_SLUG)
    assert type_options_for_slug(UI_BRANCH_VERSION_SLUG).methods == frozenset(
        {"GET", "HEAD", "OPTIONS"}
    )


@pytest.mark.django_db
@test_context
def test_ui_type_options_are_no_authorization_hooks():
    """The write hooks and ``methods`` authorize nothing: no `HookLeaf` in
    the UI types' requirements, whose grants alone decide."""
    for slug in UI_SLUGS:
        resource_type = rtype(slug)
        assert not has_hooks(type_options_for_slug(slug)), slug
        assert not any(
            isinstance(leaf, HookLeaf)
            for leaf in requirement_leaves(requirement_for(resource_type))
        ), slug


def test_type_options_refuse_a_write_hook_that_is_not_callable():
    for name in (
        "prepare_write",
        "around_save",
        "after_write",
        "before_destroy",
        "after_destroy",
    ):
        with pytest.raises(TypeError, match=name):
            TypeOptions(**{name: "not callable"})
    with pytest.raises(TypeError, match="methods"):
        TypeOptions(methods="GET")
    assert TypeOptions(methods=["get", "Head"]).methods == frozenset({"GET", "HEAD"})


def push_body(**fields):
    return {
        "source": "wip",
        "target": "main",
        "restore": None,
        "state": "applied",
        "rbac_context": "",
        **fields,
    }


@pytest.mark.django_db
@test_context
def test_core_create_alone_applies_a_push(admin_login):
    """The push is applied by the save (``after_write`` in `save_checked`),
    not by the caller's `answered_content`."""
    wip_overrides_row(admin_login)
    main_versions = versions_of("main")
    resource_type = rtype(UI_PUSH_SLUG)
    using = write_db()
    with transaction.atomic(using=using):
        saved = writes.create(
            request_by("post", admin_login),
            resource_type,
            push_body(),
            requirement=requirement_for(resource_type),
            using=using,
        )
    assert [row["template"] for row in own_rows(UI_COMPONENT_TYPE_URI, "main")] == [
        "<i>wip</i>"
    ]
    assert own_rows(UI_COMPONENT_TYPE_URI, "wip") == []
    assert versions_of("main") == [*main_versions, main_versions[-1] + 1]
    # What create returns is the push as stored, with its result.
    assert saved.content["result"] == {"version": versions_of("main")[-1]}
    assert Resource.objects.get(pk=saved.pk).content == saved.content


@pytest.mark.django_db
@test_context
def test_core_update_alone_records_a_pulled_version(admin_login):
    make_wip(admin_login)
    before = versions_of("wip")
    resource_type = rtype(UI_BRANCH_SLUG)
    using = write_db()
    with transaction.atomic(using=using):
        writes.update(
            request_by("put", admin_login),
            resource_type,
            stored_branch("wip"),
            {"name": "wip", "rbac_context": "alice", "parent": "main", "pull": True},
            requirement=requirement_for(resource_type),
            using=using,
        )
    assert len(versions_of("wip")) == len(before) + 1


def a_version():
    return Resource.objects.filter(
        resource_type__type_uri=UI_BRANCH_VERSION_TYPE_URI
    ).first()


@pytest.mark.django_db
@test_context
def test_core_create_of_a_version_is_405(admin_login):
    make_wip(admin_login)  # records main's first version
    body = dict(a_version().content)
    before = versions()
    with pytest.raises(MethodNotAllowedError) as refused:
        core_create(admin_login, UI_BRANCH_VERSION_SLUG, body)
    assert refused.value.status_code == 405
    assert str(refused.value.detail) == 'Method "POST" not allowed.'
    assert versions() == before
    When.http.post(reverse(f"{UI_BRANCH_VERSION_SLUG}-list"), data=body, format="json")
    Then.http.status_code_is(405)
    assert Http.response.json()["code"] == refused.value.detail.code


@pytest.mark.django_db
@test_context
def test_core_create_of_a_version_asks_the_request_check_first(
    admin_login, login_no_roles
):
    """As DRF answers 405 once ``initial()`` passed: a requester the request
    check refuses gets its 403."""
    make_wip(admin_login)
    with pytest.raises(access.AccessDenied):
        core_create(login_no_roles, UI_BRANCH_VERSION_SLUG, dict(a_version().content))


@pytest.mark.django_db
@test_context
def test_core_update_of_a_version_is_405(admin_login):
    make_wip(admin_login)
    version = a_version()
    with pytest.raises(MethodNotAllowedError) as refused:
        core_update(admin_login, UI_BRANCH_VERSION_SLUG, version, dict(version.content))
    assert str(refused.value.detail) == 'Method "PUT" not allowed.'
    assert Resource.objects.get(pk=version.pk).etag == version.etag
    When.http.put(
        reverse(f"{UI_BRANCH_VERSION_SLUG}-detail", args=[version.pk]),
        data=version.content,
        format="json",
        HTTP_IF_MATCH=f'"{version.etag}"',
    )
    Then.http.status_code_is(405)


@pytest.mark.django_db
@test_context
def test_core_destroy_of_a_version_is_405(admin_login):
    make_wip(admin_login)
    version = a_version()
    with pytest.raises(MethodNotAllowedError) as refused:
        core_destroy(admin_login, UI_BRANCH_VERSION_SLUG, version)
    assert str(refused.value.detail) == 'Method "DELETE" not allowed.'
    assert Resource.objects.filter(pk=version.pk).exists()
    assert drf_delete(admin_login, UI_BRANCH_VERSION_SLUG, version) == 405


@pytest.mark.django_db
@test_context
def test_successive_core_writes_on_one_request_each_use_their_own_state(
    admin_login,
):
    """The hook state of a write is its own: cleared before its
    ``prepare_write``, consumed by its save. A write on the stored ``wip``
    then one on the virtual ``main``, on one request object: the second
    stores ``main`` (its own ``around_save`` state), and none is left."""
    make_branch("wip", "alice")
    assert MAIN not in {
        row.content["name"]
        for row in Resource.objects.filter(resource_type__type_uri=UI_BRANCH_TYPE_URI)
    }
    request = request_by("post", admin_login)
    on_wip, _ = core_create(
        admin_login, UI_COMPONENT_SLUG, component(**WIP), request=request
    )
    assert on_wip.content["branch"] == "wip"
    assert writes.hook_state(request) == {}
    assert MAIN not in {
        row.content["name"]
        for row in Resource.objects.filter(resource_type__type_uri=UI_BRANCH_TYPE_URI)
    }
    on_main, _ = core_create(
        admin_login, UI_COMPONENT_SLUG, component(), request=request
    )
    assert on_main.content["branch"] == MAIN
    assert stored_branch(MAIN).content["name"] == MAIN
    assert writes.hook_state(request) == {}


@pytest.mark.django_db
@test_context
def test_core_rebase_resolves_the_conflict(admin_login):
    _first, current, override = conflicted(admin_login)
    assert [c["key"] for c in stored_branch("wip").content["conflicts"]] == [["row"]]
    saved, _ = core_update(
        admin_login,
        UI_COMPONENT_SLUG,
        override,
        component(name="row", template="<i>kept</i>", rebase=True, **WIP),
    )
    assert "rebase" not in saved.content
    assert saved.content["base"] == current
    assert stored_branch("wip").content["conflicts"] == []


@pytest.mark.django_db
@test_context
def test_core_destroy_of_an_override_resolves_the_conflict(admin_login):
    _first, _current, override = conflicted(admin_login)
    assert [c["key"] for c in stored_branch("wip").content["conflicts"]] == [["row"]]
    core_destroy(admin_login, UI_COMPONENT_SLUG, override)
    assert not Resource.objects.filter(pk=override.pk).exists()
    assert stored_branch("wip").content["conflicts"] == []


@pytest.mark.django_db
@test_context
def test_drf_refuses_a_prepare_write_that_returns_another_body(
    admin_login, monkeypatch
):
    """The DRF views save ``request.data`` itself: a ``prepare_write`` that
    returns another body would have it checked, and this one saved."""
    from scrud_django.ui.views import UIComponentViewSet

    rtype(UI_COMPONENT_SLUG)
    monkeypatch.setattr(
        UIComponentViewSet,
        "prepare_write",
        staticmethod(lambda request, resource_type, body, **kwargs: dict(body)),
    )
    before = count(UI_COMPONENT_TYPE_URI)
    Given.http.force_authenticate(admin_login)
    with pytest.raises(TypeError, match="request.data itself"):
        When.http.post(
            reverse(f"{UI_COMPONENT_SLUG}-list"), data=component(), format="json"
        )
    assert count(UI_COMPONENT_TYPE_URI) == before
