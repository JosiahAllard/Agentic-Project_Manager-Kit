"""kitlib.stubs (tools/kit_stubs.py): writing and checking a repository's stubs, and what a written
stub runs. The PM kit's kit_stubs.py had no tests; these are new.

Every case runs against a throwaway repository (tests/gitfixture.py). A stub runs this kit's own
tools (landed.py, read-only) on that repository; the fake kits are folders in the fixture's temp.
"""
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

from kitlib import stubs
from tests import gitfixture

KIT = stubs.KIT_ROOT
TOOL = os.path.join(KIT, 'tools', 'kit_stubs.py')


class StubCase(unittest.TestCase):

    def setUp(self):
        env = mock.patch.dict(os.environ, gitfixture.fixture_env(), clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.fx = gitfixture.TempRepo()
        self.addCleanup(self.fx.cleanup)
        self.tmp = os.path.realpath(self.fx.base)
        self.root = os.path.realpath(self.fx.root)

    def config(self, **sections):
        with open(os.path.join(self.root, '.kit.json'), 'w', encoding='utf-8') as fh:
            json.dump(sections, fh)

    def run_stubs(self, verb, root=None, **kw):
        got = stubs.run(root or self.root, verb, **kw)
        return got.code, '\n'.join(got.lines)

    def stub(self, name, rel='tools', root=None):
        return os.path.join(root or self.root, *rel.split('/'), name + '.py')

    def read(self, path):
        with open(path, encoding='utf-8', newline='') as fh:
            return fh.read()

    def put(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(text)

    def call(self, args, cwd):
        # A stub's kit tool may look up the home (landed.py: the kit temp), and under the home fence
        # (docs/test-tools.md "Home fence") the real one is refused: the fixture's folder is its home.
        done = subprocess.run([sys.executable] + list(args), cwd=cwd, capture_output=True,
                              text=True, encoding='utf-8', errors='replace',
                              env=gitfixture.fixture_env({'USERPROFILE': self.tmp, 'HOME': self.tmp}),
                              timeout=120)
        return done.returncode, (done.stdout or '') + (done.stderr or '')


class WhichTools(StubCase):

    def fake_kit(self):
        kit = os.path.join(self.tmp, 'fake-kit')
        self.put(os.path.join(kit, 'tools', 'cmd_a.py'), "if __name__ == '__main__':\n    pass\n")
        self.put(os.path.join(kit, 'tools', 'lib_b.py'), 'X = 1\n')
        self.put(os.path.join(kit, 'tools', 'kit_stubs.py'), 'if __name__ == "__main__":\n    pass\n')
        return kit

    def test_commands_are_the_tools_with_a_main_block_but_kit_stubs(self):
        self.assertEqual(stubs.commands(self.fake_kit()), ['cmd_a'])
        have = stubs.commands()
        self.assertIn('landed', have)
        self.assertNotIn('kit_stubs', have)

    def test_the_default_is_the_shared_tools_the_kit_has(self):
        self.assertEqual(self.run_stubs('write')[0], stubs.OK)
        want = [n for n in stubs.SHARED if n in stubs.commands()]
        self.assertIn('landed', want)
        self.assertEqual(sorted(n[:-3] for n in os.listdir(os.path.join(self.root, 'tools'))),
                         sorted(want))

    def test_stubs_tools_selects(self):
        self.config(stubs={'tools': ['landed']})
        code, out = self.run_stubs('write')
        self.assertEqual(code, stubs.OK, out)
        self.assertEqual(os.listdir(os.path.join(self.root, 'tools')), ['landed.py'])

    def test_the_tools_argument_wins_over_the_config(self):
        self.config(stubs={'tools': ['landed']})
        self.run_stubs('write', tools=['gate'])
        self.assertEqual(os.listdir(os.path.join(self.root, 'tools')), ['gate.py'])

    def test_a_tool_the_kit_does_not_have_is_refused(self):
        self.config(stubs={'tools': ['landed', 'no_such_tool']})
        code, out = self.run_stubs('write')
        self.assertEqual(code, stubs.CONFIG, out)
        self.assertIn('kit_stubs: refused:', out)
        self.assertIn('no_such_tool', out)
        self.assertFalse(os.path.exists(os.path.join(self.root, 'tools')))
        code, out = self.run_stubs('write', tools=['kit_stubs'])
        self.assertEqual(code, stubs.CONFIG, out)

    def test_a_bad_stubs_section_is_refused(self):
        self.config(stubs={'tools': 'landed'})
        self.assertEqual(self.run_stubs('check')[0], stubs.CONFIG)
        self.config(stubs={'tool': ['landed']})
        self.assertEqual(self.run_stubs('check')[0], stubs.CONFIG)


class RetiredTools(StubCase):
    """A tool the kit no longer has (stubs.RETIRED). A "stubs".tools list written before the
    removal still loads, and a stub of it left in the repository is named, never deleted."""

    GONE = 'ruling_cites'

    def test_the_kit_has_no_command_of_a_retired_name(self):
        self.assertNotIn(self.GONE, stubs.commands())
        self.assertIn(self.GONE, stubs.RETIRED)
        listed = set(stubs.SHARED) | set(stubs.KIT_ONLY) | set(stubs.commands())
        self.assertEqual(sorted(set(stubs.RETIRED) & listed), [])

    def test_a_retired_name_in_the_config_is_dropped_and_no_stub_is_written(self):
        self.config(stubs={'tools': ['landed', self.GONE]})
        code, out = self.run_stubs('write')
        self.assertEqual(code, stubs.OK, out)
        self.assertEqual(os.listdir(os.path.join(self.root, 'tools')), ['landed.py'])
        self.assertNotIn('retired', out)
        code, out = self.run_stubs('check')
        self.assertEqual(code, stubs.OK, out)
        self.assertIn('kit_stubs: 1 stub(s) current', out)
        self.assertIsNone(stubs.problem_line(self.root))

    def test_a_stub_of_it_left_behind_is_a_note_and_no_problem(self):
        self.config(stubs={'tools': ['landed', self.GONE]})
        left = self.stub(self.GONE)
        self.put(left, stubs.stub_text(self.GONE))
        for verb, last in (('write', None), ('check', 'kit_stubs: 1 stub(s) current')):
            code, out = self.run_stubs(verb)
            self.assertEqual(code, stubs.OK, (verb, out))
            self.assertIn('retired  %s (delete it)' % left, out.splitlines())
            if last:
                self.assertEqual(out.splitlines()[-1], last)
        self.assertEqual(self.read(left), stubs.stub_text(self.GONE))    # nothing deleted it
        self.assertIsNone(stubs.problem_line(self.root))

    def test_the_note_is_printed_whatever_names_the_tools(self):
        left = self.stub(self.GONE)
        self.put(left, stubs.stub_text(self.GONE))
        code, out = self.run_stubs('write', tools=['landed'])
        self.assertEqual(code, stubs.OK, out)
        self.assertIn('retired  %s (delete it)' % left, out.splitlines())
        code, out = self.run_stubs('check', tools=['landed', 'gate'])      # a problem keeps its code
        self.assertEqual(code, stubs.PROBLEMS, out)
        self.assertIn('retired  %s (delete it)' % left, out.splitlines())
        self.assertEqual(out.splitlines()[-1], 'kit_stubs: 1 problem(s)')

    def test_a_retired_name_given_as_the_tools_argument_is_refused(self):
        code, out = self.run_stubs('write', tools=[self.GONE])
        self.assertEqual(code, stubs.CONFIG, out)
        self.assertIn(self.GONE, out)
        self.assertFalse(os.path.exists(os.path.join(self.root, 'tools')))


class RetiredToolsUnchanged(StubCase):
    """What the retirement leaves as it was."""

    def test_the_repositorys_own_file_of_a_retired_name_is_not_named(self):
        self.config(stubs={'tools': ['landed']})
        own = "print('the repository\\'s own copy')\n"
        for name in stubs.RETIRED:
            self.put(self.stub(name), own)
        code, out = self.run_stubs('write')
        self.assertEqual(code, stubs.OK, out)
        self.assertNotIn('retired', out)
        for name in stubs.RETIRED:
            self.assertEqual(self.read(self.stub(name)), own)

    def test_an_unknown_name_beside_a_retired_one_is_still_refused(self):
        self.config(stubs={'tools': ['landed', 'no_such_tool'] + list(stubs.RETIRED)})
        code, out = self.run_stubs('write')
        self.assertEqual(code, stubs.CONFIG, out)
        self.assertIn('no_such_tool', out)
        for name in stubs.RETIRED:
            self.assertNotIn(name, out)


class Partition(StubCase):
    """Every tools/*.py command is stubbed (SHARED) or kit-only (KIT_ONLY), exactly one."""

    def test_every_kit_command_is_in_exactly_one_list(self):
        both = sorted(set(stubs.SHARED) & set(stubs.KIT_ONLY))
        self.assertEqual(both, [], 'in both SHARED and KIT_ONLY')
        self.assertEqual(stubs.unclassified(), [],
                         'add each new tool to kitlib.stubs.SHARED (it acts on a repository checkout '
                         'named by -C or the current directory) or KIT_ONLY (machine, owner or kit)')
        self.assertEqual(sorted(set(stubs.SHARED) | set(stubs.KIT_ONLY)), sorted(stubs.commands()),
                         'a listed name is not a kit command')

    def test_unclassified_names_a_new_tool(self):
        kit = os.path.join(self.tmp, 'fake-kit')
        for n in ('landed', 'machine_lock', 'zzz_new'):
            self.put(os.path.join(kit, 'tools', n + '.py'), "if __name__ == '__main__':\n    pass\n")
        self.assertEqual(stubs.unclassified(kit), ['zzz_new'])


class ProblemLine(StubCase):

    def test_none_when_current(self):
        self.assertEqual(self.run_stubs('write', tools=['landed'])[0], stubs.OK)
        self.config(stubs={'tools': ['landed']})
        self.assertIsNone(stubs.problem_line(self.root))

    def test_names_missing_and_gives_the_write_command(self):
        self.config(stubs={'tools': ['landed', 'gate']})
        self.assertEqual(self.run_stubs('write', tools=['landed'])[0], stubs.OK)
        line = stubs.problem_line(self.root)
        self.assertEqual(line, 'stubs (%s): 1 missing/stale: gate; run python %s write -C %s' % (
            os.path.basename(self.root), os.path.join(KIT, 'tools', 'kit_stubs.py'), self.root))

    def test_a_stub_running_another_kit_carries_the_hint(self):
        self.config(stubs={'tools': ['landed']})
        other = os.path.join(self.tmp, 'other-kit')
        self.put(self.stub('landed'), stubs.stub_text('landed', 'tools', other))
        line = stubs.problem_line(self.root)
        self.assertIn('1 missing/stale: landed;', line)
        self.assertTrue(line.endswith('(stale ones run the kit at %s)' % os.path.normpath(other)), line)
        self.assertNotIn("'", line)
        self.assertNotIn('\\\\', line)

    def test_a_config_the_kit_refuses_is_a_could_not_check_line(self):
        self.config(stubs={'tools': ['no_such_tool']})
        self.assertTrue(stubs.problem_line(self.root).startswith(
            'could not check stubs (%s): ' % os.path.basename(self.root)))

    def test_a_repositorys_own_tools_are_no_problem(self):
        for name in stubs.SHARED:
            self.put(self.stub(name), '# the repository\'s own %s\n' % name)
        self.assertIsNone(stubs.problem_line(self.root))
        code, out = self.run_stubs('check')        # check still names them
        self.assertEqual(code, stubs.PROBLEMS, out)
        self.assertIn('not a stub  %s' % self.stub('landed'), out)

    def test_the_kits_own_repository_is_silent(self):
        self.assertIsNone(stubs.problem_line(stubs.KIT_ROOT))


class WriteAndCheck(StubCase):

    def setUp(self):
        super().setUp()
        self.config(stubs={'tools': ['landed', 'gate']})

    def test_write_then_check(self):
        code, out = self.run_stubs('write')
        self.assertEqual(code, stubs.OK, out)
        self.assertIn('wrote    %s' % self.stub('landed'), out)
        self.assertEqual(self.read(self.stub('landed')), stubs.stub_text('landed'))
        code, out = self.run_stubs('write')
        self.assertIn('ok       %s' % self.stub('landed'), out)
        code, out = self.run_stubs('check')
        self.assertEqual(code, stubs.OK, out)
        self.assertIn('kit_stubs: 2 stub(s) current', out)

    def test_check_names_a_missing_stub(self):
        self.run_stubs('write', tools=['landed'])
        code, out = self.run_stubs('check')
        self.assertEqual(code, stubs.PROBLEMS, out)
        self.assertIn('missing  %s' % self.stub('gate'), out)
        self.assertIn('kit_stubs: 1 problem(s)', out)

    def test_check_names_a_stale_stub(self):
        self.run_stubs('write')
        self.put(self.stub('gate'), self.read(self.stub('gate')) + '# edited\n')
        code, out = self.run_stubs('check')
        self.assertEqual(code, stubs.PROBLEMS, out)
        self.assertIn('stale    %s' % self.stub('gate'), out)
        self.assertEqual(self.run_stubs('write')[0], stubs.OK)
        self.assertEqual(self.run_stubs('check')[0], stubs.OK)

    def test_a_stub_running_another_kit_is_stale(self):
        other = os.path.join(self.tmp, 'other-kit')
        self.put(self.stub('gate'), stubs.stub_text('gate', kit=other))
        self.run_stubs('write', tools=['landed'])
        code, out = self.run_stubs('check')
        self.assertEqual(code, stubs.PROBLEMS, out)
        self.assertIn('stale    %s (runs the kit at %r)' % (self.stub('gate'), other), out)

    def test_a_file_that_is_not_a_stub_is_kept_unless_replace(self):
        own = "print('the repository\\'s own copy')\n"
        self.put(self.stub('gate'), own)
        code, out = self.run_stubs('write')
        self.assertEqual(code, stubs.PROBLEMS, out)
        self.assertIn('not a stub (kept; --replace to overwrite)  %s' % self.stub('gate'), out)
        self.assertEqual(self.read(self.stub('gate')), own)
        self.assertIn('not a stub  %s' % self.stub('gate'), self.run_stubs('check')[1])
        code, out = self.run_stubs('write', replace=True)
        self.assertEqual(code, stubs.OK, out)
        self.assertEqual(self.read(self.stub('gate')), stubs.stub_text('gate'))

    def test_dry_run_writes_nothing(self):
        code, out = self.run_stubs('write', dry_run=True)
        self.assertEqual(code, stubs.OK, out)
        self.assertIn('would write  %s' % self.stub('landed'), out)
        self.assertFalse(os.path.exists(os.path.join(self.root, 'tools')))

    def test_the_tools_dir_comes_from_the_config(self):
        self.config(repo={'tools_dir': 'scripts/kit'}, stubs={'tools': ['landed']})
        self.run_stubs('write')
        path = self.stub('landed', 'scripts/kit')
        self.assertEqual(self.read(path), stubs.stub_text('landed', 'scripts/kit'))
        self.assertIn("'../..'", self.read(path))

    def test_the_stub_names_the_kit_and_no_environment(self):
        """No variable decides what a stub runs or where: it reads none. It passes the caller's
        directory on in one (test_the_caller_directory_is_passed_on_only_when_the_stub_moves)."""
        text = stubs.stub_text('landed')
        self.assertIn('KIT = %r' % os.path.normpath(KIT), text)
        self.assertNotRegex(text, r'environ\.get|environ\[|getenv|environ\.setdefault')
        self.assertEqual(text.count('environ'), 1)          # the child's copy: dict(os.environ)
        self.assertIn('env = dict(os.environ)', text)
        self.assertEqual(stubs.stub_kit(text), repr(os.path.normpath(KIT)))


class AStubRunsTheKitTool(StubCase):
    """A written stub runs this kit's landed.py (read-only) on the stub's own checkout."""

    def setUp(self):
        super().setUp()
        self.config(stubs={'tools': ['landed']})
        self.run_stubs('write')

    def test_from_inside_the_checkout_and_from_outside(self):
        head = self.fx.git('rev-parse', 'HEAD')
        sub = os.path.join(self.root, 'sub')
        os.makedirs(sub)
        for cwd in (self.root, sub, self.tmp):
            code, out = self.call([self.stub('landed'), 'main'], cwd)
            self.assertEqual(code, 0, (cwd, out))
            self.assertIn('tip        %s  main' % head[:12], out)
            self.assertIn('LANDED', out)

    def test_a_worktrees_stub_acts_on_that_worktree(self):
        self.fx.git('add', '.kit.json', 'tools')
        self.fx.git('commit', '-q', '-m', 'stubs')
        wt = os.path.realpath(self.fx.worktree('wt'))
        self.fx.write('w.txt', 'w\n', root=wt)
        tip = self.fx.commit('wt work', 'w.txt', cwd=wt)
        code, out = self.call([self.stub('landed', root=wt), 'HEAD'], self.tmp)
        self.assertEqual(code, 1, out)
        self.assertIn('tip        %s  HEAD' % tip[:12], out)

    def test_the_exit_code_is_the_tools(self):
        code, out = self.call([self.stub('landed'), 'no-such-branch'], self.root)
        self.assertEqual(code, 2, out)
        self.assertIn('CANNOT ANSWER', out)

    def test_the_caller_directory_is_passed_on_only_when_the_stub_moves(self):
        """Moved out of the caller's directory, the child gets [stub checkout, caller directory]
        in KIT_STUB_CALLER; run where it was called, it gets no such variable, even one the
        caller had."""
        kit = os.path.join(self.tmp, 'echo-kit')
        self.put(os.path.join(kit, 'tools', 'echo_env.py'),
                 'import json, os\nprint(json.dumps([os.getcwd(), '
                 'os.environ.get("KIT_STUB_CALLER")]))\n')
        stub = self.stub('echo_env')
        self.put(stub, stubs.stub_text('echo_env', kit=kit))
        sub = os.path.join(self.root, 'sub')
        os.makedirs(sub)
        code, out = self.call([stub], self.tmp)
        self.assertEqual(code, 0, out)
        cwd, var = json.loads(out)
        self.assertTrue(os.path.samefile(cwd, self.root), out)
        self.assertEqual(json.loads(var), [os.path.normpath(self.root), self.tmp])
        env = dict(gitfixture.fixture_env(), KIT_STUB_CALLER='["stale", "stale"]')
        done = subprocess.run([sys.executable, stub], cwd=sub, capture_output=True, text=True,
                              env=env, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        cwd, var = json.loads(done.stdout)
        self.assertTrue(os.path.samefile(cwd, sub), done.stdout)
        self.assertIsNone(var)

    def test_a_missing_kit_tool_exits_2(self):
        gone = os.path.join(self.tmp, 'gone-kit')
        self.put(self.stub('landed'), stubs.stub_text('landed', kit=gone))
        code, out = self.call([self.stub('landed'), 'main'], self.root)
        self.assertEqual(code, 2, out)
        self.assertIn('refused: no kit tool at %s' % os.path.join(gone, 'tools', 'landed.py'), out)


class TheCommandLine(StubCase):

    def test_write_and_check_with_minus_c(self):
        code, out = self.call([TOOL, 'write', '-C', self.root, '--tools', 'landed'], self.tmp)
        self.assertEqual(code, 0, out)
        self.assertIn('wrote    %s' % self.stub('landed'), out)
        code, out = self.call([TOOL, 'check', '--tools', 'landed'], self.root)
        self.assertEqual(code, 0, out)
        code, out = self.call([TOOL, 'check', '--tools', 'landed,gate'], self.root)
        self.assertEqual(code, 1, out)

    def test_usage(self):
        for args in ([], ['remove'], ['check', '--replace'], ['check', '--dry-run'],
                     ['write', '--tools', 'landed,,gate'], ['write', '-C', self.tmp]):
            code, out = self.call([TOOL] + args, self.root)
            self.assertEqual(code, 2, (args, out))
            self.assertIn('USAGE:', out)

    def test_a_config_refusal_exits_2(self):
        code, out = self.call([TOOL, 'write', '--tools', 'no_such_tool'], self.root)
        self.assertEqual(code, 2, out)
        self.assertIn('kit_stubs: refused:', out)


if __name__ == '__main__':
    unittest.main()
