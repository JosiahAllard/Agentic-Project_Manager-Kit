"""The kit command (tools/kit.py, and `kit` on the PATH) runs a kit tool by name, so the guard reads
`kit X ...` and `python <kit>/tools/kit.py X ...` as `python <kit>/tools/X.py ...` (docs/guard.md
"The kit command"): every rule that knows a kit tool by its file name judges them the same. The other
rules' rows (owner-tool, wrapped-gate, role-lock) are test_kit_tool_forms.py's, whose forms include
both kit spellings."""
import unittest

from kitlib.guard import commands as cm
from kitlib.guard import rules
from kitlib.guard import shell as sh

from . import test_backlog_role as BR
from . import world as W
from .claudehost import decide
from .world import bash, event, ps


def kit_spellings(kit_dir, tool, args):
    """(shell, text) for each spelling of the kit command running `tool` with `args`."""
    kit = W.fwd(kit_dir)
    back = kit.replace('/', '\\')
    out = [('bash', 'python "%s/tools/kit.py" %s %s' % (kit, tool, args)),
           ('bash', 'python %s/tools/kit.py %s %s' % (kit, tool, args)),
           ('bash', 'kit %s %s' % (tool, args)),
           ('bash', 'kit.cmd %s %s' % (tool, args)),
           ('bash', 'python -m tools.kit %s %s' % (tool, args)),
           ('bash', 'python -m runpy tools.kit %s %s' % (tool, args)),
           ('bash', 'git status; kit %s %s' % (tool, args)),
           ('ps', 'python "%s\\tools\\kit.py" %s %s' % (back, tool, args)),
           ('ps', 'kit %s %s' % (tool, args)),
           ('ps', '& kit %s %s' % (tool, args)),
           ('ps', '& "%s/bin/kit.cmd" %s %s' % (kit, tool, args))]
    if '"' not in args:
        out.append(('ps', "Start-Process kit -ArgumentList '%s %s' -Wait" % (tool, args)))
        out.append(('ps', "Start-Process kit -ArgumentList %s -Wait" % ','.join(
            "'%s'" % a for a in [tool] + args.split())))
    return out


class Backlog(BR.Base):
    """backlog-role: a builder may not close a record, through the kit command either."""

    def kit_cmd(self, shell, text):
        return self.cmd(text, tool='Bash' if shell == 'bash' else 'PowerShell')

    def test_a_close_is_refused_in_every_kit_spelling(self):
        self.refused(self.cmd(self.bl('close X-1')))
        for shell, text in kit_spellings(self.w.kit_dir, 'backlog', '-C "%s" close X-1' % self.wt):
            with self.subTest(text):
                self.refused(self.kit_cmd(shell, text))

    def test_a_read_passes_in_every_kit_spelling(self):
        for shell, text in kit_spellings(self.w.kit_dir, 'backlog', '-C "%s" show X-1' % self.wt):
            with self.subTest(text):
                self.allowed(self.kit_cmd(shell, text))

    def test_code_that_runs_the_kit_backlog_is_a_raw_write(self):
        # python code is not read for what it runs; code naming the tool is refused whole
        for text in ('python -c "import subprocess; subprocess.run([\'kit\', \'backlog\', \'close\', \'X-1\'])"',
                     'python -c "import os; os.system(\'kit.cmd backlog close X-1\')"'):
            with self.subTest(text):
                self.refused(self.cmd(text), 'backlog-raw-write')
        self.allowed(self.cmd('python -c "print(\'kit\', \'hub\')"'))

    def test_what_kit_runs_no_tool_for_is_not_read_as_one(self):
        # kit refuses an option before the tool's name, a name it has no tool for, and itself;
        # a computed name is read as python reads a computed script: not at all
        for text in ('kit -h', 'kit', 'kit --x backlog close X-1', 'kit backlogx close X-1',
                     'kit kit backlog close X-1', 'kit $t close X-1', 'python tools/kit.py -h'):
            with self.subTest(text):
                self.allowed(self.cmd(text))


