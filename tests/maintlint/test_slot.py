"""kitlib.maintlint.slot and tools/maint_lint.py's use of it: a plain run books one maint-lint slot,
waiting a fixed 15 min; a busy lock or a strong hold skips the run (exit 3), leaves the previous
result.json as it was and writes the skip marker, which the next written result clears; the lock's
exit 1 and 2 become 4; --dry-run books nothing. The lock is stubbed: no test books the machine's."""
import contextlib
import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from kitlib.lock.store import (EXIT_BUSY, EXIT_FAIL, EXIT_HOLD, EXIT_REQUIRE, EXIT_RESERVED,
                               EXIT_TIMEOUT, EXIT_USAGE, LockError)
from kitlib.maintlint import slot
from tests.gitfixture import rmtree
from tools import maint_lint

TABLE = {'maint-lint': {'cpu': 1, 'gpu': 0, 'share': False, 'warn_min': 15}}


class FakeSlot(object):
    def __init__(self):
        self.entered = self.exited = 0

    def __enter__(self):
        self.entered += 1
        return self

    def __exit__(self, *exc):
        self.exited += 1


def refusing(code):
    def book(*args, **kw):
        raise LockError(code, 'NOT ACQUIRED: cpu 12/12 busy; waited 900 s')
    return book


class Take(unittest.TestCase):

    def test_books_one_maint_lint_run_waiting_15_min(self):
        seen, held = [], FakeSlot()

        def book(kind, repo, slug, **kw):
            seen.append((kind, repo, slug, kw['wait_s'], kw['kinds_table']))
            return held
        got = slot.take('kit', store=object(), table=TABLE, book=book)
        self.assertIs(got, held)
        self.assertEqual(seen, [('maint-lint', 'kit', 'maint-lint', 900, TABLE)])

    def test_the_wait_is_fixed_whatever_the_kinds_warn_min_says(self):
        # gate.py --measure rewrites warn_min from run history; the skip window must not follow it.
        seen = []

        def book(kind, repo, slug, **kw):
            seen.append(kw['wait_s'])
            return FakeSlot()
        for warn in (1, 15, 60):
            table = {'maint-lint': dict(TABLE['maint-lint'], warn_min=warn)}
            slot.take('kit', store=object(), table=table, book=book)
        self.assertEqual(seen, [900, 900, 900])

    def test_a_busy_lock_is_skipped_and_another_refusal_keeps_its_code(self):
        for code, reason in ((EXIT_TIMEOUT, 'timeout'), (EXIT_BUSY, 'busy')):
            with self.assertRaisesRegex(slot.Skipped, r'^SKIPPED: .*within 15 min') as cm:
                slot.take('kit', store=object(), table=TABLE, book=refusing(code))
            self.assertEqual((cm.exception.reason, cm.exception.code), (reason, code))
        with self.assertRaises(slot.Refused) as cm:
            slot.take('kit', store=object(), table=TABLE, book=refusing(EXIT_REQUIRE))
        self.assertEqual(cm.exception.code, EXIT_REQUIRE)

    def test_a_strong_hold_is_skipped_at_once_and_says_no_wait(self):
        with self.assertRaises(slot.Skipped) as cm:
            slot.take('kit', store=object(), table=TABLE, book=refusing(EXIT_RESERVED))
        msg = str(cm.exception)
        self.assertTrue(msg.startswith('SKIPPED: '), msg)
        self.assertNotIn('within', msg)
        self.assertIn('owner or game hold', msg)
        self.assertEqual((cm.exception.reason, cm.exception.code), ('hold', EXIT_RESERVED))

    def test_the_locks_exit_1_and_2_are_mapped_off_maint_lints_own_codes(self):
        for code in (EXIT_FAIL, EXIT_USAGE):
            with self.assertRaises(slot.Refused) as cm:
                slot.take('kit', store=object(), table=TABLE, book=refusing(code))
            self.assertEqual(cm.exception.code, slot.EXIT_LOCK)
            self.assertIn('lock exit %d' % code, str(cm.exception))
        self.assertNotIn(slot.EXIT_LOCK, (0, 1, 2, slot.EXIT_SKIPPED, EXIT_FAIL, EXIT_USAGE,
                                          EXIT_BUSY, EXIT_TIMEOUT, EXIT_REQUIRE, EXIT_RESERVED,
                                          EXIT_HOLD))

    def test_a_table_with_no_maint_lint_kind_is_refused_before_booking(self):
        with self.assertRaises(slot.Refused) as cm:
            slot.take('kit', store=object(), table={'build': TABLE['maint-lint']},
                      book=refusing(EXIT_TIMEOUT))
        self.assertEqual(cm.exception.code, EXIT_REQUIRE)

    def test_skipped_is_a_distinct_exit_code(self):
        self.assertEqual(slot.EXIT_SKIPPED, 3)
        self.assertNotIn(slot.EXIT_SKIPPED, (0, 1, 2) + slot.SKIP_CODES)


