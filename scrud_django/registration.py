import importlib.resources
import json
import logging
from uuid import uuid4

import reversion
from django.conf import settings
from django.db import router, transaction
from django.db.utils import OperationalError, ProgrammingError
from django.shortcuts import get_list_or_404, get_object_or_404
from django.urls import path
from django.utils import timezone

from scrud_django import services
from scrud_django.models import Resource, ResourceType
from scrud_django.workflow import WORKFLOW_POLICY_TYPE_URI, lock_workflow_policy_type


def write_db(instance=None):
    """The database `ResourceRegistration` writes resources to
    (``router.db_for_write(Resource)``, with `instance` as the router's
    ``instance`` hint when there is one): its transactions open there, and the
    resource, its ``ResourceContext`` rows and its ``ResourceRemoval`` stamps
    are all saved there, so that the ``WorkflowPolicy`` lock and the writes
    share one transaction. Route ``Resource``,
    ``ResourceType``, ``ResourceContext`` and ``ResourceRemoval`` writes to
    one alias (the ``scrud_django.E004`` check)."""
    if instance is None:
        return router.db_for_write(Resource)
    return router.db_for_write(Resource, instance=instance)


def lock_if_workflow_policy(resource_type, using):
    """When `resource_type` is ``WorkflowPolicy``, take
    `lock_workflow_policy_type` on database `using`. Call it first inside the
    write's transaction on that same database (``transaction.atomic(using=
    write_db())``; ``select_for_update`` outside a transaction is an error on
    PostgreSQL). `ResourceRegistration`'s writes, and so the API's, then lock
    the type row before they write the ``Resource`` row, in that one order: a
    PUT and a DELETE of the same policy cannot deadlock."""
    if resource_type.type_uri == WORKFLOW_POLICY_TYPE_URI:
        lock_workflow_policy_type(using)


def type_db():
    """The database `ResourceType` rows are written to
    (``router.db_for_write(ResourceType)``). A write reads its resource type
    there, never through ``db_for_read`` (a replica may be stale)."""
    return router.db_for_write(ResourceType)


def get_resource_type_for(slug, using=None):
    """The latest revision of the resource type `slug` (404 if none), read on
    database `using` (``None``: as the router reads it)."""
    queryset = ResourceType.objects.order_by("-revision")
    if using is not None:
        queryset = queryset.using(using)
    return get_list_or_404(queryset, slug=slug)[0]


def get_locked_resource(resource_type, slug, using):
    """The `Resource` of `resource_type` with primary key `slug` (404 if
    none), read on database `using` with its row locked
    (``select_for_update``) until the caller's transaction there ends. Take
    `lock_if_workflow_policy` first."""
    return get_object_or_404(
        Resource.objects.using(using).select_for_update(),
        resource_type=resource_type,
        pk=int(slug),
    )


