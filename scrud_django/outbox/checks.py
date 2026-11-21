"""The system check of ``SCRUD_OUTBOX`` (registered when
``scrud_django.outbox`` is installed: ``OutboxConfig.ready()`` imports this
module)."""

from collections.abc import Mapping

from django.core import checks

from scrud_django.outbox.conf import (
    ALL,
    configured_types,
    outbox_settings,
    types_are_well_formed,
)

HINT = (
    'Set SCRUD_OUTBOX["TYPES"] to a list of the type URIs to record, or to "__all__".'
)


@checks.register()
def check_outbox_types(app_configs, **kwargs):
    """A Warning (``scrud_django.W010``) when ``SCRUD_OUTBOX["TYPES"]`` is
    unset or empty: the outbox app is installed and records nothing. An
    Error (``scrud_django.E013``) when it is neither ``"__all__"`` nor a list
    or tuple of strings, or ``SCRUD_OUTBOX`` is not a mapping: it records
    nothing either."""
    if not isinstance(outbox_settings(), Mapping):
        return [
            checks.Error(
                f"SCRUD_OUTBOX must be a mapping, not {outbox_settings()!r}; the "
                "outbox records nothing.",
                hint=HINT,
                id="scrud_django.E013",
            )
        ]
    types = configured_types()
    if types is None or (isinstance(types, (list, tuple)) and not types):
        return [
            checks.Warning(
                "scrud_django.outbox is installed, but SCRUD_OUTBOX[\"TYPES\"] is "
                "unset or empty: it records no writes.",
                hint=HINT,
                id="scrud_django.W010",
            )
        ]
    if not types_are_well_formed(types):
        return [
            checks.Error(
                f"SCRUD_OUTBOX[\"TYPES\"] must be {ALL!r} or a list or tuple of type "
                f"URIs (strings), not {types!r}; the outbox records nothing.",
                hint=HINT,
                id="scrud_django.E013",
            )
        ]
    return []
