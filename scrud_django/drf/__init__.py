"""The Django REST framework front end: scrud's DRF views, serializers,
filters, permissions and exception handler, and its URLconf
(`scrud_django.drf.urls`). It needs the ``drf`` extra
(``pip install 'scrud-django[drf]'``)."""

try:
    import rest_framework  # noqa: F401
except ImportError as error:
    if not (
        isinstance(error, ModuleNotFoundError)
        and (error.name or "").split(".")[0] == "rest_framework"
    ):
        # DRF is installed but fails to import: its own error.
        raise
    raise ImportError(
        "scrud_django.drf needs scrud-django[drf] (Django REST framework): "
        "pip install 'scrud-django[drf]'"
    ) from error
