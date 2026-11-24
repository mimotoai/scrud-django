"""``SCRUD_OUTBOX``: ``TYPES`` (the type URIs whose writes are recorded, a
list or tuple of strings, or ``"__all__"``; unset or empty records nothing,
``scrud_django.W010``; anything else is ``scrud_django.E013`` and records
nothing) and ``DELIVER`` (the dotted path of ``deliver(event)``, which
``manage.py scrud_outbox_send`` calls)."""

from collections.abc import Mapping

from django.conf import settings

ALL = "__all__"


def outbox_settings():
    """``SCRUD_OUTBOX``, ``{}`` when unset."""
    return getattr(settings, "SCRUD_OUTBOX", None) or {}


def configured_types():
    """``SCRUD_OUTBOX["TYPES"]`` as set (``None`` when unset, or when
    ``SCRUD_OUTBOX`` is not a mapping)."""
    found = outbox_settings()
    if not isinstance(found, Mapping):
        return None
    return found.get("TYPES")


def types_are_well_formed(types):
    """Whether `types` is ``"__all__"`` or a list or tuple of strings."""
    if types == ALL:
        return True
    return isinstance(types, (list, tuple)) and all(isinstance(t, str) for t in types)


def recorded_type(type_uri) -> bool:
    """Whether the writes of `type_uri` are recorded: ``TYPES`` is
    ``"__all__"``, or a list or tuple naming it. Unset, empty or malformed
    ``TYPES`` records nothing (a string other than ``"__all__"`` is not a
    list of one). Reads settings only: no query."""
    types = configured_types()
    if types == ALL:
        return True
    if not isinstance(types, (list, tuple)):
        return False
    return type_uri in types
