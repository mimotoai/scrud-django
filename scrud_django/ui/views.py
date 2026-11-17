"""Viewsets of the UI resource types: the DRF adapter of their write and read
behaviour, which is their hooks (`scrud_django.ui.hooks`)."""

from contextlib import contextmanager

from scoped_rbac import access
from scoped_rbac.permissions import drf_error
from scrudful.rest_framework import scrudful_viewset

from scrud_django.core import writes as core_writes
from scrud_django.registration import write_db
from scrud_django.ui import hooks
from scrud_django.ui.renderer import negotiated_html
from scrud_django.views import ResourceViewSet


@contextmanager
def drf_access_errors(request):
    """An ``access.AccessError`` raised in the block (a UI hook's refusal)
    raised as DRF's (`drf_error`), as the views answer the core's."""
    try:
        yield
    except access.AccessError as exc:
        raise drf_error(exc, request) from exc


class UIContentValidationMixin:
    """The write behaviour of a UI resource type on DRF: `validate_content`
    (the type's ``prepare_write``) runs ahead of `create_resource` and
    `update` saving, the core's save runs its ``around_save`` and
    ``after_write`` (whose answer `answered` sends), and the core's delete
    its ``before_destroy`` and ``after_destroy``
    (`scrud_django.ui.hooks.UI_TYPE_OPTIONS`)."""

    # The type's ``prepare_write`` (a function of `scrud_django.ui.hooks`).
    prepare_write = None
    # The resource as saved by the write being served (`save_checked`).
    saved = None

    def filter_queryset(self, queryset):
        """A list shows the rows the type's ``list_rows`` shows
        (`scrud_django.ui.hooks.list_item_rows`: an item kind's, the
        effective items of the selected branch), then the RBAC list filter
        applies to each row; the browsing cookie selects only for a list
        asked for as HTML."""
        queryset = super().filter_queryset(queryset)
        if getattr(self, "action", None) != "list":
            return queryset
        return hooks.list_item_rows(
            self.request,
            self.get_resource_type(),
            queryset,
            html=negotiated_html(self.request) is not None,
        )

    def validate_content(self, content, instance=None):
        """Raise a ``ScrudException`` when `content` (the request body) is
        refused: the type's ``prepare_write`` (`prepare_write`, one of
        `scrud_django.ui.hooks`), given `instance`, the resource an update
        replaces (`scrud_django.core.writes.prepared_body`, which clears
        the write's hook state first). `content` is the request body itself
        (``request.data``, the ``dict`` the JSON parser made), which
        `create_resource` and `update` then validate and save: the hook
        changes it in place, and must return it (``TypeError`` otherwise:
        another body would be checked, and this one saved). An
        ``access.AccessError`` it raises is answered as DRF's
        (`drf_error`)."""
        with drf_access_errors(self.request):
            prepared = core_writes.prepared_body(
                self.request,
                self.get_resource_type(),
                content,
                instance=instance,
                using=self.write_database(),
                prepare_write=self.prepare_write,
            )
        if prepared is not content:
            raise TypeError(
                f"{type(self).__name__}.prepare_write returned another body than "
                "the one it was given: the DRF views save request.data itself."
            )

    def save_checked(self, save, instance=None):
        """Save as `ResourceViewMixin.save_checked` does (the core runs the
        type's ``around_save``, an item written to ``main`` while it is
        virtual storing ``main`` first, and its ``after_write``, in the same
        transaction), keeping the saved resource for `answered`."""
        self.saved = super().save_checked(save, instance=instance)
        return self.saved

    def write_database(self):
        """The database this request writes: its locked write's, else the
        write alias."""
        return self.locked_write_db or write_db()

    def create_resource(self, request):
        self.validate_content(request.data)
        return self.answered(super().create_resource(request))

    def update(self, request, slug: str):
        instance = self.get_instance(request, slug)
        self.validate_content(request.data, instance=instance)
        return self.answered(super().update(request, slug))

    def answered(self, response):
        """`response` to the write that saved `saved` (`save_checked`); when
        the type's ``after_write`` answered for it (`scrud_django.core
        .writes.written_answer`: an applied push, with its ``result``), the
        saved resource as it is now, serialized."""
        if core_writes.written_answer(self.saved) is not None:
            response.data = self.get_serializer(instance=self.saved, many=False).data
        return response


@scrudful_viewset
class UIComponentViewSet(UIContentValidationMixin, ResourceViewSet):
    prepare_write = staticmethod(hooks.prepare_component)


@scrudful_viewset
class UIStylesheetViewSet(UIContentValidationMixin, ResourceViewSet):
    prepare_write = staticmethod(hooks.prepare_stylesheet)


@scrudful_viewset
class UICssViewSet(UIContentValidationMixin, ResourceViewSet):
    prepare_write = staticmethod(hooks.prepare_css)


@scrudful_viewset
class UIJsViewSet(UIContentValidationMixin, ResourceViewSet):
    prepare_write = staticmethod(hooks.prepare_js)
