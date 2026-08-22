"""Guards on the agent-client-protocol dependency itself.

These are deliberately not tests of our code. Nothing in `src/python_acp/`
imports the SDK yet (that starts with the `acp.Agent` skeleton), so without
these the CI matrix would install the dependency on every interpreter and then
never touch it -- a broken install on one leg would stay invisible until the
first real use. They also pin the two facts the rest of the migration is built
on: which distribution we took, and that its interpreter window contains ours.
"""

from __future__ import annotations

import sys
from importlib.metadata import metadata, requires, version

from packaging.specifiers import SpecifierSet


PINNED_SDK_VERSION = "0.12.1"


def test_sdk_is_importable_and_matches_the_pin() -> None:
    import acp

    assert version("agent-client-protocol") == PINNED_SDK_VERSION
    # Zed's Agent *Client* Protocol, not IBM's `acp-sdk`. The IBM package also
    # imports as `acp` but has no Agent/Client protocol pair on the top level.
    assert hasattr(acp, "Agent")
    assert hasattr(acp, "Client")
    assert hasattr(acp, "AGENT_METHODS")


def test_running_interpreter_is_inside_the_sdk_support_window() -> None:
    """The SDK caps us below 3.15; every CI leg must land inside its window."""
    window = SpecifierSet(metadata("agent-client-protocol")["Requires-Python"])
    running = ".".join(str(part) for part in sys.version_info[:3])
    assert running in window, f"{running} is outside the SDK's {window}"


def test_sdk_http_extra_is_not_pulled_in() -> None:
    """httpx arrives only via the SDK's `http` extra, which we do not take."""
    http_extra = [r for r in (requires("agent-client-protocol") or []) if 'extra == "http"' in r]
    assert http_extra, "the SDK no longer declares an `http` extra; revisit the pin"
    assert any(r.startswith("httpx") for r in http_extra)

    import importlib.util

    assert importlib.util.find_spec("httpx") is None
