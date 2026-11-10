"""Who may read a branch through another: a parent needs GET (ruling R36),
publishing at a prefix is site-level (ruling R37), and a JSON list never
answers a refused selection with another branch's items (ruling R40). These
tests serve the API at ``/api/`` (``tests.mounted_urls``)."""

import pytest
from django.urls import reverse
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django.ui.branches import UI_BRANCH_TYPE_URI

from .fixtures import *  # noqa
from .steps import *  # noqa: F403, F401
from .test_ui_branch_css import file_url, get_css, make_css
from .test_ui_branch_push import push, pusher
from .test_ui_branch_render import BRANCH_HEADER
from .test_ui_branch_types import component, create, make_branch, problem
from .test_ui_http import grant

MAIN_GET = {"http.GET": [UI_BRANCH_TYPE_URI]}


@pytest.fixture(autouse=True)
def api_at_api_path(settings):
    settings.ROOT_URLCONF = "tests.mounted_urls"


def secret_with_css():
    """Branch ``secret`` (in "bob", over main) and its CSS item ``look``."""
    secret = make_branch("secret", "bob")
    look = make_css("look", "SECRET", branch="secret", context="bob")
    return secret, look


def refusal():
    """The last response's status and problem body, for comparing two."""
    return Http.response.status_code, problem()


@pytest.mark.django_db
@test_context
def test_a_parent_the_creator_may_not_get_is_unknown(ui_alice_login):
    """The reviewer's probe (C1): alice, who may not GET ``secret``, made it
    the parent of her branch and so read its CSS. Now the parent is refused
    exactly as an unknown one is."""
    secret, _look = secret_with_css()
    Given.http.force_authenticate(ui_alice_login)
    When.http.get(reverse("ui-branches-detail", args=[secret.pk]))
    Then.http.status_code_is(404)
    body = {"name": "mine", "rbac_context": "alice", "parent": "secret"}
    create("ui-branches", body)
    Then.http.status_code_is(400)
    hidden = refusal()
    create("ui-branches", {**body, "parent": "nope"})
    Then.http.status_code_is(400)
    assert refusal()[1]["detail"] == hidden[1]["detail"].replace("secret", "nope")
    assert hidden[1]["type"].endswith("/ui-branch-invalid")
    assert "bob" not in hidden[1]["detail"]
    # Re-parenting an existing branch under it is refused the same way.
    own = make_branch("own", "alice", parent=None)
    When.http.put(
        reverse("ui-branches-detail", args=[own.pk]),
        data={"name": "own", "rbac_context": "alice", "parent": "secret"},
        format="json",
        HTTP_IF_MATCH=f'"{own.etag}"',
    )
    Then.http.status_code_is(400)
    assert refusal() == hidden


@pytest.mark.django_db
@test_context
def test_a_readable_parent_is_accepted(ui_alice_login):
    secret_with_css()
    grant(ui_alice_login, MAIN_GET, "bob")
    Given.http.force_authenticate(ui_alice_login)
    create("ui-branches", {"name": "mine", "rbac_context": "alice", "parent": "secret"})
    Then.http.status_code_is(201)


@pytest.mark.django_db
@test_context
def test_creating_a_branch_of_main_needs_get_on_main(ui_alice_login):
    """``main`` is the parent as stored (or, virtual, as reserved): in
    context ""."""
    Given.http.force_authenticate(ui_alice_login)
    body = {"name": "alice-wip", "rbac_context": "alice", "parent": "main"}
    create("ui-branches", body)
    Then.http.status_code_is(400)
    assert problem()["type"].endswith("/ui-branch-invalid")
    grant(ui_alice_login, MAIN_GET, "")
    create("ui-branches", body)
    Then.http.status_code_is(201)


@pytest.mark.django_db
@test_context
def test_an_unchanged_parent_needs_no_get(ui_alice_login):
    """An update that keeps the parent is not a new read of it."""
    own = make_branch("own", "alice")
    Given.http.force_authenticate(ui_alice_login)
    When.http.put(
        reverse("ui-branches-detail", args=[own.pk]),
        data={"name": "own", "rbac_context": "alice", "parent": "main"}
        | {"label": "Mine"},
        format="json",
        HTTP_IF_MATCH=f'"{own.etag}"',
    )
    Then.http.status_code_is(200)


@pytest.mark.django_db
@test_context
def test_a_working_ancestor_the_viewer_may_not_get_is_not_selectable(
    ui_alice_login,
):
    """A branch stored over one its viewer may not GET (made before, or by
    someone who could) does not render it: the CSS of ``secret`` is not
    served through ``mine``."""
    _secret, look = secret_with_css()
    make_branch("mine", "alice", parent="secret")
    Given.http.force_authenticate(ui_alice_login)
    get_css(file_url(look), status=404, **{BRANCH_HEADER: "mine"})
    grant(ui_alice_login, MAIN_GET, "bob")
    assert get_css(file_url(look), **{BRANCH_HEADER: "mine"}).content == b"SECRET"


@pytest.mark.django_db
@test_context
def test_an_item_write_on_an_unreadable_branch_is_answered_as_unknown(
    ui_author_login,
):
    """The author may POST components in "" but may not GET ``secret``: an
    item naming it gets the answer an unknown branch gets, which does not
    name its context (ruling R36: it was the follow-up ui-branch-enumeration)."""
    secret_with_css()
    Given.http.force_authenticate(ui_author_login)
    create("ui-components", component(branch="secret", rbac_context=""))
    Then.http.status_code_is(400)
    hidden = refusal()
    create("ui-components", component(branch="nope", rbac_context=""))
    Then.http.status_code_is(400)
    unknown = refusal()
    assert hidden[1]["type"] == unknown[1]["type"]
    assert hidden[1]["type"].endswith("/ui-branch-invalid")
    assert hidden[1]["detail"] == unknown[1]["detail"].replace("nope", "secret")
    assert "bob" not in hidden[1]["detail"]


@pytest.mark.django_db
@test_context
def test_a_push_naming_an_unreadable_branch_is_answered_as_unknown():
    """A pusher (pushes in "", branches in "alice") who may not GET
    ``secret``: as a source or (with the wrong context) a target, it is
    answered as an unknown branch, without its context."""
    secret_with_css()
    make_branch("wip", "alice")
    Given.http.force_authenticate(pusher())
    answers = {}
    for name in ("secret", "nope"):
        push(target="main", source=name)
        Then.http.status_code_is(400)
        answers[name] = refusal()
    assert answers["secret"][1] == {
        **answers["nope"][1],
        "detail": answers["nope"][1]["detail"].replace("nope", "secret"),
    }
    for name in ("secret", "nope"):
        push(target=name, source="wip", context="")
        Then.http.status_code_is(400)
        answers[name] = refusal()
    assert answers["secret"][1] == {
        **answers["nope"][1],
        "detail": answers["nope"][1]["detail"].replace("nope", "secret"),
    }
    assert "bob" not in answers["secret"][1]["detail"]
