"""Helpers for reading JSON-LD contexts: IRI expansion and term definitions."""


def expand(term: str, context: dict) -> str:
    """Expand ``prefix:local`` using the string-valued prefix entries of
    `context`. Absolute IRIs and unknown prefixes are returned unchanged."""
    prefix, separator, local = term.partition(":")
    if separator and not local.startswith("//"):
        base = context.get(prefix)
        if isinstance(base, str):
            return base + local
    return term


def term_definition(context: dict, key: str) -> tuple[str | None, str | None, dict]:
    """Return ``(property_iri, class_iri, nested_context)`` for `key`.

    `property_iri` is the definition's ``@id`` and `class_iri` its ``@type``
    (the scrud convention for the class of the value), both expanded. The
    nested context is the definition's ``@context`` merged over `context`; for
    an unknown key (or one without a definition object) it is `context`."""
    definition = context.get(key)
    if isinstance(definition, str):
        definition = {"@id": definition}
    if not isinstance(definition, dict):
        return None, None, context
    property_iri = definition.get("@id")
    class_iri = definition.get("@type")
    nested = definition.get("@context")
    return (
        expand(property_iri, context) if isinstance(property_iri, str) else None,
        expand(class_iri, context) if isinstance(class_iri, str) else None,
        {**context, **nested} if isinstance(nested, dict) else context,
    )
