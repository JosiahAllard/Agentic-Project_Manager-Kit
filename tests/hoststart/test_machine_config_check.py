"""kitlib.hoststart.shared_checks: the machine config check (kitlib.pmstart.configcheck) for the
pm and coordinator roles, right after the standing lines; none for a worker. Every case runs the
real check on a temp home (the package stubs it: tests/pmstart/__init__.py stub_config_lines)."""
import datetime
import os
import unittest
from unittest import mock

from kitlib import hoststart
from kitlib.pmstart import configcheck
from tests import home as khome
from tests import pmstart as tpm

NOW = datetime.datetime(2026, 10, 12, tzinfo=datetime.timezone.utc)


def setUpModule():
    p = mock.patch.object(configcheck, 'lines', tpm.REAL_CONFIG_LINES)
    p.start()
    unittest.addModuleCleanup(p.stop)


class MachineConfigCheck(unittest.TestCase):
    def setUp(self):
        khome.temp_home(self)

    def rows(self, role, **kw):
        cfg = {'standing': ['s']}
        return hoststart.shared_checks(role, os.getcwd(), now=NOW, pm_config=cfg,
                                       coordinator_config=cfg, machine_config={}, **kw)

    def test_pm_shows_the_line_after_the_standing_rule(self):
        rows = self.rows('pm')
        i = rows.index('standing rule: s')
        self.assertEqual(rows[i + 1], configcheck.NO_POINTER)

    def test_coordinator_shows_the_line_after_the_standing_lines(self):
        rows = self.rows('coordinator')
        self.assertIn(configcheck.NO_POINTER, rows)
        self.assertEqual(rows[rows.index('- s') + 1], configcheck.NO_POINTER)

    def test_all_well_adds_no_row(self):
        for role in ('pm', 'coordinator'):
            rows = self.rows(role, config_lines=lambda: [])
            self.assertFalse(any('machine config' in r or 'machine_config' in r for r in rows), rows)

    def test_a_failing_check_is_one_row(self):
        def boom():
            raise OSError('disk gone')
        rows = self.rows('pm', config_lines=boom)
        self.assertTrue(any('machine_config' in r and 'disk gone' in r for r in rows), rows)
        self.assertIn('standing rule: s', rows)

    def test_codex_start_text_shows_the_line_for_a_pm_with_no_machine_config(self):
        # the Codex host's start text runs these shared checks (hosts.codex.adapter.start)
        from hosts.codex.adapter import start

        def checks(role, cwd, **kw):
            return hoststart.shared_checks(role, cwd, now=NOW, pm_config={'standing': ['s']},
                                           machine_config={}, lock_reader=lambda: {}, **{
                                               k: v for k, v in kw.items()
                                               if k in ('repo_name', 'state_note')})
        with mock.patch('kitlib.config.load') as load:
            load.return_value.repo = {'name': 'demo'}
            got = start.text('SessionStart', 'C:/kit', {'cwd': os.getcwd()}, {'PM_KIT_ROLE': 'pm'},
                             checks=checks)
        self.assertIn('- standing rule: s\n- ' + configcheck.NO_POINTER, got)

    def test_a_worker_gets_no_check(self):
        self.assertEqual(self.rows('worker'), ())


if __name__ == '__main__':
    unittest.main()
