"""kitlib.gate.gatecap: the session gate cap (docs/gate.md "The session gate cap"). A branch gate
waits while its agent session has the cap's number of gate runs of the repository in flight; a
landing, the daily full run and the owner never wait. Every in-flight run here is a scratch-store
record (StoreCase.live_record) whose ancestors hold a made-up agent host."""
import functools
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from kitlib import gate as G
import kitlib.config as kconfig
from kitlib.config import user as kuser
from kitlib.gate import gatecap, runner
from kitlib.gate import steps as S
from kitlib.proc import process
from tests.gate import fixtures

HOST = [912345, 77]
OTHER_HOST = [912346, 78]
AGENT = {'role': 'agent', 'host': HOST, 'ancestors': [HOST], 'session': 'S1', 'name': 't'}
OTHER = {'role': 'agent', 'host': OTHER_HOST, 'ancestors': [OTHER_HOST], 'session': 'S2',
         'name': 't'}
# The owner: no agent host above it, and the same (untrusted) session id as the agent's runs.
OWNER = {'role': 'owner', 'host': None, 'ancestors': [], 'session': 'S1', 'name': 't'}


class Wrongful(Exception):
    """Raised by a sleep that must never be called: a gate that waits fails at once."""


def never(_s):
    raise Wrongful('the gate waited for the session gate cap')


class TempHome(unittest.TestCase):
    def home(self, data):
        """A temp home whose machine config is `data` (kitlib.config.user DIR_NAME/FILE_NAME)."""
        h = tempfile.mkdtemp(prefix='gatecap-home-')
        self.addCleanup(shutil.rmtree, h, True)
        os.makedirs(os.path.join(h, kuser.DIR_NAME))
        with open(os.path.join(h, kuser.DIR_NAME, kuser.FILE_NAME), 'w', encoding='utf-8') as f:
            json.dump(data, f)
        return h


class Configured(TempHome):
    """The cap from "coordinator_start".gate_cap; unset, 0 and null mean no cap."""
    REPO = 'Some-Repo'

    def cap(self, data):
        return gatecap.configured(self.REPO, home=self.home(data))

    def test_the_key_passes_the_real_validator(self):
        data = {'coordinator_start': {'gate_cap': {self.REPO: 4}}}
        loaded = kuser.user_config(home=self.home(data))        # raises ConfigError if refused
        self.assertEqual(loaded['coordinator_start'], data['coordinator_start'])

    def test_exact_name_then_lower_case(self):
        self.assertEqual(self.cap({'coordinator_start': {'gate_cap': {self.REPO: 4}}}), (4, None))
        self.assertEqual(self.cap({'coordinator_start': {'gate_cap': {self.REPO.lower(): 3}}}),
                         (3, None))

    def test_off_by_default_and_for_zero_and_null(self):
        for data in ({}, {'coordinator_start': {}}, {'coordinator_start': {'gate_cap': None}},
                     {'coordinator_start': {'gate_cap': {}}},
                     {'coordinator_start': {'gate_cap': {'another-repo': 4}}},
                     {'coordinator_start': {'gate_cap': {self.REPO: 0}}},
                     {'coordinator_start': {'gate_cap': {self.REPO: None}}}):
            self.assertEqual(self.cap(data), (None, None), data)

    def test_a_value_it_cannot_use_warns_and_caps_nothing(self):
        for value in ('four', -1, 2.5, True):
            cap, warning = self.cap({'coordinator_start': {'gate_cap': {self.REPO: value}}})
            self.assertIsNone(cap, value)
            self.assertIn('WARNING: the session gate cap is not applied', warning)
        cap, warning = self.cap({'coordinator_start': {'gate_cap': [4]}})
        self.assertIsNone(cap)
        self.assertIn('is not an object', warning)


class CapCase(fixtures.GateRepoCase):
    def setUp(self):
        fixtures.GateRepoCase.setUp(self)
        self.wt, _ = self.branch('feature', {'a.py': 'x = 1\n'})
        self.name = runner.repo_name(kconfig.load(self.wt), self.wt)
        self.n = 0

    def run_rec(self, host=HOST, **fields):
        """A live gate run of this repo booked inside `host`'s session; its id."""
        self.n += 1
        rid = 'r-20261019T120000Z-%06x' % self.n
        fields.setdefault('repo', self.name)
        fields.setdefault('gate', True)
        fields.setdefault('session', AGENT['session'] if host == HOST else OTHER['session'])
        self.live_record(rid, ancestors=[host], **fields)
        return rid

    def drop(self, rid):
        os.remove(self.store.path('active', '%s.json' % rid))

    def capped(self, cap=4, who=AGENT, sleep=never):
        """kitlib.gate.gatecap.wait_turn with this cap, caller and sleep, as runner calls it."""
        return mock.patch.object(gatecap, 'wait_turn', functools.partial(
            gatecap.wait_turn, cap=cap, who=who, sleep=sleep))

    def branch_gate(self, say=None):
        return runner.branch_gate(self.wt, book_kw=self.book_kw, log_dir=self.logs, say=say)


