import json
import os
import signal
import sys
import time

# Shutdown-behaviour knobs. A well-behaved MCP server exits when its stdin
# reaches EOF; these two make it misbehave on purpose so the client's
# close-stdin -> SIGTERM -> SIGKILL escalation can be exercised end to end.
IGNORE_EOF = bool(os.environ.get("MOCK_MCP_IGNORE_EOF"))
if os.environ.get("MOCK_MCP_IGNORE_SIGTERM"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)

# Emit a burst of stderr before serving anything. With no drain on the client
# side this fills the OS pipe buffer and blocks the server mid-write, before it
# can answer a single request.
_stderr_bytes = int(os.environ.get("MOCK_MCP_STDERR_BYTES", "0"))
if _stderr_bytes:
    _written = 0
    while _written < _stderr_bytes:
        _written += sys.stderr.write("mock-mcp noise " + "x" * 240 + "\n")
    sys.stderr.flush()


# Cursor pagination knobs. Default is a single page with no nextCursor, which
# is what every pre-existing test expects.
#   MOCK_MCP_LIST_PAGES=N  -> serve N pages; nextCursor is absent on the last.
#   MOCK_MCP_LIST_STUCK=1  -> always hand back the same nextCursor, forever.
#   MOCK_MCP_LIST_EMPTY_MIDDLE=1 -> page 0 carries no items but does carry a
#                            nextCursor, proving an empty page is not a terminator.
_list_pages = int(os.environ.get("MOCK_MCP_LIST_PAGES", "1"))
_list_stuck = os.environ.get("MOCK_MCP_LIST_STUCK") == "1"
_list_empty_middle = os.environ.get("MOCK_MCP_LIST_EMPTY_MIDDLE") == "1"
# Adds `wipe`, `patch`, and `plain` beside `echo` on the first tools/list page, so the
# annotation -> ACP kind mapping has a destructive tool, an additive one, and one that
# says nothing. Opt-in: every other test expects this server to offer exactly one tool.
_annotated_tools = os.environ.get("MOCK_MCP_ANNOTATED_TOOLS") == "1"
# Adds the schema zoo (see SCHEMA_ZOO below) to the first tools/list page: one tool per
# JSON Schema construct, so a client rendering a form from `AvailableCommand._meta` has
# something worth rendering. Opt-in for the same reason as the line above -- every
# unrelated test expects this server to offer exactly one tool.
_schema_zoo = os.environ.get("MOCK_MCP_SCHEMA_ZOO") == "1"


# Protocol-version negotiation knobs. Default echoes back whatever the client
# proposed, which is what a server that supports the proposal must do.
#   MOCK_MCP_PROTOCOL_VERSION=<v> -> answer with <v> no matter what was
#                            proposed: the counter-offer a server makes when it
#                            cannot speak the client's revision.
#   MOCK_MCP_OMIT_PROTOCOL_VERSION=1 -> leave protocolVersion out of the result
#                            entirely, which the lifecycle forbids.
_protocol_version = os.environ.get("MOCK_MCP_PROTOCOL_VERSION")
_omit_protocol_version = os.environ.get("MOCK_MCP_OMIT_PROTOCOL_VERSION") == "1"

# Which primitives this server declares in its initialize capability block. All three
# by default. A comma-separated MOCK_MCP_CAPABILITIES narrows it — `tools` alone is the
# large population of real MCP servers that publish no prompts and no resources, and it
# is the case a client has to read the block to distinguish from "declared, and empty".
# The methods themselves keep answering either way, on purpose: a client that ignores
# the block and asks anyway must be caught by the *client's* check, not by the fixture
# refusing to reply.
_capabilities = os.environ.get("MOCK_MCP_CAPABILITIES", "tools,prompts,resources")
_capabilities = {name.strip() for name in _capabilities.split(",") if name.strip()}


# Cancellation knobs. A stalled request is read and then never answered, so the
# only thing that ends the client's wait is its own request_timeout — after which
# notifications/cancelled should arrive. Both are recorded here and handed back by
# the `cancel-report` tool, so a test observes what this process really received
# instead of spying on the client.
#   MOCK_MCP_STALL_INITIALIZE=1 -> stall the handshake too, which is the one
#                            request a client MUST NOT cancel.
_stall_initialize = os.environ.get("MOCK_MCP_STALL_INITIALIZE") == "1"

# MOCK_MCP_NO_TEMPLATES=1 -> still declare `resources`, but answer `-32601` for
# `resources/templates/list` by falling through to the unknown-method branch. Templates
# are optional within the capability, so this is a conforming server and not a broken
# one: a client must show its concrete resources rather than failing the listing.
_no_templates = os.environ.get("MOCK_MCP_NO_TEMPLATES") == "1"
_stalled_ids = []
_cancellations = []

# Replies to server-initiated requests, in arrival order. `provoke-detached` sends
# a request and does not wait for its answer, so this is where the answer is kept
# until a test asks for it with `provoke-report`.
_server_replies = []

# The params of the initialize request as they actually arrived, handed back by the
# `handshake-report` tool. A capability block is a promise made on the wire, so a test
# that wants to check it should read what this process received rather than the
# attribute the client was constructed with.
_initialize_params = None


def write(payload):
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def page_index(req):
    """Recover the requested page number from the opaque cursor we minted."""
    cursor = (req.get("params") or {}).get("cursor")
    if not isinstance(cursor, str) or not cursor.startswith("page-"):
        return 0
    try:
        return int(cursor[len("page-") :])
    except ValueError:
        return 0


