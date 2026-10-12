"""pmstart.hub_site: the hub site's lines of the start checklist, shown only when the machine
config has a `hub` key (docs/pm-start.md "Hub site"). The server is a stand-in client; the
real client is used once, with an injected ask, to pin the timeout and that no token is shown."""
import datetime
import os
import tempfile
import unittest

from kitlib import pmstart
from kitlib.hubsite import client as hubclient
from kitlib.hubsite import auth, serve, settings
from tests.pmstart import keep_off_launcher_repos

NOW = datetime.datetime(2026, 10, 6, 9, 0)
FAKES = dict(handoff={"globber": lambda p: []}, reminders={"read": lambda p: "[]"},
             liveness={"read": lambda p: "{}"})
SET = settings.settings({"port": 4321})
TOKEN = "SECRET-TOKEN-VALUE"


def setUpModule():
    keep_off_launcher_repos()


class FakeClient(object):
    def __init__(self, health=None, pending=(), error=None):
        self.health, self.items, self.error, self.calls = health, list(pending), error, []

    def get(self, path, role=hubclient.ANY, query=None, timeout=None):
        self.calls.append((path, timeout))
        if self.error:
            raise self.error
        if path == "/health":
            return {"ok": True} if self.health is None else self.health
        return {"ok": True, "items": self.items}


def site(client, load=lambda: SET, procs=()):
    return pmstart.hub_site(procs=list(procs), load=load, make_client=lambda port: client)


class HubSiteTest(unittest.TestCase):

    def test_no_hub_key_gives_none_and_asks_nothing(self):
        c = FakeClient()
        self.assertIsNone(site(c, load=lambda: None))
        self.assertEqual(c.calls, [])

    def test_up_with_nothing_pending_says_port_and_count_and_the_wait_line(self):
        out = site(FakeClient())
        self.assertEqual(out[0], "hub: server up on port 4321, 0 pending")
        self.assertEqual(len(out), 2)
        self.assertIn("wait --pending", out[1])
        self.assertIn("in the background", out[1])

    def test_pending_items_are_counted_and_to_be_processed(self):
        out = site(FakeClient(pending=[{"id": "a"}, {"id": "b"}]))
        self.assertEqual(out[0], "hub: server up on port 4321, 2 pending")
        self.assertIn("hub.py pending", out[1])
        self.assertIn("wait --pending", out[2])

    def test_a_running_wait_replaces_the_start_line(self):
        procs = [{"Name": "python.exe", "CommandLine": 'python "x/tools/hub.py" wait --pending'}]
        out = site(FakeClient(), procs=procs)
        self.assertEqual(out[1], "hub: wait --pending is running")

    def test_a_wait_without_pending_does_not_count(self):
        procs = [{"Name": "python.exe", "CommandLine": 'python "x/tools/hub.py" wait --timeout 5'}]
        self.assertIn("in the background", site(FakeClient(), procs=procs)[1])

    def test_nothing_listening_is_hub_server_down(self):
        out = site(FakeClient(error=serve.NotRunning(4321)))
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].startswith("hub: hub server down (nothing listens on port 4321)"), out[0])
        self.assertIn("hub.py serve", out[0])

    def test_any_other_failure_is_one_line_naming_the_class_only(self):
        for err in (serve.ServeError("secret detail"), hubclient.Refused(403, {}), OSError("x"),
                    ValueError("y")):
            out = site(FakeClient(error=err))
            self.assertEqual(out, ["hub: server did not answer as the hub does (%s)" % type(err).__name__])

    def test_a_health_answer_that_is_not_ok_is_not_up(self):
        out = site(FakeClient(health={"ok": False}))
        self.assertEqual(out, ["hub: server did not answer as the hub does (health not ok)"])

    def test_a_pending_ask_that_fails_after_health_is_one_line_too(self):
        class Half(FakeClient):
            def get(self, path, *a, **k):
                if path == "/pending":
                    raise serve.ServeError("late")
                return FakeClient.get(self, path, *a, **k)
        out = site(Half())
        self.assertEqual(out, ["hub: server up on port 4321, pending not asked (ServeError)"])

    def test_a_broken_hub_key_is_one_line(self):
        def bad():
            raise pmstart.HubSettingsError("x")
        out = site(FakeClient(), load=bad)
        self.assertEqual(out, ["hub: the hub key of the machine config cannot be used (HubSettingsError)"])

    def test_each_ask_has_a_short_timeout(self):
        c = FakeClient()
        site(c)
        self.assertEqual([t for _, t in c.calls], [pmstart.HUB_SITE_TIMEOUT_S] * 2)
        self.assertLessEqual(pmstart.HUB_SITE_TIMEOUT_S, 2)

    def test_the_real_client_gets_the_timeout_and_no_token_reaches_a_line(self):
        asked = []

        def ask(port, method, path, token, body, timeout):
            asked.append((port, method, path, token, timeout))
            return 200, {"ok": True, "items": []}
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, auth.AGENT_FILE), "w") as f:
                f.write(TOKEN)
            made = pmstart.hub_site(
                procs=[], load=lambda: SET,
                make_client=lambda port: hubclient.Client(
                    port, tokens=d, timeout=pmstart.HUB_SITE_TIMEOUT_S, ask=ask))
        self.assertTrue(asked)
        self.assertTrue(all(a[3] == TOKEN for a in asked))      # the token was sent, never shown
        self.assertTrue(all(a[4] == pmstart.HUB_SITE_TIMEOUT_S and a[0] == 4321 for a in asked))
        self.assertNotIn(TOKEN, "\n".join(made))
        self.assertNotIn("@", "\n".join(made))


class ChecklistTest(unittest.TestCase):

    def test_with_a_hub_key_the_site_lines_replace_the_old_hub_line(self):
        txt = pmstart.checklist({}, NOW, procs=[], hub_site={"load": lambda: SET,
                                "make_client": lambda port: FakeClient(error=serve.NotRunning(port))},
                                **FAKES)
        self.assertIn("- hub: hub server down (nothing listens on port 4321)", txt)
        self.assertNotIn("no Action Hub copy is recorded", txt)
        self.assertNotIn("ArtifactComments watch", txt)

    def test_without_a_hub_key_the_old_hub_line_is_unchanged_byte_for_byte(self):
        cfg = {"hub_url": "https://claude.ai/artifact/X"}
        old = pmstart.hub(cfg, account=lambda: "1", isfile=lambda p: True)
        txt = pmstart.checklist(cfg, NOW, procs=[], hub_site={"load": lambda: None},
                                hub={"account": lambda: "1", "isfile": lambda p: True}, **FAKES)
        self.assertIn("\n".join("- " + l for l in old), txt)
        self.assertEqual(old[0], "hub: republish the kit's Action Hub page %s to %s with the Artifact tool, "
                                 "restating the capabilities in docs/action-hub.md (For the PM), to arm the alert"
                         % (os.path.join(pmstart.KIT_ROOT, "claude", "hub", "action-hub.html"),
                            "https://claude.ai/artifact/X"))

    def test_a_loader_that_cannot_read_the_config_keeps_the_old_line(self):
        def boom():
            raise OSError("no config")
        cfg = {"hub_url": "https://claude.ai/artifact/X"}
        txt = pmstart.checklist(cfg, NOW, procs=[], hub_site={"load": boom},
                                hub={"account": lambda: "1", "isfile": lambda p: True}, **FAKES)
        self.assertIn("hub: republish the kit's Action Hub page", txt)
        self.assertNotIn("could not check", txt)


if __name__ == "__main__":
    unittest.main()
