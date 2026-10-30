"""The `until` key: a hold's end condition (docs/backlog.md "block --until",
kitlib/backlog/conditions.py). The grammar a program reads, the owner rule, the record checks,
`block --until`, the unblock that drops the line, and plan_condition_met, the hub job's write."""
import unittest

from kitlib.backlog import conditions as condmod
from kitlib.backlog import plan as pmod
from kitlib.backlog import record as rmod
from kitlib.backlog.errors import Refusal, Usage

from tests.backlog import test_cli, test_verbs
from tests.backlog.helpers import ADD_FLAGS, StoreCase, messages, parse, rec


class Readers(object):
    """conditions.evaluate's readers, answering from dicts (a missing key: cannot tell)."""

    def __init__(self, todos=None, questions=None, records=None, paths=None, plugins=None):
        self.todos, self.questions = todos or {}, questions or {}
        self.records, self.paths, self.plugins = records or {}, paths or {}, plugins or {}
        self.asked = []

    def todo_status(self, tid):
        self.asked.append(('todo', tid))
        return self.todos.get(tid)

    def question_status(self, qid):
        self.asked.append(('question', qid))
        return self.questions.get(qid)

    def record_closed(self, repo, rid):
        self.asked.append(('record', repo, rid))
        return self.records.get((repo, rid))

    def path_exists(self, path):
        self.asked.append(('path', path))
        return self.paths.get(path)

    def plugin_present(self, folder):
        self.asked.append(('plugin', folder))
        return self.plugins.get(folder)


def met(text, readers):
    return condmod.evaluate(condmod.parse(text), readers)


ABS = 'Q:\\builds\\out.zip'          # absolute, under no machine path folder
VARPATH = '%LOCALAPPDATA%\\pm\\out.zip'  # a path under a variable


class Grammar(unittest.TestCase):

    def test_each_form_parses(self):
        self.assertEqual(condmod.parse('todo:T-12 done'), ('todo', 'T-12', 'done'))
        self.assertEqual(condmod.parse('question:Q-3 answered'), ('question', 'Q-3', 'answered'))
        self.assertEqual(condmod.parse('record:kit:KIT-FOO-BAR closed'),
                         ('record', 'kit:KIT-FOO-BAR', 'closed'))
        self.assertEqual(condmod.parse('path:%s exists' % ABS), ('path', ABS, 'exists'))
        self.assertEqual(condmod.parse('path:%LOCALAPPDATA%\\a b\\c absent'),
                         ('path', '%LOCALAPPDATA%\\a b\\c', 'absent'))
        self.assertEqual(condmod.parse('plugin:92637108e79f absent'),
                         ('plugin', '92637108e79f', 'absent'))

    def test_anything_else_is_free_text(self):
        for text in ('when the client ships', 'todo:T-12 open', 'todo:T-x done', 'todo:T-1  done',
                     'question:Q-3 done', 'record:KIT-FOO closed', 'record:kit:FOO open',
                     'path:relative/out.zip exists', 'path:%s there' % ABS, 'plugin:x present',
                     'plugin:../up absent', 'tool:x done', '', 'todo:T-1 done later'):
            self.assertIsNone(condmod.parse(text), text)

    def test_todo_met_and_not(self):
        r = Readers(todos={'T-1': 'done', 'T-2': 'open'})
        self.assertIs(met('todo:T-1 done', r), True)
        self.assertIs(met('todo:T-2 done', r), False)
        self.assertIsNone(met('todo:T-3 done', r))      # no such to-do: cannot tell

    def test_question_met_on_every_answered_status(self):
        r = Readers(questions={'Q-1': 'answered', 'Q-2': 'filed', 'Q-3': 'logged', 'Q-4': 'done',
                               'Q-5': 'open'})
        for qid in ('Q-1', 'Q-2', 'Q-3', 'Q-4'):
            self.assertIs(met('question:%s answered' % qid, r), True, qid)
        self.assertIs(met('question:Q-5 answered', r), False)
        self.assertIsNone(met('question:Q-6 answered', r))

    def test_record_met_and_not(self):
        r = Readers(records={('kit', 'GONE'): True, ('kit', 'HERE'): False})
        self.assertIs(met('record:kit:GONE closed', r), True)
        self.assertIs(met('record:kit:HERE closed', r), False)
        self.assertIsNone(met('record:other:GONE closed', r))
        self.assertIn(('record', 'kit', 'GONE'), r.asked)

    def test_path_met_and_not(self):
        r = Readers(paths={ABS: True})
        self.assertIs(met('path:%s exists' % ABS, r), True)
        self.assertIs(met('path:%s absent' % ABS, r), False)
        r = Readers(paths={ABS: False})
        self.assertIs(met('path:%s exists' % ABS, r), False)
        self.assertIs(met('path:%s absent' % ABS, r), True)
        self.assertIsNone(met('path:%s exists' % ABS, Readers()))

    def test_a_path_variable_is_filled_and_an_unset_one_cannot_tell(self):
        self.assertEqual(condmod.expand('%HOMEX%/a/%B%', {'HOMEX': '/h', 'B': 'b'}), '/h/a/b')
        self.assertIsNone(condmod.expand('%HOMEX%/a', {}))
        self.assertIsNone(condmod.expand('%HOMEX%/a', {'HOMEX': ''}))

    def test_plugin_met_and_not(self):
        r = Readers(plugins={'old': False, 'live': True})
        self.assertIs(met('plugin:old absent', r), True)
        self.assertIs(met('plugin:live absent', r), False)
        self.assertIsNone(met('plugin:unknown absent', r))

    def test_owner_takes_only_an_owner_event(self):
        for text in ('path:%s exists' % ABS, 'record:kit:FOO closed', 'plugin:x absent',
                     'path:not-even-readable'):
            self.assertIn('owner event', condmod.owner_error('owner', text) or '', text)
        for text in ('todo:T-1 done', 'question:Q-1 answered', 'free text'):
            self.assertIsNone(condmod.owner_error('owner', text), text)
        self.assertIsNone(condmod.owner_error('live-client', 'path:%s exists' % ABS))


