"""The hub MCP server (docs/hub-mcp.md): the Action Hub's api as MCP tools, and nothing else.
JSON-RPC 2.0 over stdio, one message per line, on the standard library (CLAUDE.md), in the shape of
kitlib.lockmcp.server.

Every tool is one call to the hub server on this machine, through kitlib.hubsite.client: the
loopback listener only, so the server's checks (roles, fields, versions) are the ones that hold,
and this process never opens the hub store. The port is read from the machine's hub settings
(kitlib.hubsite.settings) at each call, so a server started later, or on another port, is found.

Read tools carry the agent's token (the owner's when there is no agent token: Client.token ANY);
write tools carry the agent's and name their source serve.VIA_MCP. The owner's writes are pinned in
kitlib.hubmcp.OWNER_ONLY and do not ship yet: tools/list leaves them out and tools/call refuses
them (`not_available`; `forbidden` in a session the launcher marked), never reading the owner's
token. They land once the guard denies them to agents by name (GUARD_WAIT).

A refusal, a hub that is not running, or anything else that goes wrong comes back as a tool result
with isError true and {ok: false, error: {code, message, field}}, never as a crashed server.

Server(port, tokens, ask, marked, load_settings).handle(message) -> the response (or None for a
notification); serve(server, rfile, wfile) runs it over two binary streams until rfile ends.
"""
import json
import traceback

from kitlib.config.types import ConfigError
from kitlib.hubmcp import OWNER_ONLY, SERVER_NAME
from kitlib.hubsite import api as hubapi
from kitlib.hubsite import client as hubclient
from kitlib.hubsite import serve as hubserve
from kitlib.hubsite import settings as hubsettings

SERVER_VERSION = "0.1.0"
PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")    # newest first
WAIT_MAX_S = hubapi.WAIT_MAX_S                              # one tools/call waits at most this long
WAIT_DEFAULT_S = 30
LISTS = ("questions", "todos", "stories", "to_pm")
VIEWS = ("notices", "repos", "usage", "agents")
ANY, AGENT = hubclient.ANY, hubclient.AGENT
GUARD_WAIT = "the guard denies them to agents by name"
INSTRUCTIONS = ("The Action Hub on this machine (docs/hub-mcp.md): read tools for its questions, "
                "to-dos, stories, notes to the PM, inbox and views, and the agent's writes. The "
                "owner's writes (answering, marking done, submitting, sending) are not tools here.")

_ID = {"type": "string", "description": "The document's id, as the hub gave it (Q-12, T-3, ...)."}
_PIN = {"type": "integer", "description": "The version of the document you read: the write is "
                                          "refused (conflict) when it has changed since."}
_REPO = {"type": "string", "description": "A repository's name."}
_FROM = {"type": "string", "description": "Who is writing: a session or agent name."}
_REFS = {"type": "array", "items": {"type": "string"},
         "description": "Ids of other hub documents this one names."}
_URGENT = {"type": "boolean"}


def _obj(props, required=()):
    return {"type": "object", "properties": props, "required": list(required),
            "additionalProperties": False}


def _one_of(values, what):
    return {"type": "string", "enum": list(values), "description": what}


