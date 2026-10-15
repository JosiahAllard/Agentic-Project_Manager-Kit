"""The hub MCP server (kitlib.hubmcp, docs/hub-mcp.md): its tools against a stand-in ask and
against a real loopback listener on port 0, the pinned owner tools, the JSON-RPC, and
mcp/hub_server.py as a process."""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from kitlib import hubmcp
from kitlib.config.types import ConfigError
from kitlib.hubmcp import server as srv
from kitlib.hubsite import api, auth, serve, todosteps
from tests.hubsite import httpfix
from tests.lock.helpers import REPO, clean_env
from tests.lockmcp.test_server import Client as Process

ENTRY = os.path.join(REPO, "mcp", "hub_server.py")
PINNED = ("hub_answer", "hub_todo_done", "hub_story_submit", "hub_to_pm_send")
READS = {"hub_pending", "hub_wait", "hub_list", "hub_inbox", "hub_backlog", "hub_completed",
         "hub_view", "hub_health"}
WRITES = {"hub_ask", "hub_question_log", "hub_todo_add", "hub_todo_ack", "hub_story_set",
          "hub_to_pm_reply", "hub_inbox_post"}


class Settings(object):
    def __init__(self, port):
        self.port = port


class FakeAsk(object):
    """serve.ask's stand-in: records each call and answers `answer` (or raises it)."""

    def __init__(self, answer=(200, {"ok": True})):
        self.calls = []
        self.answer = answer

    def __call__(self, port, method, path, token=None, body=None, timeout=None, via=None):
        self.calls.append({"port": port, "method": method, "path": path, "token": token,
                           "body": body, "timeout": timeout, "via": via})
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class Case(unittest.TestCase):
    def setUp(self):
        root = tempfile.mkdtemp(prefix="kit-hubmcp-")
        self.addCleanup(shutil.rmtree, root, True)
        self.tokens = os.path.join(root, "tokens")
        auth.ensure_tokens(self.tokens)
        self.owner = auth.read_token(self.tokens, auth.OWNER)
        self.agent = auth.read_token(self.tokens, auth.AGENT)
        self.ask = FakeAsk()

    def server(self, **kw):
        kw.setdefault("tokens", self.tokens)
        kw.setdefault("ask", self.ask)
        kw.setdefault("load_settings", lambda: Settings(8790))
        return srv.Server(**kw)

    def rpc(self, server, name, arguments=None):
        r = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": name, "arguments": arguments or {}}})
        res = r["result"]
        payload = json.loads(res["content"][0]["text"])
        self.assertEqual(res["structuredContent"], payload)
        return payload, res["isError"]


class Pinned(Case):
    """The owner's tools are named now and ship once the guard denies them to agents."""

    def test_the_pinned_names_are_these(self):
        self.assertEqual(hubmcp.OWNER_ONLY, PINNED)
        self.assertEqual(set(hubmcp.OWNER_ROUTES), set(PINNED))

    def test_each_pinned_name_maps_to_an_owner_route(self):
        for name in PINNED:
            with self.subTest(name=name):
                method, path = hubmcp.OWNER_ROUTES[name]
                route, _ = api.match(method, api.BASE + path.replace("{id}", "X-1"))
                self.assertEqual(route.role, api.OWNER)

    def test_no_pinned_name_is_listed(self):
        names = {t["name"] for t in self.server().tool_list()}
        self.assertEqual(names & set(PINNED), set())
        self.assertEqual(names, READS | WRITES)

    def test_destructive_hint_is_only_on_tools_that_change_an_existing_document(self):
        additive = {"hub_ask", "hub_todo_add", "hub_inbox_post"}
        changes = {"hub_question_log", "hub_todo_ack", "hub_story_set", "hub_to_pm_reply"}
        self.assertEqual(additive | changes, WRITES)
        for t in self.server().tool_list():
            ann = t["annotations"]
            with self.subTest(name=t["name"]):
                self.assertEqual(ann["readOnlyHint"], t["name"] in READS)
                self.assertEqual(ann["destructiveHint"], t["name"] in changes)

    def test_a_pinned_name_is_refused_and_never_asks(self):
        for marked, code in ((None, "not_available"), ("coordinator", "forbidden")):
            s = self.server(marked=marked)
            for name in PINNED:
                with self.subTest(name=name, marked=marked):
                    payload, is_error = self.rpc(s, name, {"id": "Q-1", "choice": 0})
                    self.assertTrue(is_error)
                    self.assertEqual(payload["error"]["code"], code)
        self.assertEqual(self.ask.calls, [])

    def test_the_not_available_answer_says_what_it_waits_on(self):
        payload, _ = self.server().call("hub_answer", {})
        said = payload["error"]["message"]
        self.assertIn(srv.GUARD_WAIT, said)
        # the guard's hub-mcp rule is in place (kitlib/guard/hub.py): the wait is for the tools
        # themselves, never for the guard
        self.assertIn("the guard already denies them", said)
        self.assertIn("not shipped yet", said)
        self.assertNotIn("land once", said)

    def test_no_tool_reads_the_owner_token(self):
        roles = []
        real = auth.read_token

        def read(folder, role):
            roles.append(role)
            return real(folder, role)
        with mock.patch.object(auth, "read_token", read):
            s = self.server()
            for name in PINNED:
                s.call(name, {"id": "T-1"})
            self.assertEqual(roles, [])
            s.call("hub_todo_add", {"title": "t", "commands": ["x"]})
            s.call("hub_pending", {})
        self.assertEqual(roles, [auth.AGENT, auth.AGENT])
        self.assertEqual(len(self.ask.calls), 2)


