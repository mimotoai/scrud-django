"""The system checks of published branches' prefixes, and the same rules as
the branch views apply them when a prefix is set (`prefix_problems`), and
of overrides of packaged UI layer items (`check_layer_conflicts`)."""

from django.core import checks
from django.urls import NoReverseMatch, Resolver404, get_script_prefix, resolve

CHECK_ID = "scrud_django_ui.W001"
LAYER_CONFLICTS_ID = "scrud_django_ui.W002"


def check_prefixes(app_configs, databases=None, **kwargs):
    """A database check (``manage.py check --database default``, and
    ``migrate``): a Warning (``scrud_django_ui.W001``) for each published
    branch's prefix, on each migrated database of `databases`, that

    - lies under the API's URLs (`selection.api_base`), where no prefix
      applies;
    - overlaps another: the URLs one claims (``<prefix><type slug>/...``,
      `middleware.routes_to_api`) include the other's, as ``/shop/`` and
      ``/shop/todo-lists/`` do (``/`` and ``/admin/resources/`` do not,
      unless ``admin`` is a type's slug); or
    - claims a URL that the project's URLconf serves with another view
      (``<prefix><type slug>/``), which the prefix would hide.

    A Warning, not an Error (ruling R26): it reports stored data, and an
    Error from a database check would stop ``migrate``; routing stays
    deterministic meanwhile (the longest prefix wins). The branch views
    refuse such a prefix when it is set (ruling R37), so only data stored
    otherwise, or a URLconf changed since, is reported. A database error
    reading a migrated database is reported (``scrud_django.W007``)."""
    from django.db.utils import OperationalError, ProgrammingError

    from scrud_django.checks import could_not_run, error_text, not_checked
    from scrud_django.ui.branches import PREFIX
    from scrud_django.ui.branches.middleware import FILE_ROUTES
    from scrud_django.ui.branches.store import _read_branches

    try:
        base = api_base_info()
    except NoReverseMatch:  # the project does not serve the UI's types
        return []
    found = []
    for alias in databases or ():
        if not_checked(alias):
            continue
        try:
            published = sorted(
                (b for b in _read_branches(alias).values() if b.prefix),
                key=lambda b: (b.prefix, b.name),
            )
            slugs = claimed_slugs(base, alias)
        except (OperationalError, ProgrammingError) as error:
            found.append(could_not_run(CHECK_ID, alias, error_text(error)))
            continue
        segments = {*slugs, *FILE_ROUTES}
        published = [b for b in published if PREFIX.fullmatch(b.prefix)]
        for branch in published:
            if under_api(branch.prefix, base):
                found.append(_under_api(branch, base))
                continue
            shadowed = shadowed_urls(branch.prefix, slugs)
            if shadowed:
                found.append(_shadowing(branch, shadowed))
        for index, outer in enumerate(published):
            for inner in published[index + 1 :]:
                if overlap(outer.prefix, inner.prefix, segments):
                    found.append(_overlap(outer, inner))
    return found


def api_base_info() -> str:
    """The API's base (`selection.api_base`) as a ``path_info`` (without
    the script prefix), as prefixes are matched. ``NoReverseMatch`` when the
    project does not serve the UI's types."""
    from scrud_django.ui.branches.selection import api_base

    base = api_base()
    script = get_script_prefix()
    return "/" + base[len(script) :] if base.startswith(script) else base


def claimed_slugs(base: str, using) -> list[str]:
    """The slugs of the types stored on `using` that the API at `base`
    serves with their resource views, sorted: the first segments a prefix
    claims (with the CSS and JS files')."""
    from scrud_django.models import ResourceType
    from scrud_django.ui.branches.middleware import routes_to_api

    stored = ResourceType.objects.using(using).values_list("slug", flat=True)
    return sorted(
        slug
        for slug in set(stored)
        if isinstance(slug, str) and routes_to_api(f"{base}{slug}/", f"{slug}/")
    )


def under_api(prefix: str, base: str) -> bool:
    """Whether `prefix` lies under the API's URLs (`base`, a ``path_info``),
    where no prefix applies."""
    return prefix.startswith(base)


