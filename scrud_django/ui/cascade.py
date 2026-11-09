"""The stylesheet cascade and component resolution.

Presentation is resolved with system rights: stylesheets and components are read
straight from `Resource.objects` (the router's read database), never through
views or permission code. Only components in the default context ``""`` exist,
and the built-ins (``scrud:`` names) back them. Where names are duplicated (a
race in the uniqueness check), the lowest pk wins.
"""

import contextlib
import contextvars
import importlib.resources
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import cache, lru_cache

from scoped_rbac.rbac_contexts import DEFAULT_CONTEXT

from scrud_django.mixins.resource_mixin import rbac_contexts_of
from scrud_django.models import Resource
from scrud_django.ui.engines import ENGINE_NAMES, CompiledTemplate, cached_template
from scrud_django.ui.selectors import (
    Selector,
    SelectorError,
    TypeTerm,
    parse_selector,
    selector_matches,
)
from scrud_django.ui.types import UI_COMPONENT_TYPE_URI, UI_STYLESHEET_TYPE_URI

BUILTIN_PREFIX = "scrud:"
STORED_MODES = ("view", "edit")

# Built-in names without the prefix; `-edit` ones are edit components.
_VIEW_BUILTINS = ("object", "array", "string", "number", "boolean")
_ANY_BUILTINS = ("null", "uri", "json", "page", "collection", "notice")
_EDIT_BUILTINS = (
    "object-edit",
    "array-edit",
    "string-edit",
    "number-edit",
    "boolean-edit",
    "enum-edit",
    "date-edit",
)


def _builtin_mode(short_name):
    if short_name in _ANY_BUILTINS:
        return "any"
    return "edit" if short_name.endswith("-edit") else "view"


BUILTIN_NAMES = frozenset(
    BUILTIN_PREFIX + short
    for short in (*_VIEW_BUILTINS, *_ANY_BUILTINS, *_EDIT_BUILTINS)
)


@dataclass(frozen=True)
class Component:
    name: str
    engine: str
    mode: str  # "view", "edit", or "any" (built-ins only)
    template: str
    scopes: dict[str, str] = field(default_factory=dict)
    stylesheets: tuple[str, ...] = ()
    cache_key: tuple = ()

    def compiled(self) -> CompiledTemplate:
        return cached_template(self.cache_key, self.engine, self.template)

    def serves(self, mode: str) -> bool:
        return self.mode in (mode, "any")


@dataclass(frozen=True)
class Rule:
    selector: Selector
    component: str
    rank: tuple[int, ...]


@cache
def builtin_component(name: str) -> Component:
    """The built-in `name` (``scrud:...``); ``KeyError`` for an unknown name."""
    if name not in BUILTIN_NAMES:
        raise KeyError(name)
    short = name[len(BUILTIN_PREFIX) :]
    source = (
        importlib.resources.files("scrud_django.ui.builtins")
        .joinpath(short + ".html")
        .read_text(encoding="utf-8")
    )
    return Component(
        name=name,
        engine="django",
        mode=_builtin_mode(short),
        template=source,
        cache_key=("builtin", name),
    )


@lru_cache(maxsize=1)
def _default_stylesheet() -> dict:
    text = (
        importlib.resources.files("scrud_django.ui.documents")
        .joinpath("default-stylesheet.json")
        .read_text(encoding="utf-8")
    )
    return json.loads(text)


def _stored(type_uri):
    """``(pk, etag, content)`` of every resource of the type, lowest pk first."""
    return (
        Resource.objects.filter(resource_type__type_uri=type_uri)
        .order_by("pk")
        .values_list("pk", "etag", "content")
    )


def _name_of(content):
    name = content.get("name") if isinstance(content, Mapping) else None
    return name if isinstance(name, str) else None


