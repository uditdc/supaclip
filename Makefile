.PHONY: help venv install install-mcp test lint clean build sdist wheel dist-check publish-test publish

PY ?= python3
VENV ?= .venv
BIN := $(VENV)/bin

help:
	@echo "Targets:"
	@echo "  venv          - create local virtualenv at $(VENV)"
	@echo "  install       - editable install + dev extras"
	@echo "  install-mcp   - editable install + dev + mcp extras"
	@echo "  test          - run pytest"
	@echo "  clean         - remove build artifacts and caches"
	@echo "  build         - build sdist and wheel into dist/"
	@echo "  sdist         - build sdist only"
	@echo "  wheel         - build wheel only"
	@echo "  dist-check    - validate built artifacts with twine"
	@echo "  publish-test  - upload to TestPyPI (requires TWINE_* env)"
	@echo "  publish       - upload to PyPI (requires TWINE_* env)"

venv:
	$(PY) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip

install: venv
	$(BIN)/pip install -e ".[dev]"

install-mcp: venv
	$(BIN)/pip install -e ".[dev,mcp]"

test:
	$(BIN)/pytest

clean:
	rm -rf build/ dist/ *.egg-info supaclip/*.egg-info
	find . -type d -name __pycache__ -not -path './$(VENV)/*' -exec rm -rf {} +
	find . -type d -name .pytest_cache -not -path './$(VENV)/*' -exec rm -rf {} +

build: clean
	$(BIN)/pip install --quiet --upgrade build
	$(BIN)/python -m build

sdist: clean
	$(BIN)/pip install --quiet --upgrade build
	$(BIN)/python -m build --sdist

wheel: clean
	$(BIN)/pip install --quiet --upgrade build
	$(BIN)/python -m build --wheel

dist-check:
	$(BIN)/pip install --quiet --upgrade twine
	$(BIN)/python -m twine check dist/*

publish-test: build dist-check
	$(BIN)/python -m twine upload --repository testpypi dist/*

publish: build dist-check
	$(BIN)/python -m twine upload dist/*
