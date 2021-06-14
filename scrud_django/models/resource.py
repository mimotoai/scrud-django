"""
This will support queries, such as:
    GET /resource_type/schema?uri=http://schema.org/person
    GET /resource_type/context?uri=http://schema.org/person

"""
import reversion

from scrud_django.mixins.resource_mixin import ResourceMixin


@reversion.register()
class Resource(ResourceMixin):
    pass
