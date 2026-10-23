"""The backlog's audit log (kitlib/backlog/audit.py; docs/backlog.md "Audit log"): after a write
whose rows carry an AUDITED risk (kitlib/backlog/roles.py) is published, tools/backlog.py appends
one JSON line to `<pm_data>/audit/backlog.jsonl`, making the folder on its first write: the verb,
the record id, the session's role and name, the UTC time and the store commit the write published,
never the command's text. With no pm_data the line is skipped with a note; a log that cannot be
written is a warning after the write, never a refusal."""
import datetime
import json
import os
import re
import tempfile
import unittest
from unittest import mock

from kitlib import sessionmark
from kitlib.backlog import audit
from kitlib.backlog import audit_check
from kitlib.backlog import opener
from kitlib.backlog import plan as pmod
from kitlib.backlog import record as rmod
from kitlib.backlog import store as smod
from tests import inproc
from tests.backlog.helpers import ADD_FLAGS, kit_json_ref
from tests.gitfixture import TempRepo, fixture_env, rmtree
from tests.home import PM_REPO_FENCE_ENV, machine_config, pm_repo

BACKLOG = inproc.load_tool('backlog')
UTC = re.compile(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\Z')
PRICE = ('--priority', '2', '--priority-by', 'agent', '--effort', 's')    # set-other: MEDIUM
WHEN = datetime.datetime(2026, 10, 20, 13, 5, 6, tzinfo=datetime.timezone.utc)


class Pieces(unittest.TestCase):
    def test_the_audited_rows(self):
        for rows in (['close'], ['set-hold'], ['set-other'], ['set-text', 'set-other'],
                     ['pull-collision'], ['retarget']):
            with self.subTest(rows):
                self.assertTrue(audit.audited(rows))
        for rows in ([], ['read'], ['add'], ['pull'], ['set-text', 'set-price'], ['release']):
            with self.subTest(rows):
                self.assertFalse(audit.audited(rows))

    def test_the_entry_has_the_six_fields_in_order(self):
        e = audit.entry('close', 'X-1', 'owner', 's1', 'abc123', WHEN)
        self.assertEqual(list(e), list(audit.FIELDS))
        self.assertEqual(e, {'verb': 'close', 'id': 'X-1', 'role': 'owner', 'session': 's1',
                             'utc': '2026-10-20T13:05:06Z', 'store_sha': 'abc123'})

    def test_a_missing_value_is_a_dash(self):
        e = audit.entry('retarget', None, None, '', None, WHEN)
        self.assertEqual((e['id'], e['role'], e['session'], e['store_sha']), ('-', '-', '-', '-'))

    def test_the_role_is_the_tools_own(self):
        self.assertEqual(audit.role_of({}), 'owner')
        self.assertEqual(audit.role_of({sessionmark.ENV: 'coordinator'}), 'coordinator')
        self.assertEqual(audit.role_of({sessionmark.ENV: 'no-such-role'}), '-')

    def test_the_session(self):
        named = {sessionmark.SESSION_NAME_ENV: 'kit-x'}
        self.assertEqual(audit.session_of('s9', named), 's9')
        self.assertEqual(audit.session_of(None, named), 'kit-x')
        self.assertEqual(audit.session_of(None, {}), '-')

    def test_append_makes_the_folder_and_adds_a_line(self):
        data = tempfile.mkdtemp(prefix='kit-pmdata-')
        self.addCleanup(rmtree, data)
        path = audit.append(data, audit.entry('close', 'X-1', 'owner', '-', 'a1', WHEN))
        self.assertEqual(path, os.path.join(data, 'audit', 'backlog.jsonl'))
        audit.append(data, audit.entry('delete', 'X-2', 'pm', '-', 'a2', WHEN))
        with open(path, 'rb') as fh:
            raw = fh.read()
        self.assertNotIn(b'\r', raw)
        lines = raw.decode('utf-8').splitlines()
        self.assertEqual([json.loads(x)['id'] for x in lines], ['X-1', 'X-2'])
        self.assertEqual(os.listdir(data), ['audit'])          # no .gitignore, nothing else


class Tool(unittest.TestCase):
    """tools/backlog.py's writes against a temp repository, with pm_data in the machine config of
    a temp home."""

    def setUp(self):
        self.repo = TempRepo(bare_remote=True, prepare=kit_json_ref,
                             prepare_key='test_cli.Cli.kit_json_ref')
        self.addCleanup(self.repo.cleanup)
        self.home = tempfile.mkdtemp(prefix='kit-home-')
        self.addCleanup(rmtree, self.home)
        self.data = tempfile.mkdtemp(prefix='kit-pmdata-')
        self.addCleanup(rmtree, self.data)
        machine_config(self.home, {'pm_data': {'path': self.data}}, case=self)
        self.env = fixture_env({'USERPROFILE': self.home, 'HOME': self.home})
        for name in (sessionmark.ENV, sessionmark.SESSION_NAME_ENV):
            self.env.pop(name, None)
        self.log = os.path.join(self.data, 'audit', 'backlog.jsonl')

    def run_tool(self, *args, env=None):
        """(exit code, stdout, stderr) of backlog.py's main(argv) in this process."""
        e = dict(self.env)
        e.update(env or {})
        return inproc.run_main(BACKLOG.main, ['-C', self.repo.root] + list(args), e, self.repo.root)

    def ok(self, *args, env=None):
        rc, out, err = self.run_tool(*args, env=env)
        self.assertEqual(rc, 0, out + err)
        return out, err

    def lines(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding='utf-8') as fh:
            return [json.loads(x) for x in fh.read().splitlines()]

    def tip(self):
        return self.repo.git('ls-remote', 'origin', smod.STORE_REF).split('\t')[0]

    def add(self):
        self.ok('add', 'FOO', '--summary', 'a thing to do', '--scope', 'tools/x.py', *ADD_FLAGS)

    def test_a_low_write_writes_no_line(self):
        self.add()
        self.ok('set', 'FOO', '--summary', 'another thing to do')        # set-text: LOW
        self.assertFalse(os.path.exists(os.path.join(self.data, 'audit')))

    def test_a_medium_write_makes_the_folder_and_its_line(self):
        self.add()
        self.ok('close', 'FOO', '--reason', 'the secret reason words')
        self.assertEqual(len(self.lines()), 1)
        line = self.lines()[0]
        self.assertEqual(list(line), list(audit.FIELDS))
        self.assertEqual((line['verb'], line['id'], line['role'], line['session'], line['store_sha']),
                         ('close', 'FOO', 'owner', '-', self.tip()))
        self.assertRegex(line['utc'], UTC)
        with open(self.log, encoding='utf-8') as fh:
            self.assertNotIn('secret reason', fh.read())

    def test_each_audited_write_adds_one_line(self):
        self.add()
        self.ok('set', 'FOO', *PRICE)
        first = self.tip()
        self.ok('close', 'FOO', '--reason', 'the work is finished')
        self.assertEqual([(x['verb'], x['store_sha']) for x in self.lines()],
                         [('set', first), ('close', self.tip())])

    def test_a_marked_session_and_its_name(self):
        self.add()
        marked = {sessionmark.ENV: 'coordinator', sessionmark.SESSION_NAME_ENV: 'kit-x'}
        self.ok('set', 'FOO', *PRICE, env=marked)
        self.ok('--session', 's9', 'close', 'FOO', '--reason', 'the work is finished')   # unmarked: the owner
        self.assertEqual([(x['role'], x['session']) for x in self.lines()],
                         [('coordinator', 'kit-x'), ('owner', 's9')])

    def test_a_write_that_changes_nothing_writes_no_line(self):
        self.add()
        self.ok('set', 'FOO', *PRICE)
        out, _err = self.ok('set', 'FOO', *PRICE)
        self.assertIn('nothing was written', out)
        self.assertEqual(len(self.lines()), 1)

    def no_pm_data(self):
        """The pointer names a PM repository with no kit-pm-data.json record: no pm_data."""
        self.env[PM_REPO_FENCE_ENV] = pm_repo(self, record=False)

    def test_no_pm_data_is_a_note_and_no_line(self):
        self.no_pm_data()
        self.add()
        out, err = self.ok('close', 'FOO', '--reason', 'the work is finished')
        self.assertIn('WROTE', out)
        self.assertIn('NOTE:', err)
        self.assertIn('pm_data', err)
        self.assertFalse(os.path.exists(os.path.join(self.data, 'audit')))

    def test_a_log_that_cannot_be_written_is_a_warning_after_the_write(self):
        with open(os.path.join(self.data, 'audit'), 'w', encoding='utf-8') as fh:
            fh.write('a file where the folder goes\n')
        self.add()
        before = self.tip()
        out, err = self.ok('close', 'FOO', '--reason', 'the work is finished')
        self.assertIn('WROTE', out)
        self.assertNotEqual(self.tip(), before)                # the write stands
        self.assertIn('WARNING:', err)
        self.assertIn('audit', err)

    def test_any_other_failure_of_the_log_is_a_warning_and_the_exit_stands(self):
        """An exception audit.after_write does not catch (not ConfigError, PmDataError or OSError)
        after a published write: a WARNING naming it on standard error, exit 0, never a traceback."""
        self.add()
        before = self.tip()

        def boom(path, fields):
            raise ValueError('an odd failure')
        with mock.patch.object(BACKLOG.audit, 'append', boom):
            out, err = self.ok('close', 'FOO', '--reason', 'the work is finished')
        self.assertIn('WROTE', out)
        self.assertNotEqual(self.tip(), before)                # the write stands
        self.assertIn('WARNING:', err)
        self.assertIn('ValueError', err)
        self.assertIn('an odd failure', err)
        self.assertNotIn('Traceback', err)

    def test_migrate_is_audited(self):
        self.repo.write('docs/backlog/FOO.md', 'id: FOO\narea: xx\npriority: 2\npriority_by: agent\n'
                        'effort: s\nstate: open\nblocked_on: -\nscope: tools/x.py\n'
                        'summary: a thing to do\n')
        self.repo.commit('records', 'docs/backlog/FOO.md')
        self.repo.git('push', '-q', 'origin', 'main')
        self.ok('migrate', '--dry-run')
        self.assertEqual(self.lines(), [])
        self.ok('migrate')
        self.assertEqual([(x['verb'], x['id'], x['store_sha']) for x in self.lines()],
                         [('migrate', '-', self.tip())])


AUDIT = inproc.load_tool('backlog_audit')


def kinds(report):
    return [f.kind for f in report.findings]


class Check(unittest.TestCase):
    """kitlib.backlog.audit_check over a ref store the tool wrote, against its own audit log."""

    setUp, run_tool, ok, lines, tip, add, no_pm_data = (Tool.setUp, Tool.run_tool, Tool.ok, Tool.lines,
                                            Tool.tip, Tool.add, Tool.no_pm_data)

    def report(self, entries=None, since=0):
        _top, cfg, rules = opener.open_repo(self.repo.root)
        backend = smod.open_backend(self.repo.root, cfg, fetch=False)
        if entries is None:
            entries, _notes = audit_check.read_log(self.log)
        return audit_check.check(backend, rules, entries, since)

    def test_an_audited_change_with_no_line_is_unaudited(self):
        self.add()
        self.ok('close', 'FOO', '--reason', 'the work is finished')
        self.assertEqual(self.report().findings, [])                  # its line covers it
        r = self.report(entries=[])
        self.assertEqual(kinds(r), ['UNAUDITED'])
        found = r.findings
        self.assertIn(self.tip()[:10], found[0].text)
        self.assertIn('FOO', found[0].text)
        self.assertIn(' close rows=close ', found[0].text)

    def test_a_low_change_needs_no_line(self):
        self.add()
        self.ok('set', 'FOO', '--summary', 'another thing to do')        # set-text: LOW
        r = self.report(entries=[])
        self.assertEqual((r.changes, r.findings), (2, []))

    def test_a_set_of_another_field_needs_a_line(self):
        self.add()
        self.ok('set', 'FOO', *PRICE)                                     # set-other: MEDIUM
        r = self.report(entries=[])
        self.assertEqual(kinds(r), ['UNAUDITED'])
        self.assertIn('set-other', r.findings[0].text)
        self.assertEqual(self.report().findings, [])

    def test_the_by_design_writers_are_counted_not_flagged(self):
        self.add()
        self.ok('set', 'FOO', *PRICE)
        self.ok('--session', 'owner-hub', 'set', 'FOO', '--effort', 'm')
        self.ok('--session', 'gate-land', 'close', 'FOO', '--reason', 'landed at abc1234')
        r = self.report(entries=[])
        self.assertEqual(kinds(r), ['UNAUDITED'])                       # the first set alone
        self.assertEqual(dict(r.by_design), {'gate-land': 1, 'owner-hub': 1})

    def test_the_by_design_sessions_are_the_writers_own(self):
        from kitlib.backlog import at_landing
        from kitlib.dailyfull import file as dailyfile
        from kitlib.hubsite import recordwrite
        self.assertEqual(audit_check.BY_DESIGN,
                         (at_landing.SESSION, recordwrite.SESSION, dailyfile.SLUG))

    def test_a_role_not_in_the_map_is_unmapped(self):
        def line(role, utc='2026-10-20T13:05:06Z'):
            return (1, {'verb': 'close', 'id': 'X', 'role': role, 'session': 's',
                        'utc': utc, 'store_sha': 'f' * 40})
        self.add()
        since = WHEN.timestamp() - 60
        r = self.report([line('-'), line('no-such-role'), line('coordinator'), line('owner'),
                         line('-', utc='2026-10-19T13:05:06Z')], since=since)
        self.assertEqual(kinds(r), ['UNMAPPED', 'UNMAPPED'])
        self.assertIn('role=-', r.findings[0].text)
        self.assertIn('role=no-such-role', r.findings[1].text)

    def test_a_change_before_the_window_is_not_read(self):
        self.add()
        self.ok('close', 'FOO', '--reason', 'the work is finished')
        r = self.report(entries=[], since=datetime.datetime.now().timestamp() + 3600)
        self.assertEqual((r.changes, r.findings), (0, []))

    def test_a_line_without_a_sha_or_of_another_store_covers_nothing(self):
        self.add()
        self.ok('close', 'FOO', '--reason', 'the work is finished')
        lines = [(1, dict(self.lines()[0], store_sha='-')),
                 (2, dict(self.lines()[0], store_sha='e' * 40))]
        r = self.report(lines)
        self.assertEqual(kinds(r), ['UNAUDITED'])
        self.assertEqual((r.lines_read, r.no_sha), (2, 1))

    def test_a_subject_no_verb_wrote_is_unknown(self):
        self.assertIsNone(audit_check.verb_of('records'))
        for subject, verb in (('closes X: landed 1', 'close'), ('backlog: park X on Q-1', 'set-status'),
                              ('backlog: pull X onto b', 'pull'), ('migrate: docs/backlog/*.md', 'migrate'),
                              ('backlog: park X', 'set-status'),
                              ('backlog: re-park X with no question (was Q-1)', 'set-status'),
                              ('backlog: uncite 3 cite(s) in 2 record(s)', 'uncite')):
            self.assertEqual(audit_check.verb_of(subject), verb)

    def test_an_uncite_change_is_read_as_its_apply(self):
        # an uncite commit is the --apply, never the dry run (which writes nothing)
        self.assertEqual(audit_check._given('uncite', None, None, None), {'apply': True})

    def run_check(self, *args):
        return inproc.run_main(AUDIT.main, ['-C', self.repo.root, '--no-fetch'] + list(args),
                               self.env, self.repo.root)

    def test_the_exit_codes(self):
        self.add()
        rc, out, _err = self.run_check()
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.splitlines()[-1], 'OK')
        self.assertIn('NOT LOGGED BY DESIGN: 0', out)
        self.ok('close', 'FOO', '--reason', 'the work is finished')
        os.remove(self.log)
        rc, out, _err = self.run_check()
        self.assertEqual(rc, 1, out)
        self.assertIn('NOTE: no audit log', out)
        self.assertIn('UNAUDITED ', out)
        self.assertEqual(out.splitlines()[-1], 'FINDINGS 1')
        self.no_pm_data()
        rc, out, _err = self.run_check()
        self.assertEqual(rc, 2, out)
        self.assertIn('NOT CHECKED: the machine config names no pm_data', out)
        rc, _out, _err = self.run_check('--days', '0')
        self.assertEqual(rc, 2)

    def test_all_lists_the_covered_and_by_design_changes(self):
        self.add()
        self.ok('set', 'FOO', *PRICE)                                        # covered by its line
        self.ok('--session', 'daily-full', 'set', 'FOO', '--priority', '3')  # by design
        rc, out, _err = self.run_check('--all')
        self.assertEqual(rc, 0, out)
        listed = [x for x in out.splitlines() if x.startswith('  ')]
        self.assertEqual([x.split()[0] for x in listed], ['BY', 'COVERED'], out)
        self.assertIn('session=daily-full', listed[0])
        self.assertIn('backlog: set FOO', listed[1])
        self.assertIn('NOT LOGGED BY DESIGN: 1 (gate-land 0, owner-hub 0, daily-full 1)', out)
        rc, out, _err = self.run_check()
        self.assertEqual([x for x in out.splitlines() if x.startswith('  ')], [])

    def test_an_unreadable_log_is_not_checked(self):
        self.add()
        why = PermissionError(13, 'Access is denied', self.log)
        with mock.patch.object(audit_check, 'read_log', side_effect=why):
            rc, out, _err = self.run_check()
        self.assertEqual(rc, 2, out)
        self.assertEqual(out.splitlines(), [
            'NOT CHECKED: cannot read the log or the store: PermissionError: [Errno 13] Access is '
            'denied: %r' % self.log])


class CheckFolder(unittest.TestCase):
    """A folder store: read in the PM data worktree, NOT CHECKED anywhere else."""

    def setUp(self):
        from tests.backlog import test_pmdata_commit as pmc
        pmc.PmDataStore.setUp(self)

    def test_the_pm_data_worktree_is_read(self):
        self.store.write(lambda snap: pmod.plan_close(snap, self.ctx, 'BAR', 'the work is done'))
        r = audit_check.check(self.backend, rmod.Rules(), [], 0)
        self.assertEqual(kinds(r), ['UNAUDITED', 'UNKNOWN'])            # the close, then the seed
        self.assertIn('BAR', r.findings[0].text)
        self.assertIn('seed the store', r.findings[1].text)

    def test_a_folder_store_elsewhere_is_not_checked(self):
        other = smod.MainBackend(self.repo.root, 'kit/backlog')
        with self.assertRaises(audit_check.NotChecked):
            audit_check.check(other, rmod.Rules(), [], 0)


if __name__ == '__main__':
    unittest.main()
