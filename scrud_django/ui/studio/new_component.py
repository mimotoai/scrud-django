"""New component for a node (spec Goal 5): the studio's central authoring
action. For a node of what a preview shows, it writes, on a working branch
and in one transaction, a Jinja component in the preview's mode and the rule
that selects it, in the branch's stylesheet named `STUDIO_STYLESHEET` for
the chosen ``applies_to`` (created on first use); then it says whether the
rule wins for the node, and why (`explain`).

The rule matches one of the node's terms (most specific first), optionally
narrowed to a scope of the node's chain (*where*) and, for a node with no
IRI term, to the scope of its top-level property in the resource's root
component (*inside this type*, ruling S2): the root's component gets the
scope ``<slug>-<property>`` (suffixed when the root gives it to another
property; a scope it already gives the property is kept: S11, S14), as an
override when it is inherited; or, when it is a built-in, a Jinja copy of
that built-in with the scope is created, selected by the root's most
specific class term (S12) in the same stylesheet. It is not offered when the
user may not read the root's component (S13).

Every write is a request to a resource view (`call_view`) with the user's
own rights, so the studio adds no rights; any refusal (status 400 or more)
rolls the whole action back and is the action's answer, passed through. An
inherited item it extends is read through its view too, so that no one
copies an item they may not read."""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace

from django.db import transaction
from django.http import HttpResponse
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from scrud_django.permissions import may_read_in_context, resource_contexts
from scrud_django.registration import write_db
from scrud_django.ui import ld
from scrud_django.ui.branches import ITEM_KINDS, MAIN
from scrud_django.ui.branches.effective import (
    Selection,
    effective_rows,
    using_selection,
)
from scrud_django.ui.branches.model import (
    MAIN_BRANCH,
    Branch,
    authorizing_context,
    branch_of,
    item_key,
)
from scrud_django.ui.branches.store import branches
from scrud_django.ui.branches.versions import batched_versions
from scrud_django.ui.cascade import (
    BUILTIN_PREFIX,
    rank_rules,
    resolve_component,
    rules_memo,
)
from scrud_django.ui.render import NodeInfo, node_at
from scrud_django.ui.selectors import SCOPE_NAME, TypeTerm
from scrud_django.ui.studio.dispatch import call_view
from scrud_django.ui.studio.explain import explain, term_text
from scrud_django.ui.studio.writes import KIND_SLUGS, SAVED_EVENT, _passed, own_item
from scrud_django.ui.types import UI_COMPONENT_TYPE_URI

# The name of the stylesheet the studio writes its rules to, on each branch
# and for each ``applies_to``.
STUDIO_STYLESHEET = "studio"

_NOT_IN_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_NOT_IN_SCOPE = re.compile(r"[^A-Za-z0-9_-]+")
# A generated name's length before its ``-<n>`` suffix (names have 128 at
# most, as the item schemas say).
_NAME_BASE_LENGTH = 120
# The last character of a JSON-LD context's IRI that may be a prefix.
_PREFIX_ENDS = tuple(":/?#[]@")


@dataclass(frozen=True)
class _Inside:
    """ "Inside this type" for a node: the scope of its top-level `property`
    in the root's component, the one now chosen, called `root_component`.
    `current` is the scope that component already gives the property, if
    any (then it is `scope`, and the root needs no write). When the root's
    component is a built-in, a Jinja copy of it (`root_template`) called
    `new_root` is created, selected by `root_term`, the root's most
    specific class term (ruling S12)."""

    scope: str
    property: str
    root_component: str
    current: str | None
    new_root: str | None = None
    root_template: str | None = None
    root_term: TypeTerm | None = None

    @property
    def creates_root(self) -> bool:
        return self.new_root is not None

    @property
    def carrier(self) -> str:
        """The name of the component that gives the scope."""
        return self.new_root or self.root_component

    def as_json(self) -> dict:
        return {
            "scope": self.scope,
            "root_component": self.root_component,
            "property": self.property,
            "creates_root": self.creates_root,
        }


@dataclass(frozen=True)
class _Plan:
    """What the action may do for a node (`info`): its `matches`, `where`
    and `applies_to` options, the `name` of its new component, and
    `inside` (``None`` when "inside this type" is not offered)."""

    info: NodeInfo
    matches: list[dict]
    where: list[str]
    applies_to: list[str]
    name: str
    inside: _Inside | None


