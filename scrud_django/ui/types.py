"""The built-in UI resource types: UI components, UI stylesheets, UI branches,
UI branch versions, UI pushes and UI CSS."""

import importlib.resources

from scrud_django.registration import ResourceTypeRegistration, register_or_defer

UI_COMPONENT_TYPE_URI = "http://api.openteams.com/json-ld/UIComponent"
UI_STYLESHEET_TYPE_URI = "http://api.openteams.com/json-ld/UIStylesheet"
UI_COMPONENT_SLUG = "ui-components"
UI_STYLESHEET_SLUG = "ui-stylesheets"

UI_COMPONENT_RESOURCE_TYPE_ = None
UI_COMPONENT_REGISTRATION_ = None
UI_STYLESHEET_RESOURCE_TYPE_ = None
UI_STYLESHEET_REGISTRATION_ = None
UI_BRANCH_RESOURCE_TYPE_ = None
UI_BRANCH_REGISTRATION_ = None
UI_BRANCH_VERSION_RESOURCE_TYPE_ = None
UI_BRANCH_VERSION_REGISTRATION_ = None
UI_PUSH_RESOURCE_TYPE_ = None
UI_PUSH_REGISTRATION_ = None
UI_CSS_RESOURCE_TYPE_ = None
UI_CSS_REGISTRATION_ = None

# The module-level caches above, for tests to forget.
REGISTRATION_CACHES = [
    "UI_COMPONENT_RESOURCE_TYPE_",
    "UI_COMPONENT_REGISTRATION_",
    "UI_STYLESHEET_RESOURCE_TYPE_",
    "UI_STYLESHEET_REGISTRATION_",
    "UI_BRANCH_RESOURCE_TYPE_",
    "UI_BRANCH_REGISTRATION_",
    "UI_BRANCH_VERSION_RESOURCE_TYPE_",
    "UI_BRANCH_VERSION_REGISTRATION_",
    "UI_PUSH_RESOURCE_TYPE_",
    "UI_PUSH_REGISTRATION_",
    "UI_CSS_RESOURCE_TYPE_",
    "UI_CSS_REGISTRATION_",
]


def _document(name):
    return importlib.resources.files("scrud_django.ui.documents").joinpath(name)


def ui_component_resource_type(force=False):
    global UI_COMPONENT_RESOURCE_TYPE_
    global UI_COMPONENT_REGISTRATION_
    if UI_COMPONENT_RESOURCE_TYPE_ is None or force:
        from scrud_django.ui.views import UIComponentViewSet

        UI_COMPONENT_REGISTRATION_ = ResourceTypeRegistration(
            resource_type_uri=UI_COMPONENT_TYPE_URI,
            revision="1",
            slug=UI_COMPONENT_SLUG,
            schema_func=lambda: _document("UIComponent-schema.json").read_text(),
            context_func=lambda: _document("UIComponent-ld.json").read_text(),
            viewset_class=UIComponentViewSet,
        )
        UI_COMPONENT_RESOURCE_TYPE_ = UI_COMPONENT_REGISTRATION_.resource_type
    return UI_COMPONENT_RESOURCE_TYPE_


def ui_stylesheet_resource_type(force=False):
    global UI_STYLESHEET_RESOURCE_TYPE_
    global UI_STYLESHEET_REGISTRATION_
    if UI_STYLESHEET_RESOURCE_TYPE_ is None or force:
        from scrud_django.ui.views import UIStylesheetViewSet

        UI_STYLESHEET_REGISTRATION_ = ResourceTypeRegistration(
            resource_type_uri=UI_STYLESHEET_TYPE_URI,
            revision="1",
            slug=UI_STYLESHEET_SLUG,
            schema_func=lambda: _document("UIStylesheet-schema.json").read_text(),
            context_func=lambda: _document("UIStylesheet-ld.json").read_text(),
            viewset_class=UIStylesheetViewSet,
        )
        UI_STYLESHEET_RESOURCE_TYPE_ = UI_STYLESHEET_REGISTRATION_.resource_type
    return UI_STYLESHEET_RESOURCE_TYPE_


def ui_branch_resource_type(force=False):
    # The branch constants are imported here: scrud_django.ui.branches
    # imports this module.
    from scrud_django.ui.branches import UI_BRANCH_SLUG, UI_BRANCH_TYPE_URI

    global UI_BRANCH_RESOURCE_TYPE_
    global UI_BRANCH_REGISTRATION_
    if UI_BRANCH_RESOURCE_TYPE_ is None or force:
        from scrud_django.ui.branches.views import UIBranchViewSet

        UI_BRANCH_REGISTRATION_ = ResourceTypeRegistration(
            resource_type_uri=UI_BRANCH_TYPE_URI,
            revision="1",
            slug=UI_BRANCH_SLUG,
            schema_func=lambda: _document("UIBranch-schema.json").read_text(),
            context_func=lambda: _document("UIBranch-ld.json").read_text(),
            viewset_class=UIBranchViewSet,
        )
        UI_BRANCH_RESOURCE_TYPE_ = UI_BRANCH_REGISTRATION_.resource_type
    return UI_BRANCH_RESOURCE_TYPE_


