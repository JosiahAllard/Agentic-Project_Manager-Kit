"""The caller's checkout (docs/gate.md, The caller's checkout): a gate.py run on another checkout
of its own repository with no -C or a relative one (the kit's own repository, where every checkout
has a real gate.py) is refused, and a relative --log-dir that would be made outside the gated
checkout is USAGE (OwnCopy, LogDir, RunByPath).

kitlib.gate.caller is tested with a fake classify; RunByPath runs tools/gate.py main() on a
throwaway repository with --plan, which books nothing."""
import contextlib
import importlib.util
import io
import json
import os
import unittest
from unittest import mock

from kitlib import gate as G
from kitlib.gate import caller, steps
from kitlib.git import repo
from tests import gitfixture
from tests.gate import fixtures

GATE_PY = os.path.join(steps.KIT_TOOLS, 'gate.py')


def place(kind, top=None):
    git_dir = os.path.join(top, '.git') if top else None
    return repo.Place(kind, top, '' if top else 'git said so', git_dir, git_dir)


def fake_classify(places):
    return lambda path: places[repo.norm(path)]


def load_gate_tool():
    spec = importlib.util.spec_from_file_location('gate_tool_under_test', GATE_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


RAN_FROM = 'if you meant the directory you ran from'
THIS_GATE = 'if you meant the checkout this gate.py is in'


def advice(top, intent=None):
    """The refusal's line naming the documented command that gates the checkout `top`, headed by
    `intent` (no -C) or by the tree it gates (a relative -C). Paths with no space are bare."""
    return '  %s: python %s -C %s' % (intent or 'to gate %s' % top,
                                      os.path.join(top, 'tools', 'gate.py'), top)


def linked(top, main_top):
    """The Place of a linked worktree at `top` of the repository whose main checkout is
    `main_top`: its own git dir, the main checkout's common dir."""
    return repo.Place(repo.LINKED, top, '', os.path.join(main_top, '.git', 'worktrees', 'x'),
                      os.path.join(main_top, '.git'))


class OwnCopy(unittest.TestCase):
    """caller.own_copy_refusal with a fake classify: no -C, and a gate.py in one checkout
    run from another checkout of the same repository."""

    def setUp(self):
        self.main = os.path.abspath('main-co')
        self.wt = os.path.abspath(os.path.join('main-co', '.claude', 'worktrees', 'x'))
        self.other = os.path.abspath('kit-release')
        self.out = os.path.abspath('no-repo')
        self.lost = os.path.abspath('lost')
        self.lost_in = os.path.abspath(os.path.join('main-co', '.claude', 'lost'))
        self.lost_out = os.path.abspath('lost-outer')
        self.inner = os.path.abspath(os.path.join('lost-outer', 'inner'))
        self.sibling = os.path.abspath('main-co-x')
        self.outer_x = os.path.abspath('lost-outer-x')
        self.classify = fake_classify({
            repo.norm(self.sibling): place(repo.UNKNOWN),
            repo.norm(self.outer_x): place(repo.MAIN, self.outer_x),
            repo.norm(self.main): place(repo.MAIN, self.main),
            repo.norm(self.wt): linked(self.wt, self.main),
            repo.norm(self.other): place(repo.MAIN, self.other),
            repo.norm(self.out): place(repo.NONE),
            repo.norm(self.lost): place(repo.UNKNOWN),
            repo.norm(self.lost_in): place(repo.UNKNOWN),
            repo.norm(self.lost_out): place(repo.UNKNOWN),
            repo.norm(self.inner): place(repo.MAIN, self.inner),
        })

    def refusal(self, cwd, kit_root):
        return caller.own_copy_refusal(cwd, kit_root, classify=self.classify)

    def test_another_checkout_of_the_same_repository_is_refused(self):
        """With no -C the tree meant is ambiguous: the gate.py's path names one checkout, the
        directory another. Both commands are given, each labelled by intent."""
        for cwd, kit in ((self.main, self.wt), (self.wt, self.main)):
            with self.subTest(cwd=cwd, kit=kit):
                out = self.refusal(cwd, kit)
                self.assertEqual(out.code, G.REFUSED)
                text = '\n'.join(out.lines)
                self.assertTrue(text.startswith('REFUSED: '), text)
                self.assertEqual(out.lines[1:], [advice(cwd, RAN_FROM), advice(kit, THIS_GATE)])

    def test_a_relative_minus_c_is_named_in_the_refusal(self):
        """A relative -C names the tree meant: only its command is given, never the gate.py's
        own checkout's (pasting that would gate another tree)."""
        for cwd, kit in ((self.main, self.wt), (self.wt, self.main)):
            with self.subTest(cwd=cwd, kit=kit):
                out = caller.own_copy_refusal(cwd, kit, rel='.', classify=self.classify)
                self.assertEqual(out.code, G.REFUSED)
                self.assertIn('given the relative -C . (%s)' % cwd, out.lines[0])
                self.assertEqual(out.lines[1:], [advice(cwd)])
                self.assertNotIn(advice(kit), out.lines)

    def test_the_same_checkout_passes(self):
        self.assertIsNone(self.refusal(self.wt, self.wt))
        self.assertIsNone(self.refusal(self.main, self.main))

    def test_a_kit_in_another_repository_passes(self):
        """A repository gated by the kit's gate.py by full path: another repository, the normal
        case."""
        self.assertIsNone(self.refusal(self.main, self.other))
        self.assertIsNone(self.refusal(self.wt, self.other))

    def test_a_cwd_in_no_repository_passes(self):
        self.assertIsNone(self.refusal(self.out, self.wt))

    def test_a_kit_in_no_repository_passes(self):
        self.assertIsNone(self.refusal(self.main, self.out))

    def test_a_kit_git_cannot_place_passes_when_apart(self):
        """A consumer repository's gate with a kit checkout git cannot place (dubious ownership,
        a git timeout): the two paths do not nest, so they cannot be one repository's checkouts."""
        self.assertIsNone(self.refusal(self.main, self.lost))
        self.assertIsNone(self.refusal(self.wt, self.lost))

    def test_a_kit_git_cannot_place_is_refused_when_nested(self):
        """With no -C both trees are offered, labelled by intent; a relative -C names the tree
        meant, so only its command."""
        for cwd, kit in ((self.main, self.lost_in), (self.wt, self.lost_in),
                         (self.inner, self.lost_out)):
            with self.subTest(cwd=cwd, kit=kit):
                out = self.refusal(cwd, kit)
                self.assertEqual(out.code, G.REFUSED)
                self.assertIn('cannot tell', out.lines[0])
                self.assertEqual(out.lines[1:], [advice(cwd, RAN_FROM), advice(kit, THIS_GATE)])
                out = caller.own_copy_refusal(cwd, kit, rel='.', classify=self.classify)
                self.assertEqual(out.code, G.REFUSED)
                self.assertEqual(out.lines[1:], [advice(cwd)])

    def test_a_sibling_sharing_a_name_prefix_is_apart(self):
        """`main-co-x` beside `main-co` does not nest with it: a spelling prefix is not a
        folder."""
        self.assertIsNone(self.refusal(self.main, self.sibling))
        self.assertIsNone(self.refusal(self.wt, self.sibling))
        self.assertIsNone(self.refusal(self.outer_x, self.lost_out))

    def test_paths_with_a_space_are_quoted_in_the_advice(self):
        main = os.path.abspath('main co')
        wt = os.path.join(main, '.claude', 'worktrees', 'x')
        classify = fake_classify({repo.norm(main): place(repo.MAIN, main),
                                  repo.norm(wt): linked(wt, main)})
        out = caller.own_copy_refusal(main, wt, classify=classify)
        self.assertEqual(out.lines[1:], [
            '  %s: python "%s" -C "%s"' % (RAN_FROM, os.path.join(main, 'tools', 'gate.py'), main),
            '  %s: python "%s" -C "%s"' % (THIS_GATE, os.path.join(wt, 'tools', 'gate.py'), wt)])
        out = caller.own_copy_refusal(main, wt, rel='.', classify=classify)
        self.assertEqual(out.lines[1:], ['  to gate %s: python "%s" -C "%s"' % (
            main, os.path.join(main, 'tools', 'gate.py'), main)])


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
    """tools/gate.py main() in this process on a throwaway repository with a nested
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

    def gate(self, cwd, kit, *args, extra_env=None):
        """(exit code, output) of main(['--plan', '--base', 'HEAD', *args]) from `cwd`, as the
        gate.py of the checkout `kit`, with `extra_env` over the test environment."""
        os.chdir(cwd)
        env = dict(fixtures.test_env())
        env.update(extra_env or {})
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
                # ambiguous with no -C: both commands, each labelled by intent
                self.assertIn(advice(cwd, RAN_FROM).lower(), out.lower())
                self.assertIn(advice(kit, THIS_GATE).lower(), out.lower())
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
        # (cwd, the gate.py's checkout, -C, the tree the caller asked for)
        for cwd, kit, rel, meant in ((self.main, self.wt, '.', self.main),
                                     (self.wt, self.main, '.', self.wt),
                                     (self.main, self.main, rel_wt, self.wt)):
            with self.subTest(cwd=cwd, kit=kit, rel=rel):
                code, out = self.gate(cwd, kit, '-C', rel)
                self.assertEqual(code, G.REFUSED, out)
                self.assertIn('given the relative -C %s' % rel, out)
                # the advice gates the tree asked for, never the gate.py's own checkout
                self.assertIn(advice(meant).lower(), out.lower())
                self.assertNotIn('to gate %s:' % kit.lower(), out.lower())
                self.assertNotIn('RUN   unit', out)

    def test_a_kit_git_cannot_place(self):
        """Real git: a gate.py whose checkout git cannot place (a .git file it cannot read) passes
        when it is apart from the gated repository, as a consumer repository's gate is, and is
        refused when it is inside the gated checkout."""
        apart = os.path.join(self.repo.base, 'lost-kit')
        inside = os.path.join(self.main, '.claude', 'lost-kit')     # .claude/ is ignored
        for d in (apart, inside):
            os.makedirs(d)
            with open(os.path.join(d, '.git'), 'w', encoding='utf-8') as f:
                f.write('not a gitfile\n')
        code, out = self.gate(self.main, apart)
        self.assertEqual(code, 0, out)
        self.assertIn('RUN   unit (always)', out)
        for cwd in (self.main, self.wt):
            with self.subTest(cwd=cwd):
                code, out = self.gate(cwd, inside)
                self.assertEqual(code, G.REFUSED, out)
                self.assertIn('REFUSED: cannot tell which checkout', out)
                self.assertIn(advice(cwd, RAN_FROM).lower(), out.lower())
                self.assertIn(advice(inside, THIS_GATE).lower(), out.lower())
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

    def test_the_plugins_gate_py_from_a_worktree_is_the_main_checkouts_own(self):
        """A gate.py in an export whose marker names the main checkout pins through it
        (kitlib.gate.pin target), and the run gets KIT_LIVE_ROOT=<main checkout>, which the
        own-copy refusal compares against (pluginsource.live_root): from a linked worktree with no
        -C or a relative one it is refused as the main checkout's own gate.py is. An absolute -C
        still gates the tree it names."""
        from kitlib import pluginsource
        from kitlib.gate import pin
        export = os.path.join(self.repo.base, 'plugin', '0123456789ab')
        os.makedirs(os.path.join(export, 'tools'))
        with open(os.path.join(export, pluginsource.MARKER), 'w', encoding='utf-8') as fh:
            json.dump({'sha': '0123456789ab' * 3 + 'abcd', 'checkout': self.main}, fh)
        live = pin.target(os.path.join(export, 'tools', 'gate.py'), environ={})
        self.assertEqual(live, self.main)
        env = {pluginsource.LIVE_ROOT_ENV: live}
        for args in ((), ('-C', '.')):
            with self.subTest(args=args):
                code, out = self.gate(self.wt, export, *args, extra_env=env)
                self.assertEqual(code, G.REFUSED, out)
                self.assertIn(advice(self.wt, None if args else RAN_FROM).lower(), out.lower())
                self.assertNotIn('RUN   unit', out)
        code, out = self.gate(self.wt, export, '-C', self.wt, extra_env=env)
        self.assertEqual(code, 0, out)
        self.assertIn('RUN   unit (always)', out)
        # the control: an export whose marker names no checkout, with no KIT_LIVE_ROOT, is its
        # own live root (pluginsource.live_root); it is in no repository, so it runs
        with open(os.path.join(export, pluginsource.MARKER), 'w', encoding='utf-8') as fh:
            json.dump({'sha': '0123456789ab' * 3 + 'abcd'}, fh)
        code, out = self.gate(self.wt, export)
        self.assertEqual(code, 0, out)
        self.assertIn('RUN   unit (always)', out)

    def test_the_plugins_gate_py_with_no_live_root_is_the_marker_checkouts_own(self):
        """With no KIT_LIVE_ROOT, pluginsource.live_root of an export whose marker names the main
        checkout is that checkout, so the own-copy refusal treats the export's gate.py as the main
        checkout's own: from a linked worktree with no -C it is refused."""
        from kitlib import pluginsource
        # live_root takes the marker checkout only when it looks like a kit
        with open(os.path.join(self.main, 'kit.json'), 'w', encoding='utf-8') as fh:
            fh.write('{}\n')
        os.makedirs(os.path.join(self.main, 'tools'), exist_ok=True)
        export = os.path.join(self.repo.base, 'plugin', '0123456789ab')
        os.makedirs(os.path.join(export, 'tools'))
        with open(os.path.join(export, pluginsource.MARKER), 'w', encoding='utf-8') as fh:
            json.dump({'sha': '0123456789ab' * 3 + 'abcd', 'checkout': self.main}, fh)
        code, out = self.gate(self.wt, export)
        self.assertEqual(code, G.REFUSED, out)
        self.assertIn(advice(self.wt, RAN_FROM).lower(), out.lower())
        # the printed command for "this gate.py" names the main checkout's gate.py, never the
        # export's, which a later prune may remove
        self.assertIn(advice(self.main, THIS_GATE).lower(), out.lower())
        self.assertNotIn(os.path.join(export, 'tools', 'gate.py').lower(), out.lower())
        self.assertNotIn('RUN   unit', out)


if __name__ == '__main__':
    unittest.main()