BLOCKED = dict(state='blocked', blocked_on='live-client')


class Checks(unittest.TestCase):
    """record.validate on the `until` key."""

    def test_a_line_on_a_blocker_is_clean(self):
        self.assertEqual(messages(until='live-client todo:T-1 done', **BLOCKED), [])
        self.assertEqual(messages(until='live-client whenever they ship', **BLOCKED), [])

    def test_a_member_the_record_is_not_blocked_on(self):
        out = messages(until='deliberate todo:T-1 done', **BLOCKED)
        self.assertTrue(any('not blocked on' in m for m in out), out)

    def test_one_line_per_member(self):
        out = messages(until=['live-client todo:T-1 done', 'live-client todo:T-2 done'],
                       **BLOCKED)
        self.assertTrue(any('one line per member' in m for m in out), out)

    def test_the_shape(self):
        for value in ('live-client', '- todo:T-1 done', 'nobody todo:T-1 done'):
            self.assertNotEqual(messages(until=value, **BLOCKED), [], value)

    def test_owner_on_a_non_owner_event(self):
        out = messages(until='owner path:%s exists' % ABS, **test_verbs.OWNER_BLOCKED)
        self.assertTrue(any('owner event' in m for m in out), out)


class RoundTrip(unittest.TestCase):

    def test_an_older_record_without_until_is_written_back_unchanged(self):
        for kw in ({}, BLOCKED, dict(test_verbs.OWNER_BLOCKED, body='Notes.\n\nMore notes.')):
            r = parse(**kw)
            self.assertEqual(pmod._text(r.pairs(), r.body), rmod.render(r.pairs(), r.body), kw)
            self.assertEqual(rmod.parse('backlog/FOO.md', pmod._text(r.pairs(), r.body),
                                        stem='FOO').pairs(), r.pairs(), kw)

    def test_a_line_on_a_blocker_is_kept_and_a_dangling_one_dropped(self):
        kw = dict(BLOCKED, until='live-client todo:T-1 done')
        r = parse(**kw)
        self.assertEqual(pmod._text(r.pairs(), r.body), rmod.render(r.pairs(), r.body))
        r = parse(state='open', blocked_on='-', until='live-client todo:T-1 done')
        bare = parse(state='open', blocked_on='-')
        self.assertEqual(pmod._text(r.pairs(), r.body), rmod.render(bare.pairs(), bare.body))


