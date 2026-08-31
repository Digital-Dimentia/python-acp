"""Tests for the MCP-to-ACP content mapping.

Unit tests here, and one end-to-end test in `tests/test_turn_mcp_router.py` driving the
real fixture server's `every-content` tool — because a mapping that works on hand-built
dicts and not on what a server actually sends would pass this whole file.
"""

from __future__ import annotations

from typing import Any

import pytest
from acp.schema import (
    AudioContentBlock,
    EmbeddedResourceContentBlock,
    ImageContentBlock,
    ResourceContentBlock,
    TextContentBlock,
)

from python_acp.mcp_content import (
    MAPPED_TYPES,
    to_content_block,
    to_edit_content,
    to_tool_call_content,
)


def unrendered(block: Any) -> bool:
    return isinstance(block, TextContentBlock) and block.text.startswith("[python-acp could not")


# ---------------------------------------------------------------------------
# The five types that map
# ---------------------------------------------------------------------------


def test_text_maps_to_a_text_block() -> None:
    assert to_content_block({"type": "text", "text": "hello"}).text == "hello"


def test_image_carries_its_data_and_mime_type() -> None:
    block = to_content_block({"type": "image", "data": "aGk=", "mimeType": "image/png"})

    assert isinstance(block, ImageContentBlock)
    assert (block.data, block.mime_type) == ("aGk=", "image/png")


def test_an_images_optional_uri_survives() -> None:
    block = to_content_block(
        {"type": "image", "data": "aGk=", "mimeType": "image/png", "uri": "file:///a.png"}
    )

    assert block.uri == "file:///a.png"


def test_audio_maps_even_though_mcp_added_it_later() -> None:
    """MCP added audio in `2025-03-26`; a server that sends it costs nothing to honour."""
    block = to_content_block({"type": "audio", "data": "aGk=", "mimeType": "audio/wav"})

    assert isinstance(block, AudioContentBlock)


def test_an_embedded_text_resource_maps_to_the_text_variant() -> None:
    block = to_content_block(
        {"type": "resource", "resource": {"uri": "file:///a.txt", "text": "body"}}
    )

    assert isinstance(block, EmbeddedResourceContentBlock)
    assert block.resource.text == "body"


def test_an_embedded_blob_resource_maps_to_the_blob_variant() -> None:
    """MCP gives these no `type` tag: `text` and `blob` *are* the discriminator."""
    block = to_content_block(
        {
            "type": "resource",
            "resource": {"uri": "file:///a.pdf", "blob": "aGk=", "mimeType": "application/pdf"},
        }
    )

    assert block.resource.blob == "aGk="
    assert block.resource.mime_type == "application/pdf"


def test_a_resource_link_maps_to_the_link_block() -> None:
    """ACP calls the link type `resource_link` and the embedded one `resource`; MCP uses
    the same two words for the same two things, which is a coincidence not a rule."""
    block = to_content_block(
        {"type": "resource_link", "name": "notes", "uri": "file:///n.txt", "mimeType": "text/plain"}
    )

    assert isinstance(block, ResourceContentBlock)
    assert (block.name, block.uri, block.mime_type) == ("notes", "file:///n.txt", "text/plain")


def test_the_mapped_set_is_exactly_what_the_branches_handle() -> None:
    assert MAPPED_TYPES == {"text", "image", "audio", "resource", "resource_link"}


# ---------------------------------------------------------------------------
# Annotations
# ---------------------------------------------------------------------------


def test_annotations_carry_across() -> None:
    block = to_content_block(
        {
            "type": "text",
            "text": "hi",
            "annotations": {"audience": ["user"], "priority": 0.5, "lastModified": "2026-01-01"},
        }
    )

    assert block.annotations.audience == ["user"]
    assert block.annotations.priority == 0.5
    assert block.annotations.last_modified == "2026-01-01"


def test_absent_annotations_are_silently_absent() -> None:
    """The one case where silence is right: nothing was said, so nothing is lost."""
    assert to_content_block({"type": "text", "text": "hi"}).annotations is None


