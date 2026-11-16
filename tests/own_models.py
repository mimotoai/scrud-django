"""An app's own model (``tests.Article``) served through ninja with scrud's
workflow: a hand-written router built from scoped-rbac's public ninja blocks
as ``scoped_rbac.ninja.crud_router`` builds its own (scoped-rbac 0.12.1's
``crud_router`` takes no extra checks), its writes through the
`ARTICLE_WORKFLOW` toolkit (``scrud_django.ninja.workflow_check``).

What ``crud_router`` does that this router leaves out:

- its OPTIONS ``allowed`` hook (the private ``_allowed_as_drf``): scrudful
  decides ``Allow`` from each method's ``before`` and ``load`` instead;
- its operation decorator running a conditional create in one transaction
  (the private ``_conditional_create_atomic``): ``Article`` has no
  validators, so a create takes no precondition;
- the detail-route mark (``DETAIL_MARK``, not exported) and the PATCH route;
- the schema and context links (``scoped_rbac.semantic``), which name a type
  by an IRI without ``/`` (``tests://Article`` has some).
"""

from django.http import HttpResponse
from ninja import Schema
from scoped_rbac import access
from scoped_rbac.ninja import (
    authorize_write,
    checked_list,
    default_contexts_from,
    detail_iri,
    item_check,
    list_check,
    rbac_load,
    request_json,
    resolve_user,
    validate_body,
)
from scrudful.ninja import ScrudfulRouter, scrudful, scrudful_create, scrudful_list

from scrud_django.ninja import workflow_check

from .models import Article
from .workflow_defaults import ARTICLE


class ArticleIn(Schema):
    title: str = ""
    state: str = "draft"
    rbac_context: str = ""


class ArticleOut(Schema):
    id: int | None = None
    title: str
    state: str
    rbac_context: str


# Its workflow reads the JSON the API answers with (ArticleOut's).
ARTICLE_WORKFLOW = workflow_check(ARTICLE, model=Article, schema_out=ArticleOut)

DETAIL = "article-detail"
QUERYSET = Article.objects.all()


def href(request, obj):
    return detail_iri(request, DETAIL, obj.pk)


def rows():
    return QUERYSET.order_by("pk")


load = rbac_load(Article, ARTICLE, queryset=QUERYSET, post_save_checked=True)
check = item_check(ARTICLE, load=load)
listing = list_check(ARTICLE, lambda request, **path_params: rows())

router = ScrudfulRouter()


@router.api_operation(
    ["GET", "HEAD"],
    "/",
    response=list[ArticleOut],
    url_name="article-list",
    operation_id="article_list",
)
@scrudful_list(href_func=href, page_size=100, max_page_size=1000, **listing)
def list_articles(request):
    return checked_list(request)


@router.post(
    "/",
    response={201: ArticleOut},
    url_name="article-list",
    operation_id="article_create",
)
@scrudful_create(location_func=href, **check)
def create_article(request):
    resolve_user(request)
    content = request_json(request)
    contexts = access.call_contexts_from(
        default_contexts_from,
        access.contexts_body(content),
        where=f"create of {ARTICLE}",
    )
    authorize_write(request, ARTICLE, contexts, access.body_mapping(content))
    values = validate_body(request, ArticleIn, content)
    return ARTICLE_WORKFLOW.save_checked(
        request,
        instance=None,
        values=values,
        save=lambda: Article.objects.create(**values),
        queryset=QUERYSET,
    )


@router.api_operation(
    ["GET", "HEAD"],
    "/{pk}/",
    response=ArticleOut,
    url_name=DETAIL,
    operation_id="article_retrieve",
)
@scrudful(load=load, **check)
def retrieve_article(request, pk: str, obj):
    return obj


@router.put(
    "/{pk}/", response=ArticleOut, url_name=DETAIL, operation_id="article_replace"
)
@scrudful(load=load, **check)
def replace_article(request, pk: str, obj):
    values = validate_body(request, ArticleIn, request_json(request))

    def save():
        for name, value in values.items():
            setattr(obj, name, value)
        obj.save()
        return obj

    return ARTICLE_WORKFLOW.save_checked(
        request, instance=obj, values=values, save=save, queryset=QUERYSET
    )


@router.delete(
    "/{pk}/", response={204: None}, url_name=DETAIL, operation_id="article_destroy"
)
@scrudful(load=load, **check)
def destroy_article(request, pk: str, obj):
    ARTICLE_WORKFLOW.delete_checked(request, instance=obj)
    # No content, so no Content-Type (ninja's would be application/json).
    response = HttpResponse(status=204)
    del response["Content-Type"]
    return response
