"""The hub MCP server (docs/hub-mcp.md): the Action Hub's api as MCP tools, and nothing else.
JSON-RPC 2.0 over stdio, one message per line, on the standard library (CLAUDE.md), in the shape of
kitlib.lockmcp.server.

Every tool is one call to the hub server on this machine, through kitlib.hubsite.client: the
loopback listener only, so the server's checks (roles, fields, versions) are the ones that hold,
and this process never opens the hub store. The port is read from the machine's hub settings
(kitlib.hubsite.settings) at each call, so a server started later, or on another port, is found.

Every tool carries the agent's token and nothing else (AgentClient: a route open to ANY is called
with it too, and with no usable agent token a tool answers `no_agent_token` instead of falling
back to the owner's); write tools name their source serve.VIA_MCP. The owner's writes are pinned in
kitlib.hubmcp.OWNER_ONLY and do not ship yet: tools/list leaves them out and tools/call refuses
them (`not_available`; `forbidden` in a session the launcher marked), never reading the owner's
token. The guard already denies them to agents by name (its hub-mcp rule, kitlib/guard/hub.py
OWNER_MCP_TOOLS); shipping the tools themselves is still to come (GUARD_WAIT says so).

A refusal, a hub that is not running, or anything else that goes wrong comes back as a tool result
with isError true and {ok: false, error: {code, message, field}}, never as a crashed server.

Server(port, tokens, ask, marked, load_settings).handle(message) -> the response (or None for a
notification); serve(server, rfile, wfile) runs it over two binary streams until rfile ends.
"""
import json
import os
import traceback

from kitlib import hubapprove, mcpwire
from kitlib.config.types import ConfigError
from kitlib.hubmcp import OWNER_ONLY, SERVER_NAME
from kitlib.hubsite import api as hubapi
from kitlib.hubsite import approvals as hubapprovals
from kitlib.hubsite import auth as hubauth
from kitlib.hubsite import client as hubclient
from kitlib.hubsite import pagecheck
from kitlib.hubsite import serve as hubserve
from kitlib.hubsite import settings as hubsettings
from kitlib.hubsite import todosteps

SERVER_VERSION = "0.1.0"
PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")    # newest first
WAIT_MAX_S = hubapi.WAIT_MAX_S                              # one tools/call waits at most this long
WAIT_DEFAULT_S = 30
LISTS = ("questions", "todos", "stories", "to_pm")
VIEWS = ("notices", "repos", "usage", "agents")
ANY, AGENT = hubclient.ANY, hubclient.AGENT
PAGE_SUFFIXES = (".html", ".htm")                           # the files hub_page_publish reads
GUARD_WAIT = ("the guard already denies them to agents by name, but the tools themselves are not "
              "shipped yet")
INSTRUCTIONS = ("The Action Hub on this machine (docs/hub-mcp.md): read tools for its questions, "
                "to-dos, stories, notes to the PM, inbox, pages and views, and the agent's "
                "writes. The owner's writes (answering, marking done, submitting, sending, "
                "deleting a page) are not tools here.")

_ID = {"type": "string", "description": "The document's id, as the hub gave it (Q-12, T-3, ...)."}
_PIN = {"type": "integer", "description": "The version of the document you read: the write is "
                                          "refused (conflict) when it has changed since."}
_REPO = {"type": "string", "description": "A repository's name."}
_FROM = {"type": "string", "description": "Who is writing: a session or agent name."}
_ASK_FROM = {"type": "string", "description": "The session the answer goes back to: a launcher "
                                              "session key, or pm (the default when absent or "
                                              "blank). The hub refuses any other value "
                                              "(invalid)."}
_FOR = {"type": "string", "description": "Only the pending items for this session key: your own "
                                         "launcher session key, or pm (the PM's items and every "
                                         "answered question). Absent: every pending item."}
_CREATOR = {"type": "string", "description": "The from the item was added with (pm when it was "
                                             "added without one). All agents share one token, "
                                             "so this is a check against mistakes."}
