"""A kit tool run from the kit's folder is judged exactly as the same call through a repository
stub (decision 21 in docs/multi-host-plan.md section 7: a repository holds no stubs, so skills
and roles run `python <kit>/tools/<x>.py -C <worktree>`). The guard knows a kit tool by its
file name, never by a path relative to the checkout: each row's stub form
(`python tools/<x>.py ...`) and its kit forms (the kit's absolute path, quoted and not, with -C
when the tool takes one) get the same verdict and rule, from the shared rules and from the role
policy. The alternatives the guard prints name the kit's tool, never a repository's tools folder."""
import os
import unittest

from . import world as W
from .claudehost import decide as claude_decide
from .test_rolepolicy import kit as policy_kit
from .world import bash, event, ps

from kitlib.guard import decide as guard_decide
from kitlib.host import Action

# (name, tool, -C?, the arguments after the tool and its -C, shell, cwd attribute, subagent)
SHARED = [
    ('gate piped to tail', 'gate', True, '--full 2>&1 | tail -5', 'bash', 'wt', False),
    ('gate piped, subagent from main', 'gate', True, '2>&1 | tail -n 40', 'bash', 'main', True),
    ('gate to a temp log', 'gate', True, '--full > "{temp}/g.log" 2>&1', 'bash', 'wt', False),
    ('gate with ||', 'gate', True, '|| echo failed', 'bash', 'wt', False),
    ('godot as an argument of gate', 'gate', True, '--run godot --headless', 'ps', 'wt', False),
    ('lock run piped', 'machine_lock', False, 'run --kind engine-test --repo demo --slug wt -- python -m unittest '
     '2>&1 | Select-Object -Last 20', 'ps', 'wt', False),
    ('lock run of godot', 'machine_lock', False, 'run --kind engine-run --repo demo --slug wt -- godot --headless',
     'bash', 'wt', False),
    ('lock owner write, subagent', 'machine_lock', False, 'hold whole', 'bash', 'wt', True),
    ('lock status, subagent', 'machine_lock', False, 'status', 'bash', 'wt', True),
    ('land from a worktree', 'land', True, 'kit/wt --expect-main <sha> --expect-tip <sha>', 'bash', 'wt', True),
]
# (name, role, tool, -C?, arguments, cwd attribute)
POLICY = [
    ('a read role runs the lock', 'reviewer', 'machine_lock', False, 'run --kind build -- x', 'wt'),
    ('a read role reads the lock', 'reviewer', 'machine_lock', False, 'status', 'wt'),
    ('a lander runs a gate under the lock', 'lander', 'machine_lock', False,
     'run --kind build -- python tools/gate.py', 'main'),
    ('a lander holds the lock', 'lander', 'machine_lock', False, 'hold agent', 'main'),
    ('a read role gates to a log in its checkout', 'reviewer', 'gate', True, '--full > gate.log', 'wt'),
    ('a read role gates', 'reviewer', 'gate', True, '--full', 'wt'),
    ('a test role gates to a temp log', 'test-runner', 'gate', True, '--full > "{temp}/g.log" 2>&1', 'wt'),
    ('a read role reviews', 'reviewer', 'review_pack', True, 'kit/wt', 'wt'),
]


def forms(w, tool, takes_c, args, cwd):
    """The stub form, then each kit form, of one call."""
    kit, here = W.fwd(w.kit_dir), W.fwd(cwd)
    args = args.format(temp=W.fwd(w.temp))
    c = ' -C "%s"' % here if takes_c else ''
    return ['python tools/%s.py %s' % (tool, args),
            'python "%s/tools/%s.py"%s %s' % (kit, tool, c, args),
            'python %s/tools/%s.py%s %s' % (kit, tool, c, args)]


def verdict(d):
    return ('allow', None) if d is None or d.verdict == 'allow' else (d.verdict, d.rule)


