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

from kitlib import pmdata, pmstart
from kitlib.config import user as kuser
from kitlib.config.types import ConfigError
from kitlib.pmstart import fglock
from kitlib.hubsite import client as hubclient
from kitlib.hubsite import auth, serve, settings
from tests.home import machine_config, pm_repo
from tests.pmstart import HANDOFF_FAKE, keep_off_launcher_repos, keep_off_session_job

NOW = datetime.datetime(2026, 10, 6, 9, 0)
FAKES = dict(handoff=HANDOFF_FAKE, reminders={"read": lambda p: "[]"},
             liveness={"read": lambda p: "{}"})
SET = settings.settings({"port": 4321})
TOKEN = "secret_token_value_" + "x" * auth.MIN_TOKEN


def setUpModule():
    keep_off_launcher_repos()
    keep_off_session_job()      # the watcher check asks no hub (the hub check is the one tested)
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


def site(client, load=lambda: SET):
    return pmstart.hub_site(load=load, make_client=lambda port: client)


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

    def test_a_hub_that_says_its_code_is_old_gets_a_warn_line_with_the_owners_restart_step(self):
        from kitlib.hubsite import codestale
        out = site(FakeClient(health={"ok": True, "code": {"stale": True, "restart": "x"}}))
        self.assertEqual(out[0], "hub: server up on port 4321, 0 pending for the PM")
        self.assertEqual(out[1], "hub: WARN " + codestale.text(pmstart.HUB_TOOL))
        self.assertIn('python "%s" restart' % pmstart.HUB_TOOL, out[1])
        self.assertEqual(len(out), 3)

    def test_a_current_hub_or_one_with_no_or_a_malformed_code_field_gets_no_warn_line(self):
        for code in ({"stale": False, "restart": None}, None, "stale", {"stale": "yes"}, []):
            out = site(FakeClient(health={"ok": True, "code": code}))
            self.assertEqual(len(out), 2, code)
            self.assertFalse(any("older code" in line for line in out), code)
        self.assertEqual(len(site(FakeClient())), 2)

    def test_up_with_nothing_pending_says_port_and_count_and_the_push_line(self):
        out = site(FakeClient())
        self.assertEqual(out[0], "hub: server up on port 4321, 0 pending for the PM")
        self.assertEqual(len(out), 2)
        self.assertTrue(out[1].startswith("hub: WARN push notifications are off"), out[1])

    def test_pending_items_are_counted_and_to_be_processed(self):
        out = site(FakeClient(pending=[{"id": "a"}, {"id": "b"}]))
        self.assertEqual(out[0], "hub: server up on port 4321, 2 pending for the PM")
        self.assertIn('hub.py" pending', out[1])
        self.assertIn("push", out[2])

    def test_only_the_pms_items_are_counted_not_one_routed_to_a_reachable_creator(self):
        class Routed(FakeClient):
            def get(self, path, role=hubclient.ANY, query=None, timeout=None):
                if path != "/pending":
                    return FakeClient.get(self, path, role, query, timeout)
                self.calls.append((path, timeout))
                self.asked = query
                pm = [{"id": "q1", "route": "pm"}]
                return {"ok": True, "items": pm if (query or {}).get("for") == "pm"
                        else pm + [{"id": "t1", "route": "kit"}]}
        c = Routed()
        out = site(c)
        self.assertEqual(c.asked, {"for": "pm"})
        self.assertEqual(out[0], "hub: server up on port 4321, 1 pending for the PM")
        self.assertIn('process the 1 pending item(s): python "%s" pending --for pm, then handle'
                      % pmstart.HUB_TOOL, out[1])

    def test_push_up_with_a_live_watcher_is_one_plain_line_and_names_no_wait(self):
        health = {"ok": True, "push": {"state": "up", "last": "written", "undelivered": 0},
                  "watcher": {"state": "up", "age": 42}}
        out = site(FakeClient(health=health))
        self.assertEqual(out[1], "hub: push up (last delivery to the PM: written, 0 undelivered); "
                                 "watcher up (beat 42s ago)")
        self.assertFalse(any("wait" in line.replace(pmstart.HUB_TOOL, "") for line in out), out)

    def test_push_up_before_any_delivery_and_with_no_heartbeat_yet(self):
        health = {"ok": True, "push": {"state": "up", "last": None, "undelivered": 3},
                  "watcher": {"state": "none", "age": None}}
        self.assertEqual(site(FakeClient(health=health))[1],
                         "hub: push up (last delivery to the PM: none yet, 3 undelivered); "
                         "watcher none")

    def test_push_down_is_a_warn_naming_the_pending_read(self):
        health = {"ok": True, "push": {"state": "down", "last": "not-written", "undelivered": 2},
                  "watcher": {"state": "down", "age": 900}}
        line = site(FakeClient(health=health))[1]
        self.assertTrue(line.startswith("hub: WARN push down (last delivery to the PM: not-written, "
                                        "2 undelivered); watcher down (beat 900s ago)"), line)
        self.assertIn('read python "%s" pending --for pm' % pmstart.HUB_TOOL, line)
        self.assertNotIn("wait", line.replace(pmstart.HUB_TOOL, ""))

    def test_no_push_worker_is_a_warn_that_nothing_arrives_by_itself(self):
        for health in ({"ok": True}, {"ok": True, "push": None, "watcher": None},
                       {"ok": True, "push": "up", "watcher": "up"}):
            with self.subTest(health=health):
                line = site(FakeClient(health=health))[1]
                self.assertTrue(line.startswith("hub: WARN push notifications are off"), line)
                self.assertIn("watcher not read (no push worker)", line)
                self.assertIn('pending --for pm', line)

    def test_server_values_outside_the_allow_list_never_reach_the_line(self):
        odd = "x@mail.invalid secret"
        health = {"ok": True, "push": {"state": odd, "last": odd, "undelivered": odd},
                  "watcher": {"state": odd, "age": odd}}
        line = site(FakeClient(health=health))[1]
        self.assertNotIn(odd, line)
        self.assertTrue(line.startswith("hub: WARN push ? (last delivery to the PM: ?, ? undelivered); "
                                        "watcher ? (beat ?s ago)"), line)
        for bad in (True, -1, 1.5):
            health = {"ok": True, "push": {"state": "up", "last": "written", "undelivered": bad},
                      "watcher": {"state": "up", "age": bad}}
            with self.subTest(bad=bad):
                self.assertEqual(site(FakeClient(health=health))[1],
                                 "hub: push up (last delivery to the PM: written, ? undelivered); "
                                 "watcher up (beat ?s ago)")

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

    def test_no_pm_repo_pointer_names_the_pointer_not_a_hub_that_did_not_answer(self):
        with mock.patch.object(auth, "folder", side_effect=ConfigError(auth.NO_POINTER)):
            out = pmstart.hub_site(load=lambda: SET)
        self.assertEqual(len(out), 1)
        self.assertIn("no PM repo pointer is set", out[0])
        self.assertIn("--pm-repo", out[0])
        self.assertNotIn("did not answer", out[0])

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
        out = pmstart.hub_site(load=lambda: SET, make_client=no_token)
        self.assertEqual(out, ['hub: no hub token file is there to read: run python "%s" setup'
                               % pmstart.HUB_TOOL])

    def test_the_real_client_maker_gives_no_token_when_no_file_is_there(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(auth, "folder", lambda: d):
            with self.assertRaises(pmstart.NoHubToken):
                pmstart._hub_client(4321)
            out = pmstart.hub_site(load=lambda: SET, make_client=pmstart._hub_client)
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
            self.assertEqual(out[0], "hub: server up on port 4321, 0 pending for the PM", repr(bad))
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
                load=lambda: SET,
                make_client=lambda port: hubclient.Client(
                    port, tokens=d, timeout=pmstart.HUB_SITE_TIMEOUT_S, ask=ask))
        self.assertTrue(asked)
        self.assertIn("/api/v1/pending?for=pm", [a[2] for a in asked])   # the PM's items only
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
        self.pm = pm_repo(self)          # the fenced pointer names it: the machine config's place

    def write(self, body):
        """The machine config `body`, split between the fenced PM repository's two files."""
        return machine_config(self.home, body, case=self)

    def test_no_hub_key_gives_the_default_settings_and_the_site_lines(self):
        self.write({})
        self.assertEqual(pmstart.hub_settings().port, settings.PORT)
        self.assertEqual(settings.PORT, 8790)
        out = pmstart.hub_site(make_client=lambda port: FakeClient(error=serve.NotRunning(port)))
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].startswith("hub: hub server down (nothing listens on port 8790)"), out)

    def test_no_machine_config_at_all_gives_the_default_settings(self):
        self.assertFalse(os.path.exists(os.path.join(self.pm, kuser.DIR_NAME)))
        self.assertEqual(pmstart.hub_settings().port, settings.PORT)

    def test_machine_config_reads_local_json_too_and_its_keys_win(self):
        # hub and launcher live in the PM repository's local.json (kitlib.config.user.LOCAL_KEYS)
        folder = self.write({"pm_start": {}})
        cfg_file, local_file = (os.path.join(folder, n) for n in (kuser.FILE_NAME, kuser.LOCAL_FILE_NAME))
        with open(cfg_file, "w", encoding="utf-8") as f:
            json.dump({"pm_start": {}, "hub": {"port": 1111}}, f)
        with open(local_file, "w", encoding="utf-8") as f:
            json.dump({"hub": {"port": 4322}, "launcher": {"accounts": {}}}, f)
        self.assertEqual(pmstart.machine_config(),
                         {"pm_start": {}, "hub": {"port": 4322}, "launcher": {"accounts": {}}})
        self.assertEqual(pmstart.machine_config(cfg_file), {"pm_start": {}, "hub": {"port": 1111}})
        with open(local_file, "w", encoding="utf-8") as f:
            f.write("[not an object")                   # unreadable: config.json's keys stand
        self.assertEqual(pmstart.machine_config(), {"pm_start": {}, "hub": {"port": 1111}})

    def test_a_valid_hub_key_gives_the_settings(self):
        self.write({"hub": {"port": 4322}})
        self.assertEqual(pmstart.hub_settings().port, 4322)

    def test_a_hub_key_the_config_check_refuses_is_a_hub_settings_error(self):
        self.write({"hub": ["not", "an", "object"]})
        with self.assertRaises(pmstart.HubSettingsError) as cm:
            pmstart.hub_settings()
        self.assertIn("hub must be an object", str(cm.exception))
        out = pmstart.hub_site()
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].startswith("hub: the hub key of the machine config cannot be used: "), out)
        self.assertIn("hub must be an object", out[0])

    def test_a_hub_key_the_settings_refuse_is_a_hub_settings_error(self):
        self.write({"hub": {"port": 4322, "tunnel_port": 4322}})
        with self.assertRaises(pmstart.HubSettingsError):
            pmstart.hub_settings()

    def test_a_pm_data_key_in_the_machine_config_is_named_and_its_value_never_shown(self):
        # pm_data is derived from the PM-repo pointer (kitlib.config.user.locate): a pm_data key
        # left in config.json makes the machine config unreadable, and no line shows its value
        down = dict(make_client=lambda port: FakeClient(error=serve.NotRunning(port)))
        for hub in (None, {"port": 4322}):
            with self.subTest(hub=hub):
                folder = self.write({"hub": hub} if hub else {})
                with open(os.path.join(folder, kuser.FILE_NAME), "w", encoding="utf-8") as f:
                    json.dump({"pm_data": {"path": ["SECRETPATH"], "branch": ["SECRETBR"],
                                           "remote": 7654321}}, f)
                txt = pmstart.checklist({}, NOW, procs=[], pm_data=lambda: None, hub_site=down, **FAKES)
                hub_lines = [l for l in txt.splitlines() if l.startswith("- hub: ")]
                self.assertEqual(len(hub_lines), 1, txt)
                if hub:
                    self.assertTrue(hub_lines[0].startswith(
                        "- hub: the hub key of the machine config cannot be used: "), txt)
                    self.assertIn("pm_data is derived", hub_lines[0])
                else:
                    self.assertEqual(hub_lines[0], "- hub: the machine config could not be read (ConfigError)")
                for word in ("SECRET", "7654321", ", not "):
                    self.assertNotIn(word, txt)

    def test_a_bad_pm_data_value_fails_the_kit_sync_check_naming_the_rule_and_never_the_value(self):
        # the kit_sync check reads pm_data through kitlib.pmdata's own check (check_settings), whose
        # PmDataError echoes the rejected value: the "could not check" line takes it out
        # (pmstart._failure_text)
        down = dict(make_client=lambda port: FakeClient(error=serve.NotRunning(port)))
        for bad, rule in (({"path": "C:\\SECRETPATH", "branch": "SECRET BR", "remote": 7654321},
                           "pm_data: branch must be a branch or remote name; "
                           "remote must be a branch or remote name"),
                          ({"path": ["SECRETPATH"]}, "pm_data: path must be an absolute path")):
            with self.subTest(bad=bad):
                def settings():
                    return pmdata.check_settings(bad, "pm_data")
                with self.assertRaises(pmdata.PmDataError) as cm:
                    settings()
                self.assertIn("SECRET", str(cm.exception))    # the value the line must not show
                txt = pmstart.checklist({}, NOW, procs=[], pm_data=settings, hub_site=down, **FAKES)
                self.assertIn("- could not check kit_sync: PmDataError: %s\n" % rule, txt + "\n")
                for word in ("SECRET", "7654321", ", not "):
                    self.assertNotIn(word, txt)

    def test_a_broken_config_with_no_hub_key_is_one_line_naming_the_class(self):
        self.write({"no_such_key": 1})
        with self.assertRaises(ConfigError) as cm:
            pmstart.hub_settings()
        self.assertNotIsInstance(cm.exception, pmstart.HubSettingsError)
        self.assertEqual(pmstart.hub_site(),
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


UTC = datetime.timezone.utc
AT = datetime.datetime(2026, 10, 6, 9, 0, tzinfo=UTC)


class FiledClient(FakeClient):
    """FakeClient whose GET /questions answers `filed` (the questions the hub holds filed) for
    status filed and `done` (the done ones) for status done, and keeps each query it was asked."""

    def __init__(self, filed=(), done=(), **kw):
        FakeClient.__init__(self, **kw)
        self.filed, self.done, self.queries = list(filed), list(done), []

    def get(self, path, role=hubclient.ANY, query=None, timeout=None):
        if path != "/questions":
            return FakeClient.get(self, path, role, query, timeout)
        self.calls.append((path, timeout))
        self.queries.append(query)
        if self.error:
            raise self.error
        return {"ok": True, "items": self.done if (query or {}).get("status") == "done"
                else self.filed}


def filed(*ats):
    return [{"id": "Q-%d" % (1000 + n), "status": "filed",
             "filed_at": at.isoformat() if isinstance(at, datetime.datetime) else at}
            for n, at in enumerate(ats)]


def done(by, *ats):
    """Done questions filed at `ats`, marked done by `by` (`hub`: the push worker; None: their
    asker or the PM)."""
    return [dict(q, id="Q-%d" % (2000 + n), status="done", done_by=by)
            for n, q in enumerate(filed(*ats))]


class FiledDigestTest(unittest.TestCase):
    """pmstart.filed_digest: the start checklist's one line of the owner's answers filed into
    records since the last PM start (docs/pm-start.md "Filed answers")."""

    def setUp(self):
        d = tempfile.mkdtemp(prefix="kit-filed-")
        self.addCleanup(shutil.rmtree, d, True)
        self.stamp = os.path.join(d, "pm_watch", pmstart.FILED_STAMP_NAME)

    def write(self, text):
        os.makedirs(os.path.dirname(self.stamp), exist_ok=True)
        with open(self.stamp, "w", encoding="utf-8") as f:
            f.write(text)

    def held(self):
        with open(self.stamp, encoding="utf-8") as f:
            return json.load(f)

    def line(self, count, since):
        text = since.isoformat(timespec="seconds")
        return ('hub: %d answer(s) filed into records since %s, for your information: python '
                '"%s" questions --status filed --since %s' % (count, text, pmstart.HUB_TOOL, text))

    def test_a_first_start_counts_the_last_24_hours_and_writes_the_stamp(self):
        hour = datetime.timedelta(hours=1)
        c = FiledClient(filed(AT - hour, AT - 25 * hour))
        since = AT - pmstart.FILED_FALLBACK
        self.assertEqual(pmstart.filed_digest(c, self.stamp, AT), [self.line(1, since)])
        text = since.isoformat(timespec="seconds")
        self.assertEqual(c.queries,
                         [{"status": "filed", "since": text}, {"status": "done", "since": text}])
        self.assertEqual(c.calls, [("/questions", pmstart.HUB_SITE_TIMEOUT_S)] * 2)
        self.assertEqual(self.held(), {"at": AT.isoformat(timespec="seconds")})

    def test_the_next_start_counts_from_the_stamp_the_last_one_wrote(self):
        last = AT - datetime.timedelta(hours=3)
        self.write(json.dumps({"at": last.isoformat()}))
        later = (last + datetime.timedelta(hours=1)).astimezone(
            datetime.timezone(datetime.timedelta(hours=10)))
        c = FiledClient(filed(last - datetime.timedelta(seconds=1), last, later))
        self.assertEqual(pmstart.filed_digest(c, self.stamp, AT), [self.line(2, last)])
        self.assertEqual(c.queries[0]["since"], last.isoformat(timespec="seconds"))
        self.assertEqual(self.held()["at"], AT.isoformat(timespec="seconds"))

    def test_no_answer_filed_since_is_no_line_and_moves_the_stamp(self):
        last = AT - datetime.timedelta(hours=3)
        self.write(json.dumps({"at": last.isoformat()}))
        c = FiledClient(filed(last - datetime.timedelta(hours=1), None, "not a time",
                              "2026-10-06T08:00:00"))
        self.assertEqual(pmstart.filed_digest(c, self.stamp, AT), [])
        self.assertEqual(self.held()["at"], AT.isoformat(timespec="seconds"))

    def test_an_ask_that_fails_is_one_line_and_leaves_the_stamp_for_the_next_start(self):
        last = AT - datetime.timedelta(hours=3)
        self.write(json.dumps({"at": last.isoformat()}))
        c = FiledClient(filed(AT), error=OSError("C:\\secret"))
        self.assertEqual(pmstart.filed_digest(c, self.stamp, AT),
                         ["hub: answers filed since %s not asked (OSError)"
                          % last.isoformat(timespec="seconds")])
        self.assertEqual(self.held()["at"], last.isoformat())

    def test_answers_the_hub_marked_done_are_counted_and_named_apart(self):
        # red proof: before the hub marked filed answers done, a done one dropped out unseen
        last = AT - datetime.timedelta(hours=3)
        self.write(json.dumps({"at": last.isoformat()}))
        hour = datetime.timedelta(hours=1)
        c = FiledClient(filed(AT - hour),
                        done("hub", AT - hour, AT - 2 * hour, last - hour) + done(None, AT - hour))
        text = last.isoformat(timespec="seconds")
        self.assertEqual(pmstart.filed_digest(c, self.stamp, AT), [
            self.line(3, last) + '; 2 of them marked done by the hub once their asker had them '
            '(done_by hub): python "%s" questions --status done --since %s'
            % (pmstart.HUB_TOOL, text)])
        self.write(json.dumps({"at": last.isoformat()}))
        only = FiledClient(done=done("hub", AT - hour))     # none still filed: a line all the same
        self.assertEqual(pmstart.filed_digest(only, self.stamp, AT), [
            self.line(1, last) + '; 1 of them marked done by the hub once their asker had them '
            '(done_by hub): python "%s" questions --status done --since %s'
            % (pmstart.HUB_TOOL, text)])

    def test_a_done_ask_that_fails_is_one_line_and_leaves_the_stamp(self):
        last = AT - datetime.timedelta(hours=3)
        self.write(json.dumps({"at": last.isoformat()}))

        class DoneFails(FiledClient):
            def get(self, path, role=hubclient.ANY, query=None, timeout=None):
                if (query or {}).get("status") == "done":
                    raise OSError("C:\\secret")
                return FiledClient.get(self, path, role, query, timeout)
        self.assertEqual(pmstart.filed_digest(DoneFails(filed(AT)), self.stamp, AT),
                         ["hub: answers filed since %s not asked (OSError)"
                          % last.isoformat(timespec="seconds")])
        self.assertEqual(self.held()["at"], last.isoformat())

    def test_a_stamp_that_cannot_be_read_falls_back_to_24_hours(self):
        since = AT - pmstart.FILED_FALLBACK
        for text in ("{", "[]", '{"at": 7}', '{"at": "2026-10-06T06:00:00"}', '{"no": 1}'):
            with self.subTest(text=text):
                self.write(text)
                c = FiledClient(filed(AT))
                self.assertEqual(pmstart.filed_digest(c, self.stamp, AT), [self.line(1, since)])
                self.assertEqual(self.held()["at"], AT.isoformat(timespec="seconds"))

    def test_a_stamp_path_that_cannot_be_told_is_one_line_and_asks_nothing(self):
        def where():
            raise ConfigError("C:\\secret")
        c = FiledClient(filed(AT))
        self.assertEqual(pmstart.filed_digest(c, where, AT),
                         ["hub: answers filed since the last start not counted (ConfigError)"])
        self.assertEqual(c.calls, [])

    def test_the_site_lines_carry_the_digest_only_when_given_a_stamp(self):
        c = FiledClient(filed(AT - datetime.timedelta(hours=1)))
        out = pmstart.hub_site(load=lambda: SET, make_client=lambda port: c,
                               stamp=lambda: self.stamp, now=AT)
        self.assertEqual(out[0], "hub: server up on port 4321, 0 pending for the PM")
        self.assertEqual(out[1], self.line(1, AT - pmstart.FILED_FALLBACK))
        self.assertIn("push", out[2])
        self.assertEqual([t for _, t in c.calls], [pmstart.HUB_SITE_TIMEOUT_S] * 4)
        bare = FiledClient(filed(AT))
        self.assertEqual(len(site(bare)), 2)
        self.assertNotIn("/questions", [p for p, _ in bare.calls])

    def test_a_hub_that_is_down_reads_and_writes_no_stamp(self):
        def where():
            raise AssertionError("the stamp was looked up")
        out = pmstart.hub_site(load=lambda: SET,
                               make_client=lambda port: FiledClient(error=serve.NotRunning(port)),
                               stamp=where, now=AT)
        self.assertEqual(len(out), 1)
        self.assertFalse(os.path.exists(self.stamp))

    def test_the_stamp_sits_beside_the_pm_watch_state_file(self):
        from kitlib import pmwatch
        state = os.path.join(os.path.dirname(self.stamp), "state.json")
        with mock.patch.object(pmwatch, "load_config", lambda: {"state_file": state}):
            self.assertEqual(pmstart.filed_stamp_file(), self.stamp)
        with mock.patch.object(pmwatch, "load_config", lambda: {}):
            with mock.patch.object(pmwatch, "default_state_file", lambda: state):
                self.assertEqual(pmstart.filed_stamp_file(), self.stamp)

    def test_the_checklist_gives_the_hub_check_the_stamp_file(self):
        c = FiledClient(filed(datetime.datetime.now(UTC)))
        with mock.patch.object(pmstart, "filed_stamp_file", lambda: self.stamp):
            txt = pmstart.checklist({}, NOW, procs=[], hub_site={
                "load": lambda: SET, "make_client": lambda port: c}, **FAKES)
        self.assertIn("- hub: 1 answer(s) filed into records since ", txt)
        self.assertIn("at", self.held())

if __name__ == "__main__":
    unittest.main()
