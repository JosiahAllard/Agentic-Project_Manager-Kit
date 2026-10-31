"""kitlib.maintlint and tools/maint_lint.py: the checks on in-memory snapshots, the whole run on a
real store (a record that is merely old gives no finding), the ledger as counts only, the result
file, and the page that kitlib.hubsite.pagecheck passes."""
import datetime
import io
import json
import os
import re
import tempfile
import unittest
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from unittest import mock

from kitlib import maintlint
from kitlib.backlog import audit_check
from kitlib.backlog import record as rmod
from kitlib.backlog import store as smod
from kitlib.hubsite import pagecheck
from kitlib.maintlint import Finding, backlog, ledger, page, result, verify, worktrees
from kitlib.modulemap import scopelog

from tests.backlog.helpers import BRANCH, StoreCase, kit_json_ref, priced, rec
from tests.backlog.test_stale import at
from tests.gitfixture import rmtree
from tests.home import result_drive
from tests.ledger.test_standing import entry, rid
from tests.pathfree import without_paths
from tools import maint_lint

CMD = 'python "kit/tools/backlog.py" -C "wt"'
KIT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


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

    def test_a_duplicate_target_is_the_sorted_ids_whatever_order_the_store_gives(self):
        s = snap(rec('ZZZ', summary='same'), rec('AAA', summary='same'))
        s.readable.sort(key=lambda r: r.stem, reverse=True)
        got, _ = backlog.duplicates(s, CMD)
        self.assertEqual([f.key for f in got], ['AAA ZZZ'])

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
            return audit_check.FINDINGS, ['AUDIT CHECK 7 days', 'NOTE: the log has a bad line',
                                          'UNAUDITED abc1234 set FOO --why "see private words"',
                                          'UNAUDITED def5678 close BAR', 'UNMAPPED x y',
                                          'UNVERIFIED abc1234 answer: x y (not written by z)',
                                          'FINDINGS 4']
        got, notes = backlog.audit('r', CMD, run=run)
        self.assertEqual(calls, [(7, False)])
        self.assertEqual([(f.key, f.text) for f in got],
                         [('UNAUDITED', '2 store change(s) in 7 days with no audit line'),
                          ('UNMAPPED', '1 audit line(s) in 7 days whose role the role map does'
                                       ' not list'),
                          ('UNVERIFIED', '1 answer line(s) in 7 days the hub did not file or'
                                         ' does not hold')])
        self.assertIn('backlog_audit', got[0].fix)
        self.assertEqual(notes, ['audit: the log has a bad line'])
        for f in got:
            for text in (f.key, f.text, f.fix):
                # the fix names the kit's own checkout, whose path may contain the word
                text = without_paths(text, KIT_ROOT)
                self.assertNotIn('private', text)
                self.assertNotIn('abc1234', text)
        got, notes = backlog.audit('r', CMD, run=lambda *a, **k: (audit_check.NOT_CHECKED,
                                                                  ['NOT CHECKED: no pm_data']))
        self.assertEqual((got, notes), ([], ['audit NOT CHECKED: no pm_data']))


QID = '-'.join(('Q', '901'))            # an invented question id, built at run time


