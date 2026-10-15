"""kitlib.ledger.fmt and kitlib.ledger.write: the rulings-ledger writer (add, retire, check).

Ids are built by rid(n), so this file holds no ruling id of its own (tests/scrub)."""
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from kitlib import ledger
from kitlib.backlog import ledger as backlog_ledger
from kitlib.ledger import fmt, write

WHEN = '2026-10-06 08:00Z'
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HOLD = 0.4          # seconds a racing child sleeps between its read of the ledger and its write
START_WAIT = 30.0   # seconds the children get to start (Python under a loaded gate)
RACE_TIMEOUT = 60.0  # the hard bound on a child; past it the test kills the pids it started

# A racing child: write_atomic sleeps HOLD first, so two children that read the ledger together
# both read it before either writes. Each says it is ready, waits for the go file, then adds one
# entry (mode `add`) or, with the vcs seam, prepares, adds and commits it under the ledger lock
# (mode `commit`). It prints the id it was given.
RACE_CHILD = r'''
import contextlib, os, sys, time
sys.path.insert(0, sys.argv[1])
from kitlib.ledger import vcs, write
mode, path, ready, go, decides, hold = sys.argv[2:8]
real = write.write_atomic
def slow(p, text):
    time.sleep(float(hold))
    real(p, text)
write.write_atomic = slow
open(ready, 'w').close()
deadline = time.monotonic() + 30
while not os.path.exists(go):
    if time.monotonic() > deadline:
        sys.exit('no go file')
    time.sleep(0.01)
args = dict(repo='alpha', prov='selected', q='q', a='a', decides=decides, source='s',
            when='2026-10-06 08:00Z')
if mode == 'add':
    print(write.add(path, **args).id)
else:
    # Without write.locked (the code before the lock) the same steps run unlocked: the race.
    with getattr(write, 'locked', lambda p: contextlib.nullcontext())(path):
        plan = vcs.prepare(path, model='Test Model 1')
        got = write.add(path, **args)
        print(got.id)
        vcs.finish(plan, got.id, None, decides, push=False)
'''

# A child that holds the ledger lock until it is killed (or the stop file appears).
HOLDER_CHILD = r'''
import os, sys, time
sys.path.insert(0, sys.argv[1])
from kitlib.ledger import write
path, ready, stop = sys.argv[2:5]
with write.locked(path):
    open(ready, 'w').close()
    deadline = time.monotonic() + 60
    while not os.path.exists(stop) and time.monotonic() < deadline:
        time.sleep(0.02)
'''


def rid(n):
    return 'R-%04d' % n


def start_child(test, code, args, env=None):
    """Start `python -c code ROOT args...`; the test's cleanup kills it (by its own pid handle)
    if it is still running."""
    proc = subprocess.Popen([sys.executable, '-c', code, ROOT] + list(args), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def stop():
        if proc.poll() is None:
            proc.kill()
        proc.communicate(timeout=RACE_TIMEOUT)
    test.addCleanup(stop)
    return proc


def wait_for(path, procs, wait=START_WAIT):
    deadline = time.monotonic() + wait
    while not os.path.exists(path):
        for p in procs:
            if p.poll() is not None:
                raise AssertionError('a child exited before it was ready: %s' % (p.communicate(),))
        if time.monotonic() > deadline:
            raise AssertionError('a child was not ready within %s s' % wait)
        time.sleep(0.01)


def race(test, folder, mode, path, names, env=None):
    """Start one RACE_CHILD per name in `names`, let them go together, and return each one's
    (returncode, stdout, stderr). Children past RACE_TIMEOUT are killed and fail the test."""
    procs, readies = [], []
    for name in names:
        ready = os.path.join(folder, 'ready-' + name)
        readies.append(ready)
        procs.append(start_child(test, RACE_CHILD, [mode, path, ready, os.path.join(folder, 'go'),
                                                    name, str(HOLD)], env=env))
    for ready in readies:
        wait_for(ready, procs)
    open(os.path.join(folder, 'go'), 'w').close()
    out = []
    for p in procs:
        try:
            o, e = p.communicate(timeout=RACE_TIMEOUT)
        except subprocess.TimeoutExpired:
            for q in procs:
                if q.poll() is None:
                    q.kill()
            raise AssertionError('a racing child ran past %s s' % RACE_TIMEOUT)
        out.append((p.returncode, o.strip(), e))
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='kit-ledger-')
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, 'rulings', 'LEDGER.md')

    def add(self, repos=None, **kw):
        args = dict(repo='alpha', prov='selected', q='Which?', a='This one', decides='Use this one.',
                    source='PM session chat', when=WHEN)
        args.update(kw)
        return write.add(self.path, repos=repos, **args)

    def text(self):
        with open(self.path, encoding='utf-8', newline='') as f:
            return f.read()


