"""Tests for scripts/release.py, the CI release checks."""

import importlib.util
import io
import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from packaging.version import Version

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "release.py"
_spec = importlib.util.spec_from_file_location("release_script", _PATH)
release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release)


def http_error(code):
    return urllib.error.HTTPError("https://x", code, "error", {}, io.BytesIO())


def pyproject(version="1.0.0", deps=(), dev=("pytest",), tool=None):
    return {
        "project": {
            "name": "scrud-django",
            "version": version,
            "dependencies": list(deps),
        },
        "dependency-groups": {"dev": list(dev)},
        "tool": tool or {"uv": {"build-backend": {"module-root": ""}}},
        "build-system": {"requires": ["uv_build"], "build-backend": "uv_build"},
    }


# --- bump_required -----------------------------------------------------------


@pytest.mark.parametrize(
    "paths, expected",
    [
        (["scrud_django/steps.py"], True),
        (["scrud_django/py.typed", "docs/intro.md"], True),
        (["docs/intro.md", "README.md", ".gitlab-ci.yml"], False),
        (["tests/test_Context.py", "tests/typecheck/usage.py"], False),
        (["scripts/release.py", "uv.lock"], False),
        ([], False),
    ],
)
def test_bump_required_by_paths(paths, expected):
    assert release.bump_required(paths, pyproject(), pyproject()) is expected


def test_bump_required_by_published_metadata():
    old = pyproject()
    assert release.bump_required(["pyproject.toml"], old, pyproject(deps=["x"]))
    changed = pyproject()
    changed["project"]["description"] = "new"
    assert release.bump_required(["pyproject.toml"], old, changed)
    changed = pyproject()
    changed["build-system"]["requires"] = ["uv_build>=1"]
    assert release.bump_required(["pyproject.toml"], old, changed)


def test_dev_only_pyproject_changes_need_no_bump():
    old = pyproject()
    assert not release.bump_required(["pyproject.toml"], old, pyproject(dev=["x"]))
    tool = {"uv": {"build-backend": {"module-root": ""}}, "pytest": {}}
    assert not release.bump_required(["pyproject.toml"], old, pyproject(tool=tool))
    tool = {
        "uv": {"build-backend": {"module-root": ""}, "default-groups": ["dev"]},
    }
    assert not release.bump_required(["pyproject.toml"], old, pyproject(tool=tool))
    # The version itself is not a package change.
    assert not release.bump_required(["pyproject.toml"], old, pyproject("2.0.0"))


def test_build_backend_changes_need_a_bump():
    """[tool.uv.build-backend] decides what goes into the wheel and sdist."""
    old = pyproject()
    tool = {"uv": {"build-backend": {"module-root": "", "source-exclude": ["x"]}}}
    assert release.bump_required(["pyproject.toml"], old, pyproject(tool=tool))
    assert release.bump_required(["pyproject.toml"], old, pyproject(tool={"uv": {}}))


def poetry_pyproject(version="0.3.2"):
    """The Poetry-format pyproject.toml the default branch had before uv."""
    return {
        "tool": {"poetry": {"name": "scrud-django", "version": version}},
        "build-system": {"requires": ["poetry-core>=1.0.0"]},
    }


def test_version_of_reads_poetry_metadata():
    assert release.version_of(poetry_pyproject("0.3.2")) == Version("0.3.2")


def test_migrating_from_poetry_needs_a_bump():
    assert release.bump_required(["pyproject.toml"], poetry_pyproject(), pyproject())


# --- bump_error --------------------------------------------------------------


def versions(*values):
    return [Version(v) for v in values]


@pytest.mark.parametrize(
    "new, target, released",
    [
        ("0.4.0", "0.3.0", ("0.2.0",)),
        ("1.0.0", "0.3.0", ("0.2.0",)),
        ("0.3.0", "0.2.0", ()),  # nothing on PyPI yet
        ("0.3.1", "0.3.0", ("0.2.0", "0.3.0")),
        ("1.0.0rc1", "0.9.0", ("0.9.0",)),
    ],
)
def test_bump_ok(new, target, released):
    assert (
        release.bump_error(Version(new), Version(target), versions(*released)) is None
    )


