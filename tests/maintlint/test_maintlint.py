"""kitlib.maintlint and tools/maint_lint.py: the checks on in-memory snapshots, the whole run on a
real store (a record that is merely old gives no finding), the ledger as counts only, the result
file, and the page that kitlib.hubsite.pagecheck passes."""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from kitlib import maintlint
from kitlib.backlog import audit_check
from kitlib.backlog import record as rmod
from kitlib.backlog import store as smod
from kitlib.hubsite import pagecheck
from kitlib.maintlint import Finding, backlog, ledger, page, result, verify, worktrees

from tests.backlog.helpers import BRANCH, StoreCase, kit_json_ref, priced, rec
from tests.backlog.test_stale import at
from tests.gitfixture import rmtree
from tests.ledger.test_standing import entry, rid
from tools import maint_lint

CMD = 'python "kit/tools/backlog.py" -C "wt"'


def snap(*records):
    texts = {}
    for r in records:
        texts.update(r)
    parsed = [rmod.parse('backlog/%s.md' % stem, text, stem) for stem, text in texts.items()]
    return smod.Snapshot(parsed, 'a' * 40, 'test', rmod.Rules('upper', ('L0', 'L1')))


def temp_dir(case):
    path = tempfile.mkdtemp(prefix='maintlint-')
    case.addCleanup(rmtree, path)
    return path


class BacklogChecks(unittest.TestCase):

    def test_records_with_the_same_summary_are_one_finding_that_retires_the_rest(self):
        got, _ = backlog.duplicates(snap(priced('AAA', summary='Fix the thing.'),
                                         rec('BBB', summary='fix the  THING'),
                                         rec('CCC', summary='fix the thing, too')), CMD)
        self.assertEqual([(f.check, f.key) for f in got], [('duplicate', 'AAA BBB')])
        self.assertEqual(got[0].fix, CMD + ' retire BBB --why "duplicate of AAA"')

    def test_a_blocker_naming_no_live_record_is_a_finding_and_a_live_one_is_not(self):
        got, _ = backlog.dead_blockers(snap(
            rec('AAA', state='blocked', blocked_on='item:GONE'),
            rec('BBB', state='blocked', blocked_on='item:AAA')), CMD)
        self.assertEqual([f.key for f in got], ['AAA item:GONE'])
        self.assertEqual(got[0].fix, CMD + ' unblock AAA --on item:GONE --reason "<why>"')

    def test_an_open_record_with_no_price_is_unpriced_and_a_priced_one_is_not(self):
        got, _ = backlog.unpriced(snap(rec('AAA'), priced('BBB')), CMD)
        self.assertEqual([f.key for f in got], ['AAA'])
        self.assertIn('set AAA --priority', got[0].fix)

    def test_a_pull_with_no_branch_is_dead_and_a_failed_probe_is_a_note(self):
        s = snap(priced('AAA', state='pulled', pulled_at='2020-01-01', pulled_by='gone'))
        ctx = mock.Mock(root='r', remote='origin')
        got, notes = backlog.dead_pulls(s, ctx, CMD, split=lambda *a: ([], [s.by_id['AAA']], None))
        self.assertEqual([f.key for f in got], ['AAA'])
        got, notes = backlog.dead_pulls(s, ctx, CMD, split=lambda *a: ([], [], 'git failed'))
        self.assertEqual((got, notes), ([], ['dead pulls NOT CHECKED: git failed']))

    def test_audit_runs_seven_days_unfetched_and_not_checked_is_a_note(self):
        calls = []

        def run(root, days, fetch, home, now):
            calls.append((days, fetch))
            return audit_check.FINDINGS, ['UNAUDITED abc1234 set FOO', 'covered x']
        got, _ = backlog.audit('r', CMD, run=run)
        self.assertEqual(calls, [(7, False)])
        self.assertEqual([f.key for f in got], ['UNAUDITED abc1234 set FOO'])
        got, notes = backlog.audit('r', CMD, run=lambda *a, **k: (audit_check.NOT_CHECKED,
                                                                  ['NOT CHECKED: no pm_data']))
        self.assertEqual((got, notes), ([], ['audit NOT CHECKED: no pm_data']))