class Routes(Case):
    """Each shipped tool calls a route the agent may call, with the agent's token on writes."""

    def test_every_shipped_tool_maps_to_a_route_that_is_not_the_owners(self):
        for name, (_, schema, method, path, role) in srv.TOOLS.items():
            fills = {"collection": srv.LISTS, "view": srv.VIEWS}
            values = [path]
            for key, choices in fills.items():
                if "{%s}" % key in path:
                    values = [path.replace("{%s}" % key, c) for c in choices]
            for p in values:
                with self.subTest(name=name, path=p):
                    got = api.match(method, api.BASE + p.replace("{id}", "X-1"))
                    self.assertIsNotNone(got)
                    self.assertNotEqual(got[0].role, api.OWNER)
            self.assertEqual(role, api.ANY if method == "GET" else api.AGENT, name)
            self.assertEqual(name in READS, method == "GET", name)

    def test_no_tool_and_no_api_route_stops_or_restarts_the_server(self):
        # the stop route is the loopback listener's own (httpd.stop_route), for hub.py's owner
        # token alone: no MCP tool, pinned or shipped, and no api route a tunnel serves
        paths = [t[3] for t in srv.TOOLS.values()] + [p for _, p in hubmcp.OWNER_ROUTES.values()]
        for path in paths:
            self.assertNotIn(path.rstrip("/").rsplit("/", 1)[-1], ("stop", "restart"), path)
        for route in api.ROUTES:
            self.assertFalse(route.pattern.endswith(("/stop", "/restart")), route.pattern)

    def test_a_write_carries_the_agent_token_and_names_itself_mcp(self):
        s = self.server()
        payload, is_error = self.rpc(s, "hub_question_log",
                                     {"id": "Q 1/x", "ruling": "R-9", "if_version": 3})
        self.assertFalse(is_error, payload)
        call = self.ask.calls[-1]
        self.assertEqual((call["method"], call["path"]),
                         ("POST", api.BASE + "/questions/Q%201%2Fx/log"))
        self.assertEqual(call["body"], {"ruling": "R-9", "if_version": 3})
        self.assertEqual(call["token"], self.agent)
        self.assertEqual(call["via"], serve.VIA_MCP)
        self.assertEqual(call["port"], 8790)

    def test_a_read_puts_its_arguments_in_the_query(self):
        self.rpc(self.server(), "hub_list", {"collection": "todos", "status": "all", "repo": "r"})
        self.rpc(self.server(), "hub_backlog", {"fresh": True})
        self.rpc(self.server(), "hub_view", {"view": "usage"})
        self.assertEqual([c["path"] for c in self.ask.calls],
                         [api.BASE + "/todos?repo=r&status=all", api.BASE + "/backlog?fresh=1",
                          api.BASE + "/usage"])
        self.assertEqual({c["method"] for c in self.ask.calls}, {"GET"})

    def test_the_port_is_read_from_the_settings_at_each_call(self):
        ports = [5001, 5002]
        s = self.server(load_settings=lambda: Settings(ports.pop(0)))
        self.rpc(s, "hub_health")
        self.rpc(s, "hub_health")
        self.assertEqual([c["port"] for c in self.ask.calls], [5001, 5002])

    def test_wait_is_capped_at_the_api_limit(self):
        self.ask.answer = (200, {"ok": True, "timed_out": False, "revision": 4})
        payload, is_error = self.rpc(self.server(), "hub_wait", {"since": 3, "timeout_s": 600})
        self.assertFalse(is_error)
        self.assertEqual(payload["revision"], 4)
        call = self.ask.calls[-1]
        self.assertIn("timeout=%d" % api.WAIT_MAX_S, call["path"])
        self.assertIn("since=3", call["path"])
        self.assertEqual(srv.WAIT_MAX_S, api.WAIT_MAX_S)

    def test_pending_and_wait_pass_for_through_as_the_query(self):
        self.ask.answer = (200, {"ok": True, "timed_out": False, "revision": 4, "items": []})
        s = self.server()
        for name, args in (("hub_pending", {"for": "k-one"}),
                           ("hub_wait", {"pending": True, "for": "k-one", "timeout_s": 0})):
            with self.subTest(name=name):
                payload, is_error = self.rpc(s, name, args)
                self.assertFalse(is_error, payload)
                self.assertIn("for=k-one", self.ask.calls[-1]["path"])
        self.assertIn("pending=1", self.ask.calls[-1]["path"])

    def test_pending_and_wait_without_for_send_none(self):
        self.ask.answer = (200, {"ok": True, "timed_out": False, "revision": 4, "items": []})
        s = self.server()
        for name, args in (("hub_pending", {}), ("hub_wait", {"pending": True, "timeout_s": 0}),
                           ("hub_pending", {"for": ""})):
            with self.subTest(name=name, args=args):
                payload, is_error = self.rpc(s, name, args)
                self.assertFalse(is_error, payload)
                self.assertNotIn("for=", self.ask.calls[-1]["path"])

    def test_the_from_of_a_question_is_described_as_the_hub_takes_it(self):
        said = srv.TOOLS["hub_ask"][1]["properties"]["from"]["description"]
        self.assertIn("launcher session key", said)
        self.assertIn(api.AGENT_FROM, said)
        self.assertNotIn("agent name", said)
        for name in ("hub_pending", "hub_wait"):
            self.assertEqual(srv.TOOLS[name][1]["properties"]["for"]["type"], "string", name)


