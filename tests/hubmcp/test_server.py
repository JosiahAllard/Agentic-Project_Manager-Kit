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
from kitlib.hubsite import api, approvals, auth, pagecheck, serve, todosteps
from tests.gitfixture import TempRepo
from tests.hubsite import httpfix
from tests.lock.helpers import REPO, clean_env
from tests.lockmcp.test_server import Client as Process
from tests.realgit import real_git
from tests.platforms import windows_only

ENTRY = os.path.join(REPO, "mcp", "hub_server.py")
PINNED = ("hub_answer", "hub_todo_done", "hub_story_submit", "hub_to_pm_send")
READS = {"hub_pending", "hub_wait", "hub_list", "hub_inbox", "hub_backlog", "hub_completed",
         "hub_view", "hub_health", "hub_page"}
CREATOR = {"hub_question_edit", "hub_question_withdraw", "hub_todo_edit", "hub_todo_withdraw"}
WRITES = {"hub_ask", "hub_question_log", "hub_question_done", "hub_todo_add", "hub_todo_ack", "hub_story_set",
          "hub_to_pm_reply", "hub_inbox_post", "hub_page_publish",
          "hub_approval_request", "hub_handoff_ready"} | CREATOR


class Settings(object):
    def __init__(self, port, store=None):
        self.port = port
        self.store = store


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
        additive = {"hub_ask", "hub_todo_add", "hub_inbox_post", "hub_approval_request",
                    "hub_handoff_ready"}
        changes = {"hub_question_log", "hub_question_done", "hub_todo_ack", "hub_story_set",
                   "hub_to_pm_reply", "hub_page_publish"} | CREATOR    # publish replaces a page
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
                                     {"id": "Q 1/x", "if_version": 3})
        self.assertFalse(is_error, payload)
        call = self.ask.calls[-1]
        self.assertEqual((call["method"], call["path"]),
                         ("POST", api.BASE + "/questions/Q%201%2Fx/log"))
        self.assertEqual(call["body"], {"if_version": 3})
        self.assertEqual(call["token"], self.agent)
        self.assertEqual(call["via"], serve.VIA_MCP)
        self.assertEqual(call["port"], 8790)

    def test_the_log_and_reply_tools_take_no_ruling_id(self):
        for name, args in (("hub_question_log", {"id": "Q-1", "if_version": 3, "ruling": "R-9"}),
                           ("hub_question_log", {"id": "Q-1", "if_version": 3, "replace": True}),
                           ("hub_to_pm_reply", {"id": "P-1", "reply": "r", "ruling": "R-9"})):
            with self.subTest(name=name, args=sorted(args)):
                payload, is_error = self.rpc(self.server(), name, args)
                self.assertTrue(is_error, payload)
                self.assertIn("unknown argument", json.dumps(payload))
        self.assertEqual(self.ask.calls, [], "a refused call asks the hub nothing")
        self.assertEqual(srv.TOOLS["hub_question_log"][1]["required"], ["id", "if_version"])
        self.assertNotIn("ruling", srv.TOOLS["hub_to_pm_reply"][1]["properties"])

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
                ("hub_question_log", {"id": " ", "if_version": 1}, "id"),
                ("hub_question_done", {"id": "Q-1", "if_version": 1}, "why"),
                ("hub_question_done", {"id": "Q-1", "why": "w"}, "if_version"),
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

    def test_os_names_the_target_of_a_per_os_run_and_posts_as_given(self):
        os_ = srv.TOOLS["hub_todo_add"][1]["properties"]["os"]
        self.assertEqual(os_["enum"], list(todosteps.OSES))
        steps = [{"run": {"windows": "Get-Date", "linux": "date"}}]
        args = {"title": "t", "steps": steps, "os": "linux"}
        payload, is_error = self.rpc(self.server(), "hub_todo_add", args)
        self.assertFalse(is_error, payload)
        self.assertEqual(self.ask.calls[-1]["body"], args)
        payload, is_error = self.rpc(self.server(), "hub_todo_add", dict(args, os="mac"))
        self.assertTrue(is_error)
        self.assertEqual((payload["error"]["code"], payload["error"]["field"]), ("invalid", "os"))
        self.assertEqual(len(self.ask.calls), 1)

    def test_the_steps_description_names_the_per_os_run(self):
        says = srv.TOOLS["hub_todo_add"][1]["properties"]["steps"]["description"]
        self.assertNotIn("{run: <PowerShell command>}", says)
        for word in ("windows", "linux", "os"):
            self.assertIn(word, says)


