"""GET snapshot never waits on git for the backlog views while the backlog timer runs.

The page's only load is GET snapshot. It used to read every repository's backlog store in the
request thread (backlog.Views.get), so a busy machine left the page blank. With the timer running
the snapshot's backlog and completed views answer from what the timer's reads left
(backlog.Views.held) and start no git; with nothing read yet both are `unavailable`. With no timer
running a snapshot reads as before. The stand-in opener here blocks until the test releases it,
so a snapshot that reads git in its own thread is caught as one that does not answer."""
import datetime
import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock

from kitlib.hubsite import api, backlog, serve
from kitlib.hubsite.store import Store

WAIT = 30           # seconds a test waits for a thread it released before it fails
ANSWER_S = 2.0      # a snapshot that does not wait on git answers well inside this
TIMER = 'hub-backlog-refresh'       # backlog.Views' timer thread's name
OPTIONS = [{'label': 'first', 'description': 'b'}, {'label': 'second', 'description': 'c'}]


def today():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d')


def fake_read_at(name, root, commit, folder, since, days=backlog.DAYS, flaws=None):
    """Stands in for backlog.read_at: one open record and one closed today, no git."""
    return {'repo': name, 'days': days, 'parked': [],
            'open': [{'id': name.upper() + '-1', 's': 'open', 't': 'an open record'}],
            'completed': [{'id': name.upper() + '-0', 'd': today(), 't': 'a closed record'}]}


class Opener(object):
    """Stands in for backlog.open_read: notes (root, fetch, thread name) of every call, and a
    call for a root in `blocked` waits until `gate` is set (WAIT only bounds a test that has
    already failed). `entered` is released as each call begins."""

    def __init__(self, blocked):
        self.blocked = set(blocked)
        self.gate = threading.Event()
        self.calls = []
        self.entered = threading.Semaphore(0)
        self._guard = threading.Lock()

    def __call__(self, root, fetch):
        with self._guard:
            self.calls.append((root, fetch, threading.current_thread().name))
        self.entered.release()
        if root in self.blocked and not self.gate.wait(WAIT):
            raise AssertionError('the opener was never released')
        return ('c' * 40, 'backlog', True) if fetch else ('c' * 40, 'backlog')

    def outside_timer(self):
        with self._guard:
            return [c for c in self.calls if c[2] != TIMER]


class Pause(object):
    """Stands in for the timer's wait between rounds: each pause releases `rounds` and lasts
    until the timer is stopped, so a test drives exactly the rounds it waits for."""

    def __init__(self):
        self.rounds = threading.Semaphore(0)

    def __call__(self, stop, seconds):
        self.rounds.release()
        return stop.wait(WAIT)


class SnapshotCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='kit-hub-snapnb-')
        patcher = mock.patch.object(backlog, 'read_at', fake_read_at)
        patcher.start()
        self._patcher = patcher
        self.store = Store.open(os.path.join(self.root, 'store'))
        self.threads = []
        self.views_b = None
        self.opener = None

    def tearDown(self):
        if self.opener is not None:
            self.opener.gate.set()          # a blocked read (the red run's) ends
        if self.views_b is not None:
            self.assertTrue(self.views_b.stop(WAIT), 'the timer thread did not end')
        for t in self.threads:
            t.join(WAIT)
        alive = [t.name for t in self.threads if t.is_alive()]
        self._patcher.stop()
        self.store.close()
        shutil.rmtree(self.root, True)
        self.assertEqual(alive, [], 'threads left running')

    def build(self, repos, blocked=(), refresh_s=60, reuse_s=backlog.REUSE_S):
        """A backlog.Views over `repos` (names; each gets a folder), wrapped as serve.build
        wraps it: SiteViews, the timer's rounds followed for health, and the Api."""
        pairs = []
        for name in repos:
            folder = os.path.join(self.root, name)
            os.makedirs(folder)
            pairs.append((name, folder))
        self.opener = Opener([dict(pairs)[n] for n in blocked])
        self.pause = Pause()
        self.views_b = backlog.Views(pairs, refresh_s=refresh_s, opener=self.opener,
                                     reuse_s=reuse_s, wait=self.pause)
        self.views = serve.SiteViews(self.store, serve.Sources(tokens=self.root), self.views_b)
        serve._follow_rounds(self.views_b, self.views)
        self.api = api.Api(self.store, self.views)
        status, obj = self.api.call('POST', api.BASE + '/questions', api.AGENT,
                                    {'title': 'a question', 'repo': 'kit', 'options': OPTIONS,
                                     'class': 'go'})
        self.assertEqual(status, 200, obj)
        self.qid = obj['id']
        return dict(pairs)

    def snapshot(self, within=ANSWER_S):
        """GET snapshot, in a thread of its own as a request is: its answer, or a failure when
        it has not answered within `within` seconds."""
        got = []
        t = threading.Thread(target=lambda: got.append(
            self.api.call('GET', api.BASE + '/snapshot', api.OWNER)), name='snapshot', daemon=True)
        self.threads.append(t)
        t.start()
        t.join(within)
        self.assertFalse(t.is_alive(), 'GET snapshot waited on the backlog read')
        status, obj = got[0]
        self.assertEqual(status, 200, obj)
        return obj

    def assertQuestions(self, snap):
        self.assertEqual(snap['collections']['questions'][self.qid]['title'], 'a question')
        self.assertEqual(snap['views']['notices'], [])

    def assertFilled(self, snap, name):
        self.assertNotIn('backlog', snap['unavailable'])
        self.assertNotIn('completed', snap['unavailable'])
        entry = [e for e in snap['views']['backlog'] if e['repo'] == name][0]
        self.assertEqual(([i['id'] for i in entry['items']], entry['error']),
                         ([name.upper() + '-1'], None))
        done = [e for e in snap['views']['completed'] if e['repo'] == name][0]
        self.assertEqual([i['id'] for i in done['items']], [name.upper() + '-0'])


