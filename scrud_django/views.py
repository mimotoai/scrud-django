from django.utils.cache import patch_vary_headers
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import viewsets
from rest_framework.decorators import permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.views import APIView
from scoped_rbac import problems
from scoped_rbac.permissions import IsAuthorized
from scoped_rbac.rest import AccessControlledAPIView, RbacJSONParser
from scrudful.rest_framework import scrudful_api_view, scrudful_viewset

from scrud_django import collection_type_uri_for, services
from scrud_django.core.collections import (  # noqa: F401 (moved from here)
    COLLECTION_CONTENT_POINTER,
    collection_context,
    collection_schema,
    embedded_schema,
)
from scrud_django.filters import ResourceContentFilter, ResourceRbacFilter
from scrud_django.mixins.view_mixins import (
    ReadableMixin,
    ResourceViewMixin,
    ScrudErrorsMixin,
    gate_uniform_errors,
)
from scrud_django.registration import get_resource_type_for
from scrud_django.renderers import JSONLDRenderer, SchemaJSONRenderer
from scrud_django.validators import JsonValidator


class DocumentMediaTypeMixin:
    """Serve a view's document (a JSON Schema or a JSON-LD context) as the
    media type a ``Link`` to it gives (`document_renderer_class`): its
    renderer comes first in content negotiation for a request that
    `serves_document`, ahead of the view's other renderers, so that a
    client accepting anything gets that type and one asking for
    ``application/json`` gets the same body as ``application/json``. The
    response varies on ``Accept``. An error is rendered as plain JSON
    (``application/json``, or the problem details' own type)."""

    document_renderer_class = None

    def serves_document(self, request):
        """Whether `request`'s response is the document: a GET or HEAD."""
        return request.method in ("GET", "HEAD")

    def get_renderers(self):
        renderers = super().get_renderers()
        if self.document_renderer_class is not None and self.serves_document(
            self.request
        ):
            renderers.insert(0, self.document_renderer_class())
        return renderers

    def finalize_response(self, request, response, *args, **kwargs):
        if self.document_renderer_class is not None and self.serves_document(request):
            patch_vary_headers(response, ["Accept"])
            if (
                isinstance(response, Response)
                and not 200 <= response.status_code < 300
                and isinstance(
                    getattr(request, "accepted_renderer", None),
                    self.document_renderer_class,
                )
            ):
                request.accepted_renderer = JSONRenderer()
                request.accepted_media_type = JSONRenderer.media_type
        return super().finalize_response(request, response, *args, **kwargs)


@scrudful_viewset
class ResourceViewSet(
    ReadableMixin, AccessControlledAPIView, ResourceViewMixin, viewsets.ModelViewSet
):
    # Fail closed: DRF's default permission is AllowAny, and scoped-rbac sets no
    # default either, so without this a project that does not configure
    # DEFAULT_PERMISSION_CLASSES would let anyone write and delete resources.
    permission_classes = [IsAuthorized]

    # JSON bodies only. A form or multipart body can name ``rbac_context`` more
    # than once: scoped-rbac checks the last value, the list would be stored.
    parser_classes = [RbacJSONParser]

    # The list shows exactly the resources the detail route shows the user, as
    # IsAuthorized permissions decide it (ResourceRbacFilter); a detail lookup
    # is scoped as RbacFilter scopes it.
    filter_backends = [ResourceRbacFilter]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.resource_type_name = kwargs.get('resource_type_name')
        self.validator = JsonValidator()

    @property
    def resource_type_iri(self):
        resource_type = self.get_resource_type()
        return resource_type.type_uri

    @property
    def list_type_iri(self):
        return collection_type_uri_for(self.resource_type_iri)


@scrudful_viewset
class ResourceViewSetGenericFilter(
    ReadableMixin, AccessControlledAPIView, ResourceViewMixin, viewsets.ModelViewSet
):
    # Fail closed: DRF's default permission is AllowAny, and scoped-rbac sets no
    # default either, so without this a project that does not configure
    # DEFAULT_PERMISSION_CLASSES would let anyone write and delete resources.
    permission_classes = [IsAuthorized]

    # JSON bodies only. A form or multipart body can name ``rbac_context`` more
    # than once: scoped-rbac checks the last value, the list would be stored.
    parser_classes = [RbacJSONParser]

    # ResourceRbacFilter first: it scopes the lookup in get_object() (and so
    # scrudful's object validators) as scoped-rbac's RbacFilter does, and
    # restricts the list to the resources the detail route shows the user;
    # check_object_permissions then enforces the object's access for the
    # method (GET, PUT, DELETE). ResourceContentFilter filters the list on its
    # query parameters.
    filter_backends = [ResourceRbacFilter, ResourceContentFilter, DjangoFilterBackend]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.resource_type_name = kwargs.get('resource_type_name')
        self.validator = JsonValidator()

    @property
    def resource_type_iri(self):
        resource_type = self.get_resource_type()
        return resource_type.type_uri

    @property
    def list_type_iri(self):
        return collection_type_uri_for(self.resource_type_iri)


class DocumentResourceViewSet(DocumentMediaTypeMixin, ResourceViewSet):
    """The resource views of a type whose resources are documents (JSON
    Schemas, JSON-LD contexts): a resource's representation (its detail
    route's GET, HEAD and PUT) is served as `document_renderer_class`'s
    media type; the list, of envelopes, stays ``application/json``."""

    def serves_document(self, request):
        return getattr(self, "action", None) in ("retrieve", "update")


class JsonSchemaViewSet(DocumentResourceViewSet):
    """The ``json-schema`` type's views: its resources are JSON Schemas."""

    document_renderer_class = SchemaJSONRenderer


class JsonLdViewSet(DocumentResourceViewSet):
    """The ``json-ld`` type's views: its resources are JSON-LD contexts."""

    document_renderer_class = JSONLDRenderer


class ResourceCollectionSchemaView(ScrudErrorsMixin, DocumentMediaTypeMixin, APIView):
    """The JSON Schema of a resource type's list body (the list's
    ``describedby``): the page envelope of `StandardResultsSetPagination`,
    whose ``content`` is the page's resource envelopes, each with the type's
    schema embedded as its ``content`` (`embedded_schema`), a ``$ref`` to the
    type's ``schema_uri``, or any value for a type without a schema."""

    permission_classes = [AllowAny]
    document_renderer_class = SchemaJSONRenderer

    def get(self, request, slug: str):
        return Response(collection_schema(get_resource_type_for(slug)))


class ResourceCollectionContextView(ScrudErrorsMixin, DocumentMediaTypeMixin, APIView):
    """The JSON-LD context of a resource type's list body (served as
    ``application/ld+json``). Its terms are kept as they were (the
    ``opententeams:`` prefix of first/previous/next/last included): the
    ``api.openteams.com`` IRIs are part of the API's semantics."""

    permission_classes = [AllowAny]
    document_renderer_class = JSONLDRenderer

    def get(self, request, slug: str):
        return Response(collection_context(get_resource_type_for(slug)))


def services_etag(view, request, *args, **kwargs):
    """The service list's ETag: a hash of the catalog it serves
    (`ScrudServices.etag_for`). It has no Last-Modified."""
    return services.etag_for(request)


@scrudful_api_view(
    etag_func=services_etag,
    error=problems.problem_entity(),
    error_body_func=problems.conditional_problem,
)
@permission_classes([AllowAny])
def get_service_list(request, *args, **kwargs):
    return Response(services.catalog(request))


# scrudful's 400, 412 and 428 are problem details only while
# SCOPED_RBAC["UNIFORM_ERRORS"] is on, as on the resource views.
gate_uniform_errors(get_service_list.cls)