_REFS = {"type": "array", "items": {"type": "string"},
         "description": "Ids of other hub documents this one names."}
_URGENT = {"type": "boolean"}
_RECORD_SAYS = ("The backlog record the question is about: {repo, id}, repo a repository the "
                "hub has configured (the kit's own records: the PM repository's folder name, "
                "which hub_view view 'repos' lists). The hub reads it now and "
                "refuses a repo it has not configured, a record that repo's backlog does not "
                "have or has completed, and a record it cannot read (unavailable).")
_RECORD = {"type": "object", "description": _RECORD_SAYS,
           "properties": {"repo": {"type": "string"}, "id": {"type": "string"}},
           "required": ["repo", "id"], "additionalProperties": False}
_RECORD_EDIT = dict(_RECORD, type=["object", "null"],
                    description=_RECORD_SAYS + " null unbinds the question (with class, unless "
                                               "it has one).")
_CLASS_SAYS = ("What kind of question it is, for one bound to no record (docs/action-hub.md "
               "\"A question's class\"): rule (a standing rule, or a delegation run line), "
               "machine, cross-repo, or go (a one-off landing or archive go). A question needs "
               "record or class, never both: refused (invalid, field class) otherwise.")
_UNFILED = {"type": "string", "description": "Why a question bound to a record is handled while "
                                             "its answer could not be filed there for good "
                                             "(not_found, invalid, no_store); stored on the "
                                             "question. Refused (invalid, field filing) while "
                                             "the filing can still be retried."}


def _obj(props, required=()):
    return {"type": "object", "properties": props, "required": list(required),
            "additionalProperties": False}


def _one_of(values, what):
    return {"type": "string", "enum": list(values), "description": what}


