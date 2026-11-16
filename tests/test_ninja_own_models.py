"""`scrud_django.ninja.workflow_check`: an app's own model (``tests.Article``)
served by a hand-written ninja router (`tests.own_models`) gets scrud's
workflow check, its signals and its workflow actions, as a resource does."""

import logging

import pytest
from django.db import connections
from django.db import router as db_router
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext
from django.urls import get_resolver
from ninja import Schema
from scoped_rbac import access

from scrud_django.core import transitions
from scrud_django.core.authz import _workflow_authorized
from scrud_django.exceptions import HiddenWorkflowTransitionError
from scrud_django.ninja import ModelWorkflow, workflow_check
from scrud_django.ninja.errors import PROBLEM_JSON
from scrud_django.registration import (
    ResourceRegistration,
    workflow_policy_resource_type,
)

from .fixtures import assign_role
from .models import Article
from .workflow_actions import article_calls
from .workflow_defaults import ARTICLE, article_state

pytestmark = [pytest.mark.urls("tests.own_model_urls"), pytest.mark.django_db]

LIST = "/api/articles/"
# The problem scrud answers a workflow error with (scrud's errors have no
# ``code`` member: the ``type`` tells them apart).
WORKFLOW_ERROR = "http://api.openteams.com/http-problem/workflow-transition-invalid"
DEFAULT_POLICY = {"resource_type": ARTICLE, "default": True}
WRITER = {
    "http.GET": [ARTICLE, f"{ARTICLE}/list"],
    "http.POST": [ARTICLE],
    "http.PUT": [ARTICLE],
    "http.DELETE": [ARTICLE],
}
READER = {"http.GET": [ARTICLE, f"{ARTICLE}/list"]}


def scrud_workflow(*transitions):
    """A grant conditioned on the ``scrud_workflow`` operator (the README's
    syntax): the workflow transitions it lists, and nothing else."""
    return {
        ARTICLE: {
            "condition": {
                "operator": "scrud_workflow",
                "authorized_transitions": list(transitions),
            }
        }
    }


def item(pk):
    return f"{LIST}{pk}/"


@pytest.fixture
def registry(monkeypatch, db):
    """The registry of governed models as the test found it, restored
    afterwards. Both URLconfs that govern ``Article`` are imported first, so
    that neither governs it into the copy the test discards."""
    get_resolver().url_patterns  # noqa: B018 (tests.urls governs Article)
    import tests.own_model_urls  # noqa: F401 (tests.own_models governs it)

    monkeypatch.setattr(transitions, "_GOVERNED", dict(transitions._GOVERNED))


@pytest.fixture
def calls():
    """`article_calls`, emptied before and after the test."""
    article_calls.clear()
    yield article_calls
    article_calls.clear()


@pytest.fixture
def writer(client):
    """A client logged in as a non-superuser who may read, list, create,
    update and delete articles in context "a" (superusers bypass the
    workflow)."""
    client.force_login(assign_role(WRITER, "a"))
    return client


def draft_in_a():
    with transitions.checked_save(Article):
        return Article.objects.create(title="t", state="draft", rbac_context="a")


def assert_workflow_error(response):
    assert response.status_code == 400, response.content
    assert response["Content-Type"] == PROBLEM_JSON
    body = response.json()
    assert body["type"] == WORKFLOW_ERROR
    assert body["title"] == "Invalid workflow transition"
    assert body["status"] == 400
    return body


def own_models():
    from tests import own_models

    return own_models


def test_article_state_is_the_routers_state():
    # tests.urls governs Article with article_state, tests.own_models with
    # ArticleOut's dump: whichever is imported last governs, so they agree.
    article = draft_in_a()
    workflow = own_models().ARTICLE_WORKFLOW
    assert article_state(article) == workflow.state(article)
    unsaved = Article(title="u", state="published", rbac_context="b")
    assert article_state(unsaved) == workflow.state(unsaved)


def test_an_allowed_create_and_transition(writer):
    response = writer.post(
        LIST,
        {"title": "t", "state": "draft", "rbac_context": "a"},
        content_type="application/json",
    )
    assert response.status_code == 201, response.content
    pk = response.json()["id"]
    assert Article.objects.get(pk=pk).state == "draft"

    response = writer.put(
        item(pk), {"state": "published"}, content_type="application/json"
    )
    assert response.status_code == 200, response.content
    assert response.json()["state"] == "published"
    assert Article.objects.get(pk=pk).state == "published"


