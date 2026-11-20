"""The UI resource types written through the core alone (`scrud_django.core
.writes` with a plain Django request, no front end): their write behaviour
is their `TypeOptions` hooks (`scrud_django.ui.hooks`), and answers as the
DRF routes do."""

import pytest
from django.conf import settings
from django.db import transaction
from django.test import RequestFactory, override_settings
from django.urls import reverse
from django.utils.http import http_date
from safetydance_django.test import *  # noqa: F403, F401
from scoped_rbac import access
from scrudful._django import precondition_headers

from scrud_django.core import lists, writes
from scrud_django.core.errors import error_answer
from scrud_django.core.requirements import requirement_leaves
from scrud_django.core.resources import (
    allowed_methods_for,
    item_preconditions,
    item_read,
    load_for_read,
    served_methods,
)
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
    UIBranchUnavailableError,
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
from scrud_django.ui.branches.effective import BRANCH_HEADER
from scrud_django.ui.branches.model import MAIN_BRANCH, applies_to
from scrud_django.ui.hooks import UI_TYPE_OPTIONS, list_item_rows, read_version
from scrud_django.ui.types import (
    UI_COMPONENT_SLUG,
    UI_COMPONENT_TYPE_URI,
    UI_STYLESHEET_SLUG,
)

from .fixtures import *  # noqa
from .fixtures import UI_TYPE_URIS, assign_ui_role
from .steps import *  # noqa: F403, F401
from .test_ui_branch_pull import conflicted
from .test_ui_branch_push import (
    WIP,
    create,
    created,
    make_wip,
    own_rows,
    stored_branch,
    wip_overrides_row,
)
from .test_ui_branch_types import component, make_branch, stylesheet
from .test_ui_branch_versions import store, versions_of
from .test_ui_http import make_component

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
    """The write and read hooks and ``methods`` authorize nothing: no
    `HookLeaf` in the UI types' requirements, whose grants alone decide."""
    read_hooks = 0
    for slug in UI_SLUGS:
        resource_type = rtype(slug)
        options = type_options_for_slug(slug)
        read_hooks += sum(
            hook is not None for hook in (options.list_rows, options.read_view)
        )
        assert not has_hooks(options), slug
        assert not any(
            isinstance(leaf, HookLeaf)
            for leaf in requirement_leaves(requirement_for(resource_type))
        ), slug
    # The four item kinds' list_rows and the versions' read_view.
    assert read_hooks == 5


@pytest.mark.django_db
@test_context
def test_ui_read_hooks_are_registered_for_their_types():
    for slug in (UI_COMPONENT_SLUG, UI_STYLESHEET_SLUG, UI_CSS_SLUG, UI_JS_SLUG):
        rtype(slug)
        assert type_options_for_slug(slug).list_rows is list_item_rows, slug
        assert type_options_for_slug(slug).read_view is None, slug
    for slug in (UI_BRANCH_SLUG, UI_PUSH_SLUG):
        assert UI_TYPE_OPTIONS[slug].list_rows is None, slug
    rtype(UI_BRANCH_VERSION_SLUG)
    assert type_options_for_slug(UI_BRANCH_VERSION_SLUG).read_view is read_version


