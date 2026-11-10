"""Large UI items on every path that reads them.

MySQL sorts a result with every column it selects. A row whose JSON content
is larger than the server's ``sort_buffer_size`` (256 KB by default) fails
the sort with error 1038 ("Out of sort memory"). So no query that reads
item or version content may ask the database to sort it, including
implicitly: ``Resource.Meta.ordering`` is ``["id"]``. These tests write
items near the CSS size limit (262144 characters) and enough of them that a
version snapshot of main is well over 256 KB, then exercise rendering, the
CSS file, the JSON list, writes, pull, push, restore, version reads and
branch deletion. On SQLite and PostgreSQL they pass either way; on MySQL
(``SCRUD_TEST_MYSQL_*``) they fail with 1038 if a query sorts content."""

import pytest
from django.db import DEFAULT_DB_ALIAS
from django.urls import reverse
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django.ui.branches import UI_CSS_TYPE_URI
from scrud_django.ui.branches.versions import current_version, snapshot

from .fixtures import *  # noqa
from .steps import *  # noqa: F403, F401
from .test_ui_branch_css import api_at_api_path, css_body, css_hrefs, get_css  # noqa: F401
from .test_ui_branch_prefixes import api_url, get_html
from .test_ui_branch_push import create, make_wip, own_rows, push
from .test_ui_branch_render import may_select_branches

MAX_CSS = 262144
LARGE = "a" * MAX_CSS


def css(name, text=LARGE, **fields):
    return css_body(name=name, css=text, **fields)


@pytest.mark.django_db
@test_context
def test_a_page_and_its_css_render_with_a_max_size_item(
    admin_login, todo_list, todo_reader_login
):
    Given.http.force_authenticate(admin_login)
    create("ui-css", css("site"))
    Given.http.force_authenticate(may_select_branches(todo_reader_login))
    [href] = css_hrefs(get_html(api_url(todo_list)))
    assert get_css(href).content == LARGE.encode()


@pytest.mark.django_db
@test_context
def test_the_json_list_reads_max_size_items(admin_login):
    Given.http.force_authenticate(admin_login)
    create("ui-css", css("site"))
    When.http.get(reverse("ui-css-list"), HTTP_ACCEPT="application/json")
    Then.http.status_code_is(200)
    assert LARGE in Http.response.content.decode()


@pytest.mark.django_db
@test_context
def test_main_takes_writes_once_its_versions_are_large(admin_login):
    """Each write of main records a version holding all of main's items, so
    by the third write a version row is over 256 KB, and the next write
    reads the latest version and every own item again."""
    Given.http.force_authenticate(admin_login)
    for index in range(4):
        create("ui-css", css(f"site-{index}"))
    latest = snapshot("main", current_version("main", DEFAULT_DB_ALIAS))
    assert len(latest["items"]["css"]) == 4


@pytest.mark.django_db
@test_context
def test_pull_push_and_restore_move_max_size_items(admin_login):
    Given.http.force_authenticate(admin_login)
    create("ui-css", css("site"))
    make_wip(admin_login)
    create("ui-css", css("site", "b" * MAX_CSS, branch="wip", rbac_context="alice"))
    # A pull reads both branches' items and records a version.
    When.http.get(reverse("ui-branches-list"), HTTP_ACCEPT="application/json")
    Then.http.status_code_is(200)
    wip = next(
        row
        for row in Http.response.json()["content"]
        if row["content"]["name"] == "wip"
    )
    When.http.put(
        wip["href"],
        data={**wip["content"], "pull": True},
        format="json",
        HTTP_IF_MATCH=wip["etag"],
    )
    Then.http.status_code_is(200)
    before = current_version("main", DEFAULT_DB_ALIAS)
    push()
    Then.http.status_code_is(201)
    assert own_rows(UI_CSS_TYPE_URI, "wip") == []
    assert [item["css"][0] for item in own_rows(UI_CSS_TYPE_URI, "main")] == ["b"]
    # Restore main as it was before the push: the large item comes back.
    push(source=None, restore=before)
    Then.http.status_code_is(201)
    assert [item["css"][0] for item in own_rows(UI_CSS_TYPE_URI, "main")] == ["a"]


@pytest.mark.django_db
@test_context
def test_versions_read_and_branches_delete_with_max_size_items(admin_login):
    Given.http.force_authenticate(admin_login)
    wip = make_wip(admin_login)
    create("ui-css", css("site", branch="wip", rbac_context="alice"))
    create("ui-css", css("more", branch="wip", rbac_context="alice"))
    When.http.get(reverse("ui-branch-versions-list"), HTTP_ACCEPT="application/json")
    Then.http.status_code_is(200)
    assert LARGE in Http.response.content.decode()
    When.http.delete(api_url(wip), HTTP_IF_MATCH=f'"{wip.etag}"')
    Then.http.status_code_is(204)
    assert own_rows(UI_CSS_TYPE_URI, "wip") == []