def overlap(one: str, other: str, segments) -> bool:
    """Whether the prefixes `one` and `other` claim a common URL (ruling
    R25): the longer is the shorter, or the shorter followed by a claimed
    first segment (one of `segments`), as ``/shop/`` and
    ``/shop/todo-lists/`` are. ``/`` and ``/admin/resources/`` are not,
    unless ``admin`` is a type's slug."""
    outer, inner = sorted((one, other), key=len)
    if not inner.startswith(outer):
        return False
    rest = inner[len(outer) :]
    return not rest or rest.partition("/")[0] in segments


def shadowed_urls(prefix, slugs) -> list[str]:
    """The URLs ``<prefix><slug>/`` that the project's URLconf serves, each
    with its view's name."""
    shadowed = []
    for slug in slugs:
        path = f"{prefix}{slug}/"
        try:
            match = resolve(path)
        except Resolver404:
            continue
        shadowed.append(f"{path} ({match.view_name})")
    return shadowed


def prefix_problems(prefix: str, others, using) -> list[str]:
    """Why `prefix` (not ``""``), to be set on a branch, is refused (ruling
    R37): it lies under the API's URLs, claims URLs that the project's
    URLconf serves, or overlaps the prefix of one of `others` (the other
    stored branches), as the system check would warn. The types are read on
    `using`. Empty when there is none, or when the project does not serve
    the UI's types."""
    from scrud_django.ui.branches import PREFIX
    from scrud_django.ui.branches.middleware import FILE_ROUTES

    try:
        base = api_base_info()
    except NoReverseMatch:
        return []
    if under_api(prefix, base):
        return [
            f"The prefix {prefix!r} is under the API's URLs ({base!r}), where "
            "no prefix applies."
        ]
    slugs = claimed_slugs(base, using)
    problems = []
    shadowed = shadowed_urls(prefix, slugs)
    if shadowed:
        problems.append(
            f"The prefix {prefix!r} claims URLs that the project's URLconf "
            f"serves: {', '.join(shadowed)}. The branch's resource views would "
            "hide them."
        )
    segments = {*slugs, *FILE_ROUTES}
    for other in sorted(others, key=lambda b: (b.prefix or "", b.name)):
        if (
            other.prefix
            and PREFIX.fullmatch(other.prefix)
            and overlap(prefix, other.prefix, segments)
        ):
            problems.append(
                f"The prefix {prefix!r} overlaps the prefix {other.prefix!r} "
                f"of branch {other.name!r}: the URLs one claims "
                "(<prefix><type slug>/...) include the other's."
            )
    return problems


def _under_api(branch, base):
    return checks.Warning(
        f"The prefix {branch.prefix!r} of branch {branch.name!r} is under the "
        f"API's URLs ({base!r}), where no prefix applies.",
        hint="Give the branch a prefix outside the API's URLs.",
        id=CHECK_ID,
    )


def _shadowing(branch, shadowed):
    return checks.Warning(
        f"The prefix {branch.prefix!r} of branch {branch.name!r} claims URLs "
        f"that the project's URLconf serves: {', '.join(shadowed)}. The "
        "branch's resource views would hide them.",
        hint="Give the branch another prefix, or move the project's URLs.",
        id=CHECK_ID,
    )


def _overlap(outer, inner):
    return checks.Warning(
        f"The prefixes {outer.prefix!r} (branch {outer.name!r}) and "
        f"{inner.prefix!r} (branch {inner.name!r}) overlap: the URLs of "
        f"{inner.prefix!r} are among those {outer.prefix!r} claims.",
        hint="Give one of the branches a prefix that the other's types' URLs "
        "do not start.",
        id=CHECK_ID,
    )


