#!/usr/bin/env python
"""Django's command-line utility for the library's own checks.

It configures ``tests.settings.SETTINGS``, the test settings, whose databases
are SQLite ``:memory:``: nothing a command writes persists, and each command
starts from an empty database. Use it for ``check`` and
``makemigrations scrud_django --check --dry-run`` (and to generate a
migration). For a server with a persistent database, use the demo project
(``cd demo && uv run python manage.py migrate`` then ``runserver``).
"""

import os
import sys


def main():
    if "DJANGO_SETTINGS_MODULE" not in os.environ:
        from django.conf import settings

        from tests.settings import SETTINGS

        settings.configure(**SETTINGS)
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == '__main__':
    main()
