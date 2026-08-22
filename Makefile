SHELL := /bin/bash

PYTHON ?= python3
VENV_DIR ?= $(if $(wildcard .venv),.venv,$(if $(wildcard venv),venv,.venv))
PYTHON_BIN := $(VENV_DIR)/bin/python
PIP_BIN := $(VENV_DIR)/bin/pip
BUILD_DIR := dist
ARTIFACTS_DIR := artifacts
DEMO_MCP_COMMAND ?= python3 tests/fixtures/mock_mcp_server.py
HOST ?= 127.0.0.1
PORT ?= 8766

.PHONY: venv install lint test build wheel sdist container-image package release-bundle run clean

venv:
	@if [ ! -d "$(VENV_DIR)" ]; then \
		$(PYTHON) -m venv $(VENV_DIR); \
	fi
	$(PYTHON_BIN) -m pip install --upgrade pip
	$(PYTHON_BIN) -m pip install -e '.[dev]'

install: venv

lint: venv
	$(PYTHON_BIN) -m ruff check src tests

test: venv
	$(PYTHON_BIN) -m pytest tests

build: venv
	mkdir -p $(BUILD_DIR)
	$(PYTHON_BIN) -m build

wheel: build
	@ls -1 $(BUILD_DIR)/*.whl 2>/dev/null | head -n 1

sdist: build
	@ls -1 $(BUILD_DIR)/*.tar.gz 2>/dev/null | head -n 1

container-image: venv
	mkdir -p $(BUILD_DIR)
	@ENGINE=$$(command -v podman || command -v docker || true); \
	if [ -z "$$ENGINE" ]; then \
		echo "Neither podman nor docker is installed; skipping container-image export." >&2; \
		exit 0; \
	fi; \
	$$ENGINE build -t python-acp:local -f Containerfile .; \
	$$ENGINE save -o $(BUILD_DIR)/python-acp-container.tar python-acp:local

package: build container-image
	mkdir -p $(ARTIFACTS_DIR)
	tar -czf $(ARTIFACTS_DIR)/python-acp-artifacts.tar.gz -C $(BUILD_DIR) .
	@echo "Generated artifacts:"
	@ls -l $(BUILD_DIR) $(ARTIFACTS_DIR)

release-bundle: build
	mkdir -p $(ARTIFACTS_DIR)
	@tmpdir=$$(mktemp -d); \
	cp -f $(BUILD_DIR)/*.whl $$tmpdir/ 2>/dev/null || true; \
	cp -f $(BUILD_DIR)/*.tar.gz $$tmpdir/ 2>/dev/null || true; \
	if [ -f "$(BUILD_DIR)/python-acp-container.tar" ]; then \
		cp -f $(BUILD_DIR)/python-acp-container.tar $$tmpdir/; \
	fi; \
	tar -czf $(ARTIFACTS_DIR)/python-acp-release-bundle.tar.gz -C "$$tmpdir" .; \
	rm -rf "$$tmpdir"; \
	printf 'Created %s\n' "$(ARTIFACTS_DIR)/python-acp-release-bundle.tar.gz"; \
	ls -l $(ARTIFACTS_DIR)

run: venv
	@printf 'Starting python-acp with demo MCP server...\n'
	@printf 'Connect to: ws://$(HOST):$(PORT)\n'
	@printf 'Press Ctrl+C to stop.\n'
	$(PYTHON_BIN) -m python_acp.cli --mcp-command $(DEMO_MCP_COMMAND) --host $(HOST) --port $(PORT)

clean:
	rm -rf build dist artifacts *.egg-info .pytest_cache .ruff_cache $(VENV_DIR)