class ScopeChecks(unittest.TestCase):

    def setUp(self):
        self.root = temp_dir(self)
        os.makedirs(os.path.join(self.root, 'here'))

    def test_a_scope_path_history_held_and_the_checkout_lacks_is_a_finding(self):
        s = snap(rec('AAA', scope='gone/**'), rec('BBB', scope='here/**'),
                 rec('CCC', scope='planned/new.py'), rec('DDD', scope='none'))
        asked = []

        def git(root, rev, path):
            asked.append(path)
            return ('abc1234', None) if path == 'gone' else ('', None)
        got, notes = verify.scopes(s, self.root, 'main', CMD, git=git)
        self.assertEqual([(f.check, f.key) for f in got], [('scope-path-gone', 'AAA gone/**')])
        self.assertEqual(sorted(asked), ['gone', 'planned/new.py'])
        self.assertEqual(notes, [])

    def test_a_git_failure_is_a_note_not_a_finding(self):
        got, notes = verify.scopes(snap(rec('AAA', scope='gone/**')), self.root, 'main', CMD,
                                   git=lambda *a: (None, 'git failed'))
        self.assertEqual(got, [])
        self.assertEqual(notes, ['scope checks: 1 path(s) NOT CHECKED (git failed)'])

    def test_the_path_sweep_is_given_no_history_so_it_computes_no_age(self):
        seen = []

        def sweep(s, touched, today, days, root):
            seen.append(touched)
            return mock.Mock(bad_paths=[('AAA', 'a/b.py')], moved=[], unchecked=[],
                             files_skipped=0)
        got, _ = verify.paths(snap(rec('AAA')), self.root, CMD, '2026-10-22', sweep=sweep)
        self.assertEqual(seen, [{}])
        self.assertEqual([f.check for f in got], ['body-path-gone'])


class WorktreeChecks(unittest.TestCase):

    def test_reported_classes_and_a_landed_tree_still_holding_a_pull(self):
        rows = [{'class': 'L', 'path': 'w/live', 'branch': 'b1', 'claims': []},
                {'class': 'A', 'path': 'w/done', 'branch': 'b2', 'claims': ['AAA']},
                {'class': 'O', 'path': 'w/orphan', 'branch': None, 'claims': 'unknown: x'}]
        res = mock.Mock(data={'trees': rows}, lines=[])
        with mock.patch.object(worktrees.remove, 'open_ctx', return_value=(None, None)):
            got, _ = worktrees.findings('top', classify=lambda top: res)
        self.assertEqual([(f.check, f.key) for f in got],
                         [('worktree-A', 'w/done'), ('landed-still-pulled', 'AAA'),
                          ('worktree-O', 'w/orphan')])
        self.assertIn('release AAA --reason "landed"', got[1].fix)


