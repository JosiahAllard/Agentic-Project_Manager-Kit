"""Every place the kit starts a process says what happens to the console window.

A console program started by a process with no console (pythonw.exe running a hook, a scheduled
task) gets a console window of its own unless its start carries a creation flag
(kitlib/proc/job.py, "No console window from a parent without a console"). So every start in the
kit's code goes one of three ways, and this test reads the source to hold that:

- through kitlib.proc.spawn (run, call, popen), which adds the flag when there is no console;
- a subprocess call with an explicit `creationflags=` keyword (the site made its own choice);
- a row in EXEMPT below, with the reason that site needs neither.

What is read: every .py and .pyw file, and every file with no extension whose first line names
python (githooks/pre-push), that git lists for the checkout (python_files: tracked, or new and
not ignored), outside SKIP_DIRS. A file git ignores, and a worktree or a repository of its own
inside the checkout's folder (another session's, a whole copy of some other kit), is not the kit's
source and is not read. What is found: a
`subprocess.<SPAWNERS>` call with no `creationflags=` keyword (a `**keywords` splat alone does not
count: nothing shows the flag is in it), the same names handed on without a call (a `run=
subprocess.run` default, an alias), `from subprocess import <one of them>`, os.system, os.startfile,
os.popen, os.spawn*, asyncio's create_subprocess_*, multiprocessing and ProcessPoolExecutor.

One text that is not a file is read too: the stub kitlib.stubs.stub_text writes into a repository's
tools folder, under the name STUB. Its start is inside a string, which the reader of files cannot see.

What it cannot see: a start written inside any other string (the scripts a test writes to a
temporary folder), a name reached with getattr, and code in another language (the PowerShell the
kit runs). tests/ is not read: a test starts what it tests, on purpose. The reader itself
(scan_source) is tests/proc/spawnscan.py, a helper module tests/hubsite/test_package.py uses too.

An EXEMPT row is (file, the function the start is in or '<module>', what was found): (how many
such starts it excuses, reason). One more start of that kind in that function than the row counts
fails the test, and so does a row that counts more than there are: the table never outlives the
code it excuses, and never covers a start nobody gave a reason for.
"""
import os
import unittest

from kitlib import machinepaths, stubs
from tests import gitfixture
from tests.proc.spawnscan import SPAWNERS, scan_source

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SKIP_DIRS = ('tests', '.claude')        # folders of the kit's own files that are not read

HELPER = 'the helper itself: it adds kitlib.proc.job.no_window_flags to the keywords it hands on'
OWN_GATE_TOOL = ('a template for another repository\'s own gate tool, copied there: it cannot import '
                 'kitlib, and it runs under that repository\'s gate, whose console it inherits')
STANDALONE = ('a standalone optional tool that imports nothing of the kit: it starts itself again '
              'under the machine lock tool and hands its own console and output on')

STUB = 'kitlib/stubs/__init__.py TEMPLATE'      # the name the stub's text is read under
STUB_TOOL = 'gate'                              # any name: the template's start is the same

