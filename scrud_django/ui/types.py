"""The built-in UI resource types: UI components and UI stylesheets."""

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

# The module-level caches above, for tests to forget.
REGISTRATION_CACHES = [
    "UI_COMPONENT_RESOURCE_TYPE_",
    "UI_COMPONENT_REGISTRATION_",
    "UI_STYLESHEET_RESOURCE_TYPE_",
    "UI_STYLESHEET_REGISTRATION_",
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


def ui_resource_type_urls():
    """The routes of the UI resource types, each registered first, or its
    registration left pending on a database error (`register_or_defer`).
    ``scrud_django.urls`` serves them when ``scrud_django.ui`` is installed."""
    from scrud_django.ui.views import UIComponentViewSet, UIStylesheetViewSet

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
