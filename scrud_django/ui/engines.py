"""Compiling the templates of stored UI components.

Django templates compile on a standalone engine with no loaders and no tag
libraries, so `{% load %}` of app tags is unavailable, and `{% include %}` and
`{% extends %}` (which would need a loader) are refused at compile time. Jinja2
templates run in a `SandboxedEnvironment`, which blocks access to dunder
attributes and unsafe callables; Jinja `include`, `extends` and `import` fail at
render time because there is no loader. Both engines autoescape; `SafeString`
values (child HTML) pass through unescaped.
"""

from functools import lru_cache

from django.template import Context, Engine, TemplateSyntaxError
from django.template.loader_tags import ExtendsNode, IncludeNode
from django.utils.safestring import SafeString, mark_safe

ENGINE_NAMES = ("django", "jinja2")

_django_engine = Engine(
    loaders=[], libraries={}, builtins=[], autoescape=True, debug=False
)


class TemplateCompileError(ValueError):
    """A template that does not compile, or whose engine is unavailable."""


class CompiledTemplate:
    def __init__(self, template, render):
        self._template = template
        self._render = render

    def render(self, context: dict) -> SafeString:
        return mark_safe(self._render(self._template, context))


def _compile_django(source: str) -> CompiledTemplate:
    try:
        template = _django_engine.from_string(source)
    except TemplateSyntaxError as error:
        raise TemplateCompileError(str(error)) from error
    if template.nodelist.get_nodes_by_type((IncludeNode, ExtendsNode)):
        raise TemplateCompileError("include and extends are not available")
    return CompiledTemplate(
        template, lambda compiled, context: compiled.render(Context(context))
    )


@lru_cache(maxsize=1)
def _jinja_environment():
    """The sandboxed Jinja2 environment, built on first use (lazy import)."""
    import jinja2
    from jinja2.sandbox import SandboxedEnvironment

    return SandboxedEnvironment(
        autoescape=True, undefined=jinja2.Undefined, loader=None
    )


def _compile_jinja2(source: str) -> CompiledTemplate:
    try:
        import jinja2
    except ImportError as error:
        raise TemplateCompileError("jinja2 is not installed") from error
    environment = _jinja_environment()
    try:
        template = environment.from_string(source)
    except jinja2.TemplateSyntaxError as error:
        raise TemplateCompileError(str(error)) from error
    return CompiledTemplate(
        template, lambda compiled, context: compiled.render(context)
    )


_COMPILERS = {"django": _compile_django, "jinja2": _compile_jinja2}


def compile_template(engine: str, source: str) -> CompiledTemplate:
    """Compile `source` for `engine` (``"django"`` or ``"jinja2"``), raising
    `TemplateCompileError` when it cannot be used."""
    try:
        compiler = _COMPILERS[engine]
    except (KeyError, TypeError):
        raise TemplateCompileError(f"unknown template engine {engine!r}") from None
    if not isinstance(source, str):
        raise TemplateCompileError("template must be a string")
    return compiler(source)


@lru_cache(maxsize=512)
def cached_template(key: tuple, engine: str, source: str) -> CompiledTemplate:
    """`compile_template`, cached. `key` is ``(resource pk, etag)`` or
    ``("builtin", name)``; the cache is keyed on `engine` and `source` too, so
    a stale key can never serve a different source."""
    return compile_template(engine, source)