EXEMPT = {
    ('kitlib/proc/spawn.py', 'run', 'subprocess.run'): (1, HELPER),
    ('kitlib/proc/spawn.py', 'call', 'subprocess.call'): (1, HELPER),
    ('kitlib/proc/spawn.py', 'popen', 'subprocess.Popen'): (1, HELPER),
    ('kitlib/git/batch.py', '<module>', 'ref subprocess.Popen'): (
        1, 'kept at import only to tell whether a test replaced Popen; the real start is Job.start, '
           'which passes creationflags'),
    ('kitlib/git/batch.py', 'CatFile._proc', 'ref subprocess.Popen'): (
        1, 'the comparison with the Popen kept at import: it starts nothing'),
    ('kitlib/launcher/window.py', 'open_window', 'ref subprocess.run'): (
        1, 'the default of a test seam; the one call through it passes CREATE_NO_WINDOW, and the '
           'session window that PowerShell opens is meant to be seen'),
    ('kitlib/pluginsource/claude.py', 'Runner.__init__', 'ref subprocess.run'): (
        1, 'the default of a test seam; Runner.run, the one call through it, passes creationflags'),
    ('kitlib/machinesetup/agenttemp.py', 'task_registered', 'ref subprocess.run'): (
        1, 'the default of a test seam; the one call through it passes CREATE_NO_WINDOW'),
    ('kitlib/machinesetup/agenttemp.py', 'sweep_dry_run', 'ref subprocess.run'): (
        1, 'the default of a test seam; the one call through it passes CREATE_NO_WINDOW'),
    ('kitlib/redfirst/run.py', 'cleanup_in_child', 'subprocess.Popen'): (
        1, 'its keywords are OWN_CONSOLE, which is creationflags=CREATE_NO_WINDOW on Windows: the '
           'child gets a console of its own on purpose, so a Ctrl+C here does not reach it'),
    ('optional/comfy/comfy_gen.py', 'machine_lock_rerun', 'subprocess.call'): (1, STANDALONE),
    ('optional/comfy/render_glb.py', 'machine_lock_rerun', 'subprocess.call'): (1, STANDALONE),
    ('templates/stacks/dotnet/tools/dotnet_each.py', 'dotnet_each', 'ref subprocess.run'): (
        1, OWN_GATE_TOOL),
    ('templates/stacks/python/tools/js_tests.py', 'run_js_tests', 'ref subprocess.run'): (
        1, OWN_GATE_TOOL),
    (STUB, 'main', 'subprocess.call'): (
        1, 'a stub is a console program, run as `python <tools>/<name>.py`: it has a console when '
           'it runs, its caller\'s or the one Windows gave it, and the kit tool it starts shares '
           'that one. It imports nothing of the kit, so a stub whose kit is gone can still say so'),
}


def is_python(path):
    """A .py or .pyw file, or a file with no extension whose first line is a python shebang."""
    ext = os.path.splitext(path)[1].lower()
    if ext in ('.py', '.pyw'):
        return True
    if ext:
        return False
    try:
        with open(path, 'rb') as f:
            first = f.readline(200)
    except OSError:
        return False
    return first.startswith(b'#!') and b'python' in first


def python_files(root):
    """[path relative to root, with /] of every file read, sorted.

    The names come from git (kitlib.machinepaths.tree_files: tracked, plus untracked and not
    ignored), never from walking folders: git lists a worktree or a repository nested in the
    checkout as one folder name, if at all, and none of the files in it."""
    return [rel for rel in machinepaths.tree_files(root)
            if not set(rel.split('/')[:-1]).intersection(SKIP_DIRS)
            and os.path.isfile(os.path.join(root, rel)) and is_python(os.path.join(root, rel))]


def scan_tree(root):
    found = []
    for rel in python_files(root):
        with open(os.path.join(root, rel), encoding='utf-8-sig') as f:
            found.extend(scan_source(f.read(), rel))
    return found


def scan_stub():
    """The findings in the text of a stub, under the name STUB."""
    return scan_source(stubs.stub_text(STUB_TOOL, kit=ROOT), STUB)


def check(found, exempt):
    """(the findings no row excuses, the rows that count more starts than were found). A row
    excuses as many findings as its count, the first ones in `found`; the rest are unexcused."""
    left = dict((key, row[0]) for key, row in exempt.items())
    unlisted = []
    for f in found:
        if left.get(f[:3], 0) > 0:
            left[f[:3]] -= 1
        else:
            unlisted.append(f)
    return unlisted, sorted(k for k, n in left.items() if n)


