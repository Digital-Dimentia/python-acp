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
    COMMAND_NAMES,
    CommandError,
    InvokePrompt,
    InvokeTool,
    ListPrompts,
    ListResources,
    ListTools,
    ShowPrompt,
    ShowResource,
    coerce_arguments,
    parse_command,
    positional_argument_error,
    prompt_arguments,
    prompt_message_blocks,
    render_prompt_heading,
    render_prompt_listing,
    render_resource_contents,
    render_resource_listing,
    render_tool_listing,
)
from test_markdown import assert_markdown_safe


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


def test_a_positional_argument_is_carried_rather_than_refused_here() -> None:
    """Parsing keeps a loose token; `positional_argument_error` is what refuses it.

    The split is the whole of `pyacp-ysq`: refused here, the message could name no
    parameter but the failing token, so it advised a flag named after the reader's own
    value. The tool's real parameters are an await away, so the complaint goes there.
    """
    command = parse_command("/invokeTool demo/echo hi")
    assert isinstance(command, InvokeTool)
    assert command.positional == ("hi",)
    assert command.raw_arguments == {}
    assert command.typed_as == "/invokeTool demo/echo"


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


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("--v 7", 7, id="number-branch"),
        pytest.param("--v abc123", "abc123", id="string-branch"),
        pytest.param('--v \'{"a": 1}\'', {"a": 1}, id="json-object"),
        pytest.param("--v", True, id="bare-flag"),
    ],
)
def test_a_union_type_is_read_as_json_rather_than_coerced(text: str, expected: Any) -> None:
    """`{"type": ["string", "number"]}` is legal JSON Schema and says *either*.

    There is no single type to coerce to, so a union gets the same honest reading an
    undeclared property gets. Every value here is legal under the union, and the server
    decides which one it wanted.

    This crashed the **whole turn** before `pyacp-708`: `_scalar` asked
    `declared in {"number", "integer"}`, `x in <set>` hashes `x`, and an unhashable list
    raised `TypeError` where every other bad-argument path raises a readable
    `CommandError`. It escaped to the SDK as `-32603 Internal error`, and acp-ui reported
    it as "Failed to run tool invocation" with no way to tell which argument was at fault.
    The zoo's `zoo-types.either` existed for exactly this shape and was never *called*.
    """
    command = parse_command(f"/invokeTool t/x {text}")
    assert isinstance(command, InvokeTool)
    assert coerce_arguments(command, schema({"v": {"type": ["string", "number"]}})) == {
        "v": expected
    }


def test_a_union_type_inside_items_is_read_the_same_way() -> None:
    """The array path reaches `_scalar` too, once per element, with the `items` spec."""
    command = parse_command("/invokeTool t/x --v 1 --v two")
    assert isinstance(command, InvokeTool)
    schema_with_union = schema({"v": {"type": "array", "items": {"type": ["string", "integer"]}}})
    assert coerce_arguments(command, schema_with_union) == {"v": [1, "two"]}


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


# ---------------------------------------------------------------------------
# Prompts: recognition and arguments
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("/listPrompts", ListPrompts),
        ("listPrompts", ListPrompts),
        ("/listResources", ListResources),
        ("listResources", ListResources),
    ],
)
def test_the_other_two_listings_are_recognised_with_or_without_the_slash(
    text: str, kind: type
) -> None:
    assert isinstance(parse_command(text), kind)


@pytest.mark.parametrize("name", ["listPrompts", "listResources"])
def test_the_other_two_listings_take_no_arguments(name: str) -> None:
    with pytest.raises(CommandError, match="takes no arguments"):
        parse_command(f"/{name} --verbose")


def test_an_unbalanced_quote_is_reported_for_every_command_name() -> None:
    """The guard reads `COMMAND_NAMES`, so a command added without being put in that set
    would silently hand its own bad quoting to the JSON parser instead."""
    for name in COMMAND_NAMES:
        with pytest.raises(CommandError, match="unbalanced quote"):
            parse_command(f'/{name} demo/thing --text "unterminated')