def options(request, slug, resource_type, shown, pointer: str) -> dict:
    """The options of the action for the node at `pointer` of `shown` (a
    preview's `_Shown`) of type `slug`:

    - ``matches``: ``{select, label, broad}`` for each of the node's terms,
      most specific first (the first is the default); a schema-type term is
      ``broad``, labelled ``every <type>[ format=…| enum] on this branch``,
      an IRI term is labelled shortened with the type's context prefixes;
    - ``inside_type``: ``{scope, root_component, property, creates_root}``
      (rulings S2, S13: ``root_component`` is the root's component now, a
      built-in when ``creates_root``), or ``None``: for a node with an IRI
      term, the root, or a root component the user may not read;
    - ``where``: the scopes of the node's chain (one may narrow the rule);
    - ``applies_to``: ``""``, and the preview's data context if it has one;
    - ``name``: the new component's, unique on the branch."""
    plan = _plan(request, slug, resource_type, shown, pointer)
    return {
        "matches": plan.matches,
        "inside_type": plan.inside.as_json() if plan.inside else None,
        "where": plan.where,
        "applies_to": plan.applies_to,
        "name": plan.name,
    }


def create(
    request,
    slug,
    resource_type,
    branch: Branch,
    shown,
    body: Mapping,
    readable: Callable,
) -> HttpResponse:
    """Run the action on working `branch` (which `shown` selects) for the
    body ``{pointer, match?, inside_type?, where?, applies_to?, template}``
    (``match``, one of the options' ``select``, defaults to the first):

    1. with ``inside_type``, give the root's component the scope (writing a
       new root component, or the branch's own or overriding one);
    2. create the Jinja component, named as `options` says;
    3. add the rule(s) to the branch's `STUDIO_STYLESHEET` for
       ``applies_to`` (creating it, with the type's context prefixes).

    Answers 201 with ``{written: [{kind, name[, applies_to]}], wins,
    explanation}``: whether the new rule now wins for the node, and the
    inspector's explanation (`explain`, with `readable`). Any write or read
    refused rolls everything back, and its answer is passed through."""
    pointer = body.get("pointer", "")
    template = body.get("template")
    if not isinstance(template, str):
        raise ValidationError({"template": ["A template is required."]})
    plan = _plan(request, slug, resource_type, shown, pointer)
    term = _chosen_term(plan, body.get("match"))
    inside = body.get("inside_type", False)
    if not isinstance(inside, bool):
        raise ValidationError({"inside_type": ["Must be true or false."]})
    if inside and plan.inside is None:
        raise ValidationError(
            {"inside_type": ["Only for a node with no IRI term, below the root."]}
        )
    where = body.get("where") or None
    if where is not None and where not in plan.where:
        raise ValidationError({"where": ["Must be a scope of the node's chain."]})
    target = body.get("applies_to", "")
    if target not in plan.applies_to:
        raise ValidationError(
            {"applies_to": ["Must be \"\" or the preview's data context."]}
        )
    mode = shown.mode
    inside = plan.inside if inside else None
    rules = []  # (scopes, term, component) to add, in order
    try:
        with transaction.atomic(using=write_db()), batched_versions():
            written = []
            if inside is not None and inside.current is None:
                if inside.creates_root:
                    _create_component(
                        request,
                        branch,
                        inside.new_root,
                        mode,
                        inside.root_template,
                        scopes={inside.property: inside.scope},
                    )
                    rules.append(((), inside.root_term, inside.new_root))
                else:
                    _extend(
                        request,
                        "component",
                        branch,
                        inside.root_component,
                        "",
                        _with_scope(inside.property, inside.scope),
                    )
                written.append({"kind": "component", "name": inside.carrier})
            _create_component(request, branch, plan.name, mode, template)
            written.append({"kind": "component", "name": plan.name})
            scopes = _selector_scopes(plan, shown, where, inside)
            rules.append((scopes, term, plan.name))
            _extend(
                request,
                "stylesheet",
                branch,
                STUDIO_STYLESHEET,
                target,
                _with_rules(rules),
                fresh={"prefixes": context_prefixes(resource_type), "rules": []},
            )
            written.append(
                {"kind": "stylesheet", "name": STUDIO_STYLESHEET, "applies_to": target}
            )
    except _Refused as refused:
        return _passed(refused.response)
    with using_selection(shown.selection), rules_memo():
        info = _node(request, resource_type, shown, pointer)
        wins = False
        if not info.as_json:
            ranking = rank_rules(info.rules, info.scope_chain, info.terms, mode)
            wins = (
                ranking.winner is not None
                and ranking.ranked[ranking.winner][1].component == plan.name
            )
        explanation = explain(info, mode, readable=readable)
    return Response(
        {"written": written, "wins": wins, "explanation": explanation},
        status=201,
        headers={"HX-Trigger": SAVED_EVENT},
    )