class Scanner(unittest.TestCase):
    """The reader finds what it says it finds: a scan that sees nothing would pass for ever."""

    def found(self, text):
        return [(func, what) for _, func, what, _ in scan_source(text, 'm.py')]

    def test_a_bare_call_is_found_with_its_function(self):
        text = ('import subprocess\n'
                'class C:\n'
                '    def go(self):\n'
                '        return subprocess.run(["x"], capture_output=True)\n'
                'subprocess.call(["y"])\n')
        self.assertEqual(self.found(text), [('C.go', 'subprocess.run'),
                                            ('<module>', 'subprocess.call')])

    def test_every_spawner_is_found(self):
        for name in SPAWNERS:
            self.assertEqual(self.found('import subprocess\nsubprocess.%s(["x"])\n' % name),
                             [('<module>', 'subprocess.%s' % name)])

    def test_an_explicit_creationflags_keyword_passes(self):
        self.assertEqual(self.found('import subprocess\n'
                                    'subprocess.Popen(["x"], creationflags=flags, **kw)\n'), [])

    def test_a_splat_alone_does_not_pass(self):
        self.assertEqual(self.found('import subprocess\nsubprocess.Popen(["x"], **kw)\n'),
                         [('<module>', 'subprocess.Popen')])

    def test_a_name_handed_on_is_found(self):
        text = ('import subprocess\n'
                'def f(run=subprocess.run):\n'
                '    start = subprocess.Popen\n')
        self.assertEqual(self.found(text), [('f', 'ref subprocess.run'),    # a default is f's own
                                            ('f', 'ref subprocess.Popen')])

    def test_an_alias_and_a_late_import_are_followed(self):
        text = ('def f():\n'
                '    return sp.check_output(["x"])\n'
                'import subprocess as sp\n')
        self.assertEqual(self.found(text), [('f', 'subprocess.check_output')])

    def test_a_from_import_is_found(self):
        self.assertEqual(self.found('from subprocess import Popen, PIPE\n'),
                         [('<module>', 'from subprocess import Popen')])

    def test_the_other_ways_to_start_a_process_are_found(self):
        text = ('import os, asyncio, multiprocessing\n'
                'from concurrent.futures import ProcessPoolExecutor\n'
                'os.system("x")\n'
                'os.startfile("x")\n'
                'os.spawnl(0, "x")\n'
                'asyncio.create_subprocess_exec("x")\n')
        self.assertEqual([what for _, what in self.found(text)],
                         ['import multiprocessing',
                          'from concurrent.futures import ProcessPoolExecutor',
                          'os.system', 'os.startfile', 'os.spawnl', 'create_subprocess_exec'])

    def test_what_is_not_a_start_is_not_found(self):
        text = ('import subprocess, os\n'
                'x = subprocess.PIPE\n'
                'try:\n    pass\nexcept subprocess.TimeoutExpired:\n    pass\n'
                'os.path.join("a", "b")\n'
                'run(["x"])\n')
        self.assertEqual(self.found(text), [])

    def test_a_stale_row_is_reported(self):
        found = scan_source('import subprocess\nsubprocess.run(["x"])\n', 'm.py')
        row, gone = ('m.py', '<module>', 'subprocess.run'), ('m.py', 'old', 'subprocess.call')
        self.assertEqual(check(found, {row: (1, 'why')}), ([], []))
        self.assertEqual(check(found, {row: (1, 'why'), gone: (1, 'why')}), ([], [gone]))
        self.assertEqual(check(found, {}), (found, []))

    def test_a_row_excuses_its_count_of_starts_and_no_more(self):
        """A second start of the same kind in the same function is not covered by the first's row."""
        found = scan_source('import subprocess\n'
                            'def f():\n'
                            '    subprocess.run(["x"])\n'
                            '    subprocess.run(["y"])\n', 'm.py')
        row = ('m.py', 'f', 'subprocess.run')
        self.assertEqual([f[3] for f in found], [3, 4])
        self.assertEqual(check(found, {row: (1, 'why')}), (found[1:], []))
        self.assertEqual(check(found, {row: (2, 'why')}), ([], []))
        self.assertEqual(check(found, {row: (3, 'why')}), ([], [row]))      # one of them is gone

    def test_the_stub_text_is_read(self):
        """The start a stub makes is in a string in kitlib/stubs: it is read from the stub's text."""
        self.assertEqual([f[:3] for f in scan_stub()], [(STUB, 'main', 'subprocess.call')])
        self.assertIn(stubs.MARK, stubs.stub_text(STUB_TOOL, kit=ROOT))

    def test_the_hooks_with_no_extension_are_read(self):
        files = python_files(ROOT)
        self.assertIn('githooks/pre-push', files)
        self.assertIn('reserve_ui.pyw', files)
        self.assertIn('kitlib/proc/job.py', files)
        self.assertNotIn('githooks/pre-commit', files)              # a shell script
        self.assertEqual([f for f in files if f.startswith('tests/')], [])


BARE = 'import subprocess\nsubprocess.run(["x"])\n'


