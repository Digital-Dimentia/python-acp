"""Tests for the operator-configured MCP catalogue.

Everything here is file in, entries out — no registry, no agent, no subprocess. The
catalogue's whole job is to fail loudly on a file that does not mean what it says, so most
of this is the failure cases: a catalogue that half-parses produces a server that is
advertised, toggled on, and only then fails to spawn, with an error naming a subprocess
rather than the line that was wrong.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from python_acp.mcp_catalogue import (
    CONFIG_ID_PREFIX,
    CatalogueEntry,
    CatalogueError,
    McpCatalogue,
    load,
)

TOML_CATALOGUE = """
[servers.tools]
command = "python"
args = ["server.py"]
env = { LOG = "debug" }
description = "Local demo tools"

[servers.notes]
command = "note-server"
enabled = false
"""


def write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text)
    return path


# ---------------------------------------------------------------------------
# Reading a good file
# ---------------------------------------------------------------------------


def test_a_toml_catalogue_loads_in_file_order(tmp_path: Path) -> None:
    """Order is the operator's: it is what a settings panel shows."""
    catalogue = load(write(tmp_path, "servers.toml", TOML_CATALOGUE))

    assert catalogue.names == ("tools", "notes")
    assert len(catalogue) == 2
    assert "tools" in catalogue


def test_every_field_survives_the_round_trip(tmp_path: Path) -> None:
    catalogue = load(write(tmp_path, "servers.toml", TOML_CATALOGUE))
    tools = catalogue.get("tools")

    assert tools is not None
    assert tools.command == "python"
    assert tools.args == ("server.py",)
    assert tools.env == (("LOG", "debug"),)
    assert tools.description == "Local demo tools"
    assert tools.enabled is True


def test_a_json_catalogue_means_the_same_thing(tmp_path: Path) -> None:
    """`{"mcpServers": {...}}` is what every editor already writes, so an operator can
    paste the config they have rather than translating it."""
    document = {
        "mcpServers": {
            "tools": {
                "command": "python",
                "args": ["server.py"],
                "env": {"LOG": "debug"},
                "description": "Local demo tools",
            },
            "notes": {"command": "note-server", "enabled": False},
        }
    }
    catalogue = load(write(tmp_path, "servers.json", json.dumps(document)))

    assert catalogue.names == ("tools", "notes")
    assert catalogue.get("tools").args == ("server.py",)  # type: ignore[union-attr]
    assert catalogue.get("notes").enabled is False  # type: ignore[union-attr]


def test_a_bare_map_of_servers_is_accepted_in_json(tmp_path: Path) -> None:
    """A pasted fragment, without the wrapper it was cut out of."""
    catalogue = load(
        write(tmp_path, "servers.json", json.dumps({"tools": {"command": "python"}}))
    )
    assert catalogue.names == ("tools",)


def test_our_own_servers_table_is_the_documented_shape(tmp_path: Path) -> None:
    catalogue = load(write(tmp_path, "s.toml", '[servers.a]\ncommand = "x"\n'))
    assert catalogue.names == ("a",)


def test_an_empty_catalogue_is_ordinary(tmp_path: Path) -> None:
    """The feature costs nothing when it is not used, which is what keeps `--mcp-config`
    optional rather than a mode switch."""
    catalogue = load(write(tmp_path, "servers.toml", ""))

    assert len(catalogue) == 0
    assert catalogue.config_options() == ()
    assert catalogue.specs() == ()


# ---------------------------------------------------------------------------
# What it produces
# ---------------------------------------------------------------------------


def test_specs_default_to_the_entries_enabled_for_a_new_session(tmp_path: Path) -> None:
    catalogue = load(write(tmp_path, "servers.toml", TOML_CATALOGUE))
    specs = catalogue.specs()

    assert [spec.name for spec in specs] == ["tools"]
    assert specs[0].command == "python"
    assert specs[0].args == ["server.py"]
    # `env` is not optional on the wire: the SDK drops an entry that omits it.
    assert [(item.name, item.value) for item in specs[0].env] == [("LOG", "debug")]


def test_specs_can_be_asked_for_exactly_what_a_session_selected(tmp_path: Path) -> None:
    catalogue = load(write(tmp_path, "servers.toml", TOML_CATALOGUE))

    assert [spec.name for spec in catalogue.specs(["notes"])] == ["notes"]
    assert [spec.name for spec in catalogue.specs([])] == []
    # A caller reading a session's own options cannot have invented a name, so an unknown
    # one is skipped rather than raised on.
    assert [spec.name for spec in catalogue.specs(["gone", "tools"])] == ["tools"]


def test_a_server_with_no_env_still_carries_an_empty_list(tmp_path: Path) -> None:
    catalogue = load(write(tmp_path, "s.toml", '[servers.a]\ncommand = "x"\n'))
    assert catalogue.specs()[0].env == []


def test_config_options_are_namespaced_so_they_cannot_shadow_the_executors(
    tmp_path: Path,
) -> None:
    """`Session.set_config_option` looks options up by id alone, so a server called
    `announce-tools` would otherwise take over the executor's own toggle."""
    catalogue = load(write(tmp_path, "s.toml", '[servers."announce-tools"]\ncommand = "x"\n'))
    (option,) = catalogue.config_options()

    assert option.id == f"{CONFIG_ID_PREFIX}announce-tools"
    assert option.id != "announce-tools"
    assert option.type == "boolean"