class _Refused(Exception):
    """A resource view's refusal (`response`), which rolls the action back."""

    def __init__(self, response):
        super().__init__(response.status_code)
        self.response = response


def _ok(response):
    """`response`, unless it is a refusal (`_Refused`)."""
    if response.status_code >= 400:
        raise _Refused(response)
    return response


def _plan(request, slug, resource_type, shown, pointer) -> _Plan:
    if not isinstance(pointer, str):
        raise ValidationError({"pointer": ["A JSON Pointer is required."]})
    with using_selection(shown.selection), rules_memo():
        info = _node(request, resource_type, shown, pointer)
        root = root_term = None
        if (
            info.pointer
            and not info.as_json
            and not any(term.iri for term in info.terms)
            and isinstance(shown.content, Mapping)
        ):
            root_info = _node(request, resource_type, shown, "")
            root = resolve_component(
                root_info.rules, root_info.scope_chain, root_info.terms, shown.mode
            )
            root_term = next((t for t in root_info.terms if t.iri), None)
            builtin = root.name.startswith(BUILTIN_PREFIX)
            if (builtin and root_term is None) or (
                not builtin and not _may_read_component(request, root.name)
            ):
                root = None  # not offered (ruling S13)
        taken = _taken_names(shown.selection.branch)
    prefixes = context_prefixes(resource_type)
    name = _unique_name(
        [slug, *_property_path(shown.content, info.pointer), shown.mode], taken
    )
    inside = None
    if root is not None:
        top = _unescape(info.pointer[1:].split("/")[0])
        current = root.scopes.get(top)
        others = {scope for prop, scope in root.scopes.items() if prop != top}
        inside = _Inside(
            scope=current or _scope_name(slug, top, others),
            property=top,
            root_component=root.name,
            current=current,
        )
        if root.name.startswith(BUILTIN_PREFIX):
            inside = replace(
                inside,
                new_root=_unique_name([slug, shown.mode], taken | {name}),
                root_template=_jinja_copy(root.template),
                root_term=root_term,
            )
    return _Plan(
        info=info,
        matches=[] if info.as_json else [_match(t, prefixes) for t in info.terms],
        where=[s for s in dict.fromkeys(info.scope_chain) if SCOPE_NAME.match(s)],
        applies_to=["", shown.data_context] if shown.data_context else [""],
        name=name,
        inside=inside,
    )


def _node(request, resource_type, shown, pointer) -> NodeInfo:
    return node_at(
        shown.content,
        resource_type=resource_type,
        request=request,
        pointer=pointer,
        scope_chain=shown.scope_chain,
        mode=shown.mode,
        contexts=shown.contexts,
    )


def _chosen_term(plan: _Plan, match) -> TypeTerm:
    """The node term the body's ``match`` names (default: the first)."""
    selects = [m["select"] for m in plan.matches]
    if match is None and selects:
        match = selects[0]
    if not isinstance(match, str) or match not in selects:
        raise ValidationError({"match": ["Must be one of the node's matches."]})
    return plan.info.terms[selects.index(match)]


def _selector_scopes(plan: _Plan, shown, where, inside: _Inside | None) -> tuple:
    """The scopes of the new rule's selector: `where` and `inside`'s scope,
    in the order of the node's chain once the root gives the scope."""
    chain = list(plan.info.scope_chain)
    wanted = {where} if where else set()
    if inside is not None:
        if inside.current is None:
            chain.insert(len(shown.scope_chain), inside.scope)
        wanted.add(inside.scope)
    return tuple(sorted(wanted, key=chain.index))


