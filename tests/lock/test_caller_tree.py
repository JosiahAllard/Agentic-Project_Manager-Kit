"""kitlib.lock.store.caller reads who is asking from this process's tree: a chain that runs into a
shell that has exited, but that a handle keeps openable, is never the owner's on that account,
nor is one WMI's provider host tops, and the owner's own chains still are. Fakes only
(tests/proc/fakeproc.py): the process calls answer from a table, this process at its head."""
import os
import unittest
from unittest import mock

from kitlib.lock import store

from tests.proc.fakeproc import FakeProc, patched
from tests.platforms import windows_only

ME = os.getpid()
SHELL, HOST, PWSH, CONHOST, EXPLORER, USERINIT, WINLOGON = range(900002, 900009)
CARRIER, WMI, SVCHOST, TERMINAL = range(900010, 900014)
GONE = 900099                   # a pid that cannot be opened
WALK = ('the fakes answer the calls the Windows walk makes (handles, the listing); on Linux the '
        'walk reads /proc (tests/proc/test_linux_process.py)')


def who(table, env=None):
    with patched(table, ME), mock.patch.object(store, 'agent_hosts',
                                               lambda *a: frozenset({'claude.exe'})):
        return store.caller(env=env or {})


@windows_only(WALK)
class ExitedShell(unittest.TestCase):
    """An agent's command whose shell has exited: its agent host holds the shell's handle (it
    waits on it), so the shell is still openable, and the listing may still name it."""

    def test_an_exited_shell_under_a_live_agent_host_is_the_agent_and_names_its_host(self):
        me = who({ME: FakeProc(400, SHELL, False, 'python.exe'),
                  SHELL: FakeProc(300, HOST, True, 'bash.exe'),
                  HOST: FakeProc(200, GONE, False, 'claude.exe')})
        self.assertEqual(me['role'], 'agent')
        self.assertEqual(me['host'], [HOST, 200])

    def test_an_exited_shell_whose_parent_is_gone_is_not_the_owner(self):
        me = who({ME: FakeProc(400, SHELL, False, 'python.exe'),
                  SHELL: FakeProc(300, GONE, True, None)})
        self.assertEqual(me['role'], 'agent')
        self.assertIsNone(me['host'])

    def test_an_exited_shell_whose_parent_pid_was_reused_is_not_the_owner(self):
        # HOST's pid now names a process made after SHELL: not SHELL's parent.
        me = who({ME: FakeProc(400, SHELL, False, 'python.exe'),
                  SHELL: FakeProc(300, HOST, True, None),
                  HOST: FakeProc(350, GONE, False, 'pwsh.exe')})
        self.assertEqual(me['role'], 'agent')

    def test_an_exited_shell_whose_parent_pid_a_process_it_cannot_open_reused_is_not_the_owner(self):
        # HOST's pid now names a SYSTEM svchost made after SHELL: listed, not openable; the
        # system's process listing gives its creation time.
        me = who({ME: FakeProc(400, SHELL, False, 'python.exe'),
                  SHELL: FakeProc(300, HOST, True, None),
                  HOST: FakeProc(350, GONE, False, 'svchost.exe', opens=False)})
        self.assertEqual(me['role'], 'agent')

    def test_an_agent_host_below_an_exited_shell_makes_the_agent_whatever_is_above(self):
        # The walk ends not-exited at a listed parent it cannot open; the host below decides.
        me = who({ME: FakeProc(500, HOST, False, 'python.exe'),
                  HOST: FakeProc(400, SHELL, False, 'claude.exe'),
                  SHELL: FakeProc(300, WINLOGON, True, None),
                  WINLOGON: FakeProc(100, GONE, False, 'winlogon.exe', opens=False)})
        self.assertEqual(me['role'], 'agent')
        self.assertEqual(me['host'], [HOST, 400])


