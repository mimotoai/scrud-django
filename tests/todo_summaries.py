"""The DRF viewset of ``todo-summaries`` (`tests.resource_types.json_resource_types`)."""

from scrud_django.core.workflow import may_read_in_context
from scrud_django.drf.computed import ComputedResourceViewSet
from scrud_django.mixins.resource_mixin import rbac_contexts_of
from scrud_django.models import Resource

from .resource_types import TODO_LIST_TYPE_URI


class TodoSummaryViewSet(ComputedResourceViewSet):
    """The titles of the to-do lists in the context `key` that the requesting
    user may read: computed, never stored."""

    def compute(self, request, key):
        titles = []
        lists = Resource.objects.filter(resource_type__type_uri=TODO_LIST_TYPE_URI)
        for stored in lists:
            content = stored.content
            if content.get("rbac_context", "") != key:
                continue
            if may_read_in_context(
                request, TODO_LIST_TYPE_URI, content, rbac_contexts_of(content)
            ):
                titles.append(content["title"])
        return {"context": key, "titles": sorted(titles)}