def check_layer_conflicts(app_configs, databases=None, **kwargs):
    """A database check (``manage.py check --database default``, and
    ``migrate``): a Warning (``scrud_django_ui.W002``) for each branch, on
    each migrated database of `databases`, with overrides of packaged UI
    layer items in conflict with the installed layer (`layers.layer_conflicts`):
    one whose layer item changed since the override was based on it (an
    upgrade changed it), and one whose layer item is gone (an orphan, which
    keeps rendering as an ordinary item). A Warning, as ``W001``: it
    reports stored data. A branch whose chain is broken, or whose pinned
    snapshot is missing, cannot be checked (it renders its published
    branch): its Warning says so. A
    database error reading a migrated database is reported
    (``scrud_django.W007``).

    With no layer installed, only an orphan can be reported, and only an
    item named with a layer prefix (``abc:``) can be one: a database whose
    items have no such name is not read further (`_prefixed_names`, one
    query), so a site without layers pays one query of its own per database
    (besides the check that the database is migrated), not several per
    branch."""
    from django.db.utils import OperationalError, ProgrammingError

    from scrud_django.checks import could_not_run, error_text, not_checked
    from scrud_django.ui.branches.store import BranchChainError, _read_branches
    from scrud_django.ui.layers import installed_layers, layer_conflicts

    found = []
    for alias in databases or ():
        if not_checked(alias):
            continue
        try:
            if not installed_layers() and not _prefixed_names(alias):
                continue
            listed = []
            for name, branch in sorted(_read_branches(alias).items()):
                try:
                    conflicted = layer_conflicts(branch, using=alias)
                except BranchChainError as error:
                    listed.append((name, error))
                    continue
                if conflicted:
                    listed.append((name, conflicted))
        except (OperationalError, ProgrammingError) as error:
            found.append(could_not_run(LAYER_CONFLICTS_ID, alias, error_text(error)))
            continue
        found.extend(
            _not_checked(name, conflicted)
            if isinstance(conflicted, Exception)
            else _layer_conflicts(name, conflicted)
            for name, conflicted in listed
        )
    return found


def _not_checked(name, error):
    """The W002 of branch `name`, which could not be checked: its chain is
    broken, or its pinned snapshot is missing (`error`)."""
    return checks.Warning(
        f"Branch {name!r} was not checked for conflicts with packaged UI "
        f"layers: {error}",
        hint="Its chain is broken or its pinned snapshot is missing, so it "
        "renders its published branch: repair or delete it.",
        id=LAYER_CONFLICTS_ID,
    )


def _prefixed_names(alias) -> bool:
    """Whether any item stored on database `alias` (of any branch, a
    deletion marker too) has a name with a layer prefix (`name_prefix`):
    its names alone, unsorted, in one query."""
    from django.db.models.fields.json import KeyTextTransform

    from scrud_django.models import Resource
    from scrud_django.rows import unsorted
    from scrud_django.ui.branches import ITEM_KINDS
    from scrud_django.ui.layers import name_prefix

    names = unsorted(
        Resource.objects.using(alias).filter(
            resource_type__type_uri__in=list(ITEM_KINDS.values())
        )
    ).values_list(KeyTextTransform("name", "content"), flat=True)
    return any(isinstance(name, str) and name_prefix(name) for name in names)


def _layer_conflicts(name, conflicted):
    def described(entry):
        key = entry["key"]
        text = f"{entry['kind']} {key[0]!r}"
        if len(key) > 1 and key[1]:
            text += f" (applies to {key[1]!r})"
        return text

    changed = [described(e) for e in conflicted if e["current"] is not None]
    orphaned = [described(e) for e in conflicted if e["current"] is None]
    parts = []
    if changed:
        parts.append(
            "overrides packaged UI layer items that changed since they were "
            f"based on them: {', '.join(changed)}"
        )
    if orphaned:
        parts.append(
            "has orphaned overrides of packaged UI layer items that are no "
            f"longer installed (they render as ordinary items): {', '.join(orphaned)}"
        )
    return checks.Warning(
        f"Branch {name!r} {'; and '.join(parts)}.",
        hint="Re-save each changed override with rebase: true to keep it (its "
        "base moves to the layer item's ETag), or delete it to show the "
        "layer's; delete or rename an orphaned override.",
        id=LAYER_CONFLICTS_ID,
    )
