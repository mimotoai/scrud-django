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
    # Optional: the workflow reads ArticleOut's JSON of a create's unsaved
    # instance too, which has no primary key yet.
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


# The request and object checks run inside ARTICLE_WORKFLOW.covering: a grant
# conditioned on scrud_workflow allows a POST to the list and a PUT to an item
# (the workflow check then decides the transition), and refuses anything
# else, as on scrud's resource routes. A list's GET is never covered.
load = ARTICLE_WORKFLOW.covered(
    rbac_load(Article, ARTICLE, queryset=QUERYSET, post_save_checked=True),
    item=True,
)
before = item_check(ARTICLE, load=load)["before"]
create_check = {"before": ARTICLE_WORKFLOW.covered(before, item=False)}
item_hooks = {"load": load, "before": ARTICLE_WORKFLOW.covered(before, item=True)}
listing = list_check(ARTICLE, lambda request, **path_params: rows())


def updater(obj, values):
    """The save of an update: `values` set on `obj` (the row as
    ``save_checked`` locked it), then saved."""

    def save():
        for name, value in values.items():
            setattr(obj, name, value)
        obj.save()
        return obj

    return save


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
@scrudful_create(location_func=href, **create_check)
def create_article(request):
    resolve_user(request)
    content = request_json(request)
    contexts = access.call_contexts_from(
        default_contexts_from,
        access.contexts_body(content),
        where=f"create of {ARTICLE}",
    )
    with ARTICLE_WORKFLOW.covering(request, item=False):
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
@scrudful(**item_hooks)
def retrieve_article(request, pk: str, obj):
    return obj


@router.put(
    "/{pk}/", response=ArticleOut, url_name=DETAIL, operation_id="article_replace"
)
@scrudful(**item_hooks)
def replace_article(request, pk: str, obj):
    values = validate_body(request, ArticleIn, request_json(request))
    return ARTICLE_WORKFLOW.save_checked(
        request,
        instance=obj,
        values=values,
        save=updater(obj, values),
        queryset=QUERYSET,
    )


@router.delete(
    "/{pk}/", response={204: None}, url_name=DETAIL, operation_id="article_destroy"
)
@scrudful(**item_hooks)
def destroy_article(request, pk: str, obj):
    ARTICLE_WORKFLOW.delete_checked(request, instance=obj)
    # No content, so no Content-Type (ninja's would be application/json).
    response = HttpResponse(status=204)
    del response["Content-Type"]
    return response