class BlockUntil(StoreCase):
    remote = False

    def test_block_writes_the_line(self):
        self.seed(rec('FOO'))
        change = self.verb('block', 'FOO', ['live-client'], until='  todo:T-4   done ')
        r = self.get('FOO')
        self.assertEqual((r.get('state'), r.all('until')), ('blocked', ['live-client todo:T-4 done']))
        self.assertIn('  until: live-client todo:T-4 done', change.lines)

    def test_free_text_is_kept_and_said_so(self):
        self.seed(rec('FOO'))
        change = self.verb('block', 'FOO', ['live-client'], until='when the client ships')
        self.assertEqual(self.get('FOO').all('until'), ['live-client when the client ships'])
        self.assertTrue(any('free text' in line for line in change.lines), change.lines)

    def test_a_reblock_replaces_that_members_line_only(self):
        self.seed(rec('FOO'), rec('BAR'))
        self.verb('block', 'FOO', ['live-client'], until='todo:T-1 done')
        self.verb('block', 'FOO', ['item:BAR'], until='record:kit:BAR closed')
        self.verb('block', 'FOO', ['live-client'], until='todo:T-2 done')
        self.assertEqual(self.get('FOO').all('until'),
                         ['item:BAR record:kit:BAR closed', 'live-client todo:T-2 done'])

    def test_refusals_write_nothing(self):
        self.seed(rec('FOO'), rec('BAR', **test_verbs.OWNER_BLOCKED))
        tip = self.remote_tip()
        cases = [('FOO', ['live-client', 'deliberate'], 'todo:T-1 done', 'exactly one'),
                 ('FOO', ['live-client'], '   ', 'needs the condition'),
                 ('BAR', ['owner'], 'path:%s exists' % VARPATH, 'owner event'),
                 ('BAR', ['owner'], 'record:kit:FOO closed', 'owner event'),
                 ('BAR', ['owner'], 'plugin:x absent', 'owner event'),
                 ('BAR', ['owner'], 'path:nowhere', 'owner event')]
        for rid, members, until, says in cases:
            with self.assertRaises(Usage) as ctx:
                self.verb('block', rid, members, until=until)
            self.assertIn(says, str(ctx.exception), until)
        for drive in test_verbs.MachinePaths.DRIVES:
            with self.assertRaises(Usage) as ctx:
                self.verb('block', 'FOO', ['live-client'], until='path:%sx\\y exists' % drive)
            self.assertIn('machine path', str(ctx.exception))
        self.assertEqual(self.remote_tip(), tip)

    def test_owner_takes_a_question(self):
        self.seed(rec('FOO'))
        self.verb('block', 'FOO', ['owner'], owner_ask='which one?', until='question:Q-9 answered',
                  role_checked=True)
        self.assertEqual(self.get('FOO').all('until'), ['owner question:Q-9 answered'])

    def test_an_owner_until_with_no_role_check_is_refused(self):
        # the unblock-owner row (policy.sort): the role check decides it, not a library caller
        self.seed(rec('FOO', **test_verbs.OWNER_BLOCKED))
        tip = self.remote_tip()
        with self.assertRaises(Refusal) as ctx:
            self.verb('block', 'FOO', ['owner'], until='todo:T-1 done')
        self.assertIn('unblock-owner', str(ctx.exception))
        self.assertEqual((self.remote_tip(), self.get('FOO').all('until')), (tip, []))
        self.verb('block', 'FOO', ['live-client'], until='todo:T-1 done')   # not the owner's

    def test_a_newly_blocked_member_drops_a_stale_line_it_did_not_give(self):
        # an older plugin's unblock left `until: live-client ...` behind; a re-block on that
        # member without --until must not bring it back to life
        self.seed(rec('FOO', state='blocked', blocked_on='item:BAR',
                      until=['item:BAR todo:T-2 done', 'live-client todo:T-1 done']), rec('BAR'))
        self.verb('block', 'FOO', ['live-client'])
        r = self.get('FOO')
        self.assertEqual((r.all('blocked_on'), r.all('until')),
                         (['item:BAR', 'live-client'], ['item:BAR todo:T-2 done']))
        self.verb('block', 'FOO', ['item:BAR'])             # already blocked on: its line stays
        self.assertEqual(self.get('FOO').all('until'), ['item:BAR todo:T-2 done'])

    def test_unblock_drops_that_members_line(self):
        self.seed(rec('FOO'), rec('BAR'))
        self.verb('block', 'FOO', ['live-client'], until='todo:T-1 done')
        self.verb('block', 'FOO', ['item:BAR'], until='record:kit:BAR closed')
        self.verb('unblock', 'FOO', 'live-client', 'the client shipped its build')
        r = self.get('FOO')
        self.assertEqual((r.all('blocked_on'), r.all('until')),
                         (['item:BAR'], ['item:BAR record:kit:BAR closed']))
        self.verb('unblock', 'FOO')
        r = self.get('FOO')
        self.assertEqual((r.get('state'), r.all('until')), ('open', []))


