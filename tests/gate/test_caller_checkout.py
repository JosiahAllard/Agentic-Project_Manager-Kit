"""The caller's checkout (docs/gate.md, The caller's checkout): a branch gate started through a stub
that moved the caller out of their own checkout is refused, so a shell in one checkout never gates
another by running that checkout's stub.

With no stub, a gate.py run on another checkout of its own repository with no -C or a relative one
(the kit's own repository, where every checkout has a real gate.py) is refused, and a relative --log-dir that would
be made outside the gated checkout is USAGE (OwnCopy, LogDir, RunByPath).

kitlib.gate.caller is tested with a fake classify; the end-to-end cases run a written gate stub on a
throwaway repository with --plan, which books nothing."""
import contextlib
import ctypes
import functools
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from kitlib import gate as G
from kitlib import stubs
from kitlib.gate import caller, runner, steps
from kitlib.git import repo
from tests import gitfixture
from tests.gate import fixtures

GATE_PY = os.path.join(steps.KIT_TOOLS, 'gate.py')


def place(kind, top=None):
    git_dir = os.path.join(top, '.git') if top else None
    return repo.Place(kind, top, '' if top else 'git said so', git_dir, git_dir)


def fake_classify(places):
    return lambda path: places[repo.norm(path)]


def short_name(path):
    """The 8.3 short form of `path`, or None when the volume gives none."""
    buf = ctypes.create_unicode_buffer(1024)
    n = ctypes.windll.kernel32.GetShortPathNameW(path, buf, len(buf))
    return buf.value if n and buf.value.lower() != path.lower() else None


def gate_py(args, cwd, value=None):
    """(exit code, output) of tools/gate.py run directly, with KIT_STUB_CALLER = `value`."""
    env = dict(fixtures.test_env())
    env.pop(caller.VAR, None)
    if value is not None:
        env[caller.VAR] = value
    r = subprocess.run([sys.executable, GATE_PY] + list(args), cwd=cwd, capture_output=True,
                       text=True, encoding='utf-8', errors='replace', env=env, timeout=120)
    return r.returncode, r.stdout + r.stderr


class Take(unittest.TestCase):

    def test_no_variable_is_no_caller(self):
        env = {}
        self.assertEqual(caller.take(env, cwd='/x'), (None, None))

    def test_the_variable_is_removed_and_read(self):
        here = os.path.abspath('stub-root')
        env = {caller.VAR: json.dumps([here, os.path.abspath('elsewhere')]), 'OTHER': '1'}
        got, why = caller.take(env, cwd=here)
        self.assertEqual((got, why), (os.path.abspath('elsewhere'), None))
        self.assertEqual(env, {'OTHER': '1'})

    def test_a_variable_for_another_directory_is_dropped(self):
        """Inherited from a stub further up, not set for this process: removed, not used."""
        env = {caller.VAR: json.dumps([os.path.abspath('a'), os.path.abspath('b')])}
        self.assertEqual(caller.take(env, cwd=os.path.abspath('c')), (None, None))
        self.assertEqual(env, {})

    def test_another_spelling_of_this_directory_is_this_directory(self):
        """A junction or an 8.3 short name for the stub's checkout is still this process's value:
        dropping it would skip the check (fail open)."""
        base = os.path.realpath(tempfile.mkdtemp(prefix='kit-caller-'))
        self.addCleanup(shutil.rmtree, base, True)
        real = os.path.join(base, 'a long checkout name')
        os.makedirs(real)
        link = os.path.join(base, 'link')
        import _winapi
        _winapi.CreateJunction(real, link)
        self.addCleanup(lambda: os.path.isdir(link) and os.rmdir(link))
        spellings = [link]
        short = short_name(real)
        if short:
            spellings.append(short)
        for spelling in spellings:
            for root, cwd in ((spelling, real), (real, spelling)):
                with self.subTest(root=root, cwd=cwd):
                    env = {caller.VAR: json.dumps([root, os.path.abspath('elsewhere')])}
                    self.assertEqual(caller.take(env, cwd=cwd),
                                     (os.path.abspath('elsewhere'), None))

    def test_a_path_the_os_rejects_is_another_directory(self):
        self.assertFalse(caller.same_directory('a\0', os.getcwd()))
        self.assertFalse(caller.same_directory(os.getcwd(), 'b\0'))

    def test_a_malformed_variable_is_a_problem(self):
        for raw in ('not json', '[1]', '"x"', '[null, "b"]',
                    json.dumps(['a\0', 'b']), json.dumps(['a', 'b\0'])):
            with self.subTest(raw=raw):
                env = {caller.VAR: raw}
                got, why = caller.take(env, cwd='/x')
                self.assertIsNone(got)
                self.assertIn(caller.VAR, why)
                self.assertEqual(env, {})