class SharedRules(unittest.TestCase):
    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)
        W.deny_softened()                       # the rows pin wrapped-gate, off by default (test_cheap.py)
        self.addCleanup(W.allow_unplaced)

    def test_each_kit_form_is_judged_as_the_stub_form(self):
        for name, tool, takes_c, args, shell, where, sub in SHARED:
            cwd = getattr(self.w, where)
            make = bash if shell == 'bash' else ps
            got = []
            for text in forms(self.w, tool, takes_c, args, cwd):
                got.append(verdict(claude_decide(make(text, cwd, sub=sub), self.w.ctx())))
            with self.subTest(name):
                self.assertEqual(got[1:], [got[0]] * 2, got)

    def test_the_rows_do_test_something(self):
        """The parity holds for refusals, not only for calls nothing judges."""
        seen = set()
        for name, tool, takes_c, args, shell, where, sub in SHARED:
            cwd = getattr(self.w, where)
            make = bash if shell == 'bash' else ps
            seen.add(verdict(claude_decide(make(forms(self.w, tool, takes_c, args, cwd)[1], cwd, sub=sub),
                                           self.w.ctx())))
        self.assertTrue({('deny', 'wrapped-gate'), ('deny', 'owner-tool'), ('allow', None)} <= seen, seen)

    def test_a_wrapped_kit_gate_keeps_its_own_path_in_the_alternative(self):
        cmd = forms(self.w, 'gate', True, '--full 2>&1 | tail -5', self.w.wt)[1]
        d = claude_decide(bash(cmd, self.w.wt), self.w.ctx())
        self.assertEqual(d.rule, 'wrapped-gate', d.reason)
        # bare: no redirection after it, run in the background
        self.assertEqual(d.instead, 'python "%s/tools/gate.py" -C "%s" --full' % (W.fwd(self.w.kit_dir), W.fwd(self.w.wt)))
        self.assertIn('in the background', d.reason)


# (name, tool, -C?, the arguments after the tool and its -C, the verdict): a kit tool's output may be
# piped like any program's, except a run that holds the machine or waits on it (docs/guard.md
# "Rules", wrapped-gate)
PIPED = [
    ('gate help piped', 'gate', True, '--help | head', ('allow', None)),
    ('gate plan piped', 'gate', True, '--plan | head', ('allow', None)),
    ('backlog list piped', 'backlog', True, 'list | head', ('allow', None)),
    ('hub questions list piped', 'hub', False, 'questions list | head', ('allow', None)),
    ('gate full piped', 'gate', True, '--full | tail', ('deny', 'wrapped-gate')),
    ('gate land piped', 'gate', True, 'land kit/wt | tail', ('deny', 'wrapped-gate')),
]


class KitToolsMayBePiped(unittest.TestCase):
    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)
        W.deny_softened()                       # wrapped-gate is off by default (test_cheap.py)
        self.addCleanup(W.allow_unplaced)

    def test_a_kit_tool_is_piped_unless_its_run_holds_the_machine(self):
        for sub in (False, True):
            for name, tool, takes_c, args, want in PIPED:
                for text in forms(self.w, tool, takes_c, args, self.w.wt):
                    with self.subTest(name, sub=sub, text=text):
                        self.assertEqual(verdict(claude_decide(bash(text, self.w.wt, sub=sub), self.w.ctx())),
                                         want)