def test_prompt_show_parses_a_server_a_name_and_string_arguments() -> None:
    command = parse_command('/promptShow demo/greeting --name "Ada Lovelace"')
    assert isinstance(command, ShowPrompt)
    assert (command.server, command.name) == ("demo", "greeting")
    assert command.raw_arguments == {"name": ["Ada Lovelace"]}


def test_prompt_invoke_parses_identically_and_is_a_distinct_command() -> None:
    """The two differ only in what happens after parsing, so they must not be one class:
    `execute` dispatches on which it got."""
    show = parse_command("/promptShow demo/greeting --name Ada")
    invoke = parse_command("/promptInvoke demo/greeting --name Ada")
    assert isinstance(invoke, InvokePrompt)
    assert not isinstance(show, InvokePrompt)
    assert (invoke.server, invoke.name, invoke.raw_arguments) == (
        show.server,
        show.name,
        show.raw_arguments,
    )
    assert (show.verb, invoke.verb) == ("promptShow", "promptInvoke")


def test_a_bare_prompt_name_leaves_the_server_for_the_executor_to_resolve() -> None:
    command = parse_command("/promptShow greeting")
    assert isinstance(command, ShowPrompt)
    assert command.server is None and command.name == "greeting"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("/promptShow", "needs a prompt"),
        ("/promptShow --name Ada", "the first argument is the prompt"),
        ("/promptShow demo/", "names no prompt"),
        ("/promptShow demo/greeting Ada", "Every argument is named"),
        ("/promptShow demo/greeting --=x", "names no argument"),
    ],
)
def test_a_malformed_prompt_command_says_which_part_is_wrong(text: str, message: str) -> None:
    with pytest.raises(CommandError, match=message):
        parse_command(text)


# --- arguments, which are strings and only strings -------------------------

GREETING = [{"name": "name", "required": True, "description": "Who to greet"},
            {"name": "style", "description": "How formal"}]


def show(*tokens: str) -> ShowPrompt:
    command = parse_command("/promptShow demo/greeting " + " ".join(tokens))
    assert isinstance(command, ShowPrompt)
    return command


def test_prompt_arguments_stay_strings_whatever_they_look_like() -> None:
    """`coerce_arguments`' whole table has no counterpart here: MCP types a prompt's
    arguments as `{[key: string]: string}`, so `3` is the *string* `"3"`."""
    command = show("--name", "3", "--style", "true")
    assert prompt_arguments(command, GREETING) == {"name": "3", "style": "true"}


def test_a_prompt_argument_with_no_value_is_refused_rather_than_read_as_true() -> None:
    """A bare `--flag` is `True` for a tool whose schema says boolean. A prompt has no
    schema and no non-string argument for one to describe."""
    with pytest.raises(CommandError, match="needs a value"):
        prompt_arguments(show("--name"), GREETING)


def test_a_prompt_argument_given_twice_is_refused_rather_than_made_a_list() -> None:
    with pytest.raises(CommandError, match="given 2 times"):
        prompt_arguments(show("--name", "Ada", "--name", "Grace"), GREETING)


def test_an_undeclared_prompt_argument_is_refused_with_the_ones_it_takes() -> None:
    with pytest.raises(CommandError, match=r"no argument --colour.*--name, --style"):
        prompt_arguments(show("--name", "Ada", "--colour", "red"), GREETING)


def test_a_missing_required_prompt_argument_is_refused() -> None:
    with pytest.raises(CommandError, match="missing required --name"):
        prompt_arguments(show("--style", "formal"), GREETING)


def test_an_optional_prompt_argument_may_simply_be_left_out() -> None:
    assert prompt_arguments(show("--name", "Ada"), GREETING) == {"name": "Ada"}


