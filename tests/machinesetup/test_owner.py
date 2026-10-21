"""The owner's email step of machine setup (kitlib.machinesetup.owner; docs/machine-setup.md "Owner
email"), the machine config writes it shares with the agent temp sweep key
(kitlib.machinesetup.machineconfig), the per-host plugin step's notes (plugin.survey_hosts) and
the tool's mode checks. A temp home only; nothing here reads or writes the real machine config."""
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from kitlib.config import user as kuser
from kitlib.machinesetup import agenttemp, machineconfig, owner, plugin
from kitlib.machinesetup.common import Plan
from tests import gitfixture
from tests.home import PM_REPO_FENCE_ENV, pm_repo

TOOL = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                    'tools', 'machine_setup.py')
OWNER = 'owner@example.invalid'


class Host(object):
    def __init__(self, email=None):
        self.email = email
        self.asked = []

    def git_email(self, kit):
        self.asked.append(kit)
        return self.email


class OwnerCase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix='kit-owner-home-')
        self.addCleanup(gitfixture.rmtree, self.home)
        self.kit = os.path.dirname(os.path.dirname(TOOL))
        self.folder = os.path.join(pm_repo(self), kuser.DIR_NAME)      # the fenced pointer's .pm-kit
        self.local = os.path.join(self.folder, kuser.LOCAL_FILE_NAME)  # owner_email's file

    def ctx(self, given=None, found=None):
        return types.SimpleNamespace(home=self.home, kit=self.kit, host=Host(found), owner_email=given)

    def config(self, data=None, raw=None, name=kuser.LOCAL_FILE_NAME):
        os.makedirs(self.folder, exist_ok=True)
        with open(os.path.join(self.folder, name), 'wb') as f:
            f.write(raw if raw is not None else json.dumps(data).encode('utf-8'))

    def read(self, name=kuser.LOCAL_FILE_NAME):
        with open(os.path.join(self.folder, name), 'rb') as f:
            return f.read()

    def plan(self, ctx):
        plan = Plan(Plan.FRESH_SECTIONS)
        owner.survey(plan, ctx)
        return plan

    def apply(self, plan):
        for c in plan.changes:
            c.do()


class Survey(OwnerCase):
    def test_given_is_stored_in_a_new_machine_config(self):
        plan = self.plan(self.ctx(given=OWNER))
        self.assertEqual(len(plan.changes), 1, plan.lines())
        self.assertIn('owner_email = %s (from --owner-email; unset before)' % OWNER, plan.changes[0].text)
        self.assertFalse(os.path.exists(self.local))                       # the dry run writes nothing
        self.apply(plan)
        self.assertEqual(kuser.owner_email(self.home), OWNER)
        self.assertEqual(self.plan(self.ctx(given=OWNER.upper())).changes, [])   # stored: nothing to do

    def test_git_config_is_only_suggested_never_stored_unconfirmed(self):
        """A git identity can be any address, an agent's among them: --apply does not store it on
        its own. The note names it and how to confirm it; the console question offers it."""
        ctx = self.ctx(found=OWNER)
        plan = self.plan(ctx)
        self.assertEqual((plan.changes, plan.refusals), ([], []))
        self.assertTrue(any('git config user.email is %s' % OWNER in n and '--owner-email %s' % OWNER in n
                            and 'not stored unconfirmed' in n for n in plan.notes['owner']), plan.lines())
        self.assertEqual(ctx.host.asked, [self.kit])
        self.assertTrue(owner.needs_prompt(ctx))
        self.assertEqual(owner.suggestion(ctx), OWNER)
        self.assertIsNone(owner.suggestion(self.ctx(found='not-an-email')))
        self.assertFalse(os.path.exists(self.local))

    def test_a_stored_one_is_kept_and_git_config_not_asked(self):
        self.config({'owner_email': OWNER})
        ctx = self.ctx(found='other@example.invalid')
        self.assertEqual(self.plan(ctx).changes, [])
        self.assertEqual(ctx.host.asked, [])
        self.assertFalse(owner.needs_prompt(ctx))

    def test_given_replaces_a_stored_one_naming_it(self):
        self.config({'owner_email': 'old@example.invalid', 'scrub_patterns': []})
        self.config({'temp_dir': self.home}, name=kuser.FILE_NAME)
        plan = self.plan(self.ctx(given=OWNER))
        self.assertIn('was old@example.invalid', plan.changes[0].text)
        self.assertIn('set owner_email back to old@example.invalid', plan.changes[0].undo)
        self.apply(plan)
        self.assertEqual(json.loads(self.read()), {'owner_email': OWNER, 'scrub_patterns': []})
        self.assertEqual(json.loads(self.read(kuser.FILE_NAME)), {'temp_dir': self.home})   # untouched

    def test_none_found_is_a_note_and_a_prompt(self):
        ctx = self.ctx()
        plan = self.plan(ctx)
        self.assertEqual(plan.changes, [])
        self.assertTrue(any(n.startswith('no owner email') for n in plan.notes['owner']), plan.lines())
        self.assertTrue(owner.needs_prompt(ctx))
        self.assertFalse(owner.needs_prompt(self.ctx(given=OWNER)))

    def test_a_bad_email_is_refused_given_and_noted_found(self):
        plan = self.plan(self.ctx(given='not-an-email'))
        self.assertEqual(len(plan.refusals), 1)
        self.assertIn('--owner-email', plan.refusals[0])
        plan = self.plan(self.ctx(found='not-an-email'))
        self.assertEqual((plan.changes, plan.refusals), ([], []))
        self.assertTrue(any('is not an email address' in n for n in plan.notes['owner']))

    def test_an_unreadable_machine_config_refuses(self):
        self.config(raw=b'{not json')
        plan = self.plan(self.ctx(given=OWNER))
        self.assertEqual(len(plan.refusals), 1)
        self.assertIn('cannot read the machine config for owner_email', plan.refusals[0])