class AnsweredQuestions(unittest.TestCase):

    def parked(self, stem, q):
        return priced(stem, state='parked', q=q, parked_at='2026-10-01')

    def test_a_parked_record_whose_question_is_answered_or_logged_is_a_finding(self):
        other = '-'.join(('Q', '902'))
        third = '-'.join(('Q', '903'))
        s = snap(self.parked('AAA', QID), self.parked('BBB', other), self.parked('CCC', third),
                 priced('DDD'))
        docs = [{'id': QID, 'status': 'answered'}, {'id': other, 'status': 'logged',
                                                     'ruling': 'EX-2001'},
                {'id': third, 'status': 'open'}]
        got, notes = backlog.answered(s, CMD, questions=lambda: docs)
        self.assertEqual(notes, [])
        self.assertEqual([(f.check, f.key) for f in got],
                         [('answered-question', 'AAA'), ('answered-question', 'BBB')])
        self.assertIn('questions log %s' % QID, got[0].fix)
        self.assertIn(CMD + ' unpark AAA, giving the ruling', got[0].fix)
        self.assertTrue(got[1].fix.startswith(CMD + ' unpark BBB, giving the ruling'))
        self.assertEqual(got[0].age, 'parked_at 2026-10-01')
        for f in got:
            self.assertNotIn('EX-2001', f.text + f.fix)

    def test_a_parked_record_whose_question_is_filed_is_a_finding_with_no_log_step(self):
        s = snap(self.parked('AAA', QID))
        docs = [{'id': QID, 'status': 'filed'}]
        got, notes = backlog.answered(s, CMD, questions=lambda: docs)
        self.assertEqual(notes, [])
        self.assertEqual([(f.check, f.key) for f in got], [('answered-question', 'AAA')])
        self.assertIn('(filed)', got[0].text)
        self.assertTrue(got[0].fix.startswith(CMD + ' unpark AAA, giving the ruling'), got[0].fix)

    def test_a_parked_record_whose_question_is_done_is_a_finding_with_no_ruling_to_cite(self):
        s = snap(self.parked('AAA', QID))
        docs = [{'id': QID, 'status': 'done', 'done_why': 'a one-off go'}]
        got, notes = backlog.answered(s, CMD, questions=lambda: docs)
        self.assertEqual(notes, [])
        self.assertEqual([(f.check, f.key) for f in got], [('answered-question', 'AAA')])
        self.assertIn('(done)', got[0].text)
        self.assertTrue(got[0].fix.startswith(CMD + ' unpark AAA; '), got[0].fix)
        self.assertIn('no ruling', got[0].fix)
        self.assertNotIn('questions log', got[0].fix)
        self.assertNotIn('a one-off go', got[0].text + got[0].fix)

    def test_a_hub_that_cannot_be_asked_is_a_not_checked_note(self):
        from kitlib.hubsite import serve

        def down():
            raise serve.NotRunning(4321)
        got, notes = backlog.answered(snap(self.parked('AAA', QID)), CMD, questions=down)
        self.assertEqual((got, notes), ([], ['answered questions NOT CHECKED: no hub server is'
                                             ' listening on port 4321']))

    def test_with_no_parked_record_the_hub_is_not_asked(self):
        def never():
            raise AssertionError('asked the hub')
        self.assertEqual(backlog.answered(snap(priced('AAA')), CMD, questions=never), ([], []))

    def test_the_hub_is_read_with_the_agent_token_and_every_status(self):
        from kitlib.hubsite import client
        seen = []

        class Fake(object):
            def __init__(self, port):
                seen.append(port)

            def listing(self, collection, status=None, repo=None, role=client.ANY):
                seen.append((collection, status, role))
                return []
        with mock.patch.object(client, 'Client', Fake), \
                mock.patch('kitlib.hubsite.settings.load', return_value=mock.Mock(port=4321)):
            self.assertEqual(backlog.hub_questions(), [])
        self.assertEqual(seen, [4321, ('questions', 'all', client.AGENT)])


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
        self.assertIn('which main held and the checkout no longer has', got[0].text)
        self.assertIsNone(re.search(r'\b[0-9a-f]{7,40}\b', got[0].text + got[0].fix))
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
    """Only what the nightly sweep (kitlib.worktree.sweep) leaves is reported."""

    def found(self, *rows):
        res = mock.Mock(data={'trees': list(rows)}, lines=[])
        with mock.patch.object(worktrees.remove, 'open_ctx', return_value=(None, None)):
            got, _ = worktrees.findings('top', classify=lambda top: res)
        return got

    def test_reported_classes_and_a_landed_tree_still_holding_a_pull(self):
        got = self.found({'class': 'L', 'path': 'w/live', 'branch': 'b1', 'claims': []},
                         {'class': 'A', 'path': 'w/done', 'branch': 'b2', 'claims': ['AAA'],
                          'idle_s': 3 * 86400},
                         {'class': 'O', 'path': 'w/orphan', 'branch': None,
                          'claims': 'unknown: x'},
                         {'class': 'E', 'path': 'w/ev', 'branch': 'b5', 'claims': []})
        self.assertEqual([(f.check, f.key) for f in got],
                         [('worktree-A', 'w/done'), ('landed-still-pulled', 'AAA'),
                          ('worktree-O', 'w/orphan'), ('worktree-E', 'w/ev')])
        self.assertIn('release AAA --reason "landed"', got[1].fix)
        self.assertEqual(got[0].age, 'idle 3d')

    def test_an_a_tree_no_record_claims_is_the_sweeps_to_remove(self):
        got = self.found({'class': 'A', 'path': 'w/a1', 'branch': 'b1', 'claims': []},
                         {'class': 'A', 'path': 'w/a2', 'branch': 'b2', 'claims': None},
                         {'class': 'A', 'path': 'w/a3', 'branch': 'b3',
                          'claims': 'unknown: store unreadable'})
        self.assertEqual(got, [])

    def test_a_b_or_c_tree_whose_stale_mark_holds_is_the_sweeps_to_abandon(self):
        mark = {'at': 't', 'why': 'w', 'by': 'pm', 'tip': 'abc', 'void': None}
        got = self.found({'class': 'B', 'path': 'w/b', 'branch': 'b1', 'claims': [],
                          'mark': mark},
                         {'class': 'C', 'path': 'w/c', 'branch': 'b2', 'claims': [],
                          'mark': dict(mark, void='')})
        self.assertEqual(got, [])

    def test_a_b_or_c_tree_with_a_void_mark_or_none_is_reported(self):
        mark = {'at': 't', 'why': 'w', 'by': 'pm', 'tip': 'abc', 'void': 'its tip moved'}
        got = self.found({'class': 'B', 'path': 'w/b', 'branch': 'b1', 'claims': [],
                          'mark': mark},
                         {'class': 'C', 'path': 'w/c', 'branch': 'b2', 'claims': []})
        self.assertEqual([f.check for f in got], ['worktree-B', 'worktree-C'])


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
        self.assertEqual(got, {'stale_cites': 1, 'rules': {}, 'over_budget': [],
                               'conflict_pairs': 0, 'overrides': 0})
        self.assertEqual(notes, [])
        res = {'repo': 'x', 'counts': {}, 'ledger': got, 'notes': notes, 'findings': []}
        for text in (json.dumps(got), page.render(res)):
            self.assertNotIn('R-', text)
            self.assertNotIn('Rule ', text)

    def test_no_ledger_named_is_not_checked(self):
        self.assertEqual(ledger.counts(user_config=lambda home: {}),
                         (None, ['ledger NOT CHECKED: the machine config names no ledger']))

    def test_stale_cites_above_0_is_a_finding_for_the_pm_with_numbers_only(self):
        got = ledger.findings({'stale_cites': 2, 'rules': {}, 'over_budget': [],
                               'conflict_pairs': 0, 'overrides': 0})
        self.assertEqual([(f.check, f.key) for f in got], [('ledger-stale-cites', 'stale_cites')])
        self.assertIn('2 citation(s)', got[0].text)
        self.assertIn('ledger.py" check', got[0].fix)
        self.assertIn('the PM', got[0].fix)
        self.assertNotIn('R-', got[0].text + got[0].fix)

    def test_no_count_above_0_and_an_unchecked_ledger_are_no_finding(self):
        self.assertEqual(ledger.findings({'stale_cites': 0, 'rules': {'kit': 4},
                                          'over_budget': [], 'conflict_pairs': 0,
                                          'overrides': 1}), [])
        self.assertEqual(ledger.findings(None), [])


