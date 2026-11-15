"""Stylesheet selectors: ``[scope ...] type-term``.

Grammar: whitespace-separated tokens. Every token but the last is a scope name
(``[A-Za-z][A-Za-z0-9_-]*``). The last is the type term: an IRI (a token
containing ``:``, expanded with the stylesheet's prefixes) or one of the schema
types, optionally followed by ``[format=<name>]`` or ``[enum]``.
"""

import re
from dataclasses import dataclass

from scrud_django.ui.ld import expand

SCHEMA_TYPES = ("object", "array", "string", "number", "integer", "boolean", "null")
SCOPE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]*\Z")
SCHEMA_TERM = re.compile(r"([a-z]+)(?:\[(?:format=([^\[\]=\s]+)|(enum))\])?\Z")


class SelectorError(ValueError):
    """A selector that does not parse."""


@dataclass(frozen=True)
class TypeTerm:
    """One type a node has, or one a selector asks for. Exactly one of `iri`
    and `schema_type` is set.

    Node terms (most specific first) are the class IRI, the property IRI, then
    schema terms: a format term (``schema_type="string", format="date"``), an
    enum term (``TypeTerm(schema_type="string", enum=True)``) and the bare type
    (``TypeTerm(schema_type="string")``). An ``integer`` node also carries a
    ``number`` term.

    A selector term matches a node term when: both are the same IRI; or the
    selector has a ``format`` and the node term has the same ``schema_type`` and
    ``format``; or the selector is ``[enum]`` and the node term has the same
    ``schema_type`` with ``enum=True`` (so ``string[enum]`` matches only enum
    node terms); or the selector is bare and the node term has the same
    ``schema_type``, regardless of its format or enum."""

    iri: str | None = None
    schema_type: str | None = None
    format: str | None = None
    enum: bool = False


@dataclass(frozen=True)
class Selector:
    scopes: tuple[str, ...]
    term: TypeTerm


def parse_selector(text: str, prefixes: dict[str, str]) -> Selector:
    """Parse `text`, expanding IRIs with `prefixes`; raise ``SelectorError``."""
    tokens = text.split()
    if not tokens:
        raise SelectorError("The selector is empty.")
    *scopes, last = tokens
    for scope in scopes:
        if not SCOPE_NAME.match(scope):
            raise SelectorError(f"{scope!r} is not a valid scope name.")
    if ":" in last:
        return Selector(tuple(scopes), TypeTerm(iri=expand(last, prefixes)))
    found = SCHEMA_TERM.match(last)
    if not found or found.group(1) not in SCHEMA_TYPES:
        raise SelectorError(
            f"{last!r} is not an IRI or one of {', '.join(SCHEMA_TYPES)}"
            " (optionally with [format=name] or [enum])."
        )
    return Selector(
        tuple(scopes),
        TypeTerm(
            schema_type=found.group(1),
            format=found.group(2),
            enum=found.group(3) is not None,
        ),
    )


def _term_matches(wanted: TypeTerm, node: TypeTerm) -> bool:
    if wanted.iri is not None:
        return node.iri == wanted.iri
    if node.schema_type != wanted.schema_type:
        return False
    if wanted.format is not None:
        return node.format == wanted.format
    if wanted.enum:
        return node.enum
    return True


def _is_subsequence(scopes: tuple[str, ...], chain: tuple[str, ...]) -> bool:
    remaining = iter(chain)
    return all(scope in remaining for scope in scopes)


def selector_matches(
    sel: Selector, scope_chain: tuple[str, ...], terms: list[TypeTerm]
) -> int | None:
    """The index in `terms` of the first term `sel` matches, or ``None``. The
    selector's scopes must be a subsequence of `scope_chain` (descendant
    semantics)."""
    if not _is_subsequence(sel.scopes, scope_chain):
        return None
    for index, node in enumerate(terms):
        if _term_matches(sel.term, node):
            return index
    return None