class GateAllow(unittest.TestCase):
    """gate-allow: only a coordinator (or the PM, or the owner) lifts a gate budget."""

    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)

    def cmd(self, shell, text, sub=True, env=None):
        ev = event('Bash' if shell == 'bash' else 'PowerShell', {'command': text, 'description': 'test'},
                   self.w.wt, sub, 'worker')
        return decide(ev, self.w.ctx(environ=env if env is not None else {}))

    def test_a_subagent_is_refused_in_every_kit_spelling(self):
        for shell, text in kit_spellings(self.w.kit_dir, 'gate', 'allow kit/x --runs 8 --why w'):
            with self.subTest(text):
                d = self.cmd(shell, text)
                self.assertIsNotNone(d, 'allowed')
                self.assertEqual((d.verdict, d.rule), ('deny', 'gate-allow'), d.reason)

    def test_a_coordinator_may_allow_through_kit(self):
        from kitlib.guard import context
        for shell, text in kit_spellings(self.w.kit_dir, 'gate', 'allow kit/x --runs 8 --why w'):
            with self.subTest(text):
                d = self.cmd(shell, text, sub=False, env={context.ROLE_ENV: 'coordinator'})
                self.assertIsNone(d, d and d.reason)


class Hub(unittest.TestCase):
    """hub-owner-verb: an owner verb of the hub tool, through the kit command too."""

    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)

    def judge(self, shell, text):
        make = bash if shell == 'bash' else ps
        return decide(make(text, self.w.wt, True), self.w.ctx())

    def test_an_owner_verb_is_refused_in_every_kit_spelling(self):
        for args in ('answer EX-1 --label yes', 'todos done T-1', 'approvals approve A-1'):
            for shell, text in kit_spellings(self.w.kit_dir, 'hub', args):
                with self.subTest(text):
                    d = self.judge(shell, text)
                    self.assertIsNotNone(d, 'allowed')
                    self.assertEqual((d.verdict, d.rule), ('deny', 'hub-owner-verb'), d.reason)

    def test_an_agent_verb_passes(self):
        for shell, text in kit_spellings(self.w.kit_dir, 'hub', 'questions list'):
            with self.subTest(text):
                self.assertIsNone(self.judge(shell, text))