# name -> (description, input schema, HTTP method, path below hubapi.BASE, role of the token).
# A `{name}` in the path is filled from the argument of that name; every other argument is the
# query of a GET or the body of a POST.
TOOLS = {
    "hub_pending": (
        "The owner input the PM has not handled (GET /pending), as {ok, items}.",
        _obj({}), "GET", "/pending", ANY),
    "hub_wait": (
        "Waits for the hub to change (GET /wait): returns {revision, reset, timed_out, changes, "
        "pending} once the store's revision moves past `since`, or after timeout_s (default 30, "
        "at most 55) with timed_out true. pending: also return at once while items wait.",
        _obj({"since": {"type": "integer", "description": "The revision you last saw."},
              "timeout_s": {"type": "number", "description": "Seconds to wait, at most 55."},
              "pending": {"type": "boolean"}}), "GET", "/wait", ANY),
    "hub_list": (
        "The documents of one collection (GET /<collection>): questions, todos, stories or to_pm, "
        "as {ok, items}; each item carries its id and version.",
        _obj({"collection": _one_of(LISTS, "The collection."),
              "status": {"type": "string", "description": "Only this status (all: every one)."},
              "repo": _REPO}, ["collection"]), "GET", "/{collection}", ANY),
    "hub_inbox": (
        "The inbox documents (from GET /snapshot; the api has no route that lists them), as "
        "{ok, items}.",
        _obj({}), "GET", "/snapshot", ANY),
    "hub_backlog": (
        "The backlog view (GET /backlog), per repository, as {ok, repos}.",
        _obj({"repo": _REPO, "state": {"type": "string"},
              "fresh": {"type": "boolean", "description": "Read the backlog again first."}}),
        "GET", "/backlog", ANY),
    "hub_completed": (
        "Recently completed work (GET /completed).",
        _obj({"repo": _REPO, "days": {"type": "integer"}}), "GET", "/completed", ANY),
    "hub_view": (
        "One of the hub's derived views (GET /<view>): notices, repos, usage or agents.",
        _obj({"view": _one_of(VIEWS, "The view.")}, ["view"]), "GET", "/{view}", ANY),
    "hub_health": (
        "The hub server's health (GET /health): its api version, revision, git sync and "
        "backlog views, and the views it could not answer.",
        _obj({}), "GET", "/health", ANY),
    "hub_ask": (
        "Asks the owner a question (POST /questions): it shows on the Action Hub until answered.",
        _obj({"title": {"type": "string"}, "context": {"type": "string"},
              "options": {"type": "array", "items": {"type": "object"},
                          "description": "The choices: [{label, description}]."},
              "recommended": {"type": "integer", "description": "The index of the option you "
                                                                "recommend."},
              "repo": _REPO, "from": _FROM, "refs": _REFS, "urgent": _URGENT,
              "story": {"type": "string", "description": "The story this question is about."}},
             ["title"]), "POST", "/questions", AGENT),
    "hub_question_log": (
        "Records the ruling an answered question became (POST /questions/<id>/log); the question "
        "is then logged.",
        _obj({"id": _ID, "ruling": {"type": "string", "description": "The ruling's id."},
              "if_version": _PIN}, ["id", "ruling", "if_version"]),
        "POST", "/questions/{id}/log", AGENT),
    "hub_todo_add": (
        "Adds a to-do for the owner (POST /todos). An agent's open to-do needs commands: the "
        "steps the owner runs.",
        _obj({"title": {"type": "string"}, "detail": {"type": "string"},
              "commands": {"type": "array", "items": {"type": "string"}},
              "repo": _REPO, "refs": _REFS, "urgent": _URGENT, "from": _FROM}, ["title"]),
        "POST", "/todos", AGENT),
    "hub_todo_ack": (
        "Marks an owner's to-do as read by the PM (POST /todos/<id>/ack).",
        _obj({"id": _ID, "if_version": _PIN}, ["id"]), "POST", "/todos/{id}/ack", AGENT),
    "hub_story_set": (
        "Moves an owner's story on (POST /stories/<id>): status review, questions, added or "
        "declined, with the reviewer, a note, the backlog id or a closing note.",
        _obj({"id": _ID, "status": _one_of(hubapi.AGENT_STORY_STATUSES, "The new status."),
              "reviewer": {"type": "string"}, "review_note": {"type": "string"},
              "backlog_id": {"type": "string"}, "closed_note": {"type": "string"},
              "if_version": _PIN}, ["id", "status", "if_version"]),
        "POST", "/stories/{id}", AGENT),
    "hub_to_pm_reply": (
        "Replies to a note the owner sent the PM (POST /to_pm/<id>/reply); the note is then done.",
        _obj({"id": _ID, "reply": {"type": "string"},
              "ruling": {"type": "string", "description": "The ruling it became, if any."},
              "if_version": _PIN}, ["id"]), "POST", "/to_pm/{id}/reply", AGENT),
    "hub_inbox_post": (
        "Puts a document in the owner's inbox (POST /inbox): an answer to something the owner "
        "asked, or a report.",
        _obj({"text": {"type": "string"}, "title": {"type": "string"},
              "ref": {"type": "string", "description": "The id it answers."},
              "asked": {"type": "string", "description": "What the owner asked."},
              "repo": _REPO, "from": _FROM, "refs": _REFS}, ["text"]), "POST", "/inbox", AGENT),
}
assert not set(TOOLS) & set(OWNER_ONLY)