def test_malformed_annotations_do_not_cost_the_content() -> None:
    """Annotations are decoration; losing the block over them would be the wrong trade."""
    block = to_content_block({"type": "text", "text": "hi", "annotations": "nonsense"})

    assert block.text == "hi"
    assert block.annotations is None


# ---------------------------------------------------------------------------
# The fallback
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("block", "because"),
    [
        ({"type": "chart", "spec": {}}, "type 'chart'"),
        ({"type": "image", "data": "aGk="}, "malformed MCP 'image'"),
        ({"type": "resource", "resource": {"uri": "file:///a"}}, "malformed MCP 'resource'"),
        ({"type": "resource", "resource": "not an object"}, "malformed MCP 'resource'"),
        ({"type": "resource_link", "uri": "file:///a"}, "malformed MCP 'resource_link'"),
        ("a bare string", "a str where a content block was expected"),
    ],
)
def test_unmappable_content_becomes_a_visible_placeholder(block: Any, because: str) -> None:
    """Skipped is what `pyacp-hnk.2` did, and it was right only while the mapping was
    known to be narrow. Once it claims to be complete, a silent skip makes a client
    render "the tool did nothing" — a wrong answer — instead of "we could not show this".
    """
    rendered = to_content_block(block)

    assert unrendered(rendered)
    assert because in rendered.text
    assert "rawOutput" in rendered.text


def test_the_placeholder_cannot_be_mistaken_for_the_tools_own_words() -> None:
    rendered = to_content_block({"type": "chart"})

    assert rendered.text.startswith("[python-acp could not render")


# ---------------------------------------------------------------------------
# Whole results
# ---------------------------------------------------------------------------


def test_a_result_with_no_content_maps_to_none_not_an_empty_list() -> None:
    """`[]` would claim the tool answered with nothing; it answered with no field."""
    assert to_tool_call_content({"isError": False}) is None
    assert to_tool_call_content({"content": [], "isError": False}) is None


def test_every_block_is_wrapped_as_tool_call_content() -> None:
    content = to_tool_call_content(
        {"content": [{"type": "text", "text": "a"}, {"type": "chart"}], "isError": False}
    )

    assert [c.type for c in content] == ["content", "content"]
    assert content[0].content.text == "a"
    assert unrendered(content[1].content)


def test_an_unmappable_block_does_not_cost_the_ones_around_it() -> None:
    content = to_tool_call_content(
        {
            "content": [
                {"type": "text", "text": "before"},
                {"type": "nope"},
                {"type": "text", "text": "after"},
            ],
            "isError": False,
        }
    )

    assert [c.content.text for c in content if not unrendered(c.content)] == ["before", "after"]


# ---------------------------------------------------------------------------
# A verified edit, which is not MCP content at all — see the module docstring
# ---------------------------------------------------------------------------


def edited() -> Any:
    from python_acp.edit_json import JSON_DIALECT
    from python_acp.edits import Op, OpKind, apply

    return apply(
        '{\n  "version": "1.0.0"\n}\n',
        [Op(kind=OpKind.SET, address="/version", scalar="2.0.0")],
        dialect=JSON_DIALECT,
        path="/work/package.json",
    )


def test_an_edit_becomes_a_whole_file_diff_and_a_readable_one() -> None:
    """`Diff.old_text`/`new_text` are contents, not a patch. Handing `unified()` to
    `new_text` typechecks and would make a client replace the file with the diff."""
    diff, readable = to_edit_content(edited())

    assert (diff.type, diff.path) == ("diff", "/work/package.json")
    assert diff.old_text == '{\n  "version": "1.0.0"\n}\n'
    assert diff.new_text == '{\n  "version": "2.0.0"\n}\n'
    assert "---" in readable.content.text


def test_the_readable_diff_is_fenced_so_its_columns_survive_markdown() -> None:
    """A bare unified diff loses its leading `-` to a list bullet — the column that
    carries the entire meaning."""
    text = to_edit_content(edited())[1].content.text

    assert text.startswith("```")
    assert text.rstrip().endswith("```")
    assert '-  "version": "1.0.0"' in text