def commits(case):
    from kitlib.backlog import store as smod
    return int(case.repo.git('rev-list', '--count', smod.STORE_REF))


class ConditionMet(StoreCase):
    remote = False

    def test_one_write_then_nothing(self):
        self.seed(rec('FOO', state='blocked', blocked_on='live-client',
                      until='live-client todo:T-1 done'))
        before = commits(self)
        change = self.verb('condition_met', 'FOO', 'live-client', 'todo:T-1 done')
        self.assertEqual(change.subject,
                         'backlog: unblock FOO from live-client: condition met: todo:T-1 done')
        self.assertEqual(commits(self), before + 1)
        r = self.get('FOO')
        self.assertEqual((r.get('state'), r.all('blocked_on'), r.all('until')),
                         ('open', ['-'], []))
        again = self.verb('condition_met', 'FOO', 'live-client', 'todo:T-1 done')
        self.assertEqual(again.writes, {})
        self.assertEqual(commits(self), before + 1)

    def test_only_the_member_named_is_cleared(self):
        self.seed(rec('FOO', state='blocked', blocked_on=['live-client', 'item:BAR'],
                      until=['live-client todo:T-1 done', 'item:BAR todo:T-2 done']), rec('BAR'))
        self.verb('condition_met', 'FOO', 'live-client', 'todo:T-1 done')
        r = self.get('FOO')
        self.assertEqual((r.get('state'), r.all('blocked_on'), r.all('until')),
                         ('blocked', ['item:BAR'], ['item:BAR todo:T-2 done']))

    def test_a_condition_the_record_no_longer_holds_writes_nothing(self):
        self.seed(rec('FOO', state='blocked', blocked_on='live-client',
                      until='live-client todo:T-1 done'),
                  rec('BAR', state='blocked', blocked_on='deliberate'))
        before = commits(self)
        for rid, member, cond in (('FOO', 'live-client', 'todo:T-2 done'),
                                  ('FOO', 'deliberate', 'todo:T-1 done'),
                                  ('BAR', 'deliberate', 'todo:T-1 done')):
            self.assertEqual(self.verb('condition_met', rid, member, cond).writes, {})
        self.assertEqual(commits(self), before)
        self.assertEqual(self.get('FOO').all('blocked_on'), ['live-client'])

    def test_free_text_and_owner_on_a_non_owner_event_are_refused(self):
        self.seed(rec('FOO', state='blocked', blocked_on='live-client',
                      until='live-client when they ship'),
                  rec('BAR', until='owner path:%s exists' % ABS, **test_verbs.OWNER_BLOCKED))
        before = commits(self)
        with self.assertRaises(Refusal):
            self.verb('condition_met', 'FOO', 'live-client', 'when they ship')
        with self.assertRaises(Refusal):
            self.verb('condition_met', 'BAR', 'owner', 'path:%s exists' % ABS)
        self.assertEqual(commits(self), before)
        self.assertEqual(self.get('BAR').all('blocked_on'), ['owner'])

    def test_owner_clears_on_a_question(self):
        self.seed(rec('FOO', until='owner question:Q-2 answered', **test_verbs.OWNER_BLOCKED))
        change = self.verb('condition_met', 'FOO', 'owner', 'question:Q-2 answered')
        self.assertIsNone(change.ruling)
        r = self.get('FOO')
        self.assertEqual((r.get('state'), r.get('owner_ask'), r.all('until')), ('open', None, []))


class UntilCli(unittest.TestCase):
    setUp = test_cli.Cli.setUp
    run_tool = test_cli.Cli.run_tool
    ok = test_cli.Cli.ok

    def test_block_until_and_its_refusal(self):
        self.ok('add', 'FOO', '--summary', 'a thing to hold for now', '--scope', 'a/foo.py',
                *ADD_FLAGS)
        out = self.ok('block', 'FOO', '--on', 'live-client', '--until', 'todo:T-3 done')
        self.assertIn('until: live-client todo:T-3 done', out)
        self.assertIn('until: live-client todo:T-3 done', self.ok('show', 'FOO'))
        rc, out = self.run_tool('block', 'FOO', '--on', 'owner', '--owner-ask', 'which one, Q-123?',
                                '--until', 'plugin:abc absent')
        self.assertNotEqual(rc, 0, out)
        self.assertIn('owner event', out)


if __name__ == '__main__':
    unittest.main()
