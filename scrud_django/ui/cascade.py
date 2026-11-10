"""The stylesheet cascade and component resolution.

Presentation is resolved with system rights: stylesheets and components are read
straight from `Resource.objects` (the router's read database), never through
views or permission code. Stored components and stylesheets are those visible
on the selected branch (`effective_rows`; main's with none), and the built-ins
(``scrud:`` names) back the components. Where names are duplicated (a
race in the uniqueness check), the lowest pk wins.

A stored stylesheet, rule or component the cascade cannot use (unnamed, a
duplicate name's loser, a selector that does not parse, an unknown engine or
mode) is skipped, and logged as a warning (`_skipped`), once per `rules_memo`.
"""

import contextlib
import contextvars
import importlib.resources
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import cache, lru_cache
from operator import itemgetter

from django.db.models import Q
from scoped_rbac.rbac_contexts import DEFAULT_CONTEXT

from scrud_django.mixins.resource_mixin import rbac_contexts_of
from scrud_django.models import Resource
from scrud_django.rows import by_pk, unsorted
from scrud_django.ui.branches.model import branch_of
from scrud_django.ui.engines import ENGINE_NAMES, CompiledTemplate, cached_template
from scrud_django.ui.layers import LAYER_BRANCH_NAME, layer_index
from scrud_django.ui.selectors import (
    Selector,
    SelectorError,
    TypeTerm,
    parse_selector,
    selector_matches,
)
from scrud_django.ui.types import UI_COMPONENT_TYPE_URI, UI_STYLESHEET_TYPE_URI

logger = logging.getLogger(__name__)

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
    "uri-edit",
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
class RuleSource:
    """The stylesheet a rule comes from: its `pk` (``None`` for a pinned
    snapshot's, a packaged layer's and the default one), `name`, `branch`
    (``""`` for the default one, which every branch has;
    `layers.LAYER_BRANCH_NAME` for a layer's), the context `applies_to` that
    the cascade applied it in, and its `content` as stored, for read checks
    (``None`` for the default stylesheet, which everyone may read)."""

    pk: int | None
    name: str | None
    branch: str
    applies_to: str
    content: Mapping | None


@dataclass(frozen=True)
class Rule:
    selector: Selector
    component: str
    rank: tuple[int, ...]
    source: RuleSource | None = field(default=None, compare=False)


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


def _stored(type_uri, branches=None):
    """``(pk, etag, content)`` of every resource of the type on one of the
    branches named `branches` (`on_branches`; default: ``main``), lowest pk
    first. Inside `rules_memo` the rows of every UI item kind
    (`ITEM_KINDS`) on those branches are read in one query, once per
    response, so that a page's components, stylesheets and CSS cost one
    query between them (ruling R31), and that query reads no other branch's
    rows (ruling R39)."""
    from scrud_django.ui.branches import ITEM_KINDS, MAIN

    names = frozenset(branches or (MAIN,))
    memo = _memo.get()
    if memo is None or type_uri not in ITEM_KINDS.values():
        return item_rows([type_uri], names)[type_uri]
    key = (_ITEM_ROWS, names)
    if key not in memo:
        memo[key] = item_rows(ITEM_KINDS.values(), names)
    return memo[key][type_uri]


def item_rows(type_uris, branches, using=None) -> dict[str, list]:
    """``{type URI: [(pk, etag, content), ...]}`` of the resources of
    `type_uris` on the branches named `branches` (`on_branches`), lowest pk
    first, read in one query on `using` (default: the router's read
    database)."""
    grouped = {uri: [] for uri in type_uris}
    rows = Resource.objects.using(using) if using else Resource.objects
    rows = unsorted(
        rows.filter(on_branches(branches), resource_type__type_uri__in=list(grouped))
    ).values_list("resource_type__type_uri", "pk", "etag", "content")
    # Sorted here, not by the database (`scrud_django.rows`).
    for uri, pk, etag, content in by_pk(rows, pk=itemgetter(1)):
        grouped[uri].append((pk, etag, content))
    return grouped


def on_branches(branches) -> Q:
    """The rows on one of the branches named `branches` (not empty), as
    `branches.model.branch_of` reads an item: its ``branch``, and for
    ``main`` also an item with no ``branch`` (or a JSON ``null`` one), as
    every item stored before branches is. An item whose ``branch`` is
    neither a string nor ``null`` (only a write around the API stores one)
    is on no branch. Only the ``content__<key>=`` lookup and ``has_key``,
    which every supported database has."""
    from scrud_django.ui.branches import MAIN

    names = sorted(branches)
    if not names:
        raise ValueError("on_branches needs at least one branch name")
    condition = Q(content__branch=names[0])
    for name in names[1:]:
        condition |= Q(content__branch=name)
    if MAIN in names:
        condition |= ~Q(content__has_key="branch") | Q(content__branch=None)
    return condition


