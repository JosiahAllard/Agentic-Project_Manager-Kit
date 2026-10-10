"""The caller's checkout (docs/gate.md, The caller's checkout): a branch gate started through a stub
that moved the caller out of their own checkout is refused, so a shell in one checkout never gates
another by running that checkout's stub.

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