class Tool(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='kit-maintlint-slot-')
        self.addCleanup(rmtree, self.dir)
        self.result = os.path.join(self.dir, 'result.json')

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        # Every completed plain run calls kitlib.maintlint.publish. These results name no repo,
        # so it sends nothing; a run that would reach the machine's hub fails loudly instead.
        live = mock.Mock(side_effect=AssertionError('test reached the live hub publish'))
        with redirect_stdout(out), redirect_stderr(err), \
                mock.patch.object(maint_lint.publish, 'hub_client', live):
            code = maint_lint.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_a_busy_lock_skips_the_run_and_leaves_result_json_untouched(self):
        with open(self.result, 'wb') as fh:
            fh.write(b'{"findings": [], "previous": true}\n')
        before = os.stat(self.result)
        with mock.patch.object(slot.lockrun, 'book', refusing(EXIT_TIMEOUT)), \
                mock.patch.object(slot.kinds_mod, 'load', return_value=TABLE), \
                mock.patch.object(maint_lint.maintlint, 'run') as run:
            code, out, err = self.main('-C', self.dir, '--result', self.result)
        self.assertEqual(code, 3, err)
        self.assertEqual(len(out.splitlines()), 1, out)
        self.assertTrue(out.startswith('SKIPPED: '), out)
        run.assert_not_called()
        with open(self.result, 'rb') as fh:
            self.assertEqual(fh.read(), b'{"findings": [], "previous": true}\n')
        self.assertEqual(os.stat(self.result).st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(sorted(os.listdir(self.dir)), ['result.json', 'skipped.json'])

    def test_a_skipped_run_leaves_a_marker_with_its_reason_and_time(self):
        for code, reason in ((EXIT_TIMEOUT, 'timeout'), (EXIT_BUSY, 'busy'),
                             (EXIT_RESERVED, 'hold')):
            with mock.patch.object(slot.lockrun, 'book', refusing(code)), \
                    mock.patch.object(slot.kinds_mod, 'load', return_value=TABLE), \
                    mock.patch.object(maint_lint.maintlint, 'run') as run:
                got, out, err = self.main('-C', self.dir, '--result', self.result)
            self.assertEqual(got, 3, err)
            run.assert_not_called()
            mark = slot.read_skip(self.result)
            self.assertEqual(sorted(mark), ['at', 'code', 'reason'])
            self.assertEqual((mark['reason'], mark['code']), (reason, code))
            self.assertRegex(mark['at'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')
            self.assertEqual(slot.marker_path(self.result),
                             os.path.join(self.dir, 'skipped.json'))

    def test_a_run_that_writes_result_json_clears_the_skip_marker(self):
        res = {'findings': [], 'notes': [], 'counts': {}, 'ledger': None, 'elapsed_s': 0.0}
        with mock.patch.object(slot.lockrun, 'book', refusing(EXIT_TIMEOUT)), \
                mock.patch.object(slot.kinds_mod, 'load', return_value=TABLE):
            self.main('-C', self.dir, '--result', self.result)
        self.assertIsNotNone(slot.read_skip(self.result))
        with mock.patch.object(slot, 'take', return_value=FakeSlot()), \
                mock.patch.object(maint_lint.maintlint, 'run', return_value=res):
            code, out, err = self.main('-C', self.dir, '--result', self.result)
        self.assertEqual(code, 0, err)
        self.assertIsNone(slot.read_skip(self.result))
        self.assertEqual(sorted(os.listdir(self.dir)), ['page.html', 'result.json'])

    def test_read_skip_is_none_for_no_marker_or_one_of_another_shape(self):
        self.assertIsNone(slot.read_skip(self.result))
        mark = os.path.join(self.dir, 'skipped.json')
        for body in ('not json', '[]', '{"reason": "late", "at": "x", "code": 8}',
                     '{"reason": "busy", "code": 6}', '{"reason": "busy", "at": "x"}'):
            with open(mark, 'w', encoding='utf-8') as fh:
                fh.write(body)
            self.assertIsNone(slot.read_skip(self.result), body)

    def test_read_skip_rejects_a_bool_code(self):
        # bool is a subclass of int; a marker's code is a lock exit code, never true or false.
        mark = os.path.join(self.dir, 'skipped.json')
        for body in ('{"reason": "busy", "at": "x", "code": true}',
                     '{"reason": "busy", "at": "x", "code": false}'):
            with open(mark, 'w', encoding='utf-8') as fh:
                fh.write(body)
            self.assertIsNone(slot.read_skip(self.result), body)

    def test_a_failed_marker_write_leaves_no_temp_file(self):
        skipped = slot.Skipped('busy', EXIT_BUSY, 'SKIPPED: busy')
        for target in ('replace', 'dump'):
            where = slot.os if target == 'replace' else slot.json
            with mock.patch.object(where, target, side_effect=OSError('no space')):
                with self.assertRaises(OSError):
                    slot.write_skip(self.result, skipped)
            self.assertEqual(os.listdir(self.dir), [], target)

    def test_a_marker_that_cannot_be_written_still_exits_skipped_with_a_note(self):
        with mock.patch.object(slot.lockrun, 'book', refusing(EXIT_TIMEOUT)), \
                mock.patch.object(slot.kinds_mod, 'load', return_value=TABLE), \
                mock.patch.object(slot, 'write_skip', side_effect=OSError('denied')), \
                mock.patch.object(maint_lint.maintlint, 'run') as run:
            code, out, err = self.main('-C', self.dir, '--result', self.result)
        self.assertEqual(code, slot.EXIT_SKIPPED, err)
        self.assertTrue(out.startswith('SKIPPED: '), out)
        self.assertEqual(err, 'note: the skip marker was not written: denied\n')
        run.assert_not_called()

    def test_a_marker_that_cannot_be_cleared_keeps_the_runs_exit_and_adds_a_note(self):
        clean = {'findings': [], 'notes': [], 'counts': {}, 'ledger': None, 'elapsed_s': 0.0}
        found = dict(clean, findings=[{'check': 'x', 'id': 'A', 'text': 't', 'fix': 'f'}])
        for res, want in ((clean, 0), (found, 1)):
            with mock.patch.object(slot, 'take', return_value=FakeSlot()), \
                    mock.patch.object(slot, 'clear_skip', side_effect=OSError('in use')), \
                    mock.patch.object(maint_lint.page, 'render', return_value=''), \
                    mock.patch.object(maint_lint.maintlint, 'run', return_value=res):
                code, out, err = self.main('-C', self.dir, '--result', self.result)
            self.assertEqual(code, want, err)
            self.assertEqual(err, 'note: the old skip marker was not removed: in use\n')
            self.assertTrue(os.path.isfile(self.result))

    def test_an_unexpected_error_exits_5_with_one_error_line(self):
        self.assertNotIn(maint_lint.EXIT_ERROR,
                         (0, 1, 2, slot.EXIT_SKIPPED, slot.EXIT_LOCK, EXIT_FAIL, EXIT_USAGE,
                          EXIT_BUSY, EXIT_TIMEOUT, EXIT_REQUIRE, EXIT_RESERVED, EXIT_HOLD))
        held = FakeSlot()
        cases = (
            ('RuntimeError: take broke',
             [mock.patch.object(slot, 'take', side_effect=RuntimeError('take broke'))]),
            ('RuntimeError: book broke',
             [mock.patch.object(slot.kinds_mod, 'load', return_value=TABLE),
              mock.patch.object(slot.lockrun, 'book', side_effect=RuntimeError('book broke'))]),
            ('ValueError: lint broke',
             [mock.patch.object(slot, 'take', return_value=held),
              mock.patch.object(maint_lint.maintlint, 'run',
                                side_effect=ValueError('lint broke'))]),
        )
        for want, patches in cases:
            with contextlib.ExitStack() as stack:
                for p in patches:
                    stack.enter_context(p)
                code, out, err = self.main('-C', self.dir, '--result', self.result)
            self.assertEqual(code, maint_lint.EXIT_ERROR, (want, out, err))
            self.assertEqual(err, 'ERROR: %s\n' % want)
        self.assertEqual((held.entered, held.exited), (1, 1))

    def test_an_unexpected_error_in_a_dry_run_exits_5(self):
        page = os.path.join(self.dir, 'page.html')
        with mock.patch.object(maint_lint.maintlint, 'run', side_effect=KeyError('gone')):
            code, _, err = self.main('-C', self.dir, '--dry-run', '--result', self.result,
                                     '--out', page)
        self.assertEqual(code, maint_lint.EXIT_ERROR, err)
        self.assertIn('KeyError', err)

    def test_keyboard_interrupt_and_other_base_exceptions_are_not_caught(self):
        for exc in (KeyboardInterrupt, SystemExit):
            with mock.patch.object(slot, 'take', return_value=FakeSlot()), \
                    mock.patch.object(maint_lint.maintlint, 'run', side_effect=exc):
                with self.assertRaises(exc):
                    self.main('-C', self.dir, '--result', self.result)

    def test_another_refusal_exits_with_the_locks_code_and_runs_nothing(self):
        with mock.patch.object(slot.lockrun, 'book', refusing(EXIT_REQUIRE)), \
                mock.patch.object(slot.kinds_mod, 'load', return_value=TABLE), \
                mock.patch.object(maint_lint.maintlint, 'run') as run:
            code, _, err = self.main('-C', self.dir, '--result', self.result)
        self.assertEqual(code, EXIT_REQUIRE)
        self.assertIn('REFUSED: ', err)
        run.assert_not_called()

    def test_a_plain_run_lints_inside_the_held_slot(self):
        held, order = FakeSlot(), []
        res = {'findings': [], 'notes': [], 'counts': {}, 'ledger': None, 'elapsed_s': 0.0}

        def run(root, previous=None):
            order.append(('run', held.entered, held.exited))
            return res
        with mock.patch.object(slot, 'take', return_value=held) as take, \
                mock.patch.object(maint_lint.maintlint, 'run', run):
            code, out, _ = self.main('-C', self.dir, '--result', self.result)
        self.assertEqual(code, 0, out)
        take.assert_called_once_with(os.path.basename(self.dir))
        self.assertEqual(order, [('run', 1, 0)])
        self.assertEqual((held.entered, held.exited), (1, 1))
        self.assertTrue(os.path.isfile(self.result))

    def test_dry_run_books_nothing(self):
        res = {'findings': [], 'notes': [], 'counts': {}, 'ledger': None, 'elapsed_s': 0.0}
        page = os.path.join(self.dir, 'page.html')
        with mock.patch.object(slot.lockrun, 'book', side_effect=AssertionError('booked')), \
                mock.patch.object(slot, 'take', side_effect=AssertionError('took')), \
                mock.patch.object(maint_lint.maintlint, 'run', return_value=res):
            code, out, err = self.main('-C', self.dir, '--dry-run', '--result', self.result,
                                       '--out', page)
        self.assertEqual(code, 0, err)
        self.assertEqual(os.listdir(self.dir), ['page.html'])


if __name__ == '__main__':
    unittest.main()