def _match(term: TypeTerm, prefixes) -> dict:
    text = term_text(term)
    if term.iri is not None:
        return {"select": text, "label": compact(term.iri, prefixes), "broad": False}
    detail = ""
    if term.format is not None:
        detail = f" format={term.format}"
    elif term.enum:
        detail = " enum"
    return {
        "select": text,
        "label": f"every {term.schema_type}{detail} on this branch",
        "broad": True,
    }


def context_prefixes(resource_type) -> dict[str, str]:
    """The prefixes of `resource_type`'s JSON-LD context: its string-valued
    terms (not keywords) whose IRI ends with a character a prefix's may
    (``:/?#[]@``)."""
    document = getattr(getattr(resource_type, "context", None), "content", None)
    document = document if isinstance(document, Mapping) else {}
    terms = document.get("@context")
    terms = terms if isinstance(terms, Mapping) else document
    return {
        key: value
        for key, value in terms.items()
        if isinstance(key, str)
        and not key.startswith("@")
        and isinstance(value, str)
        and value.endswith(_PREFIX_ENDS)
    }


def compact(iri: str, prefixes: Mapping) -> str:
    """`iri` as ``prefix:local`` with the prefix of `prefixes` whose IRI is
    the longest start of it (one that a selector expands back to `iri`),
    else `iri` as it is."""
    best = None
    for prefix, base in prefixes.items():
        if not isinstance(base, str) or not base or not iri.startswith(base):
            continue
        token = f"{prefix}:{iri[len(base) :]}"
        if (
            len(token.split()) == 1
            and token == token.strip()
            and ld.expand(token, dict(prefixes)) == iri
            and (best is None or len(base) > best[0])
        ):
            best = (len(base), token)
    return best[1] if best else iri


def _taken_names(branch_name: str) -> set:
    """The names of the components on branch `branch_name` (its own rows,
    deletion markers included, and those it inherits)."""
    return {
        content.get("name")
        for _, _, content in effective_rows(
            UI_COMPONENT_TYPE_URI,
            Selection(branch=branch_name),
            include_deleted=True,
            authorized_only=False,
        )
    }


def _unique_name(parts, taken) -> str:
    """``-``-joined `parts` as a component name (characters a name may not
    have become ``-``), with ``-2``, ``-3``, … when it is `taken`."""
    base = _NOT_IN_NAME.sub("-", "-".join(parts))[:_NAME_BASE_LENGTH]
    base = base.lstrip("._-") or "component"
    name, number = base, 1
    while name in taken:
        number += 1
        name = f"{base}-{number}"
    return name


def _scope_name(slug, prop, taken) -> str:
    """The scope "inside this type" gives property `prop` of type `slug`:
    ``<slug>-<prop>`` (characters a scope may not have become ``-``), with
    ``-2``, ``-3``, … when the root gives another property that scope
    (`taken`; ruling S14)."""
    base = _NOT_IN_SCOPE.sub("-", f"{slug}-{prop}")
    base = base if SCOPE_NAME.match(base) else f"s-{base}"
    scope, number = base, 1
    while scope in taken:
        number += 1
        scope = f"{base}-{number}"
    return scope


def _jinja_copy(template: str) -> str:
    """Built-in `template` as a Jinja template rendering the same bytes:
    Jinja drops a template's last newline (``keep_trailing_newline`` is
    off), which the built-in (a Django template) keeps, so the copy has one
    more."""
    return template + "\n" if template.endswith("\n") else template


def _may_read_component(request, name) -> bool:
    """Whether the user of `request` may read the component `name` the
    selected branch shows, as the list filter checks it."""
    for _, _, content in effective_rows(UI_COMPONENT_TYPE_URI):
        if content.get("name") == name:
            return may_read_in_context(
                request, UI_COMPONENT_TYPE_URI, content, resource_contexts(content)
            )
    return False


def _property_path(content, pointer) -> list[str]:
    """The property names `pointer` (a node of `content`) goes through,
    without array indices."""
    parts, value = [], content
    for token in pointer[1:].split("/") if pointer else ():
        key = _unescape(token)
        if isinstance(value, list):
            value = value[int(key)]
        else:
            parts.append(key)
            value = value.get(key) if isinstance(value, Mapping) else None
    return parts


