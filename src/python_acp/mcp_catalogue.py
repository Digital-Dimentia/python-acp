"""The MCP servers an *operator* configured, for a client to select from.

ACP puts `mcpServers` on `session/new` because its canonical client is an editor that
**spawns** the agent over stdio: the editor holds the user's MCP configuration and hands
it to a process it just created. That is a parent configuring its own child, and it is
right.

This bridge is also deployed the other way round — a long-lived WebSocket server an
operator brings up, that clients connect to afterwards. There, requiring every client to
know a server's command line is backwards, and it is more than an inconvenience: a client
naming `command` and `args` is asking this process to execute an arbitrary binary. On a
socket that is exactly the wrong thing to accept from anyone who got past the access key.

So the operator configures a **catalogue** and the client selects from it. This module is
that catalogue and nothing else: a file in, and two things out —

* `specs()` — `McpServerStdio` recipes, the same type a client would have sent, so
  everything downstream stays one code path;
* `config_options()` — one `SessionConfigOptionBoolean` per entry, which is how the
  selection reaches the client. ACP's `select` variant is single-choice, so a set of
  booleans is what a multi-select looks like here. No extension is involved: the options
  ride `NewSessionResponse.configOptions`, `session/set_config_option` changes them, and
  `config_option_update` announces a change.

**This is not `--mcp-command` coming back** (`pyacp-sld.4`). That was *one* server, shared
by every client, with a passthrough surface that let a client drive it directly. A
catalogue holds **recipes**: every session still spawns its own subprocesses, still gets
its own isolated backends, and still tears them down when it closes. What changed is where
the recipe comes from, not how many servers there are or who owns them.

## The file

TOML is the primary format — `tomllib` is stdlib at this project's floor, and a catalogue
an operator maintains wants comments, which JSON cannot carry:

```toml
[servers.tools]
command = "python"
args = ["server.py"]
env = { LOG = "debug" }
description = "Local demo tools"   # shown beside the toggle
enabled = true                     # whether a new session starts with it on
```

JSON is accepted too, chosen by suffix, because `{"mcpServers": {...}}` is the shape every
editor and desktop app already writes and an operator should be able to paste the one they
have. A bare top-level map works as well.

**Order is the file's order**, not sorted: it is what a settings panel shows, and that is
the operator's to decide.

## Why the validation is loud

A catalogue that half-parses is worse than one that refuses. A typo'd `commmand` would
otherwise produce an entry that is advertised, toggled on, and then fails to spawn — at
which point the error names a subprocess rather than the line that was wrong. So an
unknown key is an error, every type is checked, and every message names the file, the
entry, and what was wrong with it.

A name may not contain `/`. Server identity travels as `<server>/<tool>` — that is how a
tool call is routed and the only way the palette carries which server a command came from
— so a slash in a name would split in the wrong place.
"""

from __future__ import annotations

import json
import logging
import tomllib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from acp.schema import EnvVariable, McpServerStdio, SessionConfigOptionBoolean

logger = logging.getLogger(__name__)

#: Namespace for a catalogue entry's config option id. Without it a server called
#: `announce-tools` would shadow the executor's own option, and `Session.set_config_option`
#: looks options up by id alone.
CONFIG_ID_PREFIX = "mcp/"

#: What a catalogue entry may say. Anything else is a typo worth failing on — see the
#: module docstring.
_ENTRY_KEYS = frozenset({"command", "args", "env", "description", "enabled"})


class CatalogueError(ValueError):
    """A catalogue file that cannot be trusted to mean what it says.

    Raised for a missing file, a syntax error, and every validation failure. One type
    because there is one caller — the CLI, at startup — and its answer to all of them is
    the same: refuse to start and print why.
    """


@dataclass(frozen=True)
class CatalogueEntry:
    """One configured MCP server: the recipe, plus how it is offered to a client."""

    name: str
    command: str
    args: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = ()
    description: str | None = None
    #: Whether a *new* session starts with this server on. Not whether it is running:
    #: a session's own selection is its config options, and it may differ from this.
    enabled: bool = True

    @property
    def config_id(self) -> str:
        """This entry's config option id, namespaced. See `CONFIG_ID_PREFIX`."""
        return f"{CONFIG_ID_PREFIX}{self.name}"

    def spec(self) -> McpServerStdio:
        """The recipe, in the same type a client would have sent in `session/new`.

        `env` is a list of `EnvVariable` and not optional on the wire: the SDK drops an
        entry that omits it (`pyacp-mej`), so an empty list is the right empty value.
        """
        return McpServerStdio(
            name=self.name,
            command=self.command,
            args=list(self.args),
            env=[EnvVariable(name=name, value=value) for name, value in self.env],
        )

    def config_option(self) -> SessionConfigOptionBoolean:
        """The toggle a client renders for this entry."""
        return SessionConfigOptionBoolean(
            type="boolean",
            id=self.config_id,
            name=self.name,
            description=self.description or f"MCP server {self.name!r}.",
            currentValue=self.enabled,
        )


