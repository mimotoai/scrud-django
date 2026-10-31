"""MyST heading slugs for the scrud-django docs."""

import re

try:
    # Private to myst-parser: pinned to its major version in pyproject.toml.
    from myst_parser.mdit_to_docutils.base import default_slugify
except ImportError as error:  # pragma: no cover
    raise ImportError(
        "docs/_ext/myst_slugs.py needs myst_parser.mdit_to_docutils.base."
        "default_slugify, which this myst-parser version does not provide. "
        "Install the myst-parser range pinned in pyproject.toml's docs group "
        "or update this extension."
    ) from error

FOLLOW_UP_HEADING = re.compile(r"^[a-z0-9-]+:")


def follow_up_slug(title):
    """Slug a follow-ups heading ("short-slug: title", under an
    ``<a id="short-slug"></a>`` for GitLab) by its anchor, so that links such as
    ``follow-ups.md#short-slug`` resolve; any other heading gets MyST's default
    slug. Only a lower-case id (``[a-z0-9-]+``) before the first colon counts."""
    if FOLLOW_UP_HEADING.match(title):
        return title.partition(":")[0]
    return default_slugify(title)
