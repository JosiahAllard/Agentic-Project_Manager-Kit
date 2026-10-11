"""The old hook and hub paths stay live until settings point at claude/; delete this package then."""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
PAYLOAD = json.dumps({"hook_event_name": "SessionStart", "source": "startup", "cwd": ROOT})


def run(path, role):
    """Run a stub with an empty temp home, so it never reads the live machine config (~/.pm-kit)."""
    env = dict(os.environ)
    env.pop("PM_KIT_ROLE", None)
    if role:
        env["PM_KIT_ROLE"] = role
    with tempfile.TemporaryDirectory() as home:
        env["USERPROFILE"] = env["HOME"] = home
        r = subprocess.run([sys.executable, path], input=PAYLOAD, capture_output=True, text=True, env=env, timeout=60)
    return r.returncode, r.stdout


def steady(result):
    """(code, out) with every "<n> min ago" age blanked: the two runs are a moment apart, so a minute boundary between them
    would otherwise make the same stub print 8099 in one and 8100 in the other."""
    return result[0], re.sub(r"\d+ min ago", "N min ago", result[1])


class StubTest(unittest.TestCase):
    def same(self, name, role):
        old = run(os.path.join(ROOT, "hooks", name), role)
        new = run(os.path.join(ROOT, "claude", "hooks", name.replace("conductor", "coordinator")), role)
        self.assertEqual(old[0], 0)
        self.assertEqual(steady(old), steady(new))

    def test_steady_ignores_an_age_ticking_over_a_minute(self):
        self.assertEqual(steady((0, "last seen 8099 min ago; x")), steady((0, "last seen 8100 min ago; x")))

    def test_steady_still_sees_other_differences(self):
        self.assertNotEqual(steady((0, "8099 min ago")), steady((0, "8099 min ago!")))
        self.assertNotEqual(steady((0, "8099 min ago")), steady((1, "8099 min ago")))

    def test_coordinator_stub_silent_without_role(self):
        self.same("conductor_session_start.py", None)

    def test_coordinator_stub_with_role(self):
        self.same("conductor_session_start.py", "coordinator")

    def test_coordinator_stub_with_old_role(self):
        self.same("conductor_session_start.py", "conductor")

    def test_pm_stub(self):
        self.same("pm_session_start.py", None)
        code, out = run(os.path.join(ROOT, "hooks", "pm_session_start.py"), None)
        self.assertIn("SessionStart", out)


class HubCopyTest(unittest.TestCase):
    def test_old_hub_is_byte_identical(self):
        with open(os.path.join(ROOT, "hub", "action-hub.html"), "rb") as a, \
                open(os.path.join(ROOT, "claude", "hub", "action-hub.html"), "rb") as b:
            self.assertEqual(a.read(), b.read())


if __name__ == "__main__":
    unittest.main()
