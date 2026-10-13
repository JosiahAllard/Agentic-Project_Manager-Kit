"""The lock MCP server (kitlib.lockmcp.server, docs/lock-mcp.md) driven over stdio as a client
would drive it, on a scratch store (tests/lockmcp/scratch_server.py) that tests/lock/helpers
isolates: its own folder and mutex names, fake hardware. The machine's store is never read or
written; the real entry point (mcp/lock_server.py) is only asked to initialize and list tools,
which reads no store."""
import json
import os
import queue
import subprocess
import sys
import threading
import unittest

from tests.lock.helpers import REPO, StoreCase, clean_env

if os.name == "nt":
    from kitlib.lock import api
    from kitlib.lockmcp import server as srv

LAUNCHER = os.path.join(REPO, "tests", "lockmcp", "scratch_server.py")
ENTRY = os.path.join(REPO, "mcp", "lock_server.py")
READS = {"lock_snapshot", "lock_wait_change", "lock_stats", "lock_run", "lock_hold"}
WRITES = {"lock_hold_create", "lock_hold_edit", "lock_hold_end", "lock_set_game_mode",
          "lock_set_games", "lock_set_division", "lock_end_run"}


class Client:
    """One server process: send(msg) writes a line, recv() reads one (timeout: the test fails
    rather than hangs)."""

    def __init__(self, test, argv):
        self.p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, env=clean_env(), cwd=REPO)
        test.addCleanup(self.close)
        self.lines = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        self.n = 0

    def _pump(self):
        for raw in iter(self.p.stdout.readline, b""):
            self.lines.put(raw)
        self.lines.put(None)

    def send_raw(self, data):
        self.p.stdin.write(data)
        self.p.stdin.flush()

    def recv(self, timeout=60):
        raw = self.lines.get(timeout=timeout)
        if raw is None:
            raise AssertionError("the server exited: %s" % self.p.stderr.read().decode(
                "utf-8", "replace"))
        return json.loads(raw.decode("utf-8"))

    def request(self, method, params=None):
        self.n += 1
        msg = {"jsonrpc": "2.0", "id": self.n, "method": method}
        if params is not None:
            msg["params"] = params
        self.send_raw(json.dumps(msg).encode("utf-8") + b"\n")
        got = self.recv()
        assert got.get("id") == self.n, got
        return got

    def initialize(self):
        r = self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                        "clientInfo": {"name": "test", "version": "0"}})
        self.send_raw(b'{"jsonrpc": "2.0", "method": "notifications/initialized"}\n')
        return r

    def call(self, name, arguments=None):
        """(payload, isError) of a tools/call."""
        r = self.request("tools/call", {"name": name, "arguments": arguments or {}})
        res = r["result"]
        payload = json.loads(res["content"][0]["text"])
        assert res.get("structuredContent") == payload
        return payload, res["isError"]

    def close(self):
        if self.p.poll() is None:
            try:
                self.p.stdin.close()
                self.p.wait(10)
            except (OSError, subprocess.TimeoutExpired):
                self.p.kill()
                self.p.wait(10)
        for f in (self.p.stdout, self.p.stderr):
            f.close()


class McpCase(StoreCase):
    WHO = "owner"

    def setUp(self):
        super().setUp()
        self.c = Client(self, [sys.executable, LAUNCHER, self.root, self.kinds_path, self.WHO])
        self.assertEqual(self.c.initialize()["result"]["protocolVersion"], "2025-06-18")

    def ok(self, name, arguments=None):
        payload, is_error = self.c.call(name, arguments)
        self.assertFalse(is_error, payload)
        return payload

    def err(self, name, arguments, code, field=None):
        payload, is_error = self.c.call(name, arguments)
        self.assertTrue(is_error, payload)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], code, payload)
        if field is not None:
            self.assertEqual(payload["error"]["field"], field, payload)
        return payload["error"]

    def alive(self):
        self.assertEqual(self.c.request("ping")["result"], {})
        self.assertIsNone(self.c.p.poll())