def list_result(req, key, item_for_page):
    """Build one page of a cursor-paginated list result."""
    index = page_index(req)
    if _list_empty_middle and index == 0:
        items = []
    else:
        items = [item_for_page(index)]
    # An item factory may hand back several items for one page: `tool_for_page` does,
    # when MOCK_MCP_ANNOTATED_TOOLS asks for the annotated ones.
    items = [entry for item in items for entry in (item if isinstance(item, list) else [item])]
    result = {key: items}
    if _list_stuck:
        result["nextCursor"] = "stuck"
    elif index + 1 < _list_pages:
        result["nextCursor"] = "page-%d" % (index + 1)
    return result


# ---------------------------------------------------------------------------
# The schema zoo (MOCK_MCP_SCHEMA_ZOO=1)
# ---------------------------------------------------------------------------
# `every-content` serves every MCP content type in one result so the content mapping is
# exercised against a real server. This is its counterpart for the other direction: every
# JSON Schema construct a tool can publish, so the schema this bridge forwards on
# `AvailableCommand._meta` (`pyacp-ma2`) can be rendered, mis-rendered and fixed against
# something real. `pyacp-6kz`.
#
# One tool per concern rather than one tool carrying everything. A kitchen-sink schema
# renders as a single enormous form, and when it renders wrong nothing says which
# construct broke.
#
# Every zoo tool answers `tools/call` by echoing its arguments back as JSON. That is the
# round trip worth having: it shows the JSON *types* the client's form and this bridge's
# `coerce_arguments` actually produced, so `--count 3` arriving as `3` rather than `"3"`
# is visible rather than assumed.
SCHEMA_ZOO = [
    # --- Every JSON type, bare. The floor: no constraints, nothing to infer from. ---
    {
        "name": "zoo-types",
        "description": "One property of every JSON type, with no constraints",
        "inputSchema": {
            "type": "object",
            "properties": {
                "a_string": {"type": "string", "title": "A string"},
                "a_number": {"type": "number", "title": "A number"},
                "an_integer": {"type": "integer", "title": "An integer"},
                "a_boolean": {"type": "boolean", "title": "A boolean"},
                "an_array": {"type": "array", "items": {"type": "string"}, "title": "An array"},
                "an_object": {"type": "object", "title": "An object"},
                "a_null": {"type": "null", "title": "A null"},
                # No `type` at all. A client has nothing to pick a widget from and must
                # fall back to free text rather than guessing string.
                "untyped": {"title": "Untyped", "description": "Declares no type"},
                # Two types at once, which JSON Schema allows and most form builders do
                # not expect.
                "either": {"type": ["string", "number"], "title": "String or number"},
            },
        },
    },
    # --- String constraints and formats. ---
    {
        "name": "zoo-strings",
        "description": "String constraints and the formats a client may specialise",
        "inputSchema": {
            "type": "object",
            "properties": {
                "short": {
                    "type": "string",
                    "title": "Short",
                    "description": "Between 2 and 8 characters",
                    "minLength": 2,
                    "maxLength": 8,
                },
                "slug": {
                    "type": "string",
                    "title": "Slug",
                    "description": "Lowercase letters, digits and hyphens",
                    "pattern": "^[a-z0-9-]+$",
                },
                "email": {"type": "string", "format": "email", "title": "Email"},
                "date": {"type": "string", "format": "date", "title": "Date"},
                "when": {"type": "string", "format": "date-time", "title": "Timestamp"},
                "where": {"type": "string", "format": "uri", "title": "URI"},
                "id": {"type": "string", "format": "uuid", "title": "UUID"},
                # A format nobody has heard of. It must degrade to a plain text box, not
                # be dropped and not be refused.
                "odd": {"type": "string", "format": "x-not-a-real-format", "title": "Odd"},
            },
            "required": ["slug"],
        },
    },
    # --- Numeric constraints. ---
    {
        "name": "zoo-numbers",
        "description": "Numeric bounds, steps, and integer against number",
        "inputSchema": {
            "type": "object",
            "properties": {
                "percent": {
                    "type": "integer",
                    "title": "Percent",
                    "description": "Inclusive bounds, so 0 and 100 are both legal",
                    "minimum": 0,
                    "maximum": 100,
                    "default": 50,
                },
                "ratio": {
                    "type": "number",
                    "title": "Ratio",
                    "description": "Exclusive bounds, so 0 and 1 are both illegal",
                    "exclusiveMinimum": 0,
                    "exclusiveMaximum": 1,
                },
                "step": {
                    "type": "number",
                    "title": "Step",
                    "description": "Multiples of 0.25 only",
                    "multipleOf": 0.25,
                },
                # Unbounded below and above: a slider is the wrong widget here, and a
                # client that renders one anyway is caught by this.
                "offset": {"type": "integer", "title": "Offset"},
            },
        },
    },
    # --- The three ways to spell a choice. A client gets them wrong independently. ---
    {
        "name": "zoo-choices",
        "description": "Enums three ways: bare, labelled, and as oneOf/const",
        "inputSchema": {
            "type": "object",
            "properties": {
                "colour": {
                    "type": "string",
                    "title": "Colour",
                    "description": "A bare enum: the value is the label",
                    "enum": ["red", "green", "blue"],
                },
                "priority": {
                    "type": "string",
                    "title": "Priority",
                    "description": "enum + enumNames: show the name, send the value",
                    "enum": ["P0", "P1", "P2", "P3"],
                    "enumNames": ["Critical", "High", "Normal", "Low"],
                    "default": "P2",
                },
                "mode": {
                    "title": "Mode",
                    "description": "oneOf with const and title -- the same idea, spelt "
                    "the way a JSON Schema generator emits it",
                    "oneOf": [
                        {"const": "fast", "title": "Fast"},
                        {"const": "thorough", "title": "Thorough"},
                    ],
                },
                # A single legal value. Not a dropdown -- there is nothing to choose.
                "version": {"type": "string", "const": "v1", "title": "Version"},
                # Enum over a non-string type, which a client that assumed strings will
                # send back as "1" and break.
                "retries": {"type": "integer", "enum": [0, 1, 3, 5], "title": "Retries"},
            },
            "required": ["colour"],
        },
    },
    # --- Arrays. ---
    {
        "name": "zoo-arrays",
        "description": "Arrays of scalars, of enums, and of objects",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tags": {
                    "type": "array",
                    "title": "Tags",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 5,
                },
                "kinds": {
                    "type": "array",
                    "title": "Kinds",
                    "description": "A multi-select: an array whose items are an enum",
                    "items": {"type": "string", "enum": ["code", "docs", "config", "tests"]},
                },
                "counts": {"type": "array", "title": "Counts", "items": {"type": "integer"}},
                "people": {
                    "type": "array",
                    "title": "People",
                    "description": "An array of objects -- a repeating sub-form",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "title": "Name"},
                            "age": {"type": "integer", "title": "Age", "minimum": 0},
                        },
                        "required": ["name"],
                    },
                },
                # No `items`. The array's contents are unconstrained, and a client has
                # nothing to build a row editor from.
                "anything": {"type": "array", "title": "Anything"},
            },
        },
    },
    # --- Required, optional, defaulted, and conditionally required. ---
    {
        "name": "zoo-required",
        "description": "Required against optional against defaulted, plus dependentRequired",
        "inputSchema": {
            "type": "object",
            "properties": {
                "must": {"type": "string", "title": "Must", "description": "Required"},
                "may": {"type": "string", "title": "May", "description": "Optional"},
                "filled": {
                    "type": "string",
                    "title": "Filled",
                    "description": "Optional, but pre-filled from `default`",
                    "default": "already here",
                },
                "card": {"type": "string", "title": "Card number"},
                "expiry": {"type": "string", "title": "Expiry"},
            },
            "required": ["must"],
            # Naming `card` makes `expiry` required too. This is the one conditional
            # construct acp-ui does render, so it is here rather than with the declined
            # four below.
            "dependentRequired": {"card": ["expiry"]},
        },
    },
    # --- Nesting. ---
    {
        "name": "zoo-nested",
        "description": "Objects inside objects, two deep",
        "inputSchema": {
            "type": "object",
            "properties": {
                "label": {"type": "string", "title": "Label"},
                "server": {
                    "type": "object",
                    "title": "Server",
                    "properties": {
                        "host": {"type": "string", "title": "Host", "default": "localhost"},
                        "port": {
                            "type": "integer",
                            "title": "Port",
                            "minimum": 1,
                            "maximum": 65535,
                        },
                        "tls": {
                            "type": "object",
                            "title": "TLS",
                            "properties": {
                                "enabled": {"type": "boolean", "title": "Enabled"},
                                "ca": {"type": "string", "title": "CA path"},
                            },
                        },
                    },
                    "required": ["port"],
                },
            },
            "required": ["server"],
        },
    },
    # --- The four constructs a client is expected to DECLINE. -------------------
    # Each is its own tool so each fallback can be looked at separately. A client that
    # renders a subset of a conditional schema shows the user a form that is confidently
    # wrong, which is worse than the plain text box it should fall back to.
    {
        "name": "zoo-if-then-else",
        "description": "Conditional: `kind: advanced` makes `tuning` required",
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["simple", "advanced"]},
                "tuning": {"type": "string", "title": "Tuning"},
            },
            "if": {"properties": {"kind": {"const": "advanced"}}},
            "then": {"required": ["tuning"]},
            "else": {"properties": {"tuning": False}},
            "required": ["kind"],
        },
    },
    {
        "name": "zoo-dependent-schemas",
        "description": "Conditional: naming `billing` pulls in a whole sub-schema",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "billing": {"type": "string"}},
            "dependentSchemas": {
                "billing": {
                    "properties": {"address": {"type": "string"}},
                    "required": ["address"],
                }
            },
            "required": ["name"],
        },
    },
    # NOTE these next two publish no top-level `properties` at all -- theirs live inside
    # the composition keyword. `tool_command_hint` therefore says "(no parameters)" for
    # both, which is the whole argument for `_meta` in one line: the hint can only
    # describe what it can walk, and a client with the schema can see two parameters the
    # hint could not mention. Not a bug in either place; keep it that way.
    {
        "name": "zoo-all-of",
        "description": "Composed: two schemas intersected with allOf",
        "inputSchema": {
            "type": "object",
            "allOf": [
                {"properties": {"a": {"type": "string"}}, "required": ["a"]},
                {"properties": {"b": {"type": "integer"}}},
            ],
        },
    },
    {
        "name": "zoo-one-of",
        "description": "Discriminated union: exactly one branch applies",
        "inputSchema": {
            "type": "object",
            "oneOf": [
                {
                    "title": "By id",
                    "properties": {"id": {"type": "integer"}},
                    "required": ["id"],
                },
                {
                    "title": "By name",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                },
            ],
        },
    },
    # --- The two edges, which are the ones this repo's own code claims to handle. ---
    {
        "name": "zoo-empty",
        "description": "Publishes an empty property block",
        # `properties: {}` is a *statement*: this tool takes no parameters. Not the same
        # as the tool below, and `commands.py` says so in its error messages.
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "zoo-silent",
        "description": "Publishes no inputSchema at all",
        # DELIBERATELY OFF-SPEC: MCP's `Tool` declares `inputSchema` required. Servers in
        # the wild omit it anyway, and this repo already distinguishes "said nothing" from
        # "said it takes none" -- `_tool_meta` omits the `_meta` key rather than sending
        # `"inputSchema": null`, and `commands.py` writes a different error for each. That
        # branch was reachable only from a unit test until this tool existed.
    },
]