# name -> (description, input schema, HTTP method, path below hubapi.BASE, the role the tool
# calls as: ANY for a read, AGENT for a write, the route's role in hubapi.ROUTES being AGENT or
# ANY). AgentClient carries the agent's token whatever the role says.
# A `{name}` in the path is filled from the argument of that name; every other argument is the
# query of a GET or the body of a POST.
TOOLS = {
    "hub_pending": (
        "The owner input not yet handled (GET /pending), as {ok, items}; with `for`, only the "
        "items for that session key.",
        _obj({"for": _FOR}), "GET", "/pending", ANY),
    "hub_wait": (
        "Waits for the hub to change (GET /wait): returns {revision, reset, timed_out, changes, "
        "pending} once the store's revision moves past `since`, or after timeout_s (default 30, "
        "at most 55) with timed_out true. pending: also return at once while items wait. for "
        "(with pending true): wait for that session key's pending items only.",
        _obj({"since": {"type": "integer", "description": "The revision you last saw."},
              "timeout_s": {"type": "number", "description": "Seconds to wait, at most 55."},
              "pending": {"type": "boolean"}, "for": _FOR}), "GET", "/wait", ANY),
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
              "repo": _REPO, "from": _ASK_FROM, "refs": _REFS, "urgent": _URGENT,
              "story": {"type": "string", "description": "The story this question is about."},
              "record": _RECORD, "class": _one_of(hubapi.QUESTION_CLASSES, _CLASS_SAYS)},
             ["title"]), "POST", "/questions", AGENT),
    "hub_question_log": (
        "Marks an answered or filed question logged once its answer is in the ledger "
        "(POST /questions/<id>/log); no ledger id is given. Refused (invalid) for a question "
        "that is neither answered nor filed, a logged one included.",
        _obj({"id": _ID, "unfiled": _UNFILED, "if_version": _PIN}, ["id", "if_version"]),
        "POST", "/questions/{id}/log", AGENT),
    "hub_question_done": (
        "Marks an answered or filed question handled when it became no ruling "
        "(POST /questions/<id>/done); the question is then done. why says why the answer needs "
        "no ruling. Refused (invalid) for a question that is neither answered nor filed and for "
        "a blank why.",
        _obj({"id": _ID, "why": {"type": "string", "description": "Why the answer needs no "
                                                                   "ruling."},
              "unfiled": _UNFILED, "if_version": _PIN}, ["id", "why", "if_version"]),
        "POST", "/questions/{id}/done", AGENT),
    "hub_todo_add": (
        "Adds a to-do for the owner (POST /todos). An agent's open to-do needs commands or "
        "steps: what the owner runs. Refused: plain Notepad, 'open and apply' prose, and "
        "coordinator work (worktree add/remove, landings, gates, ref deletes).",
        _obj({"title": {"type": "string"}, "detail": {"type": "string"},
              "commands": {"type": "array", "items": {"type": "string"}},
              "steps": {"type": "array", "items": {"type": "object"},
                        "description": "Steps the hub turns into commands (after any given), "
                                       "each one of {edit: <absolute file>, old, new} (an exact "
                                       "replace of the one place old is; prints ok or MISS), "
                                       "{open: <absolute file>, line} (Notepad++) or {run: "
                                       "<command>} (the same on every OS) or {run: {windows: "
                                       "<PowerShell command>, linux: <bash command>}} (the form "
                                       "os names)."},
              "os": _one_of(todosteps.OSES, "The OS a per-OS run is rendered for (windows "
                                            "when absent)."),
              "repo": _REPO, "refs": _REFS, "urgent": _URGENT, "from": _FROM}, ["title"]),
        "POST", "/todos", AGENT),
    "hub_todo_ack": (
        "Marks an owner's to-do as read by the PM (POST /todos/<id>/ack).",
        _obj({"id": _ID, "if_version": _PIN}, ["id"]), "POST", "/todos/{id}/ack", AGENT),
    "hub_question_edit": (
        "Changes an open question you asked (POST /questions/<id>/edit): the fields given "
        "replace the old ones (options as a whole list). Refused (forbidden) unless from is the "
        "question's own (pm when it has none); refused (conflict) once it is answered.",
        _obj({"id": _ID, "from": _CREATOR, "if_version": _PIN, "title": {"type": "string"},
              "context": {"type": "string"},
              "options": {"type": "array", "items": {"type": "object"},
                          "description": "The choices: [{label, description}]."},
              "recommended": {"type": "integer", "description": "The index of the option you "
                                                                "recommend."},
              "refs": _REFS, "urgent": _URGENT, "record": _RECORD_EDIT,
              "class": _one_of(hubapi.QUESTION_CLASSES, _CLASS_SAYS)},
             ["id", "from", "if_version"]),
        "POST", "/questions/{id}/edit", AGENT),
    "hub_question_withdraw": (
        "Removes an open question you asked (POST /questions/<id>/delete): it leaves the Action "
        "Hub and the store. Refused as hub_question_edit is.",
        _obj({"id": _ID, "from": _CREATOR, "if_version": _PIN}, ["id", "from", "if_version"]),
        "POST", "/questions/{id}/delete", AGENT),
    "hub_todo_edit": (
        "Changes an open to-do you added (POST /todos/<id>/edit): the fields given replace the "
        "old ones (commands as a whole list) and are judged by hub_todo_add's rules. Refused "
        "(forbidden) unless from is the to-do's own (pm when it has none); refused (conflict) "
        "once it is done.",
        _obj({"id": _ID, "from": _CREATOR, "if_version": _PIN, "title": {"type": "string"},
              "detail": {"type": "string"}, "commands": {"type": "array",
                                                         "items": {"type": "string"}},
              "repo": _REPO, "refs": _REFS, "urgent": _URGENT}, ["id", "from", "if_version"]),
        "POST", "/todos/{id}/edit", AGENT),
    "hub_todo_withdraw": (
        "Removes an open to-do you added (POST /todos/<id>/delete): it leaves the Action Hub and "
        "the store. Refused as hub_todo_edit is.",
        _obj({"id": _ID, "from": _CREATOR, "if_version": _PIN}, ["id", "from", "if_version"]),
        "POST", "/todos/{id}/delete", AGENT),
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
        _obj({"id": _ID, "reply": {"type": "string"}, "if_version": _PIN}, ["id"]),
        "POST", "/to_pm/{id}/reply", AGENT),
    "hub_inbox_post": (
        "Puts a document in the owner's inbox (POST /inbox): an answer to something the owner "
        "asked, or a report.",
        _obj({"text": {"type": "string"}, "title": {"type": "string"},
              "ref": {"type": "string", "description": "The id it answers."},
              "asked": {"type": "string", "description": "What the owner asked."},
              "repo": _REPO, "from": _FROM, "refs": _REFS}, ["text"]), "POST", "/inbox", AGENT),
    "hub_page": (
        "A published page's HTML (GET /pages/<id>), as {ok, id, html, version}; not_found when "
        "it has no body.",
        _obj({"id": {"type": "string", "description": "The page's id."}}, ["id"]),
        "GET", "/pages/{id}", ANY),
    "hub_page_publish": (
        "Publishes an HTML page to the Action Hub (POST /pages), read from html_file. The page is "
        "checked here first as the hub checks it (kitlib.hubsite.pagecheck): at most %d bytes of "
        "UTF-8; a whole document is fine; no script, iframe, object, embed, form, link or base, "
        "a meta only charset utf-8 or name (%s) with content, a title only plain text; no on* "
        "handler, no URL that leaves the page (links are #fragments, images data: URIs, CSS "
        "url() data: or #); Mermaid must be rendered to inline svg first. Over a page already "
        "published, give its version as if_version."
        % (pagecheck.MAX_BYTES, ', '.join(pagecheck.META_NAMES)),
        _obj({"id": {"type": "string", "description": "The page's id: letters, digits, . _ -; "
                                                      "never shaped like a hub item or a ruling "
                                                      "(Q-1, R-12)."},
              "title": {"type": "string"},
              "html_file": {"type": "string", "description": "The absolute path of a UTF-8 HTML "
                                                             "file: the page's body."},
              "summary": {"type": "string"}, "repo": _REPO, "from": _FROM, "refs": _REFS,
              "if_version": {"type": "integer", "description": "The version of the page you "
                                                               "are publishing over (absent for "
                                                               "a new page)."}},
             ["id", "title", "html_file"]), "POST", "/pages", AGENT),
    "hub_approval_request": (
        "Asks the owner to approve a change to files (POST /approvals), built here from a git "
        "checkout (kitlib.hubapprove): each file's base is its blob at head (none for a new "
        "file), its new text the exact UTF-8 bytes of its content_file. Once approved, "
        "tools/commit.py --owner-approval hub:A-<n> writes and commits it. It only asks: it never "
        "approves, and it cannot ask for a delete. At most %d files, %d bytes each."
        % (hubapprovals.MAX_FILES, hubapprovals.MAX_CONTENT_BYTES),
        _obj({"checkout": {"type": "string", "description": "The absolute path of the git "
                                                            "checkout the change is for."},
              "title": {"type": "string"},
              "files": {"type": "array", "items": {"type": "object"},
                        "description": "The files: [{path, content_file}], path relative to the "
                                       "repository top with /, content_file the absolute path of "
                                       "the UTF-8 file holding its new text."},
              "repo": {"type": "string", "description": "The repository's remote URL or id "
                                                        "(default: the checkout's origin)."},
              "head": {"type": "string", "description": "The base commit, 40 hex digits "
                                                        "(default: the checkout's HEAD)."},
              "reason": {"type": "string"}},
             ["checkout", "title", "files"]), "POST", "/approvals", AGENT),
    "hub_handoff_ready": (
        "Records that this session checkpointed and stands down (POST /handoffs), beside its "
        "ready reply to the PM (skill session-handoff): the same kind and state note path. A "
        "successor told to in its first message records kind checkin once it has checked in. The "
        "hub stamps the time; GET /handoffs gives each session's newest record.",
        _obj({"kind": _one_of(hubapi.HANDOFF_KINDS, "The ready reply's word: handoff (ready to "
                                                    "hand off), restart or stop; or checkin, a "
                                                    "successor that has checked in."),
              "state_path": {"type": "string", "description": "The absolute path of the state "
                                                              "note, as in the ready reply."},
              "from": {"type": "string", "description": "Your launcher session key (pm for the "
                                                        "PM). The hub refuses any other value "
                                                        "(invalid)."}},
             ["kind", "state_path", "from"]), "POST", "/handoffs", AGENT),
}
assert not set(TOOLS) & set(OWNER_ONLY)
# a POST below no {id} that can still replace a document: publishing over a page
REPLACES = frozenset({"hub_page_publish"})