_ITEM_ROWS = "item-rows"


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
    from scrud_django.ui.branches.effective import active_selection, effective_rows

    rows = []
    for pk, etag, content in effective_rows(UI_STYLESHEET_TYPE_URI, active_selection()):
        name = _name_of(content)
        if name is None:
            _skipped("stylesheet", pk, None, "it has no name")
            continue
        rows.append((pk, etag, content, name, _applies_to_contexts(content)))
    if memo is not None:
        memo[_STYLESHEETS] = rows
    return rows


def _applies_to_contexts(content):
    """The contexts a stylesheet applies in: its ``applies_to``, else (a row
    stored before branches) its ``rbac_context``s."""
    target = content.get("applies_to")
    return [target] if isinstance(target, str) else rbac_contexts_of(content)


def _stylesheets_by_context(contexts):
    """``{context: {name: (pk, etag, content)}}`` over the default context and
    `contexts`; the lowest pk wins a duplicate name in a context."""
    wanted = {DEFAULT_CONTEXT, *contexts}
    found: dict[str, dict] = {}
    for pk, etag, content, name, sheet_contexts in _stylesheet_rows():
        for context in sheet_contexts:
            if context not in wanted:
                continue
            winner = found.setdefault(context, {}).setdefault(name, (pk, etag, content))
            if winner[0] != pk:
                _skipped(
                    "stylesheet",
                    pk,
                    name,
                    f"stylesheet {winner[0]} has its name in context {context!r}",
                )
    return found


def _sheet_rules(content, rank_of, pk=None, source=None):
    """The rules of one stylesheet's `content` (stored stylesheet `pk`, or
    ``None``: the default one), ranked by `rank_of(index)`, each from
    `source`; a rule that does not parse is skipped (and logged)."""
    prefixes = content.get("prefixes")
    prefixes = prefixes if isinstance(prefixes, Mapping) else {}
    rules = content.get("rules")
    name = _name_of(content)
    out = []
    for index, rule in enumerate(rules if isinstance(rules, list) else []):
        if not isinstance(rule, Mapping):
            _skipped("stylesheet", pk, name, f"rule {index} is not an object")
            continue
        select, component = rule.get("select"), rule.get("component")
        if not isinstance(select, str) or not isinstance(component, str):
            _skipped(
                "stylesheet",
                pk,
                name,
                f"rule {index} has no string select and component",
            )
            continue
        try:
            selector = parse_selector(select, dict(prefixes))
        except SelectorError as error:
            _skipped("stylesheet", pk, name, f"rule {index} ({select!r}): {error}")
            continue
        out.append(Rule(selector, component, rank_of(index), source))
    return out


def _skipped(kind, pk, name, reason):
    """Log (a warning) that the cascade skips stored `kind` `pk` (called
    `name`), or a part of it, for `reason`: once inside a `rules_memo` (one
    request), every time outside one."""
    memo = _memo.get()
    if memo is not None:
        warned = memo.setdefault(_WARNED, set())
        key = (kind, pk, reason)
        if key in warned:
            return
        warned.add(key)
    logger.warning("scrud-ui: skipped %s %s (%r): %s", kind, pk, name, reason)


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
    wins): the default stylesheet, then the packaged layers' stylesheets
    (`_layer_rank`), then stylesheets in ``""``, in each context in list
    order, then the `linked` stylesheets in order. A branch's override of a
    layer stylesheet is a stored row, and ranks as one."""
    contexts = [c for c in dict.fromkeys(contexts) if c != DEFAULT_CONTEXT]
    linked = tuple(linked)
    memo = _memo.get()
    key = (tuple(contexts), linked)
    if memo is not None and key in memo:
        return memo[key]

    by_context = _stylesheets_by_context(contexts)
    default = _default_stylesheet()
    rules = _sheet_rules(
        default,
        lambda i: (0, 0, i),
        source=RuleSource(None, _name_of(default), "", DEFAULT_CONTEXT, None),
    )
    for pk, _etag, content in by_context.get(DEFAULT_CONTEXT, {}).values():
        order = _order_of(content)
        layer = _layer_rank(pk, content)
        rules += _sheet_rules(
            content,
            (
                (lambda i, o=order, p=pk or 0: (1, o, p, i))
                if layer is None
                else (lambda i, o=order, n=layer: (0, 1, n, o, i))
            ),
            pk,
            _source(pk, content, DEFAULT_CONTEXT),
        )
    for k, context in enumerate(contexts):
        for pk, _etag, content in by_context.get(context, {}).values():
            order = _order_of(content)
            rules += _sheet_rules(
                content,
                lambda i, o=order, p=pk or 0, k=k: (2, k, o, p, i),
                pk,
                _source(pk, content, context),
            )
    for j, name in enumerate(linked):
        for context in (DEFAULT_CONTEXT, *contexts):
            sheet = by_context.get(context, {}).get(name)
            if sheet is not None:
                rules += _sheet_rules(
                    sheet[2],
                    lambda i, j=j: (3, j, i),
                    sheet[0],
                    _source(sheet[0], sheet[2], context),
                )
                break
    result = tuple(rules)
    if memo is not None:
        memo[key] = result
    return result


def _layer_rank(pk, content) -> int | None:
    """The position of the packaged layer stylesheet `content` is from
    (`layers.layer_index`), whose rules rank ``(0, 1, layer, order, rule
    index)``: after the default stylesheet's and before every stored
    stylesheet's. ``None`` for a stored stylesheet."""
    if pk is not None or branch_of(content) != LAYER_BRANCH_NAME:
        return None
    index = layer_index(_name_of(content) or "")
    return 0 if index is None else index


