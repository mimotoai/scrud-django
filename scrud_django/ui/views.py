"""Viewsets of the UI resource types, with their write-time checks."""

from scrudful.rest_framework import scrudful_viewset

from scrud_django.exceptions import (
    UIComponentNameTakenError,
    UISelectorInvalidError,
    UIStylesheetNameTakenError,
    UITemplateInvalidError,
)
from scrud_django.models import Resource
from scrud_django.registration import write_db
from scrud_django.ui.engines import TemplateCompileError, compile_template
from scrud_django.ui.selectors import SelectorError, parse_selector
from scrud_django.views import ResourceViewSet


class UIContentValidationMixin:
    """Checks the content of a UI resource before it is saved: `validate_content`
    runs ahead of `create_resource` and `update` saving. (Later checks of the
    content, such as template compilation, belong in `validate_content`.)"""

    def validate_content(self, content, instance=None):
        """Raise a ``ScrudException`` when `content` (the request body) is
        refused. `instance` is the resource an update replaces."""

    def create_resource(self, request):
        self.validate_content(request.data)
        return super().create_resource(request)

    def update(self, request, slug: str):
        instance = self.get_instance(request, slug)
        self.validate_content(request.data, instance=instance)
        return super().update(request, slug)

    def other_resources(self, instance=None):
        """The stored resources of this view's type, but `instance`."""
        db = self.locked_write_db or write_db()
        resources = Resource.objects.using(db).filter(
            resource_type=self.get_resource_type()
        )
        if instance is not None:
            resources = resources.exclude(pk=instance.pk)
        return resources


@scrudful_viewset
class UIComponentViewSet(UIContentValidationMixin, ResourceViewSet):
    def validate_content(self, content, instance=None):
        super().validate_content(content, instance=instance)
        name = content.get("name") if isinstance(content, dict) else None
        if name is not None and self.other_resources(instance).filter(
            content__name=name
        ):
            raise UIComponentNameTakenError(name)
        if isinstance(content, dict) and "template" in content:
            try:
                compile_template(content.get("engine"), content["template"])
            except TemplateCompileError as error:
                raise UITemplateInvalidError(error) from error


@scrudful_viewset
class UIStylesheetViewSet(UIContentValidationMixin, ResourceViewSet):
    def validate_content(self, content, instance=None):
        super().validate_content(content, instance=instance)
        if not isinstance(content, dict):
            return
        self.validate_selectors(content)
        name = content.get("name")
        context = content.get("rbac_context", "")
        if name is None or not isinstance(context, str):
            return
        for other in self.other_resources(instance).filter(content__name=name):
            if other.content.get("rbac_context", "") == context:
                raise UIStylesheetNameTakenError(name, context)

    def validate_selectors(self, content):
        """Parse every rule's selector with the stylesheet's own prefixes."""
        prefixes = content.get("prefixes")
        if not isinstance(prefixes, dict):
            prefixes = {}
        for rule in content.get("rules") or []:
            select = rule.get("select") if isinstance(rule, dict) else None
            if not isinstance(select, str):
                continue
            try:
                parse_selector(select, prefixes)
            except SelectorError as error:
                raise UISelectorInvalidError(error) from error
