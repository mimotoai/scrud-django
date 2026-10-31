"""Release checks used by the CI pipeline (.gitlab-ci.yml).

The version to release is set in the merge request (``uv version 0.2.0`` or
``uv version --bump minor``); merging it to the default branch publishes it.

``check-bump BASE TARGET``
    Merge requests: if the MR changes the package (``scrud_django/``, or
    ``pyproject.toml`` outside the version and the dev-only sections) since
    ``BASE``, or changes the version (which publishes it when merged), its
    version must be higher than the ``TARGET`` branch's version and than every
    release on PyPI.

``needs-release``
    Default branch: exit 0 if the version in ``pyproject.toml`` is not on PyPI
    yet, or only some of its files are (publish it), 10 if it is (nothing to
    release); any other status is an error. Refuses (exit 1) a version that is
    not on PyPI and is lower than its latest release.

``create-release``
    Default branch: create the tag ``vX.Y.Z`` and a GitLab release for the
    current commit, unless they already exist for it. Runs before the upload,
    so a failed upload can be retried from the same commit.

Needs ``packaging`` (run with ``uv run --no-project --with packaging``).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
from typing import Any

from packaging.version import Version

PACKAGE = "scrud-django"
PACKAGE_PATHS = ("scrud_django/",)
# pyproject.toml sections that don't change what is published, except for
# [tool.uv.build-backend], which decides what goes into the wheel and sdist.
DEV_ONLY_SECTIONS = ("dependency-groups", "tool")
PYPI = "https://pypi.org/pypi"
ALREADY_RELEASED = 10
# The files `uv build` makes, by PyPI's packagetype: a release has both.
PACKAGETYPES = frozenset({"sdist", "bdist_wheel"})


def version_of(pyproject: dict[str, Any]) -> Version:
    # Before the move to uv, the metadata lived in Poetry's [tool.poetry] table.
    metadata = pyproject.get("project") or pyproject["tool"]["poetry"]
    return Version(metadata["version"])


def published_part(pyproject: dict[str, Any]) -> dict[str, Any]:
    """The pyproject.toml content that affects the package, minus the version."""
    part = {k: v for k, v in pyproject.items() if k not in DEV_ONLY_SECTIONS}
    if "project" in part:
        part["project"] = {k: v for k, v in part["project"].items() if k != "version"}
    part["tool.uv.build-backend"] = (
        pyproject.get("tool", {}).get("uv", {}).get("build-backend")
    )
    return part


def bump_required(
    changed_paths: list[str], old: dict[str, Any], new: dict[str, Any]
) -> bool:
    """Whether a change to these paths and pyproject.toml needs a new version."""
    if any(path.startswith(PACKAGE_PATHS) for path in changed_paths):
        return True
    return published_part(old) != published_part(new)


def bump_error(new: Version, target: Version, released: list[Version]) -> str | None:
    """Why ``new`` can't be released after ``target`` and ``released``, if so."""
    if new in released:
        return f"{PACKAGE} {new} is already on PyPI."
    if new <= target:
        return f"{new} is not higher than the target branch's version ({target})."
    latest = max(released, default=None)
    if latest is not None and new <= latest:
        return f"{new} is not higher than the latest release on PyPI ({latest})."
    return None


def released_versions() -> list[Version]:
    try:
        with urllib.request.urlopen(f"{PYPI}/{PACKAGE}/json", timeout=30) as resp:
            releases = json.load(resp)["releases"]
    except urllib.error.HTTPError as error:
        error.close()
        if error.code == 404:
            return []
        raise
    return [Version(v) for v, files in releases.items() if files]


def uploaded_packagetypes(version: Version) -> set[str]:
    """The kinds of file (``sdist``, ``bdist_wheel``) of ``version`` on PyPI."""
    url = f"{PYPI}/{PACKAGE}/{version}/json"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            files = json.load(resp)["urls"]
    except urllib.error.HTTPError as error:
        error.close()
        if error.code == 404:
            return set()
        raise
    return {file["packagetype"] for file in files}


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True
    ).stdout


def pyproject_at(ref: str | None) -> dict[str, Any]:
    if ref is None:
        with open("pyproject.toml", "rb") as f:
            return tomllib.load(f)
    return tomllib.loads(git("show", f"{ref}:pyproject.toml"))