# ---------------------------------------------------------------------------
# The prompt and resource zoo (MOCK_MCP_SCHEMA_ZOO=1)
# ---------------------------------------------------------------------------
# The same flag, the same reasoning, the other two primitives. `/listPrompts`,
# `/promptShow`, `/listResources` and `/resourceShow` are as much client-rendered surface
# as a tool's form is, and the baseline fixture publishes exactly one prompt, one
# resource and one template -- enough to prove the plumbing works, not enough to find out
# what a renderer does with an argument it must ask for, a blob it must not print, a body
# that opens a fence of its own, or a read whose answer changes.
#
# One entry per concern, as with the tools above. Every name and URI carries the `zoo`
# mark, so a listing shows at a glance what the flag added.
ZOO_PROMPTS = [
    {
        "name": "zoo-prompt-bare",
        "description": "Takes no arguments; expands to a single user message",
        # `arguments: []` is a statement -- this prompt asks for nothing -- and it is the
        # case a client must not render an argument form for.
        "arguments": [],
    },
    {
        "name": "zoo-prompt-arguments",
        "description": "One required argument and one optional one",
        # MCP types prompt arguments as `{[key: string]: string}`: there is no schema
        # here and no type to infer, which is the whole difference from a tool's
        # inputSchema. A client renders text boxes and marks one of them required.
        "arguments": [
            {
                "name": "subject",
                "description": "What to write about",
                "required": True,
            },
            {
                "name": "tone",
                "description": "How to write it. Optional; the server defaults it",
            },
        ],
    },
    {
        "name": "zoo-prompt-conversation",
        "description": "Expands to several messages, alternating user and assistant",
        # A prompt is a message *list*, not a string. A client that shows only the first
        # message, or that flattens the roles away, looks right until it meets this one.
        "arguments": [],
    },
    {
        "name": "zoo-prompt-contents",
        "description": "One message per MCP content type, including a blob nobody prints",
        # `every-content` does this for a tool result; expanded prompt messages travel the
        # same `to_content_block` mapping, and nothing exercised them through it.
        "arguments": [],
    },
]

