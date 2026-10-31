.PHONY: test lint format docs migrations-check

test:
	uv run pytest -vv

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff check --fix .
	uv run ruff format .

docs:
	uv run --group docs sphinx-build -W -b html docs docs/_build

migrations-check:
	uv run python manage.py makemigrations scrud_django --check --dry-run
