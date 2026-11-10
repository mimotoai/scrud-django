"""The system check of published branches' prefixes."""

from django.core import checks
from django.urls import NoReverseMatch, Resolver404, get_script_prefix, resolve

CHECK_ID = "scrud_django_ui.W001"


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
    deterministic meanwhile (the longest prefix wins). A database error
    reading a migrated database is reported (``scrud_django.W007``)."""
    from django.db.utils import OperationalError, ProgrammingError

    from scrud_django.checks import could_not_run, error_text, not_checked
    from scrud_django.models import ResourceType
    from scrud_django.ui.branches import PREFIX
    from scrud_django.ui.branches.middleware import CSS_FILES_SEGMENT, routes_to_api
    from scrud_django.ui.branches.selection import api_base
    from scrud_django.ui.branches.store import _read_branches

    try:
        base = api_base()
    except NoReverseMatch:  # the project does not serve the UI's types
        return []
    script = get_script_prefix()
    base = "/" + base[len(script) :] if base.startswith(script) else base
    found = []
    for alias in databases or ():
        if not_checked(alias):
            continue
        try:
            published = sorted(
                (b for b in _read_branches(alias).values() if b.prefix),
                key=lambda b: (b.prefix, b.name),
            )
            stored_slugs = set(
                ResourceType.objects.using(alias).values_list("slug", flat=True)
            )
        except (OperationalError, ProgrammingError) as error:
            found.append(could_not_run(CHECK_ID, alias, error_text(error)))
            continue
        slugs = sorted(
            slug
            for slug in stored_slugs
            if isinstance(slug, str) and routes_to_api(f"{base}{slug}/", f"{slug}/")
        )
        segments = {*slugs, CSS_FILES_SEGMENT}
        published = [b for b in published if PREFIX.fullmatch(b.prefix)]
        for branch in published:
            if branch.prefix.startswith(base):
                found.append(_under_api(branch, base))
                continue
            shadowed = _shadowed(branch.prefix, slugs)
            if shadowed:
                found.append(_shadowing(branch, shadowed))
        for index, outer in enumerate(published):
            for inner in published[index + 1 :]:
                rest = inner.prefix[len(outer.prefix) :]
                if inner.prefix.startswith(outer.prefix) and (
                    not rest or rest.partition("/")[0] in segments
                ):
                    found.append(_overlap(outer, inner))
    return found


def _shadowed(prefix, slugs):
    """The URLs ``<prefix><slug>/`` that the project's URLconf serves."""
    shadowed = []
    for slug in slugs:
        path = f"{prefix}{slug}/"
        try:
            match = resolve(path)
        except Resolver404:
            continue
        shadowed.append(f"{path} ({match.view_name})")
    return shadowed


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
