"""Unit coverage for the command layer: parsing, typing, and rendering.

`test_turn_mcp_router.py` drives these through a real turn against a real MCP subprocess,
which is what proves they are wired up. These are the table: one case per row of the
coercion rules in `commands.md`, where an end-to-end test per row would be all setup and
no signal.
"""

from __future__ import annotations

from typing import Any

import pytest

from python_acp.commands import (
    CommandError,
    InvokeTool,
    ListTools,
    coerce_arguments,
    parse_command,
    render_tool_listing,
)


def schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or []}


# --- recognition -----------------------------------------------------------


@pytest.mark.parametrize("text", ["/tools", "tools", "  /tools  "])
def test_the_listing_command_is_recognised_with_or_without_the_slash(text: str) -> None:
    assert isinstance(parse_command(text), ListTools)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "please list the tools",
        '{"tool": "echo"}',
        "/toolsx",
        "/invoke",
    ],
)
def test_anything_else_is_not_a_command_and_falls_through(text: str) -> None:
    """Returning None rather than raising is what keeps the layer additive: every prompt
    that worked before still takes the JSON path."""
    assert parse_command(text) is None


def test_the_listing_command_takes_no_arguments() -> None:
    with pytest.raises(CommandError, match="takes no arguments"):
        parse_command("/tools --verbose")


def test_an_unbalanced_quote_is_reported_only_for_our_own_commands() -> None:
    """JSON with a stray quote gets json's message, which is better than ours."""
    with pytest.raises(CommandError, match="unbalanced quote"):
        parse_command('/invokeTool demo/echo --text "hello')
    assert parse_command('{"tool": "echo}') is None


# --- parsing ---------------------------------------------------------------


def test_a_qualified_target_splits_into_server_and_tool() -> None:
    command = parse_command("/invokeTool demo/echo --text hi")
    assert isinstance(command, InvokeTool)
    assert (command.server, command.tool) == ("demo", "echo")
    assert command.raw_arguments == {"text": ["hi"]}


def test_an_unqualified_target_leaves_the_server_for_the_session_to_settle() -> None:
    command = parse_command("/invokeTool echo --text hi")
    assert isinstance(command, InvokeTool)
    assert command.server is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/invokeTool t/x --a 1 --b 2", {"a": ["1"], "b": ["2"]}),
        ("/invokeTool t/x --a=1", {"a": ["1"]}),
        ('/invokeTool t/x --a "two words"', {"a": ["two words"]}),
        ("/invokeTool t/x --a one --a two", {"a": ["one", "two"]}),
        ("/invokeTool t/x --flag", {"flag": []}),
    ],
)
def test_the_argument_forms(text: str, expected: dict[str, list[str]]) -> None:
    command = parse_command(text)
    assert isinstance(command, InvokeTool)
    assert command.raw_arguments == expected


def test_a_positional_argument_is_refused_with_the_named_form() -> None:
    with pytest.raises(CommandError, match="Every parameter is named"):
        parse_command("/invokeTool demo/echo hi")


def test_the_tool_is_required() -> None:
    with pytest.raises(CommandError, match="needs a tool"):
        parse_command("/invokeTool")


# --- typing, one case per row of the table in commands.md ------------------


@pytest.mark.parametrize(
    ("declared", "text", "expected"),
    [
        ("string", "--v 3", "3"),
        ("string", "--v hello", "hello"),
        ("integer", "--v 3", 3),
        ("number", "--v 1.5", 1.5),
        ("boolean", "--v true", True),
        ("boolean", "--v no", False),
        ("boolean", "--v", True),
        ("array", "--v one --v two", ["one", "two"]),
        ("array", '--v \'["one"]\'', ["one"]),
        ("object", '--v \'{"a": 1}\'', {"a": 1}),
    ],
)
def test_a_declared_type_is_honoured(declared: str, text: str, expected: Any) -> None:
    command = parse_command(f"/invokeTool t/x {text}")
    assert isinstance(command, InvokeTool)
    assert coerce_arguments(command, schema({"v": {"type": declared}})) == {"v": expected}