@pytest.mark.parametrize("declared", [None, [], "nonsense", [{"no": "name"}]])
def test_a_prompt_that_declares_nothing_usable_passes_everything_through(declared: Any) -> None:
    """Same latitude `coerce_arguments` gives a tool with no `inputSchema`: nothing is
    known, so nothing is checked, and the server gets to have the opinion."""
    assert prompt_arguments(show("--anything", "at-all"), declared) == {"anything": "at-all"}


# ---------------------------------------------------------------------------
# Resources: a URI, not a slash-separated pair
# ---------------------------------------------------------------------------


def test_resource_show_takes_a_bare_uri() -> None:
    command = parse_command("/resourceShow file:///etc/hosts")
    assert isinstance(command, ShowResource)
    assert (command.server, command.uri) == (None, "file:///etc/hosts")


def test_resource_show_takes_the_server_as_a_separate_token() -> None:
    """`demo/file:///etc/hosts` would be split by `rpartition('/')` at the last slash in
    the path, which is why the count is the discriminator and not the shape."""
    command = parse_command("/resourceShow demo file:///etc/hosts")
    assert isinstance(command, ShowResource)
    assert (command.server, command.uri) == ("demo", "file:///etc/hosts")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("/resourceShow", "needs a resource"),
        ("/resourceShow --uri x", "is an option"),
        ("/resourceShow demo file://a file://b", "but got 3 arguments"),
    ],
)
def test_a_malformed_resource_command_says_which_part_is_wrong(text: str, message: str) -> None:
    with pytest.raises(CommandError, match=message):
        parse_command(text)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_the_prompt_listing_names_arguments_and_which_are_required() -> None:
    text = render_prompt_listing(
        {
            "demo": [
                {"name": "greeting", "description": "Build a greeting", "arguments": GREETING},
                {"name": "bare", "description": "Takes nothing"},
            ]
        }
    )
    assert "2 prompts on 1 server." in text
    assert "demo/greeting" in text
    assert "--name" in text and "required" in text and "Who to greet" in text
    assert "--style" in text
    assert "(no arguments)" in text
    assert "/promptShow demo/greeting --name <value>" in text


def test_the_prompt_listing_says_which_servers_declared_no_prompts() -> None:
    """An omitted server is indistinguishable from an empty one, and the two want
    different reactions."""
    text = render_prompt_listing({"demo": [{"name": "greeting"}]}, undeclared=["files"])
    assert "files declares no prompts capability" in text
    assert "not asked" in text


def test_a_session_whose_servers_all_lack_prompts_says_so_rather_than_looking_empty() -> None:
    text = render_prompt_listing({}, undeclared=["files", "shell"])
    assert "none of this session's 2 MCP servers declares the prompts capability" in text


@pytest.mark.parametrize(
    ("render", "noun"),
    [(render_prompt_listing, "prompts"), (render_resource_listing, "resources")],
)
def test_an_empty_session_is_told_where_servers_come_from(render: Any, noun: str) -> None:
    text = render({})
    assert f"no MCP servers, so there are no {noun}" in text
    assert "session/new" in text and "mcpServers" in text


def test_the_resource_listing_shows_uri_name_mime_type_and_a_two_token_example() -> None:
    text = render_resource_listing(
        {
            "demo": [
                {
                    "uri": "greeting://ada",
                    "name": "greeting",
                    "mimeType": "text/plain",
                    "description": "A greeting",
                }
            ]
        }
    )
    assert "1 resource on 1 server." in text
    assert "greeting://ada" in text
    assert "greeting text/plain" in text
    assert "A greeting" in text
    # The server is a separate word, and the example is the only thing that says so.
    assert "/resourceShow demo greeting://ada" in text


