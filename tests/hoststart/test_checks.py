import datetime
import os
import tempfile
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
                    coordinator_config={'cap': {}}, now=datetime.datetime(2026, 10, 12), machine_config={})
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

    def coordinator(self, **kw):
        base = dict(coordinator_config={}, now=datetime.datetime(2026, 10, 12), machine_config={})
        base.update(kw)
        return hoststart.shared_checks('coordinator', os.getcwd(), **base)

    def test_a_bound_state_note_runs_no_rulings_check(self):
        # The rulings reader is gone from coordinatorstart: no check may call it.
        with tempfile.TemporaryDirectory() as d:
            rows = self.coordinator(repo_name='demo', state_note=os.path.join(d, 'state.md'))
        self.assertFalse([r for r in rows if 'rulings' in r], rows)
        self.assertFalse([r for r in rows if 'AttributeError' in r], rows)

    def test_no_state_note_promises_no_ruling_comparison(self):
        rows = self.coordinator(repo_name='demo')
        self.assertFalse([r for r in rows if 'rulings' in r], rows)

    def test_a_state_note_inside_the_checkout_is_still_refused(self):
        rows = self.coordinator(state_note=os.path.join(os.getcwd(), 'state.md'))
        self.assertIn('state note must be an absolute path outside the checkout', rows)

    def test_the_coordinators_ledger_file_is_named(self):
        split = {'ledger_dir': 'D:/pm/rulings', 'ledger_repos': ['demo', 'pm']}
        rows = self.coordinator(repo_name='Demo', machine_config=split)
        self.assertIn('ledger (reference, HOW this repo works): %s' % os.path.join('D:/pm/rulings', 'demo.md'), rows)
        self.assertFalse([r for r in self.coordinator(repo_name='demo') if r.startswith('ledger')])

    def test_no_repo_name_no_ledger_line(self):
        split = {'ledger_dir': 'D:/pm/rulings', 'ledger_repos': ['demo']}
        self.assertFalse([r for r in self.coordinator(machine_config=split) if 'ledger' in r])

    def test_a_machine_config_that_cannot_be_read_adds_no_row(self):
        with mock.patch.object(coordinatorstart.kuser, 'user_config', side_effect=ValueError('broken')):
            rows = self.coordinator(repo_name='demo', machine_config=None)
        self.assertEqual(rows, self.coordinator(repo_name='demo', machine_config={}))
        self.assertFalse([r for r in rows if 'machine config' in r or 'ledger' in r], rows)

    def test_the_ledger_name_is_normalised(self):
        split = {'ledger_dir': 'D:/pm/rulings', 'ledger_repos': ['my-tools-repo', 'pm']}
        rows = self.coordinator(repo_name='My-Tools_Repo', machine_config=split)
        self.assertIn('ledger (reference, HOW this repo works): %s'
                      % os.path.join('D:/pm/rulings', 'my-tools-repo.md'), rows)

    def test_machine_summary_uses_api_snapshot(self):
        rows = hoststart.machine_lines({'revision': 2, 'runs': [{}], 'holds': [], 'queue': {'entries': [], 'error': None}})
        self.assertIn('1 runs', rows[0])
