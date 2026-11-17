"""The django-ninja front end (django-ninja is a base dependency)."""

try:
    import ninja  # noqa: F401
except ImportError as error:
    if not (
        isinstance(error, ModuleNotFoundError)
        and (error.name or "").split(".")[0] == "ninja"
    ):
        # django-ninja is installed but fails to import: its own error.
        raise
    raise ImportError(
        "scrud_django.ninja needs django-ninja, which scrud-django installs; "
        "reinstall it: pip install scrud-django"
    ) from error

from scrud_django.ninja.api import api  # noqa: E402
from scrud_django.ninja.workflow import ModelWorkflow, workflow_check  # noqa: E402

__all__ = ["ModelWorkflow", "api", "workflow_check"]