class ResourceTypeRegistration:
    def __init__(
        self,
        resource_type_uri: str,
        revision: str = '1',
        schema_resource: Resource = None,
        schema_func: callable = None,
        schema_uri: str = None,
        context_resource: Resource = None,
        context_func: callable = None,
        context_uri: str = None,
        indexing_policy_resource: Resource = None,
        indexing_policy_func: callable = None,
        slug: str = None,
        viewset_class: object = None,
        uses_reversion: bool = True,
        register_service: bool = True,
    ):
        """
        Register resource type model.
        """
        from scrud_django import views

        search = ResourceType.objects.filter(type_uri=resource_type_uri)

        self.viewset_class = viewset_class or views.ResourceViewSet

        if search:
            self.resource_type = search[0]
            # update everything if settings.DEBUG (in development)
            if settings.DEBUG:
                self.resource_type = ResourceType(
                    type_uri=resource_type_uri,
                    schema_uri=schema_uri,
                    context_uri=context_uri,
                    slug=slug,
                    etag=uuid4().hex,
                    modified_at=timezone.now(),
                    uses_reversion=uses_reversion,
                    revision=revision,
                )
                self.resource_type.pk = search[0].pk

        else:
            resource_type = ResourceType(
                type_uri=resource_type_uri,
                schema_uri=schema_uri,
                context_uri=context_uri,
                slug=slug,
                etag=uuid4().hex,
                modified_at=timezone.now(),
                uses_reversion=uses_reversion,
                revision=revision,
            )
            self.resource_type = resource_type

        if schema_resource is None and schema_func:
            self.resource_type.schema = register_json_schema(
                schema_func, self.resource_type.schema
            )
        else:
            self.resource_type.schema = schema_resource

        if context_resource is None and context_func:
            self.resource_type.context = register_json_ld_context(
                context_func, self.resource_type.context
            )
        else:
            self.resource_type.context = context_resource

        if indexing_policy_resource is None and indexing_policy_func:
            self.resource_type.indexing_policy = register_indexing_policy(
                indexing_policy_func, self.resource_type.indexing_policy
            )
        else:
            self.resource_type.indexing_policy = indexing_policy_resource
        self.resource_type.uses_reversion = uses_reversion
        with reversion.create_revision():
            self.resource_type.save()
            reversion.set_comment(
                'Update resourcetype' if search else 'Add resourcetype'
            )

        # Register with service catalog
        if register_service:
            services.add_service(self.resource_type.type_uri, self.resource_type.slug)
        self.register_urls()

    def register_urls(self):
        """Register resource type url."""
        scrud_list = self.viewset_class.as_view(
            {
                'get': 'list',
                'head': 'list',
                'post': 'create',
            },  # noqa: E231
            resource_type_name=self.resource_type.slug,
        )
        scrud_detail = self.viewset_class.as_view(
            {
                'get': 'retrieve',
                'head': 'retrieve',
                'put': 'update',
                'delete': 'destroy',
            },
            resource_type_name=self.resource_type.slug,
        )
        self.urls = [
            path(
                f'{self.resource_type.slug}/',
                scrud_list,
                # name=f'{resource_type.slug}-list',
                name=self.resource_type.route_name_list(),
            ),
            path(
                f'{self.resource_type.slug}/<str:slug>/',
                scrud_detail,
                # name=f'{resource_type.slug}-detail',
                name=self.resource_type.route_name_detail(),
            ),
        ]