@pytest.mark.parametrize(
    ("text", "expected"),
    [("--v 3", 3), ("--v hello", "hello"), ("--v true", True), ("--v 1.5", 1.5)],
)
def test_an_undeclared_property_is_read_as_json_and_kept_as_text_when_that_fails(
    text: str, expected: Any
) -> None:
    """The one row that guesses. A tool wanting the string "3" for a property it never
    declared cannot be reached this way, which is the documented cost of no schema."""
    command = parse_command(f"/invokeTool t/x {text}")
    assert isinstance(command, InvokeTool)
    assert coerce_arguments(command, None) == {"v": expected}


@pytest.mark.parametrize(
    ("declared", "text", "match"),
    [
        ("integer", "--v hello", "is not one"),
        ("boolean", "--v maybe", "neither true nor false"),
        ("string", "--v", "needs a value"),
        ("object", "--v notjson", "takes JSON"),
    ],
)
def test_a_value_the_type_cannot_take_is_refused(declared: str, text: str, match: str) -> None:
    command = parse_command(f"/invokeTool t/x {text}")
    assert isinstance(command, InvokeTool)
    with pytest.raises(CommandError, match=match):
        coerce_arguments(command, schema({"v": {"type": declared}}))


def test_an_undeclared_parameter_is_refused_with_the_ones_that_exist() -> None:
    command = parse_command("/invokeTool t/x --txet hi")
    assert isinstance(command, InvokeTool)
    with pytest.raises(CommandError) as caught:
        coerce_arguments(command, schema({"text": {"type": "string"}}))
    assert "--txet" in str(caught.value) and "--text" in str(caught.value)


def test_a_missing_required_parameter_is_refused_before_the_call() -> None:
    command = parse_command("/invokeTool t/x")
    assert isinstance(command, InvokeTool)
    with pytest.raises(CommandError, match="missing required --text"):
        coerce_arguments(command, schema({"text": {"type": "string"}}, ["text"]))


def test_a_schema_with_no_declared_properties_accepts_anything() -> None:
    """A server that publishes an open schema is taken at its word rather than second-
    guessed: refusing here would make this command stricter than the server itself."""
    command = parse_command("/invokeTool t/x --anything 1")
    assert isinstance(command, InvokeTool)
    assert coerce_arguments(command, schema({})) == {"anything": 1}


# --- rendering -------------------------------------------------------------


def test_the_listing_names_every_tool_its_parameters_and_which_are_required() -> None:
    text = render_tool_listing(
        {
            "demo": [
                {
                    "name": "echo",
                    "description": "Echoes text",
                    "inputSchema": schema(
                        {"text": {"type": "string", "description": "What to say"}}, ["text"]
                    ),
                },
                {"name": "ping", "description": "", "inputSchema": schema({})},
            ]
        }
    )
    assert "demo/echo" in text
    assert "Echoes text" in text
    assert "--text" in text and "<string>" in text and "required" in text
    assert "What to say" in text
    assert "(no parameters)" in text, "a tool that takes nothing should say so"
    assert "\n" in text, "the answer is multi-line by design"


def test_the_listing_ends_with_an_example_built_from_this_session() -> None:
    """A generic <server>/<tool> teaches the shape and not the vocabulary."""
    text = render_tool_listing(
        {"demo": [{"name": "echo", "inputSchema": schema({"text": {"type": "string"}}, ["text"])}]}
    )
    assert "/invokeTool demo/echo --text <value>" in text


def test_an_empty_session_is_told_how_to_get_servers_rather_than_shown_nothing() -> None:
    text = render_tool_listing({})
    assert "no MCP servers" in text
    assert "session/new" in text
    assert "mcpServers" in text


def test_a_server_that_publishes_no_tools_is_still_named() -> None:
    """Silence would read as "the server is missing" rather than "it offers nothing"."""
    text = render_tool_listing({"empty": []})
    assert "empty" in text
    assert "publishes no tools" in text