class Arguments(Case):
    def test_bad_arguments_are_refused_before_any_call(self):
        for name, args, field in (
                ("hub_ask", {}, "title"),
                ("hub_ask", {"title": "t", "nope": 1}, "nope"),
                ("hub_ask", {"title": 5}, "title"),
                ("hub_list", {"collection": "inbox"}, "collection"),
                ("hub_story_set", {"id": "S-1", "status": "submitted", "if_version": 1}, "status"),
                ("hub_question_log", {"id": " ", "ruling": "R", "if_version": 1}, "id"),
                ("hub_todo_ack", {"id": "T-1", "if_version": True}, "if_version")):
            with self.subTest(name=name, args=args):
                payload, is_error = self.rpc(self.server(), name, args)
                self.assertTrue(is_error)
                self.assertEqual(payload["error"]["code"], "invalid")
                self.assertEqual(payload["error"]["field"], field)
        self.assertEqual(self.ask.calls, [])


class TodoSteps(Case):
    """hub_todo_add takes steps (kitlib.hubsite.todosteps): it posts them as given, for the hub
    to render (api.todo_steps), and refuses what an agent's to-do may not hold before any call."""

    def test_the_schema_takes_steps_as_a_list_of_objects(self):
        steps = srv.TOOLS["hub_todo_add"][1]["properties"]["steps"]
        self.assertEqual(steps, {"type": "array", "items": {"type": "object"},
                                 "description": steps["description"]})
        self.assertIn("edit", steps["description"])

    def test_steps_post_as_given_for_the_hub_to_render(self):
        steps = [{"edit": "C:\\w\\a.txt", "old": "x\ny", "new": "z"}, {"run": "python t.py"}]
        args = {"title": "t", "detail": "Why.", "commands": ["first"], "steps": steps}
        payload, is_error = self.rpc(self.server(), "hub_todo_add", args)
        self.assertFalse(is_error, payload)
        self.assertEqual(self.ask.calls[-1]["body"], args)

    def test_bad_steps_and_refused_text_never_reach_the_hub(self):
        for args, field in (({"steps": [{"open": "rel.txt"}]}, "steps"),
                            ({"steps": [{"edit": "C:\\w\\a.txt", "old": "a\ud800", "new": "b"}]},
                             "steps"),
                            ({"steps": [{"open": "C:\\w\\\udc00.txt"}]}, "steps"),
                            ({"steps": [{"run": "notepad x"}]}, "steps"),
                            ({"steps": [{"run": "echo a\ud800b"}]}, "steps"),
                            ({"commands": ["git worktree add x"]}, "commands"),
                            ({"commands": ["dir", "echo a\ud800b"]}, "commands"),
                            ({"detail": "Open it and apply the change."}, "detail")):
            with self.subTest(args=args):
                payload, is_error = self.rpc(self.server(), "hub_todo_add",
                                             dict(args, title="t"))
                self.assertTrue(is_error)
                self.assertEqual(payload["error"]["code"], "invalid")
                self.assertEqual(payload["error"]["field"], field)
        self.assertEqual(self.ask.calls, [])

    def test_a_to_do_without_steps_posts_as_before(self):
        self.rpc(self.server(), "hub_todo_add", {"title": "t", "commands": ["x"]})
        self.assertEqual(self.ask.calls[-1]["body"], {"title": "t", "commands": ["x"]})


