"""The lock MCP server (ARCHITECTURE.md, Scheduler UI; docs/lock-mcp.md): MCP tools that are thin
wrappers over kitlib.lock.api, and nothing else. JSON-RPC 2.0 over stdio, one message per
line, written here on the standard library (the kit uses no packages, CLAUDE.md).

Read tools are open to all. Write tools call the api's validated writes and are the owner's:
their descriptions say so, and guard.py is to deny agents the names in OWNER_ONLY. A lock error
(HoldsUnreadable among them) comes back as a tool result with isError true, never as a crashed
server or a JSON-RPC error.

Server(store, actor).handle(message) -> the response (or None for a notification);
serve(server, rfile, wfile) runs it over two binary streams until rfile ends.
"""
import json
import traceback

from kitlib.lock import api
from kitlib.lock.store import ID_RE
from kitlib.lockmcp import SERVER_NAME

SERVER_VERSION = "0.1.0"
PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")    # newest first
WAIT_MAX_S = 60.0

OWNER_NOTE = ("OWNER-ONLY: the machine owner's write. Agents must not call it (the kit guard denies "
              "it); in the owner's session each call asks for approval. ")

_ISO = {"type": "string", "description": "An ISO time with its offset, like 2026-10-06T14:00:00Z."}
_RUN_ID = {"type": "string", "description": "A run id, like r-20261006T120000Z-abcdef."}
_HOLD_ID = {"type": "string", "description": "A hold id, like h-20261006T120000Z-abcd."}


def _obj(props, required=()):
    return {"type": "object", "properties": props, "required": list(required),
            "additionalProperties": False}


# name -> (description, input schema, owner-only)
TOOLS = {
    "lock_snapshot": (
        "The machine lock now (kitlib.lock.api.snapshot, docs/lock-api.md Snapshot): "
        "schema_version, revision, hardware, layout, slots, runs, queue, holds (and holds_error), "
        "game, kinds and stats. Large; prefer lock_run or lock_hold for one record.",
        _obj({"history_days": {"type": "number", "description": "Stats range in days (default 7)."},
              "since": _ISO, "until": _ISO}), False),
    "lock_wait_change": (
        "The snapshot once the lock's revision moves past `revision`, or after timeout_s "
        "(default 30, at most 60); the same revision back means nothing was written.",
        _obj({"revision": {"type": "integer"},
              "timeout_s": {"type": "number", "description": "Seconds to wait, at most 60."}},
             ["revision"]), False),
    "lock_stats": (
        "Run history per kind and repo (the snapshot's stats): runs, done, failed, interrupted, "
        "held_s, wait_s, cpu_s, peaks.",
        _obj({"days": {"type": "number"}, "since": _ISO, "until": _ISO}), False),
    "lock_run": (
        "One run's record, live (live: true) or ended; found: false when there is none.",
        _obj({"id": _RUN_ID}, ["id"]), False),
    "lock_hold": (
        "One hold's record, live (live_record: true, as the snapshot shows it) or ended; "
        "found: false when there is none.",
        _obj({"id": _HOLD_ID}, ["id"]), False),
    "lock_hold_create": (
        OWNER_NOTE + "Makes a hold (api.hold_create). spec: owner (owner|agent), scope "
        "(owner: whole|half|slots), start (ISO, default now), end (ISO) or for_s, note, job; "
        "slots adds slots {cpu, gpu}; agent adds repo, slug, kind, gpu_share.",
        _obj({"spec": {"type": "object"}}, ["spec"]), True),
    "lock_hold_edit": (
        OWNER_NOTE + "Changes a live hold (api.hold_edit): owner holds start (before it "
        "starts), end, note; agent and game holds end, note.",
        _obj({"id": _HOLD_ID, "changes": {"type": "object"}}, ["id", "changes"]), True),
    "lock_hold_end": (
        OWNER_NOTE + "Ends a live hold now (api.hold_end). A game hold comes back while the "
        "game runs; switch game mode off instead.",
        _obj({"id": _HOLD_ID}, ["id"]), True),
    "lock_set_game_mode": (
        OWNER_NOTE + "Game mode on or off (api.set_game_mode); off ends a live game hold.",
        _obj({"enabled": {"type": "boolean"}}, ["enabled"]), True),
    "lock_set_games": (
        OWNER_NOTE + "Replaces the games list (api.set_games): [{exe, pin?, name?}].",
        _obj({"games": {"type": "array", "items": {"type": "object"}}}, ["games"]), True),
    "lock_set_division": (
        OWNER_NOTE + "The owner's division of this machine (api.set_division): an object of "
        "layout.DIVISION_KEYS, or null for every default.",
        _obj({"division": {"type": ["object", "null"]}}, ["division"]), True),
    "lock_end_run": (
        OWNER_NOTE + "Asks a live run to end (api.end_run); it does not wait.",
        _obj({"id": _RUN_ID}, ["id"]), True),
}
OWNER_ONLY = tuple(n for n, t in TOOLS.items() if t[2])


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
    """The tools over one store. actor is what the writes pass to the api: on the machine's store
    only its name is used (api.who reads who is asking from this process's tree); a scratch store
    (tests) takes it as given."""

    def __init__(self, store=None, actor=None):
        self.store = store
        self.actor = actor if actor is not None else {"name": "lock-mcp"}

    # ---------------------------------------------------------------- tools
    def tool_list(self):
        return [{"name": n, "description": d, "inputSchema": s,
                 "annotations": {"readOnlyHint": not w, "destructiveHint": w}}
                for n, (d, s, w) in TOOLS.items()]

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

    def _write(self, res):
        """An api write's {ok, result | error, snapshot} without the snapshot (lock_snapshot gives
        it), keeping its revision."""
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
            return self._write(rec)
        return {"found": rec is not None, "run": rec}, False

    def _t_lock_hold(self, a):
        rec = api.hold_record(_id(a["id"], "h"), self.store)
        if _failed(rec):              # holds unreadable: the api's store error result
            return self._write(rec)
        return {"found": rec is not None, "hold": rec}, False

    def _t_lock_hold_create(self, a):
        return self._write(api.hold_create(self.actor, a["spec"], self.store))

    def _t_lock_hold_edit(self, a):
        return self._write(api.hold_edit(self.actor, _id(a["id"], "h"), a["changes"], self.store))

    def _t_lock_hold_end(self, a):
        return self._write(api.hold_end(self.actor, _id(a["id"], "h"), self.store))

    def _t_lock_set_game_mode(self, a):
        return self._write(api.set_game_mode(self.actor, a["enabled"], self.store))

    def _t_lock_set_games(self, a):
        return self._write(api.set_games(self.actor, a["games"], self.store))

    def _t_lock_set_division(self, a):
        return self._write(api.set_division(self.actor, a["division"], self.store))

    def _t_lock_end_run(self, a):
        return self._write(api.end_run(self.actor, _id(a["id"], "r"), self.store))

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
                             "instructions": "The machine lock (kitlib.lock.api). Read tools are "
                                             "open to all; tools marked OWNER-ONLY are the "
                                             "owner's and agents must not call them."})
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
