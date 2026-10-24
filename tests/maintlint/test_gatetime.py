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
from tools import maint_lint

NOW = datetime.datetime(2026, 10, 24, 12, 0, tzinfo=datetime.timezone.utc)
FILING = {'maint_lint': {'gate_time': {'prefix': 'EX'}}}


class GateTimeCase(StoreCase):

    def setUp(self):
        StoreCase.setUp(self)
        self.root = self.repo.root
        self.folder = gate_ok.record_dir(self.root)
        self.n = 0

    def gates(self, count, seconds, step='kit-suite', full=True, mode='full', start=NOW):
        """`count` gate-OK records, an hour apart before `start`, each timing `step`."""
        os.makedirs(self.folder, exist_ok=True)
        for i in range(count):
            self.n += 1
            ended = start - datetime.timedelta(hours=i + 1)
            rec = {'commit': '%040x' % self.n, 'full': full, 'mode': mode,
                   'ended_utc': ended.strftime('%Y-%m-%dT%H:%M:%SZ'),
                   'steps': [{'name': step, 'result': 'pass', 'seconds': seconds}]}
            with open(os.path.join(self.folder, '%040x.json' % self.n), 'w',
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
