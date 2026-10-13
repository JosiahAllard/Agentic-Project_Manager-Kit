import datetime
import os
import unittest
from unittest import mock

from kitlib import hoststart, pmstart, coordinatorstart


class Checks(unittest.TestCase):
    def test_pm_reuses_checks_and_never_runs_automatic_update_or_claude_discovery(self):
        with mock.patch.object(pmstart, 'standing', return_value=['standing rule: test']) as standing, \
             mock.patch.object(pmstart, 'reminders', return_value=['reminder']) as reminders, \
             mock.patch.object(pmstart, 'kit_sync', side_effect=AssertionError('must not mutate')), \
             mock.patch.object(pmstart, 'list_processes', side_effect=AssertionError('wrong host')):
            cfg = {'standing': ['test'], 'reminders_file': 'configured'}
            rows = hoststart.shared_checks('pm', os.getcwd(), pm_config=cfg,
                    now=datetime.datetime(2026, 10, 12, tzinfo=datetime.timezone.utc))
        standing.assert_called_once_with(cfg)
        self.assertIn('reminder', rows)
        self.assertEqual(reminders.call_args.args[1], datetime.date(2026, 10, 12))

    def test_coordinator_reuses_cap_and_reports_missing_role_liveness(self):
        with mock.patch.object(coordinatorstart, 'cap', return_value=['agent cap 4']) as cap:
            rows = hoststart.shared_checks('coordinator', os.getcwd(), repo_name='demo',
                    coordinator_config={'cap': {}}, now=datetime.datetime(2026, 10, 12))
        self.assertIn('agent cap 4', rows)
        cap.assert_called_once()
        self.assertTrue(any('duplicate-coordinator check unavailable' in row for row in rows))

    def test_machine_errors_are_reported_as_errors_not_free_resources(self):
        rows = hoststart.shared_checks('pm', os.getcwd(), pm_config={}, lock_reader=lambda: {})
        self.assertTrue(any('could not check machine' in row for row in rows))
        self.assertFalse(any('0 runs' in row for row in rows))

    def test_session_errors_do_not_turn_into_empty_inventory(self):
        host = mock.Mock()
        host.sessions.side_effect = RuntimeError('proxy closed')
        rows = hoststart.shared_checks('pm', os.getcwd(), pm_config={}, session_host=host)
        self.assertTrue(any('proxy closed' in row for row in rows))
        self.assertFalse(any('0 records' in row for row in rows))

    def test_worker_has_no_controller_checks_or_machine_reads(self):
        reader = mock.Mock(side_effect=AssertionError('must not read'))
        self.assertEqual(hoststart.shared_checks('worker', os.getcwd(), lock_reader=reader), ())
        reader.assert_not_called()

    def test_machine_summary_uses_api_snapshot(self):
        rows = hoststart.machine_lines({'revision': 2, 'runs': [{}], 'holds': [], 'queue': {'entries': [], 'error': None}})
        self.assertIn('1 runs', rows[0])