class ResourceRegistration:
    @staticmethod
    def register(content: str, register_type: str) -> Resource:
        """
        Register resource content for the given resource type name.

        Parameters
        ----------
        content : dict
        register_type : str

        Returns
        -------
        Resource

        """
        resource_type = get_resource_type_for(register_type)
        resource = Resource(content=content, resource_type=resource_type)
        resource = ResourceRegistration.register_instance(resource)
        return resource

    @staticmethod
    def register_instance(resource: Resource) -> Resource:
        """
        Register the resource for the given resource type.

        Parameters
        ----------
        resource : Resource
        resource_type : ResourceType

        Returns
        -------
        Resource

        """
        db = write_db(resource)
        with transaction.atomic(using=db):
            lock_if_workflow_policy(resource.resource_type, db)
            resource.etag = uuid4().hex
            resource.modified_at = timezone.now()
            if resource.resource_type.uses_reversion:
                with reversion.create_revision():
                    resource.save(using=db)
                    reversion.set_comment('Add Resource')
            else:
                resource.save(using=db)
        return resource

    @staticmethod
    def update(content: str, register_type: str, slug: str) -> Resource:
        """
        Update resource content for the given resource type name and slug.

        In one transaction on `write_db`: take `lock_if_workflow_policy`, read
        the resource with its row locked (`get_locked_resource`) and write it
        (`update_locked`). Nothing is read through the read router.

        Parameters
        ----------
        content : dict
        register_type : str
        slug : str

        Returns
        -------
        Resource

        """
        db = write_db()
        with transaction.atomic(using=db):
            resource_type = get_resource_type_for(register_type, using=type_db())
            lock_if_workflow_policy(resource_type, db)
            # note: for now slug is used as ID
            resource = get_locked_resource(resource_type, slug, db)
            return ResourceRegistration.update_locked(
                resource, content, resource_type=resource_type, using=db
            )

    @staticmethod
    def update_locked(
        resource: Resource, content, *, resource_type: ResourceType, using: str
    ) -> Resource:
        """
        Write `content` to `resource`, a row the caller read on database
        `using`, inside its transaction there, after it took
        `lock_if_workflow_policy` and then locked the row
        (`get_locked_resource`), in that order. `resource_type` is the
        resource's type (its row is not written). The instance is saved as it is:
        the row is not read again, so what the caller checked is what is
        written over. `update` and the API's PUT use it.
        """
        resource.resource_type = resource_type
        resource.content = content
        resource.etag = uuid4().hex
        resource.modified_at = timezone.now()
        if resource_type.uses_reversion:
            with reversion.create_revision():
                resource.save(using=using)
                reversion.set_comment('Change Resource')
        else:
            resource.save(using=using)
        return resource

    @staticmethod
    def delete(register_type: str, slug: str):
        """
        Delete resource for the given resource type name and slug.

        In one transaction on `write_db`: take `lock_if_workflow_policy`, read
        the resource with its row locked (`get_locked_resource`) and delete it
        (`delete_locked`).

        Parameters
        ----------
        register_type : str
        slug : str

        """
        db = write_db()
        with transaction.atomic(using=db):
            resource_type = get_resource_type_for(register_type, using=type_db())
            lock_if_workflow_policy(resource_type, db)
            # note: for now slug is used as ID
            resource = get_locked_resource(resource_type, slug, db)
            ResourceRegistration.delete_locked(
                resource, resource_type=resource_type, using=db
            )

    @staticmethod
    def delete_locked(resource: Resource, *, resource_type: ResourceType, using: str):
        """
        Delete `resource`, read and locked as for `update_locked`.
        `resource_type` is its type (its row is not written). `delete` and the
        API's DELETE use it.
        """
        resource.delete(using=using)


JSON_SCHEMA_REGISTRATION_ = None
JSON_SCHEMA_RESOURCE_TYPE_ = None
JSON_LD_RESOURCE_TYPE_ = None
JSON_LD_REGISTRATION_ = None
INDEXING_POLICY_RESOURCE_TYPE_ = None
INDEXING_POLICY_REGISTRATION_ = None
WORKFLOW_POLICY_RESOURCE_TYPE_ = None
WORKFLOW_POLICY_REGISTRATION_ = None


def register_json_schema_resource_type():
    global JSON_SCHEMA_RESOURCE_TYPE_
    global JSON_SCHEMA_REGISTRATION_
    if JSON_SCHEMA_RESOURCE_TYPE_ is None:
        JSON_SCHEMA_REGISTRATION_ = ResourceTypeRegistration(
            resource_type_uri="http://json-schema.org/draft-04/schema",
            revision="1",
            schema_uri="http://json-schema.org/draft-04/schema",
            slug="json-schema",
        )
        JSON_SCHEMA_RESOURCE_TYPE_ = JSON_SCHEMA_REGISTRATION_.resource_type
    return JSON_SCHEMA_RESOURCE_TYPE_


def register_json_ld_resource_type():
    global JSON_LD_RESOURCE_TYPE_
    global JSON_LD_REGISTRATION_
    if JSON_LD_RESOURCE_TYPE_ is None:
        JSON_LD_REGISTRATION_ = ResourceTypeRegistration(
            resource_type_uri="http://www.w3.org/ns/json-ld#context",
            revision="1",
            slug="json-ld",
        )
        JSON_LD_RESOURCE_TYPE_ = JSON_LD_REGISTRATION_.resource_type
    return JSON_LD_RESOURCE_TYPE_