@pytest.mark.parametrize(
    "new, target, released, message",
    [
        ("0.3.0", "0.3.0", ("0.2.0",), "not higher than the target"),
        ("0.2.9", "0.3.0", ("0.2.0",), "not higher than the target"),
        ("0.3.0", "0.2.0", ("0.2.0", "0.3.0"), "already on PyPI"),
        ("0.2.5", "0.2.0", ("0.3.0",), "not higher than the latest release"),
    ],
)
def test_bump_rejected(new, target, released, message):
    error = release.bump_error(Version(new), Version(target), versions(*released))
    assert message in error


# --- commands ----------------------------------------------------------------


@pytest.fixture
def repo(monkeypatch):
    """Fake git, pyproject.toml files and PyPI for the commands."""
    state = {
        "changed": ["scrud_django/steps.py"],
        "base": pyproject("0.3.0"),
        "target": pyproject("0.3.0"),
        "head": pyproject("0.4.0"),
        "released": versions("0.2.0", "0.3.0"),
    }
    monkeypatch.setattr(release, "git", lambda *a: "\n".join(state["changed"]))
    monkeypatch.setattr(
        release,
        "pyproject_at",
        lambda ref: {None: state["head"], "BASE": state["base"]}.get(
            ref, state["target"]
        ),
    )
    monkeypatch.setattr(release, "released_versions", lambda: state["released"])
    # The kinds of file on PyPI per version; a released version has both.
    state["files"] = {}
    monkeypatch.setattr(
        release,
        "uploaded_packagetypes",
        lambda v: state["files"].get(
            v, release.PACKAGETYPES if v in state["released"] else set()
        ),
    )
    return state


def test_check_bump_passes_with_a_new_version(repo, capsys):
    assert release.main(["check-bump", "BASE", "TARGET"]) == 0
    assert "0.4.0 will be published" in capsys.readouterr().out


def test_check_bump_fails_without_a_bump(repo, capsys):
    repo["head"] = pyproject("0.3.0")
    assert release.main(["check-bump", "BASE", "TARGET"]) == 1
    out = capsys.readouterr().out
    assert "must bump the version" in out and "uv version --bump" in out


def test_check_bump_skips_non_package_changes(repo, capsys):
    repo["changed"], repo["head"] = ["docs/intro.md"], pyproject("0.3.0")
    assert release.main(["check-bump", "BASE", "TARGET"]) == 0
    assert "no version bump needed" in capsys.readouterr().out


def test_check_bump_validates_a_version_only_change(repo, capsys):
    """Changing the version publishes it, so it is checked even when nothing
    else in the package changed."""
    repo["changed"] = ["pyproject.toml", "docs/intro.md"]
    repo["released"] = versions("0.2.0", "0.3.0", "0.5.0")
    repo["head"] = pyproject("0.2.5")
    assert release.main(["check-bump", "BASE", "TARGET"]) == 1
    out = capsys.readouterr().out
    assert "changes the version" in out
    assert "0.2.5 is not higher than the target branch's version (0.3.0)" in out
    repo["head"] = pyproject("0.4.0")
    assert release.main(["check-bump", "BASE", "TARGET"]) == 1
    out = capsys.readouterr().out
    assert "0.4.0 is not higher than the latest release on PyPI (0.5.0)" in out
    repo["head"] = pyproject("0.5.0")
    assert release.main(["check-bump", "BASE", "TARGET"]) == 1
    assert "0.5.0 is already on PyPI" in capsys.readouterr().out
    repo["head"] = pyproject("0.6.0")
    assert release.main(["check-bump", "BASE", "TARGET"]) == 0
    assert "0.6.0 will be published" in capsys.readouterr().out


def test_check_bump_skips_a_version_the_merge_does_not_change(repo, capsys):
    """A branch that leaves the version alone keeps the target's when merged,
    even if the target's moved on since the branch started."""
    repo["changed"] = ["docs/intro.md"]
    repo["base"], repo["head"] = pyproject("0.2.0"), pyproject("0.2.0")
    assert release.main(["check-bump", "BASE", "TARGET"]) == 0
    assert "no version bump needed" in capsys.readouterr().out
    # The branch and the target made the same change.
    repo["head"] = pyproject("0.3.0")
    assert release.main(["check-bump", "BASE", "TARGET"]) == 0
    assert "no version bump needed" in capsys.readouterr().out