def test_a_uri_template_is_shown_in_a_section_of_its_own() -> None:
    """A template is not readable as printed, so it must not sit in the readable list.

    `greeting://{name}` pasted into `/resourceShow` earns "Unknown resource" from the
    server, which reads as the template being broken rather than unexpanded. Rendering
    the two in one flat list is what invites that (`pyacp-as5`).
    """
    text = render_resource_listing(
        {"demo": [{"uri": "greeting://ada", "name": "greeting", "mimeType": "text/plain"}]},
        templates={
            "demo": [
                {
                    "uriTemplate": "greeting://{name}",
                    "name": "greeting-template",
                    "mimeType": "text/plain",
                }
            ]
        },
    )
    # Counted apart, in the summary and in the server's own header.
    assert "1 resource on 1 server." in text
    assert "1 URI template, which name" in text
    assert "demo (1 resource, 1 template)" in text
    # Under its own heading, and after the resources it is not one of.
    assert "URI templates" in text
    assert text.index("greeting://ada") < text.index("URI templates") < text.index(
        "greeting://{name}"
    )
    # And the reader is told how to turn one into something that reads.
    assert "`/resourceShow demo greeting://<name>`" in text


def test_a_server_publishing_only_templates_is_not_reported_as_empty() -> None:
    """The bug in one assertion: a filesystem server publishing `file:///{path}` and
    nothing concrete was reported as having nothing to read."""
    text = render_resource_listing(
        {"files": []}, templates={"files": [{"uriTemplate": "file:///{path}", "name": "file"}]}
    )
    assert "publishes no resources" not in text
    assert "file:///{path}" in text
    assert "files (0 resources, 1 template)" in text


def test_a_listing_with_no_templates_is_unchanged() -> None:
    """The common case pays nothing for the new section — not a heading, not a sentence."""
    listings = {"demo": [{"uri": "greeting://ada", "name": "greeting"}]}
    assert render_resource_listing(listings, templates={}) == render_resource_listing(listings)
    assert "template" not in render_resource_listing(listings)


def test_a_templated_listing_survives_a_markdown_renderer() -> None:
    """`{name}` is not the risk; the `<name>` in the note is — a renderer eats it as a tag."""
    text = render_resource_listing(
        {"demo": []},
        templates={"demo": [{"uriTemplate": "greeting://{name}", "name": "greeting-template"}]},
    )
    assert_markdown_safe(text)
    assert "<name>" in text, "the blank must still be there to be worth protecting"


def test_a_server_that_publishes_no_prompts_or_resources_is_still_named() -> None:
    assert "publishes no prompts" in render_prompt_listing({"empty": []})
    assert "publishes no resources" in render_resource_listing({"empty": []})


def test_resource_text_is_printed_and_a_blob_never_is() -> None:
    """Base64 in a chat transcript is unreadable and unbounded; the placeholder names the
    size a reader can act on instead."""
    text = render_resource_contents(
        "greeting://ada",
        {
            "contents": [
                {"uri": "greeting://ada", "mimeType": "text/plain", "text": "Hello, Ada!"},
                {"uri": "greeting://ada.png", "mimeType": "image/png", "blob": "A" * 400},
            ]
        },
    )
    assert "Hello, Ada!" in text
    assert "AAAA" not in text
    assert "[binary, about 300 bytes, not shown]" in text


@pytest.mark.parametrize(
    ("result", "message"),
    [
        ({"contents": []}, "no contents"),
        ({"contents": [{"uri": "x"}]}, "neither text nor blob"),
    ],
)
def test_a_resource_with_nothing_in_it_says_so(result: Any, message: str) -> None:
    assert message in render_resource_contents("greeting://ada", result)


def test_the_prompt_heading_counts_messages_and_an_empty_expansion_says_so() -> None:
    """An expansion with no messages emits no chunks at all, so without the count it is
    indistinguishable from a call that failed."""
    filled = render_prompt_heading(
        "demo", "greeting", {"description": "Greeting prompt", "messages": [{}, {}]}
    )
    assert "`demo/greeting` — Greeting prompt" in filled
    assert "2 messages" in filled
    assert "expanded to no messages" in render_prompt_heading("demo", "greeting", {})