def test_a_refused_transition_is_the_workflow_error(writer):
    article = draft_in_a()
    response = writer.put(
        item(article.pk), {"state": "archived"}, content_type="application/json"
    )
    body = assert_workflow_error(response)
    # As the resource routes answer it: the writer may read the article, so
    # the error names the settings default and the states.
    assert body == {
        "type": WORKFLOW_ERROR,
        "title": "Invalid workflow transition",
        "detail": DEFAULT_POLICY,
        "policy": DEFAULT_POLICY,
        "prior_state": "draft",
        "proposed_state": "archived",
        "message": "Invalid workflow transition",
        "status": 400,
    }
    article.refresh_from_db()
    assert (article.state, article.rbac_context) == ("draft", "a")


def test_a_refused_initial_state(writer):
    response = writer.post(
        LIST,
        {"title": "t", "state": "published", "rbac_context": "a"},
        content_type="application/json",
    )
    body = assert_workflow_error(response)
    assert body["message"] == "The request is not a valid initial state."
    assert body["policy"] == DEFAULT_POLICY
    assert not Article.objects.exists()


def test_a_hidden_error_for_a_writer_who_may_not_read(client):
    client.force_login(assign_role({"http.PUT": [ARTICLE]}, "a"))
    article = draft_in_a()
    response = client.put(
        item(article.pk), {"state": "archived"}, content_type="application/json"
    )
    body = assert_workflow_error(response)
    assert body["message"] == HiddenWorkflowTransitionError.MESSAGE
    assert body["detail"] == HiddenWorkflowTransitionError.MESSAGE
    assert body["policy"] is None
    assert body["prior_state"] is None
    assert body["proposed_state"] is None
    assert "draft" not in response.content.decode()
    article.refresh_from_db()
    assert article.state == "draft"


def test_signals_and_actions_fire_on_commit(
    writer, calls, django_capture_on_commit_callbacks, caplog
):
    db = db_router.db_for_write(Article)
    with caplog.at_level(logging.WARNING, logger="scrud_django.workflow"):
        with django_capture_on_commit_callbacks(execute=True):
            response = writer.post(
                LIST,
                {"title": "t", "state": "draft", "rbac_context": "a"},
                content_type="application/json",
            )
            assert response.status_code == 201, response.content
            pk = response.json()["id"]
        with django_capture_on_commit_callbacks(execute=True):
            response = writer.put(
                item(pk), {"state": "published"}, content_type="application/json"
            )
            assert response.status_code == 200, response.content
    assert [kind for kind, _ in calls] == ["on_save", "on_save"]
    for (_, kwargs), action in zip(calls, ["CREATE", "UPDATE"], strict=True):
        assert kwargs["resource_type_uri"] == ARTICLE
        assert isinstance(kwargs["instance"], Article)
        assert kwargs["instance"].pk == pk
        assert kwargs["action"] == action
        assert kwargs["using"] == db
        assert kwargs["update_fields"] is None
        assert kwargs["transition_error"] is None
    created, updated = (kwargs for _, kwargs in calls)
    assert created["transition_data"].transition == "__INITIALIZE__"
    assert created["transition_data"].proposed_state == "draft"
    assert dict(created["transitions"]) == {"a": created["transition_data"]}
    assert updated["transition_data"].prior_state == "draft"
    assert updated["transition_data"].proposed_state == "published"
    assert updated["transition_data"].transition == "publish"
    # The toolkit's saves checked the workflow: none is logged as skipping it.
    assert [r for r in caplog.records if r.name == "scrud_django.workflow"] == []

    with django_capture_on_commit_callbacks(execute=True):
        response = writer.delete(item(pk))
        assert response.status_code == 204, response.content
    assert not Article.objects.filter(pk=pk).exists()
    assert len(calls) == 3
    kind, kwargs = calls[2]
    assert kind == "on_delete"
    assert kwargs["pk"] == pk
    assert kwargs["resource_type_uri"] == ARTICLE
    assert kwargs["action"] == "DELETE"
    assert kwargs["using"] == db
    assert isinstance(kwargs["instance"], Article)


def put_request(user, method="put"):
    request = getattr(RequestFactory(), method)("/api/articles/")
    request.user = user
    # As Django's test client does: the session's CSRF check passes.
    request._dont_enforce_csrf_checks = True
    return request