class Errors(Case):
    def test_a_refusal_comes_back_as_the_hubs_error(self):
        self.ask.answer = (409, api.error_body("conflict", "Q-1 changed", "if_version"))
        payload, is_error = self.rpc(self.server(), "hub_question_log",
                                     {"id": "Q-1", "ruling": "R-1", "if_version": 1})
        self.assertTrue(is_error)
        self.assertEqual(payload, {"ok": False, "status": 409, "error": {
            "code": "conflict", "message": "Q-1 changed", "field": "if_version"}})

    def test_a_hub_that_is_not_running_says_so(self):
        self.ask.answer = serve.NotRunning(8790)
        payload, is_error = self.rpc(self.server(), "hub_pending")
        self.assertEqual((is_error, payload["error"]["code"]), (True, "not_running"))

    def test_other_failures_are_results_not_crashes(self):
        for answer, code in ((serve.ServeError("port 1 did not answer"), "client"),
                             (ValueError("boom"), "internal")):
            self.ask.answer = answer
            payload, is_error = self.rpc(self.server(), "hub_health")
            self.assertEqual((is_error, payload["error"]["code"]), (True, code))

    def test_a_wait_that_fails_is_a_result_like_any_other_tool(self):
        for answer, code in ((serve.NotRunning(8790), "not_running"),
                             (serve.ServeError("port 1 did not answer"), "client"),
                             (ValueError("boom"), "internal")):
            with self.subTest(code=code):
                self.ask.answer = answer
                payload, is_error = self.rpc(self.server(), "hub_wait",
                                             {"pending": True, "for": "k-one", "timeout_s": 0})
                self.assertEqual((is_error, payload["error"]["code"]), (True, code))
        self.ask.answer = (400, api.error_body("invalid", "for needs pending", "for"))
        payload, is_error = self.rpc(self.server(), "hub_wait", {"for": "k-one", "timeout_s": 0})
        self.assertTrue(is_error)
        self.assertEqual((payload["status"], payload["error"]["field"]), (400, "for"))

    def test_settings_that_cannot_be_read_are_a_result(self):
        def bad():
            raise ConfigError("hub.port must be a whole number")
        payload, is_error = self.rpc(self.server(load_settings=bad), "hub_health")
        self.assertEqual((is_error, payload["error"]["code"]), (True, "config"))

    def test_no_agent_token_is_its_own_error(self):
        s = self.server(tokens=os.path.join(self.tokens, "absent"))
        payload, is_error = self.rpc(s, "hub_inbox_post", {"text": "x"})
        self.assertEqual((is_error, payload["error"]["code"]), (True, "no_agent_token"))
        self.assertEqual(self.ask.calls, [])

    def test_a_read_never_falls_back_to_the_owner_token(self):
        opened = []
        real = open

        def spy(path, *a, **kw):
            opened.append(os.path.basename(path))
            return real(path, *a, **kw)
        agent_file = os.path.join(self.tokens, auth.TOKEN_FILES[auth.AGENT])
        for state in ("deleted", "unusable"):
            if state == "deleted":
                os.remove(agent_file)
            else:
                with open(agent_file, "w") as f:
                    f.write("short")
            self.assertIsNotNone(auth.read_token(self.tokens, auth.OWNER))
            for name, args in (("hub_pending", {}), ("hub_health", {}), ("hub_inbox", {}),
                               ("hub_wait", {"timeout_s": 0}), ("hub_ask", {"title": "t"})):
                with self.subTest(state=state, name=name):
                    with mock.patch("builtins.open", spy):
                        payload, is_error = self.rpc(self.server(), name, args)
                    self.assertEqual((is_error, payload["error"]["code"]),
                                     (True, "no_agent_token"))
                    self.assertIn("agent's token only", payload["error"]["message"])
        self.assertEqual(self.ask.calls, [])
        self.assertIn(auth.TOKEN_FILES[auth.AGENT], opened)
        self.assertNotIn(auth.TOKEN_FILES[auth.OWNER], opened)