def test_prompt_messages_come_back_as_role_and_block_pairs() -> None:
    """MCP types `content` as one block. A list is accepted anyway, because a server that
    sends one would otherwise have its whole message rendered as a placeholder."""
    pairs = prompt_message_blocks(
        {
            "messages": [
                {"role": "user", "content": {"type": "text", "text": "one"}},
                {"role": "assistant", "content": [{"type": "text", "text": "two"}, {"t": 3}]},
                {"content": {"type": "text", "text": "roleless"}},
                "not a message",
            ]
        }
    )
    assert [role for role, _ in pairs] == ["user", "assistant", "assistant", "unknown"]
    assert [block for _, block in pairs][0] == {"type": "text", "text": "one"}


# --- markdown safety -------------------------------------------------------
#
# Every string in this section reaches a client as an `agent_message_chunk`, and every
# real ACP client renders that field as Markdown. These assert the *property* rather than
# the text — see `tests/test_markdown.py` for why the exact-string assertions elsewhere in
# this file could not have caught `pyacp-nlv`.


def test_the_tool_listing_survives_a_markdown_renderer() -> None:
    """Types and the example placeholder are the whole content of those columns."""
    text = render_tool_listing(
        {
            "demo": [
                {"name": "echo", "inputSchema": schema({"text": {"type": "string"}}, ["text"])},
                {"name": "bare"},
            ],
            "other": [],
        }
    )
    assert_markdown_safe(text)
    assert "<string>" in text, "the type must still be there to be worth protecting"
    assert "`/invokeTool demo/echo --text <value>`" in text


def test_the_prompt_and_resource_listings_survive_a_markdown_renderer() -> None:
    prompts = render_prompt_listing(
        {"demo": [{"name": "greeting", "arguments": [{"name": "who", "required": True}]}]},
        undeclared=["quiet"],
    )
    resources = render_resource_listing(
        {"demo": [{"uri": "file:///tmp/a", "name": "a", "mimeType": "text/plain"}]},
        undeclared=["quiet"],
    )
    for text in (prompts, resources):
        assert_markdown_safe(text)
    assert "quiet declares no prompts capability" in prompts, (
        "the undeclared note belongs outside the fence, as prose about the listing"
    )


def test_the_empty_listings_survive_a_markdown_renderer() -> None:
    """The `session/new` example is JSON, and reflows into nonsense as a paragraph."""
    for text in (
        render_tool_listing({}),
        render_prompt_listing({}),
        render_resource_listing({}, undeclared=["quiet"]),
    ):
        assert_markdown_safe(text)


def test_a_resource_whose_text_is_markdown_cannot_escape_its_fence() -> None:
    """The most dangerous body there is: someone else's Markdown, containing a fence."""
    text = render_resource_contents(
        "file:///tmp/a",
        {"contents": [{"uri": "file:///tmp/a", "text": "# Title\n\n```\ncode\n```\n"}]},
    )
    assert_markdown_safe(text)
    assert "```\ncode\n```" in text, "the resource's own fence must survive verbatim"


def test_the_prompt_heading_and_empty_contents_survive_a_markdown_renderer() -> None:
    for text in (
        render_prompt_heading("demo", "greeting", {"description": "Hi", "messages": [{}]}),
        render_prompt_heading("demo", "greeting", {}),
        render_resource_contents("file:///tmp/a", {}),
    ):
        assert_markdown_safe(text)


def test_an_unnamed_argument_is_refused_with_a_rendered_shape() -> None:
    """`--flag <value>` is the advice, and the shape it renders is the *tool's*."""
    command = parse_command("/invokeTool demo/echo positional")
    assert isinstance(command, InvokeTool)
    message = str(positional_argument_error(command, schema({"text": {"type": "string"}})))
    assert_markdown_safe(message)
    assert "<string>" in message


