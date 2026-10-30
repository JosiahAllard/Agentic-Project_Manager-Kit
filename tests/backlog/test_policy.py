"""kitlib.backlog.policy: a backlog command sorted into kitlib/backlog/roles.py's rows, the refusal for
a role that may not take one, the guard's reader of a backlog.py command line (pinned to the tool's
own parser), and tools/backlog.py's own check of its session's mark (docs/backlog.md "Roles")."""
import os
import tempfile
import unittest

from kitlib.backlog import policy
from kitlib.backlog import roles
from tests import inproc
from tests.backlog.helpers import ADD_FLAGS, kit_json_ref, parse
from tests.gitfixture import TempRepo, fixture_env, rmtree

BACKLOG = inproc.load_tool('backlog')

def record(**over):
    return parse(**over)


def words(*texts):
    return [(t, True) for t in texts]


class Sort(unittest.TestCase):
    def test_reads_and_dry_runs_are_reads(self):
        for verb in ('next', 'show', 'list', 'check', 'stale', 'landable'):
            self.assertEqual(policy.sort(verb), ['read'], verb)
        self.assertEqual(policy.sort('retarget', {}), ['read'])
        self.assertEqual(policy.sort('retarget', {'apply': True}), ['retarget'])
        self.assertEqual(policy.sort('uncite', {}), ['read'])
        self.assertEqual(policy.sort('uncite', {'apply': False}), ['read'])

    @unittest.skipUnless('uncite' in roles.ACTIONS['retarget'].verbs,
                         "roles.py's retarget row does not name uncite yet (owner approval)")
    def test_uncite_apply_is_the_retarget_row(self):
        self.assertEqual(policy.sort('uncite', {'apply': True}), ['retarget'])
        self.assertEqual(policy.sort('uncite', {'unknown': True}), ['retarget'])

    def test_clearing_resume_is_the_owners_and_the_ruling_keys_are_set_other(self):
        priced = record(priority='3', priority_by='agent')
        for key, row in (('resume', 'set-price-owner'), ('hold_ruling', 'set-other'),
                         ('priority_ruling', 'set-other')):
            given = policy.read_argv(words('set', 'FOO', '--unset', key)).given
            self.assertEqual(given['fields'], [key], key)
            self.assertEqual(policy.sort('set', given, priced), [row], key)
            self.assertEqual(policy.sort('set', given, None), [row], key)

    def test_a_set_sorts_by_its_fields_and_the_record(self):
        self.assertEqual(policy.sort('set', {'fields': ['scope', 'summary']}), ['set-text'])
        self.assertEqual(policy.sort('set', {'fields': ['body']}), ['set-text'])
        self.assertEqual(policy.sort('set', {'fields': ['area']}), ['set-other'])
        self.assertEqual(policy.sort('set', {'fields': ['scope', 'area']}), ['set-text', 'set-other'])
        self.assertEqual(policy.sort('set', {}), ['set-other'])
        priced = record(priority='3', priority_by='agent')
        owner = record(priority='3', priority_by='owner')
        one = {'fields': ['priority'], 'priority': ['2']}
        self.assertEqual(policy.sort('set', one, priced), ['set-price'])
        self.assertEqual(policy.sort('set', one, owner), ['set-price-owner'])
        self.assertEqual(policy.sort('set', one, None), ['set-price-owner'])   # not read: the strictest
        top = {'fields': ['priority'], 'priority': ['1']}
        self.assertEqual(policy.sort('set', top, priced), ['set-price-owner'])
        # retired `-1` sorts as a low price, so the plan's refusal, naming `block --on owner`, is seen
        retired = {'fields': ['priority'], 'priority': ['-1']}
        self.assertEqual(policy.sort('set', retired, priced), ['set-price'])
        unset = {'fields': ['priority'], 'priority': ['-']}
        self.assertEqual(policy.sort('set', unset, priced), ['set-price'])
        blocked = record(priority='3', priority_by='agent', state='blocked', blocked_on='owner',
                         owner_ask='which one?')
        self.assertEqual(policy.sort('set', one, blocked), ['set-price-owner'])
        by = {'fields': ['priority_by'], 'priority_by': ['agent']}
        self.assertEqual(policy.sort('set', by, owner), ['set-price-owner'])
        self.assertEqual(policy.sort('set', by, priced), ['set-other'])
        clear = {'fields': ['blocked_on'], 'blocked_on': ['-']}
        self.assertEqual(policy.sort('set', clear, blocked), ['set-other', 'unblock-owner'])
        self.assertEqual(policy.sort('set', clear, priced), ['set-other'])

    def test_pull_block_and_close_sort_by_their_options_and_the_record(self):
        free = record()
        held = record(state='blocked', blocked_on='owner', owner_ask='which one?')
        self.assertEqual(policy.sort('pull', {}, free), ['pull'])
        self.assertEqual(policy.sort('pull', {}, held), ['pull-held'])
        # a collision is the plan's to find, not a flag's: the CLI's sort never names it
        self.assertEqual(policy.sort('pull', {'reason': True}, free), ['pull'])
        self.assertEqual(policy.sort('block', {'on': ['BAR']}, free), ['block'])
        self.assertEqual(policy.sort('block', {'on': ['owner']}, free), ['block-owner'])
        self.assertEqual(policy.sort('block', {'owner_ask': True}, free), ['block-owner'])
        # `--until` on the owner schedules the owner's hold to end: the unblock-owner row too
        self.assertEqual(policy.sort('block', {'on': ['owner'], 'until': True}, held),
                         ['block-owner', 'unblock-owner'])
        self.assertEqual(policy.sort('block', {'on': [policy.UNREAD], 'until': True}, free),
                         ['unblock-owner'])
        self.assertEqual(policy.sort('block', {'on': ['BAR'], 'until': True}, free), ['block'])
        self.assertIn('unblock-owner', policy.sort('block', {'unknown': True, 'until': True}))
        self.assertEqual(policy.sort('unblock', {}, None), ['unblock-owner'])
        self.assertEqual(policy.sort('unblock', {'on': ['owner']}, free), ['unblock-owner'])
        self.assertEqual(policy.sort('unblock', {'on': ['BAR']}, None), ['block'])
        self.assertEqual(policy.sort('close', {}, free), ['close'])
        self.assertEqual(policy.sort('close', {}, held), ['close-held'])
        self.assertEqual(policy.sort('close', {}, None), ['close-held'])
        self.assertEqual(policy.sort('retire', {}, free), ['retire'])
        self.assertEqual(policy.sort('delete', {}, held), ['close-held'])
        owner = record(priority='3', priority_by='pm')
        self.assertEqual(policy.sort('retire', {}, owner), ['retire-priced'])
        self.assertEqual(policy.sort('delete', {}, owner), ['retire-priced'])
        self.assertEqual(policy.sort('close', {}, owner), ['close'])
        for verb, row in (('add', 'add'), ('release', 'release'), ('clear-review', 'clear-review'),
                          ('set-status', 'set-status'), ('unpark', 'set-status')):
            self.assertEqual(policy.sort(verb, {}, free), [row], verb)

    def test_what_is_not_read_counts_as_every_row_it_could_be(self):
        self.assertEqual(policy.sort(None), [n for n in roles.ACTIONS if n != 'read'])
        self.assertEqual(policy.sort('set', {'unknown': True}),
                         ['set-text', 'set-price', 'set-price-owner', 'set-other',
                          'set-widen-collision', 'unblock-owner'])
        self.assertEqual(policy.sort('show', {'unknown': True}), ['read'])
        self.assertEqual(policy.sort('catch-up'), ['read'])
        self.assertEqual(policy.sort('catch-up', {'apply': True}), ['catch-up'])
        self.assertEqual(policy.sort('frobnicate'), [])        # no row names it


