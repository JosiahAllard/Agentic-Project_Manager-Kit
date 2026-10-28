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

What it cannot see: a start written inside any other string (the scripts a test writes to a
temporary folder), a name reached with getattr, and code in another language (the PowerShell the
kit runs). tests/ is not read: a test starts what it tests, on purpose. The reader itself
(scan_source) is tests/proc/spawnscan.py, a helper module tests/hubsite/test_package.py uses too.

An EXEMPT row is (file, the function the start is in or '<module>', what was found): (how many
such starts it excuses, reason). One more start of that kind in that function than the row counts
fails the test, and so does a row that counts more than there are: the table never outlives the
code it excuses, and never covers a start nobody gave a reason for.

A second rule, window hazards (class Hazards), reads more widely: every Python file and every
.ps1, .cmd and .bat file git lists, tests/ included, outside `.claude` (hazard_files). A hazard
is a construct that can open a console window whatever the CreateProcess hook
kitlib.proc.console installs does (tests/proc/windowscan.py says which, and how each is seen):
os.system, os.spawn*, os.exec*, os.startfile, ShellExecute*, CreateProcess* reached through
ctypes, CREATE_NEW_CONSOLE, DETACHED_PROCESS (unless the start's argv names pythonw), AllocConsole,
AllocConsoleWithOptions, AttachConsole, a WMI Win32_Process Create, and Start-Process, `cmd /c
start`, `start ""` and `-WindowStyle Hidden`, and Python code text pythonw runs (`-c`) that
starts a console child with no window choice and no kitlib import. Each one is forbidden unless it is in its home
(RULE_HOMES: the console calls in kitlib/proc/console.py, the WMI start in kitlib/lock/detach.py,
the lock tool's detach path) or has a HAZARD_EXEMPT row, counted and reasoned the same way as
EXEMPT.
"""
import os
import unittest

from kitlib import machinepaths
from tests import gitfixture
from tests.proc import windowscan
from tests.proc.spawnscan import SPAWNERS, scan_source

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SKIP_DIRS = ('tests', '.claude')        # folders of the kit's own files that are not read

HELPER = 'the helper itself: it adds kitlib.proc.job.no_window_flags to the keywords it hands on'
OWN_GATE_TOOL = ('a template for another repository\'s own gate tool, copied there: it cannot import '
                 'kitlib, and it runs under that repository\'s gate, whose console it inherits')
STANDALONE = ('a standalone optional tool that imports nothing of the kit: it starts itself again '
              'under the machine lock tool and hands its own console and output on')

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
}

# The window-hazard rule (class Hazards).
SCRIPT_EXTS = ('.ps1', '.cmd', '.bat')
HAZARD_SKIP = {     # files the hazard scan does not read: the rule's own tables and fixture text
    'tests/proc/windowscan.py': 'the reader itself: its tables and patterns name every hazard it '
                                'looks for, and it starts nothing',
    'tests/proc/test_spawn_sites.py': 'this rule\'s own exemption table and fixture sources name '
                                      'every hazard, written to a temporary repository and never run',
}
RULE_HOMES = {      # what -> the one file where the rule itself allows it
    'AllocConsole': 'kitlib/proc/console.py',
    'AllocConsoleWithOptions': 'kitlib/proc/console.py',
    'AttachConsole': 'kitlib/proc/console.py',
    'WMI Win32_Process Create': 'kitlib/lock/detach.py',
}

GUARD_READS = ('the guard parses the commands agents run: it names Start-Process and cmd start to '
               'recognise and judge them, and starts nothing')
GUARD_TEST = ('a guard test hands the guard a command line that holds this text to judge; the '
              'command is parsed, never run')
FLAG_PASSES = ('a test of how a caller\'s console choice is passed on: the flag goes to a stand-in '
               'for CreateProcess, nothing is started with it')
OPEN = ' OPEN (reported, not fixed here): '

HAZARD_EXEMPT = {
    ('kitlib/guard/commands.py', '_start_process', 'Start-Process'): (3, GUARD_READS),
    ('kitlib/guard/commands.py', '_sp_unreadable', 'Start-Process'): (4, GUARD_READS),
    ('kitlib/guard/commands.py', 'invocation', 'Start-Process'): (2, GUARD_READS),
    ('kitlib/guard/commands.py', '_nested_text', 'Start-Process'): (1, GUARD_READS),
    ('kitlib/guard/commands.py', '_sp_shell_text', 'Start-Process'): (2, GUARD_READS),
    ('kitlib/guard/rules.py', '<module>', 'Start-Process'): (1, GUARD_READS),
    ('kitlib/guard/rules.py', 'python_unread', 'Start-Process'): (1, GUARD_READS),
    ('kitlib/guard/rules.py', 'plain_run', 'Start-Process'): (1, GUARD_READS),
    ('kitlib/guard/rules.py', 'kit_script', 'Start-Process'): (1, GUARD_READS),
    ('kitlib/guard/rules.py', '_tool_run', 'Start-Process'): (1, GUARD_READS),
    ('kitlib/guard/commands.py', 'run_dir', 'Start-Process'): (1, GUARD_READS),
    ('kitlib/guard/commands.py', 'bash_start', 'Start-Process'): (1, GUARD_READS),
    ('kitlib/guard/rules.py', 'shell_writes', 'Start-Process'): (1, GUARD_READS),
    ('kitlib/guard/rules.py', 'write_place', 'Start-Process'): (1, GUARD_READS),
    ('kitlib/guard/rules.py', 'bare_wrapper', 'Start-Process'): (1, GUARD_READS),
    ('kitlib/loadnode/box.py', 'serve', 'os.execvp'): (
        1, 'a load node\'s forced command (optional/loadnode/loadnode.py serve, run by sshd on a '
           'Linux node, never on Windows) replaces itself with receive-pack for a push, so the '
           'receiving side reads and writes the ssh channel itself; there is no console to open'),
    ('kitlib/pluginsource/__init__.py', '<module>', 'DETACHED_PROCESS'): (
        1, '_DETACHED (DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP) is used by one start only, '
           'Popen([pythonw(), plugin_refresh.py, ...]): pythonw is a GUI program that never gets '
           'a console. The find is the bit in the constant definition; every use of the name '
           '_DETACHED in a creationflags is followed, and one in a start that does not name '
           'pythonw is reported on its own'),
    ('kitlib/proc/console.py', '<module>', 'CREATE_NEW_CONSOLE'): (
        2, 'the CreateProcess hook itself: it defines the bit and reads it in CONSOLE_CHOICE to '
           'leave a caller\'s own console choice alone; it never asks for one'),
    ('kitlib/proc/console.py', '<module>', 'DETACHED_PROCESS'): (
        2, 'the CreateProcess hook itself: it defines the bit and reads it in CONSOLE_CHOICE to '
           'leave a caller\'s own console choice alone; it never asks for one'),
    ('kitlib/proc/win32.py', '<module>', 'CREATE_NEW_CONSOLE'): (
        1, 'the constant definition the kit\'s callers and tests name; defining it starts nothing'),
    ('kitlib/proc/win32.py', '<module>', 'DETACHED_PROCESS'): (
        1, 'the constant definition the kit\'s callers and tests name; defining it starts nothing'),
    ('optional/bridge/win/startup.py', 'render', '-WindowStyle Hidden'): (
        2, 'renders the optional bridge\'s logon task (powershell -WindowStyle Hidden -File) and '
           'start-bridge.ps1, which the owner installs by hand.' + OPEN + 'a console started '
           'hidden can still show a window first; it wants a pythonw carrier'),
    ('optional/bridge/win/startup.py', 'render', 'Start-Process'): (
        1, 'start-bridge.ps1 starts the bridge service with Start-Process -WindowStyle Hidden; '
           'the owner installs it by hand.' + OPEN + 'python.exe gets a console of its own, '
           'hidden after the fact; it wants pythonw'),
    ('optional/comfy/comfy_gen.py', '_launch', 'DETACHED_PROCESS'): (
        2, 'a standalone optional tool that imports nothing of the kit starts ComfyUI with '
           'CREATE_NO_WINDOW | DETACHED_PROCESS.' + OPEN + 'with DETACHED_PROCESS the no-window '
           'flag is ignored, and a console child of ComfyUI opens its own window (two finds: '
           'the getattr string and the `flags` name it is bound to, used in the start)'),
    ('optional/comfy/comfy_gen.py', '_ensure_watcher', 'DETACHED_PROCESS'): (
        2, 'a standalone optional tool that imports nothing of the kit starts its watcher with '
           'sys.executable and CREATE_NO_WINDOW | DETACHED_PROCESS.' + OPEN + 'python.exe '
           'detached has no console, so any console program it starts opens a window (two '
           'finds: the getattr string and the `flags` name it is bound to, used in the start)'),
    ('tests/guard/extra_rows.py', '<module>', 'Start-Process'): (2, GUARD_TEST),
    ('tests/guard/shell_rows.py', '<module>', 'Start-Process'): (122, GUARD_TEST),
    ('tests/guard/shell_rows.py', '<module>', 'cmd /c start'): (2, GUARD_TEST),
    ('tests/guard/test_jail.py', 'UnplacedGit.test_each_env_changer_unread_passes_for_every_jailed_role',
     'Start-Process'): (1, GUARD_TEST),
    ('tests/guard/test_provider_paths.py', 'NativePrograms.test_native_writers_in_the_main_checkout',
     'Start-Process'): (1, GUARD_TEST),
    ('tests/guard/test_rolepolicy.py', 'LockVerbs.test_every_real_lock_call_is_still_read',
     'Start-Process'): (3, GUARD_TEST),
    ('tests/guard/test_rolepolicy.py', 'LockVerbs.test_every_real_lock_call_is_still_read',
     'start ""'): (1, GUARD_TEST),
    ('tests/guard/test_rolepolicy.py',
     'LockVerbs.test_a_read_or_a_lock_call_whose_output_stays_still_passes',
     'Start-Process'): (1, GUARD_TEST),
    ('tests/guard/test_kit_command_forms.py', 'kit_spellings', 'Start-Process'): (2, GUARD_TEST),
    ('tests/guard/test_kit_command_forms.py',
     'ToolRun.test_a_split_or_quoted_name_reads_its_first_token', 'Start-Process'): (1, GUARD_TEST),
    ('tests/guard/test_kit_cmd_reparse.py', 'CmdReparse.kit_rows', 'Start-Process'): (1, GUARD_TEST),
    ('tests/guard/test_kit_cmd_reparse.py', 'CmdReparse.test_plain_arguments_pass',
     'Start-Process'): (1, GUARD_TEST),
    ('tests/guard/test_rules.py', '<module>', 'Start-Process'): (2, GUARD_TEST),
    ('tests/guard/test_start_process_workdir.py',
     'GrantedLander.test_a_start_process_workdir_it_cannot_place_is_refused', 'Start-Process'): (1, GUARD_TEST),
    ('tests/guard/test_start_process_workdir.py', 'LockedMain.sp', 'Start-Process'): (1, GUARD_TEST),
    ('tests/guard/test_start_process_workdir.py', 'LockedMain.test_a_bare_drive_is_no_place',
     'Start-Process'): (2, GUARD_TEST),
    ('tests/guard/test_start_process_workdir.py', 'LockedMain.test_a_computed_folder_is_no_place',
     'Start-Process'): (2, GUARD_TEST),
    ('tests/guard/test_unknown_dir_jail.py', '<module>', 'Start-Process'): (4, GUARD_TEST),
    ('tests/guard/test_instead_passes.py', '<module>', 'Start-Process'): (4, GUARD_TEST),
    ('tests/guard/test_advice_workdir.py', 'RunDir.test_the_workdir_is_where_the_program_runs',
     'Start-Process'): (2, GUARD_TEST),
    ('tests/guard/test_advice_workdir.py', 'RunDir.test_no_workdir_is_the_command_s_directory',
     'Start-Process'): (1, GUARD_TEST),
    ('tests/guard/test_advice_workdir.py', 'RunDir.test_a_computed_or_unplaceable_workdir_is_none',
     'Start-Process'): (1, GUARD_TEST),
    ('tests/guard/test_advice_workdir.py',
     'StartProcessWorkdir.test_git_writes_in_the_main_checkout_it_starts_in', 'Start-Process'): (4, GUARD_TEST),
    ('tests/guard/test_advice_workdir.py',
     'StartProcessWorkdir.test_git_in_the_worktree_it_starts_in_passes', 'Start-Process'): (3, GUARD_TEST),
    ('tests/guard/test_advice_workdir.py',
     'StartProcessWorkdir.test_python_writes_the_main_checkout_it_starts_in', 'Start-Process'): (5, GUARD_TEST),
    ('tests/guard/test_advice_workdir.py',
     'StartProcessWorkdir.test_the_alternative_aims_the_workdir_at_the_worktree', 'Start-Process'): (2, GUARD_TEST),
    ('tests/guard/test_advice_workdir.py',
     'StartProcessWorkdir.test_a_worktree_workdir_does_not_free_a_writer_in_the_main_checkout',
     'Start-Process'): (3, GUARD_TEST),
    ('tests/guard/test_advice_workdir.py',
     'StartProcessWorkdir.test_a_redirect_is_placed_from_the_outer_location', 'Start-Process'): (2, GUARD_TEST),
    ('tests/guard/test_advice_workdir.py',
     'UnreadPython.test_a_start_process_python_says_its_command_line_is_unread', 'Start-Process'): (3, GUARD_TEST),
    ('tests/guard/test_rules.py', 'PythonCodeCountsAsWrite.test_python_stdin_counts_as_write_of_cwd',
     'Start-Process'): (2, GUARD_TEST),
    ('tests/guard/test_kit_tool_forms.py',
     'KitToolChecked.test_where_the_c_may_not_be_python_s_the_working_directory_is_judged',
     'Start-Process'): (1, GUARD_TEST),
    ('tests/guard/test_kit_tool_forms.py',
     'LookalikeWrappers.test_a_start_process_script_is_read_from_its_working_directory',
     'Start-Process'): (1, GUARD_TEST),
    ('tests/guard/test_kit_tool_forms.py', 'LookalikeWrappers.test_a_start_process_script_elsewhere_is_not_the_kit_s',
     'Start-Process'): (1, GUARD_TEST),
    ('tests/guard/test_kit_tool_forms.py',
     'LookalikeWrappers.test_a_drive_less_script_in_a_computed_working_directory_is_not_the_kit_s',
     'Start-Process'): (1, GUARD_TEST),
    ('tests/guard/test_backlog_role.py', 'Spellings.test_start_process', 'Start-Process'): (4, GUARD_TEST),
    ('tests/guard/test_backlog_role.py',
     'AsFromItsWorktree.test_a_start_process_runs_where_its_working_directory_says.sp',
     'Start-Process'): (1, GUARD_TEST),
    ('tests/guard/test_backlog_role.py',
     'OwnPull.test_a_start_process_reads_the_store_where_its_working_directory_says.sp',
     'Start-Process'): (1, GUARD_TEST),
    ('tests/guard/test_backlog_role.py',
     'OwnPull.test_a_start_process_reads_the_store_where_its_working_directory_says',
     'Start-Process'): (1, GUARD_TEST),
    ('tests/guard/test_gaterole.py', 'GateAllow.ps_spellings', 'Start-Process'): (2, GUARD_TEST),
    ('tests/guard/test_rules_hub.py', 'OwnerVerbs.test_launchers', 'Start-Process'): (2, GUARD_TEST),
    ('tests/guard/test_rules_hub.py', 'OwnerVerbs.test_review_rows_for_owner_verbs', 'Start-Process'): (1, GUARD_TEST),
    ('tests/guard/test_rules.py', 'OwnerLockTools', 'Start-Process'): (3, GUARD_TEST),
    ('tests/guard/test_rules.py', 'OwnerLockTools', 'cmd /c start'): (2, GUARD_TEST),
    ('tests/guard/test_rules.py', 'OwnerLockTools', 'start ""'): (2, GUARD_TEST),
    ('tests/guard/test_rules.py', 'OwnerSetup', 'Start-Process'): (1, GUARD_TEST),
    ('tests/proc/test_console.py', '<module>', 'CREATE_NEW_CONSOLE'): (
        1, 'the test\'s own copy of the constant, for the wrapper tests below; defining it starts '
           'nothing'),
    ('tests/proc/test_console.py', '<module>', 'DETACHED_PROCESS'): (
        1, 'the test\'s own copy of the constant, for the wrapper tests below; defining it starts '
           'nothing'),
    ('tests/proc/test_console.py', 'WrapperTest.test_a_console_choice_passes_through_and_ensures_nothing',
     'CREATE_NEW_CONSOLE'): (2, FLAG_PASSES),
    ('tests/proc/test_console.py', 'WrapperTest.test_a_console_choice_passes_through_and_ensures_nothing',
     'DETACHED_PROCESS'): (2, FLAG_PASSES),
    ('tests/proc/test_console.py',
     'WrapperTest.test_no_console_to_be_had_gives_every_bare_start_no_window',
     'DETACHED_PROCESS'): (2, FLAG_PASSES),
    ('tests/proc/test_console.py', 'WrapperTest.test_the_first_bare_start_ensures_once',
     'CREATE_NEW_CONSOLE'): (2, FLAG_PASSES),
    ('tests/proc/test_console.py', 'alloc_exported', 'AllocConsoleWithOptions'): (
        1, 'asks whether kernel32 exports the function, to skip a test where it does not; it '
           'never calls it'),
    ('tests/proc/test_no_window.py', 'NoWindowFlagsTest.test_caller_console_choice_unchanged',
     'CREATE_NEW_CONSOLE'): (1, FLAG_PASSES),
    ('tests/proc/test_no_window.py', 'NoWindowFlagsTest.test_caller_console_choice_unchanged',
     'DETACHED_PROCESS'): (1, FLAG_PASSES),
    ('tests/proc/test_spawn.py', 'FlagsTest.test_a_callers_console_choice_is_kept',
     'CREATE_NEW_CONSOLE'): (1, FLAG_PASSES),
    ('tests/proc/test_spawn.py', 'FlagsTest.test_a_callers_console_choice_is_kept',
     'DETACHED_PROCESS'): (1, FLAG_PASSES),
    ('tests/proc/test_spawn.py', 'FlagsTest.test_detached_process_is_never_added',
     'DETACHED_PROCESS'): (
        1, 'asserts the flag is absent from what kitlib.proc.spawn hands on; nothing is started '
           'with it'),
    ('tests/proc/test_winwatch.py', 'ConsoleWindow.test_a_new_console_child_is_recorded_with_its_pid',
     'CREATE_NEW_CONSOLE'): (
        1, 'the window monitor\'s one real-window test: it must open one console window to prove '
           'the monitor sees it. ConsoleWindow runs only when KIT_WINDOW_TESTS=1, so the gate never '
           'opens it; every other child in the file passes CREATE_NO_WINDOW'),
}


def python_files(root):
    """[path relative to root, with /] of every file read, sorted.

    The names come from git (kitlib.machinepaths.tree_files: tracked, plus untracked and not
    ignored), never from walking folders: git lists a worktree or a repository nested in the
    checkout as one folder name, if at all, and none of the files in it."""
    return [rel for rel in machinepaths.tree_files(root)
            if not set(rel.split('/')[:-1]).intersection(SKIP_DIRS)
            and os.path.isfile(os.path.join(root, rel))
            and windowscan.is_python(os.path.join(root, rel))]


def scan_tree(root):
    found = []
    for rel in python_files(root):
        with open(os.path.join(root, rel), encoding='utf-8-sig') as f:
            found.extend(scan_source(f.read(), rel))
    return found


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


def hazard_files(root):
    """[path relative to root, with /] of every file the hazard rule reads, sorted: the Python
    files and .ps1/.cmd/.bat files git lists (as python_files does), tests/ included, outside any
    `.claude` folder, less HAZARD_SKIP."""
    out = []
    for rel in machinepaths.tree_files(root):
        path = os.path.join(root, rel)
        if '.claude' in rel.split('/')[:-1] or rel in HAZARD_SKIP or not os.path.isfile(path):
            continue
        if os.path.splitext(rel)[1].lower() in SCRIPT_EXTS or windowscan.is_python(path):
            out.append(rel)
    return sorted(out)


def scan_hazards(root):
    """[(rel, function, what, line)] of every window hazard in hazard_files(root), less those in
    their RULE_HOMES file."""
    found = []
    for rel in hazard_files(root):
        with open(os.path.join(root, rel), encoding='utf-8-sig', errors='replace') as f:
            text = f.read()
        if os.path.splitext(rel)[1].lower() in SCRIPT_EXTS:
            found.extend(windowscan.scan_script(text, rel))
        else:
            found.extend(windowscan.scan_python(text, rel))
    return [f for f in found if RULE_HOMES.get(f[2]) != f[0]]


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
        cls.found = scan_tree(ROOT)
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


# One of each hazard, for HazardFixture. Each line's comment is the label it must be reported as.
HAZARD_PY = '''\
import ctypes, os, subprocess
import os as o2
from os import system
from ctypes import windll
os.system("x")                                              # os.system
os.spawnl(os.P_NOWAIT, "x")                                 # os.spawnl
o2.execv("x", ["x"])                                        # os.execv
os.startfile("x")                                           # os.startfile
windll.shell32.ShellExecuteW(None, "open", "x", None, None, 0)    # ShellExecuteW
windll.kernel32.CreateProcessW(None, "x", None, None, 0, 0, None, None, None, None)  # CreateProcessW
windll.advapi32.CreateProcessAsUserW(None)                 # CreateProcessAsUserW
subprocess.Popen(["x"], creationflags=subprocess.CREATE_NEW_CONSOLE)    # CREATE_NEW_CONSOLE
subprocess.Popen(["python", "y"], creationflags=0x8)       # DETACHED_PROCESS
windll.kernel32.AllocConsole()                              # AllocConsole
windll.kernel32.AllocConsoleWithOptions(None, None)         # AllocConsoleWithOptions
windll.kernel32.AttachConsole(-1)                           # AttachConsole
PS = "Invoke-CimMethod -ClassName Win32_Process -MethodName Create"     # WMI Win32_Process Create
subprocess.run("powershell Start-Process x", creationflags=0)         # Start-Process
subprocess.run(["cmd", "/c", "start", "x"], creationflags=0)            # cmd /c start
CMD = 'start "" x.exe'                                      # start ""
subprocess.run(["powershell", "-WindowStyle", "Hidden"], creationflags=0)  # -WindowStyle Hidden
CODE = ("import subprocess; " + "subprocess.Popen(['x'])")     # start in code text
subprocess.Popen([PYTHONW, "-c", CODE], creationflags=0)
subprocess.run([pythonw, "-c", f"import subprocess; subprocess.run(['{x}'])"], creationflags=0)  # start in code text
TWO = ("import subprocess as s; s.Popen(['a'], creationflags=s.CREATE_NO_WINDOW); "  # start in code text
       "s.Popen(['b'])")
ZERO = "import subprocess; subprocess.run(['x'], creationflags=0)"     # start in code text
SP = "import subprocess as sp; sp.run(['x'])"               # start in code text
FROM = "from subprocess import Popen as P; P(['x'])"        # start in code text
LATE = "import subprocess; subprocess.run(['x']); import kitlib"   # start in code text
for code in (TWO, ZERO, SP, FROM, LATE):
    subprocess.run([pythonw, "-c", code], creationflags=0)
_DETACHED = 0x00000008 | 0x00000200                         # DETACHED_PROCESS
subprocess.Popen([pythonw(), "t.py"], creationflags=_DETACHED)
subprocess.Popen(["python.exe", "t.py"], creationflags=_DETACHED)     # DETACHED_PROCESS
both = _DETACHED | 0x8000000
subprocess.Popen(["python.exe"], creationflags=both)        # DETACHED_PROCESS
NEW = getattr(subprocess, "CREATE_NEW_CONSOLE")             # CREATE_NEW_CONSOLE
subprocess.Popen([pythonw(), "t.py"], creationflags=NEW)    # CREATE_NEW_CONSOLE
'''
HAZARD_PY_LABELS = ['from os import system', 'os.system', 'os.spawnl', 'os.execv', 'os.startfile',
                    'ShellExecuteW', 'CreateProcessW', 'CreateProcessAsUserW', 'CREATE_NEW_CONSOLE',
                    'DETACHED_PROCESS', 'AllocConsole', 'AllocConsoleWithOptions', 'AttachConsole',
                    'WMI Win32_Process Create', 'Start-Process', 'cmd /c start', 'start ""',
                    '-WindowStyle Hidden', 'start in code text', 'start in code text',
                    'start in code text', 'start in code text', 'start in code text',
                    'start in code text', 'start in code text', 'DETACHED_PROCESS',
                    'DETACHED_PROCESS', 'DETACHED_PROCESS', 'CREATE_NEW_CONSOLE',
                    'CREATE_NEW_CONSOLE']
HAZARD_FINE = '''\
"""Mentions in a docstring are not hazards: os.system, CREATE_NEW_CONSOLE, Start-Process."""
import subprocess
# Start-Process and DETACHED_PROCESS in a comment are not read either.
DETACHED = 0x00000008 | 0x00000200      # a flag named by a constant: the pythonw check cannot see it
def go(pythonw):
    """AllocConsole, cmd /c start, -WindowStyle Hidden."""
    subprocess.Popen([pythonw(), "tool.py"], creationflags=subprocess.DETACHED_PROCESS | 0x8000000)
    subprocess.Popen([r"C:/Python/pythonw.exe", "t.py"], creationflags=0x8)
    hid = "import subprocess; subprocess.run(['x'], creationflags=subprocess.CREATE_NO_WINDOW)"
    hooked = "import kitlib, subprocess; subprocess.run(['x'])"
    subprocess.run([pythonw(), "-c", hid], creationflags=0)
    subprocess.run([pythonw(), "-c", hooked], creationflags=0)
    subprocess.run(["python", "-c", "import subprocess; subprocess.run(['x'])"], creationflags=0)
    named = "import subprocess as sp; NW = sp.CREATE_NO_WINDOW | 0x200; sp.run(['x'], creationflags=NW)"
    bits = "from subprocess import run; run(['x'], creationflags=0x08000000)"
    early = "import kitlib; from subprocess import Popen; Popen(['x'])"
    for code in (named, bits, early):
        subprocess.run([pythonw(), "-c", code], creationflags=0)
    subprocess.Popen([pythonw(), "t.py"], creationflags=DETACHED | 0x8000000)
    flags = DETACHED
    subprocess.Popen([pythonw(), "t.py"], creationflags=flags)
'''
HAZARD_PS1 = ('# Start-Process in a comment is not read\n'
              'Start-Process -FilePath python -ArgumentList x\n'
              'powershell -WindowStyle Hidden -File x.ps1\n'
              'pwsh -w hidden -c x\n')
HAZARD_CMD = ('@echo off\n'
              'rem start "" x.exe in a comment\n'
              ':: cmd /c start x in a comment\n'
              'start "" x.exe\n'
              'cmd /c start x\n'
              'cmd.exe /k start y\n')


class HazardFixture(unittest.TestCase):
    """The hazard rule bites: a throwaway repository holding one of each forbidden construct has
    every one reported, and what the rule allows is not."""

    def setUp(self):
        self.repo = gitfixture.TempRepo()
        self.addCleanup(self.repo.cleanup)
        r = self.repo
        r.write('kit/bad.py', HAZARD_PY)
        r.write('kit/fine.py', HAZARD_FINE)
        r.write('kit/run.ps1', HAZARD_PS1)
        r.write('kit/run.cmd', HAZARD_CMD)
        r.write('tests/test_bad.py', 'import os\nos.system("x")\n')
        r.write('tests/test_plain.py', 'import subprocess\nsubprocess.run(["x"])\n')
        r.write('.claude/worktrees/w/bad.py', 'import os\nos.system("x")\n')
        r.write('kitlib/proc/console.py', 'from ctypes import windll\nwindll.kernel32.AllocConsole()\n')
        r.write('kitlib/other.py', 'from ctypes import windll\nwindll.kernel32.AttachConsole(-1)\n')
        r.write('kitlib/lock/detach.py', 'D = "Win32_Process -MethodName Create"\n')
        r.write('tools/other.py', 'D = "Win32_Process -MethodName Create"\n')
        r.write('kit/startup.py', 'S = "Win32_ProcessStartup Create"\n')
        r.commit('fixture', 'kit/bad.py')
        self.found = scan_hazards(r.root)

    def labels(self, rel):
        return [f[2] for f in self.found if f[0] == rel]

    def test_every_python_hazard_is_reported(self):
        self.assertEqual(sorted(self.labels('kit/bad.py')), sorted(HAZARD_PY_LABELS))
        lines = HAZARD_PY.splitlines()
        for _, _, what, line in (f for f in self.found if f[0] == 'kit/bad.py'):
            if what != 'from os import system':
                self.assertTrue(lines[line - 1].endswith('# ' + what), (what, lines[line - 1]))

    def test_docstrings_comments_and_pythonw_detached_are_not(self):
        """The DETACHED constant's name holds DETACH, so its 0x8 bit is read as the flag."""
        self.assertEqual(self.labels('kit/fine.py'), ['DETACHED_PROCESS'])
        self.assertEqual([f[3] for f in self.found if f[0] == 'kit/fine.py'], [4])

    def test_every_script_hazard_is_reported_and_comments_are_not(self):
        self.assertEqual(self.labels('kit/run.ps1'),
                         ['Start-Process', '-WindowStyle Hidden', '-WindowStyle Hidden'])
        self.assertEqual(self.labels('kit/run.cmd'), ['start ""', 'cmd /c start', 'cmd /c start'])
        self.assertEqual([f[1] for f in self.found if f[0] == 'kit/run.cmd'], ['<script>'] * 3)

    def test_tests_are_read_and_claude_folders_are_not(self):
        self.assertEqual(self.labels('tests/test_bad.py'), ['os.system'])
        self.assertEqual(self.labels('tests/test_plain.py'), [])    # no creationflags rule in tests
        self.assertNotIn('.claude/worktrees/w/bad.py', hazard_files(self.repo.root))

    def test_a_rule_home_is_allowed_only_in_its_file(self):
        self.assertEqual(self.labels('kitlib/proc/console.py'), [])
        self.assertEqual(self.labels('kitlib/other.py'), ['AttachConsole'])
        self.assertEqual(self.labels('kitlib/lock/detach.py'), [])
        self.assertEqual(self.labels('tools/other.py'), ['WMI Win32_Process Create'])
        self.assertEqual(self.labels('kit/startup.py'), [])

    def test_unlisted_hazards_fail_and_a_row_for_nothing_is_stale(self):
        row = ('kit/bad.py', '<module>', 'os.system')
        gone = ('tests/proc/test_gone.py', 'T.t', 'CREATE_NEW_CONSOLE')
        unlisted, stale = check(self.found, {row: (1, 'why'), gone: (1, 'why')})
        self.assertNotIn(row, [f[:3] for f in unlisted])
        self.assertIn(('kit/bad.py', '<module>', 'os.startfile'), [f[:3] for f in unlisted])
        self.assertEqual(stale, [gone])


class Hazards(unittest.TestCase):
    """The kit's own files: no window hazard without its home or a reasoned row."""

    @classmethod
    def setUpClass(cls):
        cls.found = scan_hazards(ROOT)
        cls.unlisted, cls.stale = check(cls.found, HAZARD_EXEMPT)

    def test_no_window_hazard_without_a_row(self):
        said = ['%s:%d  %s  in %s' % (rel, line, what, func)
                for rel, func, what, line in self.unlisted]
        self.assertEqual(said, [], '\n' + '\n'.join(
            said + ['Each of these can open a console window that the CreateProcess hook cannot '
                    'stop (tests/proc/windowscan.py says why). Start it through subprocess or '
                    'kitlib.proc.spawn with no console flag (pythonw for a detached start), or add '
                    'a row with its count and reason to HAZARD_EXEMPT in '
                    'tests/proc/test_spawn_sites.py.']))

    def test_no_hazard_row_outlives_its_site(self):
        self.assertEqual(self.stale, [], 'HAZARD_EXEMPT rows that count more hazards than there '
                                         'are: lower the count, or remove the row')

    def test_every_hazard_row_gives_its_count_and_reason(self):
        for key, (count, reason) in HAZARD_EXEMPT.items():
            with self.subTest(key=key):
                self.assertEqual(len(key), 3)
                self.assertIs(type(count), int)
                self.assertGreaterEqual(count, 1)
                self.assertIsInstance(reason, str)
                self.assertGreaterEqual(len(reason.split()), 6, 'a reason is a sentence')
        for rel, why in HAZARD_SKIP.items():
            self.assertGreaterEqual(len(why.split()), 6, rel)

    def test_the_scan_saw_the_kit(self):
        """It read Python in kitlib, tests and optional/, and the PowerShell and cmd files."""
        files = hazard_files(ROOT)
        for rel in ('kitlib/proc/win32.py', 'tests/proc/test_console.py', 'githooks/pre-push',
                    'reserve_ui.pyw', 'boot/hook.py', 'kitlib/host/hookmain.py'):
            self.assertIn(rel, files)
        self.assertTrue([f for f in files if f.startswith('optional/bridge/')])
        self.assertTrue([f for f in files if f.startswith('hosts/')])
        self.assertTrue([f for f in files if os.path.splitext(f)[1] in SCRIPT_EXTS])
        self.assertFalse([f for f in files if '.claude/' in f])
        self.assertIn(('kitlib/proc/win32.py', '<module>', 'CREATE_NEW_CONSOLE'),
                      [f[:3] for f in self.found])


if __name__ == '__main__':
    unittest.main()