Change = audit_check.Change


class LandingChecks(StoreCase):
    """verify.code_base, landed_pulls and closed_unlanded on a real repository: a branch has
    landed when its tip is in the pushed main and off its first-parent chain."""

    def side_commit(self, branch, name):
        self.repo.git('checkout', '-q', '-b', branch)
        self.repo.write(name, name + '\n')
        tip = self.repo.commit('work on ' + branch, name)
        self.repo.git('checkout', '-q', 'main')
        return tip

    def land(self, branch, push=True):
        self.repo.git('merge', '-q', '--no-ff', '-m', 'Merge ' + branch, branch)
        if push:
            self.repo.git('push', '-q', 'origin', 'main')
        return self.repo.head()

    def base(self):
        got, why = verify.code_base(self.repo.root, 'origin', 'main')
        self.assertIsNone(why)
        return got

    def pulled(self, stem, branch):
        return priced(stem, state='pulled', pulled_at='2026-10-01', pulled_by=branch)

    def test_a_pull_whose_branch_landed_is_a_finding_and_a_fresh_or_unpushed_one_is_not(self):
        self.side_commit('kit/done', 'a.txt')
        self.land('kit/done')
        self.side_commit('kit/local', 'b.txt')
        self.land('kit/local', push=False)        # merged on the local main only: not pushed
        self.side_commit('kit/open', 'c.txt')     # never merged
        s = snap(self.pulled('AAA', 'kit/done'), self.pulled('BBB', BRANCH),
                 self.pulled('CCC', 'kit/local'), self.pulled('DDD', 'kit/open'),
                 self.pulled('EEE', 'kit/no-such-branch'))
        base = self.base()
        self.assertEqual(base[0], 'refs/remotes/origin/main')
        got, notes = verify.landed_pulls(s, self.repo.root, 'origin', base, CMD)
        self.assertEqual(notes, [])
        self.assertEqual([(f.check, f.key) for f in got], [('landed-still-pulled', 'AAA')])
        self.assertIn('kit/done is in refs/remotes/origin/main', got[0].text)
        self.assertTrue(got[0].fix.startswith(CMD + ' close AAA --reason "landed <merge>"'))
        self.assertIn('--as kit/done release AAA', got[0].fix)

    def test_a_branch_at_a_main_tip_or_merged_by_fast_forward_has_not_landed(self):
        self.repo.git('branch', '-q', 'kit/fresh')                  # at the main tip
        self.side_commit('kit/ff', 'f.txt')
        self.repo.git('merge', '-q', '--ff-only', 'kit/ff')         # on the first-parent chain
        self.repo.git('push', '-q', 'origin', 'main')
        self.repo.git('branch', '-q', 'kit/fresh-after')            # at the new main tip
        s = snap(self.pulled('AAA', 'kit/fresh'), self.pulled('BBB', 'kit/ff'),
                 self.pulled('CCC', 'kit/fresh-after'))
        self.assertEqual(verify.landed_pulls(s, self.repo.root, 'origin', self.base(), CMD),
                         ([], []))

    def test_a_branch_known_only_on_the_remote_is_read_there(self):
        self.side_commit('kit/done', 'a.txt')
        self.land('kit/done')
        self.repo.git('push', '-q', 'origin', 'kit/done')
        self.repo.git('fetch', '-q', 'origin')
        self.repo.git('branch', '-q', '-D', 'kit/done')
        got, _ = verify.landed_pulls(snap(self.pulled('AAA', 'kit/done')), self.repo.root,
                                     'origin', self.base(), CMD)
        self.assertEqual([f.key for f in got], ['AAA'])

    def test_a_branch_listing_git_cannot_give_is_a_note(self):
        got, notes = verify.landed_pulls(snap(self.pulled('AAA', 'kit/x')), self.repo.root,
                                         'origin', self.base(), CMD,
                                         tips=lambda root, remote: (None, 'git failed'))
        self.assertEqual((got, notes), ([], ['landed pulls NOT CHECKED: git failed']))

    def test_with_no_main_to_measure_against_code_base_says_why(self):
        got, why = verify.code_base(self.repo.root, 'origin', 'no-such-main')
        self.assertIsNone(got)
        self.assertIn('no-such-main', why)

    def close(self, rid, sha, epoch=0):
        return Change('f' * 40, epoch, 'closes %s: landed %s' % (rid, sha), '', [])

    def test_a_close_as_landed_by_a_commit_the_base_lacks_is_a_finding(self):
        unlanded = self.side_commit('kit/open', 'c.txt')
        self.side_commit('kit/done', 'a.txt')
        merge = self.land('kit/done')
        changes = [self.close('AAA', unlanded), self.close('BBB', merge),
                   self.close('CCC', 'e' * 40),                     # no such commit here
                   self.close('DDD', unlanded),                     # back in the store: reopened
                   self.close('FFF', merge), self.close('FFF', unlanded),  # newest first: landed
                   Change('f' * 40, 0, 'closes GGG: the work is finished', '', [])]
        got, notes = verify.closed_unlanded(changes, snap(priced('DDD')), self.repo.root,
                                            self.base(), CMD)
        self.assertEqual([(f.check, f.key) for f in got], [('closed-unlanded', 'AAA')])
        self.assertIn('closed as landed by %s, which refs/remotes/origin/main does not hold'
                      % unlanded[:12], got[0].text)
        self.assertTrue(got[0].fix.startswith(CMD + ' add AAA --reopen --summary'))
        self.assertEqual(notes, ['closed-unlanded: 1 close(s) name a commit this checkout does'
                                 ' not have (another repository\'s, or not fetched): NOT CHECKED'])

    def test_a_landed_close_followed_by_another_close_a_retire_or_a_delete_is_not_judged(self):
        unlanded = self.side_commit('kit/open', 'c.txt')
        ended = [Change('f' * 40, 0, '%s %s: %s' % (verb, rid, why), '', [])
                 for verb, rid, why in (('closes', 'AAA', 'the work is finished'),
                                        ('retires', 'BBB', 'not wanted'),
                                        ('deletes', 'CCC', 'filed by mistake'))]
        older = [self.close(rid, unlanded) for rid in ('AAA', 'BBB', 'CCC', 'DDD')]
        got, notes = verify.closed_unlanded(ended + older, snap(), self.repo.root, self.base(),
                                            CMD)
        self.assertEqual(([(f.check, f.key) for f in got], notes),
                         ([('closed-unlanded', 'DDD')], []))

    def test_with_no_close_to_check_git_is_not_asked(self):
        def never(*a, **k):
            raise AssertionError('asked git')
        merge = self.side_commit('kit/done', 'a.txt') and self.land('kit/done')
        self.assertEqual(verify.closed_unlanded([self.close('AAA', merge)], snap(),
                                                self.repo.root, self.base(), CMD,
                                                commits=never), ([], []))


