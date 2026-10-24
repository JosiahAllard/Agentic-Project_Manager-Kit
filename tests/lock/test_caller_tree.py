"""kitlib.lock.store.caller reads who is asking from this process's tree: a chain that runs into a
shell that has exited, but that a handle keeps openable, is never the owner's on that account
(KIT-LOCK-CALLER-EXITED-SHELL), and the owner's own chains still are. Fakes only
(tests/proc/fakeproc.py): the process calls answer from a table, this process at its head."""
import os
import unittest
from unittest import mock

from kitlib.lock import store

from tests.proc.fakeproc import FakeProc, patched

ME = os.getpid()
SHELL, HOST, PWSH, CONHOST, EXPLORER, USERINIT, WINLOGON = range(900002, 900009)
GONE = 900099                   # a pid that cannot be opened


def who(table):
    with patched(table, ME), mock.patch.object(store, 'agent_hosts',
                                               lambda *a: frozenset({'claude.exe'})):
        return store.caller(env={})


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


    def test_an_agent_host_below_an_exited_shell_makes_the_agent_whatever_is_above(self):
        # The walk ends not-exited at a listed parent it cannot open; the host below decides.
        me = who({ME: FakeProc(500, HOST, False, 'python.exe'),
                  HOST: FakeProc(400, SHELL, False, 'claude.exe'),
                  SHELL: FakeProc(300, WINLOGON, True, None),
                  WINLOGON: FakeProc(100, GONE, False, 'winlogon.exe', opens=False)})
        self.assertEqual(me['role'], 'agent')
        self.assertEqual(me['host'], [HOST, 400])


class OwnersChain(unittest.TestCase):
    def test_an_exited_hop_under_a_listed_parent_that_cannot_be_opened_is_the_owner(self):
        # No agent host anywhere in the chain: the walk ends not-exited (kitlib.proc.process
        # ancestry's known limit: that parent's creation time cannot be checked).
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


if __name__ == '__main__':
    unittest.main()
