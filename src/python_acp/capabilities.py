"""The compliance matrix, compiled.

`docs/acp-compliance-matrix.md` decides what `initialize` is allowed to advertise. This
module is that decision in executable form: one `Capability` row per leaf field of
`acp.schema.AgentCapabilities`, each carrying the value we advertise, the bead that owns
the flip, and why. `build_agent_capabilities()` assembles the response block from those
rows and nothing else, so the manifest is the source rather than a description of code
written elsewhere.

**Why a manifest instead of a literal block.** The capability block is a *promise*: a
`true` in it entitles a client to call something. Three failure modes are worth the
indirection.

* **Aspirational literals.** A hand-built block can be edited to `True` in the same
  keystroke as the wish. Here a flip is a manifest row, and `tests/test_capabilities.py`
  refuses any advertised capability that does not name a test proving the feature runs.
* **Silent SDK drift.** `AgentCapabilities()` already defaults to Phase 1's values, so
  building from defaults would look correct today and change meaning under us the day
  the SDK changes a default. Every field is stated here instead. The same test walks the
  SDK model and fails when a field exists that no row covers — an SDK bump that adds a
  capability is a decision we make, not one we inherit.
* **Losing the why.** A `False` with no owner is indistinguishable from an oversight.

Nothing here knows about a transport, a request, or a connection; `agent.py` calls it.

`PROTOCOL_VERSION` is the **ACP** version. It has nothing to do with the MCP
`protocolVersion` string in `mcp_stdio.py`. Two protocols, two version fields, and they
are not interchangeable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from acp import PROTOCOL_VERSION
from acp.schema import (
    AgentAuthCapabilities,
    AgentCapabilities,
    AuthMethodAgent,
    EnvVarAuthMethod,
    McpCapabilities,
    PromptCapabilities,
    SessionCapabilities,
    SessionCloseCapabilities,
    SessionForkCapabilities,
    SessionListCapabilities,
    SessionResumeCapabilities,
    TerminalAuthMethod,
)

#: Every ACP protocol version this agent can serve. A set rather than a scalar because
#: negotiation is a membership test, and the day we support two versions the shape of
#: the answer should not have to change.
SUPPORTED_PROTOCOL_VERSIONS = frozenset({PROTOCOL_VERSION})

#: Authentication methods offered at `initialize`. Empty, and not provisionally so: this
#: process runs locally as a subprocess of the client, under the user's own credentials,
#: and authenticates nobody. An empty list is the accurate statement — and it is what
#: makes `Agent.authenticate` a typed `auth_required` refusal instead of a `-32601`.
#: Owner: `pyacp-fln.1`.
AUTH_METHODS: tuple[EnvVarAuthMethod | TerminalAuthMethod | AuthMethodAgent, ...] = ()


@dataclass(frozen=True)
class Capability:
    """One leaf of the advertised capability block.

    `path` is the attribute path into `AgentCapabilities` in the SDK's Python spelling
    (`("prompt_capabilities", "image")`), not the wire spelling; `build_agent_capabilities`
    walks it and the SDK's aliases handle serialization.

    `advertised` is the literal value. For a boolean flag that is `True`/`False`; for an
    optional sub-capability it is `None` (absent) or an instance of its marker model,
    because those capabilities are advertised by *presence*, not by a boolean.
    """

    path: tuple[str, ...]
    advertised: Any
    owner: str
    why: str
    #: Whether this capability is only real on a connection built with
    #: `use_unstable_protocol=True`. Three lifecycle methods are registered
    #: `unstable=True` in the SDK's agent router, and with the flag off the router
    #: answers `method_not_found` **without calling the agent** — so advertising them on
    #: such a connection would be a promise the SDK itself refuses to keep. Both
    #: transports pass the flag; this exists so the one that does not stays honest.
    requires_unstable: bool = False

    @property
    def is_advertised(self) -> bool:
        """Whether this promises a client something.

        `False` and `None` both mean "not offered" — the schema uses a boolean for flags
        and presence for sub-capabilities — and everything else is a promise that owes
        the test suite a proof.
        """
        return self.advertised is not None and self.advertised is not False

    @property
    def name(self) -> str:
        """Dotted path, for error messages that have to be read under pressure."""
        return ".".join(self.path)


#: The block `initialize` advertises. One row per leaf; a value flips **in the same
#: commit as the feature it advertises**, never ahead of it.
#:
#: Derived from the "Consequences for the `initialize` capability block" table in
#: `docs/acp-compliance-matrix.md`. Keep the two in step: the table is the ratified
#: contract, this tuple is what the wire actually carries.
AGENT_CAPABILITY_MANIFEST: tuple[Capability, ...] = (
    Capability(
        path=("load_session",),
        advertised=True,
        owner="pyacp-3rw.3",
        why=(
            "session/load replays the session's recorded session/update notifications "
            "before returning. In-process only: a session does not survive a restart, "
            "and load on an id we no longer hold is -32602."
        ),
    ),
    Capability(
        path=("prompt_capabilities", "image"),
        advertised=False,
        owner="pyacp-hnk.3",
        why="ImageContentBlock is not handled in a prompt turn.",
    ),
    Capability(
        path=("prompt_capabilities", "audio"),
        advertised=False,
        owner="pyacp-hnk.3",
        why="AudioContentBlock is not handled in a prompt turn.",
    ),
    Capability(
        path=("prompt_capabilities", "embedded_context"),
        advertised=False,
        owner="pyacp-hnk.3",
        why="EmbeddedResourceContentBlock is not handled in a prompt turn.",
    ),
    Capability(
        path=("mcp_capabilities", "http"),
        advertised=False,
        owner="pyacp-db3",
        why=(
            "Gates the *transport* of a client-supplied MCP server, not the ability to "
            "accept one. McpServerStdio needs no capability and stdio is the only MCP "
            "transport this bridge drives (D6), so this stays false and session/new "
            "rejects HttpMcpServer entries."
        ),
    ),
    Capability(
        path=("mcp_capabilities", "sse"),
        advertised=False,
        owner="pyacp-db3",
        why="Same as mcpCapabilities.http, for SseMcpServer.",
    ),
    Capability(
        path=("mcp_capabilities", "acp"),
        advertised=False,
        owner="pyacp-db3",
        why="Same as mcpCapabilities.http, for AcpMcpServer. Also UNSTABLE in the schema.",
    ),
    Capability(
        path=("session_capabilities", "list"),
        advertised=SessionListCapabilities(),
        owner="pyacp-3rw.3",
        why="session/list reads the session registry, with keyset cursor pagination.",
    ),
    Capability(
        path=("session_capabilities", "delete"),
        advertised=None,
        owner="never",
        why=(
            "session/delete has no route and no Agent member in agent-client-protocol "
            "0.12.1. Advertising it would promise a method the SDK cannot dispatch."
        ),
    ),
    Capability(
        path=("session_capabilities", "additional_directories"),
        advertised=None,
        owner="pyacp-3rw.4",
        why=(
            "Advertising this promises the absolute-path constraint on "
            "additionalDirectories is enforced. Nothing enforces it yet."
        ),
    ),
    Capability(
        path=("session_capabilities", "fork"),
        advertised=SessionForkCapabilities(),
        owner="pyacp-3rw.3",
        requires_unstable=True,
        why=(
            "session/fork deep-copies the session and opens the fork its own MCP "
            "subprocesses. Registered unstable=True in the SDK's agent router, so it is "
            "advertised only on a connection carrying use_unstable_protocol."
        ),
    ),
    Capability(
        path=("session_capabilities", "resume"),
        advertised=SessionResumeCapabilities(),
        owner="pyacp-3rw.3",
        requires_unstable=True,
        why="Same unstable gate as sessionCapabilities.fork, for session/resume.",
    ),
    Capability(
        path=("session_capabilities", "close"),
        advertised=SessionCloseCapabilities(),
        owner="pyacp-3rw.3",
        requires_unstable=True,
        why="Same unstable gate as sessionCapabilities.fork, for session/close.",
    ),
    Capability(
        path=("auth", "logout"),
        advertised=None,
        owner="never",
        why=(
            "AUTH_METHODS is empty, so there is nothing to log out of. `logout` is also "
            "unrouted by build_agent_router in 0.12.1 — see the matrix's Consequences."
        ),
    ),
    Capability(
        path=("providers",),
        advertised=None,
        owner="never",
        why="UNSTABLE in the schema and unrouted by the SDK. Out of scope for ACP v1.",
    ),
    Capability(
        path=("nes",),
        advertised=None,
        owner="never",
        why="UNSTABLE in the schema and unrouted by the SDK. Out of scope for ACP v1.",
    ),
    Capability(
        path=("position_encoding",),
        advertised=None,
        owner="never",
        why=(
            "UNSTABLE in the schema. Declaring an encoding without honouring it in every "
            "position we emit would be worse than declaring none."
        ),
    ),
)


def build_agent_capabilities(*, unstable: bool = True) -> AgentCapabilities:
    """Assemble the `initialize` capability block from the manifest.

    Every sub-model is constructed explicitly rather than left to the SDK's field
    defaults: what we promise a client must not be able to change because a dependency
    changed a default. A fresh object each call, so a caller that mutates the response
    cannot reach into the next connection's.

    `unstable` is the connection's `use_unstable_protocol`. With it off, the rows marked
    `requires_unstable` are withheld — the SDK's router answers `method_not_found` for
    those three *without calling the agent*, so advertising them would be a promise the
    SDK itself refuses to keep. It is per-connection rather than per-process because the
    flag is.
    """
    capabilities = AgentCapabilities(
        promptCapabilities=PromptCapabilities(),
        mcpCapabilities=McpCapabilities(),
        sessionCapabilities=SessionCapabilities(),
        auth=AgentAuthCapabilities(),
    )
    for capability in AGENT_CAPABILITY_MANIFEST:
        advertised = capability.advertised
        if capability.requires_unstable and not unstable:
            advertised = None
        target: Any = capabilities
        for part in capability.path[:-1]:
            target = getattr(target, part)
        # A fresh instance per call for the marker models too: the manifest holds one
        # object per row, and handing the same one to two connections would let a client
        # that mutated its response reach into the next.
        if hasattr(advertised, "model_copy"):
            advertised = advertised.model_copy(deep=True)
        setattr(target, capability.path[-1], advertised)
    return capabilities


def negotiate_protocol_version(requested: int) -> int:
    """Answer the version this connection will speak.

    The ACP handshake is not a rejection point. An agent that supports the requested
    version echoes it; an agent that does not answers with the latest version it *does*
    support and leaves the client to decide whether that is usable — the client
    disconnects, we do not error. Returning an unsupported version as an error would
    make a recoverable mismatch fatal.
    """
    if requested in SUPPORTED_PROTOCOL_VERSIONS:
        return requested
    return max(SUPPORTED_PROTOCOL_VERSIONS)