def test_a_prompt_command_still_refuses_a_positional_argument_at_parse_time() -> None:
    """The deferral is the two tool commands' alone.

    `prompts/list` describes an argument with a name and a description and no type, so
    there is nothing a later message could add that this one cannot say now.
    """
    with pytest.raises(CommandError, match="Every argument is named"):
        parse_command("/promptShow demo/greeting Ada")


# --- a loose token, and the message that names the tool's real parameters ---
#
# `pyacp-ysq`. The parser sees a token with no `--name` in front of it and nothing else,
# so the message it used to write echoed that token back as `--foo <value>` — advice to
# name a parameter after the reader's own *value*. These assert the two halves of the fix:
# the failing token is never rendered as a flag, and what is named instead comes from the
# tool's own schema.


ECHO_SCHEMA = schema({"text": {"type": "string"}}, ["text"])


def loose(text: str) -> InvokeTool:
    command = parse_command(text)
    assert isinstance(command, InvokeTool)
    assert command.positional, "this fixture is for commands that carry a loose token"
    return command


def test_the_failing_token_is_never_offered_back_as_a_flag() -> None:
    """The defect itself, stated as a test: `foo` was a value, not a parameter name."""
    message = str(positional_argument_error(loose("/Demo/echo foo bar"), ECHO_SCHEMA))
    assert "--foo" not in message
    assert "--bar" not in message
    assert "--text <string>" in message


def test_the_example_quotes_a_value_the_shell_split() -> None:
    """`/Demo/echo foo bar` is one value that wanted quoting, which is the whole case.

    The example has to survive `parse_command`'s own `shlex.split`, so it is asserted by
    round-tripping it rather than by matching the string.
    """
    message = str(positional_argument_error(loose("/Demo/echo foo bar"), ECHO_SCHEMA))
    assert '`/Demo/echo --text "foo bar"`' in message

    replayed = parse_command('/Demo/echo --text "foo bar"')
    assert isinstance(replayed, InvokeTool)
    assert replayed.raw_arguments == {"text": ["foo bar"]}
    assert not replayed.positional


def test_the_example_is_offered_for_the_one_required_parameter() -> None:
    """Several parameters and one required: the required one is the unambiguous pick."""
    message = str(
        positional_argument_error(
            loose("/Demo/echo hi"),
            schema({"text": {"type": "string"}, "count": {"type": "integer"}}, ["text"]),
        )
    )
    assert "Try `/Demo/echo --text hi`." in message
    assert "--count <integer>" in message, "the others are still named"


def test_no_example_is_invented_when_no_parameter_is_the_obvious_one() -> None:
    """Two parameters and neither required. Picking one would be the guess
    `_resolve_server` already refuses to make about servers."""
    message = str(
        positional_argument_error(
            loose("/Demo/echo hi"),
            schema({"text": {"type": "string"}, "count": {"type": "integer"}}),
        )
    )
    assert "Try" not in message
    assert "--text <string>" in message and "--count <integer>" in message


def test_a_tool_with_no_schema_names_no_parameter_it_cannot_know() -> None:
    """A server that omits `inputSchema` has said nothing about its parameters.

    Reporting that as "takes no parameters" would invent a fact, and inventing a flag is
    the defect this bead is about, so the message says only what is true.
    """
    message = str(positional_argument_error(loose("/Demo/echo foo"), None))
    assert "no input schema" in message
    assert "--foo" not in message
    assert "`--<name> <value>`" in message


def test_a_tool_that_declares_no_parameters_says_so() -> None:
    """Distinct from the case above: an empty `properties` block is a statement."""
    message = str(positional_argument_error(loose("/Demo/wipe foo"), schema({})))
    assert "takes no parameters" in message
    assert "`foo`" in message
    assert "--foo" not in message


