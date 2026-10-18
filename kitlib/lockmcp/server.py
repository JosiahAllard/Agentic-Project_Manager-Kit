"""The lock MCP server (ARCHITECTURE.md, Scheduler UI; docs/lock-mcp.md): MCP tools that are thin
wrappers over kitlib.lock.api, and nothing else. JSON-RPC 2.0 over stdio, one message per
line, written here on the standard library (the kit uses no packages, CLAUDE.md).

Read tools only, open to all (the owner's decision, docs/lock-mcp.md "No write tools"): the server has no write tool, so no session mark
and no process tree decides who may write through it. The owner changes the lock outside any agent
session (docs/lock-mcp.md). The retired write names are pinned in kitlib.lockmcp.OWNER_ONLY:
tools/list never lists one, tools/call answers it as an unknown tool, and the guard still denies
them. A lock error (HoldsUnreadable among them) comes back as a tool result with isError true,
never as a crashed server or a JSON-RPC error.

Server(store).handle(message) -> the response (or None for a notification);
serve(server, rfile, wfile) runs it over two binary streams until rfile ends.
"""
import json
import traceback

from kitlib.lock import api
from kitlib.lock.store import ID_RE
from kitlib import mcpwire
from kitlib.lockmcp import OWNER_ONLY, SERVER_NAME

SERVER_VERSION = "0.1.0"
PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")    # newest first
WAIT_MAX_S = 60.0

_ISO = {"type": "string", "description": "An ISO time with its offset, like 2026-10-06T14:00:00Z."}
_RUN_ID = {"type": "string", "description": "A run id, like r-20261006T120000Z-abcdef."}
_HOLD_ID = {"type": "string", "description": "A hold id, like h-20261006T120000Z-abcd."}


def _obj(props, required=()):
    return {"type": "object", "properties": props, "required": list(required),
            "additionalProperties": False}


# name -> (description, input schema); every tool reads
TOOLS = {
    "lock_snapshot": (
        "The machine lock now (kitlib.lock.api.snapshot, docs/lock-api.md Snapshot): "
        "schema_version, revision, hardware, layout, slots, runs, queue, holds (and holds_error), "
        "game, kinds and stats. Large; prefer lock_run or lock_hold for one record.",
        _obj({"history_days": {"type": "number", "description": "Stats range in days (default 7)."},
              "since": _ISO, "until": _ISO})),
    "lock_wait_change": (
        "The snapshot once the lock's revision moves past `revision`, or after timeout_s "
        "(default 30, at most 60); the same revision back means nothing was written.",
        _obj({"revision": {"type": "integer"},
              "timeout_s": {"type": "number", "description": "Seconds to wait, at most 60."}},
             ["revision"])),
    "lock_stats": (
        "Run history per kind and repo (the snapshot's stats): runs, done, failed, interrupted, "
        "held_s, wait_s, cpu_s, peaks.",
        _obj({"days": {"type": "number"}, "since": _ISO, "until": _ISO})),
    "lock_run": (
        "One run's record, live (live: true) or ended; found: false when there is none.",
        _obj({"id": _RUN_ID}, ["id"])),
    "lock_hold": (
        "One hold's record, live (live_record: true, as the snapshot shows it) or ended; "
        "found: false when there is none.",
        _obj({"id": _HOLD_ID}, ["id"])),
}
assert not set(TOOLS) & set(OWNER_ONLY)


class ToolError(Exception):
    def __init__(self, code, message, field=None):
        Exception.__init__(self, message)
        self.error = {"code": code, "message": message, "field": field}


def _args(name, args):
    """Checks args against the tool's schema: an object, its required keys, no other keys, and the
    simple types. The api checks the rest."""
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
        types = schema["properties"][k].get("type")
        types = types if isinstance(types, list) else [types]
        if not any(_is(v, t) for t in types):
            raise ToolError("invalid", "%s must be %s" % (k, " or ".join(types)), k)
    return args


def _is(v, t):
    if t == "integer":
        return isinstance(v, int) and not isinstance(v, bool)
    if t == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    py = {"string": str, "boolean": bool, "object": dict, "array": list, "null": type(None)}.get(t)
    return py is not None and isinstance(v, py)


def _id(v, prefix):
    """A run (r-) or hold (h-) id as the lock makes them (store.ID_RE, the whole string: a
    re.match `$` also matches before a final newline); anything else is refused before it
    reaches a path."""
    if not (isinstance(v, str) and v.startswith(prefix + "-") and ID_RE.fullmatch(v)):
        raise ToolError("invalid", "id must be a %s id like %s-20261006T120000Z-abcd" % (
            "run" if prefix == "r" else "hold", prefix), "id")
    return v