class TopOnly(unittest.TestCase):
    """policy.top_only: the rows a plan run without the role check refuses (plan.refuse_top) are
    the ones only the owner and the PM may take."""

    def test_the_owner_gates_are_top_only(self):
        gates = ['set-price-owner', 'unblock-owner', 'close-held', 'retire-priced']
        for name in gates:
            self.assertEqual(roles.ACTIONS[name].allowed, roles.TOP, name)
        # and retarget, a rewrite of many records, not an owner gate on one
        self.assertEqual(policy.top_only(list(roles.ACTIONS)), gates + ['retarget'])

    def test_the_collision_rows_are_the_leads(self):
        self.assertEqual(sorted(policy.COLLISION_ROWS.values()),
                         ['pull-collision', 'set-widen-collision'])
        for name in policy.COLLISION_ROWS.values():
            self.assertEqual(roles.ACTIONS[name].allowed, roles.LEADS, name)
        self.assertEqual(policy.top_only(list(policy.COLLISION_ROWS.values())), [])


class Refusal(unittest.TestCase):
    def test_the_text_names_the_row_its_risk_who_may_and_the_role(self):
        self.assertIsNone(policy.refusal('coordinator', ['set-text', 'set-other']))
        text = policy.refusal('worker', ['set-text'])
        self.assertIn('the backlog action `set-text` (risk low) is for ', text)
        self.assertIn("worker (own-pull)", text)
        self.assertTrue(text.endswith("this session's role is worker"), text)
        self.assertIsNone(policy.refusal('worker', ['set-text'], (roles.OWN_PULL,)))
        text = policy.refusal(None, ['add'])
        self.assertTrue(text.endswith("this session's role is unlisted (only risk none)"), text)
        self.assertIn('`raw-write` (risk high) is for nobody', policy.refusal('owner', ['raw-write']))
        self.assertIsNone(policy.refusal(None, ['read']))
        for role in ('owner', 'coordinator', 'worker', None):      # no row names it: fail closed
            self.assertIn('no backlog action names this command', policy.refusal(role, []))

    def test_own_pull_is_the_record_pulled_by_that_branch(self):
        pulled = record(state='pulled', pulled_by='kit/wk')
        self.assertTrue(policy.own_pull(pulled, 'kit/wk'))
        self.assertFalse(policy.own_pull(pulled, 'kit/other'))
        self.assertFalse(policy.own_pull(pulled, None))
        self.assertFalse(policy.own_pull(record(), 'kit/wk'))
        self.assertFalse(policy.own_pull(None, 'kit/wk'))

    def test_the_tool_session_role_is_its_mark_and_unmarked_is_the_owner(self):
        self.assertEqual(policy.cli_role({}), 'owner')
        self.assertEqual(policy.cli_role({'PM_KIT_ROLE': 'coordinator'}), 'coordinator')
        self.assertEqual(policy.cli_role({'PM_KIT_ROLE': 'pm-kit:pm'}), 'pm')
        self.assertIsNone(policy.cli_role({'PM_KIT_ROLE': 'plain'}))
        self.assertIsNone(policy.cli_role({'PM_KIT_ROLE': 'owner'}))      # marked-owner: unlisted