class ToolError(Exception):
    def __init__(self, code, message, field=None):
        Exception.__init__(self, message)
        self.error = {"code": code, "message": message, "field": field}


def _args(name, args):
    """Checks args against the tool's schema: an object, its required keys, no other keys, the
    simple types and an enum. The hub checks the rest."""
    schema = TOOLS[name][1]
    if not isinstance(args, dict):
        raise ToolError("invalid", "arguments must be an object")
    for k in args:
        if k not in schema["properties"]:
            raise ToolError("invalid", "unknown argument %s" % k, k)
    for k in schema["required"]:
        if k not in args:
            raise ToolError("invalid", "%s is required" % k, k)
    for k, v in args.items():
        prop = schema["properties"][k]
        types = prop.get("type")
        types = types if isinstance(types, list) else [types]
        if not any(_is(v, t) for t in types):
            raise ToolError("invalid", "%s must be %s" % (k, " or ".join(types)), k)
        if "enum" in prop and v not in prop["enum"]:
            raise ToolError("invalid", "%s is one of %s" % (k, ", ".join(prop["enum"])), k)
    return args


def _is(v, t):
    if t == "integer":
        return isinstance(v, int) and not isinstance(v, bool)
    if t == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    py = {"string": str, "boolean": bool, "object": dict, "array": list, "null": type(None)}.get(t)
    return py is not None and isinstance(v, py)


def _query(v):
    if isinstance(v, bool):
        return "1" if v else "0"
    return v if isinstance(v, str) else str(v)


def _error(code, message, field=None, **more):
    return dict({"ok": False, "error": {"code": code, "message": message, "field": field}},
                **more), True


