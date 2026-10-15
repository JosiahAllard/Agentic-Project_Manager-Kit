"""pmstart.hub_site: the hub site's lines of the start checklist, on the machine config's `hub`
key or, with no key, on the default settings (docs/pm-start.md "Hub site"). The server is a
stand-in client; the real client is used once, with an injected ask, to pin the timeout and that
no token is shown."""
import datetime
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from kitlib import pmstart
from kitlib.config import user as kuser
from kitlib.config.types import ConfigError
from kitlib.pmstart import fglock
from kitlib.hubsite import client as hubclient
from kitlib.hubsite import auth, serve, settings
from tests.pmstart import keep_off_launcher_repos

NOW = datetime.datetime(2026, 10, 6, 9, 0)
FAKES = dict(handoff={"globber": lambda p: []}, reminders={"read": lambda p: "[]"},
             liveness={"read": lambda p: "{}"})
SET = settings.settings({"port": 4321})
TOKEN = "secret_token_value_" + "x" * auth.MIN_TOKEN


def setUpModule():
    keep_off_launcher_repos()
    # the checklist must not read this machine's pm_data setting, foreground lock or heartbeat
    for owner, name, value in ((pmstart, "pm_data_settings", lambda: None),
                               (fglock, "read_live", lambda user32=None: 0),
                               (pmstart, "read_heartbeat", lambda: None)):
        p = mock.patch.object(owner, name, value)
        p.start()
        unittest.addModuleCleanup(p.stop)


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

    def test_a_config_that_cannot_be_read_is_one_line_naming_the_class_and_asks_nothing(self):
        c = FakeClient()
        for err in (OSError("made-up\\folder"), ConfigError("no_such_key, not 'x@mail.invalid'")):
            def bad():
                raise err
            out = site(c, load=bad)
            self.assertEqual(out, ["hub: the machine config could not be read (%s)"
                                   % type(err).__name__])
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
        self.assertIn('hub.py" pending', out[1])
        self.assertIn("wait --pending", out[2])

    def test_a_running_wait_replaces_the_start_line(self):
        procs = [{"Name": "python.exe", "CommandLine": 'python "x/tools/hub.py" wait --pending'}]
        out = site(FakeClient(), procs=procs)
        self.assertEqual(out[1], "hub: wait --pending is running")

    def test_only_the_pms_wait_counts_not_another_sessions_for_its_key(self):
        def line(cmd):
            return site(FakeClient(), procs=[{"Name": "python.exe", "CommandLine": cmd}])[1]
        hub = 'python "x/tools/hub.py" wait --pending'
        for cmd in (hub + ' --for kit', hub + ' --for=kit', hub + ' --for "kit"',
                    'python "x/tools/hub.py" wait --for kit --pending'):
            with self.subTest(cmd=cmd):
                self.assertIn("in the background", line(cmd))
        for cmd in (hub + ' --for pm', hub + ' --for=pm', hub + ' --for "pm"',
                    hub + ' --timeout 30'):
            with self.subTest(cmd=cmd):
                self.assertEqual(line(cmd), "hub: wait --pending is running")

    def test_a_wait_without_pending_does_not_count(self):
        procs = [{"Name": "python.exe", "CommandLine": 'python "x/tools/hub.py" wait --timeout 5'}]
        self.assertIn("in the background", site(FakeClient(), procs=procs)[1])

    def test_nothing_listening_is_hub_server_down(self):
        out = site(FakeClient(error=serve.NotRunning(4321)))
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].startswith("hub: hub server down (nothing listens on port 4321)"), out[0])
        self.assertTrue(out[0].endswith('hub.py" serve'), out[0])

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

    def test_a_broken_hub_key_is_one_line_with_the_configuration_errors_own_text(self):
        def bad():
            raise pmstart.HubSettingsError("hub.port must be a whole number\nbetween 1 and 65535")
        out = site(FakeClient(), load=bad)
        self.assertEqual(out, ["hub: the hub key of the machine config cannot be used: "
                               "hub.port must be a whole number between 1 and 65535"])

    def test_a_broken_hub_key_line_names_the_key_and_the_rule_and_no_rejected_value(self):
        team = "hub.access.team must be an Access team name (one DNS label)"
        aud = "hub.access.aud must be an audience tag without spaces"
        store = "hub.store must be an absolute path"
        head = "hub: the hub key of the machine config cannot be used: "
        for said, line in (
                # an email-like and a tag-like value, as kitlib.config echoes them
                ("%s, not 'someone@mail.invalid'; %s, not '0f1e2d3c 4b5a6978'" % (team, aud),
                 "%s; %s" % (team, aud)),
                ("%s, not 'made-up\\\\folder'" % store, store),
                # a value written with the other quote, with both, and with an escaped backslash
                ("%s, not \"it's@mail.invalid\"; %s, not 'a\\'b\"c@mail.invalid'; hub.port must "
                 "be a whole number" % (team, aud),
                 "%s; %s; hub.port must be a whole number" % (team, aud)),
                ("%s, not 'ends in a backslash@mail.invalid\\\\'; %s, not 'tag one'" % (team, aud),
                 "%s; %s" % (team, aud)),
                # a value that holds the words a problem is joined and ended with
                ("%s, not 'x; %s, not y@mail.invalid'; %s, not 'tag one'" % (team, aud, aud),
                 "%s; %s" % (team, aud)),
                # a value over several lines, or with a run of spaces: gone before the line is
                # joined, so no half of it is left
                ("%s, not 'one@mail.invalid\n  two@mail.invalid'; %s, not 'tag   one'"
                 % (team, aud), "%s; %s" % (team, aud)),
                # a value that does not end as a written string does: cut, with what follows
                ("%s, not 'someone@mail.invalid; %s" % (team, aud), team),
                ("%s, not \"someone@mail.invalid" % team, team),
                # a value that is not a string, as kitlib.pmdata echoes it: a number goes alone,
                # a list is cut with what follows
                ("pm_data: remote must be a branch or remote name, not 7; %s, not 'tag one'" % aud,
                 "pm_data: remote must be a branch or remote name; %s" % aud),
                ("pm_data: path must be an absolute path, not ['made-up@mail.invalid']; %s" % aud,
                 "pm_data: path must be an absolute path")):
            with self.subTest(said=said):
                def bad():
                    raise pmstart.HubSettingsError(said)
                out = site(FakeClient(), load=bad)
                self.assertEqual(out, [head + line])
                for word in ("mail.invalid", "@", "0f1e2d3c", "made-up", "tag one", "'", '"'):
                    self.assertNotIn(word, out[0])

    def test_a_long_rejected_value_is_taken_out_before_the_line_is_cut(self):
        # cut first, the value would lose its end and stay in the line
        value = "someone@mail.invalid" + "x" * (2 * pmstart.HUB_ERROR_MAX)
        said = "hub.access.team must be an Access team name (one DNS label), not %r" % value

        def bad():
            raise pmstart.HubSettingsError("%s; %s" % (said, said))
        out = site(FakeClient(), load=bad)
        rule = "hub.access.team must be an Access team name (one DNS label)"
        self.assertEqual(out, ["hub: the hub key of the machine config cannot be used: %s; %s"
                               % (rule, rule)])

    def test_a_very_long_configuration_error_is_cut(self):
        def bad():
            raise pmstart.HubSettingsError("k" * 5000)
        out = site(FakeClient(), load=bad)
        self.assertEqual(len(out), 1)
        self.assertLess(len(out[0]), pmstart.HUB_ERROR_MAX + 100)

    def test_the_home_fence_is_not_swallowed_by_the_loader_catch(self):
        def fenced():
            raise kuser.HomeFenced("fence")
        with self.assertRaises(kuser.HomeFenced):
            site(FakeClient(), load=fenced)

    def test_no_token_file_is_its_own_line_naming_setup(self):
        def no_token(port):
            raise pmstart.NoHubToken()
        out = pmstart.hub_site(procs=[], load=lambda: SET, make_client=no_token)
        self.assertEqual(out, ['hub: no hub token file is there to read: run python "%s" setup'
                               % pmstart.HUB_TOOL])

    def test_the_real_client_maker_gives_no_token_when_no_file_is_there(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(auth, "folder", lambda: d):
            with self.assertRaises(pmstart.NoHubToken):
                pmstart._hub_client(4321)
            out = pmstart.hub_site(procs=[], load=lambda: SET, make_client=pmstart._hub_client)
            self.assertEqual(out, ['hub: no hub token file is there to read: run python "%s" setup'
                                   % pmstart.HUB_TOOL])
            with open(os.path.join(d, auth.AGENT_FILE), "w") as f:
                f.write(TOKEN)
            self.assertEqual(pmstart._hub_client(4321).port, 4321)

    def test_a_pending_answer_whose_items_are_not_a_list_is_zero_pending(self):
        for bad in ({"a": 1}, "abc", 7, None):
            class Odd(FakeClient):
                def get(self, path, *a, **k):
                    if path == "/pending":
                        return {"ok": True, "items": bad}
                    return FakeClient.get(self, path, *a, **k)
            out = site(Odd())
            self.assertEqual(out[0], "hub: server up on port 4321, 0 pending", repr(bad))
            self.assertEqual(len(out), 2, repr(bad))

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


class RealSettingsTest(unittest.TestCase):
    """The real hub_settings over a machine config in a temp home (the real home is fenced)."""

    def setUp(self):
        d = tempfile.mkdtemp(prefix="kit-pmstart-home-")
        self.addCleanup(shutil.rmtree, d, True)
        self.home = d
        p = mock.patch.dict(os.environ, {"USERPROFILE": d, "HOME": d})
        p.start()
        self.addCleanup(p.stop)

    def write(self, body):
        os.makedirs(os.path.dirname(kuser.config_file(self.home)), exist_ok=True)
        with open(kuser.config_file(self.home), "w", encoding="utf-8") as f:
            f.write(json.dumps(body))

    def test_no_hub_key_gives_the_default_settings_and_the_site_lines(self):
        self.write({})
        self.assertEqual(pmstart.hub_settings().port, settings.PORT)
        self.assertEqual(settings.PORT, 8790)
        out = pmstart.hub_site(procs=[], make_client=lambda port: FakeClient(error=serve.NotRunning(port)))
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].startswith("hub: hub server down (nothing listens on port 8790)"), out)

    def test_no_machine_config_at_all_gives_the_default_settings(self):
        self.assertFalse(os.path.exists(kuser.config_file(self.home)))
        self.assertEqual(pmstart.hub_settings().port, settings.PORT)

    def test_a_valid_hub_key_gives_the_settings(self):
        self.write({"hub": {"port": 4322}})
        self.assertEqual(pmstart.hub_settings().port, 4322)

    def test_a_hub_key_the_config_check_refuses_is_a_hub_settings_error(self):
        self.write({"hub": ["not", "an", "object"]})
        with self.assertRaises(pmstart.HubSettingsError) as cm:
            pmstart.hub_settings()
        self.assertIn("hub must be an object", str(cm.exception))
        out = pmstart.hub_site(procs=[])
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].startswith("hub: the hub key of the machine config cannot be used: "), out)
        self.assertIn("hub must be an object", out[0])

    def test_a_hub_key_the_settings_refuse_is_a_hub_settings_error(self):
        self.write({"hub": {"port": 4322, "tunnel_port": 4322}})
        with self.assertRaises(pmstart.HubSettingsError):
            pmstart.hub_settings()

    def test_a_pm_data_value_that_is_not_a_string_is_named_and_never_shown(self):
        from kitlib import pmdata
        down = dict(make_client=lambda port: FakeClient(error=serve.NotRunning(port)))
        for pm, rule in (({"path": ["SECRETPATH"]}, "path must be an absolute path"),
                         ({"path": "C:\\pm", "branch": ["SECRETBR"]},
                          "branch must be a branch or remote name"),
                         ({"path": "C:\\pm", "remote": 7654321}, "remote must be a branch or remote name")):
            for hub in (None, {"port": 4322}):
                with self.subTest(pm=pm, hub=hub):
                    body = {"pm_data": pm}
                    if hub:
                        body["hub"] = hub
                    self.write(body)
                    txt = pmstart.checklist({}, NOW, procs=[], pm_data=pmdata.machine_settings,
                                            hub_site=down, **FAKES)
                    kit = [l for l in txt.splitlines() if "kit_sync" in l]
                    self.assertEqual(len(kit), 1, txt)
                    self.assertIn("could not check kit_sync: PmDataError: ", kit[0])
                    self.assertIn(rule, kit[0])
                    if hub:
                        self.assertIn("hub: the hub key of the machine config cannot be used: ", txt)
                    for word in ("SECRET", "7654321", ", not "):
                        self.assertNotIn(word, txt)

    def test_a_broken_config_with_no_hub_key_is_one_line_naming_the_class(self):
        self.write({"no_such_key": 1})
        with self.assertRaises(ConfigError) as cm:
            pmstart.hub_settings()
        self.assertNotIsInstance(cm.exception, pmstart.HubSettingsError)
        self.assertEqual(pmstart.hub_site(procs=[]),
                         ["hub: the machine config could not be read (ConfigError)"])

    def test_without_a_hub_key_the_checklist_shows_the_site_lines_on_port_8790(self):
        self.write({})
        txt = pmstart.checklist({}, NOW, procs=[], hub_site={
            "make_client": lambda port: FakeClient(error=serve.NotRunning(port))}, **FAKES)
        self.assertIn("- hub: hub server down (nothing listens on port 8790)", txt)
        self.assertNotIn("could not check hub", txt)
        for old in ("republish", "Action Hub", "Artifact", "claude.ai"):
            self.assertNotIn(old, txt)


class ChecklistTest(unittest.TestCase):

    def test_with_a_hub_key_the_checklist_shows_the_site_lines_on_its_port(self):
        txt = pmstart.checklist({}, NOW, procs=[], hub_site={"load": lambda: SET,
                                "make_client": lambda port: FakeClient(error=serve.NotRunning(port))},
                                **FAKES)
        self.assertIn("- hub: hub server down (nothing listens on port 4321)", txt)
        self.assertNotIn("Action Hub", txt)
        self.assertNotIn("ArtifactComments watch", txt)

    def test_a_loader_that_cannot_read_the_config_is_one_hub_line_not_a_failed_check(self):
        def boom():
            raise OSError("no config")
        txt = pmstart.checklist({}, NOW, procs=[], hub_site={"load": boom}, **FAKES)
        self.assertIn("- hub: the machine config could not be read (OSError)", txt)
        self.assertNotIn("could not check hub", txt)
        self.assertNotIn("no config", txt)


if __name__ == "__main__":
    unittest.main()