def test_the_message_answers_in_the_spelling_the_reader_used() -> None:
    """`/Demo/echo` and `/invokeTool Demo/echo` are one call, and a refusal that answers
    one in the other's spelling makes the reader translate before they can retry."""
    palette = str(positional_argument_error(loose("/Demo/echo hi"), ECHO_SCHEMA))
    assert palette.startswith("/Demo/echo:")
    assert "Try `/Demo/echo --text hi`." in palette

    verbose = str(positional_argument_error(loose("/invokeTool Demo/echo hi"), ECHO_SCHEMA))
    assert verbose.startswith("/invokeTool Demo/echo:")
    assert "Try `/invokeTool Demo/echo --text hi`." in verbose


def test_several_loose_tokens_are_all_reported() -> None:
    message = str(positional_argument_error(loose("/Demo/echo a b c"), ECHO_SCHEMA))
    assert "`a`, `b`, `c` are values" in message


@pytest.mark.parametrize(
    "text",
    ["/Demo/echo foo", "/Demo/echo <b>x</b>", "/Demo/echo `tick`", "/invokeTool Demo/echo a b"],
)
@pytest.mark.parametrize("shape", [ECHO_SCHEMA, None, {"type": "object", "properties": {}}])
def test_every_shape_of_the_message_is_markdown_safe(text: str, shape: Any) -> None:
    assert_markdown_safe(str(positional_argument_error(loose(text), shape)))


# --- curly quotes ----------------------------------------------------------


def test_a_curly_quoted_value_is_refused_with_the_quote_named_as_the_cause() -> None:
    """`shlex` splits at every space, so the refusal lands four tokens past the mistake.

    `"` and `“` are near-indistinguishable at a chat font size, so naming the word `from`
    alone sends the reader looking at something they did nothing wrong with.
    """
    command = parse_command("/invokeTool demo/echo --text “Hello from the GUI”")
    assert isinstance(command, InvokeTool)
    message = str(positional_argument_error(command, schema({"text": {"type": "string"}})))

    assert "`from`, `the`, `GUI”`" in message, "the literal finding is still reported"
    assert "curly quotes" in message
    assert '`/invokeTool demo/echo --text "Hello from the GUI"`' in message, (
        "the straightened command is the actionable half"
    )
    assert_markdown_safe(message)


def test_a_curly_quote_suppresses_the_example_it_would_contradict() -> None:
    """Two instructions, one of them wrong, is worse than one.

    The loose token here is `there”` — the fragment the unrecognised quote left behind —
    so an example built from it would say `--text there”`, next to a note whose whole
    point is that the reader should retype the line with straight quotes.
    """
    command = parse_command("/Demo/echo --text “hello there”")
    assert isinstance(command, InvokeTool)
    message = str(positional_argument_error(command, schema({"text": {"type": "string"}})))

    assert "Try" not in message
    assert "--text <string>" in message, "the parameter is still named"
    assert '`/Demo/echo --text "hello there"`' in message, "the note carries the fix"


def test_curly_quotes_are_not_delimiters() -> None:
    """Tokenisation is unchanged on purpose: `’` is an apostrophe far more often than a
    quote, so straightening it would corrupt ordinary text."""
    command = parse_command('/invokeTool demo/echo --text "it’s fine"')
    assert isinstance(command, InvokeTool)
    assert command.raw_arguments == {"text": ["it’s fine"]}


def test_a_command_that_parses_gets_no_curly_quote_note() -> None:
    """The note is only ever reached by a command that already failed."""
    command = parse_command('/invokeTool demo/echo --text "say “hi” twice"')
    assert isinstance(command, InvokeTool)
    assert command.raw_arguments == {"text": ["say “hi” twice"]}


def test_an_unbalanced_quote_still_reports_itself() -> None:
    """A different failure, and the curly note must not displace it."""
    with pytest.raises(CommandError) as refusal:
        parse_command('/invokeTool demo/echo --text "unclosed')
    assert "unbalanced quote" in str(refusal.value)
    assert_markdown_safe(str(refusal.value))
