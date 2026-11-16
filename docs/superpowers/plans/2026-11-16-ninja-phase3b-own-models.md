# ninja phase 3B: own models with workflow — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An app's own Django models, served through django-ninja with scoped-rbac's building blocks, get scrud's workflow authorization, its `scrud_post_save`/`scrud_post_delete` signals and its `SCRUD_WORKFLOW_ACTIONS`, as scrud's resources do.

**Architecture:** The signals and the transition computation move to the neutral core (`core/signals.py`, `core/transitions.py`); `ResourceMixin` delegates to them, so resources keep every answer. A core registry (`govern`) records each governed model's IRI, its state and its contexts, and logs the unchecked-save warning from Django's `pre_save`/`post_save`. `scrud_django.ninja.workflow_check(...)` returns a `ModelWorkflow` whose `save_checked`/`delete_checked` an app's hand-written router calls inside scoped-rbac's blocks: the workflow check, scoped-rbac's `save_checked`, and the signal, in one transaction.

**Tech Stack:** Django, django-ninja 1.7.0–1.7.3, django-scoped-rbac 0.12.1 (`scoped_rbac.ninja` blocks), scrudful 0.10.1, pytest + safetydance.

**Spec:** `docs/superpowers/specs/2026-11-11-ninja-front-end-design.md` §3.4, §3.5 item 2, Testing (toolkit tests). Its "Versioning" section is stale: 0.16.0 was released on 2026-11-14 without phase 3B/C, so this lands on `develop` under `## Unreleased`.

## Global Constraints

- Never edit upstream packages (scoped-rbac, scrudful, safetydance, safetydance-django, django-ninja). scoped-rbac 0.12.1 has no `crud_router(checks=)` (§3.5 item 2): the documented form is the hand-written router from the blocks (spec: "Until then the docs show the hand-written router built from the blocks"). Proposals are already recorded: `scoped-rbac-crud-router-checks`, `scoped-rbac-list-iri`, `scoped-rbac-options-denials` in `docs/follow-ups.md`.
- No version bump. Release notes go under a new `## Unreleased` at the top of `docs/release-notes.md`.
- `scrud_django/core/` imports nothing from `rest_framework`, `django_filters`, `scrudful.rest_framework`, `ninja` or scoped-rbac's DRF modules (`tests/test_core_imports.py`).
- Resources' behaviour is unchanged: every existing test passes unmodified. Moves are move-then-delegate.
- Saves that skip the check are logged, not refused (owner). Logger `scrud_django.workflow`, the resource warning's wording.
- Signals are sent from the toolkit's save wrapper, not a model mixin: an app's models need no scrud base class (spec §3.4 "Where they are sent").
- `sender` is the model class, `instance` the model object, `resource_type_uri` the model's IRI, `using` the write's database; `transitions`, `transition_data`, `transition_error` are computed from `state_of` as for resources (spec §3.4 "Arguments").
- `state_of` defaults to the output schema's `model_dump(mode="json")` (spec §3.4).
- Tests must pass on SQLite, PostgreSQL, MySQL and MariaDB, in any order and alone (CLAUDE.md).
- Commit messages end with `Claude-Session: https://claude.ai/code/session_01SAZeWvu8MUgWeP2fTMBG43`.

## Review Focus