def register_indexing_policy_resource_type():
    global INDEXING_POLICY_RESOURCE_TYPE_
    global INDEXING_POLICY_REGISTRATION_
    if INDEXING_POLICY_RESOURCE_TYPE_ is None:
        INDEXING_POLICY_REGISTRATION_ = ResourceTypeRegistration(
            resource_type_uri="http://api.openteams.com/json-ld/IndexingPolicy",
            revision="1",
            slug="indexing-policies",
            schema_func=lambda: (
                importlib.resources.files("scrud_django.static")
                .joinpath("IndexingPolicy-schema.json")
                .read_text()
            ),
            context_func=lambda: (
                importlib.resources.files("scrud_django.static")
                .joinpath("IndexingPolicy-ld.json")
                .read_text()
            ),
        )
        INDEXING_POLICY_RESOURCE_TYPE_ = INDEXING_POLICY_REGISTRATION_.resource_type
    return INDEXING_POLICY_RESOURCE_TYPE_


def workflow_policy_resource_type():
    global WORKFLOW_POLICY_RESOURCE_TYPE_
    global WORKFLOW_POLICY_REGISTRATION_
    if WORKFLOW_POLICY_RESOURCE_TYPE_ is None:
        WORKFLOW_POLICY_REGISTRATION_ = ResourceTypeRegistration(
            resource_type_uri="http://api.openteams.com/json-ld/WorkflowPolicy",
            revision="1",
            slug="workflow-policies",
            schema_func=lambda: (
                importlib.resources.files("scrud_django.static")
                .joinpath("WorkflowPolicy-schema.json")
                .read_text()
            ),
            context_func=lambda: (
                importlib.resources.files("scrud_django.static")
                .joinpath("WorkflowPolicy-ld.json")
                .read_text()
            ),
        )
        WORKFLOW_POLICY_RESOURCE_TYPE_ = WORKFLOW_POLICY_REGISTRATION_.resource_type
    return WORKFLOW_POLICY_RESOURCE_TYPE_


def register_json_file(file_contents_func, previous_revision, resource_type):
    contents = file_contents_func()
    if type(contents) is str:
        contents = json.loads(contents)
    if previous_revision:
        resource = ResourceRegistration.update(
            contents, resource_type.slug, previous_revision.id
        )
    else:
        resource = ResourceRegistration.register(contents, resource_type.slug)
    return resource


def register_json_schema(schema_func, previous_revision):
    resource_type = register_json_schema_resource_type()
    return register_json_file(schema_func, previous_revision, resource_type)


def register_json_ld_context(context_func, previous_revision):
    resource_type = register_json_ld_resource_type()
    return register_json_file(context_func, previous_revision, resource_type)


def register_indexing_policy(indexing_policy_func, previous_revision):
    resource_type = register_indexing_policy_resource_type()
    return register_json_file(indexing_policy_func, previous_revision, resource_type)


def json_resource_type(
    resource_type_uri: str,
    slug: str,
    revision: str = '1',
    schema_resource: Resource = None,
    schema_func=None,
    schema_uri: str = None,
    context_resource: Resource = None,
    context_func=None,
    context_uri: str = None,
    viewset_class: object = None,
    indexing_policy_resource: Resource = None,
    indexing_policy_func=None,
    uses_reversion: bool = True,
    register_service: bool = True,
):
    """Registration of a single json resource type. Callers **should** provide one and
    only one of `schema_resource, schema_path, schema_uri` as well as one and only  one
    of `context_resource, context_path, context_uri`.
    """
    try:
        resource_type_registration = ResourceTypeRegistration(
            resource_type_uri,
            revision,
            slug=slug,
            schema_resource=schema_resource,
            schema_func=schema_func,
            schema_uri=schema_uri,
            context_resource=context_resource,
            context_func=context_func,
            context_uri=context_uri,
            viewset_class=viewset_class,
            indexing_policy_func=indexing_policy_func,
            uses_reversion=uses_reversion,
            register_service=register_service,
        )
        return resource_type_registration.urls
    except (OperationalError, ProgrammingError):
        logging.warning(
            "Failed to register resource type due to OperationalError. "
            "This is expected for migrations."
        )
    return []


def resource_types(*args):
    """Convenience function to make the configuration of resource type registrations
    more readable.
    """
    ret = []
    for urls in args:
        ret.extend(urls)
    return ret