def _stylesheet_rows():
    """``(pk, etag, content, name, contexts)`` of every named stored
    stylesheet, lowest pk first. Inside `rules_memo` they are read once (on
    the first call), whatever the contexts asked for: one query per render,
    not one per context set."""
    memo = _memo.get()
    if memo is not None and _STYLESHEETS in memo:
        return memo[_STYLESHEETS]
    rows = []
    for pk, etag, content in _stored(UI_STYLESHEET_TYPE_URI):
        name = _name_of(content)
        if name is not None:
            rows.append((pk, etag, content, name, rbac_contexts_of(content)))
    if memo is not None:
        memo[_STYLESHEETS] = rows
    return rows


def _stylesheets_by_context(contexts):
    """``{context: {name: (pk, etag, content)}}`` over the default context and
    `contexts`; the lowest pk wins a duplicate name in a context."""
    wanted = {DEFAULT_CONTEXT, *contexts}
    found: dict[str, dict] = {}
    for pk, etag, content, name, sheet_contexts in _stylesheet_rows():
        for context in sheet_contexts:
            if context in wanted:
                found.setdefault(context, {}).setdefault(name, (pk, etag, content))
    return found


def _sheet_rules(content, rank_of):
    """The rules of one stylesheet's `content`, ranked by `rank_of(index)`;
    a rule that does not parse is skipped."""
    prefixes = content.get("prefixes")
    prefixes = prefixes if isinstance(prefixes, Mapping) else {}
    rules = content.get("rules")
    out = []
    for index, rule in enumerate(rules if isinstance(rules, list) else []):
        if not isinstance(rule, Mapping):
            continue
        select, component = rule.get("select"), rule.get("component")
        if not isinstance(select, str) or not isinstance(component, str):
            continue
        try:
            selector = parse_selector(select, dict(prefixes))
        except SelectorError:
            continue
        out.append(Rule(selector, component, rank_of(index)))
    return out


def _order_of(content):
    order = content.get("order", 0)
    return order if isinstance(order, int) and not isinstance(order, bool) else 0


_memo: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "scrud_ui_rules_memo", default=None
)


@contextlib.contextmanager
def rules_memo():
    """Memoise `effective_rules` inside the block (one request), and read the
    stored stylesheets and components once there (`load_components`). Enter
    it around rendering only, after any write: an edit made inside the block
    applies in the next one. Outside it nothing is cached across calls."""
    token = _memo.set({})
    try:
        yield
    finally:
        _memo.reset(token)


@contextlib.contextmanager
def request_memo():
    """`rules_memo`, unless one is active already: the block then shares it."""
    if _memo.get() is not None:
        yield
        return
    with rules_memo():
        yield


def effective_rules(
    contexts: list[str], linked: tuple[str, ...] = ()
) -> tuple[Rule, ...]:
    """Every rule that applies in `contexts`, with its cascade rank (higher
    wins): the default stylesheet, then stylesheets in ``""``, in each context
    in list order, then the `linked` stylesheets in order."""
    contexts = [c for c in dict.fromkeys(contexts) if c != DEFAULT_CONTEXT]
    linked = tuple(linked)
    memo = _memo.get()
    key = (tuple(contexts), linked)
    if memo is not None and key in memo:
        return memo[key]

    by_context = _stylesheets_by_context(contexts)
    rules = _sheet_rules(_default_stylesheet(), lambda i: (0, 0, i))
    for pk, _etag, content in by_context.get(DEFAULT_CONTEXT, {}).values():
        order = _order_of(content)
        rules += _sheet_rules(content, lambda i, o=order, p=pk: (1, o, p, i))
    for k, context in enumerate(contexts):
        for pk, _etag, content in by_context.get(context, {}).values():
            order = _order_of(content)
            rules += _sheet_rules(
                content, lambda i, o=order, p=pk, k=k: (2, k, o, p, i)
            )
    for j, name in enumerate(linked):
        for context in (DEFAULT_CONTEXT, *contexts):
            sheet = by_context.get(context, {}).get(name)
            if sheet is not None:
                rules += _sheet_rules(sheet[2], lambda i, j=j: (3, j, i))
                break
    result = tuple(rules)
    if memo is not None:
        memo[key] = result
    return result