ZOO_RESOURCES = [
    {
        "uri": "zoo://text",
        "name": "zoo-text",
        "description": "Plain text, and it opens a fenced block of its own",
        "mimeType": "text/plain",
    },
    {
        "uri": "zoo://data.json",
        "name": "zoo-json",
        "description": "A JSON body -- still `text`, with a mimeType that says otherwise",
        "mimeType": "application/json",
    },
    {
        "uri": "zoo://blob.bin",
        "name": "zoo-blob",
        "description": "Binary: carries `blob` and never `text`",
        "mimeType": "application/octet-stream",
    },
    {
        "uri": "zoo://multi",
        "name": "zoo-multi",
        "description": "One read, three contents -- `resources/read` returns a list",
        "mimeType": "text/plain",
    },
    {
        "uri": "zoo://ticks",
        "name": "zoo-ticks",
        "description": "Dynamic: one minute-stamped line per read, the last ten kept",
        "mimeType": "text/plain",
    },
    {
        "uri": "zoo://animals",
        "name": "zoo-animals",
        "description": "The known animals, as an enum -- the vocabulary the template takes",
        "mimeType": "application/json",
    },
]

ZOO_TEMPLATES = [
    {
        # Published here and by nothing else, the way `greeting://{name}` is: a template
        # is expanded client-side (RFC 6570) into a URI `resources/read` will accept.
        "uriTemplate": "zoo://echo/{word}",
        "name": "zoo-echo-template",
        "description": "Expand it client-side; reading the result echoes the word",
        "mimeType": "text/plain",
    },
    {
        # The pair worth having: `zoo://animals` publishes the vocabulary and this reads
        # one member of it. That is the shape a real templated resource has -- a listing
        # small enough to send whole, and a detail read that would not be.
        "uriTemplate": "zoo://animals/{id}",
        "name": "zoo-animal-template",
        "description": "Details for one animal; `id` comes from zoo://animals",
        "mimeType": "application/json",
    },
]

# `zoo://ticks` is the one body here that is not a constant. Every read appends a line
# stamped to the *minute* and keeps the last ten, which makes two things visible that a
# static resource cannot show: that a second read really went to the server rather than a
# cache, and that a resource is a window on state rather than a file. Minute resolution
# on purpose -- reads inside one minute share a stamp, so the sequence number is what
# separates them, and a client that de-duplicates by content is caught by it.
ZOO_TICK_KEEP = 10
_zoo_ticks = []
_zoo_tick_reads = 0


def zoo_tick_text():
    """Record this read and return the rolling window, newest last."""
    global _zoo_tick_reads
    _zoo_tick_reads += 1
    _zoo_ticks.append("%04d  %s" % (_zoo_tick_reads, time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime())))
    # Keep the last ten. `del list[:-10]` is a no-op until there are more than ten.
    del _zoo_ticks[:-ZOO_TICK_KEEP]
    return "\n".join(
        [
            "# read %d time(s); the last %d kept, newest last. Minute resolution."
            % (_zoo_tick_reads, ZOO_TICK_KEEP),
            *_zoo_ticks,
        ]
    )


ZOO_TEXT = """A zoo resource, as text/plain.

It deliberately contains a fenced block:

```json
{"why": "a resource body is arbitrary, and frequently is Markdown itself"}
```

Rendered as prose this would style itself and close the fence around it early, which is
what `fenced_lines` sizes its own fence past."""

# "hi there, this is a zoo blob" -- short enough to read in a diff, long enough that the
# rendered "about N bytes" placeholder is not 2.
ZOO_BLOB = "aGkgdGhlcmUsIHRoaXMgaXMgYSB6b28gYmxvYg=="


