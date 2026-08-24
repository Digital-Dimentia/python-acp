SHELL := /bin/bash

# --- environment contract -------------------------------------------------
# VENV_DIR is pinned, not inferred: every developer and every CI leg uses the
# same directory unless they deliberately override it. `.venv` is canonical; a
# pre-existing `venv/` is migrated to it on the first `make venv`.
# PYTHON is the interpreter the venv is *built from*. If it resolves to an
# interpreter inside a virtual environment (an activated shell), the bootstrap
# script steps out to its base interpreter first.
PYTHON ?= python3
VENV_DIR ?= .venv
PYTHON_BIN := $(VENV_DIR)/bin/python
VENV_STAMP := $(VENV_DIR)/.python-acp-venv.json
VENV_BOOTSTRAP := scripts/venv_bootstrap.py
BUILD_DIR := dist
ARTIFACTS_DIR := artifacts
CONTAINER_SCRIPT := scripts/container_image.py
CONTAINER_TAG ?= python-acp:local

# REQUIRE_CONTAINER=1 makes `container-image` fail rather than skip when no
# usable engine is present. Empty by default so packaging works on a machine
# without one; set in .github/workflows/publish-artifacts.yml.
REQUIRE_CONTAINER ?=
CONTAINER_FLAGS := $(if $(strip $(REQUIRE_CONTAINER)),--require,)
DEMO_MCP_COMMAND ?= $(PYTHON_BIN) tests/fixtures/mock_mcp_server.py
HOST ?= 127.0.0.1
PORT ?= 8766

# Opt-in escape hatch for a TLS-intercepting proxy whose CA pip does not trust.
# Empty by default, so nothing is exported and no verification is relaxed on a
# normal machine. Space-separated; pip reads PIP_TRUSTED_HOST natively.
PIP_TRUSTED_HOST ?=
ifneq ($(strip $(PIP_TRUSTED_HOST)),)
export PIP_TRUSTED_HOST
endif

# OFFLINE=1 forbids the bootstrap from touching the network; it succeeds only if
# the venv already satisfies pyproject.toml.
OFFLINE ?=
VENV_FLAGS := $(if $(strip $(OFFLINE)),--offline,)

.PHONY: venv sync install lint docs-check test transcripts build wheel sdist container-image package release-bundle run clean clean-outputs clean-venv distclean

venv: $(VENV_STAMP)

# The stamp records the interpreter and the pyproject.toml digest the venv was
# built for. It is why `make test` no longer runs `pip install` (and therefore
# no longer needs the network) on every invocation.
$(VENV_STAMP): pyproject.toml $(VENV_BOOTSTRAP)
	$(PYTHON) $(VENV_BOOTSTRAP) --venv-dir $(VENV_DIR) --python $(PYTHON) $(VENV_FLAGS)

# Force a dependency install even when the stamp is current (dependencies
# changed outside pyproject.toml, or a half-finished install needs repairing).
sync:
	$(PYTHON) $(VENV_BOOTSTRAP) --venv-dir $(VENV_DIR) --python $(PYTHON) --sync

install: venv

lint: venv
	$(PYTHON_BIN) -m ruff check src tests scripts

## Documentation invariants nothing else enforces: relative links resolve, every
## Mermaid flowchart edge names a node its own block defines, and every production
## module has a sibling .md. See scripts/check_docs.py for what it deliberately
## does not check.
docs-check: venv
	$(PYTHON_BIN) scripts/check_docs.py

test: venv
	$(PYTHON_BIN) -m pytest tests

## Re-record the golden JSON-RPC transcripts in tests/transcripts/, then READ THE DIFF.
## A regeneration nobody looked at is worse than no transcript: it launders a wire
## regression into a committed file. See tests/test_transcripts.py.
transcripts: venv
	PYTHON_ACP_RECORD_TRANSCRIPTS=1 $(PYTHON_BIN) -m pytest tests/test_transcripts.py
	@git --no-pager diff --stat tests/transcripts/ || true

build: venv
	mkdir -p $(BUILD_DIR)
	$(PYTHON_BIN) -m build

wheel: build
	@ls -1 $(BUILD_DIR)/*.whl 2>/dev/null | head -n 1

sdist: build
	@ls -1 $(BUILD_DIR)/*.tar.gz 2>/dev/null | head -n 1

## Build the container image and export it to $(BUILD_DIR).
## Skips -- exit 0, loud message -- when no engine is installed OR when one is
## installed but its backend is unreachable (a stopped `podman machine`, a dead
## docker daemon). REQUIRE_CONTAINER=1 turns any skip into a failure; the release
## workflow sets it, because a release that ships without its image should not be
## quiet about it. Logic lives in $(CONTAINER_SCRIPT), not here: see its docstring.
container-image: venv
	$(PYTHON_BIN) $(CONTAINER_SCRIPT) \
		--tag $(CONTAINER_TAG) \
		--containerfile Containerfile \
		--context . \
		--output $(BUILD_DIR)/python-acp-container.tar \
		$(CONTAINER_FLAGS)

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
	@printf 'Starting python-acp...\n'
	@printf 'Connect to: ws://$(HOST):$(PORT)\n'
	@printf 'Name your MCP servers in session/new; there is no process-wide one.\n'
	@printf 'The demo server is: $(DEMO_MCP_COMMAND)\n'
	@printf 'Press Ctrl+C to stop.\n'
	$(PYTHON_BIN) -m python_acp.cli --host $(HOST) --port $(PORT)

## Build outputs and tool caches. **Leaves the virtual environment alone.**
##
## Since the venv became stamped and reused (`pyacp-caq`), deleting it is the one action
## here that forces a full reinstall over the network -- and behind a TLS-intercepting
## proxy that may not be recoverable at all without PIP_TRUSTED_HOST or PIP_CERT. A
## target named `clean` should not be able to leave a checkout unbuildable offline, so
## asking for that is now a separate, deliberate word.
##
## `src/*.egg-info` and not just `*.egg-info`: this is a src-layout project, so the
## editable install writes `src/python_acp.egg-info`, which the old glob never matched.
clean: clean-outputs
	@printf 'Build outputs and caches removed. $(VENV_DIR) was left alone;\n'
	@printf 'use `make clean-venv` to remove it (needs the network to rebuild).\n'

## The removal itself, shared with `distclean` so that target does not inherit the note
## above -- which would be false there, since `distclean` does remove the venv.
clean-outputs:
	rm -rf build $(BUILD_DIR) $(ARTIFACTS_DIR) src/*.egg-info *.egg-info \
		.pytest_cache .ruff_cache

## The virtual environment, and nothing else. Rebuilding it needs the network unless
## every wheel is already cached. Honours VENV_DIR, so `make clean-venv VENV_DIR=.venv312`
## removes that one and leaves the default alone.
clean-venv:
	rm -rf $(VENV_DIR)

## Everything `clean` removes, plus the venv -- the GNU convention name, and what the
## single old `clean` target used to do.
distclean: clean-outputs clean-venv
