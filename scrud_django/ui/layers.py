"""Packaged UI layers: components, stylesheets, CSS and JS items an installed
app ships as package files, beneath every branch (see ``docs/ui.md``,
"Packaged UI layers").

An app declares a layer with an `AppConfig` attribute::

    scrud_ui_layer = {"prefix": "studio:", "package": "myapp.ui_layer"}

The package holds ``component/*.json``, ``stylesheet/*.json``, ``css/*.json``
and ``js/*.json``, each one item's content without the branch fields, and
every item's name starts with the layer's prefix. Layers stack in
``INSTALLED_APPS`` order. They are read once (`installed_layers`,
`layer_rows`), and read again after any setting changes (``setting_changed``)
or `clear_caches`.

Layer items are not rows: they show on the virtual branch `LAYER_BRANCH`,
which `effective.effective_rows` and `effective.parent_state` put at the
bottom of every chain, so a branch overrides one by name as it does any
inherited item. A file that is not valid is a system check Error
(`check_layers`, ``scrud_django_ui.E002``) and is left out."""

import copy
import hashlib
import importlib.resources
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from types import MappingProxyType

from django.apps import apps
from django.core import checks
from django.core.signals import setting_changed

from scrud_django.ui.branches import ITEM_KINDS
from scrud_django.ui.branches.model import Branch, item_key, kind_of

CHECK_ID = "scrud_django_ui.E002"

# The virtual branch of every layer item. A branch's name matches
# ``^[a-z0-9]``, so none has this one.
LAYER_BRANCH_NAME = "@layers"
LAYER_BRANCH = Branch(
    name=LAYER_BRANCH_NAME,
    context="",
    parent=None,
    base="live",
    branching_point=MappingProxyType({}),
    prefix=None,
    pk=None,
)

# The name of every UI item (the item schemas' ``name`` pattern): an
# optional layer prefix, never ``scrud:``, which the built-ins have.
NAME_PATTERN = re.compile(
    r"^(?!scrud:)(?:[a-z][a-z0-9-]*:)?[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
)
# A layer's prefix, and the prefix of a name.
PREFIX_PATTERN = re.compile(r"^[a-z][a-z0-9-]*:$")
_NAME_PREFIX = re.compile(r"^([a-z][a-z0-9-]*:)")
_RESERVED_PREFIX = "scrud:"

# Fields a layer file may not have: the branch fields, which the layer sets.
_BRANCH_FIELDS = ("branch", "rbac_context", "deleted", "base")
# The text field of the CSS and JS items, and what it may not contain.
_TEXT_FIELDS = {"css": ("css", "</style"), "js": ("js", "</script")}


@dataclass(frozen=True)
class UILayer:
    label: str  # the declaring app's label
    prefix: str
    package: str


def installed_layers() -> tuple[UILayer, ...]:
    """The valid layers the installed apps declare, in ``INSTALLED_APPS``
    order (read once; see the module docstring)."""
    return _declared()[0]


def layer_rows(type_uri: str) -> list[tuple[None, str, dict]]:
    """``(None, etag, content)`` of the layer items of `type_uri`, layer by
    layer, each in file name order: a copy of each, on `LAYER_BRANCH_NAME`
    in context ``""``. The ETag is the sha256 hex of the file's content as
    canonical JSON (sorted keys, no spaces)."""
    kind = kind_of(type_uri)
    if kind is None:
        return []
    return [
        (None, etag, copy.deepcopy(content))
        for etag, content in _loaded()[0].get(kind, {}).values()
    ]


def layer_item(kind: str, key: tuple) -> tuple[str, dict] | None:
    """``(etag, content)`` of the layer item of `kind` whose `item_key` is
    `key` (a stylesheet's: ``(name, "")``), a copy, or ``None``."""
    found = _loaded()[0].get(kind, {}).get(tuple(key))
    return None if found is None else (found[0], copy.deepcopy(found[1]))


def layer_index(name: str) -> int | None:
    """The position in `installed_layers` of the layer whose prefix `name`
    has, or ``None``."""
    prefix = name_prefix(name)
    return next(
        (i for i, layer in enumerate(installed_layers()) if layer.prefix == prefix),
        None,
    )


def name_prefix(name: str) -> str | None:
    """The layer prefix (``"abc:"``) `name` starts with, or ``None``."""
    match = _NAME_PREFIX.match(name)
    return match.group(1) if match else None


def may_use_name(kind: str, content: Mapping) -> bool:
    """Whether an item of `kind` with `content` (its ``applies_to`` set,
    for a stylesheet) may be stored with its ``name``: a name without a
    prefix, or the name of an installed layer's item of that kind (and
    ``applies_to``), which the item overrides. Never ``scrud:``, nor with
    an ``applies_to`` that is not a string (which the schema refuses)."""
    name = content.get("name")
    if not isinstance(name, str) or name_prefix(name) is None:
        return True
    if kind == "stylesheet":
        target = content.get("applies_to", "")
        if not isinstance(target, str):
            return False
        key = (name, target)
    else:
        key = (name,)
    return _loaded()[0].get(kind, {}).get(key) is not None