class LedgerCounts(unittest.TestCase):

    def ledger(self):
        sup = entry(3).replace('Supersedes: -', 'Supersedes: %s, %s' % (rid(1), rid(2)))
        return '# Rulings ledger\n\n' + entry(1) + entry(2, retired=True) + sup

    def test_counts_only_and_no_entry_id_or_text_leaves_the_module(self):
        folder = temp_dir(self)
        path = os.path.join(folder, 'ledger.md')
        with open(path, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(self.ledger())
        with open(os.path.join(folder, 'STANDING.md'), 'w', encoding='utf-8', newline='\n') as fh:
            fh.write('# Standing rulings\n\n- %s: an old rule.\n- %s: a live rule.\n'
                     % (rid(2), rid(3)))
        got, notes = ledger.counts(user_config=lambda home: {'ledger_path': path})
        self.assertEqual(got, {'stale_cites': 1, 'superseded_live': 1, 'superseded': 2})
        self.assertEqual(notes, [])
        res = {'repo': 'x', 'counts': {}, 'ledger': got, 'notes': notes, 'findings': []}
        for text in (json.dumps(got), page.render(res)):
            self.assertNotIn('R-', text)
            self.assertNotIn('Rule ', text)

    def test_no_ledger_named_is_not_checked(self):
        self.assertEqual(ledger.counts(user_config=lambda home: {}),
                         (None, ['ledger NOT CHECKED: the machine config names no ledger']))


class PageAndResult(unittest.TestCase):

    def result(self, findings):
        rows = [dict(f._asdict(), fingerprint=maintlint.fingerprint(f), new=True)
                for f in findings]
        return {'version': 1, 'repo': 'demo', 'started': '2026-10-22T08:00:00Z', 'elapsed_s': 1.5,
                'counts': {'duplicate': len(rows)}, 'ledger': None, 'notes': ['a <b> note'],
                'findings': rows}

    def test_the_page_passes_pagecheck_and_escapes_record_text(self):
        bad = Finding('duplicate', 'AAA BBB', '<script>x</script> JavaScript: http-equiv & "q"',
                      'fix <img src=x onerror=y>')
        doc = page.render(self.result([bad]))
        self.assertIsNone(pagecheck.check(doc))
        self.assertNotIn('<script', doc.lower())
        self.assertNotIn('<img', doc)
        self.assertIn('&lt;script&gt;', doc)

    def test_a_page_too_big_for_pagecheck_shows_fewer_rows(self):
        many = [Finding('duplicate', str(i), 'x' * 3000, 'fix') for i in range(400)]
        doc = page.render(self.result(many))
        self.assertIsNone(pagecheck.check(doc))
        self.assertIn('more not shown', doc)

    def test_result_round_trip_and_a_finding_seen_last_run_is_not_new(self):
        f = Finding('duplicate', 'AAA BBB', 'same', 'fix')
        path = os.path.join(temp_dir(self), 'sub', result.NAME)
        result.write(path, self.result([f]))
        back = result.read(path)
        self.assertEqual(result.previous_fingerprints(back), {maintlint.fingerprint(f)})
        self.assertIsNone(result.read(path + '.missing'))


class WholeRun(StoreCase):
    """maintlint.run on a real ref store under a temp home (no ledger, no pm_data: both notes)."""

    def run_lint(self, previous=None):
        return maintlint.run(self.repo.root, home=self.home, previous=previous)

    def test_a_record_that_is_merely_old_gives_no_finding(self):
        kit_json_ref(self.repo)
        with at('2020-01-01'):
            self.seed(priced('OLD', summary='an old open item nobody touched'),
                      priced('PULLED', state='pulled', pulled_at='2020-01-02',
                             pulled_by=BRANCH, summary='an old pull whose branch is live'),
                      rec('WAITS', state='blocked', blocked_on='live-client',
                          summary='an old hold on a live client'))
        res = self.run_lint()
        self.assertEqual(res['findings'], [], res['notes'])
        self.assertFalse(any(n.startswith('backlog NOT CHECKED') for n in res['notes']),
                         res['notes'])

    def test_evidence_is_flagged_and_marked_new_only_the_first_time(self):
        kit_json_ref(self.repo)
        self.seed(priced('AAA', state='blocked', blocked_on='item:GONE', summary='one'),
                  priced('BBB', summary='the same words'), priced('CCC', summary='The same words.'))
        first = self.run_lint()
        self.assertEqual(sorted(f['check'] for f in first['findings']),
                         ['dead-blocker', 'duplicate'])
        self.assertFalse(any(f['new'] for f in first['findings']))     # no previous run
        again = self.run_lint(previous=first)
        self.assertFalse(any(f['new'] for f in again['findings']))
        self.assertEqual(again['counts']['duplicate'], 1)


class Cli(StoreCase):

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = maint_lint.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_dry_run_prints_each_fix_and_writes_nothing(self):
        kit_json_ref(self.repo)
        self.seed(priced('AAA', state='blocked', blocked_on='item:GONE'))
        folder = temp_dir(self)
        res = os.path.join(folder, 'result.json')
        code, out, _ = self.main('-C', self.repo.root, '--dry-run', '--result', res)
        self.assertEqual(code, 1)
        self.assertIn('dead-blocker: AAA is blocked on item:GONE', out)
        self.assertIn('    fix: ', out)
        self.assertEqual(os.listdir(folder), [])

    def test_a_clean_run_writes_the_result_and_the_page_and_exits_0(self):
        kit_json_ref(self.repo)
        self.seed(priced('AAA'))
        folder = temp_dir(self)
        res = os.path.join(folder, 'result.json')
        code, out, _ = self.main('-C', self.repo.root, '--result', res)
        self.assertEqual(code, 0, out)
        self.assertEqual(sorted(os.listdir(folder)), ['page.html', 'result.json'])
        with open(os.path.join(folder, 'page.html'), encoding='utf-8') as fh:
            self.assertIsNone(pagecheck.check(fh.read()))

    def test_usage_errors_exit_2(self):
        self.assertEqual(self.main('--bogus')[0], 2)
        self.assertEqual(self.main('-C', os.path.join(self.repo.root, 'nope'))[0], 2)


if __name__ == '__main__':
    unittest.main()