class ReadArgv(unittest.TestCase):
    def test_globals_verb_id_and_options(self):
        a = policy.read_argv(words('-C', 'Q:/pm', '--session', 'kit', '--as', 'kit/wk', 'set', 'FOO',
                                   '--scope', 'tools/x.py', '--priority=2'))
        self.assertEqual((a.root, a.branch, a.verb, a.rid, a.help), ('Q:/pm', 'kit/wk', 'set', 'FOO', False))
        self.assertEqual(a.given['fields'], ['scope', 'priority'])
        self.assertEqual(a.given['priority'], ['2'])
        self.assertEqual(policy.read_argv(words('-CQ:/pm', 'show', 'FOO')).root, 'Q:/pm')
        a = policy.read_argv(words('block', 'FOO', '--on', 'owner', '--reason', 'r'))
        self.assertEqual((a.verb, a.given['on']), ('block', ['owner']))
        a = policy.read_argv(words('set', 'FOO', '--unset', 'priority', '--body-file', 'b.md'))
        self.assertEqual((a.given['fields'], a.given['priority']), (['priority', 'body'], ['-']))
        self.assertTrue(policy.read_argv(words('pull', 'FOO', '--ruling', 'x')).given['unknown'])
        self.assertTrue(policy.read_argv(words('retarget', 'A', 'B', '--apply')).given['apply'])
        a = policy.read_argv(words('uncite', '--apply'))
        self.assertEqual((a.verb, a.rid, a.given['apply']), ('uncite', None, True))
        self.assertFalse(policy.read_argv(words('uncite')).given.get('apply'))

    def test_help_runs_nothing(self):
        self.assertTrue(policy.read_argv(words('set', 'FOO', '--help')).help)
        self.assertTrue(policy.read_argv(words('-h')).help)
        self.assertFalse(policy.read_argv(words('set', 'FOO', '--', '-h')).help)

    def test_a_dash_dash_before_the_verb_leaves_its_options_options(self):
        """The tool's parser drops a `--` right before the verb, so the verb's options after it are
        still options: each such command sorts as it does without the `--`."""
        parser = BACKLOG.build_parser()
        rec = record()                                    # read, so a price sorts by its value
        rows = ((('retarget', 'A', 'B', '--apply'), ['retarget']),
                (('set', 'FOO', '--priority', '1'), ['set-price-owner']),
                (('pull', 'FOO', '--reason', 'R'), ['pull']),
                (('block', 'FOO', '--on', 'owner'), ['block-owner']))
        for argv, want in rows:
            for lead in ((), ('-C', 'Q:/pm')):
                line = lead + ('--',) + argv
                parser.parse_args(list(line))            # the tool takes it
                a = policy.read_argv(words(*line))
                self.assertEqual((a.verb, a.rid), (argv[0], argv[1]), line)
                self.assertEqual(policy.sort(a.verb, a.given, rec), want, line)
                self.assertEqual(a.given, policy.read_argv(words(*argv)).given, line)

    def test_a_word_it_cannot_read_counts_as_unknown(self):
        self.assertEqual(policy.read_argv([('set', True), ('FOO', True), ('$x', False)]).given,
                         {'fields': [], 'priority': [], 'on': [], 'priority_by': [], 'blocked_on': [],
                          'unknown': True})
        a = policy.read_argv([('-C', True), ('Q:/pm', True), ('$verb', False)])
        self.assertEqual((a.verb, a.given), (None, {'unknown': True}))
        for argv in (('--sess', 'kit', 'show', 'FOO'), ('set', 'FOO', '--scop', 'x'),
                     ('frobnicate', 'FOO'), ('set', 'FOO', '--scope')):
            self.assertTrue(policy.read_argv(words(*argv)).given.get('unknown'), argv)
        self.assertIsNone(policy.read_argv(words('--sess', 'kit', 'show', 'FOO')).verb)

    def test_the_options_are_the_tools_own(self):
        """Every option tools/backlog.py's parser takes, by verb, is in the reader's table with the
        same value-taking, so a spelling the tool takes is never read as another."""
        parser = BACKLOG.build_parser()

        def table(p):
            out = {}
            for act in p._actions:
                for name in act.option_strings:
                    if name not in policy.HELP:
                        out[name] = act.nargs != 0
            return out
        self.assertEqual(table(parser), policy.GLOBAL)
        subs = [a for a in parser._actions if a.__class__.__name__ == '_SubParsersAction'][0]
        self.assertEqual(sorted(subs.choices), sorted(policy.OPTIONS))
        for verb, sub in subs.choices.items():
            mine = policy.verb_options(verb)
            if mine is None:
                self.assertEqual(policy.sort(verb), ['read'], verb)
                continue
            want = table(sub)
            if verb == 'catch-up':
                want['--apply'] = False                  # its paths read as positionals
            self.assertEqual(want, mine, verb)


