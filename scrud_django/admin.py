from django.contrib import admin

from .models import Resource, ResourceType

admin.site.register(Resource)
admin.site.register(ResourceType)
