"""The lock command lines the guard advises (docs/guard.md "Rules": heavy, lock-mcp, owner-tool,
lock-state) are ones the kit's lock tool takes: the words after the script name go to the argument
parser of tools/machine_lock.py itself, and the script they name is the kit's own, not a link in
the home folder. The tool is loaded from its file, as tests/lock/test_cli.py does: this is a test
of arguments."""
import contextlib
import importlib.util
import io
import os
import shlex
import unittest

from kitlib.git import repo
from kitlib.guard import context, rules
from kitlib.guard import shell as sh
from kitlib.lock import kinds

from . import world as W
from .claudehost import MCP_PREFIX, decide
from .world import bash, event, ps

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
TOOL = os.path.join(REPO, 'tools', 'machine_lock.py')
SCRIPT = 'machine_lock.py'

HEAVY = (('godot --headless --path .', 'app-run'),
         ('blender -b scene.blend -a', 'render'),
         ('dotnet build', 'build'),
         ('gradlew assemble', 'build'))


def load_tool():
    spec = importlib.util.spec_from_file_location('machine_lock_for_guard_advice', TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class AdvisedLockLine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = load_tool()

    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)
        W.deny_softened()                       # the advice is heavy's, a warning by default (test_cheap.py)
        self.addCleanup(W.allow_unplaced)

    def advice(self, d, rule, role='main'):
        """The command after "Instead: " of deny `d`, without the guard's tag."""
        self.assertIsNotNone(d, 'allowed')
        self.assertEqual((d.verdict, d.rule), ('deny', rule), d.reason)
        text, tag = d.reason.split(' Instead: ', 1)[1], rules.tag(rule, role)
        self.assertTrue(text.endswith(tag), d.reason)
        return text[:-len(tag)].strip()

    def lock_call(self, line):
        """(the script word, the lock tool's own arguments, the wrapped command) of an advised
        line, split as the tool's main() splits it: at the first `--`."""
        words = shlex.split(line)
        self.assertEqual(words[0], 'python', line)
        at = [i for i, w in enumerate(words) if w.replace('\\', '/').rsplit('/', 1)[-1] == SCRIPT]
        self.assertEqual(at, [1], line)
        rest = words[2:]
        cut = rest.index('--') if '--' in rest else len(rest)
        return words[1], rest[:cut], rest[cut + 1:]

    def parse(self, head):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            try:
                return self.tool.parser().parse_args(head)
            except SystemExit as e:
                self.fail('tools/machine_lock.py refuses the advised arguments %r (exit %s): %s'
                          % (head, e.code, err.getvalue().strip()))

    def heavy_line(self, command='godot --headless --path .', ctx=None, sub=False):
        d = decide(bash(command, self.w.wt, sub=sub), ctx or self.w.ctx())
        return self.advice(d, 'heavy', 'subagent' if sub else 'main')

    def status_lines(self, ctx=None):
        """The status line of each rule that advises one: lock-mcp, owner-tool, lock-state."""
        ctx = ctx or self.w.ctx()
        state = os.path.join(self.w.home, '.claude', 'machine-lock', 'state.json')
        return [
            self.advice(decide(event(MCP_PREFIX + 'lock_hold_create', {'id': 'h-1'}, self.w.wt, sub=True),
                               ctx), 'lock-mcp', 'subagent'),
            self.advice(decide(bash('python tools/machine_lock.py hold whole --for 1h', self.w.wt, sub=True),
                               ctx), 'owner-tool', 'subagent'),
            self.advice(decide(event('Write', {'file_path': state, 'content': '{}'}, self.w.wt, sub=True),
                               ctx), 'lock-state', 'subagent'),
        ]

    def test_advised_lock_line_is_accepted_by_the_lock_tool(self):
        table = kinds.load()
        for command, kind in HEAVY:
            with self.subTest(command):
                line = self.heavy_line(command)
                _, head, wrapped = self.lock_call(line)
                a = self.parse(head)
                self.assertEqual((a.cmd, a.kind, a.repo, a.slug), ('run', kind, 'demo', 'wt'), line)
                self.assertIn(a.kind, table, 'a kind the lock tool cannot size')
                self.assertEqual(wrapped, shlex.split(command), line)

    def test_advised_status_line_is_accepted_by_the_lock_tool(self):
        lines = self.status_lines()
        self.assertEqual(len(set(lines)), 1, lines)
        script, head, wrapped = self.lock_call(lines[0])
        a = self.parse(head)
        self.assertEqual((a.cmd, a.id, wrapped), ('status', None, []), lines[0])
        home = W.fwd(self.w.home) + '/'
        self.assertFalse(W.fwd(script).startswith(home), 'a tool under the home folder: %s' % script)

    def test_no_advised_line_names_the_home_link_or_the_retired_flag(self):
        for line in [self.heavy_line()] + self.status_lines():
            with self.subTest(line):
                script, head, _ = self.lock_call(line)
                self.assertNotIn('--lock', head)
                self.assertNotIn('.claude', W.fwd(script).split('/'))
                self.assertEqual(W.fwd(script).split('/')[-2:], ['tools', SCRIPT])

    def test_the_guard_on_this_machine_advises_this_checkouts_lock_tool(self):
        # no kit given: the default is the checkout the guard module is in (kitlib.kitroot.root())
        ctx = context.Ctx(environ={}, home=self.w.home, classify=self.w.classify, gate_ok=self.w.gate_ok,
                          git=self.w.git, lock_held=lambda: (False, 'none'), tempdir=self.w.temp)
        for line in (self.heavy_line(ctx=ctx), self.status_lines(ctx)[0]):
            with self.subTest(line):
                script = self.lock_call(line)[0]
                self.assertTrue(os.path.isfile(script), 'no such file: %s' % script)
                self.assertTrue(os.path.samefile(script, TOOL), script)

    def test_a_kit_path_with_a_space_is_one_quoted_word(self):
        kit = os.path.join(self.w.base, 'my kit')
        ctx = self.w.ctx(kit=kit)
        want = '"%s/tools/%s"' % (W.fwd(kit), SCRIPT)
        for line, verb in ((self.heavy_line(ctx=ctx), 'run'), (self.status_lines(ctx)[0], 'status')):
            with self.subTest(verb):
                self.assertTrue(line.startswith('python %s %s' % (want, verb)), line)
                script, head, _ = self.lock_call(line)
                self.assertEqual(script, want.strip('"'))
                self.assertEqual(self.parse(head).cmd, verb)

    def special_kits(self):
        """Kit paths with a character that expands inside double quotes in bash and PowerShell
        (`$`, a backtick); one holds a space as well."""
        return [os.path.join(self.w.base, name, 'kit') for name in ('a$b kit', 'a`b')]

    def advised_lines(self, ctx):
        return [self.heavy_line(ctx=ctx)] + self.status_lines(ctx)

    def test_a_kit_path_with_dollar_or_backtick_is_one_literal_word(self):
        for kit in self.special_kits():
            want = '%s/tools/%s' % (W.fwd(kit), SCRIPT)
            ctx = self.w.ctx(kit=kit)
            for line in self.advised_lines(ctx):
                with self.subTest(line):
                    self.assertTrue(line.startswith("python '%s' " % want), line)
                    script, head, _ = self.lock_call(line)
                    self.assertEqual(script, want)
                    self.assertIn(self.parse(head).cmd, ('run', 'status'))
                    for dialect in (sh.BASH, sh.PS):
                        p, _ = sh.parse(line, dialect, self.w.wt)
                        self.assertEqual(p.unclean, [], dialect)
                        word = p.commands[0].words[1]
                        self.assertEqual((word.text, word.dyn), (want, False), dialect)

    def test_a_kit_path_with_a_single_quote_stays_double_quoted(self):
        # a quote alone: the double-quoted word is literal in both shells, so it is unchanged
        kit = os.path.join(self.w.base, "o'neil", 'kit')
        line = self.heavy_line(ctx=self.w.ctx(kit=kit))
        self.assertTrue(line.startswith('python "%s/tools/%s" run' % (W.fwd(kit), SCRIPT)), line)

    def test_a_kit_path_with_a_quote_and_a_dollar_is_the_known_limit(self):
        # docs/guard.md "Rules", the `heavy` row: no one quoted word is literal in both shells; it stays
        # double-quoted, as before. Asserted so that a change to it is a deliberate one.
        kit = os.path.join(self.w.base, "o'$x", 'kit')
        line = self.heavy_line(ctx=self.w.ctx(kit=kit))
        self.assertTrue(line.startswith('python "%s/tools/%s" run' % (W.fwd(kit), SCRIPT)), line)

    def assert_allowed_when_run(self, kit, sub):
        ctx = self.w.ctx(kit=kit)
        for line in self.advised_lines(ctx):
            for make in (bash, ps):
                with self.subTest(line, shell=make.__name__, sub=sub):
                    d = decide(make(line, self.w.wt, sub=sub), self.w.ctx(kit=kit))
                    self.assertIsNone(d, d and d.reason)

    def test_the_advised_lines_are_allowed_when_run(self):
        # an agent that runs the alternative as written is not denied again, in either shell
        for kit in [self.w.kit_dir, os.path.join(self.w.base, 'my kit')] + self.special_kits():
            self.assert_allowed_when_run(kit, sub=True)

    def test_the_owner_may_run_the_advised_lines(self):
        # a main session nothing marks is the owner's (kitlib.guard.context.OWNER)
        self.assertEqual(self.w.environ, {})
        for kit in [self.w.kit_dir] + self.special_kits():
            self.assert_allowed_when_run(kit, sub=False)

    def test_a_kit_that_is_a_repository_checkout(self):
        # the guard runs from a kit checkout that is itself a repository's main checkout
        self.assertEqual(self.w.classify(self.w.main).kind, repo.MAIN)
        for sub in (True, False):
            self.assert_allowed_when_run(self.w.main, sub=sub)


if __name__ == '__main__':
    unittest.main()