class PageAndResult(unittest.TestCase):

    def result(self, findings):
        rows = [dict(f._asdict(), fingerprint=maintlint.fingerprint('demo', f), new=True)
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

    def test_notes_are_capped_like_the_rows(self):
        res = self.result([])
        res['notes'] = ['note %d %s' % (i, 'y' * 3000) for i in range(400)]
        doc = page.render(res)
        self.assertIsNone(pagecheck.check(doc))
        self.assertIn('<h2>Notes (400)</h2>', doc)
        self.assertIn('more not shown', doc)

    def test_a_lone_surrogate_is_dropped_and_the_page_still_passes(self):
        bad = Finding('body-path-gone', 'AAA x', 'AAA names a/\udcff b.py', 'fix \ud800')
        res = self.result([bad])
        res['notes'] = ['note \udc80 here']
        doc = page.render(res)
        doc.encode('utf-8')
        self.assertIsNone(pagecheck.check(doc))
        self.assertIn('a/ b.py', doc)
        self.assertIn('note  here', doc)

    def test_the_module_scope_section_shows_the_sum_and_its_routes_escaped(self):
        res = self.result([])
        res['module_scope'] = {
            'days': 7, 'runs': 4, 'heads': 3, 'widened': 3, 'widened_heads': 2,
            'routes': [{'from': 'node', 'why': 'via edge build: <b>tests</b>',
                        'modules': ['brain', 'worker'], 'runs': 2}]}
        doc = page.render(res)
        self.assertIsNone(pagecheck.check(doc))
        self.assertIn('<h2>Module scope</h2>', doc)
        self.assertIn('4 gate runs in 7 days kept a scope record (3 heads); 3 widened beyond the '
                      'changed modules (75%).', doc)
        self.assertIn('<tr><td>node</td><td>brain, worker</td>'
                      '<td>via edge build: &lt;b&gt;tests&lt;/b&gt;</td><td>2</td></tr>', doc)
        res['module_scope'] = None
        self.assertNotIn('Module scope', page.render(res))

    def test_the_fingerprint_is_repo_check_and_target_never_the_text(self):
        f = Finding('scope-path-gone', 'AAA gone/**', 'AAA scope gone/** is gone', 'x')
        reworded = f._replace(text='AAA: gone/** was removed', fix='y', age='idle 9d')
        self.assertEqual(maintlint.fingerprint('demo', f), maintlint.fingerprint('demo', reworded))
        self.assertNotEqual(maintlint.fingerprint('demo', f), maintlint.fingerprint('other', f))
        self.assertNotEqual(maintlint.fingerprint('demo', f),
                            maintlint.fingerprint('demo', f._replace(key='BBB gone/**')))

    def test_the_default_result_folder_is_under_pm_data_beside_the_audit_folder(self):
        with mock.patch.object(result.audit, 'pm_data_path', return_value=('Z:/pmd', None)):
            self.assertEqual(result.default_dir('demo'),
                             (os.path.join('Z:/pmd', 'maint-lint', 'demo'), None))
        with mock.patch.object(result.audit, 'pm_data_path', return_value=(None, 'bad config')):
            self.assertEqual(result.default_dir('demo'), (None, 'bad config'))
        with mock.patch.object(result.audit, 'pm_data_path', return_value=(None, None)):
            self.assertIsNone(result.default_dir('demo')[0])

    def test_result_round_trip_and_a_finding_seen_last_run_is_not_new(self):
        f = Finding('duplicate', 'AAA BBB', 'same', 'fix')
        path = os.path.join(temp_dir(self), 'sub', result.NAME)
        result.write(path, self.result([f]))
        back = result.read(path)
        self.assertEqual(result.previous_fingerprints(back), {maintlint.fingerprint('demo', f)})
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

    def _lint_with_unreadable_resources(self, *records):
        kit_json_ref(self.repo)
        self.seed(*records)
        broken = mock.PropertyMock(side_effect=maintlint.Refusal('resources config is broken'))
        with mock.patch.object(type(maintlint.open_store(self.repo.root, fetch=False)), 'resources',
                               broken):
            return self.run_lint()

    def test_the_uses_hint_needs_a_uses_line_in_the_store(self):
        kit_json_ref(self.repo, {'backlog': {'resources': {'gpu': 1}}})
        self.seed(priced('AAA', summary='needs a live client'))
        res = self.run_lint()
        fixes = [f['fix'] for f in res['findings'] if f['check'] == 'keyword-hold']
        self.assertEqual(len(fixes), 1, res)
        self.assertNotIn('--uses', fixes[0])

    def test_no_uses_line_never_reads_the_resources_config(self):
        res = self._lint_with_unreadable_resources(priced('AAA'))
        self.assertFalse(any('backlog resources not read' in n for n in res['notes']), res['notes'])

    def test_a_uses_line_with_an_unreadable_resources_config_gives_a_note(self):
        res = self._lint_with_unreadable_resources(priced('AAA', uses='dev-clone'))
        self.assertTrue(any('backlog resources not read: resources config is broken' in n
                            for n in res['notes']), res['notes'])

    def test_in_a_pm_data_worktree_paths_are_read_in_the_code_checkout(self):
        kit_json_ref(self.repo)
        self.seed(priced('AAA'))
        code = temp_dir(self)
        seen = []
        with mock.patch.object(maintlint.pullmod, 'code_config', return_value=mock.Mock(root=code)), \
                mock.patch.object(maintlint.verify, 'paths',
                                  side_effect=lambda s, root, *a: (seen.append(root), ([], []))[1]), \
                mock.patch.object(maintlint.verify, 'scopes',
                                  side_effect=lambda s, root, rev, cmd: (seen.append(root),
                                                                         ([], []))[1]):
            self.run_lint()
        self.assertEqual(seen, [code, code])
        self.assertEqual(maintlint.code_checkout(self.repo.root), self.repo.root)

    def test_evidence_is_flagged_and_new_marks_what_the_last_run_lacked(self):
        kit_json_ref(self.repo)
        self.seed(priced('AAA', state='blocked', blocked_on='item:GONE', summary='one'),
                  priced('BBB', summary='the same words'), priced('CCC', summary='The same words.'))
        first = self.run_lint()
        self.assertEqual(sorted(f['check'] for f in first['findings']),
                         ['dead-blocker', 'duplicate'])
        self.assertFalse(any(f['new'] for f in first['findings']))     # no previous run
        repo = maintlint.repo_name(self.repo.root)
        self.assertEqual(first['repo'], repo)
        dup_row = [f for f in first['findings'] if f['check'] == 'duplicate'][0]
        self.assertEqual((dup_row['repo'], dup_row['target']), (repo, 'BBB CCC'))
        self.assertIn('age', dup_row)
        again = self.run_lint(previous=first)
        self.assertFalse(any(f['new'] for f in again['findings']))
        self.assertEqual(again['counts']['duplicate'], 1)
        dup = [f for f in first['findings'] if f['check'] == 'duplicate']
        third = self.run_lint(previous={'findings': dup})
        self.assertEqual(sorted((f['check'], f['new']) for f in third['findings']),
                         [('dead-blocker', True), ('duplicate', False)])


    def test_with_no_module_map_there_is_no_module_scope_and_no_note_of_it(self):
        kit_json_ref(self.repo)
        res = self.run_lint()
        self.assertIsNone(res['module_scope'])
        self.assertFalse(any('module scope' in n for n in res['notes']), res['notes'])

    def test_with_a_module_map_the_scope_records_are_summed(self):
        kit_json_ref(self.repo, {'modules': {'map': 'docs/module-map.json'}})
        result_drive(self)
        where, why = scopelog.folder(self.repo.root)
        self.assertIsNone(why)
        os.makedirs(where)
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime(scopelog.STAMP)
        with open(os.path.join(where, '%s-1-abcd.json' % stamp), 'w', encoding='utf-8') as fh:
            json.dump({'schema': 1, 'head': 'h1', 'widen': [
                {'module': 'brain', 'from': 'node', 'why': 'via messaging'}]}, fh)
        res = self.run_lint()
        self.assertEqual(res['module_scope']['runs'], 1, res['notes'])
        self.assertEqual(res['module_scope']['routes'],
                         [{'from': 'node', 'why': 'via messaging', 'modules': ['brain'],
                           'runs': 1}])
        self.assertEqual(res['findings'], [], res['notes'])            # counts only, no finding
        self.assertIn('module scope: 1 gate run(s) in 7 days (1 head(s)), 1 widened beyond the '
                      'changed modules (100%)', maint_lint._counts(res))

    def test_a_landed_pull_and_a_close_of_unlanded_work_are_flagged_once_each(self):
        kit_json_ref(self.repo)
        self.repo.git('push', '-q', 'origin', 'main')
        self.repo.git('checkout', '-q', '-b', 'kit/done')
        self.repo.write('a.txt', 'a\n')
        self.repo.commit('work', 'a.txt')
        self.repo.git('checkout', '-q', '-b', 'kit/open')
        self.repo.write('b.txt', 'b\n')
        unlanded = self.repo.commit('more work', 'b.txt')
        self.repo.git('checkout', '-q', 'main')
        self.repo.git('merge', '-q', '--no-ff', '-m', 'Merge kit/done', 'kit/done')
        self.repo.git('push', '-q', 'origin', 'main')
        self.seed(priced('AAA', state='pulled', pulled_at='2026-10-01', pulled_by='kit/done'),
                  priced('BBB', summary='work that never landed'))
        self.verb('close', 'BBB', 'landed %s' % unlanded)
        # The A worktree list names AAA too: one finding, not two.
        claim = Finding('landed-still-pulled', 'AAA', 'from the worktree list', 'fix')
        with mock.patch.object(maintlint.worktrees, 'findings', return_value=([claim], [])):
            res = self.run_lint()
        got = sorted((f['check'], f['target']) for f in res['findings'])
        self.assertEqual(got, [('closed-unlanded', 'BBB'), ('landed-still-pulled', 'AAA')],
                         res['notes'])
        self.assertEqual(res['counts']['landed-still-pulled'], 1)
        self.assertEqual(res['counts']['closed-unlanded'], 1)


class Cli(StoreCase):

    def main(self, *argv, **kw):
        out, err = io.StringIO(), io.StringIO()
        # A plain run books the machine lock (kitlib.maintlint.slot) and publishes on the hub
        # (kitlib.maintlint.publish): both stubbed here, never booked and never sent.
        sent = kw.get('publish') or mock.Mock(return_value=(None, []))
        with redirect_stdout(out), redirect_stderr(err), \
                mock.patch.object(maint_lint.slot, 'take',
                                  return_value=kw.get('held') or nullcontext()), \
                mock.patch.object(maint_lint.publish, 'publish', sent):
            code = maint_lint.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_a_plain_run_publishes_the_page_it_wrote_and_prints_the_line(self):
        kit_json_ref(self.repo)
        self.seed(priced('AAA', state='blocked', blocked_on='item:GONE'))
        folder = temp_dir(self)
        sent = mock.Mock(return_value=('PUBLISHED maint-lint-x v3', []))
        code, out, _ = self.main('-C', self.repo.root, '--result',
                                 os.path.join(folder, 'result.json'), publish=sent)
        self.assertEqual(code, 1, out)
        sent.assert_called_once()
        res, doc = sent.call_args[0]
        self.assertEqual(res['repo'], maintlint.repo_name(self.repo.root))
        with open(os.path.join(folder, 'page.html'), encoding='utf-8') as fh:
            self.assertEqual(doc, fh.read())
        self.assertIn('PUBLISHED maint-lint-x v3\n', out)

    def test_a_plain_run_publishes_only_after_the_slot_is_given_back(self):
        kit_json_ref(self.repo)
        self.seed(priced('AAA'))
        folder = temp_dir(self)
        state = {'held': False, 'entered': 0}
        seen = []

        class Held(object):
            def __enter__(self):
                state['held'] = True
                state['entered'] += 1

            def __exit__(self, *exc):
                state['held'] = False

        def sent(res, doc):
            seen.append(state['held'])
            return None, []
        code, out, _ = self.main('-C', self.repo.root, '--result',
                                 os.path.join(folder, 'result.json'), publish=sent, held=Held())
        self.assertEqual(code, 0, out)
        self.assertEqual((state['entered'], seen), (1, [False]))

    def test_dry_run_never_publishes(self):
        kit_json_ref(self.repo)
        self.seed(priced('AAA'))
        folder = temp_dir(self)
        sent = mock.Mock(side_effect=AssertionError('published'))
        code, out, _ = self.main('-C', self.repo.root, '--dry-run', '--result',
                                 os.path.join(folder, 'result.json'),
                                 '--out', os.path.join(folder, 'page.html'), publish=sent)
        self.assertEqual(code, 0, out)
        sent.assert_not_called()
        self.assertNotIn('PUBLISHED', out)

    def test_a_hub_that_cannot_take_the_page_is_a_note_and_keeps_the_exit_code(self):
        kit_json_ref(self.repo)
        folder = temp_dir(self)
        for seed, want in ((priced('AAA'), 0),
                           (priced('BBB', state='blocked', blocked_on='item:GONE'), 1)):
            self.seed(seed)
            note = 'hub page NOT CHECKED: no hub server is listening on port 4321'
            sent = mock.Mock(return_value=(None, [note]))
            code, out, err = self.main('-C', self.repo.root, '--result',
                                       os.path.join(folder, 'result.json'), publish=sent)
            self.assertEqual(code, want, out + err)
            self.assertIn('note: %s\n' % note, out)
            self.assertNotIn('PUBLISHED', out)
            with open(os.path.join(folder, 'result.json'), encoding='utf-8') as fh:
                self.assertNotIn(note, fh.read())

    def test_dry_run_writes_only_the_page_to_out_and_prints_the_counts(self):
        kit_json_ref(self.repo)
        self.seed(priced('AAA', state='blocked', blocked_on='item:GONE'))
        folder = temp_dir(self)
        res = os.path.join(folder, 'result.json')
        out_page = os.path.join(folder, 'pages', 'lint.html')
        code, out, _ = self.main('-C', self.repo.root, '--dry-run', '--result', res,
                                 '--out', out_page)
        self.assertEqual(code, 1)
        self.assertEqual(os.listdir(folder), ['pages'])
        self.assertEqual(os.listdir(os.path.join(folder, 'pages')), ['lint.html'])
        with open(out_page, encoding='utf-8') as fh:
            doc = fh.read()
        self.assertIsNone(pagecheck.check(doc))
        self.assertIn('AAA is blocked on item:GONE', doc)
        self.assertIn('page: %s' % os.path.abspath(out_page), out)
        self.assertIn('dead-blocker', out)
        self.assertNotIn('result: ', out)

    def test_dry_run_without_out_is_usage(self):
        kit_json_ref(self.repo)
        folder = temp_dir(self)
        code, _, err = self.main('-C', self.repo.root, '--dry-run', '--result',
                                 os.path.join(folder, 'result.json'))
        self.assertEqual(code, 2)
        self.assertIn('--out', err)
        self.assertEqual(os.listdir(folder), [])

    def test_no_result_and_no_pm_data_is_usage(self):
        kit_json_ref(self.repo)
        with mock.patch.object(result.audit, 'pm_data_path', return_value=(None, None)):
            code, _, err = self.main('-C', self.repo.root)
        self.assertEqual(code, 2)
        self.assertIn('pm_data', err)

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
