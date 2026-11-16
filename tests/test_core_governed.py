"""`scrud_django.core.transitions`' governed models: an app's own model
recorded with ``govern`` has its saves that skip the workflow check (any
save outside ``checked_save``) logged, as a resource's are, and its IRI is
one ``scrud_django.W006`` accepts."""

import logging

import pytest
from django.core import checks
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings
from django.urls import get_resolver
from django.utils.timezone import now

from scrud_django.checks import check_workflow_action_types
from scrud_django.core import transitions
from scrud_django.core.transitions import checked_save, govern, governed_type_iris
from scrud_django.registration import (
    ResourceRegistration,
    workflow_policy_resource_type,
)

from .models import Article, Note
from .workflow_actions import article_on_save
from .workflow_defaults import (
    ARTICLE,
    article_contexts,
    article_state,
    read_workflow_policy,
)

NOTE_FLOW = "tests://NoteFlow"


@pytest.fixture
def registry(monkeypatch, db):
    """The registry of governed models as the test found it, restored
    afterwards (``Article`` is governed already: the URLconfs govern it).
    The URLconfs that govern it (``tests.urls``, and ``tests.own_model_urls``
    through ``tests.own_models``) are imported first, so that their own
    ``govern`` is not undone with the test's: each is imported once. The
    receivers ``govern`` connects stay connected: they consult the registry,
    so a model it no longer holds is left alone."""
    get_resolver().url_patterns  # noqa: B018 (governs the URLconf's models)
    import tests.own_model_urls  # noqa: F401 (tests.own_models governs Article)

    monkeypatch.setattr(transitions, "_GOVERNED", dict(transitions._GOVERNED))


@pytest.fixture
def governed_article(registry):
    govern(Article, ARTICLE, state_of=article_state, contexts_for=article_contexts)


def warnings_logged(caplog):
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "scrud_django.workflow" and record.levelno == logging.WARNING
    ]


def draft_in_a():
    return Article.objects.create(title="t", state="draft", rbac_context="a")


@pytest.mark.django_db
def test_an_unchecked_invalid_save_of_a_governed_model_warns(governed_article, caplog):
    article = draft_in_a()
    article.state = "archived"
    with caplog.at_level(logging.WARNING, logger="scrud_django.workflow"):
        article.save()
    found = warnings_logged(caplog)
    assert len(found) == 1
    assert found[0].startswith(
        f"Saving tests.Article pk {article.pk} of tests://Article skipped the "
        "workflow check, which it fails in context 'a'"
    )
    # Logged, not refused.
    assert Article.objects.get(pk=article.pk).state == "archived"


@pytest.mark.django_db
def test_an_unchecked_valid_save_does_not_warn(governed_article, caplog):
    with caplog.at_level(logging.WARNING, logger="scrud_django.workflow"):
        article = draft_in_a()
        article.state = "published"
        article.save()
    assert warnings_logged(caplog) == []


@pytest.mark.django_db
def test_an_unchecked_create_in_no_initial_state_warns(governed_article, caplog):
    with caplog.at_level(logging.WARNING, logger="scrud_django.workflow"):
        article = Article.objects.create(state="published", rbac_context="a")
    found = warnings_logged(caplog)
    assert len(found) == 1
    assert found[0].startswith(f"Saving tests.Article pk {article.pk} of ")


@pytest.mark.django_db
def test_the_prior_state_is_the_stored_row(governed_article, caplog):
    # The instance in hand is not the prior state: the stored row is.
    article = draft_in_a()
    Article.objects.filter(pk=article.pk).update(state="published")
    article.state = "archived"
    with caplog.at_level(logging.WARNING, logger="scrud_django.workflow"):
        article.save()
    assert warnings_logged(caplog) == []


@pytest.mark.django_db
def test_a_checked_save_does_not_warn(governed_article, caplog):
    article = draft_in_a()
    article.state = "archived"
    with caplog.at_level(logging.WARNING, logger="scrud_django.workflow"):
        with checked_save(Article):
            article.save()
    assert warnings_logged(caplog) == []
    # Only while it is open.
    article.state = "draft"
    with caplog.at_level(logging.WARNING, logger="scrud_django.workflow"):
        article.save()
    assert len(warnings_logged(caplog)) == 1


