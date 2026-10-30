"""kitlib.maintlint.gatetime and its filing from tools/maint_lint.py: a gate step whose median time
over full gates is over the bound is one finding and one record, never a second while that record
is live; under the bound, too few samples, or no full gate records file nothing; --dry-run files
nothing (docs/maint-lint.md "Gate time")."""
import datetime
import io
import json
import os
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from unittest import mock

from kitlib import maintlint
from kitlib.maintlint import gatetime
from kitlib.records import gate_ok

from tests.backlog.helpers import StoreCase, kit_json_ref
from tests.gitfixture import rmtree
from tests.home import result_drive
from tools import maint_lint

NOW = datetime.datetime(2026, 10, 24, 12, 0, tzinfo=datetime.timezone.utc)
FILING = {'maint_lint': {'gate_time': {'prefix': 'EX'}}}


class GateTimeCase(StoreCase):

    def setUp(self):
        StoreCase.setUp(self)
        result_drive(self)
        self.root = self.repo.root
        self.folder = gate_ok.record_dir(self.root)
        self.n = 0

    def gates(self, count, seconds, step='kit-suite', full=True, mode='full', start=NOW,
              folder=None, first=None):
        """`count` gate-OK records, an hour apart before `start`, each timing `step`, in `folder`
        (default the record folder), numbered from `first` (default the next unused)."""
        folder = folder or self.folder
        os.makedirs(folder, exist_ok=True)
        if first is not None:
            self.n = first - 1
        for i in range(count):
            self.n += 1
            ended = start - datetime.timedelta(hours=i + 1)
            rec = {'commit': '%040x' % self.n, 'full': full, 'mode': mode,
                   'ended_utc': ended.strftime('%Y-%m-%dT%H:%M:%SZ'),
                   'steps': [{'name': step, 'result': 'pass', 'seconds': seconds}]}
            with open(os.path.join(folder, '%040x.json' % self.n), 'w',
                      encoding='utf-8') as fh:
                json.dump(rec, fh)

    def lint(self, now=NOW):
        return maintlint.run(self.root, home=self.home, now=now)

    def checks(self, res):
        return [(f['check'], f['target']) for f in res['findings'] if f['check'] == 'gate-time']

    def gate_records(self):
        return sorted(r.stem for r in self.store.load().records if '-GATE-TIME-' in r.stem)


