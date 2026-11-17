"""The deprecated aliases of the DRF adapter's old import paths, kept for one
release after it moved to `scrud_django.drf`: an old module is its new module
(`alias_module`), an old name of a module that stays is the moved object
(`moved_name`). Each warns once, a ``DeprecationWarning`` whose message
starts with the old path and names the new one."""

import importlib
import sys
import warnings

#: How long an alias stays, as each warning says.
KEPT = "kept for one release"


def alias_module(old: str, new: str) -> None:
    """Make the module `old`, while it is being imported, the module `new`
    (``sys.modules``): every name, the moved objects themselves. Warns, at
    the importing line (the import machinery's frames are skipped)."""
    warnings.warn(
        f"{old} is deprecated ({KEPT}): import {new}",
        DeprecationWarning,
        stacklevel=3,
    )
    sys.modules[old] = importlib.import_module(new)


def moved_name(module_globals: dict, name: str, new: str):
    """The object `name` of the module `new`, read through the module of
    `module_globals` (its ``__getattr__``): warns, at the reading line, then
    keeps it there, so that it warns once."""
    old = module_globals["__name__"]
    warnings.warn(
        f"{old}.{name} is deprecated ({KEPT}): use {new}.{name}",
        DeprecationWarning,
        stacklevel=3,
    )
    value = getattr(importlib.import_module(new), name)
    module_globals[name] = value
    return value
