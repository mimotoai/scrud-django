"""The built-in UI resource types: UI components, UI stylesheets, UI branches,
UI branch versions, UI pushes, UI CSS and UI JS."""

import functools
import importlib.resources

from scrud_django.registration import TypeSpec, json_resource_type_spec

UI_COMPONENT_TYPE_URI = "http://api.openteams.com/json-ld/UIComponent"
UI_STYLESHEET_TYPE_URI = "http://api.openteams.com/json-ld/UIStylesheet"
UI_COMPONENT_SLUG = "ui-components"
UI_STYLESHEET_SLUG = "ui-stylesheets"


def _document(name):
    return importlib.resources.files("scrud_django.ui.documents").joinpath(name)


def _documents(name):
    """The `json_resource_type_spec` arguments reading the schema and the
    JSON-LD context of the UI type `name` from ``scrud_django.ui.documents``."""
    return {
        "schema_func": lambda: _document(f"{name}-schema.json").read_text(),
        "context_func": lambda: _document(f"{name}-ld.json").read_text(),
    }


def ui_type_specs() -> list[TypeSpec]:
    """The `TypeSpec`s of the UI resource types (UI components, UI
    stylesheets, UI branches, UI branch versions, UI pushes, UI CSS and UI
    JS), each with its hooks (`scrud_django.ui.hooks.UI_TYPE_OPTIONS`: its
    write and read behaviour, which the core runs for every front end): what
    the ninja ``api()`` serves when ``scrud_django.ui`` is installed, and
    what `ui_resource_type_urls` registers with the DRF routes. Built afresh
    on each call."""
    # Imported here: scrud_django.ui.branches and scrud_django.ui.hooks
    # import this module.
    from scrud_django.ui.branches import (
        UI_BRANCH_SLUG,
        UI_BRANCH_TYPE_URI,
        UI_BRANCH_VERSION_SLUG,
        UI_BRANCH_VERSION_TYPE_URI,
        UI_CSS_SLUG,
        UI_CSS_TYPE_URI,
        UI_JS_SLUG,
        UI_JS_TYPE_URI,
        UI_PUSH_SLUG,
        UI_PUSH_TYPE_URI,
    )
    from scrud_django.ui.hooks import UI_TYPE_OPTIONS

    ui_types = (
        (UI_COMPONENT_TYPE_URI, UI_COMPONENT_SLUG, "UIComponent", {}),
        (UI_STYLESHEET_TYPE_URI, UI_STYLESHEET_SLUG, "UIStylesheet", {}),
        (UI_BRANCH_TYPE_URI, UI_BRANCH_SLUG, "UIBranch", {}),
        # Versions are never changed: a revision history would repeat each
        # one.
        (
            UI_BRANCH_VERSION_TYPE_URI,
            UI_BRANCH_VERSION_SLUG,
            "UIBranchVersion",
            {"uses_reversion": False},
        ),
        (UI_PUSH_TYPE_URI, UI_PUSH_SLUG, "UIPush", {}),
        (UI_CSS_TYPE_URI, UI_CSS_SLUG, "UICss", {}),
        (UI_JS_TYPE_URI, UI_JS_SLUG, "UIJs", {}),
    )
    return [
        json_resource_type_spec(
            type_uri,
            slug,
            revision="1",
            options=UI_TYPE_OPTIONS[slug],
            **_documents(name),
            **storage,
        )
        for type_uri, slug, name, storage in ui_types
    ]


def ui_resource_type_urls():
    """The DRF routes of the UI resource types (`ui_type_specs`, each served
    by its viewset, `scrud_django.drf.ui`), each type registered first
    (`TypeSpec.register`: its hooks recorded, whatever the database says, then its row stored, or its
    registration left pending on a database error, `register_or_defer`).
    ``scrud_django.drf.urls`` serves them when ``scrud_django.ui`` is
    installed."""
    from scrud_django.drf.ui import (
        UIBranchVersionViewSet,
        UIBranchViewSet,
        UIComponentViewSet,
        UICssViewSet,
        UIJsViewSet,
        UIPushViewSet,
        UIStylesheetViewSet,
    )
    from scrud_django.registration import resource_type_urls
    from scrud_django.ui.branches import (
        UI_BRANCH_SLUG,
        UI_BRANCH_VERSION_SLUG,
        UI_CSS_SLUG,
        UI_JS_SLUG,
        UI_PUSH_SLUG,
    )

    viewsets = {
        UI_COMPONENT_SLUG: UIComponentViewSet,
        UI_STYLESHEET_SLUG: UIStylesheetViewSet,
        UI_BRANCH_SLUG: UIBranchViewSet,
        UI_BRANCH_VERSION_SLUG: UIBranchVersionViewSet,
        UI_PUSH_SLUG: UIPushViewSet,
        UI_CSS_SLUG: UICssViewSet,
        UI_JS_SLUG: UIJsViewSet,
    }
    urls = []
    for spec in ui_type_specs():
        urls.extend(
            spec.register(
                urls_func=functools.partial(
                    resource_type_urls, viewset_class=viewsets[spec.slug]
                )
            )
        )
    return urls