def check_layers(app_configs=None, **kwargs) -> list[checks.CheckMessage]:
    """An Error (``scrud_django_ui.E002``) for each layer declaration that
    is not valid (it is left out) and each layer file that is not: one that
    is unreadable or not a JSON object, whose name is not a valid name with
    its layer's prefix, that has a branch field, that has another file's
    name, a stylesheet with a non-empty ``applies_to``, or a CSS or JS item
    whose text has the closing tag a viewset refuses. Such a file is left
    out."""
    return [*_declared()[1], *_loaded()[1]]


def clear_caches(**kwargs):
    """Read the layers again on next use: a receiver of
    ``setting_changed`` (connected below), and for a test that changes a
    layer's files."""
    _declared.cache_clear()
    _loaded.cache_clear()


setting_changed.connect(clear_caches, dispatch_uid="scrud_django_ui_layers")


def _error(message: str) -> checks.Error:
    return checks.Error(message, id=CHECK_ID)


@cache
def _declared() -> tuple[tuple[UILayer, ...], tuple[checks.Error, ...]]:
    layers, errors, prefixes = [], [], set()
    for config in apps.get_app_configs():
        declared = getattr(config, "scrud_ui_layer", None)
        if declared is None:
            continue
        where = f"The UI layer of app {config.label!r}"
        if not isinstance(declared, Mapping):
            errors.append(_error(f"{where} is not a mapping (prefix, package)."))
            continue
        prefix, package = declared.get("prefix"), declared.get("package")
        if (
            not isinstance(prefix, str)
            or not PREFIX_PATTERN.match(prefix)
            or prefix == _RESERVED_PREFIX
        ):
            errors.append(
                _error(
                    f"{where} has prefix {prefix!r}: a prefix is lowercase letters, "
                    "digits and '-', starts with a letter and ends with ':', and "
                    "is not 'scrud:'."
                )
            )
            continue
        if prefix in prefixes:
            errors.append(_error(f"{where} has prefix {prefix!r}, already in use."))
            continue
        if not isinstance(package, str) or not package:
            errors.append(_error(f"{where} names no package."))
            continue
        prefixes.add(prefix)
        layers.append(UILayer(label=config.label, prefix=prefix, package=package))
    return tuple(layers), tuple(errors)


@cache
def _loaded() -> tuple[dict[str, dict[tuple, tuple[str, dict]]], tuple]:
    """``({kind: {item key: (etag, content)}}, errors)`` of the installed
    layers, in layer then file name order."""
    items: dict[str, dict] = {kind: {} for kind in ITEM_KINDS}
    errors = []
    for layer in installed_layers():
        try:
            root = importlib.resources.files(layer.package)
        except (ImportError, TypeError) as error:
            errors.append(
                _error(
                    f"The UI layer of app {layer.label!r}: package "
                    f"{layer.package!r} cannot be read: {error}"
                )
            )
            continue
        for kind in ITEM_KINDS:
            folder = root.joinpath(kind)
            if not folder.is_dir():
                continue
            files = sorted(
                (entry for entry in folder.iterdir() if entry.name.endswith(".json")),
                key=lambda entry: entry.name,
            )
            for entry in files:
                where = f"UI layer file {layer.package}/{kind}/{entry.name}"
                found = _read(kind, layer, entry, where)
                if isinstance(found, str):
                    errors.append(_error(f"{where}: {found}"))
                    continue
                etag, content = found
                key = item_key(kind, content, LAYER_BRANCH)
                if key in items[kind]:
                    errors.append(
                        _error(f"{where}: another file has the name {key[0]!r}.")
                    )
                    continue
                items[kind][key] = (etag, content)
    return items, tuple(errors)


def _read(kind, layer: UILayer, entry, where) -> tuple[str, dict] | str:
    """``(etag, content)`` of layer file `entry`, or why it is not valid."""
    try:
        packaged = json.loads(entry.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as error:
        return f"it cannot be read as JSON: {error}"
    if not isinstance(packaged, dict):
        return "it is not a JSON object."
    name = packaged.get("name")
    if (
        not isinstance(name, str)
        or not NAME_PATTERN.match(name)
        or not name.startswith(layer.prefix)
    ):
        return (
            f"its name {name!r} is not a valid item name with the layer's "
            f"prefix {layer.prefix!r}."
        )
    present = [field for field in _BRANCH_FIELDS if field in packaged]
    if present:
        return f"it has branch fields {present}, which the layer sets."
    if kind == "stylesheet" and packaged.get("applies_to", "") != "":
        return "a layer stylesheet applies everywhere: its applies_to must be ''."
    if kind in _TEXT_FIELDS:
        field, closing = _TEXT_FIELDS[kind]
        text = packaged.get(field)
        if isinstance(text, str) and closing in text.lower():
            return f"its {field} contains {closing!r}."
    canonical = json.dumps(packaged, sort_keys=True, separators=(",", ":"))
    etag = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    content = {**packaged, "branch": LAYER_BRANCH_NAME, "rbac_context": ""}
    if kind == "stylesheet":
        content["applies_to"] = ""
    return etag, content