class SharedWrites(OwnerCase):
    """The owner's email (the PM repository's local.json) and the sweep key (its config.json),
    planned in one run, both land, each file's other keys and layout kept; a file changed by
    someone else since the survey refuses the write."""

    def test_two_keys_in_one_run(self):
        self.config(raw=('{\r\n    "temp_dir": %s\r\n}\r\n' % json.dumps(self.home)).encode('utf-8'),
                    name=kuser.FILE_NAME)
        self.config(raw=b'{\r\n    "scrub_patterns": []\r\n}\r\n')
        ctx = self.ctx(given=OWNER)
        snap, why = machineconfig.snapshot(ctx, agenttemp.KEY)
        self.assertIsNone(why)
        self.assertEqual(snap.path, os.path.join(self.folder, kuser.FILE_NAME))
        self.assertIs(machineconfig.snapshot(ctx, agenttemp.KEY)[0], snap)      # one per run
        self.assertIs(machineconfig.snapshot(ctx, 'temp_dir')[0], snap)         # and per file
        local, why = machineconfig.snapshot(ctx, owner.KEY)
        self.assertEqual((local.path, why), (self.local, None))
        plan = self.plan(ctx)
        folder = os.path.join(self.home, 'agent-temp')
        plan.add('extras', 'sweep key', lambda: agenttemp.write_sweep_key(self.home, snap, folder))
        self.apply(plan)
        raw = self.read()
        self.assertIn(b'\r\n    "owner_email": "%s"' % OWNER.encode(), raw)  # its layout kept
        self.assertEqual(json.loads(raw), {'scrub_patterns': [], 'owner_email': OWNER})
        raw = self.read(kuser.FILE_NAME)
        self.assertIn(b'\r\n    "agent_temp_sweep": ', raw)
        self.assertEqual(json.loads(raw), {'temp_dir': self.home, 'agent_temp_sweep': {
            'dir': folder, 'max_age_days': agenttemp.MAX_AGE_DAYS}})
        self.assertEqual(kuser.owner_email(self.home), OWNER)

    def test_with_no_pointer_both_keys_snapshot_the_home_file(self):
        # the old layout, kept until the writer refuses agent sessions (B3a-2)
        patch = mock.patch.dict(os.environ, {PM_REPO_FENCE_ENV: ''})
        patch.start()
        self.addCleanup(patch.stop)
        kuser.forget()
        ctx = self.ctx()
        snap, why = machineconfig.snapshot(ctx, owner.KEY)
        self.assertEqual((snap.path, why), (os.path.join(self.home, kuser.DIR_NAME, kuser.FILE_NAME), None))
        self.assertIs(machineconfig.snapshot(ctx, agenttemp.KEY)[0], snap)

    def test_a_pm_data_write_has_no_snapshot(self):
        snap, why = machineconfig.snapshot(self.ctx(), 'pm_data')
        self.assertIsNone(snap)
        self.assertIn('pm_data is derived', why)

    def test_a_file_changed_since_the_survey_is_not_written(self):
        self.config({})
        plan = self.plan(self.ctx(given=OWNER))
        self.config({'scrub_patterns': []})
        with self.assertRaises(RuntimeError) as e:
            self.apply(plan)
        self.assertIn('changed since the survey', str(e.exception))
        self.assertEqual(json.loads(self.read()), {'scrub_patterns': []})


class Hosts(unittest.TestCase):
    def test_a_host_machine_setup_does_not_install_is_a_note(self):
        calls = []
        plan = Plan(Plan.FRESH_SECTIONS)
        with mock.patch.object(plugin, 'survey', lambda p, c: calls.append(c)):
            plugin.survey_hosts(plan, 'ctx')
        self.assertEqual(calls, ['ctx'])
        self.assertEqual(plan.changes, [])
        self.assertEqual(plan.notes['plugin'], [
            'codex: not installed. Machine setup installs no plugin for this host yet: its files are '
            'review templates, not installed (hosts/codex/README.md)'])


class Prompt(unittest.TestCase):
    """tools/machine_setup.py's console question: git config user.email is offered, and stored only
    on a yes or when typed."""

    def ask(self, answer, offered=OWNER):
        from tools import machine_setup
        with mock.patch('builtins.input', lambda prompt: answer), mock.patch('builtins.print'):
            return machine_setup.ask_owner_email(offered)

    def test_the_offered_email_needs_a_yes(self):
        self.assertEqual(self.ask('y'), OWNER)
        self.assertEqual(self.ask('YES'), OWNER)
        self.assertIsNone(self.ask(''))
        self.assertIsNone(self.ask('n'))
        self.assertEqual(self.ask('other@example.invalid'), 'other@example.invalid')
        self.assertIsNone(self.ask('y', offered=None))               # no offer: y is no address
        self.assertIsNone(self.ask('not-an-email'))


class Modes(unittest.TestCase):
    def tool(self, *args):
        return subprocess.run([sys.executable, TOOL] + list(args), capture_output=True, text=True)

    def test_mode_usage(self):
        r = self.tool('--fresh', '--old-kit', os.path.dirname(TOOL))
        self.assertEqual(r.returncode, 2)
        self.assertIn('not allowed with', r.stderr)
        r = self.tool('--old-kit', os.path.dirname(TOOL), '--pm-settings', 'x')
        self.assertEqual(r.returncode, 2)
        self.assertIn('--pm-settings goes with the default mode only', r.stderr)
        r = self.tool('--stubs')
        self.assertEqual(r.returncode, 2)
        self.assertIn('--stubs goes with --repo only', r.stderr)


if __name__ == '__main__':
    unittest.main()