# The zoo's own subject matter, and the one pair of resources here that models how a real
# server publishes a set: a *listing* resource small enough to send whole, beside a
# *template* for the per-member read that would not be. `zoo://animals` is the vocabulary
# -- spelt as a JSON Schema enum fragment, because that is the form a client can drop
# straight into a picker -- and `zoo://animals/{id}` reads one member of it.
#
# `enumNames` rides along for the same reason it does on `zoo-choices`: it is not a JSON
# Schema keyword, and a client that normalises the body loses the labels a dropdown needs.
ZOO_ANIMALS = {
    "axolotl": {
        "name": "Axolotl",
        "scientificName": "Ambystoma mexicanum",
        "habitat": "Lake Xochimilco, Mexico",
        "diet": "carnivore",
        "conservationStatus": "Critically Endangered",
        "fact": "Neotenic: it keeps its larval gills for life and never leaves the water.",
    },
    "capybara": {
        "name": "Capybara",
        "scientificName": "Hydrochoerus hydrochaeris",
        "habitat": "Wetlands and riverbanks of South America",
        "diet": "herbivore",
        "conservationStatus": "Least Concern",
        "fact": "The largest living rodent, and a competent underwater swimmer.",
    },
    "okapi": {
        "name": "Okapi",
        "scientificName": "Okapia johnstoni",
        "habitat": "Ituri Rainforest, Democratic Republic of the Congo",
        "diet": "herbivore",
        "conservationStatus": "Endangered",
        "fact": "The giraffe's only living relative, striped like a zebra it is unrelated to.",
    },
    "pangolin": {
        "name": "Pangolin",
        "scientificName": "Manis pentadactyla",
        "habitat": "Forests and grasslands of Asia and Africa",
        "diet": "insectivore",
        "conservationStatus": "Critically Endangered",
        "fact": "The only mammal covered in keratin scales, and the most trafficked one.",
    },
    "quokka": {
        "name": "Quokka",
        "scientificName": "Setonix brachyurus",
        "habitat": "Rottnest Island and south-western Australia",
        "diet": "herbivore",
        "conservationStatus": "Vulnerable",
        "fact": "A macropod the size of a house cat; it climbs trees, which macropods rarely do.",
    },
    "red-panda": {
        "name": "Red Panda",
        "scientificName": "Ailurus fulgens",
        "habitat": "Temperate forests of the eastern Himalayas",
        "diet": "herbivore",
        "conservationStatus": "Endangered",
        # A hyphen in the id on purpose: a client that builds the detail URI by string
        # substitution is fine, and one that assumes an identifier is a bare word is not.
        "fact": "Its own family, Ailuridae -- not a bear, and not closely related to one.",
    },
}


class ZooResourceNotFound(Exception):
    """`zoo://animals/<unknown>`: a URI shaped correctly for a resource that is not here.

    MCP gives this its own code -- `-32002`, with the URI in `data` -- rather than folding
    it into the generic `-32000` the rest of this fixture uses, and the distinction is the
    point of the branch: a *mistyped expansion of a real template* is a different failure
    from an unimplemented method or a broken tool, and a client should be able to tell it
    apart without reading the message. The older `greeting://` branch keeps answering
    `-32000`, because a fixture where both codes appear is what proves they stay apart.
    """


def zoo_animals_enum():
    """The vocabulary, as the JSON Schema fragment a client can render a picker from."""
    return {
        "type": "string",
        "enum": sorted(ZOO_ANIMALS),
        "enumNames": [ZOO_ANIMALS[key]["name"] for key in sorted(ZOO_ANIMALS)],
        "description": "The animals this zoo knows about.",
        # Naming the template in the body is what makes the listing usable on its own: a
        # client that reads this knows both the ids and where to spend one of them.
        "readOne": "zoo://animals/{id}",
    }


def zoo_prompt_result(name, arguments):
    """The expansion of one zoo prompt, or None when `name` is not one of them.

    The substitution happens *here*, on the server, which is the whole reason
    `/promptShow` needs no model: the arguments arrive as strings and the messages go
    back as messages.
    """
    arguments = arguments if isinstance(arguments, dict) else {}
    if name == "zoo-prompt-bare":
        return {
            "description": "A prompt with nothing to fill in",
            "messages": [
                {
                    "role": "user",
                    "content": {"type": "text", "text": "Describe the zoo in one line."},
                }
            ],
        }
    if name == "zoo-prompt-arguments":
        # `subject` is required and the client validates it before we are called; `tone`
        # is optional, and defaulting it here is the server's job, not the caller's.
        subject = arguments.get("subject", "<no subject>")
        tone = arguments.get("tone") or "plain"
        return {
            "description": "A prompt with its arguments substituted",
            "messages": [
                {
                    "role": "user",
                    "content": {
                        "type": "text",
                        "text": "Write about %s in a %s tone." % (subject, tone),
                    },
                }
            ],
        }
    if name == "zoo-prompt-conversation":
        return {
            "description": "A three-message conversation",
            "messages": [
                {"role": "user", "content": {"type": "text", "text": "What lives here?"}},
                {
                    "role": "assistant",
                    "content": {"type": "text", "text": "One tool per JSON Schema construct."},
                },
                {"role": "user", "content": {"type": "text", "text": "Show me the strange ones."}},
            ],
        }
    if name == "zoo-prompt-contents":
        return {
            "description": "One message per content type",
            "messages": [
                {"role": "user", "content": {"type": "text", "text": "some words"}},
                {
                    "role": "user",
                    "content": {"type": "image", "data": "aGk=", "mimeType": "image/png"},
                },
                {
                    "role": "user",
                    "content": {"type": "audio", "data": "aGk=", "mimeType": "audio/wav"},
                },
                {
                    "role": "user",
                    "content": {
                        "type": "resource_link",
                        "name": "zoo-text",
                        "uri": "zoo://text",
                    },
                },
                {
                    "role": "user",
                    "content": {
                        "type": "resource",
                        "resource": {
                            "uri": "zoo://text",
                            "mimeType": "text/plain",
                            "text": "embedded, not linked",
                        },
                    },
                },
                {
                    "role": "assistant",
                    "content": {
                        "type": "resource",
                        "resource": {
                            "uri": "zoo://blob.bin",
                            "mimeType": "application/octet-stream",
                            "blob": ZOO_BLOB,
                        },
                    },
                },
            ],
        }
    return None


