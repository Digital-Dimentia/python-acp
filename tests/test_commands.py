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
    """`--flag <value>` is the advice, and bare it renders as `--flag`."""
    with pytest.raises(CommandError) as refusal:
        parse_command("/invokeTool demo/echo positional")
    assert_markdown_safe(str(refusal.value))
    assert "<value>" in str(refusal.value)


# --- curly quotes ----------------------------------------------------------


def test_a_curly_quoted_value_is_refused_with_the_quote_named_as_the_cause() -> None:
    """`shlex` splits at every space, so the refusal lands four tokens past the mistake.

    `"` and `“` are near-indistinguishable at a chat font size, so naming the word `from`
    alone sends the reader looking at something they did nothing wrong with.
    """
    with pytest.raises(CommandError) as refusal:
        parse_command("/invokeTool demo/echo --text “Hello from the GUI”")

    message = str(refusal.value)
    assert "unexpected argument 'from'" in message, "the literal finding is still reported"
    assert "curly quotes" in message
    assert '`/invokeTool demo/echo --text "Hello from the GUI"`' in message, (
        "the straightened command is the actionable half"
    )
    assert_markdown_safe(message)


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