def test_needs_release(repo):
    assert release.main(["needs-release"]) == 0
    repo["head"] = pyproject("0.3.0")
    assert release.main(["needs-release"]) == release.ALREADY_RELEASED


def test_needs_release_refuses_a_version_below_the_latest_release(repo, capsys):
    repo["head"] = pyproject("0.2.5")
    status = release.main(["needs-release"])
    assert status not in (0, release.ALREADY_RELEASED)
    assert status == 1
    out = capsys.readouterr().out
    assert "0.2.5 is lower than the latest release on PyPI (0.3.0)" in out
    # An older release that is already on PyPI is still just "released".
    repo["head"] = pyproject("0.2.0")
    assert release.main(["needs-release"]) == release.ALREADY_RELEASED


def test_needs_release_finishes_a_partial_upload(repo, capsys):
    """An upload that stopped after one file is retried: `uv publish
    --check-url` skips the file already there."""
    repo["head"] = pyproject("0.4.0")
    repo["released"] = versions("0.2.0", "0.3.0", "0.4.0")
    repo["files"][Version("0.4.0")] = {"sdist"}
    assert release.main(["needs-release"]) == 0
    out = capsys.readouterr().out
    assert "0.4.0 is only partly on PyPI (sdist)" in out
    repo["files"][Version("0.4.0")] = {"sdist", "bdist_wheel"}
    assert release.main(["needs-release"]) == release.ALREADY_RELEASED


def test_unknown_command_prints_usage(capsys):
    assert release.main(["nope"]) == 2
    assert "check-bump" in capsys.readouterr().err


@pytest.fixture
def gitlab(repo, monkeypatch):
    calls = []
    existing = {}
    monkeypatch.setenv("CI_COMMIT_SHA", "abc123")

    def fake(method, path, body=None):
        calls.append((method, path, body))
        if method == "GET":
            if path in existing:
                return existing[path]
            raise http_error(404)
        return {}

    monkeypatch.setattr(release, "gitlab", fake)
    return calls, existing


def test_create_release_creates_tag_and_release(gitlab):
    calls, _ = gitlab
    assert release.main(["create-release"]) == 0
    method, path, body = calls[-1]
    assert (method, path) == ("POST", "/releases")
    assert body["tag_name"] == "v0.4.0" and body["ref"] == "abc123"
    assert "https://pypi.org/project/scrud-django/0.4.0/" in body["description"]


def test_create_release_is_idempotent_for_the_same_commit(gitlab):
    calls, existing = gitlab
    existing["/releases/v0.4.0"] = {"commit": {"id": "abc123"}}
    assert release.main(["create-release"]) == 0
    assert [c[0] for c in calls] == ["GET"]


def test_create_release_refuses_a_tag_on_another_commit(gitlab, capsys):
    calls, existing = gitlab
    existing["/releases/v0.4.0"] = {"commit": {"id": "def456"}}
    assert release.main(["create-release"]) == 1
    assert [c[0] for c in calls] == ["GET"]
    assert "already exists for def456" in capsys.readouterr().out


def test_create_release_refuses_a_bare_tag_on_another_commit(gitlab, capsys):
    calls, existing = gitlab
    existing["/repository/tags/v0.4.0"] = {"commit": {"id": "def456"}}
    assert release.main(["create-release"]) == 1
    assert [c[0] for c in calls] == ["GET", "GET"]
    assert "Tag v0.4.0 already exists on def456" in capsys.readouterr().out


def test_create_release_attaches_to_a_bare_tag_on_this_commit(gitlab):
    calls, existing = gitlab
    existing["/repository/tags/v0.4.0"] = {"commit": {"id": "abc123"}}
    assert release.main(["create-release"]) == 0
    method, path, body = calls[-1]
    assert (method, path, body["tag_name"]) == ("POST", "/releases", "v0.4.0")


def test_create_release_raises_other_api_errors(gitlab, monkeypatch):
    def fail(method, path, body=None):
        raise http_error(403)

    monkeypatch.setattr(release, "gitlab", fail)
    with pytest.raises(urllib.error.HTTPError) as info:
        release.main(["create-release"])
    assert info.value.code == 403