def _unescape(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def _create_component(request, branch: Branch, name, mode, template, scopes=None):
    """Create the Jinja component `name` on `branch` (POST)."""
    content = {"engine": "jinja2", "mode": mode, "template": template}
    if scopes:
        content["scopes"] = scopes
    _ok(
        call_view(
            request,
            KIND_SLUGS["component"],
            "POST",
            data=_item("component", branch, name, "", content),
        )
    )


def _with_scope(prop, scope):
    """The change of a component's content that scopes `prop`."""

    def change(content):
        scopes = content.get("scopes")
        scopes = dict(scopes) if isinstance(scopes, Mapping) else {}
        return {**content, "scopes": {**scopes, prop: scope}}

    return change


def _with_rules(rules):
    """The change of a stylesheet's content that appends `rules` (``(scopes,
    term, component)``), each IRI written with the stylesheet's prefixes."""

    def change(content):
        prefixes = content.get("prefixes")
        prefixes = prefixes if isinstance(prefixes, Mapping) else {}
        existing = content.get("rules")
        added = [
            {
                "select": " ".join((*scopes, _term_token(term, prefixes))),
                "component": component,
            }
            for scopes, term, component in rules
        ]
        return {
            **content,
            "rules": [*(existing if isinstance(existing, list) else []), *added],
        }

    return change


def _term_token(term: TypeTerm, prefixes) -> str:
    """`term` as a selector's last token with `prefixes` (an IRI shortened
    by `compact`). An IRI that no token expands back to with them (one
    whose scheme is one of the prefixes) is refused (ruling S14)."""
    if term.iri is None:
        return term_text(term)
    token = compact(term.iri, prefixes)
    if ld.expand(token, dict(prefixes)) != term.iri:
        raise ValidationError(
            {
                "match": [
                    f"{term.iri!r} can't be written in a selector of the "
                    "stylesheet: one of its prefixes is the IRI's scheme."
                ]
            }
        )
    return token


def _item(kind, branch: Branch, name, target, content) -> dict:
    """`content` as the item of `kind` called `name` (a stylesheet: for
    `target`) on `branch`, with the context its write is authorized in. The
    server sets ``base``."""
    item = {
        **{k: v for k, v in content.items() if k not in ("base", "deleted")},
        "name": name,
        "branch": branch.name,
    }
    if kind == "stylesheet":
        item["applies_to"] = target
    item["rbac_context"] = authorizing_context(kind, item, branch)
    return item


def _extend(request, kind, branch: Branch, name, target, change, fresh=None):
    """Write `change(content)` as the item of `kind` called `name` (for
    `target`) on `branch`, `content` being the item as the branch shows it:
    its own row (updated, with its ETag; a deletion marker counts as
    `fresh`), else the inherited item (overridden), else `fresh` (created).
    Each item is read through its view (or, from a pinned snapshot, checked
    as the list filter checks it), so that nothing the user may not read is
    copied."""
    slug = KIND_SLUGS[kind]
    own = own_item(kind, branch, name, target)
    if own is not None:
        read = _ok(call_view(request, slug, "GET", pk=own.pk))
        content = read.data
        if content.get("deleted") is True:
            content = {**content, **(fresh or {})}
        return _ok(
            call_view(
                request,
                slug,
                "PUT",
                pk=own.pk,
                data=_item(kind, branch, name, target, change(content)),
                if_match=read["ETag"],
            )
        )
    inherited = _inherited(
        kind, branch, (name, target) if kind == "stylesheet" else (name,)
    )
    if inherited is None:
        content = fresh or {}
    elif inherited[0] is not None:
        content = _ok(call_view(request, slug, "GET", pk=inherited[0])).data
    else:
        content = inherited[2]
        if not may_read_in_context(
            request, ITEM_KINDS[kind], content, resource_contexts(content)
        ):
            raise PermissionDenied()
    return _ok(
        call_view(
            request,
            slug,
            "POST",
            data=_item(kind, branch, name, target, change(dict(content))),
        )
    )


def _inherited(kind, branch: Branch, key):
    """``(pk, etag, content)`` of the item with `key` that `branch` shows
    from an ancestor (pk ``None``: a pinned snapshot's), or ``None``."""
    all_branches = branches(write_db())
    for row in effective_rows(ITEM_KINDS[kind], Selection(branch=branch.name)):
        on = branch_of(row[2])
        if on == branch.name:
            continue
        row_branch = all_branches.get(on) or (MAIN_BRANCH if on == MAIN else None)
        if row_branch is not None and item_key(kind, row[2], row_branch) == key:
            return row
    return None