class Server:
    """The tools over the hub server on this machine.

    port: the hub's port (None: kitlib.hubsite.settings at each call). tokens: the folder of the
    token files (None: kitlib.hubsite.auth.folder()). ask: serve.ask or a stand-in. marked: the
    session's mark (kitlib.sessionmark.mark), read once at start by mcp/hub_server.py.
    load_settings: kitlib.hubsite.settings.load or a stand-in."""

    def __init__(self, port=None, tokens=None, ask=None, marked=None, load_settings=None):
        self.port = port
        self.tokens = tokens
        self.ask = ask
        self.marked = marked
        self.load_settings = load_settings or hubsettings.load

    def client(self):
        port = self.port if self.port is not None else self.load_settings().port
        return hubclient.Client(port, tokens=self.tokens, ask=self.ask, via=hubserve.VIA_MCP)

    # ---------------------------------------------------------------- tools
    def tool_list(self):
        return [{"name": n, "description": d, "inputSchema": s,
                 "annotations": {"readOnlyHint": m == "GET", "destructiveHint": m != "GET"}}
                for n, (d, s, m, _, _) in TOOLS.items()]

    def call(self, name, args):
        """(payload, is_error) for one tool call. Never raises."""
        if name in OWNER_ONLY:
            return self.owner_tool(name)
        try:
            a = _args(name, args if args is not None else {})
            special = getattr(self, "_t_" + name, None)
            return (special(a) if special else self._route(name, a)), False
        except ToolError as e:
            return {"ok": False, "error": e.error}, True
        except hubclient.Refused as e:
            return _error(e.code, e.message, e.field, status=e.status)
        except hubserve.NotRunning as e:
            return _error("not_running", "%s (tools/hub.py serve starts it)" % e)
        except hubserve.ServeError as e:
            return _error("client", str(e))
        except ConfigError as e:
            return _error("config", "the machine's hub settings cannot be read: %s" % e)
        except Exception as e:        # a bug here must not take the server down
            return _error("internal", "%s: %s" % (type(e).__name__, e),
                          trace=traceback.format_exc(limit=5))

    def owner_tool(self, name):
        """The answer to a pinned owner tool. The owner's token is never read."""
        if self.marked is not None:
            return _error("forbidden", "%s is the owner's, and this session is marked %s"
                          % (name, self.marked))
        return _error("not_available", "%s is the owner's and is not available yet: the owner's "
                      "hub tools land once %s" % (name, GUARD_WAIT))

    def _route(self, name, a):
        _, _, method, path, role = TOOLS[name]
        rest = dict(a)
        for key in list(rest):
            mark = "{%s}" % key
            if mark in path:
                value = rest.pop(key)
                if not value.strip():
                    raise ToolError("invalid", "%s is required" % key, key)
                path = path.replace(mark, hubclient.part(value))
        c = self.client()
        if method == "GET":
            return c.get(path, role, {k: _query(v) for k, v in rest.items()})
        return c.post(path, role, rest)

    def _t_hub_wait(self, a):
        t = min(max(float(a.get("timeout_s", WAIT_DEFAULT_S)), 0.0), float(WAIT_MAX_S))
        return self.client().wait(a.get("since"), timeout=t, pending=a.get("pending", False))

    def _t_hub_inbox(self, a):
        return {"ok": True, "items": self.client().inbox()}

    # ---------------------------------------------------------------- JSON-RPC
    def handle(self, msg):
        """One JSON-RPC message (or a batch list) -> the response, or None for notifications."""
        if isinstance(msg, list):
            out = [r for r in (self.handle(m) for m in msg) if r is not None]
            return out or None
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(
                msg.get("method"), str):
            return _err(msg.get("id") if isinstance(msg, dict) else None, -32600,
                        "invalid request")
        mid, method, params = msg.get("id"), msg["method"], msg.get("params") or {}
        if "id" not in msg:
            return None                 # notifications/initialized, cancelled: nothing to say
        if not isinstance(params, dict):
            return _err(mid, -32602, "params must be an object")
        if method == "initialize":
            want = params.get("protocolVersion")
            return _ok(mid, {"protocolVersion": want if want in PROTOCOLS else PROTOCOLS[0],
                             "capabilities": {"tools": {"listChanged": False}},
                             "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                             "instructions": INSTRUCTIONS})
        if method == "ping":
            return _ok(mid, {})
        if method == "tools/list":
            return _ok(mid, {"tools": self.tool_list()})
        if method == "tools/call":
            name = params.get("name")
            if name not in TOOLS and name not in OWNER_ONLY:
                return _err(mid, -32602, "unknown tool %s" % name)
            payload, is_error = self.call(name, params.get("arguments"))
            text = json.dumps(payload, ensure_ascii=False, default=str)
            res = {"content": [{"type": "text", "text": text}], "isError": is_error}
            if isinstance(payload, dict):
                res["structuredContent"] = json.loads(text)
            return _ok(mid, res)
        return _err(mid, -32601, "method not found: %s" % method)


def _ok(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _err(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def serve(server, rfile, wfile):
    """Reads one JSON message per line from rfile (binary) and writes each response as one line to
    wfile (binary) until rfile ends. A line that is not JSON gets a parse error (-32700)."""
    for raw in iter(rfile.readline, b""):
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            resp = _err(None, -32700, "parse error")
        else:
            try:
                resp = server.handle(msg)
            except Exception as e:        # never down: the client gets an internal error
                mid = msg.get("id") if isinstance(msg, dict) else None
                resp = _err(mid, -32603, "internal error: %s: %s" % (type(e).__name__, e))
        if resp is not None:
            wfile.write(json.dumps(resp, ensure_ascii=True, default=str).encode("utf-8") + b"\n")
            wfile.flush()