def zoo_resource_contents(uri):
    """The contents of one zoo resource, or None when `uri` is not one of them.

    A `resources/read` result is a *list*, and every branch here returns one: a client
    that reads `contents[0]` and stops is wrong about `zoo://multi` and only that one.
    """
    if uri == "zoo://text":
        return [{"uri": uri, "mimeType": "text/plain", "text": ZOO_TEXT}]
    if uri == "zoo://data.json":
        return [
            {
                "uri": uri,
                "mimeType": "application/json",
                # `text`, not `blob`. JSON is text however the mimeType reads, and a
                # client that switches on the mimeType rather than on which field is
                # present gets this one wrong.
                "text": json.dumps(
                    {"zoo": True, "tools": len(SCHEMA_ZOO), "note": "text, not blob"},
                    indent=2,
                    sort_keys=True,
                ),
            }
        ]
    if uri == "zoo://blob.bin":
        return [{"uri": uri, "mimeType": "application/octet-stream", "blob": ZOO_BLOB}]
    if uri == "zoo://multi":
        return [
            {"uri": "zoo://multi#1", "mimeType": "text/plain", "text": "first content"},
            {"uri": "zoo://multi#2", "mimeType": "text/plain", "text": "second content"},
            {"uri": "zoo://multi#3", "mimeType": "application/octet-stream", "blob": ZOO_BLOB},
        ]
    if uri == "zoo://ticks":
        return [{"uri": uri, "mimeType": "text/plain", "text": zoo_tick_text()}]
    if uri == "zoo://animals":
        return [
            {
                "uri": uri,
                "mimeType": "application/json",
                "text": json.dumps(zoo_animals_enum(), indent=2, sort_keys=True),
            }
        ]
    if uri.startswith("zoo://animals/"):
        # The expansion of `zoo://animals/{id}`. The client did the expanding -- all this
        # sees is a concrete URI, and an id that is not in the vocabulary is a miss rather
        # than an invitation to guess at one.
        animal_id = uri[len("zoo://animals/") :]
        if animal_id not in ZOO_ANIMALS:
            raise ZooResourceNotFound(uri)
        return [
            {
                "uri": uri,
                "mimeType": "application/json",
                "text": json.dumps(
                    dict(ZOO_ANIMALS[animal_id], id=animal_id), indent=2, sort_keys=True
                ),
            }
        ]
    if uri.startswith("zoo://echo/"):
        # The expansion of `zoo://echo/{word}`. The client did the expanding; all this
        # sees, and all it may see, is a concrete URI.
        word = uri[len("zoo://echo/") :] or "<nothing>"
        return [{"uri": uri, "mimeType": "text/plain", "text": "You said: %s" % word}]
    return None

def tool_for_page(index):
    if index == 0:
        echo = {
            "name": "echo",
            "description": "Echoes text",
            "inputSchema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            # A real annotation block, always present: echoing genuinely reads nothing
            # and reaches nothing, and a fixture whose only tool is unannotated could
            # not tell "we did not read the hints" from "there were none".
            "annotations": {
                "title": "Echo",
                "readOnlyHint": True,
                "openWorldHint": False,
            },
        }
        extra = list(SCHEMA_ZOO) if _schema_zoo else []
        if not _annotated_tools:
            return [echo, *extra] if extra else echo
        # Opt-in so every unrelated test keeps seeing exactly one tool. These exist to
        # exercise the annotation -> ACP kind mapping end to end.
        return [
            echo,
            {
                "name": "wipe",
                "description": "Deletes things",
                "inputSchema": {"type": "object", "properties": {}},
                "annotations": {"readOnlyHint": False, "destructiveHint": True},
            },
            {
                "name": "patch",
                "description": "Appends things",
                "inputSchema": {"type": "object", "properties": {}},
                "annotations": {"readOnlyHint": False, "destructiveHint": False},
            },
            {
                "name": "plain",
                "description": "Says nothing about itself",
                "inputSchema": {"type": "object", "properties": {}},
            },
            *extra,
        ]
    return {
        "name": "echo-%d" % index,
        "description": "Echoes text (page %d)" % index,
        "inputSchema": {"type": "object", "properties": {}},
    }


def prompt_for_page(index):
    if index == 0:
        greeting = {
            "name": "greeting",
            "description": "Build a greeting message",
            "arguments": [{"name": "name", "required": True}],
        }
        # Page 0 only, like the tool zoo: pagination is its own concern, and hanging the
        # zoo off every page would make a paginated listing test about the zoo instead.
        return [greeting, *ZOO_PROMPTS] if _schema_zoo else greeting
    return {
        "name": "greeting-%d" % index,
        "description": "Build a greeting message (page %d)" % index,
        "arguments": [],
    }


def resource_for_page(index):
    if index == 0:
        greeting = {
            # Concrete, deliberately. `greeting://{name}` lived here once, which is the
            # very mistake `pyacp-as5` is about: a template is published by
            # `resources/templates/list` and by nothing else, and a fixture that mixed
            # the two let a client that never called that method look correct.
            "uri": "greeting://ada",
            "name": "greeting-resource",
            "description": "A greeting resource",
            "mimeType": "text/plain",
        }
        return [greeting, *ZOO_RESOURCES] if _schema_zoo else greeting
    return {
        "uri": "greeting://page-%d" % index,
        "name": "greeting-resource-%d" % index,
        "description": "A greeting resource (page %d)" % index,
        "mimeType": "text/plain",
    }