class Counting(CapCase):
    def test_only_the_sessions_gate_runs_of_the_repo_count(self):
        mine = [self.run_rec() for _ in range(2)]
        self.run_rec(priority='land')                   # a landing's run
        self.run_rec(host=OTHER_HOST)                   # another session's
        self.run_rec(repo='another-repo')               # another repository's
        self.run_rec(gate=False)                        # not a gate run
        self.run_rec(repo=self.name.upper())            # the same repo, in other case: counted
        got = [r['id'] for r in gatecap.in_flight(self.store, AGENT, self.name)]
        self.assertEqual(got, mine + ['r-20261019T120000Z-%06x' % self.n])

    def test_a_run_this_process_is_inside_is_not_counted(self):
        """A gate nested in the session's own gate run never waits for that run."""
        self.run_rec()
        inside = dict(AGENT, ancestors=[HOST, [os.getpid(), process.own_ctime()]])
        self.assertEqual(gatecap.in_flight(self.store, inside, self.name), [])


class BranchGateWaits(CapCase):
    def test_at_the_cap_it_says_one_line_and_books_when_one_ends(self):
        ids = [self.run_rec() for _ in range(4)]
        naps = []

        def sleep(s):                   # two polls at the cap, then one run ends
            naps.append(s)
            if len(naps) == 2:
                self.drop(ids[0])
        lines = []
        with self.capped(sleep=sleep):
            out = self.branch_gate(say=lines.append)
        self.assertEqual(out.code, G.OK, out.lines)
        waits = [t for t in lines if 'session gate cap' in t]
        self.assertEqual(len(waits), 1, lines)          # one line, nothing per poll
        self.assertIn('4 of 4 gate runs of this session in %s' % self.name, waits[0])
        for rid in ids:
            self.assertIn(rid, waits[0])
        self.assertEqual(naps, [gatecap.POLL_S, gatecap.POLL_S])
        self.assertGreaterEqual(gatecap.POLL_S, self.store.settings.queue_poll_s)
        self.assertEqual([c.get('gate') for c in self.book_calls], [True])   # booked once, after

    def test_below_the_cap_it_books_at_once(self):
        for _ in range(3):
            self.run_rec()
        lines = []
        with self.capped():
            out = self.branch_gate(say=lines.append)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertFalse([t for t in lines if 'session gate cap' in t], lines)

    def test_the_cap_from_the_machine_config_is_read_when_runs_are_in_flight(self):
        for _ in range(2):
            self.run_rec()
        home = TempHome.home(self, {'coordinator_start': {'gate_cap': {self.name: 2}}})
        with self.assertRaises(Wrongful):
            gatecap.wait_turn(self.store, self.name, lambda t: None, who=AGENT, home=home,
                              sleep=never)
        off = TempHome.home(self, {'coordinator_start': {'gate_cap': {self.name: 0}}})
        gatecap.wait_turn(self.store, self.name, lambda t: None, who=AGENT, home=off, sleep=never)

    def test_with_nothing_in_flight_it_reads_neither_caller_nor_config(self):
        with mock.patch.object(gatecap, 'actor', side_effect=AssertionError('caller read')), \
                mock.patch.object(gatecap, 'configured', side_effect=AssertionError('config read')):
            gatecap.wait_turn(self.store, self.name, lambda t: None, sleep=never)


class NeverWaits(CapCase):
    """Each exemption, against four runs that would hold a capped agent gate at the cap."""

    def test_the_owner_never_waits(self):
        """The owner has no agent host: nothing is its session's, even runs that carry its session
        id. A cap counted by session id or by repository fails this."""
        for _ in range(4):
            self.run_rec()
        with self.capped(who=OWNER):
            out = self.branch_gate()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(gatecap.in_flight(self.store, OWNER, self.name), [])

    def test_a_landings_runs_do_not_count(self):
        """Three gate runs and a landing's at cap 4: the gate books at once. Counting priority
        "land" runs fails this."""
        for _ in range(3):
            self.run_rec()
        self.run_rec(priority='land')
        with self.capped():
            out = self.branch_gate()
        self.assertEqual(out.code, G.OK, out.lines)

    def test_a_landings_merged_gate_never_waits(self):
        """merged_gate, the landing's gate, books through gated() and never asks the cap. A cap
        checked in gated() fails this."""
        for _ in range(4):
            self.run_rec()
        with self.capped():
            out = runner.merged_gate(self.wt, kconfig.load(self.wt), self.main, log_dir=self.logs,
                                     book_kw=self.book_kw)
        self.assertEqual(out.code, G.OK, out.lines)

    def test_the_daily_full_runs_gate_never_waits(self):
        """The daily full run calls runner.gated directly (kitlib.dailyfull run). A cap checked in
        gated() fails this."""
        for _ in range(4):
            self.run_rec()
        cfg = kconfig.load(self.wt)
        chosen = S.build(cfg.section('gate'), self.wt, self.main, cfg.tools_dir, full=True)
        os.makedirs(self.logs)                          # the daily run makes its log dir first
        with self.capped():
            ran, refused = runner.gated(cfg, self.wt, self.main, [], chosen, 'engine-test',
                                        self.name, 'daily', self.logs, lambda t: None,
                                        self.book_kw)
        self.assertIsNone(refused)
        self.assertTrue(runner.verdict(ran)[0], ran)


if __name__ == '__main__':
    unittest.main()
