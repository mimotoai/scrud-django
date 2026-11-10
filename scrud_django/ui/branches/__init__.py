"""UI branches: named sets of UI items (components, stylesheets) that override
their parent branch's. See ``docs/ui.md``."""

import re

from scrud_django.ui.types import UI_COMPONENT_TYPE_URI, UI_STYLESHEET_TYPE_URI

UI_BRANCH_TYPE_URI = "http://api.openteams.com/json-ld/UIBranch"
UI_BRANCH_SLUG = "ui-branches"
UI_BRANCH_VERSION_TYPE_URI = "http://api.openteams.com/json-ld/UIBranchVersion"
UI_BRANCH_VERSION_SLUG = "ui-branch-versions"
UI_PUSH_TYPE_URI = "http://api.openteams.com/json-ld/UIPush"
UI_PUSH_SLUG = "ui-pushes"
UI_CSS_TYPE_URI = "http://api.openteams.com/json-ld/UICss"
UI_CSS_SLUG = "ui-css"

# The published branch at the API URLs (prefix ""), in context "", with no
# parent: reserved (ruling R1).
MAIN = "main"

# A published branch's prefix, but main's ("", the API's URLs).
PREFIX = re.compile(r"^/(?:[a-z0-9][a-z0-9_-]*/)*$")

# The kinds of item a branch holds, by their resource type.
ITEM_KINDS = {"component": UI_COMPONENT_TYPE_URI, "stylesheet": UI_STYLESHEET_TYPE_URI}