class JsonRpc(Case):
    def test_initialize_ping_and_unknown(self):
        s = self.server()
        r = s.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": "2025-03-26"}})["result"]
        self.assertEqual(r["serverInfo"]["name"], hubmcp.SERVER_NAME)
        self.assertEqual(r["protocolVersion"], "2025-03-26")
        self.assertEqual(s.handle({"jsonrpc": "2.0", "id": 2, "method": "ping"})["result"], {})
        self.assertIsNone(s.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertEqual(s.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                   "params": {"name": "nope"}})["error"]["code"], -32602)
        self.assertEqual(s.handle({"jsonrpc": "2.0", "id": 4, "method": "x"})["error"]["code"],
                         -32601)
        self.assertEqual(s.handle({"id": 5})["error"]["code"], -32600)

    def test_an_empty_batch_is_an_invalid_request(self):
        r = self.server().handle([])
        self.assertEqual((r["id"], r["error"]["code"]), (None, -32600))
        out = io.BytesIO()
        srv.serve(self.server(), io.BytesIO(b"[]\n"), out)
        self.assertEqual(json.loads(out.getvalue())["error"]["code"], -32600)

    def test_a_line_that_is_not_json_is_a_parse_error(self):
        out = io.BytesIO()
        srv.serve(self.server(), io.BytesIO(b"{nope\n\n"), out)
        self.assertEqual(json.loads(out.getvalue())["error"]["code"], -32700)


class Live(httpfix.ListenerCase):
    """The tools against a real loopback listener on port 0: the hub's own checks hold."""

    def setUp(self):
        httpfix.ListenerCase.setUp(self)
        self.lis = self.listener()
        self.mcp = srv.Server(port=self.lis.port, tokens=self.tokens)

    def test_an_agent_asks_lists_and_logs_through_the_listener(self):
        payload, is_error = self.mcp.call("hub_ask", {"title": "which?",
                                                      "options": httpfix.OPTIONS})
        self.assertFalse(is_error, payload)
        doc_id = payload["id"]
        doc = self.store.docs("questions")[doc_id]
        self.assertEqual((doc["title"], doc["via"]), ("which?", serve.VIA_MCP))
        payload, is_error = self.mcp.call("hub_list", {"collection": "questions"})
        self.assertFalse(is_error, payload)
        self.assertEqual([i["id"] for i in payload["items"]], [doc_id])
        payload, is_error = self.mcp.call("hub_question_log",
                                          {"id": doc_id, "ruling": "R-1", "if_version": 1})
        self.assertTrue(is_error)
        self.assertEqual(payload["error"]["code"], "invalid")      # not answered yet: the hub says
        self.assertEqual(payload["status"], 400)
        self.assertEqual(self.errors(), [])

    def test_a_to_do_with_steps_is_rendered_by_the_hub(self):
        steps = [{"open": "C:\\w\\a.txt", "line": 3}]
        payload, is_error = self.mcp.call("hub_todo_add", {"title": "t", "commands": ["first"],
                                                           "steps": steps})
        self.assertFalse(is_error, payload)
        doc = self.store.docs("todos")[payload["id"]]
        self.assertEqual(doc["commands"], ["first"] + todosteps.render(steps))
        self.assertEqual(doc["detail"], "open C:\\w\\a.txt at line 3 in Notepad++")
        self.assertNotIn("steps", doc)
        self.assertEqual(self.errors(), [])

    def test_inbox_and_health_read_through_the_listener(self):
        payload, is_error = self.mcp.call("hub_inbox_post", {"text": "done", "title": "t"})
        self.assertFalse(is_error, payload)
        payload, is_error = self.mcp.call("hub_inbox", {})
        self.assertFalse(is_error, payload)
        self.assertEqual([(i["text"], i["via"]) for i in payload["items"]],
                         [("done", serve.VIA_MCP)])
        payload, is_error = self.mcp.call("hub_health", {})
        self.assertFalse(is_error, payload)
        self.assertNotIn("store", payload)      # the agent's token: where the store is stays hidden