class McpCatalogue:
    """The configured servers, in file order.

    Empty is the ordinary state and behaves as if the feature did not exist: no config
    options, no specs, nothing added to a session. That is what makes `--mcp-config`
    optional rather than a mode switch.
    """

    def __init__(self, entries: Sequence[CatalogueEntry] = ()) -> None:
        self._entries = tuple(entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self) -> Iterator[CatalogueEntry]:
        return iter(self._entries)

    def __contains__(self, name: object) -> bool:
        return any(entry.name == name for entry in self._entries)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(entry.name for entry in self._entries)

    def get(self, name: str) -> CatalogueEntry | None:
        """The entry called `name`, or `None`. Not an error: a config id that names no
        entry is an ordinary miss for a caller deciding *whether* this is a catalogue
        option at all."""
        for entry in self._entries:
            if entry.name == name:
                return entry
        return None

    def entry_for_config_id(self, config_id: str) -> CatalogueEntry | None:
        """The entry a config option id refers to, or `None` for an id that is not ours.

        The one place `CONFIG_ID_PREFIX` is taken apart, so a caller never has to know the
        namespace exists.
        """
        if not config_id.startswith(CONFIG_ID_PREFIX):
            return None
        return self.get(config_id[len(CONFIG_ID_PREFIX) :])

    def config_options(self) -> tuple[SessionConfigOptionBoolean, ...]:
        """One toggle per entry, in file order."""
        return tuple(entry.config_option() for entry in self._entries)

    def specs(self, names: Sequence[str] | None = None) -> tuple[McpServerStdio, ...]:
        """Recipes for `names`, or for every entry `enabled` by default.

        The default is what `session/new` opens for a session that expressed no
        preference; passing names is how a caller opens exactly what a session selected.
        Unknown names are skipped rather than raised on — the caller reading a session's
        own config options cannot have invented one.
        """
        if names is None:
            chosen = [entry for entry in self._entries if entry.enabled]
        else:
            wanted = set(names)
            chosen = [entry for entry in self._entries if entry.name in wanted]
        return tuple(entry.spec() for entry in chosen)


def load(path: str | Path) -> McpCatalogue:
    """Read a catalogue file. Raises `CatalogueError` for anything wrong with it.

    Format is chosen by suffix: `.json` is JSON, everything else is TOML. The file is read
    once, at startup, and never re-read — a catalogue that changed under a running process
    would leave sessions advertising toggles for servers that no longer exist.
    """
    source = Path(path)
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise CatalogueError(f"Cannot read MCP catalogue {source}: {exc}") from exc

    try:
        if source.suffix.lower() == ".json":
            document = json.loads(raw)
        else:
            document = tomllib.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise CatalogueError(f"Cannot parse MCP catalogue {source}: {exc}") from exc

    return _from_document(document, source)


def _from_document(document: Any, source: Path) -> McpCatalogue:
    """The parsed file, validated into entries.

    Three shapes reach here and all three mean the same thing: our own `[servers.<name>]`
    table, the `mcpServers` key every editor writes, and — in JSON — a bare map of names,
    so a pasted fragment works without a wrapper.
    """
    if not isinstance(document, dict):
        raise CatalogueError(f"MCP catalogue {source} must be a table, got {_kind(document)}")

    for key in ("servers", "mcpServers"):
        section = document.get(key)
        if section is not None:
            if not isinstance(section, dict):
                raise CatalogueError(
                    f"MCP catalogue {source}: {key!r} must be a table of servers, "
                    f"got {_kind(section)}"
                )
            return _entries(section, source)

    # A bare map of names. Distinguishable from a mistyped wrapper because every value of
    # a bare map is itself a table; a scalar at the top level means neither shape.
    if document and all(isinstance(value, dict) for value in document.values()):
        return _entries(document, source)
    if not document:
        logger.warning("MCP catalogue %s is empty; no servers will be offered", source)
        return McpCatalogue()
    raise CatalogueError(
        f"MCP catalogue {source} has no 'servers' or 'mcpServers' table, and is not "
        f"itself a map of server names"
    )


def _entries(section: Mapping[str, Any], source: Path) -> McpCatalogue:
    return McpCatalogue([_entry(name, body, source) for name, body in section.items()])


def _entry(name: str, body: Any, source: Path) -> CatalogueEntry:
    where = f"MCP catalogue {source}, server {name!r}"
    if not name:
        raise CatalogueError(f"MCP catalogue {source}: a server name may not be empty")
    if "/" in name:
        raise CatalogueError(
            f"{where}: a server name may not contain '/' — tool calls are routed as "
            f"'<server>/<tool>', so a slash in the name would split in the wrong place"
        )
    if not isinstance(body, dict):
        raise CatalogueError(f"{where}: must be a table, got {_kind(body)}")

    unknown = sorted(set(body) - _ENTRY_KEYS)
    if unknown:
        raise CatalogueError(
            f"{where}: unknown key(s) {unknown}; allowed: {sorted(_ENTRY_KEYS)}"
        )

    command = body.get("command")
    if not isinstance(command, str) or not command:
        raise CatalogueError(f"{where}: 'command' must be a non-empty string")

    args = body.get("args", [])
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        raise CatalogueError(f"{where}: 'args' must be a list of strings")

    env = body.get("env", {})
    if not isinstance(env, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in env.items()
    ):
        raise CatalogueError(f"{where}: 'env' must be a table of string values")

    description = body.get("description")
    if description is not None and not isinstance(description, str):
        raise CatalogueError(f"{where}: 'description' must be a string")

    enabled = body.get("enabled", True)
    if not isinstance(enabled, bool):
        raise CatalogueError(f"{where}: 'enabled' must be true or false")

    return CatalogueEntry(
        name=name,
        command=command,
        args=tuple(args),
        env=tuple(env.items()),
        description=description,
        enabled=enabled,
    )


def _kind(value: Any) -> str:
    """A type name for an error message, in the file's vocabulary rather than Python's."""
    return {
        dict: "a table",
        list: "a list",
        str: "a string",
        bool: "a boolean",
        int: "a number",
        float: "a number",
        type(None): "nothing",
    }.get(type(value), type(value).__name__)