def test_type_options_refuse_a_write_hook_that_is_not_callable():
    for name in (
        "prepare_write",
        "around_save",
        "after_write",
        "before_destroy",
        "after_destroy",
        "list_rows",
        "read_view",
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


def answered_body(exc):
    """The body a front end answers the `ScrudException` `exc` with, uniform
    errors as they are now (`scrud_django.core.errors.error_answer`)."""
    status, body, _headers = error_answer(exc)
    assert status == exc.status_code
    return body


def uniform_errors_set(uniform):
    return override_settings(
        SCOPED_RBAC=dict(settings.SCOPED_RBAC, UNIFORM_ERRORS=uniform)
    )


@pytest.mark.django_db
@pytest.mark.parametrize("uniform", [True, False])
@test_context
def test_core_create_of_a_version_answers_the_drf_405(admin_login, uniform):
    make_wip(admin_login)  # records main's first version
    body = dict(a_version().content)
    before = versions()
    with uniform_errors_set(uniform):
        with pytest.raises(MethodNotAllowedError) as refused:
            core_create(admin_login, UI_BRANCH_VERSION_SLUG, body)
        core_body = answered_body(refused.value)
        Given.http.force_authenticate(admin_login)
        When.http.post(
            reverse(f"{UI_BRANCH_VERSION_SLUG}-list"), data=body, format="json"
        )
    Then.http.status_code_is(405)
    assert refused.value.status_code == 405
    assert str(refused.value.detail) == 'Method "POST" not allowed.'
    assert core_body == Http.response.json()
    assert versions() == before


@pytest.mark.django_db
@test_context
def test_core_create_of_a_version_asks_the_request_check_first(
    admin_login, login_no_roles
):
    """The core asks the request check before it refuses a method the type
    does not serve: a requester it refuses gets its 403. (The API front
    ends answer that method 405 before any authorization; follow-up
    ``core-unserved-order``.)"""
    make_wip(admin_login)
    with pytest.raises(access.AccessDenied):
        core_create(login_no_roles, UI_BRANCH_VERSION_SLUG, dict(a_version().content))


@pytest.mark.django_db
@pytest.mark.parametrize("uniform", [True, False])
@test_context
def test_core_update_of_a_version_answers_the_drf_405(admin_login, uniform):
    make_wip(admin_login)
    version = a_version()
    with uniform_errors_set(uniform):
        with pytest.raises(MethodNotAllowedError) as refused:
            core_update(
                admin_login, UI_BRANCH_VERSION_SLUG, version, dict(version.content)
            )
        core_body = answered_body(refused.value)
        Given.http.force_authenticate(admin_login)
        When.http.put(
            reverse(f"{UI_BRANCH_VERSION_SLUG}-detail", args=[version.pk]),
            data=version.content,
            format="json",
            HTTP_IF_MATCH=f'"{version.etag}"',
        )
    Then.http.status_code_is(405)
    assert str(refused.value.detail) == 'Method "PUT" not allowed.'
    assert core_body == Http.response.json()
    assert Resource.objects.get(pk=version.pk).etag == version.etag


@pytest.mark.django_db
@pytest.mark.parametrize("uniform", [True, False])
@test_context
def test_core_destroy_of_a_version_answers_the_drf_405(admin_login, uniform):
    make_wip(admin_login)
    version = a_version()
    with uniform_errors_set(uniform):
        with pytest.raises(MethodNotAllowedError) as refused:
            core_destroy(admin_login, UI_BRANCH_VERSION_SLUG, version)
        core_body = answered_body(refused.value)
        assert drf_delete(admin_login, UI_BRANCH_VERSION_SLUG, version) == 405
    assert str(refused.value.detail) == 'Method "DELETE" not allowed.'
    assert core_body == Http.response.json()
    assert Resource.objects.filter(pk=version.pk).exists()


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
    from scrud_django.drf.ui import UIComponentViewSet

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


@pytest.mark.django_db
@test_context
def test_core_rewrite_of_one_object_drops_the_earlier_answer(admin_login):
    """`save_checked` drops an answer an earlier write left on the object it
    writes: the second write of one object answers its own content."""
    saved, _ = core_create(admin_login, UI_COMPONENT_SLUG, component())
    setattr(saved, writes.ANSWER_ATTR, {"stale": True})
    rewritten, answered = core_update(
        admin_login, UI_COMPONENT_SLUG, saved, component(template="<i>new</i>")
    )
    assert writes.written_answer(saved) is None
    assert writes.written_answer(rewritten) is None
    assert answered == rewritten.content
    assert answered["template"] == "<i>new</i>"


def get_request(user, *, headers=None):
    """A plain Django GET by `user`, with `headers`."""
    request = RequestFactory().get("/", headers=headers or {})
    request.user = user
    return request


def core_list(user, slug, *, headers=None, html=False):
    """`lists.list_queryset` of `slug` for `user`, with `headers`."""
    resource_type = rtype(slug)
    request = get_request(user, headers=headers)
    return request, lists.list_queryset(
        request,
        resource_type,
        request.GET,
        requirement=requirement_for(resource_type),
        html=html,
    )


def key_of(href):
    return int(href.rstrip("/").rsplit("/", 1)[1])


@pytest.mark.django_db
@test_context
def test_core_list_of_components_shows_the_chosen_branch_rows(admin_login):
    """`list_queryset` runs the type's ``list_rows``: with ``Scrud-UI-Branch:
    wip``, wip's effective rows (its override and its deletion marker), as
    the DRF list answers them."""
    wip_overrides_row(admin_login)
    make_component("other", "<b>other</b>")
    create(UI_COMPONENT_SLUG, {"name": "other", "deleted": True, **WIP})
    _request, rows = core_list(
        admin_login, UI_COMPONENT_SLUG, headers={BRANCH_HEADER: "wip"}
    )
    core_rows = {obj.pk: obj.content for obj in rows}
    Given.http.force_authenticate(admin_login)
    When.http.get(reverse(f"{UI_COMPONENT_SLUG}-list"), headers={BRANCH_HEADER: "wip"})
    Then.http.status_code_is(200)
    drf_rows = {
        key_of(entry["href"]): entry["content"]
        for entry in Http.response.json()["content"]
    }
    assert core_rows == drf_rows
    assert sorted(
        (c["name"], c["branch"], c.get("deleted", False)) for c in core_rows.values()
    ) == [("other", "wip", True), ("row", "wip", False)]
    # Without a selection, main's (stored as is: no branch field).
    _request, rows = core_list(admin_login, UI_COMPONENT_SLUG)
    assert sorted(
        (obj.content["name"], obj.content.get("branch", MAIN)) for obj in rows
    ) == [("other", MAIN), ("row", MAIN)]
    # A JSON list naming a branch it may not have is refused, as on DRF.
    with pytest.raises(UIBranchUnavailableError):
        core_list(admin_login, UI_COMPONENT_SLUG, headers={BRANCH_HEADER: "nope"})
    When.http.get(reverse(f"{UI_COMPONENT_SLUG}-list"), headers={BRANCH_HEADER: "nope"})
    Then.http.status_code_is(400)


def a_narrowed_version():
    """Version 3 of main, holding the component ``row`` and the stylesheets
    ``theme`` (in "") and ``acme-theme`` (in "acme"), and a reader who may
    read the items in "" only."""
    make_component("row", "<b/>")
    store("ui-stylesheets", stylesheet(rbac_context=""))
    store("ui-stylesheets", stylesheet(name="acme-theme", rbac_context="acme"))
    version = Resource.objects.get(
        resource_type__type_uri=UI_BRANCH_VERSION_TYPE_URI, content__number=3
    )
    reader = assign_ui_role((UI_BRANCH_VERSION_TYPE_URI, *UI_TYPE_URIS), "")
    return version, reader


def core_item_read(user, version):
    versions_type = rtype(UI_BRANCH_VERSION_SLUG)
    request = get_request(user)
    obj = load_for_read(
        request,
        versions_type,
        str(version.pk),
        requirement=requirement_for(versions_type),
        method="GET",
    )
    return item_read(request, versions_type, obj)


@pytest.mark.django_db
@test_context
def test_core_item_read_of_a_version_is_narrowed_per_reader(admin_login):
    version, reader = a_narrowed_version()
    content, etag, last_modified = core_item_read(reader, version)
    assert [item["name"] for item in content["items"]["stylesheet"]] == ["theme"]
    assert etag != version.etag
    assert last_modified == version.modified_at
    Given.http.force_authenticate(reader)
    When.http.get(reverse(f"{UI_BRANCH_VERSION_SLUG}-detail", args=[version.pk]))
    Then.http.status_code_is(200)
    assert content == Http.response.json()
    assert f'"{etag}"' == Http.response["ETag"]
    # A reader of everything gets the stored version.
    content, etag, _ = core_item_read(admin_login, version)
    assert content == version.content
    assert etag == version.etag
    assert Resource.objects.get(pk=version.pk).content == version.content


@pytest.mark.django_db
@test_context
def test_core_list_of_versions_is_narrowed_per_reader(admin_login):
    """The list's envelopes and ETag follow ``read_view``, as the DRF list's
    do."""
    version, reader = a_narrowed_version()
    versions_type = rtype(UI_BRANCH_VERSION_SLUG)
    request, queryset = core_list(reader, UI_BRANCH_VERSION_SLUG)
    page = lists.requested_page(queryset, request.GET)
    body = lists.list_body(request, versions_type, page)
    etag, _ = lists.list_validators(
        request, versions_type, queryset, page, requirement_for(versions_type)
    )
    Given.http.force_authenticate(reader)
    When.http.get(reverse(f"{UI_BRANCH_VERSION_SLUG}-list"))
    Then.http.status_code_is(200)
    assert body == Http.response.json()
    assert f'"{etag}"' == Http.response["ETag"]
    narrowed = next(
        entry for entry in body["content"] if key_of(entry["href"]) == version.pk
    )
    assert narrowed["etag"] != f'"{version.etag}"'
    # Another reader's list ETag differs.
    request, queryset = core_list(admin_login, UI_BRANCH_VERSION_SLUG)
    page = lists.requested_page(queryset, request.GET)
    full, _ = lists.list_validators(
        request, versions_type, queryset, page, requirement_for(versions_type)
    )
    assert full != etag


@pytest.mark.django_db
@pytest.mark.parametrize(
    "method, header, value",
    [
        ("GET", "If-None-Match", "current"),
        ("GET", "If-Match", "stale"),
        ("GET", None, None),
        ("PUT", "If-Match", "stale"),
        ("PUT", "If-Match", "current"),
        ("PUT", None, None),
        ("DELETE", "If-Match", "stale"),
        ("DELETE", None, None),
        ("PUT", "If-Match", "malformed"),
        ("GET", "If-Modified-Since", "modified"),
        ("PUT", "If-Unmodified-Since", "long-ago"),
        ("DELETE", "If-None-Match", "current"),
    ],
)
@test_context
def test_item_preconditions_match_the_api(
    admin_login, todo_list, method, header, value
):
    """`item_preconditions` answers the status the DRF item route answers for
    the request's preconditions (``None`` where it goes on: a 200)."""
    resource_type = get_resource_type_for("todo-lists")
    values = {
        "current": f'"{todo_list.etag}"',
        "stale": '"stale"',
        "malformed": "not-an-etag",
        "modified": http_date(todo_list.modified_at.timestamp()),
        "long-ago": "Mon, 01 Jan 2001 00:00:00 GMT",
    }
    headers = {} if header is None else {header: values[value]}
    request = getattr(RequestFactory(), method.lower())("/", headers=headers)
    request.user = admin_login
    obj = load_for_read(
        request,
        resource_type,
        str(todo_list.pk),
        requirement=requirement_for(resource_type),
        method=method,
    )
    _content, etag, last_modified = item_read(request, resource_type, obj)
    status = item_preconditions(
        request, etag=etag, last_modified=last_modified, method=method
    )
    Given.http.force_authenticate(admin_login)
    send = getattr(When.http, method.lower())
    url = reverse("todo-lists-detail", args=[todo_list.pk])
    if method == "PUT":
        send(url, data=todo_list.content, format="json", headers=headers)
    else:
        send(url, headers=headers)
    drf = Http.response.status_code
    if status is None:
        assert drf == 200
    else:
        assert status == drf


@pytest.mark.django_db
@test_context
def test_core_read_view_narrows_one_version_again_for_another_reader(admin_login):
    """`read_version` narrows per request: one version object read first by
    a reader of every item, then by one who may not read them all, gives
    the second the narrowed items and ETag (from the stored ones)."""
    version, reader = a_narrowed_version()
    versions_type = rtype(UI_BRANCH_VERSION_SLUG)
    stored = (version.content, version.etag)
    content, etag = read_version(get_request(admin_login), versions_type, version)
    assert (content, etag) == stored
    narrowed, narrowed_etag = read_version(get_request(reader), versions_type, version)
    assert [i["name"] for i in narrowed["items"]["stylesheet"]] == ["theme"]
    assert narrowed_etag != stored[1]
    assert (narrowed, narrowed_etag) == core_item_read(reader, version)[:2]
    # And back: the reader of everything gets the stored version again.
    assert read_version(get_request(admin_login), versions_type, version) == stored
    # The same request is answered from its memo.
    request = get_request(reader)
    first = read_version(request, versions_type, version)
    assert read_version(request, versions_type, version) == first


def post_request(user, headers):
    request = RequestFactory().post(
        "/",
        data=component(name="new", **WIP),
        content_type="application/json",
        headers=headers,
    )
    request.user = user
    return request


@pytest.mark.django_db
@test_context
def test_core_conditional_create_on_a_branch_list_evaluates_as_drf(admin_login):
    """A POST's list validators read the list through the type's
    ``list_rows`` (as JSON), as DRF's ``create_list_validators`` does: an
    ``If-Match`` of the ``wip`` list's ETag passes on both, a stale one
    fails on both."""
    wip_overrides_row(admin_login)
    resource_type = rtype(UI_COMPONENT_SLUG)
    list_url = reverse(f"{UI_COMPONENT_SLUG}-list")
    Given.http.force_authenticate(admin_login)
    When.http.get(list_url, headers={BRANCH_HEADER: "wip"})
    Then.http.status_code_is(200)
    wip_etag = Http.response["ETag"]
    When.http.get(list_url)
    assert Http.response["ETag"] != wip_etag  # main's list differs

    def core_status(if_match):
        request = post_request(
            admin_login, {BRANCH_HEADER: "wip", "If-Match": if_match}
        )
        failed = lists.evaluate_list_preconditions(
            "POST",
            precondition_headers(request),
            lambda: lists.create_list_validators(
                request,
                resource_type,
                requirement=requirement_for(resource_type),
                using=write_db(),
            ),
        )
        return None if failed is None else failed.status

    def drf_status(if_match):
        When.http.post(
            list_url,
            data=component(name="new", **WIP),
            format="json",
            headers={BRANCH_HEADER: "wip", "If-Match": if_match},
        )
        return Http.response.status_code

    assert core_status('"stale"') == drf_status('"stale"') == 412
    assert core_status(wip_etag) is None
    assert drf_status(wip_etag) == 201


@pytest.mark.django_db
@pytest.mark.parametrize("uniform", [True, False])
@test_context
def test_core_load_of_an_unserved_method_answers_the_drf_405(
    admin_login, todo_list, uniform
):
    """`load_for_read` fails closed for a method the type does not serve,
    after the request check, as DRF answers it."""
    from unittest import mock

    from scrud_django.core import resources
    from scrud_django.core.types import TypeOptions

    resource_type = todo_list.resource_type
    request = request_by("get", admin_login)
    with mock.patch(
        "scrud_django.core.writes.type_options",
        return_value=TypeOptions(methods={"PUT"}),
    ):
        with pytest.raises(MethodNotAllowedError) as refused:
            resources.load_for_read(
                request,
                resource_type,
                todo_list.pk,
                requirement=requirement_for(resource_type),
                method="GET",
            )
    assert str(refused.value.detail) == 'Method "GET" not allowed.'
    assert refused.value.status_code == 405
    with uniform_errors_set(uniform):
        status, _body, _headers = error_answer(refused.value)
    assert status == 405


@pytest.mark.django_db
@test_context
def test_core_load_of_a_version_still_serves_get(admin_login):
    from scrud_django.core import resources

    make_wip(admin_login)
    version = a_version()
    resource_type = rtype(UI_BRANCH_VERSION_SLUG)
    for method in ("GET", "HEAD"):
        got = resources.load_for_read(
            request_by(method.lower(), admin_login),
            resource_type,
            version.pk,
            requirement=requirement_for(resource_type),
            method=method,
        )
        assert got.pk == version.pk