def test_a_config_option_carries_the_entrys_description_and_default(
    tmp_path: Path,
) -> None:
    catalogue = load(write(tmp_path, "servers.toml", TOML_CATALOGUE))
    tools, notes = catalogue.config_options()

    assert tools.name == "tools"
    assert tools.description == "Local demo tools"
    assert tools.current_value is True
    assert notes.current_value is False
    # A description is optional in the file; the option still says something useful.
    assert notes.description and "notes" in notes.description


def test_an_id_maps_back_to_its_entry_and_a_foreign_one_does_not(tmp_path: Path) -> None:
    catalogue = load(write(tmp_path, "servers.toml", TOML_CATALOGUE))

    entry = catalogue.entry_for_config_id(f"{CONFIG_ID_PREFIX}tools")
    assert entry is not None and entry.name == "tools"
    # The executor's own options must read as "not ours" rather than as a miss.
    assert catalogue.entry_for_config_id("announce-tools") is None
    assert catalogue.entry_for_config_id(f"{CONFIG_ID_PREFIX}gone") is None


# ---------------------------------------------------------------------------
# Refusing a file that does not mean what it says
# ---------------------------------------------------------------------------


def test_a_missing_file_says_so_rather_than_raising_oserror(tmp_path: Path) -> None:
    with pytest.raises(CatalogueError) as excinfo:
        load(tmp_path / "nope.toml")
    assert "nope.toml" in str(excinfo.value)


def test_a_syntax_error_names_the_file(tmp_path: Path) -> None:
    path = write(tmp_path, "servers.toml", "[servers.a\ncommand =")
    with pytest.raises(CatalogueError) as excinfo:
        load(path)
    assert str(path) in str(excinfo.value)


def test_a_name_with_a_slash_is_refused(tmp_path: Path) -> None:
    """`<server>/<tool>` is how a call is routed and how the palette carries server
    identity, so a slash in the name would split in the wrong place."""
    path = write(tmp_path, "s.toml", '[servers."a/b"]\ncommand = "x"\n')
    with pytest.raises(CatalogueError) as excinfo:
        load(path)
    assert "'/'" in str(excinfo.value)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ('[servers.a]\nargs = ["x"]\n', "'command'"),
        ('[servers.a]\ncommand = ""\n', "'command'"),
        ('[servers.a]\ncommand = 3\n', "'command'"),
        ('[servers.a]\ncommand = "x"\nargs = "one"\n', "'args'"),
        ('[servers.a]\ncommand = "x"\nargs = [1]\n', "'args'"),
        ('[servers.a]\ncommand = "x"\nenv = ["A=b"]\n', "'env'"),
        ('[servers.a]\ncommand = "x"\ndescription = 3\n', "'description'"),
        ('[servers.a]\ncommand = "x"\nenabled = "yes"\n', "'enabled'"),
    ],
)
def test_every_field_is_type_checked(tmp_path: Path, body: str, expected: str) -> None:
    with pytest.raises(CatalogueError) as excinfo:
        load(write(tmp_path, "servers.toml", body))
    message = str(excinfo.value)
    assert expected in message
    assert "server 'a'" in message


def test_a_typo_is_an_error_rather_than_a_shrug(tmp_path: Path) -> None:
    """The case this rule exists for: `commmand` would otherwise produce an entry that is
    advertised, toggled on, and only then fails to spawn."""
    with pytest.raises(CatalogueError) as excinfo:
        load(write(tmp_path, "s.toml", '[servers.a]\ncommmand = "x"\n'))
    assert "commmand" in str(excinfo.value)


def test_an_entry_that_is_not_a_table_is_refused(tmp_path: Path) -> None:
    with pytest.raises(CatalogueError) as excinfo:
        load(write(tmp_path, "s.json", json.dumps({"mcpServers": {"a": "python"}})))
    assert "a string" in str(excinfo.value)


def test_a_document_that_is_neither_shape_is_refused(tmp_path: Path) -> None:
    with pytest.raises(CatalogueError) as excinfo:
        load(write(tmp_path, "s.json", json.dumps({"port": 8765})))
    assert "mcpServers" in str(excinfo.value)


def test_a_servers_key_that_is_not_a_table_is_refused(tmp_path: Path) -> None:
    with pytest.raises(CatalogueError) as excinfo:
        load(write(tmp_path, "s.json", json.dumps({"mcpServers": ["tools"]})))
    assert "a list" in str(excinfo.value)


# ---------------------------------------------------------------------------
# The empty catalogue, constructed rather than read
# ---------------------------------------------------------------------------


def test_a_catalogue_can_be_built_without_a_file() -> None:
    """What every caller that was given no `--mcp-config` holds."""
    catalogue = McpCatalogue()

    assert len(catalogue) == 0
    assert catalogue.names == ()
    assert catalogue.get("anything") is None
    assert catalogue.entry_for_config_id(f"{CONFIG_ID_PREFIX}anything") is None


def test_an_entry_builds_its_own_spec_and_option() -> None:
    entry = CatalogueEntry(name="a", command="x", args=("--flag",), env=(("K", "v"),))

    assert entry.config_id == f"{CONFIG_ID_PREFIX}a"
    assert entry.spec().args == ["--flag"]
    assert entry.config_option().current_value is True