def test_nothing_fires_when_the_write_rolls_back(
    calls, django_capture_on_commit_callbacks
):
    workflow = own_models().ARTICLE_WORKFLOW
    request = put_request(assign_role(WRITER, "a"))
    article = draft_in_a()

    def moved_to_b():
        # The workflow check passes in "a"; scoped-rbac's post-save check
        # refuses the move to "b", where the user holds no grant.
        article.state = "published"
        article.rbac_context = "b"
        article.save()
        return article

    with django_capture_on_commit_callbacks(execute=True):
        with pytest.raises(access.AccessDenied):
            workflow.save_checked(
                request,
                instance=article,
                values={"state": "published"},
                save=moved_to_b,
            )
    stored = Article.objects.get(pk=article.pk)
    assert (stored.state, stored.rbac_context) == ("draft", "a")
    assert calls == []
    # The workflow check's mark did not outlive the refused write.
    assert not _workflow_authorized(request, type_iri=ARTICLE, stored_pk=article.pk)

    # A second, valid write on the same request is checked afresh.
    def published():
        stored.state = "published"
        stored.save()
        return stored

    with django_capture_on_commit_callbacks(execute=True):
        saved = workflow.save_checked(
            request, instance=stored, values={"state": "published"}, save=published
        )
    assert Article.objects.get(pk=article.pk).state == "published"
    assert saved.pk == article.pk
    assert [kind for kind, _ in calls] == ["on_save"]
    assert calls[0][1]["transition_data"].proposed_state == "published"
    assert not _workflow_authorized(request, type_iri=ARTICLE, stored_pk=article.pk)


def test_a_stored_policy_governs_the_model(writer):
    # In context "a", a stored policy whose only transition is draft ->
    # archived (the settings default's are draft -> published -> archived).
    workflow_policy_resource_type()
    ResourceRegistration.register(
        {
            "resource_type": ARTICLE,
            "rbac_context": "a",
            "initial_states": {"draft": 'state == "draft"'},
            "states": {
                "published": 'state == "published"',
                "archived": 'state == "archived"',
            },
            "transitions": {"archive": {"from": "draft", "to": "archived"}},
        },
        "workflow-policies",
    )
    first, second = draft_in_a(), draft_in_a()
    response = writer.put(
        item(first.pk), {"state": "archived"}, content_type="application/json"
    )
    assert response.status_code == 200, response.content
    assert Article.objects.get(pk=first.pk).state == "archived"

    response = writer.put(
        item(second.pk), {"state": "published"}, content_type="application/json"
    )
    assert_workflow_error(response)
    assert Article.objects.get(pk=second.pk).state == "draft"


def test_state_of_must_give_a_mapping(registry):
    workflow = workflow_check(
        ARTICLE, model=Article, state_of=lambda article: article.state
    )
    assert isinstance(workflow, ModelWorkflow)
    request = put_request(assign_role(WRITER, "a"))
    with pytest.raises(TypeError, match="tests.Article"):
        workflow.save_checked(
            request,
            instance=None,
            values={"title": "t", "state": "draft", "rbac_context": "a"},
            save=lambda: Article.objects.create(
                title="t", state="draft", rbac_context="a"
            ),
        )
    assert not Article.objects.exists()


def test_workflow_check_takes_one_of_schema_out_or_state_of(registry):
    with pytest.raises(TypeError, match="schema_out"):
        workflow_check(ARTICLE, model=Article)
    with pytest.raises(TypeError, match="schema_out"):
        workflow_check(
            ARTICLE,
            model=Article,
            schema_out=own_models().ArticleOut,
            state_of=article_state,
        )


def test_options_allow(writer, client):
    article = draft_in_a()
    response = writer.options(item(article.pk))
    assert response.status_code == 204, response.content
    allowed = {method.strip() for method in response["Allow"].split(",")}
    assert allowed == {"GET", "PUT", "DELETE", "HEAD", "OPTIONS"}

    client.force_login(assign_role(READER, "a"))
    response = client.options(item(article.pk))
    assert response.status_code == 204, response.content
    allowed = {method.strip() for method in response["Allow"].split(",")}
    assert allowed == {"GET", "HEAD", "OPTIONS"}


def test_a_stale_put_applies_to_the_current_row(
    calls, django_capture_on_commit_callbacks
):
    # Two writers load a draft; the first publishes and commits; the second
    # PUTs only a title, from the row it loaded before.
    workflow = own_models().ARTICLE_WORKFLOW
    request = put_request(assign_role(WRITER, "a"))
    stale = Article.objects.get(pk=draft_in_a().pk)
    Article.objects.filter(pk=stale.pk).update(state="published")
    values = {"title": "x"}
    db = db_router.db_for_write(Article)
    with django_capture_on_commit_callbacks(execute=True):
        with CaptureQueriesContext(connections[db]) as queries:
            saved = workflow.save_checked(
                request,
                instance=stale,
                values=values,
                save=own_models().updater(stale, values),
            )
    stored = Article.objects.get(pk=stale.pk)
    # Not reverted to the draft the second writer loaded.
    assert (stored.state, stored.title) == ("published", "x")
    assert saved.state == "published"
    [(kind, kwargs)] = calls
    assert kind == "on_save"
    data = kwargs["transition_data"]
    assert (data.prior_state, data.proposed_state) == ("published", "published")
    assert data.transition == "__SAME_STATE__"
    if connections[db].features.has_select_for_update:
        # Read again under a lock held until the write commits (SQLite
        # ignores FOR UPDATE, and Django leaves it out there).
        assert any("FOR UPDATE" in query["sql"] for query in queries), [
            query["sql"] for query in queries
        ]