def test_script_runs_against_this_repository(monkeypatch):
    """The real pyproject.toml parses and the published part is computable."""
    monkeypatch.chdir(Path(__file__).resolve().parents[1])
    project = release.pyproject_at(None)
    assert release.version_of(project) >= Version("0.1.0")
    assert "version" not in release.published_part(project)["project"]


# --- network layer -----------------------------------------------------------


class FakeResponse(io.BytesIO):
    """What urlopen returns: readable by json.load and a context manager."""


@pytest.fixture
def urlopen(monkeypatch):
    """Replace urllib.request.urlopen; set `.outcome` to a body dict or an exception."""
    requests = []

    class Fake:
        outcome = {}

        def __call__(self, request, timeout=None):
            requests.append(request)
            if isinstance(self.outcome, Exception):
                raise self.outcome
            return FakeResponse(json.dumps(self.outcome).encode())

    fake = Fake()
    fake.requests = requests
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return fake


def test_released_versions_reads_versions_with_files(urlopen):
    urlopen.outcome = {"releases": {"0.3.0": [{"f": 1}], "0.4.0": [], "0.2.0": [{}]}}
    assert sorted(release.released_versions()) == [Version("0.2.0"), Version("0.3.0")]
    assert urlopen.requests == [f"{release.PYPI}/scrud-django/json"]


def test_released_versions_is_empty_for_an_unpublished_project(urlopen):
    urlopen.outcome = http_error(404)
    assert release.released_versions() == []


def test_released_versions_raises_server_errors(urlopen):
    urlopen.outcome = http_error(500)
    with pytest.raises(urllib.error.HTTPError) as info:
        release.released_versions()
    assert info.value.code == 500


def test_uploaded_packagetypes_of_a_published_version(urlopen):
    urlopen.outcome = {
        "urls": [
            {"filename": "scrud_django-0.3.0.tar.gz", "packagetype": "sdist"},
            {
                "filename": "scrud_django-0.3.0-py3-none-any.whl",
                "packagetype": "bdist_wheel",
            },
        ]
    }
    assert release.uploaded_packagetypes(Version("0.3.0")) == release.PACKAGETYPES
    assert release.PACKAGETYPES == {"sdist", "bdist_wheel"}
    assert urlopen.requests == [f"{release.PYPI}/scrud-django/0.3.0/json"]


def test_uploaded_packagetypes_of_a_partial_upload(urlopen):
    urlopen.outcome = {
        "urls": [{"filename": "scrud_django-0.3.0.tar.gz", "packagetype": "sdist"}]
    }
    assert release.uploaded_packagetypes(Version("0.3.0")) == {"sdist"}


def test_uploaded_packagetypes_of_a_missing_version(urlopen):
    urlopen.outcome = http_error(404)
    assert release.uploaded_packagetypes(Version("9.9.9")) == set()


def test_uploaded_packagetypes_raises_server_errors(urlopen):
    urlopen.outcome = http_error(500)
    with pytest.raises(urllib.error.HTTPError) as info:
        release.uploaded_packagetypes(Version("0.3.0"))
    assert info.value.code == 500


def test_gitlab_posts_json_with_the_job_token(urlopen, monkeypatch):
    monkeypatch.setenv("CI_API_V4_URL", "https://gitlab.example/api/v4")
    monkeypatch.setenv("CI_PROJECT_ID", "42")
    monkeypatch.setenv("CI_JOB_TOKEN", "secret")
    urlopen.outcome = {"ok": True}
    body = {"tag_name": "v0.4.0"}
    assert release.gitlab("POST", "/releases", body) == {"ok": True}
    (request,) = urlopen.requests
    assert request.full_url == "https://gitlab.example/api/v4/projects/42/releases"
    assert request.get_method() == "POST"
    assert request.get_header("Job-token") == "secret"
    assert request.get_header("Content-type") == "application/json"
    assert json.loads(request.data) == body


def test_gitlab_get_sends_no_body(urlopen, monkeypatch):
    monkeypatch.setenv("CI_API_V4_URL", "https://gitlab.example/api/v4")
    monkeypatch.setenv("CI_PROJECT_ID", "42")
    monkeypatch.setenv("CI_JOB_TOKEN", "secret")
    release.gitlab("GET", "/releases/v0.4.0")
    (request,) = urlopen.requests
    assert request.data is None
    assert request.get_method() == "GET"
