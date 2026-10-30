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
from kitlib.backlog import roles
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
        for rows in (['close'], ['set-price-owner'], ['set-other'], ['set-text', 'set-other'],
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
                         (at_landing.SESSION, recordwrite.SESSION, dailyfile.SLUG,
                          recordwrite.CONDITION_SESSION))

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

    def test_a_write_subject_no_row_names_is_unknown(self):
        # a store commit whose subject reads as a verb roles.py has no row for sorts to no rows:
        # a forged commit with that subject must not pass unflagged
        self.add()
        ctx = opener.open_store(self.repo.root, fetch=False)
        ctx.store.write(lambda snap: smod.Change(
            {'FOO': snap.get('FOO').text + '\nforged line\n'},
            'backlog: uncite 1 cite(s) in 1 record(s)'))
        rowless = {n: r for n, r in roles.ACTIONS.items() if 'uncite' not in r.verbs}
        with mock.patch.object(roles, 'ACTIONS', rowless):
            r = self.report(entries=[])
        self.assertEqual(kinds(r), ['UNKNOWN'])
        self.assertIn(' uncite rows=none ', r.findings[0].text)

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
        self.assertIn('NOT LOGGED BY DESIGN: 1 (gate-land 0, owner-hub 0, daily-full 1, hub-condition 0)', out)
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


AT = '2026-10-24T10:00:00+00:00'


def question(qid='Q-1001', rid='FOO', at=AT, status='filed'):
    """A hub question document bound to record `rid`, answered at `at`."""
    return {'id': qid, 'status': status, 'title': 'which way', 'record': {'repo': 'pm', 'id': rid},
            'answer': {'choice': 0, 'label': 'this way', 'note': '', 'at': at, 'via': 'page'}}