class Pages(Case):
    """hub_page_publish reads its body from an absolute file and checks it as the hub does
    (kitlib.hubsite.pagecheck) before any call; hub_page reads one back. No tool deletes a page:
    that is the owner's, on the hub page only."""

    def setUp(self):
        Case.setUp(self)
        self.dir = tempfile.mkdtemp(prefix="kit-hubmcp-page-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def page(self, text, name="page.html"):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        return path

    def test_the_file_is_read_and_posted_as_html(self):
        html = "<h1>Plan</h1>\n<p>café</p>\n"
        args = {"id": "plan", "title": "The plan", "html_file": self.page(html),
                "summary": "s", "refs": ["B-1"], "from": "coord", "if_version": 2}
        payload, is_error = self.rpc(self.server(), "hub_page_publish", args)
        self.assertFalse(is_error, payload)
        call = self.ask.calls[-1]
        self.assertEqual((call["method"], call["path"], call["token"], call["via"]),
                         ("POST", api.BASE + "/pages", self.agent, serve.VIA_MCP))
        want = {k: v for k, v in args.items() if k != "html_file"}
        want["html"] = html
        self.assertEqual(call["body"], want)

    def test_what_the_check_refuses_never_reaches_the_hub(self):
        cap = pagecheck.MAX_BYTES
        for text, said in (("<script>x</script>", "<script>"),
                           ('<div class="mermaid">graph TD</div>', "render it to svg first"),
                           ('<a href="https://example.com">x</a>', "#fragment"),
                           ("x" * (cap + 1), "bytes"),
                           ("é" * (cap // 2 + 1), "bytes")):
            with self.subTest(said=said):
                payload, is_error = self.rpc(self.server(), "hub_page_publish", {
                    "id": "plan", "title": "t", "html_file": self.page(text)})
                self.assertTrue(is_error)
                self.assertEqual((payload["error"]["code"], payload["error"]["field"]),
                                 ("invalid", "html"))
                self.assertIn(said, payload["error"]["message"])
        payload, is_error = self.rpc(self.server(), "hub_page_publish", {
            "id": "plan", "title": "t", "html_file": self.page("x" * cap)})
        self.assertFalse(is_error, payload)                     # the cap itself is let through
        self.assertEqual(len(self.ask.calls), 1)

    def test_a_file_that_is_absent_relative_or_not_utf8_is_refused_before_any_call(self):
        bad = os.path.join(self.dir, "bad.html")
        with open(bad, "wb") as f:
            f.write(b"<p>\xff</p>")
        for path in (os.path.join(self.dir, "absent.html"), "page.html", bad):
            with self.subTest(path=path):
                payload, is_error = self.rpc(self.server(), "hub_page_publish", {
                    "id": "plan", "title": "t", "html_file": path})
                self.assertTrue(is_error)
                self.assertEqual((payload["error"]["code"], payload["error"]["field"]),
                                 ("invalid", "html_file"))
        self.assertEqual(self.ask.calls, [])

    def refused_unread(self, path, server=None):
        """hub_page_publish on `path` is invalid/html_file, with no file opened and no call."""
        opened = mock.Mock(side_effect=AssertionError("opened %s" % path))
        with mock.patch.object(srv, "open", opened, create=True):
            payload, is_error = self.rpc(server or self.server(), "hub_page_publish", {
                "id": "plan", "title": "t", "html_file": path})
        self.assertTrue(is_error)
        self.assertEqual((payload["error"]["code"], payload["error"]["field"]),
                         ("invalid", "html_file"), payload)
        self.assertEqual((opened.call_count, self.ask.calls), (0, []))
        return payload["error"]["message"]

    def test_a_token_file_is_refused_before_any_read_or_call(self):
        names = sorted(os.listdir(self.tokens))
        self.assertTrue(names)
        for name in names:
            with self.subTest(name=name):
                self.refused_unread(os.path.join(self.tokens, name))

    def test_an_html_file_in_the_token_folder_or_the_store_is_refused(self):
        store = os.path.join(self.dir, "store")
        os.makedirs(store)
        for folder in (self.tokens, store):
            with self.subTest(folder=folder):
                path = os.path.join(folder, "x.html")
                with open(path, "w", encoding="utf-8") as f:
                    f.write("<p>x</p>")
                said = self.refused_unread(path, self.server(
                    load_settings=lambda: Settings(8790, store=store)))
                self.assertIn("never read from", said)

    def test_a_file_that_is_not_html_is_refused(self):
        for name in ("page.txt", "page.html.txt", "page", "owner.token"):
            with self.subTest(name=name):
                self.assertIn(".html", self.refused_unread(self.page("<p>x</p>", name)))

    def test_a_link_or_junction_into_the_token_folder_is_refused(self):
        inner = os.path.join(self.tokens, "x.html")
        with open(inner, "w", encoding="utf-8") as f:
            f.write("<p>x</p>")
        made = []
        link = os.path.join(self.dir, "link.html")
        try:
            os.symlink(inner, link)
            made.append(link)
        except (OSError, NotImplementedError):
            pass
        junction = os.path.join(self.dir, "junction")
        try:
            import _winapi
            _winapi.CreateJunction(self.tokens, junction)
            made.append(os.path.join(junction, "x.html"))
        except (ImportError, AttributeError, OSError):
            pass
        if not made:
            self.skipTest("this account can make neither a symbolic link nor a junction")
        for path in made:
            with self.subTest(path=path):
                self.assertIn("token folder", self.refused_unread(path))

    def test_line_endings_are_posted_as_the_file_has_them(self):
        html = "<p>a</p>\r\n<p>b</p>\n"
        payload, is_error = self.rpc(self.server(), "hub_page_publish", {
            "id": "plan", "title": "t", "html_file": self.page(html)})
        self.assertFalse(is_error, payload)
        self.assertEqual(self.ask.calls[-1]["body"]["html"], html)

    def test_the_hubs_refusal_comes_back(self):
        self.ask.answer = (409, api.error_body("conflict", "pages/plan exists", "if_version"))
        payload, is_error = self.rpc(self.server(), "hub_page_publish", {
            "id": "plan", "title": "t", "html_file": self.page("<p>x</p>")})
        self.assertTrue(is_error)
        self.assertEqual((payload["status"], payload["error"]["code"]), (409, "conflict"))

    def test_a_page_is_read_by_its_id(self):
        self.rpc(self.server(), "hub_page", {"id": "my plan"})
        self.assertEqual((self.ask.calls[-1]["method"], self.ask.calls[-1]["path"]),
                         ("GET", api.BASE + "/pages/my%20plan"))

    def test_no_tool_deletes_a_page(self):
        for name, (_, _, method, path, _) in srv.TOOLS.items():
            self.assertFalse(path.startswith("/pages/") and method != "GET", name)
            self.assertNotIn("delete", name)
        self.assertNotIn("hub_page_delete", hubmcp.OWNER_ONLY)


@real_git("kitlib.hubapprove.build hashes the content with hash-object --path --stdin: the "
          "fake takes no --path")
class Approvals(Case):
    """hub_approval_request builds the request from a checkout (kitlib.hubapprove, whose own
    tests are tests/hubapprove) and posts it; what it refuses never reaches the hub, and a
    content_file in the token folder or the store is never read."""

    URL = "https://git.example.invalid/owner/repo.git"

    def setUp(self):
        Case.setUp(self)
        self.repo = TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.repo.git("remote", "add", "origin", self.URL)
        with open(self.repo.path(".kit.json"), "w", encoding="utf-8") as f:
            # an owner approval carries owner-only files only (kitlib.hubapprove)
            json.dump({"owner": {"paths": ["README.md"]}}, f)
        self.dir = tempfile.mkdtemp(prefix="kit-hubmcp-approval-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.ask.answer = (200, {"ok": True, "id": "A-001", "version": 1})

    def content(self, data, folder=None, name="new.txt"):
        path = os.path.join(folder or self.dir, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def args(self, **kw):
        a = {"checkout": self.repo.root, "title": "Edit the readme"}
        if "files" not in kw:
            a["files"] = [{"path": "README.md", "content_file": self.content(b"a\r\nb\n")}]
        a.update(kw)
        return a

    def refused(self, field, args, server=None):
        payload, is_error = self.rpc(server or self.server(), "hub_approval_request", args)
        self.assertTrue(is_error)
        self.assertEqual((payload["error"]["code"], payload["error"]["field"]),
                         ("invalid", field), payload)
        self.assertEqual(self.ask.calls, [])
        return payload["error"]["message"]

    def test_the_request_is_posted_with_the_files_exact_bytes(self):
        payload, is_error = self.rpc(self.server(), "hub_approval_request",
                                     self.args(reason="asked"))
        self.assertFalse(is_error, payload)
        self.assertEqual(payload["id"], "A-001")
        call, = self.ask.calls
        self.assertEqual((call["method"], call["path"], call["token"], call["via"]),
                         ("POST", api.BASE + "/approvals", self.agent, serve.VIA_MCP))
        self.assertEqual(call["body"], {
            "repo": approvals.repo_id(self.URL), "title": "Edit the readme",
            "head": self.repo.initial, "reason": "asked",
            "files": [{"path": "README.md", "base_blob": approvals.blob_id("hello\n"),
                       "content": "a\r\nb\n"}]})

    def test_what_the_builder_refuses_never_reaches_the_hub(self):
        said = self.refused("files", self.args(files=[
            {"path": "README.md", "content_file": self.content(b"hello\n")}]))
        self.assertIn("would change nothing", said)
        self.refused("head", self.args(head="1" * 40))
        self.refused("files", self.args(files=[]))
        self.assertIn("not owner-only", self.refused("files", self.args(files=[
            {"path": "src/app.py", "content_file": self.content(b"a\n")}])))

    def test_a_relative_checkout_or_content_file_is_refused(self):
        self.assertIn("absolute", self.refused("checkout", self.args(checkout="repo")))
        self.assertIn("absolute", self.refused("files", self.args(files=[
            {"path": "README.md", "content_file": "new.txt"}])))

    def test_a_file_that_is_not_a_path_and_a_content_file_is_refused(self):
        for f in ("README.md", {"path": "README.md"},
                  {"path": "README.md", "content_file": 5},
                  {"path": "README.md", "content_file": "C:\\x", "base_blob": None}):
            with self.subTest(file=f):
                self.refused("files", self.args(files=[f]))

    def test_a_content_file_in_the_token_folder_or_the_store_is_never_read(self):
        store = os.path.join(self.dir, "store")
        os.makedirs(store)
        server = self.server(load_settings=lambda: Settings(8790, store=store))
        paths = [os.path.join(self.tokens, n) for n in sorted(os.listdir(self.tokens))]
        paths.append(self.content(b"x\n", store, "x.txt"))
        for path in paths:
            with self.subTest(path=path):
                args = self.args(files=[{"path": "README.md", "content_file": path}])
                opened = mock.Mock(side_effect=AssertionError("opened %s" % path))
                with mock.patch("builtins.open", opened):
                    said = self.refused("files", args, server)
                self.assertIn("never read from", said)
                self.assertEqual(opened.call_count, 0)

    def test_the_hubs_refusal_comes_back(self):
        self.ask.answer = (400, api.error_body("invalid", "no checkout of it here", "repo"))
        payload, is_error = self.rpc(self.server(), "hub_approval_request", self.args())
        self.assertTrue(is_error)
        self.assertEqual((payload["status"], payload["error"]["code"],
                          payload["error"]["message"]), (400, "invalid", "no checkout of it here"))


class Creator(Case):
    """The creator's edit and withdraw tools: from and if_version are required, the to-do edit is
    judged before any call, and each posts to its route."""

    def test_from_and_if_version_are_required(self):
        for name in sorted(CREATOR):
            for args, field in (({"id": "X-1", "if_version": 1}, "from"),
                                ({"id": "X-1", "from": "pm"}, "if_version")):
                with self.subTest(name=name, field=field):
                    payload, is_error = self.rpc(self.server(), name, args)
                    self.assertTrue(is_error)
                    self.assertEqual((payload["error"]["code"], payload["error"]["field"]),
                                     ("invalid", field))
        self.assertEqual(self.ask.calls, [])

    def test_each_posts_its_route_with_the_agent_token(self):
        for name, path in (("hub_question_edit", "/questions/Q-1/edit"),
                           ("hub_question_withdraw", "/questions/Q-1/delete"),
                           ("hub_todo_edit", "/todos/Q-1/edit"),
                           ("hub_todo_withdraw", "/todos/Q-1/delete")):
            with self.subTest(name=name):
                args = {"id": "Q-1", "from": "pm", "if_version": 2}
                if name.endswith("edit"):
                    args["title"] = "t"
                payload, is_error = self.rpc(self.server(), name, args)
                self.assertFalse(is_error, payload)
                call = self.ask.calls[-1]
                self.assertEqual((call["method"], call["path"], call["token"]),
                                 ("POST", api.BASE + path, self.agent))
                self.assertEqual(call["body"], {k: v for k, v in args.items() if k != "id"})

    def test_a_record_is_passed_to_the_hub_as_given_and_null_unbinds(self):
        bind = {"repo": "pm", "id": "KIT-X"}
        for name, args in (("hub_ask", {"title": "t", "record": bind}),
                           ("hub_question_edit", {"id": "Q-1", "from": "pm", "if_version": 2,
                                                  "record": bind}),
                           ("hub_question_edit", {"id": "Q-1", "from": "pm", "if_version": 2,
                                                  "record": None})):
            with self.subTest(name=name, record=args["record"]):
                payload, is_error = self.rpc(self.server(), name, args)
                self.assertFalse(is_error, payload)
                self.assertIn("record", self.ask.calls[-1]["body"])
                self.assertEqual(self.ask.calls[-1]["body"]["record"], args["record"])
        said = srv.TOOLS["hub_ask"][1]["properties"]["record"]
        self.assertEqual((said["type"], said["required"]), ("object", ["repo", "id"]))
        # the kit's own records are under the PM repository's folder name, never 'pm'
        self.assertIn("the PM repository's folder name", said["description"])
        self.assertIn("hub_view view 'repos'", said["description"])
        self.assertNotIn("records: pm", said["description"])
        self.assertIn("null unbinds",
                      srv.TOOLS["hub_question_edit"][1]["properties"]["record"]["description"])

    def test_a_class_and_unfiled_are_passed_to_the_hub_as_given(self):
        said = srv.TOOLS["hub_ask"][1]["properties"]["class"]
        self.assertEqual(said["enum"], list(api.QUESTION_CLASSES))
        self.assertIn("record", said["description"])
        for name, args in (("hub_ask", {"title": "t", "class": "go"}),
                           ("hub_question_edit", {"id": "Q-1", "from": "pm", "if_version": 2,
                                                  "class": "rule", "record": None}),
                           ("hub_question_log", {"id": "Q-1", "if_version": 2,
                                                 "unfiled": "the record is closed"}),
                           ("hub_question_done", {"id": "Q-1", "why": "w", "if_version": 2,
                                                  "unfiled": "the record is closed"})):
            with self.subTest(name=name):
                payload, is_error = self.rpc(self.server(), name, args)
                self.assertFalse(is_error, payload)
                self.assertEqual(self.ask.calls[-1]["body"],
                                 {k: v for k, v in args.items() if k != "id"})
        calls = len(self.ask.calls)
        payload, is_error = self.rpc(self.server(), "hub_ask", {"title": "t", "class": "other"})
        self.assertTrue(is_error)
        self.assertEqual((payload["error"]["code"], payload["error"]["field"]),
                         ("invalid", "class"))
        self.assertEqual(len(self.ask.calls), calls)

    def test_a_record_that_is_no_object_never_reaches_the_hub(self):
        for name, args in (("hub_ask", {"title": "t", "record": "pm:KIT-X"}),
                           ("hub_ask", {"title": "t", "record": None}),
                           ("hub_question_edit", {"id": "Q-1", "from": "pm", "if_version": 2,
                                                  "record": "pm:KIT-X"})):
            with self.subTest(name=name, record=args["record"]):
                payload, is_error = self.rpc(self.server(), name, args)
                self.assertTrue(is_error)
                self.assertEqual((payload["error"]["code"], payload["error"]["field"]),
                                 ("invalid", "record"))
        self.assertEqual(self.ask.calls, [])

    def test_a_to_do_edit_the_hub_would_refuse_never_reaches_it(self):
        for args, field in (({"commands": ["git worktree add x"]}, "commands"),
                            ({"detail": "Open it and apply the change."}, "detail")):
            with self.subTest(args=args):
                payload, is_error = self.rpc(self.server(), "hub_todo_edit",
                                             dict(args, id="T-1", **{"from": "pm",
                                                                     "if_version": 1}))
                self.assertTrue(is_error)
                self.assertEqual((payload["error"]["code"], payload["error"]["field"]),
                                 ("invalid", field))
        self.assertEqual(self.ask.calls, [])


class Errors(Case):
    def test_a_refusal_comes_back_as_the_hubs_error(self):
        self.ask.answer = (409, api.error_body("conflict", "Q-1 changed", "if_version"))
        payload, is_error = self.rpc(self.server(), "hub_question_log",
                                     {"id": "Q-1", "if_version": 1})
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
        store = os.path.join(self.root, "store")        # the folder httpfix opened the store in
        self.mcp = srv.Server(port=self.lis.port, tokens=self.tokens,
                              load_settings=lambda: Settings(self.lis.port, store=store))

    def test_an_agent_asks_lists_and_logs_through_the_listener(self):
        payload, is_error = self.mcp.call("hub_ask", {"title": "which?", "class": "go",
                                                      "options": httpfix.OPTIONS})
        self.assertFalse(is_error, payload)
        doc_id = payload["id"]
        doc = self.store.docs("questions")[doc_id]
        self.assertEqual((doc["title"], doc["via"]), ("which?", serve.VIA_MCP))
        payload, is_error = self.mcp.call("hub_list", {"collection": "questions"})
        self.assertFalse(is_error, payload)
        self.assertEqual([i["id"] for i in payload["items"]], [doc_id])
        payload, is_error = self.mcp.call("hub_question_log",
                                          {"id": doc_id, "if_version": 1})
        self.assertTrue(is_error)
        self.assertEqual(payload["error"]["code"], "invalid")      # not answered yet: the hub says
        self.assertEqual(payload["status"], 400)
        self.assertEqual(self.errors(), [])

    def test_an_answered_question_is_marked_done_with_a_why_and_no_ruling(self):
        payload, is_error = self.mcp.call("hub_ask", {"title": "which?", "class": "go",
                                                      "options": httpfix.OPTIONS})
        qid = payload["id"]
        payload, is_error = self.mcp.call("hub_question_done",
                                          {"id": qid, "why": "no ruling", "if_version": 1})
        self.assertTrue(is_error)                       # not answered yet: the hub refuses
        self.assertEqual((payload["status"], payload["error"]["code"]), (400, "invalid"))
        got = httpfix.post(self.lis.port, api.BASE + "/questions/%s/answer" % qid,
                           {"choice": 0}, token=self.owner)
        self.assertEqual(got.status, 200, got.body)
        version = self.store.docs("questions")[qid]["version"]
        payload, is_error = self.mcp.call("hub_question_done",
                                          {"id": qid, "why": "  ", "if_version": version})
        self.assertTrue(is_error)                       # a blank why
        self.assertEqual((payload["error"]["code"], payload["error"]["field"]),
                         ("invalid", "why"))
        payload, is_error = self.mcp.call("hub_question_done",
                                          {"id": qid, "why": " go once ", "if_version": version})
        self.assertFalse(is_error, payload)
        doc = self.store.docs("questions")[qid]
        self.assertEqual((doc["status"], doc["done_why"]), ("done", "go once"))
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

    def test_a_to_do_for_linux_gets_the_linux_form_of_a_per_os_run(self):
        steps = [{"run": {"windows": "Get-Date", "linux": "date"}}]
        payload, is_error = self.mcp.call("hub_todo_add", {"title": "t", "steps": steps,
                                                           "os": "linux"})
        self.assertFalse(is_error, payload)
        doc = self.store.docs("todos")[payload["id"]]
        self.assertEqual(doc["commands"], todosteps.render(steps, "linux"))
        self.assertNotEqual(doc["commands"], todosteps.render(steps))
        self.assertNotIn("os", doc)
        self.assertEqual(self.errors(), [])

    def test_the_creator_edits_and_withdraws_through_the_listener(self):
        payload, is_error = self.mcp.call("hub_ask", {"title": "which?", "class": "go",
                                                      "options": httpfix.OPTIONS})
        self.assertFalse(is_error, payload)
        qid = payload["id"]
        payload, is_error = self.mcp.call("hub_question_edit", {"id": qid, "from": "someone",
                                                                "if_version": 1, "title": "x"})
        self.assertTrue(is_error)
        self.assertEqual((payload["status"], payload["error"]["code"]), (403, "forbidden"))
        payload, is_error = self.mcp.call("hub_question_edit", {"id": qid, "from": "pm",
                                                                "if_version": 1, "title": "x"})
        self.assertFalse(is_error, payload)
        self.assertEqual(self.store.docs("questions")[qid]["title"], "x")
        payload, is_error = self.mcp.call("hub_question_withdraw",
                                          {"id": qid, "from": "pm", "if_version": 2})
        self.assertFalse(is_error, payload)
        self.assertNotIn(qid, self.store.docs("questions"))
        payload, is_error = self.mcp.call("hub_todo_add", {"title": "t", "commands": ["dir"],
                                                           "from": "coord"})
        tid = payload["id"]
        payload, is_error = self.mcp.call("hub_todo_edit", {"id": tid, "from": "coord",
                                                            "if_version": 1, "commands": ["ver"]})
        self.assertFalse(is_error, payload)
        self.assertEqual(self.store.docs("todos")[tid]["commands"], ["ver"])
        payload, is_error = self.mcp.call("hub_todo_withdraw",
                                          {"id": tid, "from": "coord", "if_version": 2})
        self.assertFalse(is_error, payload)
        self.assertNotIn(tid, self.store.docs("todos"))
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

    def test_a_page_is_published_and_read_back_through_the_listener(self):
        folder = tempfile.mkdtemp(prefix="kit-hubmcp-live-")
        self.addCleanup(shutil.rmtree, folder, True)
        path = os.path.join(folder, "plan.html")
        html = "<h1>Plan</h1><p>café</p>"
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        payload, is_error = self.mcp.call("hub_page_publish",
                                          {"id": "plan", "title": "The plan", "html_file": path})
        self.assertFalse(is_error, payload)
        card = self.store.docs("pages")["plan"]
        self.assertEqual((card["title"], card["via"], card["bytes"]),
                         ("The plan", serve.VIA_MCP, len(html.encode("utf-8"))))
        payload, is_error = self.mcp.call("hub_page", {"id": "plan"})
        self.assertFalse(is_error, payload)
        self.assertEqual(payload["html"], html)
        payload, is_error = self.mcp.call("hub_page_publish",
                                          {"id": "plan", "title": "again", "html_file": path})
        self.assertTrue(is_error)                       # over a page: its version, or a conflict
        self.assertEqual((payload["status"], payload["error"]["code"]), (409, "conflict"))
        inside = os.path.join(self.root, "store", "x.html")    # the live store's own folder
        with open(inside, "w", encoding="utf-8") as f:
            f.write(html)
        payload, is_error = self.mcp.call("hub_page_publish",
                                          {"id": "x", "title": "x", "html_file": inside})
        self.assertTrue(is_error)
        self.assertEqual((payload["error"]["code"], payload["error"]["field"]),
                         ("invalid", "html_file"))
        self.assertNotIn("x", self.store.docs("pages"))
        self.assertEqual(self.errors(), [])


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
            payload, is_error = self.mcp.call("hub_ask", {"title": "t", "from": who, "class": "go"})
            self.assertFalse(is_error, payload)
        payload, is_error = self.mcp.call("hub_ask", {"title": "t", "from": "some agent",
                                                      "class": "go"})
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


@windows_only("the kit is Windows only")
class HandoffReady(Case):
    """hub_handoff_ready posts a session's hand-off record (docs/action-hub.md
    "handoffs/C-NNN"): kind, state_path and from, as given; a kind the hub never takes is
    refused here."""

    ARGS = {"kind": "restart", "state_path": r"C:\t\state-k.md", "from": "kit"}

    def test_the_record_posts_as_given_to_the_handoffs_route(self):
        payload, is_error = self.rpc(self.server(), "hub_handoff_ready", dict(self.ARGS))
        self.assertFalse(is_error, payload)
        call = self.ask.calls[-1]
        self.assertEqual((call["method"], call["path"]), ("POST", api.BASE + "/handoffs"))
        self.assertEqual(call["body"], self.ARGS)
        self.assertEqual(call["token"], self.agent)

    def test_kind_is_the_ready_reply_word_and_every_argument_is_required(self):
        schema = srv.TOOLS["hub_handoff_ready"][1]
        self.assertEqual(schema["properties"]["kind"]["enum"], ["handoff", "restart", "stop"])
        self.assertEqual(sorted(schema["required"]), ["from", "kind", "state_path"])
        payload, is_error = self.rpc(self.server(), "hub_handoff_ready",
                                     dict(self.ARGS, kind="pause"))
        self.assertTrue(is_error)
        self.assertEqual((payload["error"]["code"], payload["error"]["field"]), ("invalid", "kind"))
        self.assertEqual(self.ask.calls, [])

    def test_the_hubs_refusal_of_a_key_comes_back_as_a_result(self):
        self.ask.answer = (400, api.error_body("invalid", "from 'x' is not a session", "from"))
        payload, is_error = self.rpc(self.server(), "hub_handoff_ready",
                                     dict(self.ARGS, **{"from": "x"}))
        self.assertTrue(is_error)
        self.assertEqual((payload["status"], payload["error"]["field"]), (400, "from"))


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
