"""kitlib.gate.land posts the landing feed (docs/gate.md "Landing feed"): landed, pushed and the lock
released, one event per branch built from the land record the ledger line was, posted
best-effort. A hub that does not take it is a WARNING said as progress; the landing's outcome,
lines and code stand. A landing that did not land posts nothing."""
import json
import unittest

from kitlib import gate as G
from kitlib.gate import result, runner
from kitlib.hubsite import client, landings, serve
from kitlib.records import gate_ok

from tests.gate.test_land import BRANCH, LandCase
from tests.gate.test_land_closes_trailer import TRAILERS
from tests.gate.test_land_store import StoreLandCase, pulled


class FakeHub(object):
    """A hub client that records each post, and what the landing looked like when it came, or
    raises `error`."""

    def __init__(self, case, error=None):
        self.case, self.error, self.posted, self.seen = case, error, [], []

    def post(self, path, role, body=None):
        self.posted.append((path, role, body))
        self.seen.append((self.case.lock().read(), self.case.remote_main()))
        if self.error is not None:
            raise self.error
        return {'ok': True, 'id': 'L-%03d' % len(self.posted)}


class FeedCase(LandCase):
    def branch_with(self, message, name='feature', path='a.py'):
        wt = self.repo.worktree(name)
        self.repo.write(path, 'x = 1\n', root=wt)
        return self.repo.commit(message, path, cwd=wt)

    def ledger(self):
        with open(result.ledger_path(self.repo.root), encoding='utf-8') as f:
            return [json.loads(l) for l in f.read().splitlines()]

    def warnings(self, lines):
        return [l for l in lines if 'not posted to the hub feed' in l]

    def landed(self, out):
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertTrue(out.lines[-1].startswith('LANDED '), out.lines)
        self.assertEqual(self.remote_main(), out.merge)
        self.assertEqual(self.warnings(out.lines), [], 'a feed WARNING is progress, not outcome')


class Posts(FeedCase):
    def test_a_landing_posts_one_event_built_from_its_land_record_after_the_release(self):
        tip = self.branch_with('add a\n\nDeploy: restart the hub\n%s\n' % TRAILERS)
        hub, said = FakeHub(self), []
        lander = self.lander(BRANCH, self.main, tip, say=said.append, hub=hub)
        out = lander.run()
        self.landed(out)
        self.assertEqual(self.warnings(said), [])
        self.assertEqual(len(hub.posted), 1, hub.posted)
        path, role, ev = hub.posted[0]
        self.assertEqual((path, role), ('/landings', 'agent'))
        line = self.ledger()[-1]
        self.assertEqual({k: ev[k] for k in result.LEDGER_FIELDS},
                         {k: line[k] for k in result.LEDGER_FIELDS})
        self.assertEqual((ev['branch'], ev['tip'], ev['verdict']), (BRANCH, tip, 'pass'))
        self.assertEqual(sorted(ev), sorted(landings.FIELDS))
        self.assertEqual(ev['repo'], runner.repo_name(lander.cfg, self.repo.root))
        self.assertEqual((ev['main_branch'], ev['main']), ('main', out.merge))
        self.assertEqual((ev['records'], ev['deploy']), ([], 'restart the hub'))
        # the merged gate's mode, as its gate-OK record on the new main says (a select run here)
        self.assertEqual(ev['mode'], gate_ok.mode_of(gate_ok.read(self.repo.root, out.merge)[0]))
        self.assertEqual(ev['mode'], 'select')
        # posted outside the landing lock, after the push was verified
        self.assertEqual(hub.seen, [(None, out.merge)])

    def test_a_tip_without_a_deploy_trailer_has_no_deploy_note(self):
        tip = self.branch_with('add a\n\n%s\n' % TRAILERS)
        hub = FakeHub(self)
        self.landed(self.lander(BRANCH, self.main, tip, hub=hub).run())
        self.assertIsNone(hub.posted[0][2]['deploy'])

    def test_a_landing_that_did_not_land_posts_nothing(self):
        _, tip = self.branch('feature', {'RED': 'x\n'})
        hub = FakeHub(self)
        out = self.lander(BRANCH, self.main, tip, hub=hub).run()
        self.assertEqual(out.code, G.FAILED, out.lines)
        _, tip = self.branch('other', {'a.py': 'x = 1\n'})
        out = self.lander('kit/other', self.main, self.repo.initial, hub=hub).run()
        self.assertEqual(out.code, G.REFUSED, out.lines)
        self.assertEqual(hub.posted, [])