1. A toolkit write that fails after the workflow check passed (scoped-rbac's post-save recheck refuses, or `save` raises): no row, no `scrud_post_save`, no workflow action, and the request's workflow mark is consumed. Pinned in Task 4.
2. A non-toolkit save of a governed model nested inside a toolkit save of another governed model (a `save()` override that saves a second model): the nested one still warns. Pinned in Task 3.
3. One model governed under two IRIs: `ImproperlyConfigured` at the second `govern`; the same model and IRI twice (a URLconf imported twice) is allowed. Pinned in Task 3.
4. `state_of` returning something that is not a `Mapping`: `TypeError` naming the model, before any check or write. Pinned in Task 4.
5. An update that moves an object out of a context with a policy and into one without: `transitions` reports `LEAVE` for the first and nothing for the second, as for resources. Pinned in Task 2.

---

### Task 1: Signals and the action runner in the core

**Files:**
- Create: `scrud_django/core/signals.py` (the content of `scrud_django/scrud_signals.py`, moved)
- Modify: `scrud_django/scrud_signals.py` (becomes a re-export of `core.signals`'s public names), `scrud_django/mixins/resource_mixin.py:24`, `scrud_django/models/resource.py:24`, `scrud_django/ui/branches/versions.py:19` (import from `scrud_django.core.signals`)
- Test: `tests/test_core_signals.py`

**Interfaces:**
- Produces: `scrud_django.core.signals.{scrud_post_save, scrud_post_delete, write_alias, action_name, WorkflowActionCall, ScrudSignalProcessor}`, the same objects as `scrud_django.scrud_signals.*`.

- [ ] **Step 1: Write the failing tests**

```python
from scrud_django import scrud_signals
from scrud_django.core import signals


def test_the_old_module_names_the_core_signals():
    for name in ("scrud_post_save", "scrud_post_delete", "write_alias",
                 "action_name", "WorkflowActionCall", "ScrudSignalProcessor"):
        assert getattr(scrud_signals, name) is getattr(signals, name), name


def test_the_actions_logger_name_is_unchanged():
    # WorkflowActionCall logs on this name; the README documents it.
    assert signals.logger.name == "scrud_django.workflow_actions"
```

(Use the module's actual logger attribute name; if it has none, assert on the `getLogger` name the module uses.)

- [ ] **Step 2: Run them, expect ImportError** — `uv run pytest tests/test_core_signals.py -q`
- [ ] **Step 3: Move the module** with `git mv` then write the shim, so history follows the code. The `dispatch_uid`s (`scrud_django.ScrudSignalProcessor.post_save`/`.post_delete`) stay as they are. Add `scrud_django/core/signals.py` to nothing in `tests/test_core_imports.py`'s deny list (it imports only Django and `scrud_django.conf`); confirm that test still passes.
- [ ] **Step 4: Run** `uv run pytest -q tests/test_core_signals.py tests/test_core_imports.py tests/test_workflow_actions*.py tests/test_signals*.py` (whichever exist), then the full suite: `uv run pytest -q`. Expected: all pass.
- [ ] **Step 5: Commit** `refactor(core): move scrud_signals into core.signals`

### Task 2: Neutral transitions

**Files:**
- Create: `scrud_django/core/transitions.py`
- Modify: `scrud_django/mixins/resource_mixin.py:42` (`Transitions` re-exported from core), `:251-330` (`get_transitions` and `transition_in` delegate), `log_workflow_errors` (delegates to `log_skipped_check`)
- Test: `tests/test_core_transitions.py`

**Interfaces:**
- Produces:
  - `Transitions` (the NamedTuple, moved): `transition_data`, `transition_error`, `transitions: MappingProxyType`, `errors: MappingProxyType`.
  - `transition_in(policy, prior: Mapping | None, proposed: Mapping, *, entering: bool, leaving: bool) -> tuple[TransitionData | None, Exception | None]` — today's `ResourceMixin.transition_in` with `self.content` as `proposed`.
  - `transitions_between(type_iri: str, *, prior: Mapping | None, proposed: Mapping, contexts_before: Sequence[str], contexts_after: Sequence[str | None]) -> Transitions` — today's `get_transitions` body with the stored content as `prior` (`None`: a create, or a row not on the write database), `rbac_contexts_of(prior)` as `contexts_before` and `self.rbac_contexts` (or `[None]`) as `contexts_after`. Policies through `workflow_policies_for(type_iri, ...)` and `default_workflow_policy_for(type_iri)` (both accept a bare IRI string).
  - `log_skipped_check(errors: Mapping, *, subject: str, type_iri: str) -> None` — one WARNING per erroring context on logger `scrud_django.workflow`, with today's text: `"Saving %s of %s skipped the workflow check, which it fails in context %r (policy %r): %s (prior state %r, proposed state %r%s)."`, `subject` replacing today's `"resource pk %s"` (the resource passes `f"resource pk {self.pk}"`, so its message is byte-identical).

- [ ] **Step 1: Write the failing tests** with plain mappings and the `tests://ToDo` default policy (`tests/static/todo_flow/workflow.json`):
  - `test_a_create_reports_initialize_in_each_context` — `prior=None`, proposed `{"status": "not started", ...}`, `contexts_after=["a"]` → `transitions == {"a": TransitionData(None, "not started", INITIALIZE)}`-equivalent (assert the `to_state` and `transition` fields), `errors == {}`.
  - `test_an_invalid_transition_is_reported_not_raised` — prior `done`, proposed `not started` → `transition_data is None`, `type(transition_error)` is the workflow error class the resource path reports, `"a" in errors`.
  - `test_leaving_a_context_with_a_policy_reports_leave` (Review Focus 5) — `contexts_before=["a"]`, `contexts_after=["b"]`, a stored policy only in `"a"` (or the settings default for both, then assert `"a"`'s entry is `TransitionData(None, <prior state>, LEAVE)` and `"b"`'s is its `INITIALIZE`).
  - `test_log_skipped_check_keeps_the_resource_wording` — with `caplog`, `log_skipped_check({"a": error}, subject="resource pk 7", type_iri="tests://ToDo")` logs exactly one WARNING on `scrud_django.workflow` starting `"Saving resource pk 7 of tests://ToDo skipped the workflow check"`.
- [ ] **Step 2: Run, expect ImportError.**
- [ ] **Step 3: Move the logic** into `core/transitions.py` and make `ResourceMixin.get_transitions`, `transition_in` and `log_workflow_errors` one-line delegations (keep their signatures and docstrings, shortened to point at the core functions).
- [ ] **Step 4: Run** `uv run pytest -q tests/test_core_transitions.py`, then the full suite. Expected: all pass, the existing workflow and signal tests unchanged.
- [ ] **Step 5: Commit** `refactor(core): neutral transitions for scrud_post_save`

### Task 3: Governed models and the unchecked-save warning

**Files:**
- Modify: `scrud_django/core/transitions.py` (registry and receivers), `scrud_django/checks.py:766` (W006)
- Modify: `tests/models.py` (add `Article`), `tests/workflow_defaults.py` (add its policy), `tests/workflow_actions.py` (add its actions)
- Create: `tests/static/article_flow/__init__.py`, `tests/static/article_flow/workflow.json`
- Test: `tests/test_core_governed.py`

**Interfaces:**
- Consumes: Task 2's `transitions_between`, `log_skipped_check`.
- Produces:
  - `govern(model: type[Model], iri: str, *, state_of: Callable[[Model], Mapping], contexts_for: Callable[[Model], Sequence[str]]) -> None`. Records the model; connects `pre_save`/`post_save` receivers for it once (`dispatch_uid=f"scrud_django.governed.{model._meta.label}"`). Same model and IRI again: replaces `state_of`/`contexts_for`. Same model, another IRI: `ImproperlyConfigured`.
  - `governed_type_iris() -> frozenset[str]`.
  - `checked_save(model: type[Model]) -> ContextManager` — while open, saves of `model` (that class only, via a `ContextVar` holding the set of models being checked) are the toolkit's: no warning.
  - The receivers: `pre_save` computes, unless `checked_save` covers the instance's class, `transitions_between(iri, prior=state_of(stored row) or None, proposed=state_of(instance), contexts_before=contexts_for(stored) or [], contexts_after=contexts_for(instance))` (the stored row re-read on `using`, as `ResourceMixin.stored_content` does) and stashes it on the instance; `post_save` pops it and calls `log_skipped_check(errors, subject=f"{model._meta.label} pk {instance.pk}", type_iri=iri)`.
- Test model and policy (exact):

```python
class Article(models.Model):
    """A model of an adopter's own, governed by a workflow through
    ``scrud_django.ninja.workflow_check`` (``tests.own_models``)."""

    title = models.CharField(max_length=200, default="")
    state = models.CharField(max_length=20, default="draft")
    rbac_context = models.CharField(max_length=255, default="")
```

```json
{
  "resource_type": "tests://Article",
  "initial_states": {"draft": "state == \"draft\""},
  "states": {"published": "state == \"published\"", "archived": "state == \"archived\""},
  "transitions": {
    "publish": {"from": "draft", "to": "published"},
    "archive": {"from": "published", "to": "archived"}
  }
}
```

  `ARTICLE = "tests://Article"`; `workflow_policies()` gains `ARTICLE: read_workflow_policy("tests.static.article_flow", "workflow.json")`; `actions` gains `ARTICLE: {"on_save": article_on_save, "on_delete": article_on_delete}`, two recorders appending `(kind, kwargs)` to a module-level list `article_calls` that tests clear.

- [ ] **Step 1: Write the failing tests** (each governs `Article` in a fixture with `state_of=lambda a: {"title": a.title, "state": a.state, "rbac_context": a.rbac_context}`, `contexts_for=lambda a: [a.rbac_context]`):
  - `test_an_unchecked_invalid_save_of_a_governed_model_warns` — create a `draft` in `"a"`, then set `state="archived"` and `save()`: one WARNING on `scrud_django.workflow` starting `f"Saving tests.Article pk {pk} of tests://Article skipped the workflow check, which it fails in context 'a'"`.
  - `test_an_unchecked_valid_save_does_not_warn` — `draft` → `published`: no WARNING (as for resources).
  - `test_a_checked_save_does_not_warn` — the invalid save inside `with checked_save(Article):` logs nothing.
  - `test_a_nested_save_of_another_governed_model_still_warns` (Review Focus 2) — govern `tests.Note` too (IRI `tests://NoteFlow`, a policy you add the same way, or reuse `ARTICLE`'s shape under its own IRI); inside `checked_save(Article)`, an invalid `Note` save warns.
  - `test_governing_a_model_under_a_second_iri_is_refused` (Review Focus 3) — `pytest.raises(ImproperlyConfigured)`; governing it again under the same IRI does not raise.
  - `test_w006_accepts_a_governed_models_iri` — with `SCRUD_WORKFLOW_ACTIONS` naming `tests://Article` and the model governed, `check_workflow_action_types(None, databases=["default"])` returns no W006 for it; for an ungoverned unknown IRI it still does.
- [ ] **Step 2: Run, expect failures.**
- [ ] **Step 3: Implement** `govern`, `governed_type_iris`, `checked_save` and the receivers in `core/transitions.py`; W006 skips IRIs in `governed_type_iris()` (it imports the URLconf first already, which is where apps call `workflow_check`; say so in its docstring).
- [ ] **Step 4: Run** `uv run pytest -q tests/test_core_governed.py tests/test_workflow_actions_setting.py`, then the full suite. The unmigrated `tests` app creates `Article`'s table itself (no migration).
- [ ] **Step 5: Commit** `feat(core): govern own models: the unchecked-save warning`

### Task 4: The ninja toolkit: `workflow_check`

**Files:**
- Create: `scrud_django/ninja/workflow.py`, `tests/own_models.py` (the hand-written router: the README's example, Task 6), `tests/own_model_urls.py`
- Modify: `scrud_django/ninja/__init__.py` (export `workflow_check`, `ModelWorkflow`)
- Test: `tests/test_ninja_own_models.py`

**Interfaces:**
- Consumes: Task 1's `scrud_post_save`/`scrud_post_delete`; Task 2's `transitions_between`; Task 3's `govern`, `checked_save`; `core.workflow.{workflow_check, Create, Update, authorizing_write}`; `core.authz._consume_workflow_mark`; scoped-rbac's `scoped_rbac.ninja.save_checked` and `scoped_rbac.access.object_contexts`.
- Produces:
  - `workflow_check(iri: str, *, model: type[Model], schema_out: type[ninja.Schema] | None = None, state_of: Callable[[Model], Mapping] | None = None, contexts_for: Callable[[Model], Sequence[str]] | None = None) -> ModelWorkflow`. Exactly one of `schema_out`/`state_of` (else `TypeError`). Default `state_of`: `schema_out.from_orm(obj).model_dump(mode="json")`. Default `contexts_for`: `scoped_rbac.access.object_contexts`. Calls `govern(model, iri, ...)`.
  - `ModelWorkflow` (frozen dataclass: `iri`, `model`, `state_of`, `contexts_for`) with:
    - `state(obj) -> Mapping` — `state_of(obj)`, `TypeError` naming the model when it is not a `Mapping` (Review Focus 4).
    - `save_checked(request, *, instance: Model | None, values: Mapping[str, Any], save: Callable[[], Model], queryset: QuerySet | None = None) -> Model`, in this order, all in `transaction.atomic(using=router.db_for_write(model, instance=instance))`:
      1. `prior = state(instance)` for an update; `applied` = a copy of `instance` (or `model()`) with `values` set; `proposed = state(applied)`.
      2. `core.workflow.workflow_check(request, type_iri=iri, write=Create(proposed, contexts_after=contexts_for(applied)))`, or `Update(prior, proposed, readable=<the requester holds http.GET on iri in contexts_for(instance), as scoped-rbac's item_check decides it>, stored_pk=instance.pk, contexts_before=contexts_for(instance), contexts_after=contexts_for(applied))`.
      3. Inside `authorizing_write(request, covered=True, authorized=True, type_iri=iri, stored_pk=instance.pk if instance else None)` and `checked_save(model)`: `obj = scoped_rbac.ninja.save_checked(request, iri, instance=instance, save=save, model=model, contexts_for=contexts_for, queryset=queryset)`.
      4. `transitions_between(iri, prior=prior, proposed=state(obj), contexts_before=..., contexts_after=contexts_for(obj))`, then `scrud_post_save.send(sender=model, instance=obj, update_fields=None, using=db, resource_type_uri=iri, transition_data=..., transition_error=..., transitions=..., action="CREATE"|"UPDATE")`.
      The workflow mark is consumed in a `finally` (`_consume_workflow_mark`).
    - `delete_checked(request, *, instance: Model, delete: Callable[[], object] | None = None) -> None` — in the same transaction: `delete()` (default `instance.delete`), then `scrud_post_delete.send(sender=model, instance=instance, pk=<pk before the delete>, using=db, resource_type_uri=iri, action="DELETE")`. No workflow check on a delete (follow-up `delete-no-workflow-check`).
  - `tests/own_models.py`: `ARTICLE_WORKFLOW = workflow_check("tests://Article", model=Article, schema_out=ArticleOut)` and `router` — a `ScrudfulRouter` built as scoped-rbac's `crud_router` body is (`scoped_rbac/ninja/crud.py:395-572`): list (`list_check`, `checked_list`), create (`resolve_user`, `request_json`, `authorize_write`, `validate_body`, then `ARTICLE_WORKFLOW.save_checked`), item GET, PUT (`ARTICLE_WORKFLOW.save_checked(instance=obj, ...)`) and DELETE (`ARTICLE_WORKFLOW.delete_checked`), with `rbac_load(Article, ARTICLE, post_save_checked=True)` and `item_check(ARTICLE, load=load)`. Only public blocks; where `crud_router` uses a private helper (`_allowed_as_drf`, `_conditional_create_atomic`), leave it out and note it in the module docstring. `tests/own_model_urls.py` mounts `scrud_django.ninja.api(types=[], urls_namespace="own-models")` with `api.add_router("articles/", router)` at `path("api/", api.urls)` and includes `scoped_rbac.semantic_urls`.

- [ ] **Step 1: Write the failing tests** (`pytestmark = [pytest.mark.urls("tests.own_model_urls"), pytest.mark.django_db]`; a non-superuser with a role granting `http.GET`, `http.POST`, `http.PUT`, `http.DELETE` on `tests://Article` and `tests://Article/list` in context `"a"`, logged in with `force_login`; superusers bypass the workflow, so never use one):
  - `test_an_allowed_create_and_transition` — POST `{"title": "t", "state": "draft", "rbac_context": "a"}` → 201; PUT `state="published"` → 200.
  - `test_a_refused_transition_is_the_workflow_error` — PUT `draft` → `archived` → the status and problem body `core.workflow` gives on the resource routes for the same refusal (assert status, `Content-Type: application/problem+json` and the body's `code`), and the row is unchanged.
  - `test_a_refused_initial_state` — POST with `state="published"` → refused, no row.
  - `test_a_hidden_error_for_a_writer_who_may_not_read` — a role with `http.PUT` but not `http.GET`: the refusal does not name the states (the `HiddenWorkflowTransitionError` answer).
  - `test_signals_and_actions_fire_on_commit` — with `django_capture_on_commit_callbacks(execute=True)`: `article_calls` holds one `("on_save", kwargs)` per write with `kwargs["resource_type_uri"] == "tests://Article"`, `kwargs["instance"]` an `Article`, `kwargs["action"]` `"CREATE"` then `"UPDATE"`, `kwargs["transition_data"].to_state == "published"`; a DELETE adds `("on_delete", ...)` with `kwargs["pk"]`.
  - `test_nothing_fires_when_the_write_rolls_back` (Review Focus 1) — make scoped-rbac's post-save recheck refuse (a `save` that writes `rbac_context="b"`, where the user has no grant) → the write is refused, no row changed, `article_calls == []` after the on-commit callbacks, and `request` carries no workflow mark (assert through a second, valid write in the same test succeeding).
  - `test_a_stored_policy_governs_the_model` — register a stored `WorkflowPolicy` resource (`ResourceRegistration.register(<policy>, "workflow-policies")`, content `{"resource_type": "tests://Article", "rbac_context": "a", ...}` whose only transition is `draft` → `archived`): in context `"a"` PUT `draft` → `archived` → 200 and `draft` → `published` refused, so the stored policy, not the settings default, applies.
  - `test_state_of_must_give_a_mapping` (Review Focus 4) — `workflow_check(..., state_of=lambda a: a.state).save_checked(...)` → `TypeError` mentioning `tests.Article`, before any row is written.
  - `test_workflow_check_takes_one_of_schema_out_or_state_of` — neither, or both → `TypeError`.
  - `test_options_allow` — OPTIONS on an item → `Allow` lists `GET, PUT, DELETE, HEAD, OPTIONS` for the writer (assert the set, not the order) and omits `PUT`/`DELETE` for a reader.
- [ ] **Step 2: Run, expect ImportError.**
- [ ] **Step 3: Implement** `scrud_django/ninja/workflow.py`, `tests/own_models.py`, `tests/own_model_urls.py`.
- [ ] **Step 4: Run** `uv run pytest -q tests/test_ninja_own_models.py`, then the full suite, `uv run ruff check .`, `uv run ruff format --check .`.
- [ ] **Step 5: Commit** `feat(ninja): workflow_check for an app's own models`

### Task 5: The demo's worked example

**Files:**
- Create: `demo/articles/__init__.py`, `demo/articles/apps.py`, `demo/articles/models.py` (an `Article` with `title`, `state`, `rbac_context`, as the test model), `demo/articles/migrations/0001_initial.py` (generated), `demo/articles/api.py` (the router, as `tests/own_models.py`), `demo/articles/workflow.py` (a two-state policy: `draft` → `published`, IRI `demo://Article`)
- Modify: `demo/demo/settings.py` (`INSTALLED_APPS` gains `articles`; `SCRUD_WORKFLOW["DEFAULT_WORKFLOW_POLICIES"]` names `articles.workflow.policies`, merged with any existing), `demo/demo/urls.py` (`path("ninja/", <a scrud api(types=[], urls_namespace="demo-ninja") with add_router("articles/", router)>.urls)`), `demo/demo/management/commands/seed_todo_demo.py` (prints the articles URL)

- [ ] **Step 1:** Add the app and generate its migration: `cd demo && uv run python manage.py makemigrations articles`.
- [ ] **Step 2: Verify** `cd demo && uv run python manage.py check` (no errors; no W006) and `uv run python manage.py makemigrations --check --dry-run` (no changes).
- [ ] **Step 3: Verify by hand** on a scratch database (memory: the demo settings shim recipe): migrate, create a user with a role on `demo://Article` and `demo://Article/list` in `""`, POST a draft and PUT it to `published` (200), then PUT a new draft straight to an unknown state (refused). Record the commands and answers in the task report.
- [ ] **Step 4: Commit** `docs(demo): a model with a two-state workflow on ninja`

### Task 6: Documentation

**Files:**
- Modify: `README.md` (a section "Your own models with workflow (ninja)" after "django-ninja (preview)"), `docs/release-notes.md` (new `## Unreleased` with `### Added` and `### Changed`), `docs/follow-ups.md`, `CLAUDE.md` (Architecture: one paragraph on `core.signals`, `core.transitions`, `ninja/workflow.py`)
- Test: `tests/test_readme_examples.py` (create, or extend the module that holds the Basic-auth hook's README check)

- [ ] **Step 1: Write the failing test** `test_the_readme_own_model_router_is_the_tested_one`: the README's python block under the new section, from `ARTICLE_WORKFLOW = workflow_check(` to the end of the block, equals the same span of `tests/own_models.py`'s source (whitespace-stripped lines).
- [ ] **Step 2: Write the docs.** The README section covers: `workflow_check`'s arguments and defaults; the router (the tested code); policies for a model are `WorkflowPolicy` resources whose `resource_type` is the model's IRI, or settings defaults; the system checks E002/E003/E005/W001 cover them, W006 accepts a governed IRI; signals and actions fire on commit with the arguments above; unchecked saves log (issue #56); deletes are not workflow-checked; why not `crud_router` (scoped-rbac 0.12.1 takes no extra checks; follow-up `scoped-rbac-crud-router-checks`). Release notes: `Added` the toolkit, `core.signals`, `core.transitions`; `Changed` `scrud_django.scrud_signals` is now an alias of `scrud_django.core.signals`. Follow-ups: `ninja-entry-write-db` gains part B's ruling (own-model routers built from scoped-rbac's blocks do not use `entry`; `ModelWorkflow.save_checked` opens its transaction on the model's write database), and a new `own-model-delete-actions` only if Task 4 found a gap. Update the spec's stale "Versioning" paragraph with one line: released as 0.16.0 after part A; phases 2 and 3C ship in 0.17.0.
- [ ] **Step 3: Run** the README test, `uv run --group docs sphinx-build -W -b html docs docs/_build`, ruff, then the full suite.
- [ ] **Step 4: Commit** `docs: own models with workflow on ninja`
