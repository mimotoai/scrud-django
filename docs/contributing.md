# Contributing Guide


Contributions are welcome, and they are greatly appreciated! Every little bit
helps, and credit will always be given.

You can contribute in many ways:

## Types of Contributions

### Report Bugs

Report bugs at https://gitlab.com/dcharbon/scrud-django/-/issues.

If you are reporting a bug, please include:

* Your operating system name and version.
* Any details about your local setup that might be helpful in troubleshooting.
* Detailed steps to reproduce the bug.

### Fix Bugs

Look through the GitLab issues for bugs. Anything tagged with "bug" and "help
wanted" is open to whoever wants to implement it.

### Implement Features

Look through the GitLab issues for features. Anything tagged with "enhancement"
and "help wanted" is open to whoever wants to implement it.

### Write Documentation

scrud-django could always use more documentation, whether as part of the
official scrud-django docs, in docstrings, or even on the web in blog posts,
articles, and such.

### Submit Feedback

The best way to send feedback is to file an issue at https://gitlab.com/dcharbon/scrud-django/-/issues.

If you are proposing a feature:

* Explain in detail how it would work.
* Keep the scope as narrow as possible, to make it easier to implement.
* Remember that this is a volunteer-driven project, and that contributions
  are welcome :)

### Get Started!

Ready to contribute? Here's how to set up `scrud-django` for local development.

1. Fork the `scrud-django` repo on GitLab.
2. Clone your fork locally:
```{sourceCode} console
$ git clone git@gitlab.com:your_name_here/scrud-django.git
```
3. Install your local copy with [uv](https://docs.astral.sh/uv/):
```{sourceCode} console
$ cd scrud-django
$ uv sync
$ uv run pre-commit install
```
4. Create a branch for local development:
```{sourceCode} console
$ git checkout -b name-of-your-bugfix-or-feature
```
5. Run the checks before you commit:
```{sourceCode} console
$ make lint
$ make test
$ make migrations-check
```
   The suite runs on in-memory SQLite. To run it on PostgreSQL, as CI's
   `test-postgres` job does (some tests run only there), point the test
   settings at a server with `SCRUD_TEST_PG_HOST` (and `SCRUD_TEST_PG_PORT`,
   `SCRUD_TEST_PG_USER`, `SCRUD_TEST_PG_PASSWORD`, `SCRUD_TEST_PG_NAME` if the
   defaults 5432, postgres, none and scrud don't fit; see `tests/settings.py`):
```{sourceCode} console
$ docker run -d --name scrud-pg -p 5432:5432 -e POSTGRES_HOST_AUTH_METHOD=trust postgres:14
$ SCRUD_TEST_PG_HOST=localhost uv run pytest
```
   Tests must not depend on the order they run in. CI also runs the suite in
   reverse order:
```{sourceCode} console
$ uv run pytest -q --reverse
```
6. Commit your changes and push your branch to GitLab:
```{sourceCode} console
$ git add .
$ git commit -m "Your detailed description of your changes."
$ git push origin name-of-your-bugfix-or-feature
```
7. Submit a merge request through the GitLab website.

Merge Request Guidelines
------------------------

Before you submit a merge request, check that it meets these guidelines:

1. The merge request should include tests.
2. If the merge request adds functionality, the docs should be updated. Put your new functionality into a function with a docstring.
3. The change should work for Python 3.11, 3.12 and 3.13, on SQLite and
   PostgreSQL.
4. Merge requests target `develop` and need no version bump; the version is
   set when releasing (see "Releasing" below).

### Releasing

Releases are published to PyPI from CI, from the release branch `main`. Work
lands on `develop`, the default branch. To release, set the version on
`develop`, in a feature merge request or a small one of its own:

```{sourceCode} console
$ uv version --bump minor   # or: uv version 1.0.0
```

and commit `pyproject.toml` and `uv.lock`. Then open a merge request from
`develop` into `main`. Its `version-check` job fails it if it changes the
package and its version is not above `main`'s and every release on PyPI; it
checks any release merge request that changes the version, even one that
changes nothing else. Merge requests into `develop` are not version-checked.
When the release merge request lands on `main`, the `publish` job tags
`vX.Y.Z`, creates a GitLab release and uploads to PyPI, but only if that
version is not on PyPI yet; a merge that leaves the version alone publishes
nothing, and a version lower than the latest on PyPI fails the job. `main` is
protected: nobody pushes to it directly, and only Maintainers merge. If an upload
stops part way, retry the job: it uploads the files that are missing. The
`package` job builds the distribution on every pipeline, checks that its README
renders, and installs the wheel in a clean venv to check that it holds every
file under `scrud_django/` and imports. The upload uses PyPI trusted
publishing: PyPI accepts it only from the `publish` job in this project's
`.gitlab-ci.yml`, deploying to the `pypi` environment, and no PyPI token is
stored in GitLab. Never run `uv publish` locally. See `scripts/release.py`
for the checks.


### Code of Conduct

scrud-django is governed by the
[NumFOCUS code of conduct](https://numfocus.org/code-of-conduct),
which in a short version is:

- Be kind to others. Do not insult or put down others. Behave professionally. Remember that harassment and sexist, racist, or exclusionary jokes are not appropriate for NumFOCUS.
- All communication should be appropriate for a professional audience including people of many different backgrounds. Sexual language and imagery is not appropriate.
- NumFOCUS is dedicated to providing a harassment-free community for everyone, regardless of gender, sexual orientation, gender identity and expression, disability, physical appearance, body size, race, or religion. We do not tolerate harassment of community members in any form.
- Thank you for helping make this a welcoming, friendly community for all.