class KitToolChecked(unittest.TestCase):
    """A kit tool PowerShell may hand other words than the guard read (rules.python_unread) acts on
    the checkout its one literal absolute -C names, so that folder is judged in place of the one
    the call runs in. Not when an unsure word comes before the -C value or may split into a word
    that starts with a dash, the script is not the kit's, or Start-Process runs it."""

    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)
        W.deny_softened()                       # unplaced warns by default (test_cheap.py)
        self.addCleanup(W.allow_unplaced)
        self.kit, self.wt, self.main = W.fwd(self.w.kit_dir), W.fwd(self.w.wt), W.fwd(self.w.main)

    def judge(self, text, cwd):
        return claude_decide(ps(text, cwd, sub=True), self.w.ctx())

    def test_kit_tool_dash_c_main_checkout_judged_at_c_folder(self):
        for c in ("-C '%s'", '-C %s', "-C'%s'"):
            for tail in ("-m 'a \"b\" c' x.py", "-m '' x.py", "-m 'fix \"x\"' -b 'Part-of: y' x.py"):
                text = "python %s/tools/commit.py %s %s" % (self.kit, c % self.wt, tail)
                with self.subTest(text):
                    self.assertIsNone(self.judge(text, self.w.main))
        d = self.judge("python %s/tools/commit.py -C '%s' -m 'a \"b\" c' x.py" % (self.kit, self.main), self.w.wt)
        self.assertEqual(verdict(d), ('deny', 'unplaced'))
        self.assertIn(self.main, W.fwd(d.reason))
        self.assertIn(self.wt, d.instead)

    def test_where_the_c_may_not_be_python_s_the_working_directory_is_judged(self):
        for text in ("python %s/tools/commit.py -m '' -C '%s' x.py" % (self.kit, self.wt),
                     "python %s/tools/commit.py -C '%s' -m 'a \"b -C c\"' x.py" % (self.kit, self.wt),
                     "python %s/tools/commit.py -C '%s' -C '%s' -m '' x.py" % (self.kit, self.wt, self.wt),
                     "python %s/tools/x.py -C '%s' -m '' x.py" % (self.wt, self.wt),
                     "python tools/commit.py -C '%s' -m '' x.py" % self.wt,
                     "python %s/tools/commit.py -C wt -m '' x.py" % self.kit,
                     "Start-Process python -ArgumentList '%s/tools/commit.py','-C','%s'" % (self.kit, self.wt)):
            with self.subTest(text):
                self.assertEqual(verdict(self.judge(text, self.w.main)), ('deny', 'unplaced'))


class RolePolicy(unittest.TestCase):
    def setUp(self):
        self.w = W.World(kit=policy_kit())
        self.addCleanup(self.w.cleanup)

    def shell(self, command, role, cwd):
        return Action('shell', tool='Shell', command=command, cwd=cwd, shell='bash', agent='a1',
                      agent_role=role, workdir=None)

    def test_each_kit_form_is_judged_as_the_stub_form(self):
        seen = set()
        for name, role, tool, takes_c, args, where in POLICY:
            cwd = getattr(self.w, where)
            got = [verdict(guard_decide(self.shell(text, role, cwd), self.w.ctx()))
                   for text in forms(self.w, tool, takes_c, args, cwd)]
            seen.update(got)
            with self.subTest(name):
                self.assertEqual(got[1:], [got[0]] * 2, got)
        self.assertTrue({('deny', 'role-lock'), ('deny', 'write-jail'), ('allow', None)} <= seen, seen)

    def test_a_lock_run_of_the_kit_gate_is_a_lander_s(self):
        gate = 'python "%s/tools/gate.py" -C "%s" --full' % (W.fwd(self.w.kit_dir), W.fwd(self.w.main))
        cmd = 'python "%s/tools/machine_lock.py" run --kind build -- %s' % (W.fwd(self.w.kit_dir), gate)
        self.assertEqual(verdict(guard_decide(self.shell(cmd, 'lander', self.w.main), self.w.ctx())), ('allow', None))


