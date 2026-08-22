"""Tests for the capability manifest — the guard rail on what `initialize` promises.

Three of these are structural rather than behavioural, and they are the point of the
module:

* `test_the_manifest_covers_every_field_the_sdk_defines` walks the SDK's own model, so
  an `agent-client-protocol` bump that adds a capability field fails here instead of
  being advertised as whatever the SDK chose to default it to.
* `test_the_advertised_block_is_exactly_the_manifest` pins the wire block to the rows.
* `test_every_advertised_capability_names_a_feature_test` is the one the compliance
  matrix asks for: a literal cannot be flipped on without a test proving the feature
  behind it runs. See `CAPABILITY_EVIDENCE` below.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest
from acp import PROTOCOL_VERSION
from acp.schema import AgentCapabilities
from pydantic import BaseModel

from python_acp.capabilities import (
    AGENT_CAPABILITY_MANIFEST,
    AUTH_METHODS,
    SUPPORTED_PROTOCOL_VERSIONS,
    Capability,
    build_agent_capabilities,
    negotiate_protocol_version,
)

# The proof obligation. A capability advertised as *on* must appear here, mapped to the
# test that exercises the feature it promises, written `"module:test_name"` and resolved
# by import — so the entry cannot be satisfied by a test that does not exist.
#
# Empty at Phase 1 because the manifest advertises nothing. Flipping a row means adding
# the feature, adding its test, and adding a line here, in one commit; leaving any of the
# three out fails `test_every_advertised_capability_names_a_feature_test`.
CAPABILITY_EVIDENCE: dict[tuple[str, ...], str] = {
    ("load_session",): "test_agent:test_load_replays_the_sessions_transcript",
    ("session_capabilities", "list"): "test_agent:test_list_sessions_pages_most_recent_first",
    ("session_capabilities", "fork"): "test_agent:test_fork_copies_the_session_under_a_new_id",
    ("session_capabilities", "resume"): "test_agent:test_resume_returns_the_same_session_without_replaying",
    ("session_capabilities", "close"): "test_agent:test_close_ends_the_session_and_releases_its_backends",
}


def _leaf_paths(model: BaseModel, prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    """Every capability leaf the SDK defines, as attribute paths.

    Walks a *default-constructed* model: a field holding a sub-model is structure to
    descend into, and anything else is a leaf whose value is the promise. `_meta` is
    transport metadata, not a capability, and is the one field excluded.
    """
    paths: list[tuple[str, ...]] = []
    for name in type(model).model_fields:
        if name == "field_meta":
            continue
        value = getattr(model, name)
        if isinstance(value, BaseModel):
            paths.extend(_leaf_paths(value, prefix + (name,)))
        else:
            paths.append(prefix + (name,))
    return paths


def _read(capabilities: AgentCapabilities, path: tuple[str, ...]) -> Any:
    target: Any = capabilities
    for part in path:
        target = getattr(target, part)
    return target


def test_the_manifest_covers_every_field_the_sdk_defines() -> None:
    """An SDK bump that adds a capability is a decision, not something we inherit."""
    sdk_paths = set(_leaf_paths(AgentCapabilities()))
    manifest_paths = {capability.path for capability in AGENT_CAPABILITY_MANIFEST}

    assert sdk_paths - manifest_paths == set(), "SDK capability field with no manifest row"
    assert manifest_paths - sdk_paths == set(), "manifest row for a field the SDK dropped"


def test_no_capability_is_stated_twice() -> None:
    paths = [capability.path for capability in AGENT_CAPABILITY_MANIFEST]
    assert len(paths) == len(set(paths))


def _by_name(capability: Any) -> str:
    return capability.name if isinstance(capability, Capability) else str(capability)


@pytest.mark.parametrize("capability", AGENT_CAPABILITY_MANIFEST, ids=_by_name)
def test_every_capability_names_an_owner_and_a_reason(capability: Capability) -> None:
    """A `False` with no owner is indistinguishable from an oversight."""
    assert capability.owner
    assert len(capability.why) > 20


@pytest.mark.parametrize("capability", AGENT_CAPABILITY_MANIFEST, ids=_by_name)
def test_the_advertised_block_is_exactly_the_manifest(capability: Capability) -> None:
    assert _read(build_agent_capabilities(), capability.path) == capability.advertised


def test_the_unstable_lifecycle_is_withheld_on_a_stable_connection() -> None:
    """Advertising them there would be a promise the SDK's own router refuses to keep.

    `session/close`, `/fork`, and `/resume` are registered `unstable=True`, so with the
    flag off the router answers `method_not_found` *without calling the agent*.
    """
    gated = build_agent_capabilities(unstable=False)

    assert gated.session_capabilities.fork is None
    assert gated.session_capabilities.resume is None
    assert gated.session_capabilities.close is None
    # Everything not behind that gate is unchanged.
    assert gated.load_session is True
    assert gated.session_capabilities.list is not None


def test_only_the_lifecycle_rows_carry_the_unstable_gate() -> None:
    gated = {c.name for c in AGENT_CAPABILITY_MANIFEST if c.requires_unstable}

    assert gated == {
        "session_capabilities.fork",
        "session_capabilities.resume",
        "session_capabilities.close",
    }


def test_every_advertised_capability_names_a_feature_test() -> None:
    """A promise on the wire owes the suite a test that the feature actually runs."""
    advertised = {c.path for c in AGENT_CAPABILITY_MANIFEST if c.is_advertised}

    assert advertised == set(CAPABILITY_EVIDENCE), (
        "advertised capabilities and CAPABILITY_EVIDENCE disagree; a capability may not "
        "be turned on without naming the test that proves it"
    )
    for path, reference in CAPABILITY_EVIDENCE.items():
        module_name, _, test_name = reference.partition(":")
        module = importlib.import_module(module_name)
        assert callable(getattr(module, test_name, None)), (
            f"{'.'.join(path)} names {reference}, which does not exist"
        )


def test_each_call_builds_an_independent_block() -> None:
    """One connection's response must not be reachable from another's."""
    first = build_agent_capabilities()
    first.prompt_capabilities.image = True
    first.session_capabilities.list.field_meta = {"mutated": True}

    second = build_agent_capabilities()
    assert second.prompt_capabilities.image is False
    assert second.session_capabilities.list.field_meta is None


def test_no_auth_method_is_offered() -> None:
    """Empty is what makes `authenticate` a typed refusal rather than a -32601."""
    assert AUTH_METHODS == ()


def test_a_supported_version_is_echoed_back() -> None:
    for version in SUPPORTED_PROTOCOL_VERSIONS:
        assert negotiate_protocol_version(version) == version


@pytest.mark.parametrize("requested", [0, -1, PROTOCOL_VERSION + 1, 9999])
def test_an_unsupported_version_answers_with_our_newest(requested: int) -> None:
    """Negotiation is not a rejection point; the client decides whether we are usable."""
    assert negotiate_protocol_version(requested) == max(SUPPORTED_PROTOCOL_VERSIONS)