def load_components(names: set[str]) -> dict[str, Component]:
    """The components called `names`, those that exist: built-ins for ``scrud:``
    names, else stored components in the default context. Inside `rules_memo`
    the stored components are read once, on the first call that needs one."""
    found: dict[str, Component] = {}
    stored_names = set()
    for name in names:
        if name.startswith(BUILTIN_PREFIX):
            if name in BUILTIN_NAMES:
                found[name] = builtin_component(name)
        else:
            stored_names.add(name)
    if stored_names:
        memo = _memo.get()
        if memo is None:
            stored = _stored_components(stored_names)
        else:
            if _COMPONENTS not in memo:
                memo[_COMPONENTS] = _stored_components(None)
            stored = memo[_COMPONENTS]
        found.update((n, stored[n]) for n in stored_names if n in stored)
    return found


# The keys of the stored components and stylesheets in a `rules_memo` (no
# rules key is a str).
_COMPONENTS = "components"
_STYLESHEETS = "stylesheets"


def _stored_components(names):
    """``{name: Component}`` of the usable stored components called one of
    `names` (``None``: any name)."""
    found = {}
    seen = set()
    for pk, etag, content in _stored(UI_COMPONENT_TYPE_URI):
        name = _name_of(content)
        if name is None or name in seen or (names is not None and name not in names):
            continue
        if DEFAULT_CONTEXT not in rbac_contexts_of(content):
            continue
        seen.add(name)  # the lowest pk wins, even if it is unusable
        component = _stored_component(name, pk, etag, content)
        if component is not None:
            found[name] = component
    return found


def _stored_component(name, pk, etag, content):
    engine, mode, template = (content.get(k) for k in ("engine", "mode", "template"))
    if engine not in ENGINE_NAMES or mode not in STORED_MODES:
        return None
    if not isinstance(template, str):
        return None
    scopes = content.get("scopes")
    scopes = (
        {k: v for k, v in scopes.items() if isinstance(k, str) and isinstance(v, str)}
        if isinstance(scopes, Mapping)
        else {}
    )
    sheets = content.get("stylesheets")
    sheets = (
        tuple(s for s in sheets if isinstance(s, str))
        if isinstance(sheets, list)
        else ()
    )
    return Component(name, engine, mode, template, scopes, sheets, (pk, etag))


def _fallback(terms: list[TypeTerm], mode: str) -> Component:
    schema_type = next((t.schema_type for t in terms if t.schema_type), None)
    if schema_type == "integer":
        schema_type = "number"
    if schema_type is None:
        return builtin_component("scrud:json")
    plain = f"{BUILTIN_PREFIX}{schema_type}"
    if mode == "edit":
        edit = f"{plain}-edit"
        if edit in BUILTIN_NAMES:
            return builtin_component(edit)
        if plain in BUILTIN_NAMES and builtin_component(plain).mode == "any":
            return builtin_component(plain)
    elif plain in BUILTIN_NAMES:
        return builtin_component(plain)
    return builtin_component("scrud:json")


def resolve_component(
    rules: tuple[Rule, ...],
    scope_chain: tuple[str, ...],
    terms: list[TypeTerm],
    mode: str,
) -> Component:
    """The component for a node with `terms` in `scope_chain` in `mode`: the best
    matching rule (most scopes, then earliest node term, then highest rank)
    whose component exists and serves `mode`, else the built-in for the node's
    schema type."""
    candidates = []
    for rule in rules:
        term_index = selector_matches(rule.selector, scope_chain, terms)
        if term_index is not None:
            candidates.append(
                ((len(rule.selector.scopes), -term_index, rule.rank), rule.component)
            )
    if candidates:
        components = load_components({name for _, name in candidates})
        candidates.sort(key=lambda c: c[0], reverse=True)
        for _, name in candidates:
            component = components.get(name)
            if component is not None and component.serves(mode):
                return component
    return _fallback(terms, mode)
