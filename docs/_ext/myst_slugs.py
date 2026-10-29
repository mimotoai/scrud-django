"""MyST heading slugs for the scrud-django docs."""

from myst_parser.mdit_to_docutils.base import default_slugify


def follow_up_slug(title):
    """Slug a follow-ups heading ("short-slug: title", under an
    ``<a id="short-slug"></a>`` for GitLab) by its anchor, so that links such as
    ``follow-ups.md#short-slug`` resolve; any other heading gets MyST's default
    slug."""
    prefix, separator, _ = title.partition(":")
    if separator and prefix and " " not in prefix:
        return prefix
    return default_slugify(title)