@windows_only(WALK)
class OwnersChain(unittest.TestCase):
    def test_an_exited_hop_under_a_listed_parent_that_cannot_be_opened_is_the_owner(self):
        # No agent host anywhere in the chain: the walk ends not-exited (kitlib.proc.process
        # ancestry: the system's process listing dates that parent before the exited hop).
        me = who({ME: FakeProc(400, SHELL, False, 'python.exe'),
                  SHELL: FakeProc(300, WINLOGON, True, None),
                  WINLOGON: FakeProc(100, GONE, False, 'winlogon.exe', opens=False)})
        self.assertEqual(me['role'], 'owner')
        self.assertIsNone(me['host'])

    def test_a_chain_of_running_processes_with_no_agent_host_is_the_owner(self):
        me = who({ME: FakeProc(400, PWSH, False, 'python.exe'),
                  PWSH: FakeProc(300, CONHOST, False, 'powershell.exe'),
                  CONHOST: FakeProc(200, GONE, False, 'conhost.exe')})
        self.assertEqual(me['role'], 'owner')
        self.assertIsNone(me['host'])

    def test_a_shell_the_desktop_started_is_the_owner_past_the_exited_userinit(self):
        # OBSERVED 2026-10-24 on the kit machine: explorer.exe's parent, userinit (pid 11504),
        # had exited but was still openable, the listing no longer named it, and its handle
        # recorded winlogon.exe (pid 1016) as its parent. 71 running processes' walks ended at
        # it, among them powershell.exe, cmd.exe, python.exe and claude.exe. winlogon.exe was
        # listed, but OpenProcess on it failed with error 5 (access denied).
        me = who({ME: FakeProc(500, PWSH, False, 'python.exe'),
                  PWSH: FakeProc(400, EXPLORER, False, 'powershell.exe'),
                  EXPLORER: FakeProc(300, USERINIT, False, 'explorer.exe'),
                  USERINIT: FakeProc(200, WINLOGON, True, None),
                  WINLOGON: FakeProc(100, GONE, False, 'winlogon.exe', opens=False)})
        self.assertEqual(me['role'], 'owner')
        self.assertEqual(me['ancestors'], sorted([[PWSH, 400], [EXPLORER, 300], [USERINIT, 200]]))

    def test_the_desktop_chain_is_the_owners_when_winlogon_can_be_opened_too(self):
        me = who({ME: FakeProc(500, PWSH, False, 'python.exe'),
                  PWSH: FakeProc(400, EXPLORER, False, 'powershell.exe'),
                  EXPLORER: FakeProc(300, USERINIT, False, 'explorer.exe'),
                  USERINIT: FakeProc(200, WINLOGON, True, None),
                  WINLOGON: FakeProc(100, GONE, False, 'winlogon.exe')})
        self.assertEqual(me['role'], 'owner')
        self.assertIn([WINLOGON, 100], me['ancestors'])

    def test_a_scheduled_tasks_chain_under_svchost_is_the_owners(self):
        # OBSERVED 2026-10-24 on the kit machine: the hub (tools/hub.py serve) runs as python.exe
        # under the carrier pythonw.exe, under svchost.exe 2808 (the Schedule service), which this
        # user cannot open. svchost is no DETACHED_LAUNCHERS entry: the hub is the owner's.
        me = who({ME: FakeProc(400, CARRIER, False, 'python.exe'),
                  CARRIER: FakeProc(300, SVCHOST, False, 'pythonw.exe'),
                  SVCHOST: FakeProc(100, GONE, False, 'svchost.exe', opens=False)})
        self.assertEqual(me['role'], 'owner')

    def test_a_shell_in_windows_terminal_under_svchost_is_the_owners(self):
        # OBSERVED 2026-10-24: WindowsTerminal.exe -Embedding runs under svchost.exe (DcomLaunch).
        me = who({ME: FakeProc(500, PWSH, False, 'python.exe'),
                  PWSH: FakeProc(400, TERMINAL, False, 'powershell.exe'),
                  TERMINAL: FakeProc(300, SVCHOST, False, 'WindowsTerminal.exe'),
                  SVCHOST: FakeProc(100, GONE, False, 'svchost.exe', opens=False)})
        self.assertEqual(me['role'], 'owner')


@windows_only(WALK)
class StartedByWmi(unittest.TestCase):
    """A process Win32_Process.Create started (kitlib.lock.detach: a detached lock run, a
    submitted landing) is under WMI's provider host, whoever asked WMI: never the owner.
    OBSERVED 2026-10-24 on the kit machine: python.exe <- pythonw.exe (the carrier) <- WmiPrvSE.exe,
    listed and not openable by this user, for submitted landings; caller() read them as the owner.
    tests/lock/test_caller_wmi_real.py makes that chain on real processes."""

    def test_a_chain_that_stops_below_wmis_host_is_an_agent(self):
        me = who({ME: FakeProc(400, CARRIER, False, 'python.exe'),
                  CARRIER: FakeProc(300, WMI, False, 'pythonw.exe'),
                  WMI: FakeProc(100, GONE, False, 'WmiPrvSE.exe', opens=False)})
        self.assertEqual(me['role'], 'agent')
        self.assertIsNone(me['host'])
        self.assertEqual(me['ancestors'], [[CARRIER, 300]])

    def test_wmis_host_in_the_chain_is_an_agent_when_it_can_be_opened(self):
        # An elevated caller can open WmiPrvSE, so the walk goes on to the svchost above it.
        me = who({ME: FakeProc(400, CARRIER, False, 'python.exe'),
                  CARRIER: FakeProc(300, WMI, False, 'pythonw.exe'),
                  WMI: FakeProc(200, SVCHOST, False, 'WmiPrvSE.exe'),
                  SVCHOST: FakeProc(100, GONE, False, 'svchost.exe', opens=False)})
        self.assertEqual(me['role'], 'agent')
        self.assertIn([WMI, 200], me['ancestors'])

    def test_an_exited_carrier_under_wmis_host_is_an_agent(self):
        # The step past an exited hop: its handle records WMI's host as its parent, which the
        # walk cannot open, so it ends there not-exited, as it does at winlogon above userinit.
        me = who({ME: FakeProc(400, CARRIER, False, 'python.exe'),
                  CARRIER: FakeProc(300, WMI, True, None),
                  WMI: FakeProc(100, GONE, False, 'WmiPrvSE.exe', opens=False)})
        self.assertEqual(me['role'], 'agent')

    def test_this_process_directly_under_wmis_host_is_an_agent(self):
        me = who({ME: FakeProc(400, WMI, False, 'pythonw.exe'),
                  WMI: FakeProc(100, GONE, False, 'WmiPrvSE.exe', opens=False)})
        self.assertEqual(me['role'], 'agent')