class Tools(McpCase):
    def test_list_tools(self):
        tools = {t["name"]: t for t in self.c.request("tools/list")["result"]["tools"]}
        self.assertEqual(set(tools), READS | WRITES)
        self.assertEqual(set(srv.OWNER_ONLY), WRITES)
        for name, t in tools.items():
            owner = t["description"].startswith("OWNER-ONLY")
            self.assertEqual(owner, name in WRITES, name)
            self.assertEqual(t["annotations"]["readOnlyHint"], name in READS, name)
            self.assertEqual(t["inputSchema"]["type"], "object")

    def test_read_tools(self):
        s = self.ok("lock_snapshot")
        self.assertEqual(s["schema_version"], api.SCHEMA_VERSION)
        self.assertEqual(len(s["slots"]["cpu"]), 12)
        self.assertEqual(os.path.normcase(s["store"]), os.path.normcase(self.root))
        self.assertIsNone(s["holds_error"])
        self.assertEqual(self.ok("lock_hold", {"id": "h-20261006T120000Z-abcd"}),
                         {"found": False, "hold": None})
        self.assertEqual(self.ok("lock_run", {"id": "r-20261006T120000Z-abcdef"}),
                         {"found": False, "run": None})
        self.assertEqual(self.ok("lock_stats")["rows"], [])
        w = self.ok("lock_wait_change", {"revision": s["revision"], "timeout_s": 0})
        self.assertEqual(w["revision"], s["revision"])
        self.err("lock_snapshot", {"since": "yesterday"}, "invalid", "since")

    def test_a_read_refuses_an_id_that_is_not_one(self):
        # An id is joined into a store path (api.run_record); a path is refused before that.
        self.err("lock_run", {"id": "..\\..\\x"}, "invalid", "id")
        self.err("lock_hold", {"id": "r-20261006T120000Z-abcd"}, "invalid", "id")

    def test_a_write_with_good_input(self):
        res = self.ok("lock_hold_create", {"spec": {"owner": "owner", "scope": "half",
                                                    "for_s": 3600, "note": "mcp"}})
        self.assertTrue(res["ok"])
        self.assertNotIn("snapshot", res)
        hid = res["result"]["hold"]["id"]
        self.assertEqual(res["revision"], api.snapshot(self.store)["revision"])
        # The write reached the store through the api: the in-process api reads it.
        self.assertEqual([h["id"] for h in api.snapshot(self.store)["holds"]], [hid])
        got = self.ok("lock_hold", {"id": hid})
        self.assertTrue(got["found"])
        self.assertTrue(got["hold"]["live_record"])
        self.assertEqual(self.ok("lock_hold_edit", {"id": hid, "changes": {"note": "x"}})[
            "result"]["hold"]["note"], "x")
        self.ok("lock_hold_end", {"id": hid})
        self.assertEqual(api.snapshot(self.store)["holds"], [])
        self.assertTrue(self.ok("lock_set_game_mode", {"enabled": True})["result"]["enabled"])
        self.assertTrue(api.snapshot(self.store)["game"]["enabled"])

    def test_a_write_with_bad_input(self):
        # The api's validation: no end and no for_s.
        self.err("lock_hold_create", {"spec": {"owner": "owner", "scope": "half"}}, "invalid", "end")
        self.err("lock_hold_create", {"spec": {"owner": "owner", "scope": "half",
                                               "for_s": 60, "start": "noon"}}, "invalid", "start")
        # The schema check: wrong type, missing, unknown argument.
        self.err("lock_set_game_mode", {"enabled": "yes"}, "invalid", "enabled")
        self.err("lock_set_game_mode", {}, "invalid", "enabled")
        self.err("lock_hold_end", {"id": "h-20261006T120000Z-abcd", "force": True}, "invalid",
                 "force")
        self.err("lock_hold_end", {"id": "h-20261006T120000Z-abcd"}, "not_found", "id")
        self.err("lock_set_division", {"division": {"no_such_key": 1}}, "invalid", "division")
        self.assertEqual(api.snapshot(self.store)["holds"], [])
        self.assertFalse(api.snapshot(self.store)["game"]["enabled"])
        # An unknown tool is a JSON-RPC error, as MCP has it.
        r = self.c.request("tools/call", {"name": "lock_nope", "arguments": {}})
        self.assertEqual(r["error"]["code"], -32602)
        self.alive()

    def test_an_unreadable_holds_folder_is_a_tool_error_not_a_crash(self):
        # A file where the holds folder belongs: listdir fails (not FileNotFoundError), so
        # kitlib.lock.holds raises HoldsUnreadable after settings.queue_read_s.
        os.makedirs(self.root, exist_ok=True)
        with open(self.store.path("holds"), "w") as f:
            f.write("x")
        s = self.ok("lock_snapshot")
        self.assertEqual(s["holds"], [])
        self.assertIn("holds unreadable", s["holds_error"])
        e = self.err("lock_hold", {"id": "h-20261006T120000Z-abcd"}, "store")
        self.assertIn("holds unreadable", e["message"])
        e = self.err("lock_hold_create", {"spec": {"owner": "owner", "scope": "half",
                                                   "for_s": 60}}, "store")
        self.assertIn("holds unreadable", e["message"])
        self.alive()

    def test_a_line_that_is_not_json_gets_a_parse_error(self):
        self.c.send_raw(b"{nope\n")
        self.assertEqual(self.c.recv()["error"]["code"], -32700)
        self.alive()