class Refusal(unittest.TestCase):

    def setUp(self):
        self.main = os.path.abspath('main-co')
        self.wt = os.path.abspath(os.path.join('main-co', '.claude', 'worktrees', 'x'))
        self.out = os.path.abspath('no-repo')
        self.lost = os.path.abspath('lost')
        self.classify = fake_classify({
            repo.norm(self.main): place(repo.MAIN, self.main),
            repo.norm(self.wt): place(repo.LINKED, self.wt),
            repo.norm(self.out): place(repo.NONE),
            repo.norm(self.lost): place(repo.UNKNOWN),
        })

    def refusal(self, root, here):
        return caller.refusal(root, here, classify=self.classify)

    def test_the_same_checkout_passes(self):
        self.assertIsNone(self.refusal(self.wt, self.wt))
        self.assertIsNone(self.refusal(self.main, self.main))

    def test_another_checkout_is_refused(self):
        for root, here in ((self.wt, self.main), (self.main, self.wt)):
            with self.subTest(root=root):
                out = self.refusal(root, here)
                self.assertEqual(out.code, G.REFUSED)
                text = '\n'.join(out.lines)
                self.assertTrue(text.startswith('REFUSED: '), text)
                self.assertIn(here, text)
                self.assertIn(root, text)
                self.assertIn('-C', text)

    def test_a_caller_in_no_repository_passes(self):
        self.assertIsNone(self.refusal(self.wt, self.out))

    def test_a_caller_git_cannot_place_is_refused(self):
        out = self.refusal(self.wt, self.lost)
        self.assertEqual(out.code, G.REFUSED)
        self.assertIn('cannot tell', out.lines[0])

    def test_a_gated_tree_git_cannot_place_is_refused(self):
        for root in (self.lost, self.out):
            with self.subTest(root=root):
                self.assertEqual(self.refusal(root, self.main).code, G.REFUSED)