@windows_only(WALK)
class WmisHostHasExited(unittest.TestCase):
    """WMI's provider host exits when idle, while the run it started goes on: its pid is then
    missing from the listing (or names a newer process), and the chain no longer shows WMI. The
    kit's own detached starts mark the process (store.DETACHED_ENV, set by kitlib.lock.detach.create),
    and the marker makes it an agent; a start the kit did not make is not marked, and reads as the
    owner then (docs/lock-api.md "Known limits")."""

    CHAIN = {ME: FakeProc(400, CARRIER, False, 'python.exe'),
             CARRIER: FakeProc(300, WMI, False, 'pythonw.exe')}       # WMI: not listed

    def test_a_marked_carrier_whose_wmi_host_has_exited_is_an_agent(self):
        me = who(self.CHAIN, {store.DETACHED_ENV: '1'})
        self.assertEqual(me['role'], 'agent')
        self.assertIsNone(me['host'])

    def test_a_marked_carrier_whose_wmi_hosts_pid_was_reused_is_an_agent(self):
        table = {**self.CHAIN, WMI: FakeProc(350, GONE, False, 'svchost.exe', opens=False)}
        self.assertEqual(who(table, {store.DETACHED_ENV: '1'})['role'], 'agent')

    def test_an_unmarked_carrier_whose_wmi_host_has_exited_reads_as_the_owner(self):
        # The named limit, pinned: nothing in the tree or the environment shows WMI any more.
        self.assertEqual(who(self.CHAIN)['role'], 'owner')

    def test_the_marker_never_makes_the_owner(self):
        # Demote-only: an agent host's chain stays an agent's and names its host, marked or not.
        me = who({ME: FakeProc(400, HOST, False, 'python.exe'),
                  HOST: FakeProc(300, GONE, False, 'claude.exe')}, {store.DETACHED_ENV: '1'})
        self.assertEqual((me['role'], me['host']), ('agent', [HOST, 300]))


class LauncherRole(unittest.TestCase):
    """store.tree_role on a detached run's launcher ancestry (MACHINE_LOCK_ORIGIN): a launcher that
    itself ran under WMI recorded an ancestry that stops below WMI's host."""

    def role(self, table, pairs):
        with patched(table, ME), mock.patch.object(store, 'agent_hosts',
                                                   lambda *a: frozenset({'claude.exe'})):
            return store.tree_role(pairs)

    def test_a_launcher_whose_ancestry_stops_below_wmis_host_is_an_agent(self):
        table = {ME: FakeProc(500, GONE, False, 'python.exe'),
                 PWSH: FakeProc(400, CARRIER, False, 'python.exe'),
                 CARRIER: FakeProc(300, WMI, False, 'pythonw.exe'),
                 WMI: FakeProc(100, GONE, False, 'WmiPrvSE.exe', opens=False)}
        self.assertEqual(self.role(table, [(PWSH, 400), (CARRIER, 300)]), 'agent')

    def test_a_launcher_under_the_schedulers_svchost_is_the_owners(self):
        table = {ME: FakeProc(500, GONE, False, 'python.exe'),
                 PWSH: FakeProc(400, CARRIER, False, 'python.exe'),
                 CARRIER: FakeProc(300, SVCHOST, False, 'pythonw.exe'),
                 SVCHOST: FakeProc(100, GONE, False, 'svchost.exe', opens=False)}
        self.assertEqual(self.role(table, [(PWSH, 400), (CARRIER, 300)]), 'owner')


if __name__ == '__main__':
    unittest.main()
