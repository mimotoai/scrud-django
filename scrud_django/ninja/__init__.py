"""The django-ninja front end (optional: ``pip install 'scrud-django[ninja]'``)."""

try:
    import ninja  # noqa: F401
except ImportError as error:
    raise ImportError(
        "scrud_django.ninja needs django-ninja: pip install 'scrud-django[ninja]'"
    ) from error
