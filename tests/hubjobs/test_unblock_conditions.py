"""kitlib.hubjobs.unblock_conditions: the hub job that clears a hold whose `until` condition is met,
run end to end against a ref store on a temporary repository with a bare remote, a temporary hub
store folder and a flag file under a variable (docs/action-hub.md "Timed jobs")."""
import json
import os
import tempfile
import threading
import unittest
from unittest import mock

from kitlib.backlog import record as rmod
from kitlib.backlog import store as smod
from kitlib.hubjobs import unblock_conditions as U
from kitlib.hubsite import jobs, recordwrite
from kitlib.launcher.settings import LaunchError
from tests.backlog.helpers import kit_json_ref, rec
from tests.gitfixture import TempRepo, fixture_env, rmtree

VAR = 'KIT_UNBLOCK_TEST_DIR'
FLAG = 'path:%%%s%%\\flag exists' % VAR
OWNER = dict(state='blocked', blocked_on='owner', owner_ask='which one, Q-123?')


class Ctx(object):
    def __init__(self):
        self.stop = threading.Event()
        self.events = []

    def event(self, word, detail=None):
        self.events.append((word, detail))


class Settings(object):
    """launcher Settings' repos and repo_path."""

    def __init__(self, repos):
        self.repos = repos

    def repo_path(self, name):
        if name in self.repos:
            return self.repos[name]
        raise LaunchError('repo %r is not in launcher.repos' % name)