class AgentActor(McpCase):
    """The api decides who may (authorize): an agent's owner write is refused as a tool error."""
    WHO = "agent"

    def test_an_agents_owner_write_is_forbidden(self):
        self.err("lock_set_game_mode", {"enabled": True}, "forbidden")
        self.err("lock_hold_create", {"spec": {"owner": "owner", "scope": "half", "for_s": 60}},
                 "forbidden")
        self.assertFalse(api.snapshot(self.store)["game"]["enabled"])
        self.alive()


@unittest.skipUnless(os.name == "nt", "the lock is Windows only")
class EntryPoint(unittest.TestCase):
    """mcp/lock_server.py itself: initialize and tools/list read no store."""

    def test_initialize_and_list(self):
        c = Client(self, [sys.executable, ENTRY])
        r = c.initialize()["result"]
        self.assertEqual(r["serverInfo"]["name"], "machine-lock")
        self.assertEqual(r["capabilities"], {"tools": {"listChanged": False}})
        names = {t["name"] for t in c.request("tools/list")["result"]["tools"]}
        self.assertEqual(names, READS | WRITES)


@unittest.skipUnless(os.name == "nt", "the lock is Windows only")
class IdShape(unittest.TestCase):
    """srv._id takes the whole string as an id (ID_RE.fullmatch): `$` in a re.match also matches
    before a final newline, and an id names a store file."""

    def test_a_good_id_passes(self):
        self.assertEqual(srv._id("r-20261006T120000Z-abcdef", "r"), "r-20261006T120000Z-abcdef")
        self.assertEqual(srv._id("h-20261006T120000Z-abcd", "h"), "h-20261006T120000Z-abcd")

    def test_a_trailing_newline_is_refused(self):
        for v, p in (("r-20261006T120000Z-abcdef\n", "r"), ("h-20261006T120000Z-abcd\n", "h")):
            with self.subTest(v=v):
                with self.assertRaises(srv.ToolError) as cm:
                    srv._id(v, p)
                self.assertEqual(cm.exception.error["field"], "id")


class Manifest(unittest.TestCase):
    def test_the_plugin_starts_this_server(self):
        with open(os.path.join(REPO, ".claude-plugin", "plugin.json"), encoding="utf-8") as f:
            m = json.load(f)
        s = m["mcpServers"]["machine-lock"]
        self.assertEqual(s["args"], ["${CLAUDE_PLUGIN_ROOT}/mcp/lock_server.py"])
        self.assertTrue(os.path.isfile(ENTRY))

    def test_the_kit_names_the_plugin_and_server_as_the_manifest_does(self):
        # kitlib.lockmcp.claude_tool_prefix (the guard's MCP prefix) is built from these two names.
        from kitlib import lockmcp
        with open(os.path.join(REPO, ".claude-plugin", "plugin.json"), encoding="utf-8") as f:
            m = json.load(f)
        self.assertEqual(m["name"], lockmcp.CLAUDE_PLUGIN_NAME)
        self.assertEqual(list(m["mcpServers"]), [lockmcp.SERVER_NAME])
        self.assertEqual(lockmcp.claude_tool_prefix("p", "s"), "mcp__plugin_p_s__")

    def _manifest(self, name):
        with open(os.path.join(REPO, ".claude-plugin", name), encoding="utf-8") as f:
            return json.load(f)

    def test_the_plugin_name_has_no_reserved_claude_prefix(self):
        # `claude plugin validate` reports a name starting with "claude-" as reserved.
        self.assertFalse(self._manifest("plugin.json")["name"].startswith("claude-"))

    def test_the_marketplace_lists_the_plugin_by_its_manifest_name(self):
        self.assertEqual(self._manifest("marketplace.json")["plugins"][0]["name"],
                         self._manifest("plugin.json")["name"])

    def test_the_guard_prefix_is_built_from_the_manifest_name(self):
        from kitlib import lockmcp
        from hosts.claude.adapter.host import ADAPTER
        from kitlib.guard import rules
        want = "mcp__plugin_%s_machine-lock__" % self._manifest("plugin.json")["name"]
        self.assertEqual(lockmcp.claude_tool_prefix(), want)
        self.assertEqual(rules.host_mcp_prefix(ADAPTER.profile), want)


if __name__ == "__main__":
    unittest.main()
