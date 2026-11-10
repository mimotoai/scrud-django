"""The inspector's explanation of a node: which rule chose its component,
which rules matched but lost, and why. It ranks the rules with the cascade's
own `rank_rules`, the ranking `resolve_component` decides by, so the two
always agree."""

from collections.abc import Callable

from scrud_django.ui.cascade import RuleSource, _fallback, rank_rules
from scrud_django.ui.render import NodeInfo
from scrud_django.ui.selectors import Selector, TypeTerm


def explain(
    info: NodeInfo, mode: str, *, readable: Callable[[RuleSource | None], bool]
) -> dict:
    """The explanation of the node `info` describes, in `mode`, as JSON:

    - ``pointer``, ``terms`` (as selector terms, most specific first) and
      ``scope`` (its scope chain);
    - ``rules``: every rule that matched, best first as
      `resolve_component` tries them, each with its ``rank`` parts
      (``[scopes, -term index, *cascade rank]``) and its ``outcome``:
      ``"wins"``; ``"outranked"`` (below the winner); ``"missing
      component"`` or ``"wrong mode"`` (above the winner, or with none);
    - ``winner``: the index in ``rules`` of the rule whose component the
      node gets, or ``None`` with ``fallback`` the built-in it gets.

    A rule whose source `readable` refuses shows only ``readable``,
    ``rank`` and ``outcome``; another also shows its ``selector``,
    ``component``, and its stylesheet's name, ``branch`` and
    ``applies_to``. A node that renders as JSON whatever the rules
    (`NodeInfo.as_json`) has no rules and falls back to ``scrud:json``."""
    if info.as_json:
        ranking, fallback = None, "scrud:json"
    else:
        ranking = rank_rules(info.rules, info.scope_chain, info.terms, mode)
        fallback = None
        if ranking.winner is None:
            fallback = _fallback(info.terms, mode).name
    rules = []
    allowed = {}  # id(source): whether it is readable, asked once per source
    for index, (key, rule) in enumerate(ranking.ranked if ranking else ()):
        scopes, term_index, cascade_rank = key
        explained = {
            "readable": False,
            "rank": [scopes, term_index, *cascade_rank],
            "outcome": _outcome(index, rule, ranking),
        }
        source = rule.source
        if id(source) not in allowed:
            allowed[id(source)] = readable(source)
        if allowed[id(source)]:
            explained.update(
                readable=True,
                selector=selector_text(rule.selector),
                component=rule.component,
                stylesheet=source.name if source else None,
                branch=source.branch if source else None,
                applies_to=source.applies_to if source else None,
            )
        rules.append(explained)
    return {
        "pointer": info.pointer,
        "terms": [term_text(term) for term in info.terms],
        "scope": list(info.scope_chain),
        "winner": ranking.winner if ranking else None,
        "fallback": fallback,
        "rules": rules,
    }


def _outcome(index, rule, ranking) -> str:
    if index == ranking.winner:
        return "wins"
    if ranking.winner is not None and index > ranking.winner:
        return "outranked"
    if rule.component not in ranking.components:
        return "missing component"
    return "wrong mode"


def term_text(term: TypeTerm) -> str:
    """`term` as a selector writes it: an IRI, or a schema type with its
    ``[format=…]`` or ``[enum]``."""
    if term.iri is not None:
        return term.iri
    if term.format is not None:
        return f"{term.schema_type}[format={term.format}]"
    if term.enum:
        return f"{term.schema_type}[enum]"
    return term.schema_type


def selector_text(selector: Selector) -> str:
    """`selector` as text, its IRI expanded."""
    return " ".join((*selector.scopes, term_text(selector.term)))