class AddTest(Base):
    def test_first_add_starts_from_the_template_and_numbers_from_one(self):
        got = self.add()
        self.assertEqual(got.id, rid(1))
        text = self.text()
        self.assertTrue(text.startswith(fmt.template_text().rstrip('\n')))
        self.assertIn('## %s | %s | alpha | selected\nQ: Which?\nA: This one\nDecides: Use this one.\n'
                      'Source: PM session chat\nSupersedes: -\n' % (rid(1), WHEN), text)
        self.assertNotIn('\r', text)
        self.assertEqual(fmt.validate(text), [])

    def test_ids_run_on_from_the_last(self):
        self.add()
        self.add(decides='Second.')
        self.assertEqual(self.add(decides='Third.').id, rid(3))
        self.assertEqual([i for i, _ in fmt.split_entries(self.text())[1]], [rid(1), rid(2), rid(3)])

    def test_empty_fields_are_usage_errors_and_write_nothing(self):
        for field in ('q', 'a', 'decides', 'source'):
            with self.assertRaises(write.Usage, msg=field):
                self.add(**{field: '  '})
        self.assertFalse(os.path.exists(self.path))

    def test_repo_column_comes_from_the_configured_list_plus_all(self):
        self.add(repos=['alpha', 'beta'], repo='beta')
        self.add(repos=['alpha', 'beta'], repo='all')
        with self.assertRaises(write.Usage):
            self.add(repos=['alpha', 'beta'], repo='gamma')
        self.add(repos=None, repo='gamma')          # unset: any well-formed name
        for bad in ('Alpha', 'a b', 'x|y', ''):
            with self.assertRaises(write.Usage, msg=bad):
                self.add(repo=bad)

    def test_unknown_provenance_and_bad_when_are_usage_errors(self):
        with self.assertRaises(write.Usage):
            self.add(prov='mystery')
        for bad in ('2026-10-06', '2026-10-06 08:00', 'now'):
            with self.assertRaises(write.Usage, msg=bad):
                self.add(when=bad)

    def test_when_defaults_to_now_utc(self):
        self.add(when=None)
        header = self.text().split('\n## ', 1)[1].split('\n', 1)[0]
        self.assertRegex(header, r'^R-\d{4} \| \d{4}-\d{2}-\d{2} \d{2}:\d{2}Z \| alpha \| selected$')

    def test_multi_line_values_are_indented_and_read_back(self):
        self.add(q='line one\nline two')
        self.assertIn('Q: line one\n  line two\n', self.text())
        fields, errs = fmt.parse_block(fmt.split_entries(self.text())[1][0][1])
        self.assertEqual(errs, [])
        self.assertEqual(fields['Q'], 'line one\nline two')

    def test_delegated_needs_an_earlier_owner_entry(self):
        self.add()
        before = self.text()
        for source in ('consultant', 'consultant under %s' % rid(7)):
            with self.assertRaises(write.Refused, msg=source):
                self.add(prov='delegated', source=source)
        self.assertEqual(self.text(), before)
        got = self.add(prov='delegated', source='consultant under %s' % rid(1))
        self.assertEqual(got.id, rid(2))
        with self.assertRaises(write.Refused):        # a delegated entry is not a delegation
            self.add(prov='delegated', source='consultant under %s' % rid(2))

    def test_supersedes_must_name_an_earlier_entry(self):
        self.add()
        self.add(supersedes=rid(1))
        with self.assertRaises(write.Refused):
            self.add(supersedes=rid(9))

    def test_the_reader_lists_what_the_writer_wrote(self):
        self.add()
        self.add(prov='delegated', source='consultant under %s' % rid(1), decides='Use a record.')
        self.assertEqual(ledger.list_rulings(self.text(), 'delegated', '2026-10-06'),
                         ['%s | 2026-10-06 | Use a record.' % rid(2)])

    def test_an_invalid_ledger_is_never_appended_to(self):
        os.makedirs(os.path.dirname(self.path))
        bad = fmt.template_text() + '\n## %s | %s | alpha | selected\nQ: q\n' % (rid(1), WHEN)
        with open(self.path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(bad)
        with self.assertRaises(write.Refused) as cm:
            self.add()
        self.assertIn('already invalid', str(cm.exception))
        self.assertEqual(self.text(), bad)

    def test_a_long_decides_warns_but_writes(self):
        got = self.add(decides='x' * (fmt.DECIDES_NUDGE + 1))
        self.assertEqual(got.id, rid(1))
        self.assertTrue(any('Decides' in w for w in got.warnings))
        self.assertEqual(self.add().warnings, [])

    def test_crlf_ledger_is_read_and_written_lf(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, 'w', encoding='utf-8', newline='\r\n') as f:
            f.write(fmt.template_text())
        self.add()
        self.assertNotIn('\r', self.text())


class NoItemsWriterTest(Base):
    """The writer names no backlog record: `add` and `fmt.render` take no items."""

    def test_add_takes_no_items(self):
        with self.assertRaises(TypeError):
            self.add(items=['T3'])
        self.assertFalse(os.path.exists(self.path))

    def test_render_takes_no_items(self):
        with self.assertRaises(TypeError):
            fmt.render(rid(1), WHEN, 'alpha', 'selected', 'q', 'a', 'd', 's', '-', ['T3'])


class OlderItemsLineTest(Base):
    """An entry written before, ending with an `Items:` line, is still read and validated."""

    def test_an_older_items_line_validates_is_read_and_is_kept_by_the_next_add(self):
        os.makedirs(os.path.dirname(self.path))
        old = ('## %s | %s | alpha | selected\nQ: Which?\nA: This one\nDecides: Use this one.\n'
               'Source: PM session chat\nSupersedes: -\nItems: T3, EX-UNLOCKS\n' % (rid(1), WHEN))
        with open(self.path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(fmt.template_text().rstrip('\n') + '\n\n' + old)
        self.assertEqual(fmt.validate(self.text()), [])
        found, why = backlog_ledger.read_entries(self.path)
        self.assertIsNone(why)
        self.assertEqual(found[rid(1)].items, ('T3', 'EX-UNLOCKS'))
        self.assertEqual((found[rid(1)].repo, found[rid(1)].kind), ('alpha', 'selected'))
        self.assertEqual(self.add(decides='Second.').id, rid(2))
        text = self.text()
        self.assertIn(old, text)
        self.assertNotIn('Items:', text[text.index('## %s' % rid(2)):])
        self.assertEqual(fmt.validate(text), [])

    def test_a_bad_or_repeated_items_line_is_refused(self):
        self.add()
        for line, bad in (('Items: A, A', "['A', 'A']"), ('Items: ok, not ok', "['not ok']")):
            errs = fmt.validate(self.text() + line + '\n')
            self.assertEqual(errs, ['%s: Items must be distinct record ids, comma-separated (bad: %s)'
                                    % (rid(1), bad)], msg=line)
        self.assertEqual(fmt.validate(self.text() + 'Items: A, B\n'), [])


class ValidateTest(Base):
    def test_ids_must_run_in_order_from_one(self):
        self.add()
        self.add()
        text = self.text().replace('## %s |' % rid(2), '## %s |' % rid(3))
        self.assertTrue(any('expected %s' % rid(2) in e for e in fmt.validate(text)))

    def test_repo_column_checked_against_the_list_when_given(self):
        self.add(repo='gamma')
        self.assertEqual(fmt.validate(self.text()), [])
        self.assertTrue(any('gamma' in e for e in fmt.validate(self.text(), repos=['alpha'])))
        self.assertEqual(fmt.validate(self.text(), repos=['gamma']), [])

    def test_unexpected_and_duplicate_lines(self):
        self.add()
        text = self.text().replace('Supersedes: -\n', 'Supersedes: -\nNote: hi\nQ: again\n')
        errs = fmt.validate(text)
        self.assertTrue(any('unexpected line' in e for e in errs))
        self.assertTrue(any('duplicate field Q' in e for e in errs))


class AppendOnlyTest(Base):
    def test_edits_and_removals_are_refused(self):
        self.add()
        self.add(decides='Second.')
        old = self.text()
        self.assertEqual(fmt.check_append_only(old, old), [])
        self.assertEqual(fmt.check_append_only(None, old), [])
        edited = old.replace('Second.', 'Changed.')
        self.assertTrue(any('was edited' in e for e in fmt.check_append_only(old, edited)))
        removed = old[:old.index('## %s' % rid(2))]
        self.assertTrue(any('was removed' in e for e in fmt.check_append_only(old, removed)))

    def test_tombstoning_is_allowed_and_untombstoning_is_not(self):
        self.add()
        self.add(decides='Second.')
        old = self.text()
        write.retire(self.path, rid(1), 'spent', 'done once')
        new = self.text()
        self.assertEqual(fmt.check_append_only(old, new), [])
        self.assertTrue(any('was edited' in e for e in fmt.check_append_only(new, old)))


class RetireTest(Base):
    def test_retire_turns_the_body_into_one_line_and_the_reader_skips_it(self):
        self.add()
        self.add(decides='Second.')
        write.retire(self.path, rid(1), 'superseded', 'replaced', by=rid(2))
        text = self.text()
        self.assertIn('## %s | %s | alpha | selected\nRetired: superseded - replaced (see %s)\n'
                      % (rid(1), WHEN, rid(2)), text)
        self.assertEqual(fmt.validate(text), [])
        found, _ = backlog_ledger.read_entries(self.path)
        self.assertEqual(found[rid(1)].retired, ('superseded', rid(2)))
        self.assertEqual(ledger.list_rulings(text, 'selected', '2000-01-01'),
                         ['%s | 2026-10-06 | Second.' % rid(2)])

    def test_refusals(self):
        self.add()
        with self.assertRaises(write.Refused):
            write.retire(self.path, rid(5), 'spent', 'why')
        with self.assertRaises(write.Usage):
            write.retire(self.path, rid(1), 'spent', ' ')
        with self.assertRaises(write.Usage):
            write.retire(self.path, rid(1), 'gone', 'why')
        write.retire(self.path, rid(1), 'spent', 'why')
        with self.assertRaises(write.Refused):
            write.retire(self.path, rid(1), 'spent', 'again')

    def test_an_entry_a_live_decides_names_is_not_retired_unless_that_entry_orders_it(self):
        self.add()
        self.add(decides='Builds on %s.' % rid(1))
        before = self.text()
        with self.assertRaises(write.Refused) as cm:
            write.retire(self.path, rid(1), 'spent', 'why')
        self.assertIn('still cited', str(cm.exception))
        self.assertEqual(self.text(), before)
        with self.assertRaises(write.Refused):          # names it, but does not order it retired
            write.retire(self.path, rid(1), 'spent', 'why', ruling=rid(2))
        self.add(decides='Retire %s now.' % rid(1))
        with self.assertRaises(write.Refused):          # rid(2) still cites it
            write.retire(self.path, rid(1), 'spent', 'why', ruling=rid(3))

    def test_the_ordering_ruling_is_exempt(self):
        self.add()
        self.add(decides='Retire %s now.' % rid(1))
        write.retire(self.path, rid(1), 'spent', 'ordered', ruling=rid(2))
        self.assertIn('Retired: spent - ordered (see -)', self.text())


class ShowGrepTest(Base):
    def test_show_and_grep(self):
        self.add()
        self.add(decides='Second thing.')
        text = self.text()
        self.assertTrue(write.show(text, rid(2)).startswith('## %s |' % rid(2)))
        self.assertIn('Second thing.', write.show(text, rid(2)))
        self.assertIsNone(write.show(text, rid(9)))
        self.assertEqual(write.grep(text, 'SECOND'), ['%s | %s | alpha | selected' % (rid(2), WHEN)])
        self.assertEqual(write.grep(text, 'nowhere'), [])


class WriteLockTest(Base):
    """write.locked: one writer of a ledger at a time, across processes and threads."""

    def test_two_processes_adding_at_once_get_two_ids_and_keep_both_entries(self):
        got = race(self, self.dir, 'add', self.path, ['first', 'second'])
        for rc, out, err in got:
            self.assertEqual(rc, 0, err)
        self.assertEqual(sorted(out for _, out, _ in got), [rid(1), rid(2)])
        text = self.text()
        self.assertEqual([i for i, _ in fmt.split_entries(text)[1]], [rid(1), rid(2)])
        self.assertIn('Decides: first\n', text)
        self.assertIn('Decides: second\n', text)
        self.assertEqual(fmt.validate(text), [])

    def holder(self):
        ready = os.path.join(self.dir, 'held')
        proc = start_child(self, HOLDER_CHILD, [self.path, ready, os.path.join(self.dir, 'stop')])
        wait_for(ready, [proc])
        return proc

    def test_a_held_lock_refuses_after_the_wait_naming_the_holder_and_a_dead_one_is_taken_over(self):
        self.add()
        before = self.text()
        proc = self.holder()
        with mock.patch.object(write, 'LOCK_WAIT', 0.3):
            start = time.monotonic()
            with self.assertRaises(write.Refused) as cm:
                self.add(decides='Blocked.')
            self.assertGreaterEqual(time.monotonic() - start, 0.3)
            with self.assertRaises(write.Refused):
                write.retire(self.path, rid(1), 'spent', 'why')
        text = str(cm.exception)
        self.assertIn('held by pid %d since ' % proc.pid, text)
        self.assertIn('nothing was written', text)
        self.assertEqual(self.text(), before)
        proc.kill()                                     # dies holding it: the OS drops the lock
        proc.communicate(timeout=RACE_TIMEOUT)
        with mock.patch.object(write, 'LOCK_WAIT', 0.3):
            self.assertEqual(self.add(decides='After.').id, rid(2))

    def test_the_lock_nests_in_its_thread_under_any_spelling_and_excludes_other_threads(self):
        other = self.path.replace('\\', '/').upper()
        seen = []

        def from_another_thread():
            try:
                self.add(decides='Other thread.')
                seen.append('added')
            except write.Refused as e:
                seen.append(str(e))
        with mock.patch.object(write, 'LOCK_WAIT', 0.2):
            with write.locked(self.path):
                with write.locked(other):
                    self.assertEqual(self.add().id, rid(1))
                t = threading.Thread(target=from_another_thread)
                t.start()
                t.join(RACE_TIMEOUT)
                self.assertEqual(self.add(decides='Same thread.').id, rid(2))
            self.assertEqual(len(seen), 1)
            self.assertIn('nothing was written', seen[0])
            self.assertEqual(self.add(decides='Free again.').id, rid(3))

    def test_the_lock_file_sits_beside_the_ledger(self):
        self.add()
        self.assertTrue(os.path.isfile(os.path.join(os.path.dirname(self.path), '.LEDGER.md.lock')))


class CheckTest(Base):
    def test_check_combines_validate_and_append_only(self):
        self.add()
        old = self.text()
        self.assertEqual(write.check(old, old), [])
        self.assertEqual(write.check(old, None), [])
        bad = old.replace('Use this one.', 'Other.')
        self.assertTrue(any('was edited' in e for e in write.check(bad, old)))
        self.assertTrue(any('alpha' in e for e in write.check(old, None, repos=['beta'])))


if __name__ == '__main__':
    unittest.main()