class WithTimer(SnapshotCase):

    def test_a_snapshot_before_the_first_round_answers_without_the_backlog(self):
        self.build(['alpha'], blocked=['alpha'])
        self.assertTrue(self.views_b.start())
        self.assertTrue(self.opener.entered.acquire(timeout=WAIT))     # the round is in git
        snap = self.snapshot()
        self.assertEqual((snap['views']['backlog'], snap['views']['completed']), (None, None))
        self.assertEqual(snap['unavailable']['backlog'], serve.NotReadYet.__name__)
        self.assertEqual(snap['unavailable']['completed'], serve.NotReadYet.__name__)
        self.assertQuestions(snap)
        self.assertEqual(self.opener.outside_timer(), [])          # no git in the request thread
        h = self.views.health()
        self.assertEqual((h['backlog'], h['backlog_at']), ([], None))   # the held answer is no read
        # the round ends: the next snapshot carries what it read
        self.opener.gate.set()
        self.assertTrue(self.pause.rounds.acquire(timeout=WAIT))
        snap = self.snapshot()
        self.assertFilled(snap, 'alpha')
        self.assertQuestions(snap)
        self.assertEqual(self.opener.outside_timer(), [])
        self.assertIsNotNone(self.views.health()['backlog_at'])

    def test_a_held_view_is_answered_however_old_and_starts_no_read(self):
        self.build(['alpha'], reuse_s=0)            # nothing held is ever "recent"
        self.assertTrue(self.views_b.start())
        self.assertTrue(self.pause.rounds.acquire(timeout=WAIT))
        before = list(self.opener.calls)
        for _ in range(3):
            self.assertFilled(self.snapshot(), 'alpha')
        self.assertEqual(self.opener.calls, before)

    def test_repositories_read_so_far_show_and_the_rest_say_not_read_yet(self):
        roots = self.build(['alpha', 'beta'], blocked=['beta'])
        self.assertTrue(self.views_b.start())
        # the round fetches and reads alpha (the read reuses the fetch's open), then waits in
        # beta's fetch
        self.assertTrue(self.opener.entered.acquire(timeout=WAIT))
        self.assertTrue(self.opener.entered.acquire(timeout=WAIT))
        self.assertEqual(self.opener.calls[-1][:2], (roots['beta'], True))
        snap = self.snapshot()
        self.assertFilled(snap, 'alpha')
        beta = [e for e in snap['views']['backlog'] if e['repo'] == 'beta'][0]
        self.assertEqual((beta['items'], beta['error']), ([], backlog.NOT_READ))
        self.assertTrue(backlog.fixed(backlog.NOT_READ))
        self.assertEqual(self.opener.outside_timer(), [])

    def test_the_backlog_and_completed_routes_still_read_in_their_own_thread(self):
        roots = self.build(['alpha'], reuse_s=0)
        self.assertTrue(self.views_b.start())
        self.assertTrue(self.pause.rounds.acquire(timeout=WAIT))
        for path in ('/backlog', '/completed'):
            status, obj = self.api.call('GET', api.BASE + path, api.AGENT)
            self.assertEqual(status, 200, obj)
            self.assertEqual(obj['repos'][0]['repo'], 'alpha')
        self.assertEqual(self.opener.outside_timer(),
                         [(roots['alpha'], False, threading.current_thread().name)] * 2)


class WithoutTimer(SnapshotCase):

    def test_with_no_timer_configured_the_snapshot_reads_the_backlog_itself(self):
        roots = self.build(['alpha'], refresh_s=None)
        self.assertFalse(self.views_b.start())
        snap = self.snapshot(WAIT)
        self.assertFilled(snap, 'alpha')
        self.assertQuestions(snap)
        self.assertEqual([c[:2] for c in self.opener.outside_timer()], [(roots['alpha'], False)])

    def test_a_timer_configured_but_not_running_reads_as_a_server_with_none(self):
        self.build(['alpha'])                       # refresh_s set; start() never called
        self.assertFilled(self.snapshot(WAIT), 'alpha')
        self.assertEqual(len(self.opener.outside_timer()), 1)

    def test_held_answers_none_before_any_read(self):
        self.build(['alpha'], refresh_s=None)
        self.assertIsNone(self.views_b.held())
        self.views_b.get()
        got = self.views_b.held()
        self.assertEqual([e['repo'] for e in got['repos']], ['alpha'])
        self.assertIsNone(got['error'])


if __name__ == '__main__':
    unittest.main()
