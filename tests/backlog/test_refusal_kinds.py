"""The backlog verbs' refusal kinds and exit codes (docs/backlog.md, Exit codes): Usage 2,
Refusal 2, NotFound 1, Failure 1; and an AST audit that the guard sites raise the kind they are
meant to, so a guard cannot drift to another kind unnoticed."""
import ast
import os
import unittest

from kitlib.backlog import errors
from tests.backlog import test_cli
from tests.backlog.helpers import ADD_FLAGS

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KINDS = ('Usage', 'Refusal', 'NotFound', 'Failure', 'Ended')


def parse(rel):
    with open(os.path.join(ROOT, *rel.split('/')), encoding='utf-8') as fh:
        return ast.parse(fh.read())


def function(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError('no function %s' % name)


def raised_kinds(node):
    """Names raised by `raise Name(...)` inside `node`, in source order."""
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Raise) and isinstance(n.exc, ast.Call) and isinstance(n.exc.func, ast.Name):
            out.append((n.lineno, n.exc.func.id))
    return [k for _, k in sorted(out)]


def first_raise_after(node, guard_src_fragment, source):
    """Kind raised by the guard whose condition (an `if` test, or the assignment just before an
    `if why is not None`) contains the fragment."""
    body = node.body
    for i, n in enumerate(body):
        if isinstance(n, ast.If):
            cond = ast.get_source_segment(source, n.test)
            if i and isinstance(body[i - 1], ast.Assign):
                cond += ast.get_source_segment(source, body[i - 1])
            if guard_src_fragment in cond:
                kinds = raised_kinds(ast.Module(body=n.body, type_ignores=[]))
                if kinds:
                    return kinds[0]
    raise AssertionError('no guard %r' % guard_src_fragment)


class Kinds(unittest.TestCase):

    def test_codes_and_labels(self):
        got = {k: (getattr(errors, k).label, getattr(errors, k).code) for k in KINDS}
        self.assertEqual(got, {'Usage': ('USAGE', 2), 'Refusal': ('REFUSED', 2),
                               'NotFound': ('NOT FOUND', 1), 'Failure': ('ERROR', 1),
                               'Ended': ('ENDED', 0)})


class CliCodes(unittest.TestCase):
    setUp = test_cli.Cli.setUp
    run_tool = test_cli.Cli.run_tool
    ok = test_cli.Cli.ok

    def test_bad_id_shape_is_usage(self):
        rc, out = self.run_tool('add', 'lower', '--summary', 'x thing here', '--scope', 'tools/x.py',
                                *ADD_FLAGS)
        self.assertEqual(rc, 2, out)
        self.assertRegex(out, r'^USAGE: ')

    def test_id_collision_is_refused(self):
        self.ok('add', 'FOO', '--summary', 'a thing to do', '--scope', 'tools/x.py', *ADD_FLAGS)
        rc, out = self.run_tool('add', 'FOO', '--summary', 'again', '--scope', 'tools/y.py',
                                *ADD_FLAGS)
        self.assertEqual(rc, 2, out)
        self.assertRegex(out, r'^REFUSED: ')

    def test_unblock_of_a_record_that_is_not_blocked_is_usage(self):
        self.ok('add', 'FOO', '--summary', 'a thing to do', '--scope', 'tools/x.py', *ADD_FLAGS)
        rc, out = self.run_tool('unblock', 'FOO')
        self.assertEqual(rc, 2, out)
        self.assertRegex(out, r'^USAGE: ')

    def test_unknown_id_is_not_found_exit_1(self):
        for args in (('show', 'NOPE'), ('set', 'NOPE', '--effort', 's'), ('unblock', 'NOPE')):
            rc, out = self.run_tool(*args)
            self.assertEqual(rc, 1, (args, out))
            self.assertRegex(out, r'^NOT FOUND: ')

    def test_a_tool_that_cannot_finish_is_error_exit_1(self):
        import subprocess
        import sys
        p = subprocess.run([sys.executable, test_cli.TOOL, '-C', os.path.join(self.home, 'no-such-dir'),
                            'check'], capture_output=True, text=True, encoding='utf-8',
                           env=self.env, timeout=120)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertRegex(p.stdout, r'^ERROR: ')