def _source(pk, content, context) -> RuleSource:
    """The `RuleSource` of stored stylesheet `pk` (`content`), applied in
    `context`."""
    return RuleSource(pk, _name_of(content), branch_of(content), context, content)


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


# The keys of the stored components and stylesheets, and of what `_skipped`
# logged, in a `rules_memo` (no rules key is a str).
_COMPONENTS = "components"
_STYLESHEETS = "stylesheets"
_WARNED = "warned"


def _stored_components(names):
    """``{name: Component}`` of the usable stored components called one of
    `names` (``None``: any name)."""
    from scrud_django.ui.branches.effective import active_selection, effective_rows

    found = {}
    seen = {}
    for pk, etag, content in effective_rows(UI_COMPONENT_TYPE_URI, active_selection()):
        name = _name_of(content)
        if name is None:
            _skipped("component", pk, None, "it has no name")
            continue
        if names is not None and name not in names:
            continue
        if name in seen:
            _skipped("component", pk, name, f"component {seen[name]} has its name")
            continue
        seen[name] = pk  # the lowest pk wins, even if it is unusable
        component = _stored_component(name, pk, etag, content)
        if component is not None:
            found[name] = component
    return found


def _stored_component(name, pk, etag, content):
    engine, mode, template = (content.get(k) for k in ("engine", "mode", "template"))
    if engine not in ENGINE_NAMES or mode not in STORED_MODES:
        _skipped("component", pk, name, f"unknown engine {engine!r} or mode {mode!r}")
        return None
    if not isinstance(template, str):
        _skipped("component", pk, name, "its template is not a string")
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


@dataclass(frozen=True)
class Ranking:
    """The rules that match a node, as `resolve_component` tries them:
    `ranked` holds ``(key, rule)`` best first, the key being ``(number of
    selector scopes, -index of the node term matched, cascade rank)``;
    `components` the components they name that exist; `winner` the index in
    `ranked` of the first whose component exists and serves the mode, or
    ``None`` (the node gets the built-in fallback)."""

    ranked: list[tuple[tuple, Rule]]
    components: dict[str, Component]
    winner: int | None


def rank_rules(
    rules: tuple[Rule, ...],
    scope_chain: tuple[str, ...],
    terms: list[TypeTerm],
    mode: str,
) -> Ranking:
    """The `Ranking` of `rules` for a node with `terms` in `scope_chain` in
    `mode`: the one ordering `resolve_component` and the studio's
    explanation share."""
    ranked = []
    for rule in rules:
        term_index = selector_matches(rule.selector, scope_chain, terms)
        if term_index is not None:
            ranked.append(((len(rule.selector.scopes), -term_index, rule.rank), rule))
    if not ranked:
        return Ranking([], {}, None)
    components = load_components({rule.component for _, rule in ranked})
    ranked.sort(key=itemgetter(0), reverse=True)
    for index, (_, rule) in enumerate(ranked):
        component = components.get(rule.component)
        if component is not None and component.serves(mode):
            return Ranking(ranked, components, index)
    return Ranking(ranked, components, None)


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
    ranking = rank_rules(rules, scope_chain, terms, mode)
    if ranking.winner is None:
        return _fallback(terms, mode)
    return ranking.components[ranking.ranked[ranking.winner][1].component]