class ThroughAStub(unittest.TestCase):
    """A gate stub written into a throwaway repository with a nested linked worktree."""

    def setUp(self):
        self.repo = gitfixture.TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.repo.write('.kit.json', json.dumps({'gate': fixtures.GATE,
                                                 'stubs': {'tools': ['gate']}}) + '\n')
        self.repo.write('.gitignore', '.claude/\n')
        self.assertEqual(stubs.run(self.repo.root, 'write').code, stubs.OK)
        self.repo.commit('kit', '.kit.json', '.gitignore', 'tools')
        self.main = os.path.realpath(self.repo.root)
        self.wt = os.path.realpath(self.repo.worktree('x'))
        self.outside = os.path.realpath(self.repo.base)

    def gate(self, stub_root, cwd, *args):
        stub = os.path.join(stub_root, 'tools', 'gate.py')
        env = dict(fixtures.test_env())
        env.pop(caller.VAR, None)
        r = subprocess.run([sys.executable, stub, '--plan', '--base', 'HEAD'] + list(args),
                           cwd=cwd, capture_output=True, text=True, encoding='utf-8',
                           errors='replace', env=env, timeout=120)
        return r.returncode, r.stdout + r.stderr

    def test_another_checkouts_stub_is_refused(self):
        code, out = self.gate(self.wt, self.main)
        self.assertEqual(code, G.REFUSED, out)
        self.assertIn('REFUSED: ', out)
        self.assertNotIn('RUN   unit', out)

    def test_the_callers_own_checkout_passes(self):
        for stub_root, cwd in ((self.wt, self.wt), (self.main, self.main),
                               (self.main, self.wt)):    # a nested worktree keeps its cwd
            with self.subTest(stub=stub_root, cwd=cwd):
                code, out = self.gate(stub_root, cwd)
                self.assertEqual(code, 0, out)
                self.assertIn('RUN   unit (always)', out)

    def test_a_caller_in_no_repository_passes(self):
        code, out = self.gate(self.wt, self.outside)
        self.assertEqual(code, 0, out)

    def test_an_explicit_minus_c_names_the_tree(self):
        code, out = self.gate(self.wt, self.main, '-C', self.wt)
        self.assertEqual(code, 0, out)
        r = subprocess.run([sys.executable, GATE_PY, '--plan', '--base', 'HEAD', '-C', self.wt],
                           cwd=self.main, capture_output=True, text=True, encoding='utf-8',
                           errors='replace', env=fixtures.test_env(), timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_a_relative_minus_c_is_checked(self):
        """Through a moved stub a relative -C resolves against the stub's checkout, not where the
        caller typed it, so it names nothing for the caller: the rule still applies."""
        for rel in ('.', os.path.join('..', 'x')):
            with self.subTest(rel=rel):
                code, out = self.gate(self.wt, self.main, '-C', rel)
                self.assertEqual(code, G.REFUSED, out)
                self.assertIn('REFUSED: the caller is in the checkout', out)   # not "cannot tell"
                self.assertNotIn('RUN   unit', out)

    def test_a_malformed_value_is_refused_by_gate_py(self):
        code, out = gate_py(['--plan', '--base', 'HEAD'], self.wt, value='not json')
        self.assertEqual(code, G.REFUSED, out)
        self.assertIn('REFUSED: %s is not' % caller.VAR, out)
        self.assertNotIn('RUN   unit', out)

    def test_a_nul_in_the_value_is_refused_cleanly(self):
        """A NUL (sent as a JSON escape) makes a path unusable to the OS: refused as malformed,
        never a traceback."""
        for pair in ([self.wt + '\0', self.main], [self.wt, self.main + '\0']):
            with self.subTest(pair=pair):
                code, out = gate_py(['--plan', '--base', 'HEAD'], self.wt, value=json.dumps(pair))
                self.assertEqual(code, G.REFUSED, out)
                self.assertIn('REFUSED: %s is not' % caller.VAR, out)
                self.assertNotIn('Traceback', out)
                self.assertNotIn('RUN   unit', out)

    def test_a_value_for_another_directory_is_dropped_by_gate_py(self):
        stale = json.dumps([self.main, self.main])       # set for a process in main, not here
        code, out = gate_py(['--plan', '--base', 'HEAD'], self.wt, value=stale)
        self.assertEqual(code, 0, out)
        self.assertIn('RUN   unit (always)', out)
        mine = json.dumps([self.wt, self.main])          # the control: this directory's value
        code, out = gate_py(['--plan', '--base', 'HEAD'], self.wt, value=mine)
        self.assertEqual(code, G.REFUSED, out)

    def test_the_stub_and_the_library_name_one_variable(self):
        self.assertIn('CALLER = %r' % caller.VAR, stubs.stub_text('gate'))


def load_gate_tool():
    spec = importlib.util.spec_from_file_location('gate_tool_under_test', GATE_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def linked(top, main_top):
    """The Place of a linked worktree at `top` of the repository whose main checkout is
    `main_top`: its own git dir, the main checkout's common dir."""
    return repo.Place(repo.LINKED, top, '', os.path.join(main_top, '.git', 'worktrees', 'x'),
                      os.path.join(main_top, '.git'))


class OwnCopy(unittest.TestCase):
    """caller.own_copy_refusal with a fake classify: no stub, no -C, and a gate.py in one checkout
    run from another checkout of the same repository."""

    def setUp(self):
        self.main = os.path.abspath('main-co')
        self.wt = os.path.abspath(os.path.join('main-co', '.claude', 'worktrees', 'x'))
        self.other = os.path.abspath('kit-release')
        self.out = os.path.abspath('no-repo')
        self.lost = os.path.abspath('lost')
        self.classify = fake_classify({
            repo.norm(self.main): place(repo.MAIN, self.main),
            repo.norm(self.wt): linked(self.wt, self.main),
            repo.norm(self.other): place(repo.MAIN, self.other),
            repo.norm(self.out): place(repo.NONE),
            repo.norm(self.lost): place(repo.UNKNOWN),
        })

    def refusal(self, cwd, kit_root):
        return caller.own_copy_refusal(cwd, kit_root, classify=self.classify)

    def test_another_checkout_of_the_same_repository_is_refused(self):
        for cwd, kit in ((self.main, self.wt), (self.wt, self.main)):
            with self.subTest(cwd=cwd, kit=kit):
                out = self.refusal(cwd, kit)
                self.assertEqual(out.code, G.REFUSED)
                text = '\n'.join(out.lines)
                self.assertTrue(text.startswith('REFUSED: '), text)
                self.assertIn('-C %s' % kit, text)      # the command to use instead
                self.assertIn(os.path.join(kit, 'tools', 'gate.py'), text)

    def test_a_relative_minus_c_is_named_in_the_refusal(self):
        out = caller.own_copy_refusal(self.main, self.wt, rel='.', classify=self.classify)
        self.assertEqual(out.code, G.REFUSED)
        self.assertIn('given the relative -C . (%s)' % self.main, out.lines[0])
        self.assertIn('-C %s' % self.wt, out.lines[0])

    def test_the_same_checkout_passes(self):
        self.assertIsNone(self.refusal(self.wt, self.wt))
        self.assertIsNone(self.refusal(self.main, self.main))

    def test_a_kit_in_another_repository_passes(self):
        """A repository's stubs run the kit checkout written into them: another repository, the
        normal case."""
        self.assertIsNone(self.refusal(self.main, self.other))
        self.assertIsNone(self.refusal(self.wt, self.other))

    def test_a_cwd_in_no_repository_passes(self):
        self.assertIsNone(self.refusal(self.out, self.wt))

    def test_a_kit_in_no_repository_passes(self):
        self.assertIsNone(self.refusal(self.main, self.out))

    def test_a_kit_git_cannot_place_is_refused(self):
        out = self.refusal(self.main, self.lost)
        self.assertEqual(out.code, G.REFUSED)
        self.assertIn('cannot tell', out.lines[0])


class LogDir(unittest.TestCase):
    """caller.log_dir_refusal with a fake classify: a relative --log-dir resolves against the
    process's directory, never the gated tree."""

    def setUp(self):
        self.main = os.path.abspath('main-co')
        self.wt = os.path.abspath(os.path.join('main-co', '.claude', 'worktrees', 'x'))
        self.out = os.path.abspath('no-repo')
        self.classify = fake_classify({
            repo.norm(self.main): place(repo.MAIN, self.main),
            repo.norm(self.wt): linked(self.wt, self.main),
            repo.norm(self.out): place(repo.NONE),
        })

    def refusal(self, log_dir, root, cwd):
        return caller.log_dir_refusal(log_dir, root, cwd, classify=self.classify)

    def test_a_relative_log_dir_in_another_checkout_is_usage(self):
        for root, cwd in ((self.wt, self.main), (self.main, self.wt)):
            with self.subTest(root=root, cwd=cwd):
                out = self.refusal('logs', root, cwd)
                self.assertEqual(out.code, G.USAGE)
                self.assertTrue(out.lines[0].startswith('USAGE: --log-dir logs is relative'),
                                out.lines)

    def test_an_absolute_or_absent_log_dir_passes(self):
        self.assertIsNone(self.refusal(os.path.abspath('logs'), self.wt, self.main))
        self.assertIsNone(self.refusal(None, self.wt, self.main))

    def test_a_relative_log_dir_in_the_gated_checkout_passes(self):
        self.assertIsNone(self.refusal('logs', self.wt, self.wt))

    def test_a_cwd_in_no_repository_passes(self):
        self.assertIsNone(self.refusal('logs', self.wt, self.out))


class RunByPath(unittest.TestCase):
    """tools/gate.py main() in this process, with no stub, on a throwaway repository with a nested
    linked worktree, as the kit's own repository is: every checkout has a real gate.py (KIT is
    patched to say which checkout this one is in). --plan books nothing."""

    def setUp(self):
        self.repo = gitfixture.TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.repo.write('.kit.json', json.dumps({'gate': fixtures.GATE}) + '\n')
        self.repo.write('.gitignore', '.claude/\n')
        self.repo.commit('kit', '.kit.json', '.gitignore')
        self.main = os.path.realpath(self.repo.root)
        self.wt = os.path.realpath(self.repo.worktree('x'))
        self.tool = load_gate_tool()
        here = os.getcwd()
        self.addCleanup(os.chdir, here)

    def gate(self, cwd, kit, *args):
        """(exit code, output) of main(['--plan', '--base', 'HEAD', *args]) from `cwd`, as the
        gate.py of the checkout `kit`."""
        os.chdir(cwd)
        env = dict(fixtures.test_env())
        env.pop(caller.VAR, None)
        buf = io.StringIO()
        # create=True: the tool names its own checkout KIT; patched so it is the throwaway one
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(self.tool, 'KIT', kit, create=True), \
                mock.patch.object(self.tool, 'Store', lambda say=None: None), \
                contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                code = self.tool.main(['--plan', '--base', 'HEAD'] + list(args))
            except SystemExit as e:
                code = e.code
        return code, buf.getvalue()

    def test_another_checkouts_gate_py_without_minus_c_is_refused(self):
        """The hazard: `python <worktree>/tools/gate.py` from the main checkout gated the main
        checkout with the worktree's code, and the reverse."""
        for cwd, kit in ((self.main, self.wt), (self.wt, self.main)):
            with self.subTest(cwd=cwd, kit=kit):
                code, out = self.gate(cwd, kit)
                self.assertEqual(code, G.REFUSED, out)
                self.assertIn('Name the tree with an absolute -C: python %s -C %s' % (
                    os.path.join(kit, 'tools', 'gate.py'), kit), out)
                self.assertNotIn('RUN   unit', out)

    def test_an_absolute_minus_c_from_the_main_checkout_gates_the_worktree(self):
        """The documented command: python <worktree>/tools/gate.py -C <worktree>, from the main
        checkout."""
        self.repo.write('new.txt', 'n\n', root=self.wt)    # untracked in the worktree only
        code, out = self.gate(self.main, self.wt, '-C', self.wt)
        self.assertEqual(code, 0, out)
        self.assertIn('RUN   unit (always)', out)
        self.assertIn('1 changed path(s)', out)         # the worktree's new.txt: not main's tree

    def test_its_own_checkout_passes(self):
        for cwd in (self.main, self.wt):
            with self.subTest(cwd=cwd):
                code, out = self.gate(cwd, cwd)
                self.assertEqual(code, 0, out)
                self.assertIn('RUN   unit (always)', out)

    def test_a_relative_log_dir_for_another_checkout_is_usage(self):
        code, out = self.gate(self.main, self.wt, '-C', self.wt, '--log-dir', 'gate-logs')
        self.assertEqual(code, G.USAGE, out)
        self.assertIn('USAGE: --log-dir gate-logs is relative', out)
        self.assertNotIn('RUN   unit', out)
        self.assertFalse(os.path.exists(os.path.join(self.main, 'gate-logs')))

    def test_an_absolute_log_dir_from_another_checkout_passes(self):
        logs = os.path.join(self.repo.base, 'logs')
        code, out = self.gate(self.main, self.wt, '-C', self.wt, '--log-dir', logs)
        self.assertEqual(code, 0, out)

    def test_a_relative_minus_c_naming_another_checkout_is_refused(self):
        """`python <worktree>/tools/gate.py -C .` from the main checkout gated the main checkout
        with the worktree's code, as no -C did; so did main's gate.py with a relative -C naming
        the worktree."""
        rel_wt = os.path.join('.claude', 'worktrees', 'x')
        for cwd, kit, rel in ((self.main, self.wt, '.'), (self.wt, self.main, '.'),
                              (self.main, self.main, rel_wt)):
            with self.subTest(cwd=cwd, kit=kit, rel=rel):
                code, out = self.gate(cwd, kit, '-C', rel)
                self.assertEqual(code, G.REFUSED, out)
                self.assertIn('given the relative -C %s' % rel, out)
                self.assertIn('Name the tree with an absolute -C: python %s -C %s' % (
                    os.path.join(kit, 'tools', 'gate.py'), kit), out)
                self.assertNotIn('RUN   unit', out)

    def test_a_relative_minus_c_naming_its_own_checkout_passes(self):
        code, out = self.gate(self.main, self.wt, '-C', os.path.join('.claude', 'worktrees', 'x'))
        self.assertEqual(code, 0, out)
        self.assertIn('RUN   unit (always)', out)

    def test_an_absolute_minus_c_naming_another_checkout_passes(self):
        """main's gate.py with an absolute -C naming the worktree runs main's code on it, as
        documented: an absolute -C names the tree on purpose."""
        code, out = self.gate(self.main, self.main, '-C', self.wt)
        self.assertEqual(code, 0, out)
        self.assertIn('RUN   unit (always)', out)


class TheStepsEnvironment(fixtures.GateRepoCase):
    """tools/gate.py main() in this process on the scratch lock store: a gate step never sees
    KIT_STUB_CALLER, even when the gate was given one."""

    def setUp(self):
        out = tempfile.mkdtemp(prefix='kit-caller-env-')
        self.addCleanup(shutil.rmtree, out, True)
        self.seen = os.path.join(out, 'seen.txt')
        self.GATE = {'steps': [{'name': 'unit', 'always': True, 'pass': '^ok$', 'cmd': [
            '{python}', '-c', 'import os, sys; open(sys.argv[1], "w").write(os.environ.get('
            '"%s", "<absent>")); print("ok")' % caller.VAR, self.seen]}]}
        super().setUp()

    def test_a_step_does_not_inherit_the_variable(self):
        wt, _ = self.branch('env', {'a.txt': 'a\n'})
        wt = os.path.realpath(wt)
        tool = load_gate_tool()
        env = dict(self.env, **{caller.VAR: json.dumps([wt, wt])})   # same checkout: it runs
        book = functools.partial(runner.lockrun.book, cores=1)
        here = os.getcwd()
        self.addCleanup(os.chdir, here)
        os.chdir(wt)
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(tool, 'Store', lambda say=None: self.store), \
                mock.patch.object(runner.lockrun, 'book', book), \
                contextlib.redirect_stdout(buf):
            code = tool.main(['--log-dir', self.logs])
            self.assertNotIn(caller.VAR, os.environ)
        self.assertEqual(code, 0, buf.getvalue())
        with open(self.seen, encoding='utf-8') as f:
            self.assertEqual(f.read(), '<absent>')


if __name__ == '__main__':
    unittest.main()