class GuardSites(unittest.TestCase):
    """The audit: each guard raises the kind the mapping names, and nothing in the verbs raises
    a kind outside the four (which the entry point would let through as a traceback)."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, 'kitlib', 'backlog', 'plan.py'), encoding='utf-8') as fh:
            cls.src = fh.read()
        cls.plan = ast.parse(cls.src)

    def test_bad_id_shape_guard_raises_usage(self):
        self.assertEqual(first_raise_after(function(self.plan, 'plan_add'), 'id_error', self.src),
                         'Usage')

    def test_id_collision_guard_raises_refusal(self):
        self.assertEqual(first_raise_after(function(self.plan, 'plan_add'), 'id_refusal', self.src),
                         'Refusal')

    def test_unblock_of_a_non_blocked_record_raises_usage(self):
        self.assertEqual(first_raise_after(function(self.plan, 'plan_unblock'), "'blocked'", self.src),
                         'Usage')

    def test_the_review_mark_guards(self):
        clear = function(self.plan, 'plan_clear_review')
        self.assertEqual(first_raise_after(clear, 'bad', self.src), 'Usage')
        self.assertEqual(first_raise_after(clear, "rec.has('review')", self.src), 'Refusal')
        self.assertEqual(first_raise_after(function(self.plan, 'plan_pull'), "rec.has('review')",
                                           self.src), 'Refusal')
        self.assertEqual(first_raise_after(function(self.plan, 'plan_add'), 'review is not None',
                                           self.src), 'Usage')

    def test_the_delete_guards(self):
        fn = function(self.plan, 'plan_delete')
        # The why's form (_claim_subject) is a usage error; an owner-held or owner-priced record
        # without the role check (refuse_unchecked_end, refuse_top) is refused.
        self.assertEqual(sorted(set(raised_kinds(function(self.plan, '_claim_subject')))),
                         ['Usage'])
        self.assertEqual(raised_kinds(function(self.plan, 'refuse_top')), ['Refusal'])
        own = [n.func.id for n in ast.walk(fn) if isinstance(n, ast.Call)
               and isinstance(n.func, ast.Name)]
        self.assertIn('_claim_subject', own)
        self.assertIn('refuse_unchecked_end', own)
        end = function(self.plan, 'refuse_unchecked_end')
        self.assertIn('refuse_top', [n.func.id for n in ast.walk(end) if isinstance(n, ast.Call)
                                     and isinstance(n.func, ast.Name)])
        # The live-pull guard `delete`, `retire` and `retarget` share: a live pull refuses, a
        # branch probe git cannot answer fails.
        guard = function(self.plan, '_refuse_live_pulls')
        self.assertEqual(raised_kinds(guard), ['Failure', 'Refusal'])
        retarget = function(parse('kitlib/backlog/retarget.py'), 'plan_retarget')
        for fn in (fn, function(self.plan, 'plan_retire'), retarget):
            calls = [n.func.id for n in ast.walk(fn) if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Name)]
            self.assertIn('_refuse_live_pulls', calls, fn.name)

    def test_the_missing_id_guard_raises_not_found(self):
        self.assertEqual(raised_kinds(function(self.plan, 'require')), ['NotFound'])

    def test_a_bad_retarget_path_raises_usage(self):
        fn = function(parse('kitlib/backlog/retarget.py'), 'check_paths')
        self.assertEqual(sorted(set(raised_kinds(fn))), ['Usage'])

    def test_every_raise_in_the_verbs_is_one_of_the_four_kinds(self):
        for rel in ('kitlib/backlog/plan.py',
                    'kitlib/backlog/retarget.py', 'kitlib/backlog/opener.py',
                    'kitlib/backlog/pull_branch.py'):
            kinds = raised_kinds(parse(rel))
            self.assertTrue(kinds, '%s raises nothing: the sweep would pass unread' % rel)
            for kind in kinds:
                if kind in ('PartitionError', 'Conflict', 'UndecidableScope', 'ValueError'):
                    continue
                self.assertIn(kind, KINDS, rel)

    def test_the_entry_point_exits_with_the_kind_code(self):
        main = function(parse('tools/backlog.py'), 'main')
        handlers = [h for n in ast.walk(main) if isinstance(n, ast.Try) for h in n.handlers]
        refusal = [h for h in handlers if isinstance(h.type, ast.Name) and h.type.id == 'Refusal']
        self.assertEqual(len(refusal), 1)
        self.assertIn('exc.code', ast.unparse(refusal[0]))


if __name__ == '__main__':
    unittest.main()
