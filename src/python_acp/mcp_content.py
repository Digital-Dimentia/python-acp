"""Translating MCP result content into ACP content. The seam between two content models.

MCP and ACP both have a notion of "a piece of content in a message", and the notions
overlap without matching. This module is the mapping, written out rather than inferred at
each call site, because the failure mode of getting it wrong is silent: a client renders
what it is given, and a block dropped or mislabelled looks like a tool that produced
nothing rather than like a bug here.

**Direction matters, and this is the outbound one.** `turn_mcp_router.py` declines
`image`, `audio`, and `resource` blocks *arriving in a prompt*, because reading them
would need a model. Sending them *out* is a different question with a different answer:
they are a tool's output, the client asked for the tool, and `promptCapabilities` governs
only what the agent reads. Nothing here needs a capability flip.

## The mapping

| MCP content | ACP block | Notes |
|---|---|---|
| `text` | `TextContentBlock` | |
| `image` | `ImageContentBlock` | `data` + `mimeType`, both required by both sides |
| `audio` | `AudioContentBlock` | MCP added audio in `2025-03-26`; mapped anyway, since a server that sends it costs nothing to honour |
| `resource` (text) | `EmbeddedResourceContentBlock` → `TextResourceContents` | |
| `resource` (blob) | `EmbeddedResourceContentBlock` → `BlobResourceContents` | |
| `resource_link` | `ResourceContentBlock` | ACP calls the link type `resource_link` and the embedded one `resource`; MCP uses the same two words for the same two things, which is a coincidence worth not relying on |
| anything else | a marked placeholder — see below | |

`annotations` (`audience`, `priority`, `lastModified`) carry across where present. They
are optional on both sides and are dropped silently when absent, which is the only case
where silence is right: an absent annotation means nothing was said.

## Unmappable content becomes a visible placeholder, not a gap

A block this module cannot map is replaced by a text block that says so, rather than
skipped.

Skipping is what `pyacp-hnk.2` did, and it is defensible only while the mapping is known
to be narrow. Once the mapping claims to be complete, a silent skip is worse: a client
rendering `content` sees a tool call that produced nothing, which reads as "the tool did
nothing" — a wrong answer — rather than "there was something here we could not show".

Nothing is lost either way: `ToolCallProgress.rawOutput` already carries the server's
result verbatim, so a client that wants the original always has it. The placeholder is
for the humans.
"""

from __future__ import annotations

import logging
from typing import Any

from acp.helpers import (
    ToolCallContentVariant,
    audio_block,
    embedded_blob_resource,
    embedded_text_resource,
    image_block,
    resource_block,
    resource_link_block,
    text_block,
    tool_content,
)
from acp.schema import Annotations

logger = logging.getLogger(__name__)

#: The MCP content `type` values this module maps. A block of any other type becomes a
#: placeholder — see the module docstring.
MAPPED_TYPES = frozenset({"text", "image", "audio", "resource", "resource_link"})


def to_content_block(block: Any) -> Any:
    """One MCP content block as an ACP content block. Never `None`.

    Returns a marked text block for anything unmappable rather than dropping it, so a
    client rendering `content` cannot mistake "we could not show this" for "the tool
    produced nothing".
    """
    if not isinstance(block, dict):
        return _placeholder(f"a {type(block).__name__} where a content block was expected")

    kind = block.get("type")
    if kind not in MAPPED_TYPES:
        return _placeholder(f"MCP content of type {kind!r}")

    try:
        mapped = _map(kind, block)
    except (KeyError, TypeError, ValueError):
        # A block of a type we know, missing what that type needs. Reported rather than
        # guessed at: a half-built ImageContentBlock is worse than a sentence saying so.
        logger.debug("Unmappable MCP %r content: %r", kind, block, exc_info=True)
        return _placeholder(f"malformed MCP {kind!r} content")

    annotations = _annotations(block.get("annotations"))
    if annotations is not None:
        mapped.annotations = annotations
    return mapped


def to_tool_call_content(result: dict[str, Any]) -> list[ToolCallContentVariant] | None:
    """A `tools/call` result's content, as ACP tool-call content.

    `None` rather than `[]` for a result with no content: the schema makes the field
    optional, and an empty list would claim the tool answered with nothing when it
    answered with no content field at all.
    """
    blocks = [tool_content(to_content_block(block)) for block in result.get("content") or []]
    return blocks or None


def _map(kind: str, block: dict[str, Any]) -> Any:
    if kind == "text":
        return text_block(_require_str(block, "text"))
    if kind == "image":
        return image_block(
            _require_str(block, "data"), _require_str(block, "mimeType"), uri=block.get("uri")
        )
    if kind == "audio":
        return audio_block(_require_str(block, "data"), _require_str(block, "mimeType"))
    if kind == "resource_link":
        return resource_link_block(
            _require_str(block, "name"),
            _require_str(block, "uri"),
            mime_type=block.get("mimeType"),
            size=block.get("size"),
            description=block.get("description"),
            title=block.get("title"),
        )
    return resource_block(_embedded(block.get("resource")))


def _embedded(resource: Any) -> Any:
    """An MCP embedded resource, which is text-or-blob and never both.

    Discriminated on which field is present rather than on a `type` tag, because MCP does
    not give these one — `text` and `blob` *are* the discriminator.
    """
    if not isinstance(resource, dict):
        raise TypeError("resource must be an object")
    uri = _require_str(resource, "uri")
    mime_type = resource.get("mimeType")
    if isinstance(resource.get("text"), str):
        return embedded_text_resource(uri, resource["text"], mime_type=mime_type)
    if isinstance(resource.get("blob"), str):
        return embedded_blob_resource(uri, resource["blob"], mime_type=mime_type)
    raise ValueError("resource carries neither text nor blob")


def _annotations(raw: Any) -> Annotations | None:
    """MCP annotations, where present.

    Absent is the one case where silence is right: nothing was said, so nothing is lost.
    A malformed block is dropped rather than raised on — annotations are decoration, and
    losing the content over them would be the wrong trade.
    """
    if not isinstance(raw, dict):
        return None
    audience = raw.get("audience")
    return Annotations(
        audience=audience if isinstance(audience, list) else None,
        priority=raw.get("priority") if isinstance(raw.get("priority"), (int, float)) else None,
        last_modified=raw.get("lastModified") if isinstance(raw.get("lastModified"), str) else None,
    )


def _require_str(block: dict[str, Any], field: str) -> str:
    value = block.get(field)
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    return value


def _placeholder(what: str) -> Any:
    """What a client sees in place of content this module could not map.

    Bracketed and prefixed so it cannot be mistaken for a tool's own words, and it names
    what was there. The server's original is still in `rawOutput`.
    """
    return text_block(f"[python-acp could not render {what}; see rawOutput]")
