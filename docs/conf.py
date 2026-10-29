# Configuration file for the Sphinx documentation builder.
#
# This file only contains a selection of the most common options. For a full
# list see the documentation:
# https://www.sphinx-doc.org/en/master/usage/configuration.html

# -- Path setup --------------------------------------------------------------

# If extensions (or modules to document with autodoc) are in another directory,
# add these directories to sys.path here. If the directory is relative to the
# documentation root, use os.path.abspath to make it absolute, like shown here.

import os
import sys
from importlib.metadata import version as _package_version

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '_ext'))


# -- Project information -----------------------------------------------------

project = 'SCRUD Django'
copyright = '2020-2026, David Charboneau'
author = 'David Charboneau'

# The full version, including alpha/beta/rc tags, from the installed package
# (pyproject.toml is the single source).
release = _package_version('scrud-django')
# The short X.Y version.
version = '.'.join(release.split('.')[:2])


# -- General configuration ---------------------------------------------------

# Add any Sphinx extension module names here, as strings. They can be
# extensions coming with Sphinx (named 'sphinx.ext.*') or your custom
# ones.
extensions = [
    'myst_parser',
    'sphinx.ext.todo',
]

todo_include_todos = True

# Heading anchors, so that links such as follow-ups.md#empty-json-body
# resolve (see _ext/myst_slugs.py).
myst_heading_anchors = 2
myst_heading_slug_func = 'myst_slugs.follow_up_slug'

# Add any paths that contain templates here, relative to this directory.
templates_path = []

# List of patterns, relative to source directory, that match files and
# directories to ignore when looking for source files.
# This pattern also affects html_static_path and html_extra_path.
exclude_patterns = ['_build', 'superpowers']

pygments_style = 'sphinx'

# -- Options for HTML output -------------------------------------------------

# The theme to use for HTML and HTML Help pages.
html_theme = 'sphinx_rtd_theme'

# Add any paths that contain custom static files (such as style sheets) here,
# relative to this directory. They are copied after the builtin static files,
# so a file named "default.css" will overwrite the builtin "default.css".
html_static_path = []
