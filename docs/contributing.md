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
3. The change should work for Python 3.11, 3.12 and 3.13.


### Code of Conduct

scrud-django is governed by the
[NumFOCUS code of conduct](https://numfocus.org/code-of-conduct),
which in a short version is:

- Be kind to others. Do not insult or put down others. Behave professionally. Remember that harassment and sexist, racist, or exclusionary jokes are not appropriate for NumFOCUS.
- All communication should be appropriate for a professional audience including people of many different backgrounds. Sexual language and imagery is not appropriate.
- NumFOCUS is dedicated to providing a harassment-free community for everyone, regardless of gender, sexual orientation, gender identity and expression, disability, physical appearance, body size, race, or religion. We do not tolerate harassment of community members in any form.
- Thank you for helping make this a welcoming, friendly community for all.
