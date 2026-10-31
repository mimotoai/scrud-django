Installation
============

Requirements: Python 3.11 or newer and Django 5.2.

Stable release
--------------

```{sourceCode} console
$ uv add scrud-django
```

or, with pip:

```{sourceCode} console
$ pip install scrud-django
```

For haystack search support, install the `search` extra:

```{sourceCode} console
$ uv add "scrud-django[search]"
```

From sources
------------

The sources are hosted on [GitLab](https://gitlab.com/dcharbon/scrud-django).

```{sourceCode} console
$ git clone https://gitlab.com/dcharbon/scrud-django.git
$ cd scrud-django
$ uv sync
```