def template_for_page(index):
    if index == 0:
        greeting = {
            "uriTemplate": "greeting://{name}",
            "name": "greeting-template",
            "description": "A greeting template",
            "mimeType": "text/plain",
        }
        return [greeting, *ZOO_TEMPLATES] if _schema_zoo else greeting
    return {
        "uriTemplate": "greeting://page-%d/{name}" % index,
        "name": "greeting-template-%d" % index,
        "description": "A greeting template (page %d)" % index,
        "mimeType": "text/plain",
    }


while True:
    line = sys.stdin.readline()
    if not line:
        if IGNORE_EOF:
            # Stall instead of exiting, so the client has to escalate.
            while True:
                time.sleep(0.05)
        break

    try:
        req = json.loads(line)
    except json.JSONDecodeError:
        continue

    method = req.get("method")
    req_id = req.get("id")

    # A reply to a request *we* sent: an id and no method. Recorded rather than
    # allowed to fall through to "method not found", which would put an error on
    # the wire addressed to an id the client is not waiting on.
    if method is None:
        _server_replies.append(req)
        continue

    if method == "notifications/initialized":
        continue

    # A notification, so it gets no reply — only a record that it arrived, with
    # whatever requestId and reason the client put on it.
    if method == "notifications/cancelled":
        _cancellations.append(req.get("params") or {})
        continue

    if method == "initialize" and _stall_initialize:
        # Read the handshake and never answer it.
        _initialize_params = req.get("params") or {}
        _stalled_ids.append(req_id)
    elif method == "initialize":
        _initialize_params = req.get("params") or {}
        result = {}
        if not _omit_protocol_version:
            proposed = (req.get("params") or {}).get("protocolVersion")
            result["protocolVersion"] = _protocol_version or proposed or "2024-11-05"
        result["serverInfo"] = {"name": "mock-mcp", "version": "1.0.0"}
        result["capabilities"] = {name: {} for name in sorted(_capabilities)}
        write({"jsonrpc": "2.0", "id": req_id, "result": result})
    elif method == "tools/list":
        write(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": list_result(req, "tools", tool_for_page),
            }
        )
    # Accept the call and never answer it. The client's request_timeout is the
    # only thing that ends the wait, which is what makes the cancellation path
    # reachable from a test at all.
    elif method == "tools/call" and req.get("params", {}).get("name") == "stall":
        _stalled_ids.append(req_id)
    # Hand back the initialize params exactly as they arrived, as JSON text, so a test
    # can assert on the protocolVersion and capability block that were really sent.
    elif method == "tools/call" and req.get("params", {}).get("name") == "handshake-report":
        write(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(_initialize_params)}],
                    "isError": False,
                },
            }
        )
    # Hand back everything stalled and every cancellation received, as JSON text.
    elif method == "tools/call" and req.get("params", {}).get("name") == "cancel-report":
        write(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {"stalled": _stalled_ids, "cancelled": _cancellations}
                            ),
                        }
                    ],
                    "isError": False,
                },
            }
        )
    # Send a server-initiated request and answer the tools/call WITHOUT waiting for
    # the reply. A client whose read loop blocks inside its own request handler
    # cannot deliver this result, so a test that gets it has proved the loop is free.
    elif method == "tools/call" and req.get("params", {}).get("name") == "provoke-detached":
        args = req.get("params", {}).get("arguments", {}) or {}
        write(
            {
                "jsonrpc": "2.0",
                "id": "srv-detached",
                "method": args.get("server_method", "roots/list"),
                "params": args.get("server_params") or {},
            }
        )
        write(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": "sent"}], "isError": False},
            }
        )
    # Hand back every reply to a server-initiated request received so far.
    elif method == "tools/call" and req.get("params", {}).get("name") == "provoke-report":
        write(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(_server_replies)}],
                    "isError": False,
                },
            }
        )
    elif method == "tools/call" and req.get("params", {}).get("name") == "provoke":
        args = req.get("params", {}).get("arguments", {}) or {}
        server_method = args.get("server_method", "roots/list")
        # The params the provoked request carries. `roots/list` takes none, but
        # `elicitation/create` is only itself with a message and a schema.
        server_params = args.get("server_params") or {}
        # A notification the client should route to its notification handler.
        write(
            {
                "jsonrpc": "2.0",
                "method": "notifications/message",
                "params": {"level": "info", "data": "provoked"},
            }
        )
        # A server-initiated request. The client must answer it; we echo whatever
        # it sends back so the test can assert on the reply it actually produced.
        write(
            {
                "jsonrpc": "2.0",
                "id": "srv-1",
                "method": server_method,
                "params": server_params,
            }
        )
        reply = sys.stdin.readline()
        write(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": reply.strip()}],
                    "isError": False,
                },
            }
        )
    # Every schema-zoo tool answers the same way: the arguments it received, echoed back
    # as pretty JSON. What the tool *does* is not the point -- the point is seeing which
    # JSON types came out the far end, so `--count 3` arriving as `3` rather than `"3"` is
    # visible rather than assumed. `pyacp-6kz`.
    elif method == "tools/call" and str(req.get("params", {}).get("name", "")).startswith(
        "zoo-"
    ):
        params = req.get("params", {})
        write(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "tool": params.get("name"),
                                    "arguments": params.get("arguments", {}) or {},
                                },
                                indent=2,
                                sort_keys=True,
                            ),
                        }
                    ],
                    "isError": False,
                },
            }
        )
    # A tool that FAILS: a successful JSON-RPC result carrying isError: true.
    # This is the MCP-sanctioned way to report tool-level failure, and it must
    # not reach the client as a transport error.
    elif method == "tools/call" and req.get("params", {}).get("name") == "boom":
        args = req.get("params", {}).get("arguments", {}) or {}
        write(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {"type": "text", "text": args.get("detail", "tool exploded")}
                    ],
                    "isError": True,
                },
            }
        )
    # Every MCP content type in one result, so `pyacp-eg1.1`'s mapping is exercised
    # against a real server rather than against a hand-built dict. The trailing two are
    # deliberately broken: one type nothing maps, and one of a known type missing what
    # that type needs.
    elif method == "tools/call" and req.get("params", {}).get("name") == "every-content":
        write(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": "some words",
                            "annotations": {"audience": ["user"], "priority": 0.5},
                        },
                        {"type": "image", "data": "aGk=", "mimeType": "image/png"},
                        {"type": "audio", "data": "aGk=", "mimeType": "audio/wav"},
                        {
                            "type": "resource",
                            "resource": {"uri": "file:///notes.txt", "text": "embedded"},
                        },
                        {
                            "type": "resource",
                            "resource": {
                                "uri": "file:///doc.pdf",
                                "blob": "aGk=",
                                "mimeType": "application/pdf",
                            },
                        },
                        {"type": "resource_link", "name": "notes", "uri": "file:///notes.txt"},
                        {"type": "chart", "spec": {"kind": "bar"}},
                        {"type": "image", "data": "aGk="},
                    ],
                    "isError": False,
                },
            }
        )
    # A tool that succeeds with NO text content. `pyacp-8bv.2` needs it: an invocation
    # that asks for its output to be written has nothing to write here, and writing an
    # empty file would truncate one the client asked us to fill.
    elif method == "tools/call" and req.get("params", {}).get("name") == "picture":
        write(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "image", "data": "aGk=", "mimeType": "image/png"}],
                    "isError": False,
                },
            }
        )
    # A tool result that omits isError entirely. The spec defaults it to false;
    # the client is expected to fill it in rather than leave the field missing.
    elif method == "tools/call" and req.get("params", {}).get("name") == "no-flag":
        write(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": "no flag here"}]},
            }
        )
    # A JSON-RPC ERROR response with a caller-chosen code, so a test can prove
    # that two different server codes stay distinguishable to the client.
    elif method == "tools/call" and req.get("params", {}).get("name") == "rpc-error":
        args = req.get("params", {}).get("arguments", {}) or {}
        error = {
            "code": args.get("code", -32603),
            "message": args.get("message", "server said no"),
        }
        if "data" in args:
            error["data"] = args["data"]
        write({"jsonrpc": "2.0", "id": req_id, "error": error})
    elif method == "tools/call":
        params = req.get("params", {})
        if params.get("name") == "echo":
            text = params.get("arguments", {}).get("text", "")
            write(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": text}],
                        "isError": False,
                    },
                }
            )
        # The annotated tools exist only to carry annotations; what they return is not
        # the point, so they all answer the same way.
        elif params.get("name") in ("wipe", "patch", "plain"):
            write(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": "%s ran" % params["name"]}],
                        "isError": False,
                    },
                }
            )
        else:
            write(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32000, "message": "Unknown tool"},
                }
            )
    elif method == "prompts/list":
        write(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": list_result(req, "prompts", prompt_for_page),
            }
        )
    elif method == "prompts/get":
        params = req.get("params", {})
        # Only reachable with MOCK_MCP_SCHEMA_ZOO=1, because nothing else lists them --
        # but answered whenever asked, on the same principle as the capability block: a
        # client that asks for something unlisted must be caught by its own check.
        zoo_prompt = zoo_prompt_result(params.get("name"), params.get("arguments", {}))
        if zoo_prompt is not None:
            write({"jsonrpc": "2.0", "id": req_id, "result": zoo_prompt})
        elif params.get("name") == "greeting":
            name = params.get("arguments", {}).get("name", "friend")
            write(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "description": "Greeting prompt",
                        "messages": [
                            {
                                "role": "user",
                                "content": {"type": "text", "text": f"Hello, {name}!"},
                            }
                        ],
                    },
                }
            )
        else:
            write(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32000, "message": "Unknown prompt"},
                }
            )
    elif method == "resources/list":
        write(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": list_result(req, "resources", resource_for_page),
            }
        )
    elif method == "resources/templates/list" and not _no_templates:
        write(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": list_result(req, "resourceTemplates", template_for_page),
            }
        )
    elif method == "resources/read":
        params = req.get("params", {})
        # `resources/read` params are `{uri}`. This used to accept an `arguments`
        # member too, which no real server does -- honouring it here was the only
        # reason a client that sent one ever appeared to work (`pyacp-ito`). A
        # templated URI is expanded client-side before it gets this far.
        resource_uri = params.get("uri")
        try:
            zoo_contents = (
                zoo_resource_contents(resource_uri) if isinstance(resource_uri, str) else None
            )
        except ZooResourceNotFound:
            # The one place this fixture answers `-32002`. See ZooResourceNotFound.
            write(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {
                        "code": -32002,
                        "message": "Resource not found",
                        "data": {"uri": resource_uri},
                    },
                }
            )
            continue
        if zoo_contents is not None:
            write({"jsonrpc": "2.0", "id": req_id, "result": {"contents": zoo_contents}})
        elif isinstance(resource_uri, str) and resource_uri.startswith("greeting://"):
            name = resource_uri.split("//", 1)[1] or "friend"
            content = f"Hello, {name}!"
            write(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "contents": [{"uri": resource_uri, "mimeType": "text/plain", "text": content}]
                    },
                }
            )
        else:
            write(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32000, "message": "Unknown resource"},
                }
            )
    else:
        write(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": "Method not found"},
            }
        )