def check_bump(base: str, target: str) -> int:
    changed = git("diff", "--name-only", base, "HEAD").split()
    old, new = pyproject_at(base), pyproject_at(None)
    version, target_version = version_of(new), version_of(pyproject_at(target))
    package_changed = bump_required(changed, old, new)
    # Merging publishes a version the MR changed, unless the target has it
    # already. A version the MR left alone stays the target's when merged.
    version_changed = version not in (version_of(old), target_version)
    if not package_changed and not version_changed:
        print(f"No package changes since {base}; no version bump needed.")
        return 0
    error = bump_error(version, target_version, released_versions())
    if error:
        if package_changed:
            print(
                "This merge request changes the package, so it must bump the version."
            )
        else:
            print("This merge request changes the version, which publishes it when")
            print("merged, so it must be higher than the target's and every release.")
        print(error)
        print("Set the release version with e.g. `uv version --bump minor` or")
        print("`uv version 1.2.0`, and commit pyproject.toml and uv.lock.")
        return 1
    print(f"Version {version} will be published when this is merged.")
    return 0


def needs_release() -> int:
    version = version_of(pyproject_at(None))
    uploaded = uploaded_packagetypes(version)
    if uploaded >= PACKAGETYPES:
        print(f"{PACKAGE} {version} is already on PyPI; nothing to release.")
        return ALREADY_RELEASED
    if uploaded:
        # `uv publish --check-url` skips the files already there.
        print(
            f"{PACKAGE} {version} is only partly on PyPI ({', '.join(sorted(uploaded))});"
        )
        print("uploading the rest.")
        return 0
    latest = max(released_versions(), default=None)
    if latest is not None and version < latest:
        print(
            f"{PACKAGE} {version} is lower than the latest release on PyPI ({latest});"
        )
        print("refusing to publish it. Set a higher version in pyproject.toml.")
        return 1
    print(f"{PACKAGE} {version} is not on PyPI yet; releasing it.")
    return 0


def gitlab(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    request = urllib.request.Request(
        f"{os.environ['CI_API_V4_URL']}/projects/{os.environ['CI_PROJECT_ID']}{path}",
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "JOB-TOKEN": os.environ["CI_JOB_TOKEN"],
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as resp:
        return json.load(resp)


def gitlab_get(path: str) -> Any:
    """GET path from the GitLab API, or None if it doesn't exist."""
    try:
        return gitlab("GET", path)
    except urllib.error.HTTPError as error:
        error.close()
        if error.code != 404:
            raise
        return None


def create_release() -> int:
    version = version_of(pyproject_at(None))
    tag, sha = f"v{version}", os.environ["CI_COMMIT_SHA"]
    existing = gitlab_get(f"/releases/{tag}")
    if existing is not None:
        if existing["commit"]["id"] != sha:
            print(f"Release {tag} already exists for {existing['commit']['id']},")
            print(f"not {sha}. Bump the version or delete that release and tag.")
            return 1
        print(f"Release {tag} already exists for this commit.")
        return 0
    # GitLab ignores `ref` when the tag exists, so a tag pushed by hand (or left by a
    # failed run) would otherwise get this release on whatever commit it points at.
    existing_tag = gitlab_get(f"/repository/tags/{tag}")
    if existing_tag is not None and existing_tag["commit"]["id"] != sha:
        print(f"Tag {tag} already exists on {existing_tag['commit']['id']}, not {sha}.")
        print("Bump the version or delete that tag.")
        return 1
    gitlab(
        "POST",
        "/releases",
        {
            "name": tag,
            "tag_name": tag,
            "ref": sha,
            "description": (
                f"Published to PyPI: https://pypi.org/project/{PACKAGE}/{version}/\n\n"
                "See docs/release-notes.md for the changes."
            ),
        },
    )
    created = "release" if existing_tag is not None else "tag and release"
    print(f"Created {created} {tag} for {sha}.")
    return 0


def main(argv: list[str]) -> int:
    match argv:
        case ["check-bump", base, target]:
            return check_bump(base, target)
        case ["needs-release"]:
            return needs_release()
        case ["create-release"]:
            return create_release()
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
