"""scrud-django's models in the Django admin, for superusers only.

A write in the admin bypasses scoped-rbac's checks, the workflow and JSON
Schema validation, and shows every context: staff who are not superusers do
not see these models, whatever Django model permissions they hold.
``ResourceContext`` is not registered: its rows follow each resource's
content.
"""

from django.contrib import admin

from .models import Resource, ResourceType


class SuperuserOnlyAdmin(admin.ModelAdmin):
    """A ``ModelAdmin`` that only an active superuser may see or use."""

    @staticmethod
    def is_superuser(request):
        user = request.user
        return bool(user.is_active and user.is_superuser)

    def has_module_permission(self, request):
        return self.is_superuser(request)

    def has_view_permission(self, request, obj=None):
        return self.is_superuser(request)

    def has_add_permission(self, request):
        return self.is_superuser(request)

    def has_change_permission(self, request, obj=None):
        return self.is_superuser(request)

    def has_delete_permission(self, request, obj=None):
        return self.is_superuser(request)


admin.site.register(Resource, SuperuserOnlyAdmin)
admin.site.register(ResourceType, SuperuserOnlyAdmin)