class HubFails(FeedCase):
    def test_each_hub_failure_is_a_said_warning_and_the_landing_stands(self):
        cases = [(serve.NotRunning(8790), 'no hub is running'),
                 (client.Refused(404, {'ok': False, 'error': {'code': 'not_found'}}),
                  'no /landings route'),
                 (serve.NoAnswer('timed out'), 'did not answer'),
                 (KeyboardInterrupt(), 'Ctrl+C'),
                 (RuntimeError('boom'), 'RuntimeError: boom')]
        for i, (error, why) in enumerate(cases):
            with self.subTest(error=type(error).__name__):
                main = self.main_now()
                _, tip = self.branch('f%d' % i, {'f%d.py' % i: 'x = 1\n'})
                hub, said = FakeHub(self, error), []
                out = self.lander('kit/f%d' % i, main, tip, say=said.append, hub=hub).run()
                self.landed(out)
                warn = self.warnings(said)
                self.assertEqual(len(warn), 1, said)
                self.assertIn(why, warn[0])
                self.assertTrue(warn[0].endswith('the landing stands'), warn[0])
                self.assertEqual(len(hub.posted), 1)

    def test_event_building_that_raises_is_one_warning_and_the_landing_stands(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        hub, said = FakeHub(self), []
        lander = self.lander(BRANCH, self.main, tip, say=said.append, hub=hub)
        lander.merged_mode = lambda: 1 / 0
        out = lander.run()
        self.landed(out)
        warn = self.warnings(said)
        self.assertEqual(len(warn), 1, said)
        self.assertIn('ZeroDivisionError', warn[0])
        self.assertEqual(hub.posted, [])

    def test_no_hub_here_is_a_warning_and_the_landing_stands(self):
        # the default client: the case's temp home has no agent token, so nothing is sent
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        said = []
        out = self.lander(BRANCH, self.main, tip, say=said.append).run()
        self.landed(out)
        self.assertEqual(len(self.warnings(said)), 1, said)


class Batch(StoreLandCase):
    def branch_with(self, message, name='feature', path='a.py'):
        wt = self.repo.worktree(name)
        self.repo.write(path, 'x = 1\n', root=wt)
        return self.repo.commit(message, path, cwd=wt)

    def test_a_batch_posts_one_event_per_branch_with_its_closed_records_and_one_main(self):
        self.seed(pulled('ONE', by='main'), pulled('TWO', by='main', scope=['b.py']),
                  pulled('KEEP', by='main'))
        tip_a = self.branch_with('a\n\nCloses: ONE\n%s\n' % TRAILERS)
        tip_b = self.branch_with('b\n\nCloses: TWO\nDeploy: reload\n%s\n' % TRAILERS,
                                 name='other', path='b.py')
        hub = FakeHub(self)
        out = self.lander([BRANCH, 'kit/other'], self.main, [tip_a, tip_b], hub=hub).run()
        self.assertEqual(out.code, G.OK, out.lines)
        evs = [body for _, _, body in hub.posted]
        self.assertEqual([(e['branch'], e['tip'], e['records'], e['deploy']) for e in evs],
                         [(BRANCH, tip_a, ['ONE'], None), ('kit/other', tip_b, ['TWO'], 'reload')])
        self.assertEqual({e['main'] for e in evs}, {out.merge})
        with open(result.ledger_path(self.repo.root), encoding='utf-8') as f:
            lines = [json.loads(l) for l in f.read().splitlines()][-2:]
        self.assertEqual([{k: e[k] for k in result.LEDGER_FIELDS} for e in evs],
                         [{k: l[k] for k in result.LEDGER_FIELDS} for l in lines])


if __name__ == '__main__':
    unittest.main()