def _failed(rec):
    """True when a record read came back as the api's error result ({ok: false, error})."""
    return isinstance(rec, dict) and rec.get("ok") is False

class Server:
    """The read tools over one store (None: the machine's). actor is ignored: no tool writes, so no
    tool asks who is calling (tests/lockmcp/scratch_server.py still passes one)."""

    def __init__(self, store=None, actor=None):
        self.store = store

    # ---------------------------------------------------------------- tools
    def tool_list(self):
        return [{"name": n, "description": d, "inputSchema": s,
                 "annotations": {"readOnlyHint": True, "destructiveHint": False}}
                for n, (d, s) in TOOLS.items()]

    def call(self, name, args):
        """(payload, is_error) for one tool call. Never raises."""
        try:
            a = _args(name, args if args is not None else {})
            return getattr(self, "_t_" + name)(a)
        except ToolError as e:
            return {"ok": False, "error": e.error}, True
        except api.ApiError as e:
            return {"ok": False, "error": {"code": e.code, "message": e.message,
                                           "field": e.field}}, True
        except OSError as e:          # HoldsUnreadable, QueueUnreadable, a store that won't read
            return {"ok": False, "error": {"code": "store", "message": str(e) or repr(e),
                                           "field": None}}, True
        except Exception as e:        # a lock bug must not take the server down
            return {"ok": False, "error": {"code": "internal", "message": "%s: %s" % (
                type(e).__name__, e), "field": None,
                "trace": traceback.format_exc(limit=5)}}, True

    def _failure(self, res):
        """The api's error result ({ok: false, error, snapshot}) without the snapshot
        (lock_snapshot gives it), keeping its revision."""
        snap = res.get("snapshot")
        out = {k: v for k, v in res.items() if k != "snapshot"}
        out["revision"] = snap.get("revision") if isinstance(snap, dict) else None
        return out, not res.get("ok")

    def _t_lock_snapshot(self, a):
        return api.snapshot(self.store, history_days=a.get("history_days", 7),
                            since=a.get("since"), until=a.get("until")), False

    def _t_lock_wait_change(self, a):
        t = min(max(float(a.get("timeout_s", 30.0)), 0.0), WAIT_MAX_S)
        return api.wait_change(a["revision"], timeout_s=t, store=self.store), False

    def _t_lock_stats(self, a):
        return api.stats(self.store, days=a.get("days", 7), since=a.get("since"),
                         until=a.get("until")), False

    def _t_lock_run(self, a):
        rec = api.run_record(_id(a["id"], "r"), self.store)
        if _failed(rec):
            return self._failure(rec)
        return {"found": rec is not None, "run": rec}, False

    def _t_lock_hold(self, a):
        rec = api.hold_record(_id(a["id"], "h"), self.store)
        if _failed(rec):              # holds unreadable: the api's store error result
            return self._failure(rec)
        return {"found": rec is not None, "hold": rec}, False

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
        notify = "id" not in msg
        if notify:
            return None                 # notifications/initialized, cancelled: nothing to say
        if not isinstance(params, dict):
            return _err(mid, -32602, "params must be an object")
        if method == "initialize":
            want = params.get("protocolVersion")
            return _ok(mid, {"protocolVersion": want if want in PROTOCOLS else PROTOCOLS[0],
                             "capabilities": {"tools": {"listChanged": False}},
                             "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                             "instructions": "The machine lock (kitlib.lock.api): read tools "
                                             "only, open to all. The owner changes the lock "
                                             "outside any agent session."})
        if method == "ping":
            return _ok(mid, {})
        if method == "tools/list":
            return _ok(mid, {"tools": self.tool_list()})
        if method == "tools/call":
            name = params.get("name")
            if name not in TOOLS:
                return _err(mid, -32602, "unknown tool %s" % name)
            payload, is_error = self.call(name, params.get("arguments"))
            text = json.dumps(payload, ensure_ascii=False, default=str)
            res = {"content": [{"type": "text", "text": text}], "isError": is_error}
            if isinstance(payload, dict):
                res["structuredContent"] = json.loads(text)
            return _ok(mid, res)
        return _err(mid, -32601, "method not found: %s" % method)


_ok, _err, serve = mcpwire.ok, mcpwire.err, mcpwire.serve   # the wire both MCP servers share
