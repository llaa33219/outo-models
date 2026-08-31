.PHONY: sync lint format typecheck test smoke build-stable build-dev

sync:
	uv sync --frozen

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff format .
	uv run ruff check --fix .

typecheck:
	uv run mypy src

test:
	uv run pytest

smoke:
	uv run pytest tests/integration/test_e2e_smoke.py -v

build-stable:
	podman build --build-arg IMAGE_FLAVOR=stable -t outo-models:stable .

build-dev:
	podman build --build-arg IMAGE_FLAVOR=dev -t outo-models:dev .