class CliRole(unittest.TestCase):
    """tools/backlog.py checks its own session's mark before a write plans."""

    def setUp(self):
        self.repo = TempRepo(bare_remote=True, prepare=kit_json_ref,
                             prepare_key='test_cli.Cli.kit_json_ref')
        self.addCleanup(self.repo.cleanup)
        home = tempfile.mkdtemp(prefix='kit-home-')
        self.addCleanup(rmtree, home)
        self.env = fixture_env({'USERPROFILE': home, 'HOME': home})
        self.env.pop('PM_KIT_ROLE', None)

    def run_tool(self, mark, *args):
        env = dict(self.env)
        if mark is not None:
            env['PM_KIT_ROLE'] = mark
        rc, out, err = inproc.run_main(BACKLOG.main, ['-C', self.repo.root] + list(args), env,
                                       self.repo.root)
        return rc, out + err

    def ok(self, mark, *args):
        rc, out = self.run_tool(mark, *args)
        self.assertEqual(rc, 0, out)
        return out

    def refused(self, mark, *args):
        rc, out = self.run_tool(mark, *args)
        self.assertEqual(rc, 2, out)
        self.assertIn('REFUSED: the backlog action `', out)
        self.assertIn('(kitlib/backlog/roles.py); ask a session whose role may', out)
        return out

    def body_file(self, text):
        folder = tempfile.mkdtemp(prefix='kit-body-')
        self.addCleanup(rmtree, folder)
        path = os.path.join(folder, 'body.md')
        with open(path, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(text + '\n')
        return path

    def seed(self):
        self.ok(None, 'add', 'FOO', '--summary', 'a thing to do', '--scope', 'tools/x.py', *ADD_FLAGS)
        self.ok(None, 'set', 'FOO', '--priority', '2', '--priority-by', 'agent', '--effort', 's')

    def test_unmarked_is_the_owner(self):
        self.seed()
        self.refused('coordinator', 'retarget', 'FOO', 'BAR', '--apply')
        self.ok(None, 'set', 'FOO', '--priority', '1', '--priority-by', 'owner')

    def test_the_uncite_dry_run_is_a_read_and_its_apply_is_top_only(self):
        self.seed()
        self.ok(None, 'set', 'FOO', '--body-file', self.body_file('Per R-9033, keep it.'))
        before = self.ok(None, 'show', 'FOO')
        out = self.ok('plain', 'uncite')
        self.assertIn('UNCITE: 1 cite(s) in 1 record(s)', out)
        self.assertIn('    + Per an owner ruling, keep it.', out)
        self.assertEqual(self.ok(None, 'show', 'FOO'), before)          # a dry run writes nothing
        for mark in ('coordinator', 'worker'):
            rc, out = self.run_tool(mark, 'uncite', '--apply')
            self.assertEqual(rc, 2, out)
            self.assertIn('REFUSED: ', out)
        self.assertEqual(self.ok(None, 'show', 'FOO'), before)

    @unittest.skipUnless('uncite' in roles.ACTIONS['retarget'].verbs,
                         "roles.py's retarget row does not name uncite yet (owner approval)")
    def test_the_owner_applies_uncite(self):
        self.seed()
        self.ok(None, 'set', 'FOO', '--body-file', self.body_file('Per R-9033, keep it.'))
        self.ok(None, 'uncite', '--apply')
        self.assertIn("Per an owner ruling, keep it.", self.ok(None, 'show', 'FOO'))
        self.assertIn('nothing to rewrite', self.ok('plain', 'uncite'))

    def test_a_role_may_not_take_a_row_roles_py_does_not_give_it(self):
        self.seed()
        out = self.refused('coordinator', 'set', 'FOO', '--priority', '1', '--priority-by', 'owner')
        self.assertIn('`set-price-owner` (risk medium)', out)
        self.assertIn("this session's role is coordinator", out)
        self.ok('coordinator', 'set', 'FOO', '--scope', 'tools/y.py')
        self.ok('pm', 'set', 'FOO', '--priority', '1', '--priority-by', 'pm')
        out = self.refused('plain', 'add', 'BAR', '--summary', 'x', '--scope', 'tools/z.py', *ADD_FLAGS)
        self.assertIn('unlisted (only risk none)', out)
        self.assertIn('FOO', self.ok('plain', 'show', 'FOO'))
        self.assertIn('scope:', self.ok(None, 'show', 'FOO'))

    def test_a_coordinator_may_not_set_the_end_of_an_owner_hold(self):
        # KIT-UNBLOCK-CONDITIONS review: a lead may block on the owner, never schedule that hold
        # to end (the hub job would clear it under Session: hub-condition)
        self.seed()
        self.ok('coordinator', 'block', 'FOO', '--on', 'owner', '--owner-ask', 'which one, Q-101?')
        before = self.ok(None, 'show', 'FOO')
        # the record already holds the owner's ask, so no new one is needed: the role decides
        out = self.refused('coordinator', 'block', 'FOO', '--on', 'owner', '--reason',
                           'until T-101 is done', '--until', 'todo:T-101 done')
        self.assertIn('`unblock-owner`', out)
        out = self.refused('coordinator', 'block', 'FOO', '--on', 'owner', '--owner-ask',
                           'which one now, Q-102?', '--until', 'question:Q-102 answered')
        self.assertIn('`unblock-owner`', out)
        self.assertEqual(self.ok(None, 'show', 'FOO'), before)
        self.ok('pm', 'block', 'FOO', '--on', 'owner', '--reason', 'until T-101 is done',
                '--until', 'todo:T-101 done')
        self.assertIn('until: owner todo:T-101 done', self.ok(None, 'show', 'FOO'))

    def test_catch_up_apply_is_for_the_leads_and_the_plain_listing_for_everyone(self):
        self.seed()
        for mark in ('worker', 'deep-worker', 'plain'):
            out = self.refused(mark, 'catch-up', '--apply', 'FOO.md')
            self.assertIn('`catch-up` (risk medium)', out)
            _, out = self.run_tool(mark, 'catch-up')
            self.assertNotIn('REFUSED', out)
        for mark in ('coordinator', 'pm', None):
            _, out = self.run_tool(mark, 'catch-up', '--apply', 'FOO.md')
            self.assertNotIn('the backlog action', out)            # past the role check
            _, out = self.run_tool(mark, 'catch-up')
            self.assertNotIn('REFUSED', out)

    def test_a_builder_may_edit_the_text_of_its_own_pull_only(self):
        self.seed()
        self.repo.git('checkout', '-b', 'kit/wk')
        out = self.refused('deep-worker', 'set', 'FOO', '--scope', 'tools/y.py')
        self.assertIn('`set-text`', out)
        self.ok('deep-worker', 'pull', 'FOO')
        self.ok('deep-worker', 'set', 'FOO', '--scope', 'tools/y.py')
        self.refused('deep-worker', '--as', 'kit/other', 'set', 'FOO', '--scope', 'tools/z.py')
        self.ok('deep-worker', 'release', 'FOO', '--reason', 'handed back to the queue')
        self.refused('deep-worker', 'set', 'FOO', '--scope', 'tools/z.py')
        self.assertIn('tools/y.py', self.ok(None, 'show', 'FOO'))


if __name__ == '__main__':
    unittest.main()