class Unreadable(unittest.TestCase):
    """docs/guard.md "Fallback": a line the parser cannot read that names an owner's tool is
    refused under that tool's rule, the kit command's spelling of the name included."""

    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)

    def test_a_kit_spelling_is_refused_as_the_tool_is(self):
        kit = W.fwd(self.w.kit_dir)
        for tool, args, rule in (('hub', 'questions list "x', 'hub-owner-verb'),
                                 ('machine_lock', 'status "x', 'owner-tool'),
                                 ('machine_setup', '--apply "x', 'owner-tool')):
            for text in ('python "%s/tools/%s.py" %s' % (kit, tool, args),
                         'python "%s/tools/kit.py" %s %s' % (kit, tool, args),
                         'kit %s %s' % (tool, args),
                         'kit.cmd %s %s' % (tool, args),
                         'kit.com %s %s' % (tool, args),
                         'KIT.COM %s %s' % (tool, args),
                         "kit '' %s %s" % (tool, args),
                         'kit \'""%s\' %s' % (tool, args)):
                with self.subTest(text):
                    d = decide(bash(text, self.w.wt, True), self.w.ctx())
                    self.assertIsNotNone(d, 'allowed')
                    self.assertEqual((d.verdict, d.rule), ('deny', rule), d.reason)

    def test_a_run_the_parser_cannot_see_is_read_from_its_kit_word(self):
        # an open quote before it hides the run from the parser: the fallback re-reads the part
        # of the line holding the tool's name from its start, the kit command's word included
        kit = W.fwd(self.w.kit_dir)
        for form in ('python "%s/tools/machine_setup.py"' % kit, 'python "%s/tools/kit.py" machine_setup' % kit,
                     'kit machine_setup', 'kit.cmd machine_setup'):
            text = "echo 'x ; %s --owner-email a@example.invalid --apply" % form
            with self.subTest(text):
                d = decide(bash(text, self.w.wt, True), self.w.ctx())
                self.assertIsNotNone(d, 'allowed')
                self.assertEqual((d.verdict, d.rule), ('deny', 'owner-tool'), d.reason)
                self.assertIn('could not read this command', d.reason)

    def test_a_part_whose_start_is_unreadable_is_read_from_the_tool_s_name_too(self):
        # a computed program or an open quote before the name in the same part: read from the
        # part's start the run is unreadable, read from the name it is the owner's tool
        for shell, text in (('bash', '$PY tools/machine_setup.py --apply'),
                            ('bash', '"$PY" tools/machine_setup.py --owner-email a@example.invalid --apply'),
                            ('bash', "echo 'x python tools/machine_setup.py --apply"),
                            ('ps', '& $py C:\\k\\tools\\machine_setup.py --apply'),
                            ('ps', "echo 'x python C:\\k\\tools\\machine_setup.py --apply")):
            with self.subTest(text):
                ev = (bash if shell == 'bash' else ps)(text, self.w.wt, True)
                d = decide(ev, self.w.ctx())
                self.assertIsNotNone(d, 'allowed')
                self.assertEqual((d.verdict, d.rule), ('deny', 'owner-tool'), d.reason)

    def test_a_hidden_piped_gate_is_judged_the_same_in_every_form(self):
        W.deny_softened()                       # wrapped-gate is off by default (test_cheap.py)
        self.addCleanup(W.allow_unplaced)
        kit, wt = W.fwd(self.w.kit_dir), W.fwd(self.w.wt)
        got = []
        for form in ('python "%s/tools/gate.py"' % kit, 'python "%s/tools/kit.py" gate' % kit, 'kit gate',
                     'kit.cmd gate'):
            text = "echo 'x ; %s -C \"%s\" --full | tail -5" % (form, wt)
            d = decide(bash(text, self.w.wt, False), self.w.ctx())
            got.append(None if d is None else (d.verdict, d.rule))
        self.assertIsNotNone(got[0])
        self.assertEqual(got, [got[0]] * len(got))


class PowerShellSplit(unittest.TestCase):
    """Windows PowerShell 5.1 passes a word holding a double quote on unescaped, so it may split
    into more arguments: the kit command's name is its first token and the rest is still read."""

    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)

    def test_an_owner_verb_after_the_quote_is_refused(self):
        for text in ('kit \'hub" answer EX-1 --label yes\'', '& kit \'hub" todos done T-1\'',
                     # a double quote before the name, or an empty word (5.1 drops it) before it
                     'kit \'""hub\' answer EX-1 --label yes', 'kit \'"hub"\' answer EX-1 --label yes',
                     'kit \'"\' hub answer EX-1 --label yes', "kit '' hub answer EX-1 --label yes",
                     'kit "" hub answer EX-1 --label yes', "& kit '' '\"hub' todos done T-1"):
            with self.subTest(text):
                d = decide(ps(text, self.w.wt, True), self.w.ctx())
                self.assertIsNotNone(d, 'allowed')
                self.assertEqual((d.verdict, d.rule), ('deny', 'hub-owner-verb'), d.reason)


def run_of(text, shell=sh.BASH):
    parsed = cm.read(text, shell, 'C:\\w')
    return [rules.tool_run(cm.invocation(c)) for c in parsed.commands]


