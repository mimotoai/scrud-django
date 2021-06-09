from django.db import models
from django.utils.translation import gettext_lazy as _
from django_jsonfield_backport.models import JSONField

from scrud_django.models.resource_type import ResourceType


class ResourceMixin(models.Model):
    # The actual JSON content for this resource
    content = JSONField()
    resource_type = models.ForeignKey(
        ResourceType,
        on_delete=models.PROTECT,
        verbose_name=_('resource type'),
        related_name='resource_type',
    )
    modified_at = models.DateTimeField()
    etag = models.CharField(max_length=40)

    @property
    def rbac_context(self):
        return self.content.get("rbac_context", None)

    class Meta:
        abstract = True