class NestedTrees(unittest.TestCase):
    """What is read is the checkout's own files and no other tree that sits inside its folder.

    The main checkout holds other sessions' worktrees, each a whole copy of some older kit, under
    folders this file cannot list ahead of time. A throwaway repository stands in for it here."""

    def setUp(self):
        self.repo = gitfixture.TempRepo()
        self.addCleanup(self.repo.cleanup)
        r = self.repo
        r.write('kit/kept.py', BARE)
        r.write('tests/test_kept.py', BARE)
        r.write('.gitignore', 'scratch/\n')
        r.commit('a start', 'kit/kept.py', 'tests/test_kept.py', '.gitignore')

    def nest(self):
        """A linked worktree and a repository of its own, both inside the checkout's folder and
        both holding a start with no creation flags."""
        r = self.repo
        r.git('worktree', 'add', '-q', r.path('.other', 'worktrees', 'companion'), '-b', 'kit/companion')
        inner = r.path('vendor', 'inner')
        os.makedirs(inner)
        r.git('init', '-q', '-b', 'main', cwd=inner)
        r.write('tool.py', BARE, root=inner)
        r.git('add', '--', 'tool.py', cwd=inner)
        r.git('-c', 'user.name=%s' % gitfixture.NAME, '-c', 'user.email=%s' % gitfixture.EMAIL,
              'commit', '-q', '-m', 'inner', cwd=inner)
        self.assertTrue(os.path.isfile(r.path('.other', 'worktrees', 'companion', 'kit', 'kept.py')))
        self.assertTrue(os.path.isfile(r.path('vendor', 'inner', 'tool.py')))

    def test_a_nested_worktree_and_a_nested_repository_are_not_read(self):
        self.nest()
        self.assertEqual(python_files(self.repo.root), ['kit/kept.py'])
        self.assertEqual([f[:3] for f in scan_tree(self.repo.root)],
                         [('kit/kept.py', '<module>', 'subprocess.run')])

    def test_a_new_file_not_yet_added_is_read(self):
        """A start in a file nobody has added yet is found before its first commit."""
        self.repo.write('kit/new.py', BARE)
        self.assertEqual(python_files(self.repo.root), ['kit/kept.py', 'kit/new.py'])
        self.assertEqual([f[:3] for f in scan_tree(self.repo.root)],
                         [('kit/kept.py', '<module>', 'subprocess.run'),
                          ('kit/new.py', '<module>', 'subprocess.run')])

    def test_an_ignored_file_is_not_read(self):
        """What git ignores cannot be committed: it is no part of the kit."""
        self.repo.write('scratch/try.py', BARE)
        self.assertEqual(python_files(self.repo.root), ['kit/kept.py'])

    def test_a_tracked_file_gone_from_the_folder_is_not_read(self):
        os.remove(self.repo.path('kit', 'kept.py'))
        self.assertEqual(python_files(self.repo.root), [])
        self.assertEqual(scan_tree(self.repo.root), [])


class Sites(unittest.TestCase):
    """The kit's own source."""

    @classmethod
    def setUpClass(cls):
        cls.found = scan_tree(ROOT) + scan_stub()
        cls.unlisted, cls.stale = check(cls.found, EXEMPT)

    def test_every_start_names_its_creation_flags(self):
        said = ['%s:%d  %s  in %s' % (rel, line, what, func)
                for rel, func, what, line in self.unlisted]
        self.assertEqual(said, [], '\n' + '\n'.join(
            said + ['Start it through kitlib.proc.spawn, pass creationflags=, or add a row with '
                    'its reason to EXEMPT in tests/proc/test_spawn_sites.py.']))

    def test_no_exemption_outlives_its_site(self):
        self.assertEqual(self.stale, [], 'EXEMPT rows that count more starts than there are: '
                                         'lower the count, or remove the row')

    def test_every_exemption_gives_its_count_and_reason(self):
        for key, (count, reason) in EXEMPT.items():
            with self.subTest(key=key):
                self.assertEqual(len(key), 3)
                self.assertIs(type(count), int)
                self.assertGreaterEqual(count, 1)
                self.assertIsInstance(reason, str)
                self.assertGreaterEqual(len(reason.split()), 6, 'a reason is a sentence')

    def test_the_scan_saw_the_kit(self):
        """The helper's own three starts are found: the scan read kitlib and knows a start."""
        mine = sorted(f[1:3] for f in self.found if f[0] == 'kitlib/proc/spawn.py')
        self.assertEqual(mine, [('call', 'subprocess.call'), ('popen', 'subprocess.Popen'),
                                ('run', 'subprocess.run')])


if __name__ == '__main__':
    unittest.main()