class LookalikeWrappers(unittest.TestCase):
    """Only the kit's own gate.py and `machine_lock.py run` may run an interpreter a repository
    lists as heavy (they take the machine lock themselves). The kit is known by its resolved
    folder, not by the file name: a branch's copy or a script in temp named gate.py is any script."""

    def setUp(self):
        self.w = W.World(kit={'repo': {'name': 'demo'},
                              'heavy': {'programs': [{'program': 'python*', 'kind': 'build', 'why': 'test'}]}})
        self.addCleanup(self.w.cleanup)
        W.deny_softened()                       # the cases pin heavy, a warning by default (test_cheap.py)
        self.addCleanup(W.allow_unplaced)

    def judge(self, text):
        return verdict(claude_decide(bash(text, self.w.wt), self.w.ctx()))

    def test_the_kit_s_wrappers_run_a_heavy_interpreter(self):
        kit, wt = W.fwd(self.w.kit_dir), W.fwd(self.w.wt)
        self.assertEqual(self.judge('python "%s/tools/gate.py" -C "%s" --full' % (kit, wt)), ('allow', None))
        self.assertEqual(self.judge('python "%s/tools/machine_lock.py" run --kind build -- python x.py' % kit),
                         ('allow', None))

    def test_a_detached_lock_run_is_the_kit_s_wrapper_too(self):
        # The carrier chain WMI starts for --detach (pythonw no_window.py python machine_lock.py
        # run ...) is never a shell call the guard sees; the call it does see is this one.
        kit = W.fwd(self.w.kit_dir)
        self.assertEqual(self.judge('python "%s/tools/machine_lock.py" run --detach --kind build -- python x.py'
                                    % kit), ('allow', None))

    def test_a_script_that_shares_the_name_is_any_script(self):
        temp = W.fwd(self.w.temp)
        for text in ('python x.py',
                     'python "%s/gate.py" --full' % temp,
                     'python tools/gate.py --full',
                     'python "%s/tools/gate.py" --full' % W.fwd(self.w.wt),
                     'python "%s/machine_lock.py" run --kind build -- python x.py' % temp,
                     'python "%s/kit/../tools/gate.py" --full' % W.fwd(self.w.base)):
            with self.subTest(text):
                self.assertEqual(self.judge(text), ('deny', 'heavy'))

    def test_a_start_process_script_is_read_from_its_working_directory(self):
        # python reads a relative script from the folder Start-Process starts it in, not the call's
        kit = W.fwd(self.w.kit_dir)
        sp = "Start-Process python -WorkingDirectory %s -ArgumentList '%s','--full'"
        self.assertEqual(verdict(claude_decide(ps(sp % ("'%s'" % kit, 'tools/gate.py'), self.w.wt), self.w.ctx())),
                         ('allow', None))
        for workdir in ('$k', "'%s'" % W.fwd(self.w.temp), "'%s'" % W.fwd(self.w.wt)):
            text = sp % (workdir, 'tools/gate.py')
            with self.subTest(text):
                self.assertEqual(verdict(claude_decide(ps(text, self.w.wt), self.w.ctx())), ('deny', 'heavy'))
        # an absolute script is the kit's wherever it starts
        text = sp % ('$k', '%s/tools/gate.py' % kit)
        self.assertEqual(verdict(claude_decide(ps(text, self.w.wt), self.w.ctx())), ('allow', None))

    def test_a_start_process_script_elsewhere_is_not_the_kit_s(self):
        # the same call from the kit's folder, started in temp, runs temp's tools/gate.py
        from kitlib.guard import commands as cm
        from kitlib.guard import rules
        from kitlib.guard import shell as sh
        text = "Start-Process python -WorkingDirectory '%s' -ArgumentList 'tools/gate.py','--full'" % W.fwd(self.w.temp)
        c = cm.read(text, sh.PS, self.w.kit_dir).commands[0]
        self.assertIsNone(rules.kit_wrapper(self.w.ctx(), c, cm.invocation(c), self.w.kit_dir))
        c = cm.read(text.replace(W.fwd(self.w.temp), W.fwd(self.w.kit_dir)), sh.PS, self.w.wt).commands[0]
        self.assertTrue(rules.kit_wrapper(self.w.ctx(), c, cm.invocation(c), self.w.wt))


class Alternatives(unittest.TestCase):
    """What the guard tells an agent to run names the kit's tool with -C, never `tools/<x>.py`."""

    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)

    def test_no_alternative_names_a_repository_tools_folder(self):
        from kitlib.guard import rules
        kit = W.fwd(self.w.kit_dir)
        os.makedirs(os.path.dirname(self.w.ledger))
        self.w.user_config({'ledger_path': self.w.ledger})
        ctx = self.w.ctx()
        self.assertEqual(rules.kit_tool(ctx, 'land'), '"%s/tools/land.py"' % kit)
        d = claude_decide(event('Write', {'file_path': self.w.ledger, 'content': ''}, self.w.plain), ctx)
        self.assertIn('Instead: python "%s/tools/ledger.py" add ' % kit, d.reason)
        self.assertNotIn('python tools/', d.reason)


if __name__ == '__main__':
    unittest.main()