def test_a_row_deleted_meanwhile_is_missing(calls, django_capture_on_commit_callbacks):
    workflow = own_models().ARTICLE_WORKFLOW
    request = put_request(assign_role(WRITER, "a"))
    stale = draft_in_a()
    Article.objects.filter(pk=stale.pk).delete()
    values = {"state": "published"}
    with django_capture_on_commit_callbacks(execute=True):
        with pytest.raises(access.ObjectNotFound):
            workflow.save_checked(
                request,
                instance=stale,
                values=values,
                save=own_models().updater(stale, values),
            )
    assert not Article.objects.exists()
    assert calls == []


def test_a_scrud_workflow_grant_allows_its_transitions(client):
    # The README's per-transition grants, for an own model: a create into
    # the initial state, and the publish transition, nothing else.
    client.force_login(
        assign_role(
            {
                "http.GET": [ARTICLE, f"{ARTICLE}/list"],
                "http.POST": scrud_workflow("__INITIALIZE__"),
                "http.PUT": scrud_workflow("publish"),
            },
            "a",
        )
    )
    response = client.post(
        LIST,
        {"title": "t", "state": "draft", "rbac_context": "a"},
        content_type="application/json",
    )
    assert response.status_code == 201, response.content
    pk = response.json()["id"]

    response = client.options(item(pk))
    allowed = {method.strip() for method in response["Allow"].split(",")}
    # As on the resource routes: a PUT the grant may allow is listed; a
    # DELETE, which the workflow check does not cover, is not.
    assert allowed == {"GET", "PUT", "HEAD", "OPTIONS"}

    response = client.put(
        item(pk), {"state": "published"}, content_type="application/json"
    )
    assert response.status_code == 200, response.content
    assert Article.objects.get(pk=pk).state == "published"

    # The policy allows published -> archived; the grant does not.
    response = client.put(
        item(pk), {"state": "archived"}, content_type="application/json"
    )
    body = assert_workflow_error(response)
    assert body["message"] == (
        "The user isn't authorized to perform this state transition on this resource."
    )
    assert (body["prior_state"], body["proposed_state"]) == ("published", "archived")
    assert Article.objects.get(pk=pk).state == "published"
    assert client.delete(item(pk)).status_code == 403


class StrictArticleOut(Schema):
    id: int
    title: str
    state: str
    rbac_context: str


def test_a_schema_requiring_the_key_says_so_on_a_create(registry):
    workflow = workflow_check(ARTICLE, model=Article, schema_out=StrictArticleOut)
    request = put_request(assign_role(WRITER, "a"), method="post")
    values = {"title": "t", "state": "draft", "rbac_context": "a"}
    with pytest.raises(TypeError) as raised:
        workflow.save_checked(
            request,
            instance=None,
            values=values,
            save=lambda: Article.objects.create(**values),
        )
    message = str(raised.value)
    assert "StrictArticleOut" in message
    assert "tests.Article" in message
    assert "id: " in message
    assert "state_of=" in message
    assert not Article.objects.exists()
    # A saved row has its key: its state is the schema's JSON.
    article = draft_in_a()
    assert workflow.state(article) == article_state(article)


def test_a_save_that_raises_rolls_back(calls, django_capture_on_commit_callbacks):
    workflow = own_models().ARTICLE_WORKFLOW
    request = put_request(assign_role(WRITER, "a"), method="post")
    values = {"title": "t", "state": "draft", "rbac_context": "a"}
    marked = []

    def failing_save():
        # The workflow check passed and marked the request; the row is
        # written, then the save fails.
        marked.append(_workflow_authorized(request, type_iri=ARTICLE, stored_pk=None))
        Article.objects.create(**values)
        raise RuntimeError("the save failed")

    with django_capture_on_commit_callbacks(execute=True):
        with pytest.raises(RuntimeError, match="the save failed"):
            workflow.save_checked(
                request, instance=None, values=values, save=failing_save
            )
    assert marked == [True]
    assert not Article.objects.exists()
    assert calls == []
    assert not _workflow_authorized(request, type_iri=ARTICLE, stored_pk=None)