class Case(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='kit-unblock-')
        self.addCleanup(rmtree, self.tmp)
        home = os.path.join(self.tmp, 'home')
        self.flags = os.path.join(self.tmp, 'flags')
        self.hub = os.path.join(self.tmp, 'hub')
        self.plugins = os.path.join(self.tmp, 'plugins')
        for d in (home, self.flags, os.path.join(self.hub, 'todos'),
                  os.path.join(self.hub, 'questions'), self.plugins):
            os.makedirs(d)
        env = mock.patch.dict(os.environ, fixture_env({'USERPROFILE': home, 'HOME': home,
                                                       VAR: self.flags}), clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.repo = TempRepo(bare_remote=True)
        self.addCleanup(self.repo.cleanup)
        kit_json_ref(self.repo)
        self.writes = []

    def seed(self, *records):
        """Publish records as they are, past every plan."""
        writes = {}
        for r in records:
            writes.update(r)
        backend = smod.RefBackend(self.repo.root, 'origin', smod.STORE_REF, smod.STORE_REF)
        backend.publish(smod.Store(backend, rmod.Rules()).load(), writes, 'seed\n')

    def doc(self, coll, doc_id, status):
        with open(os.path.join(self.hub, coll, doc_id + '.json'), 'w', encoding='utf-8') as f:
            json.dump({'id': doc_id, 'status': status}, f)

    def flag(self):
        open(os.path.join(self.flags, 'flag'), 'w').close()

    def write(self, root, rid, member, cond):
        self.writes.append((rid, member, cond))
        return recordwrite.condition_met(root, rid, member, cond)

    def job(self, closed=None):
        return U.UnblockJob(lambda: ([('kit', self.repo.root)], []),
                            lambda: U.Readers(self.hub, self.plugins,
                                              closed or (lambda repo, rid: None)),
                            write=self.write)

    def get(self, rid):
        backend = smod.RefBackend(self.repo.root, 'origin', smod.STORE_REF, smod.STORE_REF)
        return smod.Store(backend, rmod.Rules()).load().get(rid)

    def log(self):
        return self.repo.git('log', '--format=%B%x00', smod.STORE_REF).split('\x00')

    def tip(self):
        return self.repo.git('rev-parse', smod.STORE_REF).strip()


class Pass(Case):

    def setUp(self):
        Case.setUp(self)
        self.seed(rec('FOO', state='blocked', blocked_on='live-client',
                      until='live-client todo:T-1 done'),
                  rec('QUX', state='blocked', blocked_on=['live-client', 'item:FOO'],
                      until=['live-client ' + FLAG, 'item:FOO todo:T-2 done']),
                  rec('OWN', until='owner ' + FLAG, **OWNER),
                  rec('ASK', until='owner question:Q-4 answered', **OWNER),
                  rec('TXT', state='blocked', blocked_on='live-client',
                      until='live-client todo:T-1 done when the build is out'),
                  rec('UNK', state='blocked', blocked_on='live-client',
                      until='live-client todo:T-9 done'),
                  rec('OFF', state='blocked', blocked_on='deliberate',
                      until='live-client todo:T-1 done'))
        self.doc('todos', 'T-1', 'done')
        self.doc('todos', 'T-2', 'open')
        self.doc('questions', 'Q-4', 'open')
        self.flag()

    def test_a_met_condition_is_one_commit_and_one_push_and_a_rerun_does_nothing(self):
        before = self.tip()
        ctx = Ctx()
        note = self.job()(ctx)
        self.assertEqual(note, 'unblocked kit:FOO, kit:QUX')
        self.assertEqual(sorted(ctx.events), [(U.EVENT, 'kit:FOO'), (U.EVENT, 'kit:QUX')])
        new = self.repo.git('rev-list', '%s..%s' % (before, smod.STORE_REF)).split()
        self.assertEqual(len(new), 2, 'one commit per cleared hold')
        for body in self.log()[:2]:
            self.assertIn('Session: hub-condition', body)
            self.assertIn('condition met: ', body)
        self.assertEqual(self.get('FOO').get('state'), 'open')
        self.assertEqual(sorted(self.writes),
                         [('FOO', 'live-client', 'todo:T-1 done'), ('QUX', 'live-client', FLAG)])
        tip, again = self.tip(), Ctx()
        out = self.job()(again)
        self.assertIsInstance(out, jobs.Skip)
        self.assertEqual((self.tip(), again.events), (tip, []))

    def test_only_the_member_whose_condition_is_met_is_cleared(self):
        self.job()(Ctx())
        r = self.get('QUX')
        self.assertEqual((r.get('state'), r.all('blocked_on'), r.all('until')),
                         ('blocked', ['item:FOO'], ['item:FOO todo:T-2 done']))

    def test_what_is_never_acted_on(self):
        self.job()(Ctx())
        for rid, member in (('OWN', 'owner'), ('ASK', 'owner'), ('TXT', 'live-client'),
                            ('UNK', 'live-client'), ('OFF', 'deliberate')):
            r = self.get(rid)
            self.assertEqual((r.get('state'), r.all('blocked_on')), ('blocked', [member]), rid)
        self.assertNotIn('OWN', [w[0] for w in self.writes], 'owner + path: skipped, met or not')

    def test_a_hold_cleared_after_the_read_is_no_write_and_no_push(self):
        stale = U.open_snapshot(self.repo.root)
        recordwrite.unblock(self.repo.root, 'FOO')
        recordwrite.unblock(self.repo.root, 'QUX', on='live-client',
                            reason='the flag came early, cleared by hand')
        tip, ctx = self.tip(), Ctx()
        job = U.UnblockJob(lambda: ([('kit', self.repo.root)], []),
                           lambda: U.Readers(self.hub, self.plugins, lambda repo, rid: None),
                           write=self.write, snapshot=lambda root: stale)
        out = job(ctx)
        self.assertEqual(sorted(w[0] for w in self.writes), ['FOO', 'QUX'], 'both were tried')
        self.assertIsInstance(out, jobs.Skip)
        self.assertEqual((self.tip(), ctx.events), (tip, []))

    def test_an_owner_question_answered_clears_the_owner(self):
        self.doc('questions', 'Q-4', 'filed')
        ctx = Ctx()
        self.job()(ctx)
        self.assertIn((U.EVENT, 'kit:ASK'), ctx.events)
        r = self.get('ASK')
        self.assertEqual((r.get('state'), r.get('owner_ask'), r.all('until')), ('open', None, []))


class Watched(Case):

    def test_watched_leaves_out_what_the_job_may_not_act_on(self):
        self.seed(rec('A', state='blocked', blocked_on='live-client',
                      until='live-client todo:T-1 done'),
                  rec('B', until='owner record:kit:A closed', **OWNER),
                  rec('C', state='blocked', blocked_on='live-client', until='live-client soon'),
                  rec('D', state='open', blocked_on='-', until='live-client todo:T-1 done'),
                  rec('E', state='blocked', blocked_on='deliberate',
                      until='live-client todo:T-1 done'))
        snap, _store = U.open_snapshot(self.repo.root)
        self.assertEqual(U.watched(snap), [('A', 'live-client', 'todo:T-1 done')])


class Faults(Case):

    def test_an_unreadable_store_fails_the_pass_after_the_others(self):
        self.seed(rec('FOO', state='blocked', blocked_on='live-client',
                      until='live-client todo:T-1 done'))
        self.doc('todos', 'T-1', 'done')

        def snapshot(root):
            if root == 'broken':
                raise OSError('gone')
            return U.open_snapshot(root)
        job = U.UnblockJob(lambda: ([('bad', 'broken'), ('kit', self.repo.root)], []),
                           lambda: U.Readers(self.hub, None, lambda repo, rid: None),
                           write=self.write, snapshot=snapshot)
        ctx = Ctx()
        with self.assertRaises(U.ConditionsFailed) as got:
            job(ctx)
        self.assertIn('bad: the store could not be read', str(got.exception))
        self.assertEqual(ctx.events, [(U.EVENT, 'kit:FOO')])

    def test_a_hub_document_that_cannot_be_read_is_not_acted_on(self):
        self.seed(rec('FOO', state='blocked', blocked_on='live-client',
                      until='live-client todo:T-1 done'))
        with open(os.path.join(self.hub, 'todos', 'T-1.json'), 'w', encoding='utf-8') as f:
            f.write('{"status": "do')
        out = self.job()(Ctx())
        self.assertIsInstance(out, jobs.Skip)
        self.assertEqual(self.writes, [])
        self.assertIsNone(U.Readers(None, None, None).todo_status('T-1'))
        self.assertIsNone(U.Readers(None, None, None).plugin_present('x'))


class Readers(Case):

    def test_plugin_folder_present_and_gone(self):
        os.mkdir(os.path.join(self.plugins, 'live'))
        r = U.Readers(self.hub, self.plugins, None)
        self.assertIs(r.plugin_present('live'), True)
        self.assertIs(r.plugin_present('gone'), False)

    def test_record_closed_reads_the_named_repos_store(self):
        self.seed(rec('FOO'), rec('BAR'))
        recordwrite.delete(self.repo.root, 'BAR', 'a duplicate of FOO, filed twice by mistake')
        closed = U.RecordReader(Settings({'kit': self.repo.root}))
        self.assertIs(closed('kit', 'FOO'), False)
        self.assertIs(closed('kit', 'BAR'), True)
        self.assertIsNone(closed('kit', 'NEVER'))
        self.assertIsNone(closed('other', 'FOO'))

    def test_repo_roots_skips_a_repo_it_cannot_place(self):
        roots, notes = U.repo_roots(Settings({'kit': self.repo.root,
                                              'gone': os.path.join(self.tmp, 'nowhere')}))
        self.assertEqual(roots, [('kit', self.repo.root)])
        self.assertEqual(len(notes), 1)
        self.assertTrue(notes[0].startswith('gone skipped: '), notes)

    def test_make_needs_a_repository(self):
        sources = mock.Mock(home=os.environ['USERPROFILE'])
        with mock.patch('kitlib.launcher.settings.load', return_value=Settings({})):
            self.assertIsNone(U.make(sources))
        with mock.patch('kitlib.launcher.settings.load',
                        return_value=Settings({'kit': self.repo.root})):
            job = U.make(sources)
        self.assertEqual((job.name, job.every_s), ('unblock-conditions', 300))
        self.assertTrue(callable(job.run))


if __name__ == '__main__':
    unittest.main()
