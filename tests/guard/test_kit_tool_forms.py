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
        self.assertTrue({('deny', 'wrapped-gate'), ('deny', 'owner-lock'), ('allow', None)} <= seen, seen)

    def test_a_wrapped_kit_gate_keeps_its_own_path_in_the_alternative(self):
        cmd = forms(self.w, 'gate', True, '--full 2>&1 | tail -5', self.w.wt)[1]
        d = claude_decide(bash(cmd, self.w.wt), self.w.ctx())
        self.assertEqual(d.rule, 'wrapped-gate', d.reason)
        # bare: no redirection after it, run in the background
        self.assertEqual(d.instead, 'python "%s/tools/gate.py" -C "%s" --full' % (W.fwd(self.w.kit_dir), W.fwd(self.w.wt)))
        self.assertIn('in the background', d.reason)


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