class Filing(GateTimeCase):

    def test_a_step_over_the_bound_is_one_finding_and_files_one_record(self):
        kit_json_ref(self.repo, FILING)
        self.gates(6, 1000)
        res = self.lint()
        self.assertEqual(self.checks(res), [('gate-time', 'kit-suite')], res['notes'])
        self.assertEqual(res['counts']['gate-time'], 1)
        lines = gatetime.file(self.root, res, NOW)
        self.assertEqual(self.gate_records(), ['EX-GATE-TIME-KIT-SUITE-20261024'], lines)
        rec = self.store.load().get('EX-GATE-TIME-KIT-SUITE-20261024')
        self.assertEqual(rec.get('ref'), 'maint-lint gate-time kit-suite')
        self.assertIn('median 1000 s', rec.get('summary'))

    def test_a_second_run_files_no_duplicate_while_the_record_is_live(self):
        kit_json_ref(self.repo, FILING)
        self.gates(6, 1000)
        gatetime.file(self.root, self.lint(), NOW)
        later = NOW + datetime.timedelta(days=1)
        lines = gatetime.file(self.root, self.lint(later), later)
        self.assertEqual(self.gate_records(), ['EX-GATE-TIME-KIT-SUITE-20261024'])
        self.assertIn('gate time: EX-GATE-TIME-KIT-SUITE-20261024 is already filed for gate '
                      'step kit-suite', lines)

    def test_once_the_record_is_closed_a_later_run_files_a_new_one(self):
        kit_json_ref(self.repo, FILING)
        self.gates(6, 1000)
        gatetime.file(self.root, self.lint(), NOW)
        self.verb('close', 'EX-GATE-TIME-KIT-SUITE-20261024', 'the step was made faster')
        later = NOW + datetime.timedelta(days=1)
        gatetime.file(self.root, self.lint(later), later)
        self.assertEqual(self.gate_records(), ['EX-GATE-TIME-KIT-SUITE-20261025'])

    def test_a_same_day_refile_after_a_close_is_one_line_never_raised(self):
        kit_json_ref(self.repo, FILING)
        self.gates(6, 1000)
        gatetime.file(self.root, self.lint(), NOW)
        self.verb('close', 'EX-GATE-TIME-KIT-SUITE-20261024', 'the step was made faster')
        lines = gatetime.file(self.root, self.lint(), NOW)
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].startswith(
            'gate time: not filed EX-GATE-TIME-KIT-SUITE-20261024: '), lines)
        self.assertEqual(self.gate_records(), [])   # closed, and not filed again

    def test_a_live_record_of_a_longer_step_name_does_not_hold_a_shorter_one(self):
        kit_json_ref(self.repo, FILING)
        self.gates(6, 1000, step='test-e2e')
        gatetime.file(self.root, self.lint(), NOW)
        self.assertEqual(self.gate_records(), ['EX-GATE-TIME-TEST-E2E-20261024'])
        self.gates(6, 1000, step='test')
        gatetime.file(self.root, self.lint(), NOW)
        self.assertEqual(self.gate_records(), ['EX-GATE-TIME-TEST-20261024',
                                               'EX-GATE-TIME-TEST-E2E-20261024'])

    def test_the_median_of_an_even_count_is_the_mean_of_the_middle_two(self):
        # Sorted: 4 x 100, 1000, 1200, 4 x 1300. Median 1100; the mean (780) is under the bound,
        # the max is 1300, the lower and upper middles are 1000 and 1200.
        kit_json_ref(self.repo, FILING)
        self.gates(4, 100)
        self.gates(1, 1000, start=NOW - datetime.timedelta(hours=4))
        self.gates(1, 1200, start=NOW - datetime.timedelta(hours=5))
        self.gates(4, 1300, start=NOW - datetime.timedelta(hours=6))
        res = self.lint()
        self.assertEqual(self.checks(res), [('gate-time', 'kit-suite')], res['notes'])
        text = [f['text'] for f in res['findings'] if f['check'] == 'gate-time'][0]
        self.assertIn('median 1100 s over its last 10 full gate(s)', text)

    def test_a_median_under_the_bound_is_no_finding_whatever_the_mean_or_max(self):
        # 6 x 100 and 4 x 3000: the mean (1260) and the max are over 900, the median (100) is not.
        kit_json_ref(self.repo, FILING)
        self.gates(6, 100)
        self.gates(4, 3000, start=NOW - datetime.timedelta(hours=6))
        self.assertEqual(self.checks(self.lint()), [])

    def test_a_median_equal_to_the_bound_is_no_finding(self):
        kit_json_ref(self.repo, FILING)
        self.gates(6, 900)
        self.assertEqual(self.checks(self.lint()), [])

    def test_a_step_under_the_bound_files_nothing(self):
        kit_json_ref(self.repo, FILING)
        self.gates(6, 800)
        res = self.lint()
        self.assertEqual(self.checks(res), [])
        self.assertEqual(gatetime.file(self.root, res, NOW), [])
        self.assertEqual(self.gate_records(), [])

    def test_too_few_samples_files_nothing_and_says_so(self):
        kit_json_ref(self.repo, FILING)
        self.gates(4, 2000)
        res = self.lint()
        self.assertEqual(self.checks(res), [])
        self.assertIn('gate time: kit-suite has 4 full gate sample(s) in 14 days, under 5: not '
                      'judged', res['notes'])
        gatetime.file(self.root, res, NOW)
        self.assertEqual(self.gate_records(), [])

    def test_the_bound_comes_from_kit_json(self):
        kit_json_ref(self.repo, {'maint_lint': {'gate_time': {'prefix': 'EX', 'bound_s': 1500}}})
        self.gates(6, 1000)
        self.assertEqual(self.checks(self.lint()), [])

    def test_the_median_is_over_the_newest_ten_in_fourteen_days(self):
        kit_json_ref(self.repo, FILING)
        self.gates(10, 800)                                          # the newest ten
        self.gates(10, 5000, start=NOW - datetime.timedelta(hours=10))   # older: out of the window
        self.gates(6, 5000, start=NOW - datetime.timedelta(days=15))     # out of the 14 days
        self.assertEqual(self.checks(self.lint()), [])

    def test_selected_and_docs_gates_are_not_samples(self):
        kit_json_ref(self.repo, FILING)
        self.gates(6, 2000, full=False, mode='select')
        self.gates(6, 2000, full=False, mode='docs')
        res = self.lint()
        self.assertEqual(self.checks(res), [])
        self.assertIn('gate time NOT CHECKED: no full gate record in the last 14 days',
                      res['notes'])

    def test_git_copies_are_samples_only_with_no_dev_drive(self):
        kit_json_ref(self.repo, FILING)
        old = gate_ok.legacy_dir(self.root)
        self.gates(6, 1000, folder=old)                 # only in .git
        self.gates(6, 100, first=1)                     # the same six commits, in the new folder
        self.gates(3, 5000, folder=old, first=7,        # three more commits, only in .git
                   start=NOW - datetime.timedelta(hours=6))
        self.assertEqual(gatetime.samples(self.root, NOW), ({'kit-suite': [100.0] * 6}, None))
        with mock.patch('kitlib.config.user.dev_drive', return_value=None):
            got, note = gatetime.samples(self.root, NOW)
        self.assertIsNone(note)
        self.assertEqual(sorted(got['kit-suite']), [1000.0] * 6 + [5000.0] * 3)

    def test_no_gate_records_is_a_note_and_no_finding(self):
        kit_json_ref(self.repo, FILING)
        res = self.lint()
        self.assertEqual(self.checks(res), [])
        self.assertTrue([n for n in res['notes'] if n.startswith('gate time NOT CHECKED: no '
                                                                 'gate-OK records')], res['notes'])

    def test_without_a_prefix_the_finding_shows_and_nothing_is_filed(self):
        kit_json_ref(self.repo)
        self.gates(6, 1000)
        res = self.lint()
        self.assertEqual(self.checks(res), [('gate-time', 'kit-suite')])
        lines = gatetime.file(self.root, res, NOW)
        self.assertEqual(lines, ['gate time: not filed (1 step(s) over the bound): no '
                                 '"maint_lint".gate_time.prefix in .kit.json'])
        self.assertEqual(self.gate_records(), [])

    def test_a_store_refusal_is_a_line_never_raised(self):
        kit_json_ref(self.repo, FILING)
        self.gates(6, 1000)
        from kitlib.backlog.errors import Refusal

        def refuse(root, session):
            raise Refusal('the store said no')
        self.assertEqual(gatetime.file(self.root, self.lint(), NOW, opener=refuse),
                         ['gate time: not filed: the store said no'])


