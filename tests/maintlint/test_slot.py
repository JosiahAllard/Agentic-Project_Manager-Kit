"""kitlib.maintlint.slot and tools/maint_lint.py's use of it: a plain run books one maint-lint slot,
waiting at most the kind's warn_min; a busy lock skips the run (exit 3) and leaves the previous
result.json as it was; --dry-run books nothing. The lock is stubbed: no test books the machine's."""
import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from kitlib.lock.store import EXIT_BUSY, EXIT_REQUIRE, EXIT_RESERVED, EXIT_TIMEOUT, LockError
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

    def test_books_one_maint_lint_run_waiting_the_kinds_warn_min(self):
        seen, held = [], FakeSlot()

        def book(kind, repo, slug, **kw):
            seen.append((kind, repo, slug, kw['wait_s'], kw['kinds_table']))
            return held
        got = slot.take('kit', store=object(), table=TABLE, book=book)
        self.assertIs(got, held)
        self.assertEqual(seen, [('maint-lint', 'kit', 'maint-lint', 900, TABLE)])

    def test_a_busy_lock_is_skipped_and_another_refusal_keeps_its_code(self):
        for code in (EXIT_TIMEOUT, EXIT_BUSY, EXIT_RESERVED):
            with self.assertRaisesRegex(slot.Skipped, r'^SKIPPED: .*within 15 min'):
                slot.take('kit', store=object(), table=TABLE, book=refusing(code))
        with self.assertRaises(slot.Refused) as cm:
            slot.take('kit', store=object(), table=TABLE, book=refusing(EXIT_REQUIRE))
        self.assertEqual(cm.exception.code, EXIT_REQUIRE)

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
        with redirect_stdout(out), redirect_stderr(err):
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
        self.assertEqual(sorted(os.listdir(self.dir)), ['result.json'])

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