@pytest.mark.django_db
def test_a_raw_save_does_not_warn(governed_article, caplog):
    # Fixture loading (``loaddata``) saves raw, as it does a resource, which
    # is not logged either.
    article = draft_in_a()
    article.state = "archived"
    with caplog.at_level(logging.WARNING, logger="scrud_django.workflow"):
        article.save_base(raw=True)
    assert warnings_logged(caplog) == []


@pytest.mark.django_db
def test_an_ungoverned_model_is_left_alone(registry, caplog):
    transitions._GOVERNED.pop(Article, None)
    article = draft_in_a()
    article.state = "archived"
    with caplog.at_level(logging.WARNING, logger="scrud_django.workflow"):
        article.save()
    assert warnings_logged(caplog) == []
    assert ARTICLE not in governed_type_iris()


def note_state(note):
    return {"state": note.text, "rbac_context": note.rbac_context}


def note_contexts(note):
    return [note.rbac_context]


@pytest.mark.django_db
def test_a_nested_save_of_another_governed_model_still_warns(governed_article, caplog):
    # A stored policy for Note's own IRI, shaped as Article's.
    workflow_policy_resource_type()
    policy = read_workflow_policy("tests.static.article_flow", "workflow.json")
    ResourceRegistration.register(
        {**policy, "resource_type": NOTE_FLOW, "rbac_context": "a"},
        "workflow-policies",
    )
    govern(Note, NOTE_FLOW, state_of=note_state, contexts_for=note_contexts)
    note = Note.objects.create(
        text="draft", rbac_context="a", etag="e", modified_at=now()
    )
    article = draft_in_a()
    with caplog.at_level(logging.WARNING, logger="scrud_django.workflow"):
        with checked_save(Article):
            # As a save() override of Article saving a second model would.
            article.state = "archived"
            article.save()
            note.text = "archived"
            note.save()
    found = warnings_logged(caplog)
    assert len(found) == 1
    assert found[0].startswith(
        f"Saving tests.Note pk {note.pk} of tests://NoteFlow skipped the "
        "workflow check, which it fails in context 'a'"
    )


@pytest.mark.django_db
def test_governing_a_model_under_a_second_iri_is_refused(governed_article):
    with pytest.raises(ImproperlyConfigured, match="tests.Article"):
        govern(
            Article,
            "tests://Other",
            state_of=article_state,
            contexts_for=article_contexts,
        )
    # The same IRI again (a URLconf imported twice) replaces the callables.
    govern(Article, ARTICLE, state_of=note_state, contexts_for=article_contexts)
    assert transitions._GOVERNED[Article].state_of is note_state
    assert governed_type_iris() == frozenset(
        governed.iri for governed in transitions._GOVERNED.values()
    )
    assert ARTICLE in governed_type_iris()
    assert "tests://Other" not in governed_type_iris()


def w006(found):
    return [(m.id, m.level, m.obj) for m in found if m.id == "scrud_django.W006"]


@pytest.mark.django_db
def test_w006_accepts_a_governed_models_iri(governed_article, monkeypatch):
    import tests.workflow_actions

    # SCRUD_WORKFLOW_ACTIONS names mappings by dotted path.
    monkeypatch.setattr(
        tests.workflow_actions,
        "governed_and_unknown",
        {
            ARTICLE: {"on_save": article_on_save},
            "tests://NoSuchType": {"on_save": article_on_save},
        },
        raising=False,
    )
    with override_settings(
        SCRUD_WORKFLOW_ACTIONS=["tests.workflow_actions.governed_and_unknown"]
    ):
        found = check_workflow_action_types(None, databases=["default"])
        assert w006(found) == [
            ("scrud_django.W006", checks.WARNING, "tests://NoSuchType")
        ]
        # Not governed, nor a registered resource type: warned of.
        del transitions._GOVERNED[Article]
        found = check_workflow_action_types(None, databases=["default"])
        assert [obj for (_, _, obj) in w006(found)] == [ARTICLE, "tests://NoSuchType"]