class Cli(GateTimeCase):

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), \
                mock.patch.object(maint_lint.slot, 'take', return_value=nullcontext()), \
                mock.patch.object(maint_lint.publish, 'publish', return_value=(None, [])):
            code = maint_lint.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def temp(self):
        import tempfile
        path = tempfile.mkdtemp(prefix='gatetime-')
        self.addCleanup(rmtree, path)
        return path

    def test_a_plain_run_files_the_record(self):
        kit_json_ref(self.repo, FILING)
        self.gates(6, 1000, start=datetime.datetime.now(datetime.timezone.utc))
        folder = self.temp()
        code, out, err = self.main('-C', self.root, '--result', os.path.join(folder, 'result.json'))
        self.assertEqual(code, 1, out + err)
        self.assertEqual(len(self.gate_records()), 1, out)
        self.assertIn('gate time: ', out)

    def test_dry_run_files_nothing(self):
        kit_json_ref(self.repo, FILING)
        self.gates(6, 1000, start=datetime.datetime.now(datetime.timezone.utc))
        folder = self.temp()
        code, out, err = self.main('-C', self.root, '--dry-run',
                                   '--out', os.path.join(folder, 'page.html'))
        self.assertEqual(code, 1, out + err)
        self.assertIn('gate-time', out)
        self.assertEqual(self.gate_records(), [])
        self.assertNotIn('gate time: ', out)
