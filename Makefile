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

# PLATFORMS is empty by default: build for the host, which is what a developer
# iterating locally wants. Two or more entries produce a manifest list and need
# QEMU for any platform that is not the host's, so the release workflow sets it
# rather than every local build paying emulation cost.
#   linux/arm64 covers Raspberry Pi 3/4/5 and Zero 2 W on 64-bit Raspberry Pi OS.
#   Do NOT add an armv8.2 entry -- see the Raspberry Pi note in CLAUDE.md.
PLATFORMS ?=
RELEASE_PLATFORMS := linux/amd64,linux/arm64

CONTAINER_FLAGS := $(if $(strip $(REQUIRE_CONTAINER)),--require,) \
	$(if $(strip $(PLATFORMS)),--platform $(strip $(PLATFORMS)),)
DEMO_MCP_COMMAND ?= $(PYTHON_BIN) tests/fixtures/mock_mcp_server.py
START_WS := scripts/start-ws.sh
HOST ?= 127.0.0.1
# 8765 is the CLI's own default and the port the README, the container examples
# and transport_ws.md all advertise. `run` used to bind 8766 for no reason anyone
# recorded, which made every copied-and-pasted client URL wrong.
PORT ?= 8765

# LOG is optional and off by default. LOG=1 takes the start script's default path
# (logs/python-acp-ws.log); any other value is used as the path itself.
LOG ?=
RUN_LOG_FLAG := $(if $(strip $(LOG)),$(if $(filter 1,$(strip $(LOG))),--log,--log=$(strip $(LOG))),)

# NO_KEY=1 runs `run`/`debug` with no access key at all. The default is to mint a
# fresh one per start, so the URL the banner prints is a complete, working example
# rather than something to be edited before it can be pasted. On loopback a key is
# optional -- the server only *refuses* a keyless bind off loopback -- but it costs
# nothing here and it stops another local account from opening a session, which
# `session/new` would let it use to run commands as whoever started this.
NO_KEY ?=

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

.PHONY: venv sync install lint docs-check test stats stats-check transcripts build wheel sdist container-image print-release-platforms package release-bundle run debug clean clean-outputs clean-venv distclean

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

## Rewrite STATISTICS.md from the source tree. The document is GENERATED -- an edit by
## hand is lost the next time anyone runs this. Counting is done on the AST, so a `def`
## in a docstring is not a function. `make stats-check` reports staleness without
## writing; it is deliberately NOT in the default gates, because failing a build over a
## line count is a tax rather than a guard. See scripts/code_stats.py.
stats: venv
	$(PYTHON_BIN) scripts/code_stats.py
	@git --no-pager diff --stat STATISTICS.md || true

stats-check: venv
	$(PYTHON_BIN) scripts/code_stats.py --check

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

## The platform list releases build for. publish-artifacts.yml reads it from here
## rather than repeating it, so the workflow and RELEASE_PLATFORMS cannot drift.
print-release-platforms:
	@printf '%s\n' '$(RELEASE_PLATFORMS)'

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

## Both `run` and `debug` go through $(START_WS) rather than calling the interpreter
## directly, because the script *activates* the venv instead of merely running its
## python. That difference is invisible here and load-bearing one level down: an MCP
## server a client names in `session/new` as a bare `python` inherits PATH from this
## process, so without activation it would resolve to whatever interpreter the caller
## happened to have -- one without this project's dependencies installed.
##
## $(1) labels the banner, $(2) is any extra CLI flag. The key is **exported**, never
## passed as an argument: argv is world-readable through `ps`, so a flag would publish
## the secret to every other user of the machine at the moment it is meant to protect
## it. That is the same reasoning that keeps a --ws-key flag out of the CLI; see
## ACCESS_KEY_ENV in transport_ws.py. An exported empty string reads as "no key", which
## is exactly what access_key_from_env() does with it.
define start_bridge
	@key=""; \
	if [ -n "$${PYTHON_ACP_WS_KEY:-}" ]; then \
		key="$$PYTHON_ACP_WS_KEY"; \
	elif [ -z "$(strip $(NO_KEY))" ]; then \
		key=$$($(PYTHON_BIN) -c 'import secrets; print(secrets.token_urlsafe(32))'); \
	fi; \
	printf 'Starting python-acp$(1)...\n'; \
	if [ -n "$$key" ]; then \
		: 'Percent-encode for the URL. A generated key is already URL-safe base64,'; \
		: 'but one supplied through the environment need not be, and a raw & or'; \
		: 'space would make the banner print a URL that quietly does not work.'; \
		: 'Handed over in the environment, not argv, for the reason above.'; \
		enc=$$(PYACP_RAW_KEY="$$key" $(PYTHON_BIN) -c \
			'import os, urllib.parse; print(urllib.parse.quote(os.environ["PYACP_RAW_KEY"], safe=""))'); \
		printf 'Connect to: ws://$(HOST):$(PORT)/?key=%s\n' "$$enc"; \
	else \
		printf 'Connect to: ws://$(HOST):$(PORT)   (no key; loopback clients only)\n'; \
	fi; \
	printf 'Name your MCP servers in session/new; there is no process-wide one.\n'; \
	printf 'The demo server is: $(DEMO_MCP_COMMAND)\n'; \
	printf 'Press Ctrl+C to stop.\n'; \
	export PYTHON_ACP_WS_KEY="$$key"; \
	exec $(START_WS) --host $(HOST) --port $(PORT) $(2) $(RUN_LOG_FLAG)
endef

run: venv
	$(call start_bridge,,)

## Same bind, with --debug: the WebSocket handshake and every MCP message in both
## directions, each line now naming the logger that emitted it. Add LOG=1 to keep a
## copy in logs/python-acp-ws.log, since debug output scrolls past faster than it can
## be read.
debug: venv
	$(call start_bridge, (debug),--debug)

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