class ToolRun(unittest.TestCase):
    """rules.tool_run and commands.tool_at read the kit command's tool as the script it runs."""

    def named(self, text, shell=sh.BASH):
        got = run_of(text, shell)[0]
        return None if got is None else (got[0], [w.text for w in got[1]])

    def test_the_tool_and_its_words(self):
        for text in ('kit backlog close X-1', 'kit.cmd backlog close X-1', 'KIT.EXE backlog close X-1',
                     'C:/k/bin/kit.cmd backlog close X-1', 'python C:/k/tools/kit.py backlog close X-1',
                     'py -3 C:/k/tools/kit.py backlog close X-1', 'python -m tools.kit backlog close X-1',
                     'kit Backlog close X-1'):
            with self.subTest(text):
                self.assertEqual(self.named(text), ('backlog.py', ['close', 'X-1']))

    def test_a_split_or_quoted_name_reads_its_first_token(self):
        self.assertEqual(self.named("Start-Process kit -ArgumentList 'backlog close X-1'", sh.PS)[0], 'backlog.py')
        self.assertEqual(self.named('kit \'backlog" x\' close', sh.PS)[0], 'backlog.py')
        # the name is the first token of any word, a double quote before it and empty words skipped
        for text, want in (('kit \'""backlog\' close X-1', ('backlog.py', ['close', 'X-1'])),
                           ('kit \'"backlog"\' close X-1', ('backlog.py', ['close', 'X-1'])),
                           ('kit \'" backlog close\' X-1', ('backlog.py', ['close', 'X-1'])),
                           ("kit '' backlog close X-1", ('backlog.py', ['close', 'X-1'])),
                           ('kit \'"\' \'\' backlog close X-1', ('backlog.py', ['close', 'X-1'])),
                           ("kit '' $t close", None), ("kit '' -h", ('kit', ['', '-h']))):
            with self.subTest(text):
                self.assertEqual(self.named(text, sh.PS), want)

    def test_tool_at_names_the_word_holding_the_name(self):
        parsed = cm.read("kit '' gate --full", sh.PS, 'C:\\w')
        got = cm.tool_at(cm.invocation(parsed.commands[0]))
        self.assertEqual((got[0], got[1], [w.text for w in got[2]]), (1, 'gate.py', ['--full']))

    def test_no_tool(self):
        self.assertEqual(self.named('kit -h'), ('kit', ['-h']))
        self.assertEqual(self.named('kit'), ('kit', []))
        self.assertIsNone(self.named('kit $t close'))
        self.assertEqual(self.named('python tools/kit.py --x backlog'), ('kit.py', ['--x', 'backlog']))

    def test_script_run_reads_runpy_of_the_kit(self):
        parsed = cm.read('python -m runpy tools.kit backlog close X-1', sh.BASH, 'C:\\w')
        got = rules.script_run(cm.invocation(parsed.commands[0]))
        self.assertEqual((got[0], [w.text for w in got[1]]), ('backlog.py', ['close', 'X-1']))

    def test_tool_at(self):
        for text, want in (('python tools/gate.py --full', (0, 'gate.py', ['--full'])),
                           ('gate.py --full', (-1, 'gate.py', ['--full'])),
                           ('kit gate --full', (0, 'gate.py', ['--full'])),
                           ('python -u tools/kit.py gate --full', (2, 'gate.py', ['--full'])),
                           ('kit -h', None), ('kit', None), ('kit $t', None), ('git status', None)):
            with self.subTest(text):
                parsed = cm.read(text, sh.BASH, 'C:\\w')
                got = cm.tool_at(cm.invocation(parsed.commands[0]))
                self.assertEqual(got and (got[0], got[1], [w.text for w in got[2]]), want)

    def test_wrapper(self):
        for text, want in (('kit gate --full', 'gate'), ('python tools/kit.py gate', 'gate'),
                           ('kit machine_lock run --kind build -- x', 'lock-run'),
                           ('kit machine_lock status', None), ('kit backlog list', None)):
            with self.subTest(text):
                parsed = cm.read(text, sh.BASH, 'C:\\w')
                self.assertEqual(cm.wrapper(cm.invocation(parsed.commands[0])), want)


if __name__ == '__main__':
    unittest.main()