class LiveKeys(httpfix.ListenerCase):
    """`from` and `for` against a real listener whose hub knows one launcher session key."""

    def setUp(self):
        httpfix.ListenerCase.setUp(self)
        self.api = api.Api(self.store, views=httpfix.Views(), clock=httpfix.CLOCK,
                           session_keys=lambda: {"k-one"})
        self.lis = self.listener()
        self.mcp = srv.Server(port=self.lis.port, tokens=self.tokens)

    def test_a_question_comes_from_a_session_key_or_pm_only(self):
        for who in ("k-one", api.AGENT_FROM):
            payload, is_error = self.mcp.call("hub_ask", {"title": "t", "from": who})
            self.assertFalse(is_error, payload)
        payload, is_error = self.mcp.call("hub_ask", {"title": "t", "from": "some agent"})
        self.assertTrue(is_error)
        self.assertEqual((payload["status"], payload["error"]["code"], payload["error"]["field"]),
                         (400, "invalid", "from"))

    def test_for_reaches_the_hub_on_pending_and_wait(self):
        payload, is_error = self.mcp.call("hub_pending", {"for": "k-one"})
        self.assertFalse(is_error, payload)
        self.assertEqual(payload["items"], [])
        payload, is_error = self.mcp.call("hub_wait",
                                          {"pending": True, "for": "k-one", "timeout_s": 0})
        self.assertFalse(is_error, payload)
        self.assertTrue(payload["timed_out"])
        # the hub's own check, not the tool's schema: `for` arrived without pending
        payload, is_error = self.mcp.call("hub_wait", {"for": "k-one", "timeout_s": 0})
        self.assertTrue(is_error)
        self.assertEqual((payload["status"], payload["error"]["field"]), (400, "for"))
        self.assertEqual(self.errors(), [])


@unittest.skipUnless(os.name == "nt", "the kit is Windows only")
class EntryPoint(unittest.TestCase):
    """mcp/hub_server.py itself: initialize and tools/list need no hub; the mark is read at start."""

    def test_initialize_and_list(self):
        c = Process(self, [sys.executable, ENTRY])
        r = c.initialize()["result"]
        self.assertEqual(r["serverInfo"]["name"], "hub")
        self.assertEqual(r["capabilities"], {"tools": {"listChanged": False}})
        names = {t["name"] for t in c.request("tools/list")["result"]["tools"]}
        self.assertEqual(names, READS | WRITES)

    def test_a_marked_session_is_refused_an_owner_tool(self):
        env = clean_env(PM_KIT_ROLE="coordinator")
        p = subprocess.run([sys.executable, ENTRY], input=json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "hub_answer", "arguments": {}}}).encode("utf-8") + b"\n",
            capture_output=True, env=env, cwd=REPO, timeout=60,
            creationflags=subprocess.CREATE_NO_WINDOW)
        res = json.loads(p.stdout.decode("utf-8"))["result"]
        self.assertTrue(res["isError"])
        self.assertEqual(res["structuredContent"]["error"]["code"], "forbidden")


class Manifest(unittest.TestCase):
    def test_the_plugin_starts_this_server(self):
        with open(os.path.join(REPO, ".claude-plugin", "plugin.json"), encoding="utf-8") as f:
            m = json.load(f)
        s = m["mcpServers"][hubmcp.SERVER_NAME]
        self.assertEqual(s["command"], "python")
        self.assertEqual(s["args"], ["${CLAUDE_PLUGIN_ROOT}/mcp/hub_server.py"])
        self.assertTrue(os.path.isfile(ENTRY))


if __name__ == "__main__":
    unittest.main()