def ui_branch_version_resource_type(force=False):
    from scrud_django.ui.branches import (
        UI_BRANCH_VERSION_SLUG,
        UI_BRANCH_VERSION_TYPE_URI,
    )

    global UI_BRANCH_VERSION_RESOURCE_TYPE_
    global UI_BRANCH_VERSION_REGISTRATION_
    if UI_BRANCH_VERSION_RESOURCE_TYPE_ is None or force:
        from scrud_django.ui.branches.views import UIBranchVersionViewSet

        UI_BRANCH_VERSION_REGISTRATION_ = ResourceTypeRegistration(
            resource_type_uri=UI_BRANCH_VERSION_TYPE_URI,
            revision="1",
            slug=UI_BRANCH_VERSION_SLUG,
            schema_func=lambda: _document("UIBranchVersion-schema.json").read_text(),
            context_func=lambda: _document("UIBranchVersion-ld.json").read_text(),
            viewset_class=UIBranchVersionViewSet,
            # Versions are never changed: a revision history would repeat
            # each one.
            uses_reversion=False,
        )
        UI_BRANCH_VERSION_RESOURCE_TYPE_ = UI_BRANCH_VERSION_REGISTRATION_.resource_type
    return UI_BRANCH_VERSION_RESOURCE_TYPE_


def ui_push_resource_type(force=False):
    from scrud_django.ui.branches import UI_PUSH_SLUG, UI_PUSH_TYPE_URI

    global UI_PUSH_RESOURCE_TYPE_
    global UI_PUSH_REGISTRATION_
    if UI_PUSH_RESOURCE_TYPE_ is None or force:
        from scrud_django.ui.branches.views import UIPushViewSet

        UI_PUSH_REGISTRATION_ = ResourceTypeRegistration(
            resource_type_uri=UI_PUSH_TYPE_URI,
            revision="1",
            slug=UI_PUSH_SLUG,
            schema_func=lambda: _document("UIPush-schema.json").read_text(),
            context_func=lambda: _document("UIPush-ld.json").read_text(),
            viewset_class=UIPushViewSet,
        )
        UI_PUSH_RESOURCE_TYPE_ = UI_PUSH_REGISTRATION_.resource_type
    return UI_PUSH_RESOURCE_TYPE_


def ui_css_resource_type(force=False):
    from scrud_django.ui.branches import UI_CSS_SLUG, UI_CSS_TYPE_URI

    global UI_CSS_RESOURCE_TYPE_
    global UI_CSS_REGISTRATION_
    if UI_CSS_RESOURCE_TYPE_ is None or force:
        from scrud_django.ui.views import UICssViewSet

        UI_CSS_REGISTRATION_ = ResourceTypeRegistration(
            resource_type_uri=UI_CSS_TYPE_URI,
            revision="1",
            slug=UI_CSS_SLUG,
            schema_func=lambda: _document("UICss-schema.json").read_text(),
            context_func=lambda: _document("UICss-ld.json").read_text(),
            viewset_class=UICssViewSet,
        )
        UI_CSS_RESOURCE_TYPE_ = UI_CSS_REGISTRATION_.resource_type
    return UI_CSS_RESOURCE_TYPE_


def ui_resource_type_urls():
    """The routes of the UI resource types, each registered first, or its
    registration left pending on a database error (`register_or_defer`).
    ``scrud_django.urls`` serves them when ``scrud_django.ui`` is installed."""
    from scrud_django.ui.branches import (
        UI_BRANCH_SLUG,
        UI_BRANCH_TYPE_URI,
        UI_BRANCH_VERSION_SLUG,
        UI_BRANCH_VERSION_TYPE_URI,
        UI_CSS_SLUG,
        UI_CSS_TYPE_URI,
        UI_PUSH_SLUG,
        UI_PUSH_TYPE_URI,
    )
    from scrud_django.ui.branches.views import (
        UIBranchVersionViewSet,
        UIBranchViewSet,
        UIPushViewSet,
    )
    from scrud_django.ui.views import (
        UIComponentViewSet,
        UICssViewSet,
        UIStylesheetViewSet,
    )

    def registered(register_type, registration_name):
        def register(force=False):
            register_type(force=force)
            return globals()[registration_name]

        return register

    ui_types = (
        (
            ui_component_resource_type,
            "UI_COMPONENT_REGISTRATION_",
            UI_COMPONENT_TYPE_URI,
            UI_COMPONENT_SLUG,
            UIComponentViewSet,
        ),
        (
            ui_stylesheet_resource_type,
            "UI_STYLESHEET_REGISTRATION_",
            UI_STYLESHEET_TYPE_URI,
            UI_STYLESHEET_SLUG,
            UIStylesheetViewSet,
        ),
        (
            ui_branch_resource_type,
            "UI_BRANCH_REGISTRATION_",
            UI_BRANCH_TYPE_URI,
            UI_BRANCH_SLUG,
            UIBranchViewSet,
        ),
        (
            ui_branch_version_resource_type,
            "UI_BRANCH_VERSION_REGISTRATION_",
            UI_BRANCH_VERSION_TYPE_URI,
            UI_BRANCH_VERSION_SLUG,
            UIBranchVersionViewSet,
        ),
        (
            ui_push_resource_type,
            "UI_PUSH_REGISTRATION_",
            UI_PUSH_TYPE_URI,
            UI_PUSH_SLUG,
            UIPushViewSet,
        ),
        (
            ui_css_resource_type,
            "UI_CSS_REGISTRATION_",
            UI_CSS_TYPE_URI,
            UI_CSS_SLUG,
            UICssViewSet,
        ),
    )
    urls = []
    for register_type, registration_name, type_uri, slug, viewset_class in ui_types:
        register = registered(register_type, registration_name)
        urls.extend(
            register_or_defer(
                register,
                type_uri,
                slug,
                viewset_class,
                retry=lambda register=register: register(force=True),
            )
        )
    return urls