class NoAgentToken(hubserve.ServeError):
    """There is no usable agent token: the hub MCP server never falls back to the owner's."""


class AgentClient(hubclient.Client):
    """kitlib.hubsite.client.Client that carries the agent's token on every call, a route open to
    ANY included. The owner's token file is never opened."""

    def token(self, role):
        token = hubauth.read_token(self.tokens, AGENT)
        if not token:
            raise NoAgentToken("no usable agent token in %s: the hub MCP server carries the "
                               "agent's token only (tools/hub.py setup writes it)" % self.tokens)
        return token


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


def _inside(path, folder):
    """True when the real path `path` is `folder` or under it (both compared normcased)."""
    p, f = os.path.normcase(path), os.path.normcase(folder)
    try:
        return os.path.commonpath([p, f]) == f
    except ValueError:                          # two drives: never inside
        return False


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
        return AgentClient(port, tokens=self.tokens, ask=self.ask, via=hubserve.VIA_MCP)

    # ---------------------------------------------------------------- tools
    def tool_list(self):
        return [{"name": n, "description": d, "inputSchema": s,
                 "annotations": {"readOnlyHint": m == "GET",
                                 "destructiveHint": m != "GET" and ("{" in p or n in REPLACES)}}
                for n, (d, s, m, p, _) in TOOLS.items()]

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
        except NoAgentToken as e:
            return _error("no_agent_token", str(e))
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
        return _error("not_available", "%s is the owner's and is not available yet: %s"
                      % (name, GUARD_WAIT))

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
        return self.client().wait(a.get("since"), timeout=t, pending=a.get("pending", False),
                                  for_key=a.get("for"))

    def _t_hub_inbox(self, a):
        return {"ok": True, "items": self.client().inbox()}

    def _t_hub_todo_add(self, a):
        """The hub makes the steps into commands and detail (kitlib.hubsite.api.todo_steps), so
        they go to it as given. The same check runs here first, so a to-do it refuses (a bad
        step, a lone surrogate in a command, text the owner's to-do rules forbid) is refused
        with the hub's own words and never reaches the hub."""
        try:
            hubapi.todo_steps(a)
        except hubapi.HubError as e:
            raise ToolError(e.code, e.message, e.field)
        return self._route("hub_todo_add", a)

    def _t_hub_todo_edit(self, a):
        """The changed text is judged here first as the hub judges it (api.todos_edit), so an
        edit it refuses never reaches the hub."""
        try:
            hubapi.todo_steps({k: v for k, v in a.items()
                               if k not in ("id", "from", "if_version")}, judge=True)
        except hubapi.HubError as e:
            raise ToolError(e.code, e.message, e.field)
        return self._route("hub_todo_edit", a)

    def _page_file(self, path):
        """html_file as the real path to read, or ToolError (invalid, html_file). This process
        reads it outside the guard, so it reads only an .html or .htm file, judged by its real
        path (links and junctions followed), and never one inside the hub's token folder or its
        store: a token published as a page could be read back with hub_page."""
        def suffix(real):
            for p in (path, real):
                if os.path.splitext(p)[1].lower() not in PAGE_SUFFIXES:
                    return "must be an %s file (its real path is %s)" % (
                        " or ".join(PAGE_SUFFIXES), real)
            return None
        return self._readable(path, "html_file", "a page", suffix)

    def _readable(self, path, field, what, first=None):
        """The real path of the file `path` (argument `field`) this process may read for `what`,
        or ToolError (invalid, field): absolute, `first(real)` (a why or None) passed, and never
        inside the hub's token folder or its store, judged by its real path (links and
        junctions followed)."""
        def refuse(why):
            raise ToolError("invalid", "%s %s: %s" % (field, path, why), field)
        if not isinstance(path, str) or not os.path.isabs(path):
            refuse("must be an absolute path")
        real = os.path.realpath(path)
        why = first(real) if first else None
        if why:
            refuse(why)
        try:
            closed = [("the hub's token folder", self.tokens or hubauth.folder()),
                      ("the hub store", getattr(self.load_settings(), "store", None))]
        except (ConfigError, OSError, ValueError) as e:
            refuse("cannot tell where the hub keeps its tokens and store (%s), so it is not read"
                   % type(e).__name__)
        for name, folder in closed:
            if folder and _inside(real, os.path.realpath(folder)):
                refuse("is inside %s, which %s is never read from" % (name, what))
        return real

    def _t_hub_page_publish(self, a):
        """The body goes to the hub as text, read here from html_file (absolute: this process's
        working folder is not the caller's; which files it may name: _page_file). It is read
        with newline="" so its bytes are the file's. The hub's page check runs here first, so a
        page it refuses, too big or unsafe, is refused with the hub's words and never reaches it."""
        path = self._page_file(a["html_file"])
        try:
            with open(path, encoding="utf-8", newline="") as f:
                # a character is a byte at least: one past the cap is enough for the check to
                # refuse, and a huge file is never read whole
                html = f.read(pagecheck.MAX_BYTES + 1)
        except (OSError, ValueError) as e:
            raise ToolError("invalid", "cannot read html_file %s: %s" % (path, type(e).__name__),
                            "html_file")
        why = pagecheck.check(html)
        if why:
            raise ToolError("invalid", why, "html")
        body = {k: v for k, v in a.items() if k != "html_file"}
        body["html"] = html
        return self.client().post("/pages", AGENT, body)

    def _t_hub_approval_request(self, a):
        """The request is built here from the checkout (kitlib.hubapprove.build: its defaults,
        its refusals and the approvals module's limits), so one it refuses never reaches the
        hub. This process reads each content_file outside the guard, so the checkout and every
        content_file are absolute and judged as html_file is (_readable): never inside the hub's
        token folder or its store, or a token could be read back from the request."""
        checkout = self._readable(a["checkout"], "checkout", "an approval")
        files = []
        for f in a["files"]:
            if (not isinstance(f, dict) or set(f) != {"path", "content_file"}
                    or not all(isinstance(v, str) for v in f.values())):
                raise ToolError("invalid", "each file is {path, content_file}, both strings",
                                "files")
            files.append((f["path"], self._readable(f["content_file"], "files", "an approval")))
        try:
            body = hubapprove.build(checkout, a["title"], files, repo=a.get("repo"),
                                    head=a.get("head"), reason=a.get("reason"))
        except hubapprove.RequestError as e:
            raise ToolError("invalid", e.message, e.field)
        return hubapprove.request(self.client(), body)

    # ---------------------------------------------------------------- JSON-RPC
    def handle(self, msg):
        """One JSON-RPC message (or a batch list) -> the response, or None for notifications."""
        if isinstance(msg, list):
            if not msg:
                return _err(None, -32600, "invalid request: an empty batch")
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


_ok, _err, serve = mcpwire.ok, mcpwire.err, mcpwire.serve   # the wire both MCP servers share
