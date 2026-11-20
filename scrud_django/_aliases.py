"""The deprecated aliases of the DRF adapter's old import paths, kept for one
release after it moved to `scrud_django.drf`: an old module is its new module
(`alias_module`), an old name of a module that stays is the moved object
(`moved_name`). Each warns once, a ``DeprecationWarning`` whose message
starts with the old path and names the new one."""

import importlib
import os
import sys
import warnings

#: How long an alias stays, as each warning says.
KEPT = "kept for one release"


def _passed_through(filename: str, alias_file: str) -> bool:
    """Whether a frame of `filename` only passes an alias's use on: this
    module, the alias's own module (`alias_file`: the old module's body, or
    its ``__getattr__``), the import machinery (its frozen frames and
    ``importlib.import_module``) and Django's ``import_string``."""
    import django.utils.module_loading

    return (
        filename in (__file__, alias_file, django.utils.module_loading.__file__)
        or filename.startswith("<frozen importlib.")
        or filename.startswith(os.path.dirname(importlib.__file__) + os.sep)
    )


def _warn(message: str, alias_file: str) -> None:
    """Warn `message` (a ``DeprecationWarning``) at the line that used the
    alias: the first frame that does not only pass it on (`_passed_through`),
    so that ``importlib.import_module`` and Django's ``import_string`` (DRF's
    settings) name their caller. (``warnings.warn``'s ``stacklevel`` cannot:
    the frames between differ by path, and CPython 3.13's
    ``skip_file_prefixes`` does not skip them.) As ``warnings.warn`` does,
    the filters see the caller's module and its ``__warningregistry__``."""
    frame = sys._getframe(1)
    while frame.f_back is not None and _passed_through(
        frame.f_code.co_filename, alias_file
    ):
        frame = frame.f_back
    module_globals = frame.f_globals
    warnings.warn_explicit(
        message,
        DeprecationWarning,
        frame.f_code.co_filename,
        frame.f_lineno,
        module=module_globals.get("__name__", "<string>"),
        registry=module_globals.setdefault("__warningregistry__", {}),
    )


def alias_module(old: str, new: str) -> None:
    """Make the module `old`, while it is being imported, the module `new`
    (``sys.modules``): every name, the moved objects themselves. Warns, at
    the importing line (`_warn`)."""
    _warn(f"{old} is deprecated ({KEPT}): import {new}", sys.modules[old].__file__)
    sys.modules[old] = importlib.import_module(new)


def moved_name(module_globals: dict, name: str, new: str):
    """The object `name` of the module `new`, read through the module of
    `module_globals` (its ``__getattr__``): warns, at the reading line, then
    keeps it there, so that it warns once."""
    old = module_globals["__name__"]
    _warn(
        f"{old}.{name} is deprecated ({KEPT}): use {new}.{name}",
        module_globals["__file__"],
    )
    value = getattr(importlib.import_module(new), name)
    module_globals[name] = value
    return value