class AnswerCheck(unittest.TestCase):
    """UNVERIFIED: an `answer:` line not written by owner-hub, or matching no answered or
    filed question in the hub store (read through a stand-in for the hub's client)."""

    run_tool, ok, tip, add = Tool.run_tool, Tool.ok, Tool.tip, Tool.add

    def setUp(self):
        Tool.setUp(self)
        # the library writes below read the machine config of the temp home, as the tool does
        patch = mock.patch.dict(os.environ, {'USERPROFILE': self.home, 'HOME': self.home})
        patch.start()
        self.addCleanup(patch.stop)

    def answer(self, session, qid='Q-1001', at=AT):
        """An answer line written straight through the store as `session` (plan_answer, the
        hub's own plan, so only the commit's Session: line tells the writer)."""
        ctx = opener.open_store(self.repo.root, fetch=False, session=session)
        ctx.store.write(lambda s: pmod.plan_answer(s, ctx, 'FOO', qid, at, 'which way',
                                                   'this way', role_checked=True))

    def report(self, docs=(), calls=None):
        _top, cfg, rules = opener.open_repo(self.repo.root)
        backend = smod.open_backend(self.repo.root, cfg, fetch=False)

        def questions():
            if calls is not None:
                calls.append(1)
            if isinstance(docs, Exception):
                raise docs
            return list(docs)
        return audit_check.check(backend, rules, [], 0, questions)

    def answers(self, r):
        return [f for f in r.findings if f.kind == audit_check.ANSWER]

    def test_a_genuine_filing_is_verified(self):
        from kitlib.hubsite import recordwrite
        self.add()
        recordwrite.answer(self.repo.root, 'FOO', 'Q-1001', AT, 'which way', 'this way')
        for status in audit_check.ANSWERED:
            with self.subTest(status):
                r = self.report([question(status=status)])
                self.assertEqual(self.answers(r), [])
                self.assertEqual(r.by_design['owner-hub'], 1)

    def test_a_forged_field_is_unverified(self):
        # the hub holds the question, but the line was committed by another session
        self.add()
        self.answer('kit')
        r = self.report([question()])
        found = self.answers(r)
        self.assertEqual(len(found), 1, r.findings)
        self.assertIn('answer: Q-1001 %s' % AT, found[0].text)
        self.assertIn('not written by owner-hub', found[0].text)

    def test_a_forged_session_is_unverified(self):
        # Session: owner-hub, so the change counts by design, but no hub question matches it
        self.add()
        self.answer('owner-hub', qid='Q-1002')
        for docs in ([], [question()], [question(qid='Q-1002', at='2026-10-24T09:00:00+00:00')],
                     [question(qid='Q-1002', rid='BAR')], [question(qid='Q-1002', status='open')]):
            with self.subTest(docs):
                r = self.report(docs)
                found = self.answers(r)
                self.assertEqual(len(found), 1, r.findings)
                self.assertIn('no answered or filed hub question Q-1002 on FOO', found[0].text)
                self.assertEqual(r.by_design['owner-hub'], 1)

    def test_a_re_answer_is_verified(self):
        # "Change answer": the hub files the new answer, which replaces the line; the hub then
        # holds only the newer `at`, and the first filing, still in the window, must pass
        from kitlib.hubsite import recordwrite
        later = '2026-10-24T11:00:00+00:00'
        self.add()
        recordwrite.answer(self.repo.root, 'FOO', 'Q-1001', AT, 'which way', 'this way')
        recordwrite.answer(self.repo.root, 'FOO', 'Q-1001', later, 'which way', 'that way')
        r = self.report([question(at=later)])
        self.assertEqual(self.answers(r), [])
        self.assertEqual(r.by_design['owner-hub'], 2)
        # a hub answer older than the line is no cover for it
        found = self.answers(self.report([question()]))
        self.assertEqual(len(found), 1, found)
        self.assertIn('answer: Q-1001 %s' % later, found[0].text)

    def test_covered_and_by_design_changes_read_no_record(self):
        # the window's records are read in one git process, shared by the answer check and
        # rows_of: a covered or by-design change starts none of its own
        self.add()
        self.ok('set', 'FOO', *PRICE)                                # covered by its line
        self.ok('--session', 'daily-full', 'set', 'FOO', '--priority', '3')    # by design
        reads = []
        real = audit_check.batch.cat_file

        def counted(specs, root):
            if any('^:' in spec for spec in specs):          # a change's records, not the tip's
                reads.append(specs)
            return real(specs, root)
        _top, cfg, rules = opener.open_repo(self.repo.root)
        backend = smod.open_backend(self.repo.root, cfg, fetch=False)
        entries, _notes = audit_check.read_log(self.log)
        with mock.patch.object(audit_check.batch, 'cat_file', counted):
            r = audit_check.check(backend, rules, entries, 0, lambda: [])
        self.assertEqual((r.covered, dict(r.by_design), r.findings), (1, {'daily-full': 1}, []))
        self.assertEqual(len(reads), 1, reads)
        reads[:] = []
        from kitlib.hubsite import recordwrite
        recordwrite.answer(self.repo.root, 'FOO', 'Q-1001', AT, 'which way', 'this way')
        with mock.patch.object(audit_check.batch, 'cat_file', counted):
            r = audit_check.check(backend, rules, entries, 0, lambda: [question()])
        self.assertEqual((self.answers(r), len(reads)), ([], 1), reads)

    def test_a_hub_that_cannot_be_read_is_not_checked(self):
        from kitlib.hubsite import serve
        self.add()
        self.answer('owner-hub')
        self.answer('kit', qid='Q-1002')               # no hub needed: never the hub's
        r = self.report(serve.NotRunning(1))
        found = self.answers(r)
        self.assertEqual(len(found), 1, r.findings)
        self.assertIn('answer: Q-1002 ', found[0].text)
        self.assertIn('not written by owner-hub', found[0].text)
        self.assertTrue(any(n.startswith('answers NOT CHECKED: the hub store could not be read')
                            for n in r.notes), r.notes)

    def test_run_gives_an_unreadable_hub_as_a_note_and_exit_ok(self):
        from kitlib.hubsite import serve
        self.add()
        self.answer('owner-hub')

        def questions():
            raise serve.NotRunning(1)
        code, lines = audit_check.run(self.repo.root, fetch=False, home=self.home,
                                      questions=questions)
        self.assertEqual(code, audit_check.OK, lines)
        self.assertEqual(lines[-1], 'OK')
        notes = [x for x in lines if x.startswith('NOTE: answers NOT CHECKED: the hub store could '
                                                  'not be read (NotRunning')]
        self.assertEqual(len(notes), 1, lines)
        self.assertFalse([x for x in lines if x.startswith(audit_check.ANSWER)], lines)

    def test_no_answer_change_asks_no_hub(self):
        self.add()
        self.ok('set', 'FOO', '--summary', 'another thing to do')
        calls = []
        r = self.report(ValueError('must not be asked'), calls)
        self.assertEqual((self.answers(r), calls), ([], []))

    def test_a_catch_up_commit_needs_the_hub_match(self):
        self.add()
        ctx = opener.open_store(self.repo.root, fetch=False, session='kit')
        ctx.store.write(lambda s: smod.Change(
            pmod.plan_answer(s, ctx, 'FOO', 'Q-1001', AT, 'which way', 'this way',
                             role_checked=True).writes,
            'backlog: catch up 1 record path'))
        self.assertEqual(self.answers(self.report([question()])), [])
        self.assertEqual(len(self.answers(self.report([]))), 1)

    def test_a_marked_session_may_not_use_a_by_design_name(self):
        self.add()
        before = self.tip()
        marked = {sessionmark.ENV: 'coordinator'}
        for name in audit_check.BY_DESIGN + ('Owner-Hub',):
            with self.subTest(name):
                rc, out, _err = self.run_tool('--session', name, 'set', 'FOO', '--effort', 'm',
                                              env=marked)
                self.assertEqual(rc, 2, out)
                self.assertIn('REFUSED', out)
                self.assertIn('a marked session may not use it', out)
        self.assertEqual(self.tip(), before)                      # nothing was written
        self.ok('--session', 'kit-x', 'set', 'FOO', '--effort', 'm', env=marked)
        self.ok('--session', 'owner-hub', 'set', 'FOO', '--effort', 'l')    # unmarked: the owner


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
