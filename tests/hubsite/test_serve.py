"""kitlib.hubsite.serve: the server wired from its parts. Every hub here serves a temp store on
port 0 from stand-in sources: no machine config, no real repository, no git, no network past the
loopback address. Two classes take a real part instead of its stand-in: TimerCase a
backlog.Views whose repository has no checkout (no git), RealSyncCase a gitsync.GitSync over a
temp repository on the fake git. Every hub a test builds is closed before the test ends, and no
listener thread may outlive its test."""
import _thread
import copy
import datetime
import json
import os
import shutil
import signal
import socket
import tempfile
import threading
import unittest
from unittest import mock

from kitlib.config.types import ConfigError
from kitlib.hubsite import auth, backlog, gitsync, httpd, serve
from kitlib.hubsite import settings as hub_settings
from kitlib.hubsite.api import BASE
from kitlib.hubsite.store import Store, StoreBusy
from tests.hubsite import httpfix
from tests.hubsite import test_gitsync as gitcase       # its Case: a data work tree on the fake git
from tests.hubsite.httpfix import TIMEOUT, bearer, get, post, send

HEALTH = BASE + '/health'
UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 1, 10, 12, 0, tzinfo=UTC)
ANSWER = {'at': '2026-01-10T12:00:00+00:00', 'error': None, 'repos': [
    {'repo': 'Alpha', 'days': 30, 'error': None, 'stale': False, 'fetch_error': None,
     'fetched': '2026-01-10T11:00:00+00:00',
     'open': [{'id': 'A-1', 't': 'one', 'p': 1, 's': 'ready'},
              {'id': 'A-2', 't': 'two', 'p': 2, 's': 'parked'}],
     'parked': [{'id': 'A-2', 't': 'two', 'p': 2, 's': 'parked'}],
     'completed': [{'id': 'A-0', 't': 'old', 'd': '2026-01-01'},
                   {'id': 'A-9', 't': 'new', 'd': '2026-01-09T10:00:00+00:00'}]},
    {'repo': 'beta', 'days': 30, 'error': 'git timed out', 'stale': True, 'fetched': None,
     'fetch_error': 'git timed out', 'open': [], 'parked': [], 'completed': []}]}
SECRET = 'X:\\somewhere\\private'
REMOTE = 'https://someone:s3cret@git.example.test/team/data.git'
WHY = "fatal: unable to access '%s/': Could not resolve host" % REMOTE     # git quotes the remote
CREDENTIALS = ('someone', 's3cret')


class FakeBacklog(object):
    """What serve needs of kitlib.hubsite.backlog.Views."""
    days = 30
    timer_error = None

    def __init__(self, calls, answer=None):
        self.calls = calls
        self.answer = ANSWER if answer is None else answer
        self.reads = []

    def clock(self):
        return NOW

    def get(self, fresh=False):
        self.reads.append(fresh)
        return copy.deepcopy(self.answer)

    def start(self):
        self.calls.append('backlog.start')
        return True

    def stop(self, timeout=None):
        self.calls.append('backlog.stop')
        return True


class FakeSync(object):
    """What serve needs of kitlib.hubsite.gitsync.GitSync. sync_once notes whether the store was
    still locked and whether every door was closed when the last pass ran, and the log lines
    there were when it began. `fail`: 'stop' (stop raises), 'slow' (stop answers that its thread
    did not end), 'pass' (sync_once raises). `state`: a test's changes to status(). `timeouts`:
    what each stop() was given; `stop_noted`: the log lines there were at the first stop();
    `on`: the name of the thread each stop() and sync_once() ran on."""

    def __init__(self, case, fail=None):
        self.case = case
        self.fail = fail
        self.last = None
        self.noted = None
        self.stop_noted = None
        self.timeouts = []
        self.on = []
        self.state = {}

    def start(self):
        self.case.calls.append('sync.start')
        return True

    def stop(self, timeout=None):
        self.case.calls.append('sync.stop')
        self.timeouts.append(timeout)
        self.on.append(threading.current_thread().name)
        if self.stop_noted is None:
            self.stop_noted = list(self.case.lines)
        if self.fail == 'stop':
            raise RuntimeError(SECRET)
        return self.fail != 'slow'

    def sync_once(self):
        self.case.calls.append('sync.sync_once')
        self.on.append(threading.current_thread().name)
        self.noted = list(self.case.lines)
        if self.fail == 'pass':
            raise RuntimeError(SECRET)
        self.last = {'locked': self.case.locked(),
                     'doors': [lis.closing for lis in self.case.hub.listeners],
                     'writes': self.case.hub.store.revision}
        return self.status()

    def status(self):
        out = {'running': True, 'branch': 'data', 'remote': 'origin', 'failed': '',
               'detail': SECRET, 'pending_commit': False, 'pending_push': True,
               'diverged': False}
        out.update(self.state)
        return out


class Stuck(FakeSync):
    """A sync whose git hangs at the close: its first call of `at` ('stop' or 'sync_once') sends
    the main thread a Ctrl+C, as the owner's second one, and does not come back before the event
    `release` is set."""

    def __init__(self, case, at, release):
        FakeSync.__init__(self, case)
        self.at = at
        self.release = release
        self.hung = False

    def hang(self, name):
        if name == self.at and not self.hung:
            self.hung = True
            _thread.interrupt_main()
            self.release.wait(TIMEOUT)

    def stop(self, timeout=None):
        out = FakeSync.stop(self, timeout)
        self.hang('stop')
        return out

    def sync_once(self):
        self.case.calls.append('sync.sync_once')
        self.on.append(threading.current_thread().name)
        self.hang('sync_once')
        return self.status()


class Late(FakeSync):
    """A sync with a pass in flight at the close: stop() with a time limit answers that its
    thread did not end. With none it waits for the pass: `waited` is what was true then (the
    store still locked, the log lines so far), and `ends` what it answers (an exception: raised)."""

    def __init__(self, case, ends=True):
        FakeSync.__init__(self, case)
        self.ends = ends
        self.waited = None

    def stop(self, timeout=None):
        FakeSync.stop(self, timeout)
        if timeout is not None:
            return False
        self.waited = {'locked': self.case.locked(), 'lines': list(self.case.lines)}
        if isinstance(self.ends, Exception):
            raise self.ends
        return self.ends


def git_waits():
    """The threads alive now on which a close waits for git."""
    return [t for t in threading.enumerate() if t.name == serve.GIT_WAIT_THREAD]


class ServeCase(unittest.TestCase):
    def setUp(self):
        self.addCleanup(lambda: self.assertEqual(httpfix.http_threads(), [],
                                                 'a listener thread outlived its test'))
        self.root = tempfile.mkdtemp(prefix='kit-hubserve-')
        self.addCleanup(shutil.rmtree, self.root, True)
        self.tokens = os.path.join(self.root, 'tokens')
        self.store_dir = os.path.join(self.root, 'store')
        self.settings = hub_settings.settings(None)._replace(port=0, store=self.store_dir)
        self.calls = []
        self.lines = []
        self.made = []          # the arguments the sync factory was called with
        self.hub = None

    def log(self, level, text):
        self.lines.append((level, text))

    def errors(self):
        return [text for level, text in self.lines if level == 'error']

    def sync_factory(self, fail=None):
        def make(store_dir, every, remote=None, branch=None, push=True):
            self.made.append((store_dir, every, remote, branch, push))
            if fail == 'refused':
                raise gitsync.Refused('%s is not in a git work tree' % SECRET)
            self.sync = FakeSync(self, fail)
            return self.sync
        return make

    def given(self, sync):
        """A sync source that gives the stand-in `sync`."""
        def make(store_dir, every, remote=None, branch=None, push=True):
            self.sync = sync
            return sync
        return make

    def build(self, settings=None, **src):
        src.setdefault('tokens', self.tokens)
        src.setdefault('static_dir', os.path.join(self.root, 'web'))
        hub = serve.build(settings or self.settings, serve.Sources(**src), log=self.log)
        self.addCleanup(hub.close)
        self.hub = hub
        self.owner = auth.read_token(self.tokens, auth.OWNER)
        self.agent = auth.read_token(self.tokens, auth.AGENT)
        return hub

    def locked(self, root=None):
        """True when the store folder is locked by a server."""
        try:
            Store.open(root or self.store_dir).close()
        except StoreBusy:
            return True
        return False


class BuildCase(ServeCase):
    def test_health_answers_on_the_bound_port_and_a_foreign_host_is_refused(self):
        hub = self.build()
        self.assertIsInstance(hub.listeners, list)
        self.assertEqual(len(hub.listeners), 1)
        self.assertNotEqual(hub.port, 0)
        self.assertEqual(hub.url, 'http://127.0.0.1:%d' % hub.port)
        self.assertEqual(hub.listeners[0].max_waits, 32)
        self.assertEqual(hub.creds.port, hub.port)
        self.assertIs(hub.start(), True)
        r = get(hub.port, HEALTH, self.owner)
        self.assertEqual(r.status, 200)
        self.assertIs(r.json()['ok'], True)
        self.assertEqual(r.json()['store'], os.path.abspath(self.store_dir))
        self.assertNotIn('store', get(hub.port, HEALTH, self.agent).json())
        for host in ('evil.example:%d' % hub.port, 'evil.example', '127.0.0.1:1'):
            r = send(hub.port, 'GET', HEALTH, [bearer(self.owner)], host=host)
            self.assertEqual((r.status, r.code()), (403, 'forbidden'), host)
        r = send(hub.port, 'GET', HEALTH, [bearer(self.owner)], host='localhost:%d' % hub.port)
        self.assertEqual(r.status, 200)
        # no page yet: the root is a clean 404, never a 500
        self.assertEqual(get(hub.port, '/').status, 404)
        self.assertEqual(self.errors(), [])

    def test_nothing_is_served_before_start_and_start_works_once(self):
        hub = self.build()
        self.assertEqual(httpfix.http_threads(), [])
        self.assertIs(hub.start(), True)
        self.assertIs(hub.start(), False)
        self.assertEqual(httpfix.http_threads().count('hub-http-%d' % hub.port), 1)
        self.assertIs(hub.close(), True)
        self.assertIs(hub.start(), False)
        self.assertEqual(httpfix.http_threads(), [])

    def test_a_write_and_a_read_go_through_the_built_server(self):
        hub = self.build()
        hub.start()
        r = post(hub.port, BASE + '/todos', {'title': 'a thing'}, self.owner, via='web')
        self.assertEqual(r.status, 200, r.body)
        r = get(hub.port, BASE + '/todos', self.agent)
        self.assertEqual(r.status, 200)
        self.assertIn('a thing', r.body.decode('utf-8'))
        self.assertEqual(self.errors(), [])

    def test_the_pairing_route_is_the_owners(self):
        hub = self.build()
        hub.start()
        self.assertEqual(post(hub.port, httpd.PAIR_ROUTE, {}, self.agent).status, 403)
        self.assertEqual(post(hub.port, httpd.PAIR_ROUTE, {}).status, 403)
        r = post(hub.port, httpd.PAIR_ROUTE, {}, self.owner)
        self.assertEqual(r.status, 200, r.body)
        r = get(hub.port, r.json()['path'])
        self.assertEqual(r.status, 303)
        cookie = r.header('Set-Cookie')[0]
        self.assertTrue(cookie.startswith(auth.cookie_name(hub.port) + '='), cookie)
        # the cookie is the owner's session on this listener
        r = get(hub.port, HEALTH, headers=[('Cookie', cookie.split(';')[0])])
        self.assertEqual(r.status, 200)
        self.assertIn('store', r.json())

    def test_a_second_server_on_the_same_store_is_refused_at_once(self):
        first = self.build()
        self.assertTrue(self.locked())
        with self.assertRaises(serve.ServeError) as cm:
            serve.build(self.settings, serve.Sources(tokens=self.tokens), log=self.log)
        self.assertIn('another hub server has this store open', str(cm.exception))
        self.assertIn(os.path.abspath(self.store_dir), str(cm.exception))
        # the refusal took nothing from the first: it still serves, and its close frees the store
        first.start()
        self.assertEqual(get(first.port, HEALTH, self.owner).status, 200)
        self.assertIs(first.close(), True)
        self.assertFalse(self.locked())
        again = self.build()
        self.assertIs(again.close(), True)

    def test_no_store_folder_is_refused(self):
        with self.assertRaises(serve.ServeError) as cm:
            serve.build(self.settings._replace(store=None), serve.Sources(tokens=self.tokens))
        self.assertIn('no store folder', str(cm.exception))
        with self.assertRaises(serve.ServeError):
            serve.build(self.settings, serve.Sources())
        self.assertFalse(os.path.exists(self.store_dir))

    def test_a_token_file_that_cannot_serve_is_refused_and_the_store_is_released(self):
        os.makedirs(self.tokens)
        bad = os.path.join(self.tokens, auth.TOKEN_FILES[auth.OWNER])
        with open(bad, 'w', encoding='utf-8'):
            pass
        with self.assertRaises(serve.ServeError) as cm:
            serve.build(self.settings, serve.Sources(tokens=self.tokens), log=self.log)
        self.assertIn(bad, str(cm.exception))
        self.assertFalse(self.locked())
        self.assertEqual(httpfix.http_threads(), [])
        self.assertEqual(os.path.getsize(bad), 0, 'the bad token file was replaced')

    def test_a_taken_port_is_refused_and_the_store_is_released(self):
        first = self.build()
        other = os.path.join(self.root, 'other')
        with self.assertRaises(serve.ServeError) as cm:
            serve.build(self.settings._replace(port=first.port, store=other),
                        serve.Sources(tokens=self.tokens), log=self.log)
        self.assertIn(str(first.port), str(cm.exception))
        self.assertFalse(self.locked(other))
        self.assertTrue(self.locked())

    def test_a_lock_left_by_a_dead_server_is_taken_over_with_a_note(self):
        hub = self.build()
        self.assertEqual(hub.notes, [])
        hub.close()

        class Opener(object):       # a real take-over needs a process that died holding the lock
            @staticmethod
            def open(root):
                store = Store.open(root)
                store.took_over = {'pid': 1}
                return store

        with mock.patch.object(serve, 'Store', Opener):
            hub = self.build()
        self.assertEqual(len(hub.notes), 1)
        self.assertIn('taken over', hub.notes[0])


class SyncCase(ServeCase):
    def test_the_sync_starts_with_the_server_and_makes_one_last_pass_before_the_store_closes(self):
        hub = self.build(sync=self.sync_factory(), backlog=lambda s: FakeBacklog(self.calls))
        self.assertEqual(self.made, [(self.store_dir, hub_settings.COMMIT_EVERY_S, None, None,
                                      self.settings.push)])
        self.assertIs(hub.sync, self.sync)
        self.assertEqual(self.calls, [])
        hub.start()
        self.assertEqual(self.calls, ['sync.start', 'backlog.start'])
        self.assertEqual(post(hub.port, BASE + '/todos', {'title': 't'}, self.owner).status, 200)
        written = hub.store.revision
        del self.calls[:]
        self.assertIs(hub.close(), True)
        self.assertEqual(self.calls, ['backlog.stop', 'sync.stop', 'sync.sync_once'])
        # the last pass ran with the door closed and the store still this server's, whole
        self.assertEqual(self.sync.last, {'locked': True, 'doors': [True], 'writes': written})
        self.assertFalse(self.locked())
        # a second close does nothing more
        self.assertIs(hub.close(), True)
        self.assertEqual(self.calls, ['backlog.stop', 'sync.stop', 'sync.sync_once'])
        self.assertEqual(self.errors(), [])

    def test_a_server_that_never_started_makes_no_last_pass(self):
        hub = self.build(sync=self.sync_factory())
        self.assertIs(hub.close(), True)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.locked())

    def test_a_refused_sync_is_noted_and_the_server_still_serves(self):
        hub = self.build(sync=self.sync_factory('refused'))
        self.assertIsNone(hub.sync)
        self.assertEqual(len(hub.notes), 1)
        self.assertIn('git sync is off', hub.notes[0])
        self.assertIn(SECRET, hub.notes[0])             # the console may see the folder
        hub.start()
        r = get(hub.port, HEALTH, self.owner)
        self.assertEqual(r.status, 200)
        self.assertEqual(r.json()['git'], {'running': False, 'off': serve.SYNC_REFUSED})
        self.assertNotIn('somewhere', r.body.decode('utf-8'))      # health may not
        self.assertEqual(post(hub.port, BASE + '/todos', {'title': 't'}, self.owner).status, 200)
        self.assertIs(hub.close(), True)
        self.assertEqual(self.calls, [])

    def test_no_sync_source_is_a_server_with_sync_off(self):
        hub = self.build()
        self.assertEqual((hub.sync, hub.notes), (None, []))
        self.assertEqual(hub.views.health()['git'], {'running': False, 'off': serve.SYNC_OFF})

    def test_health_shows_the_syncs_status_without_gits_own_words(self):
        hub = self.build(sync=self.sync_factory())
        hub.start()
        r = get(hub.port, HEALTH, self.owner)
        git = r.json()['git']
        self.assertEqual((git['running'], git['branch'], git['pending_push']),
                         (True, 'data', True))
        self.assertNotIn('detail', git)
        self.assertNotIn('somewhere', r.body.decode('utf-8'))

    def test_the_remote_and_branch_are_pm_datas_only_for_a_store_inside_it(self):
        data = os.path.join(self.root, 'data')
        pm = {'path': data, 'branch': 'pm-data', 'remote': 'origin'}
        inside = self.settings._replace(store=os.path.join(data, 'hub'), push=True)
        self.build(inside, sync=self.sync_factory(), pm_data=pm).close()
        self.build(self.settings, sync=self.sync_factory(), pm_data=pm).close()
        self.assertEqual(self.made, [
            (inside.store, hub_settings.COMMIT_EVERY_S, 'origin', 'pm-data', True),
            (self.store_dir, hub_settings.COMMIT_EVERY_S, None, None, False)])

    def test_a_close_step_that_fails_is_logged_and_the_store_is_still_released(self):
        hub = self.build(sync=self.sync_factory('stop'))
        hub.start()
        self.assertIs(hub.close(), False)
        self.assertEqual(self.calls, ['sync.start', 'sync.stop', 'sync.sync_once'])
        self.assertFalse(self.locked())
        self.assertEqual(len(self.errors()), 1)
        self.assertIn('the git sync failed: RuntimeError', self.errors()[0])
        self.assertIs(hub.close(), False)
        self.assertEqual(httpfix.http_threads(), [])


class CloseCase(ServeCase):
    def test_a_close_ends_a_long_wait_and_leaves_no_thread(self):
        hub = self.build()
        hub.start()
        lis = hub.listeners[0]
        got = []
        path = '%s/wait?since=%d&timeout=%d' % (BASE, hub.store.revision, TIMEOUT)
        client = threading.Thread(target=lambda: got.append(get(hub.port, path, self.agent)),
                                  name='test-waiter', daemon=True)
        client.start()
        self.addCleanup(client.join, TIMEOUT)
        self.assertTrue(lis.await_waits(1, TIMEOUT), 'the wait never arrived')
        self.assertIs(hub.close(TIMEOUT), True)
        client.join(TIMEOUT)
        self.assertEqual(len(got), 1, 'the wait was never answered')
        self.assertIn(got[0].status, (200, 503))
        self.assertEqual(httpfix.http_threads(), [])
        self.assertFalse(self.locked())
        self.assertEqual(self.errors(), [])

    def test_run_serves_until_stop_and_closes_cleanly(self):
        hub = self.build(sync=self.sync_factory(), backlog=lambda s: FakeBacklog(self.calls))
        stop, up, out = threading.Event(), threading.Event(), []
        seen = []

        def started(h):
            seen.append(h)
            up.set()

        runner = threading.Thread(target=lambda: out.append(serve.run(hub, started, stop)),
                                  name='test-run', daemon=True)
        runner.start()
        self.addCleanup(runner.join, TIMEOUT)
        self.addCleanup(stop.set)
        self.assertTrue(up.wait(TIMEOUT), 'the server never started')
        self.assertEqual(seen, [hub])
        self.assertEqual(get(hub.port, HEALTH, self.owner).status, 200)
        self.assertEqual(out, [])
        stop.set()
        runner.join(TIMEOUT)
        self.assertEqual(out, [True])
        self.assertEqual(self.calls, ['sync.start', 'backlog.start', 'backlog.stop', 'sync.stop',
                                      'sync.sync_once'])
        self.assertEqual(httpfix.http_threads(), [])
        self.assertFalse(self.locked())
        with self.assertRaises(serve.NotRunning):
            serve.ask(hub.port, 'GET', HEALTH, self.owner)

    def test_ctrl_c_is_a_clean_close(self):
        hub = self.build(sync=self.sync_factory())

        def started(h):
            self.assertEqual(get(h.port, HEALTH, self.owner).status, 200)
            raise KeyboardInterrupt()

        self.assertIs(serve.run(hub, started), True)
        self.assertEqual(self.calls, ['sync.start', 'sync.stop', 'sync.sync_once'])
        self.assertFalse(self.locked())
        self.assertEqual(httpfix.http_threads(), [])

    def test_run_closes_the_server_when_starting_goes_wrong(self):
        hub = self.build()

        def started(h):
            raise ValueError('no')

        with self.assertRaises(ValueError):
            serve.run(hub, started)
        self.assertFalse(self.locked())
        self.assertEqual(httpfix.http_threads(), [])
        with self.assertRaises(serve.ServeError):
            serve.run(hub)


class ViewsCase(ServeCase):
    def views(self, **src):
        self.fake = FakeBacklog(self.calls, src.pop('answer', None))
        src.setdefault('backlog', lambda refresh_s: self.fake)
        hub = self.build(**src)
        hub.start()
        return hub

    def test_the_backlog_view_is_one_entry_per_repository(self):
        hub = self.views()
        self.assertIs(hub.backlog, self.fake)
        r = get(hub.port, BASE + '/backlog', self.agent)
        self.assertEqual(r.status, 200, r.body)
        repos = r.json()['repos']
        self.assertEqual([e['repo'] for e in repos], ['Alpha', 'beta'])
        self.assertEqual(repos[0], {
            'repo': 'Alpha', 'tip': None, 'fetched_at': '2026-01-10T11:00:00+00:00',
            'items': ANSWER['repos'][0]['open'], 'error': None, 'stale': False,
            'fetch_error': None})
        self.assertEqual((repos[1]['error'], repos[1]['stale'], repos[1]['items']),
                         ('git timed out', True, []))
        r = get(hub.port, BASE + '/backlog?repo=ALPHA&state=parked', self.agent)
        self.assertEqual([[i['id'] for i in e['items']] for e in r.json()['repos']], [['A-2']])
        self.assertEqual(get(hub.port, BASE + '/backlog?repo=nope', self.agent).json()['repos'], [])
        self.assertEqual(self.fake.reads, [False, False, False])
        get(hub.port, BASE + '/backlog?fresh=1', self.agent)
        self.assertEqual(self.fake.reads[-1], True)

    def test_the_completed_view_narrows_the_window_and_never_widens_it(self):
        hub = self.views()

        def ids(query):
            r = get(hub.port, BASE + '/completed' + query, self.agent)
            self.assertEqual(r.status, 200, r.body)
            e = r.json()['repos'][0]
            return [i['id'] for i in e['items']], e['days']

        self.assertEqual(ids(''), (['A-0', 'A-9'], 30))
        self.assertEqual(ids('?days=5'), (['A-9'], 5))
        self.assertEqual(ids('?days=1'), (['A-9'], 1))
        self.assertEqual(ids('?days=90&repo=alpha'), (['A-0', 'A-9'], 30))
        self.assertEqual(backlog.window(NOW, 5), '2026-01-05')

    def test_health_carries_the_last_backlog_read_and_starts_none(self):
        hub = self.views()
        h = get(hub.port, HEALTH, self.agent).json()
        self.assertEqual((h['backlog'], h['backlog_at'], h['backlog_error'], h['backlog_timer']),
                         ([], None, None, None))
        self.assertEqual(self.fake.reads, [])
        get(hub.port, BASE + '/backlog', self.agent)
        h = get(hub.port, HEALTH, self.agent).json()
        self.assertEqual(h['backlog'], [
            {'repo': 'Alpha', 'fetched_at': '2026-01-10T11:00:00+00:00', 'error': None},
            {'repo': 'beta', 'fetched_at': None, 'error': 'git timed out'}])
        self.assertEqual(h['backlog_at'], ANSWER['at'])
        self.assertEqual(self.fake.reads, [False])
        self.assertEqual(h['unavailable'], {})

    def test_a_timer_that_is_stopping_reads_nothing_more_for_health(self):
        waits = []

        class Timed(FakeBacklog):       # with the pause of a timer, as backlog.Views has one
            def wait(self, stop, seconds):
                waits.append((stop.is_set(), seconds))
                return stop.is_set()

        fake = Timed(self.calls)
        hub = self.build(backlog=lambda refresh_s: fake)
        stop = threading.Event()
        # the pause after a round reads the view, so health has what the round read
        self.assertIs(hub.backlog.wait(stop, 5), False)
        self.assertEqual(fake.reads, [False])
        self.assertEqual(hub.views.health()['backlog_at'], ANSWER['at'])
        # not when the timer is stopping: the close waits for this thread
        stop.set()
        self.assertIs(hub.backlog.wait(stop, 5), True)
        self.assertEqual(fake.reads, [False])
        self.assertEqual(waits, [(False, 5), (True, 5)])

    def test_a_repository_list_that_cannot_be_read_is_unavailable_not_a_500(self):
        phrase = backlog.NO_REPOS + ': KeyError'
        hub = self.views(answer={'at': ANSWER['at'], 'error': phrase, 'repos': []})
        for path in ('/backlog', '/completed'):
            r = get(hub.port, BASE + path, self.owner)
            self.assertEqual((r.status, r.code()), (503, 'unavailable'), path)
            self.assertEqual(r.json()['error']['field'], 'views')
            self.assertIn('NoRepoList', r.json()['error']['message'])
        r = get(hub.port, BASE + '/snapshot', self.owner)
        self.assertEqual(r.status, 200)
        self.assertEqual(r.json()['unavailable']['backlog'], 'NoRepoList')
        self.assertEqual(get(hub.port, HEALTH, self.owner).json()['backlog_error'], phrase)
        self.assertEqual(self.errors(), [])

    def test_a_view_with_no_source_is_unavailable_and_the_rest_answer(self):
        hub = self.build()
        hub.start()
        for name in ('backlog', 'completed', 'repos', 'agents'):
            r = get(hub.port, '%s/%s' % (BASE, name), self.owner)
            self.assertEqual((r.status, r.code()), (503, 'unavailable'), name)
            self.assertEqual(r.json()['error']['field'], 'views')
        self.assertEqual(get(hub.port, BASE + '/notices', self.owner).json()['items'], [])
        usage = get(hub.port, BASE + '/usage', self.owner).json()['items']
        self.assertEqual((usage['available'], usage['reason']), (False, serve.NO_USAGE_SOURCE))
        r = get(hub.port, BASE + '/snapshot', self.owner)
        self.assertEqual(r.status, 200)
        self.assertEqual(sorted(r.json()['unavailable']), ['agents', 'backlog', 'completed',
                                                           'repos'])
        h = get(hub.port, HEALTH, self.owner).json()
        self.assertEqual((h['backlog'], h['unavailable']), (None, {}))
        self.assertEqual(self.errors(), [])

    def test_the_derived_views_say_no_path_and_no_exception_text(self):
        def usage():
            raise OSError(SECRET)

        seen = []

        def repos(stored):
            seen.append(stored)
            return [{'name': 'alpha', 'aliases': []}]

        missing = os.path.join(self.root, 'no-agents-here')
        hub = self.build(usage=usage, repos=repos, agents=missing,
                         notices=os.path.join(self.root, 'no-notices-here'))
        hub.start()
        r = get(hub.port, BASE + '/usage', self.owner)
        self.assertEqual((r.json()['items']['available'], r.json()['items']['reason']),
                         (False, serve.USAGE_FAILED))
        self.assertNotIn('somewhere', r.body.decode('utf-8'))
        r = get(hub.port, BASE + '/agents', self.owner)
        self.assertEqual((r.json()['items']['available'], r.json()['items']['reason']),
                         (False, serve.AGENTS_FAILED))
        self.assertNotIn('no-agents-here', r.body.decode('utf-8'))
        r = get(hub.port, BASE + '/repos', self.owner)
        self.assertEqual(r.json()['items'], [{'name': 'alpha', 'aliases': []}])
        self.assertEqual(seen, [[]])
        self.assertEqual(get(hub.port, BASE + '/notices', self.owner).json()['items'], [])


class ReasonCase(ServeCase):
    """Why a git sync pass failed: the owner's and the server log's, never an agent's, and
    nowhere with the user or password of a remote URL."""

    def failing(self, **state):
        hub = self.build(sync=self.sync_factory())
        self.sync.state = dict({'failed': 'push', 'detail': WHY}, **state)
        return hub

    def clean(self, text):
        for word in CREDENTIALS:
            self.assertNotIn(word, text)

    def test_scrub_takes_the_user_and_password_out_of_a_url_and_nothing_else(self):
        host = 'https://git.example.test/team/data.git'
        for text, want in (
                (REMOTE, host),
                (WHY, "fatal: unable to access '%s/': Could not resolve host" % host),
                ('https://tok3n@git.example.test/x', 'https://git.example.test/x'),
                ('https://u:p@ss@git.example.test/x', 'https://git.example.test/x'),
                ('ssh://git@git.example.test:22/team/data.git',
                 'ssh://git.example.test:22/team/data.git'),
                ('to http://a:b@h/x and https://c@i/y', 'to http://h/x and https://i/y'),
                # what is no user part of a URL stays
                ('git@git.example.test:team/data.git', 'git@git.example.test:team/data.git'),
                (host + '/a@b', host + '/a@b'),
                (host + '?to=a@b', host + '?to=a@b'),
                ('someone@example.test wrote it', 'someone@example.test wrote it'),
                (SECRET, SECRET),
                ('', '')):
            self.assertEqual(serve.scrub(text), want, text)

    def test_scrub_takes_out_a_password_that_holds_a_quote(self):
        host = 'https://git.example.test/x'
        for text, want in (
                ("https://someone:s3'cret@git.example.test/x", host),
                ("https://some'one@git.example.test/x", host),
                ("fatal: unable to access 'https://someone:s3'cret@git.example.test/x/': refused",
                 "fatal: unable to access '%s/': refused" % host),
                # a quote that ends a URL is no part of a password
                ("'%s' was refused for a@b" % host, "'%s' was refused for a@b" % host),
                ("'https://git.example.test' was refused for a@b",
                 "'https://git.example.test' was refused for a@b"),
                ('"https://git.example.test"@b', '"https://git.example.test"@b'),
                ('<https://git.example.test>@b', '<https://git.example.test>@b'),
                # the scp form of a remote has no password to take out
                ('git@git.example.test:team/data.git', 'git@git.example.test:team/data.git'),
                ("'git@git.example.test:team/data.git'", "'git@git.example.test:team/data.git'")):
            self.assertEqual(serve.scrub(text), want, text)

    def test_health_gives_no_reason_when_the_failing_step_changed_between_its_two_reads(self):
        hub = self.failing()
        hub.start()
        answers = [{'failed': 'push', 'detail': WHY},
                   {'failed': 'fetch', 'detail': 'the reason of another step'}]
        asked = []

        def status():       # the Api's health reads it first, the route's reason second
            asked.append(len(asked))
            return dict(FakeSync.status(self.sync), **answers[min(len(asked), 2) - 1])

        self.sync.status = status
        r = get(hub.port, HEALTH, self.owner)
        self.assertEqual(r.status, 200, r.body)
        self.assertEqual(asked, [0, 1], 'health did not read the status twice')
        git = r.json()['git']
        # the step the answer names is not the one the reason is of: no reason is better than
        # the wrong one
        self.assertEqual(git['failed'], 'push')
        self.assertNotIn('detail', git)
        self.assertNotIn('another step', r.body.decode('utf-8'))

    def test_health_gives_the_reason_to_the_owner_alone_and_without_credentials(self):
        hub = self.failing()
        hub.start()
        r = get(hub.port, HEALTH, self.owner)
        self.assertEqual(r.status, 200, r.body)
        git = r.json()['git']
        self.assertEqual((git['failed'], git['detail']), ('push', serve.scrub(WHY)))
        self.assertIn('git.example.test/team/data.git', git['detail'])
        self.clean(r.body.decode('utf-8'))
        # the agent learns that it failed and at which step, never why
        r = get(hub.port, HEALTH, self.agent)
        self.assertEqual(r.status, 200, r.body)
        self.assertEqual(r.json()['git']['failed'], 'push')
        self.assertNotIn('detail', r.json()['git'])
        for word in CREDENTIALS + ('example.test', 'resolve'):
            self.assertNotIn(word, r.body.decode('utf-8'))
        # a caller with no role learns nothing
        for token in (None, 'x' * 43):
            r = get(hub.port, HEALTH, token)
            self.assertEqual((r.status, r.code()), (403, 'forbidden'), token)
            for word in CREDENTIALS + ('example.test', 'push'):
                self.assertNotIn(word, r.body.decode('utf-8'))
        # the view itself holds no reason: only the listener's route adds it, by role
        self.assertNotIn('detail', hub.views.health()['git'])
        self.assertEqual(hub.views.git_failure(), ('push', serve.scrub(WHY)))

    def test_the_owners_session_gets_the_reason_and_a_failure_with_none_has_no_detail(self):
        hub = self.failing()
        hub.start()
        r = get(hub.port, post(hub.port, httpd.PAIR_ROUTE, {}, self.owner).json()['path'])
        cookie = r.header('Set-Cookie')[0].split(';')[0]
        git = get(hub.port, HEALTH, headers=[('Cookie', cookie)]).json()['git']
        self.assertEqual(git['detail'], serve.scrub(WHY))
        self.sync.state = {'failed': 'fetch', 'detail': ''}
        git = get(hub.port, HEALTH, self.owner).json()['git']
        self.assertEqual(git['failed'], 'fetch')
        self.assertNotIn('detail', git)

    def test_watch_logs_a_failure_once_then_a_new_reason_then_that_it_works_again(self):
        hub = self.failing()
        hub.watch()
        hub.watch()
        self.assertEqual(self.lines, [('error', 'the git sync failed at push: '
                                       + serve.scrub(WHY))])
        self.sync.state = {'failed': 'fetch', 'detail': ''}
        hub.watch()
        hub.watch()
        self.assertEqual(self.lines[1:], [('error', 'the git sync failed at fetch: -')])
        self.sync.state = {}
        hub.watch()
        hub.watch()
        self.assertEqual(self.lines[2:], [('note', 'the git sync works again')])
        for level, text in self.lines:
            self.clean(text)

    def test_watch_says_nothing_of_a_sync_that_works_or_is_off_or_cannot_be_asked(self):
        hub = self.build(sync=self.sync_factory())
        hub.watch()
        self.sync.status = lambda: {}['no status']
        hub.watch()
        hub.close()
        self.build().watch()
        self.assertEqual(self.lines, [])

    def test_run_logs_a_failing_sync_while_it_serves_and_its_last_pass(self):
        seen = threading.Event()

        def log(level, text):
            self.lines.append((level, text))
            if level == 'error':
                seen.set()

        self.log = log
        hub = self.failing()
        stop, out = threading.Event(), []
        with mock.patch.object(serve, 'POLL_S', 0.01):
            runner = threading.Thread(target=lambda: out.append(serve.run(hub, None, stop)),
                                      name='test-run', daemon=True)
            runner.start()
            try:
                self.assertTrue(seen.wait(TIMEOUT), 'the failing sync was never logged')
            finally:
                stop.set()
                runner.join(TIMEOUT)
        # a last pass that failed is logged and is no unclean close: the next start tries again
        self.assertEqual(out, [True])
        why = serve.scrub(WHY)
        self.assertEqual(self.errors(), [
            'the git sync failed at push: ' + why,
            'the last git sync pass failed at push: %s; the next start tries again' % why])
        self.assertIn(('note', serve.LAST_PASS), self.lines)
        for level, text in self.lines:
            self.clean(text)

    def test_hub_status_prints_the_reason_for_the_owner_only(self):
        hub = self.failing()
        hub.start()
        start = 'git: running, branch data, pending: a push, the last pass failed at push'
        status, obj = serve.ask(hub.port, 'GET', HEALTH, self.owner)
        self.assertEqual(serve.status_lines(obj, NOW)[2], '%s: %s' % (start, serve.scrub(WHY)))
        status, obj = serve.ask(hub.port, 'GET', HEALTH, self.agent)
        self.assertEqual(serve.status_lines(obj, NOW)[1], start)
        # the client scrubs what it prints too, whatever answered it
        lines = serve.status_lines({'revision': 1, 'backlog': None, 'git': {
            'running': True, 'failed': 'fetch', 'detail': WHY}}, NOW)
        self.assertEqual(lines[1], 'git: running, nothing pending, the last pass failed at '
                                   'fetch: ' + serve.scrub(WHY))

    def test_a_refused_syncs_note_holds_no_credentials(self):
        def make(store_dir, every, remote=None, branch=None, push=True):
            raise gitsync.Refused('the remote %s cannot be used' % REMOTE)

        hub = self.build(sync=make)
        self.assertEqual(hub.notes, ['git sync is off: the remote '
                                     'https://git.example.test/team/data.git cannot be used'])


class StepCase(ServeCase):
    """The steps of the clean close that fail or run out of time: each is a line in the log
    that names it, the later steps still run, and the close answers False."""

    def test_a_request_still_in_flight_is_named_and_the_last_pass_is_skipped(self):
        entered, release = threading.Event(), threading.Event()

        def usage():
            entered.set()
            release.wait(TIMEOUT)
            return []

        hub = self.build(sync=self.sync_factory(), usage=usage)
        hub.start()
        lis = hub.listeners[0]
        got = []
        client = threading.Thread(
            target=lambda: got.append(get(hub.port, BASE + '/usage', self.owner)),
            name='test-slow-read', daemon=True)
        client.start()
        self.addCleanup(client.join, TIMEOUT)
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(TIMEOUT), 'the request never arrived')
        self.assertIs(hub.close(0.05), False)
        self.assertEqual(self.errors(), [
            'closing: the requests in flight did not end within 0.05 s',
            serve.LAST_PASS_SKIPPED,
            'closing: the handler threads did not end within 0.05 s'])
        # the sync's thread was stopped; no pass ran beside a request that may still write
        self.assertEqual(self.calls, ['sync.start', 'sync.stop'])
        self.assertNotIn(('note', serve.LAST_PASS), self.lines)
        self.assertFalse(self.locked(), 'the store was not released')
        self.assertIs(hub.close(), False)
        release.set()
        self.assertTrue(lis.join(TIMEOUT), 'the handler thread never ended')
        client.join(TIMEOUT)
        self.assertEqual(len(got), 1, 'the request was never answered')

    def test_each_step_that_does_not_end_in_its_time_is_named(self):
        class Slow(FakeBacklog):
            def stop(self, timeout=None):
                FakeBacklog.stop(self, timeout)
                return False

        for step, timeout, line in (
                ('backlog', 7, 'closing: the backlog timer did not end within 7 s'),
                ('backlog', None, 'closing: the backlog timer did not end within its time'),
                ('sync', 7, 'closing: the git sync did not end within 7 s'),
                ('join', 2.5, 'closing: the handler threads did not end within 2.5 s')):
            with self.subTest(step=step, timeout=timeout):
                del self.calls[:], self.lines[:]
                made = Slow if step == 'backlog' else FakeBacklog
                hub = self.build(sync=self.sync_factory('slow' if step == 'sync' else None),
                                 backlog=lambda s: made(self.calls))
                hub.start()
                lis = hub.listeners[0]
                if step == 'join':
                    join = lis.join
                    lis.join = lambda t=None: join(t) and False
                self.assertIs(hub.close(timeout), False)
                self.assertEqual(self.errors(), [line])
                # a step that ran out of time stops none after it: the requests had ended, so
                # the last pass ran, and the store was released
                self.assertEqual(self.calls, ['sync.start', 'backlog.start', 'backlog.stop',
                                              'sync.stop', 'sync.sync_once'])
                self.assertFalse(self.locked())
                self.assertIs(hub.close(timeout), False)
                self.assertEqual(httpfix.http_threads(), [])

    def test_a_door_that_raises_is_an_unclean_close_and_the_rest_still_runs(self):
        hub = self.build(sync=self.sync_factory())
        hub.start()
        lis = hub.listeners[0]
        door = lis.close_door

        def broken():
            door()
            raise OSError('the door')

        lis.close_door = broken
        self.assertIs(hub.close(TIMEOUT), False)
        self.assertEqual(self.errors(), ['closing: the listener door failed: OSError: the door'])
        self.assertEqual(self.calls, ['sync.start', 'sync.stop', 'sync.sync_once'])
        self.assertFalse(self.locked())
        self.assertEqual(httpfix.http_threads(), [])

    def test_the_last_pass_is_announced_before_it_begins_and_names_its_limit(self):
        hub = self.build(sync=self.sync_factory())
        hub.start()
        self.assertIs(hub.close(TIMEOUT), True)
        self.assertEqual(self.sync.noted, [('note', serve.LAST_PASS)])
        self.assertTrue(serve.LAST_PASS.startswith('closing: the last git sync pass'))
        self.assertIn('%d s' % gitsync.NET_TIMEOUT, serve.LAST_PASS)
        self.assertEqual(self.errors(), [])

    def test_a_last_pass_that_raises_is_an_unclean_close_and_the_store_is_released(self):
        hub = self.build(sync=self.sync_factory('pass'))
        hub.start()
        self.assertIs(hub.close(TIMEOUT), False)
        self.assertEqual(self.errors(), ['closing: the last git sync pass failed: RuntimeError: '
                                         + SECRET])
        self.assertFalse(self.locked())
        self.assertEqual(httpfix.http_threads(), [])


class WaitCase(ServeCase):
    """The waits for git at the close: each is said in the log before it begins, a second Ctrl+C
    ends it with a line that says what to check, and the store is not closed beside a pass in
    flight without a line that says so."""

    def ctrl_c(self):
        """Make a Ctrl+C a KeyboardInterrupt in this, the main thread, whatever the runner set
        for it; and after the test, end every wait for git it left on a thread."""
        if threading.current_thread() is not threading.main_thread():
            self.skipTest('a Ctrl+C reaches the main thread only')
        was = signal.signal(signal.SIGINT, signal.default_int_handler)
        if was is not None:
            self.addCleanup(signal.signal, signal.SIGINT, was)
        release = threading.Event()

        def ended():
            for t in git_waits():
                t.join(TIMEOUT)
            self.assertEqual(git_waits(), [], 'a wait for git outlived its test')

        self.addCleanup(ended)
        self.addCleanup(release.set)
        return release

    def cut(self, hub, timeout=TIMEOUT):
        """What hub.close() answers when a second Ctrl+C arrives during it. A KeyboardInterrupt
        that leaves the close would end the whole test run: it fails this test instead."""
        try:
            with mock.patch.object(serve, 'POLL_S', 0.01):
                return hub.close(timeout)
        except KeyboardInterrupt:
            self.fail('the second Ctrl+C left the close as a KeyboardInterrupt')

    def in_flight(self, sync):
        """A started hub over `sync` with one request that does not end before the test does."""
        entered, release = threading.Event(), threading.Event()

        def usage():
            entered.set()
            release.wait(TIMEOUT)
            return []

        hub = self.build(sync=self.given(sync), usage=usage)
        hub.start()
        lis = hub.listeners[0]
        client = threading.Thread(target=lambda: get(hub.port, BASE + '/usage', self.owner),
                                  name='test-slow-read', daemon=True)
        client.start()
        self.addCleanup(client.join, TIMEOUT)
        self.addCleanup(lis.join, TIMEOUT)
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(TIMEOUT), 'the request never arrived')
        return hub

    def test_the_wait_for_git_is_said_before_it_begins(self):
        hub = self.build(sync=self.sync_factory())
        hub.start()
        self.assertIs(hub.close(TIMEOUT), True)
        # the line is in the log before the first wait (the stop of the sync's thread), not
        # only before the last pass
        self.assertEqual(self.sync.stop_noted, [('note', serve.LAST_PASS)])
        self.assertIn('finishing a sync pass, waiting for git', serve.LAST_PASS)
        self.assertIn('Ctrl+C again ends the wait', serve.LAST_PASS)
        self.assertEqual(self.lines, [('note', serve.LAST_PASS)])
        # a wait for git is never made on the caller's thread: a Ctrl+C could not end it there
        self.assertEqual(self.sync.on, [serve.GIT_WAIT_THREAD] * 2)
        self.assertEqual(self.sync.timeouts, [TIMEOUT])
        self.assertEqual(git_waits(), [])

    def test_the_text_of_the_last_pass_says_a_pass_in_flight_ends_first(self):
        text = serve.LAST_PASS
        self.assertIn('A pass in flight ends first, then the last one runs', text)
        self.assertIn('in each git may take %d s for a fetch and again for a push'
                      % gitsync.NET_TIMEOUT, text)

    def test_a_second_ctrl_c_ends_the_wait_for_the_last_pass(self):
        release = self.ctrl_c()
        hub = self.build(sync=self.given(Stuck(self, 'sync_once', release)))
        hub.start()
        self.assertIs(self.cut(hub), False)
        self.assertEqual(self.lines, [('note', serve.LAST_PASS), ('error', serve.CUT_SHORT)])
        # the sync was told to begin no further pass, with no wait; the store was still closed
        self.assertEqual(self.calls, ['sync.start', 'sync.stop', 'sync.sync_once', 'sync.stop'])
        self.assertEqual(self.sync.timeouts, [TIMEOUT, 0])
        self.assertFalse(self.locked(), 'the store was not released')
        self.assertEqual(httpfix.http_threads(), [])
        self.assertIs(hub.close(), False)

    def test_a_second_ctrl_c_ends_the_wait_for_the_syncs_thread(self):
        release = self.ctrl_c()
        hub = self.build(sync=self.given(Stuck(self, 'stop', release)))
        hub.start()
        self.assertIs(self.cut(hub), False)
        self.assertEqual(self.lines, [('note', serve.LAST_PASS), ('error', serve.CUT_SHORT)])
        self.assertEqual(self.calls, ['sync.start', 'sync.stop', 'sync.stop'])
        self.assertEqual(self.sync.timeouts, [TIMEOUT, 0])
        self.assertFalse(self.locked(), 'the store was not released')
        self.assertEqual(httpfix.http_threads(), [])

    def test_the_line_of_a_cut_wait_says_what_may_be_left_and_what_to_check(self):
        for text in (serve.CUT_SHORT, serve.LEFT_RUNNING):
            self.assertIn('a git command may', text)
            self.assertIn('half-way', text)
            self.assertIn('index.lock', text)
            self.assertIn('the next start commits what is left', text)
        self.assertTrue(serve.CUT_SHORT.startswith('closing: interrupted'))

    def interrupted(self, **src):
        """A started hub whose step 3 (the requests in flight) is left by a Ctrl+C."""
        hub = self.build(**src)
        hub.start()
        lis = hub.listeners[0]
        drain = lis.drain

        def cut(timeout=None):
            drain(timeout)
            raise KeyboardInterrupt()

        lis.drain = cut
        return hub

    def test_a_ctrl_c_in_an_earlier_step_of_a_synced_close_is_answered_the_same_way(self):
        hub = self.interrupted(sync=self.sync_factory())
        self.assertIs(self.cut(hub), False)
        self.assertEqual(self.lines, [('error', serve.CUT_SHORT)])
        self.assertEqual(self.calls, ['sync.start', 'sync.stop'])
        self.assertEqual(self.sync.timeouts, [0])
        self.assertFalse(self.locked(), 'the store was not released')
        self.assertEqual(httpfix.http_threads(), [])

    def test_a_ctrl_c_in_a_close_with_no_sync_leaves_it_with_the_store_released(self):
        hub = self.interrupted()
        with self.assertRaises(KeyboardInterrupt):
            hub.close(TIMEOUT)
        self.assertEqual(self.lines, [])
        self.assertFalse(self.locked(), 'the store was not released')
        self.assertEqual(httpfix.http_threads(), [])
        self.assertIs(hub.close(), False)

    def test_a_pass_in_flight_is_waited_for_when_no_last_pass_will(self):
        hub = self.in_flight(Late(self))
        self.assertIs(hub.close(0.05), False)
        self.assertEqual(self.lines, [
            ('error', 'closing: the requests in flight did not end within 0.05 s'),
            ('error', serve.LAST_PASS_SKIPPED),
            ('note', serve.IN_FLIGHT),
            ('error', 'closing: the git sync did not end within 0.05 s'),
            ('note', serve.STILL_WAITING),
            ('error', 'closing: the handler threads did not end within 0.05 s')])
        # the second wait has no limit, was said first, and ended before the store closed
        self.assertEqual(self.calls, ['sync.start', 'sync.stop', 'sync.stop'])
        self.assertEqual(self.sync.timeouts, [0.05, None])
        self.assertEqual(self.sync.stop_noted[-1], ('note', serve.IN_FLIGHT))
        self.assertEqual(self.sync.waited, {'locked': True, 'lines': self.lines[:5]})
        self.assertEqual(self.sync.on, [serve.GIT_WAIT_THREAD] * 2)
        self.assertFalse(self.locked(), 'the store was not released')
        for text in (serve.IN_FLIGHT, serve.STILL_WAITING):
            self.assertIn('waiting for git', text)
            self.assertIn('Ctrl+C again ends the wait', text)

    def test_a_store_closed_beside_a_pass_that_may_still_run_is_said(self):
        for ends in (False, RuntimeError(SECRET)):
            with self.subTest(ends=ends):
                del self.calls[:], self.lines[:]
                self.store_dir = tempfile.mkdtemp(prefix='store-', dir=self.root)
                self.settings = self.settings._replace(store=self.store_dir)
                hub = self.in_flight(Late(self, ends))
                self.assertIs(hub.close(0.05), False)
                self.assertEqual(self.errors(), [
                    'closing: the requests in flight did not end within 0.05 s',
                    serve.LAST_PASS_SKIPPED,
                    'closing: the git sync did not end within 0.05 s',
                    serve.LEFT_RUNNING,
                    'closing: the handler threads did not end within 0.05 s'])
                self.assertEqual(self.sync.timeouts, [0.05, None])
                self.assertFalse(self.locked(), 'the store was not released')

    def test_a_second_ctrl_c_ends_the_wait_for_a_pass_in_flight(self):
        release = self.ctrl_c()

        class Held(Late):               # its pass in flight does not end: the wait with no limit
            def stop(self, timeout=None):
                Late.stop(self, timeout)
                if timeout is None:
                    _thread.interrupt_main()
                    release.wait(TIMEOUT)
                return timeout == 0

        hub = self.in_flight(Held(self))
        self.assertIs(self.cut(hub, 0.05), False)
        self.assertEqual(self.lines[3:], [
            ('error', 'closing: the git sync did not end within 0.05 s'),
            ('note', serve.STILL_WAITING),
            ('error', serve.CUT_SHORT),
            ('error', 'closing: the handler threads did not end within 0.05 s')])
        self.assertEqual(self.sync.timeouts, [0.05, None, 0])
        self.assertFalse(self.locked(), 'the store was not released')


class TimerCase(ServeCase):
    """The backlog timer over a real kitlib.hubsite.backlog.Views: its repository has no checkout
    here, so a round starts no git."""

    def timer(self, repos):
        """A started hub whose backlog timer has ended its first round and is in its pause."""
        paused = threading.Event()

        def wait(stop, seconds):
            paused.set()
            return stop.wait(TIMEOUT)

        def source(refresh_s):
            self.refresh_s = refresh_s
            return backlog.Views(repos, refresh_s, clock=lambda: NOW, wait=wait)

        hub = self.build(self.settings._replace(backlog_refresh_s=3600), backlog=source)
        hub.start()
        self.assertTrue(paused.wait(TIMEOUT), 'the timer never ended a round')
        return hub

    def threads(self):
        return [t.name for t in threading.enumerate() if t.name == 'hub-backlog-refresh']

    def test_a_timer_round_reaches_health_with_no_client_read(self):
        hub = self.timer([('alpha', None)])
        self.assertEqual(self.refresh_s, 3600)
        self.assertIsInstance(hub.backlog, backlog.Views)
        h = get(hub.port, HEALTH, self.agent).json()
        self.assertEqual(h['backlog_at'], NOW.isoformat())
        self.assertEqual(h['backlog'], [{'repo': 'alpha', 'fetched_at': None,
                                         'error': backlog.NO_CHECKOUT}])
        self.assertEqual((h['backlog_error'], h['backlog_timer']), (None, None))
        self.assertEqual(serve.status_lines(h, NOW)[2],
                         'backlog alpha: never fetched; ' + backlog.NO_CHECKOUT)
        # the real timer goes through the clean close and leaves no thread
        self.assertEqual(self.threads(), ['hub-backlog-refresh'])
        self.assertIs(hub.close(TIMEOUT), True)
        self.assertEqual(self.threads(), [])
        self.assertEqual(self.errors(), [])

    def test_a_round_whose_view_cannot_be_read_leaves_the_timer_running(self):
        def repos():
            raise KeyError('no list')

        hub = self.timer(repos)
        h = get(hub.port, HEALTH, self.agent).json()
        self.assertEqual(h['backlog_error'], backlog.NO_REPOS + ': KeyError')
        self.assertEqual((h['backlog'], h['backlog_timer']), ([], None))
        self.assertEqual(self.threads(), ['hub-backlog-refresh'])
        self.assertIs(hub.close(TIMEOUT), True)
        self.assertEqual(self.threads(), [])


class RealSyncCase(gitcase.Case):
    """The clean close over a real kitlib.hubsite.gitsync.GitSync on its own thread: a data work
    tree with a bare remote, on the fake git (docs/test-tools.md, "Fake git")."""

    def test_the_close_commits_and_pushes_what_was_written_since_the_last_pass(self):
        self.addCleanup(lambda: self.assertEqual(httpfix.http_threads(), [],
                                                 'a listener thread outlived its test'))
        passes, first, lines = [], threading.Event(), []

        def make(store_dir, every, remote=None, branch=None, push=True):
            sync = gitsync.GitSync(store_dir, every, remote=remote, branch=branch, push=push,
                                   git=self.runner, env=self.env)
            once = sync.sync_once

            def recorded():                 # the thread's pass at start and the close's last one
                out = once()
                passes.append(dict(out))
                first.set()
                return out

            sync.sync_once = recorded
            return sync

        tokens = os.path.join(gitcase.tmpdir(self), 'tokens')
        cfg = hub_settings.settings(None)._replace(port=0, store=self.hub, push=True,
                                                   commit_every_s=3600)
        server = serve.build(cfg, serve.Sources(
            tokens=tokens, static_dir=os.path.join(gitcase.tmpdir(self), 'web'), sync=make,
            pm_data={'path': self.wt, 'branch': 'pmdata', 'remote': 'origin'}),
            log=lambda level, text: lines.append((level, text)))
        self.addCleanup(server.close)
        self.assertEqual(server.notes, [])
        self.assertIsInstance(server.sync, gitsync.GitSync)
        self.assertIs(server.start(), True)
        self.assertTrue(first.wait(TIMEOUT), 'the pass at start never ran')
        r = post(server.port, BASE + '/todos', {'title': 'written before the close'},
                 auth.read_token(tokens, auth.OWNER))
        self.assertEqual(r.status, 200, r.body)
        tip = self.head()
        self.assertIs(server.close(TIMEOUT), True)
        # two passes: the thread's at start, and the last one of the close, which committed the
        # write whole and pushed it before the store was released
        self.assertEqual(len(passes), 2, passes)
        last = passes[-1]
        self.assertEqual((last['ok'], last['failed'], last['pending_commit'],
                          last['pending_push']), (True, '', False, False))
        self.assertNotEqual(self.head(), tip)
        self.assertEqual(self.git('log', '-1', '--format=%B').strip(), gitsync.SUBJECT)
        self.assertTrue([p for p in self.committed() if p.startswith('hub/todos/')],
                        self.committed())
        self.assertEqual(self.remote_tip(), self.head())
        self.assertIn(self.hub_status(), ('', gitcase.LOCK))
        self.assertEqual([t.name for t in threading.enumerate() if t.name == 'hub-gitsync'], [])
        Store.open(self.hub).close()        # the lock is free
        self.assertEqual([text for level, text in lines if level == 'error'], [])
        self.assertIn(('note', serve.LAST_PASS), lines)


class MachineCase(unittest.TestCase):
    """machine_sources reads the machine config under the home it is given, here a temp one."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix='kit-hubhome-')
        self.addCleanup(shutil.rmtree, self.home, True)
        env = mock.patch.dict(os.environ, {'USERPROFILE': self.home, 'HOME': self.home})
        env.start()
        self.addCleanup(env.stop)

    def config(self, obj):
        folder = os.path.join(self.home, '.pm-kit')
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, 'config.json'), 'w', encoding='utf-8') as f:
            json.dump(obj, f)

    def test_a_machine_with_no_config_serves_on_defaults(self):
        src = serve.machine_sources(self.home)
        self.assertEqual(src.tokens, os.path.join(self.home, '.pm-kit', 'hub'))
        self.assertEqual((src.pm_data, src.notices, src.repos, src.usage, src.backlog),
                         (None, None, None, None, None))
        self.assertIs(src.sync, gitsync.GitSync)
        self.assertEqual(src.static_dir, serve.STATIC_DIR)
        self.assertEqual(os.path.basename(serve.STATIC_DIR), 'web')
        self.assertEqual(os.path.split(src.agents)[1], 'agents')
        self.assertIsNone(serve.machine_sources(self.home, sync=False).sync)
        self.assertEqual(os.listdir(self.home), [], 'reading the sources wrote something')

    def test_the_pm_start_config_gives_the_repository_views(self):
        notices = os.path.join(self.home, 'notices')
        self.config({'pm_start': {'sessions_file': os.path.join(self.home, 'pm', 's.json'),
                                  'repo_paths': {'alpha': 'somewhere'}},
                     'machine_lock': {'notice': {'dir': notices}}})
        src = serve.machine_sources(self.home)
        self.assertEqual(src.notices, notices)
        self.assertTrue(callable(src.repos))
        views = src.backlog(77)
        self.assertIsInstance(views, backlog.Views)
        self.assertEqual(views.refresh_s, 77)

    def test_the_repository_list_asks_git_for_the_pm_root_once(self):
        cfg = {'sessions_file': os.path.join(self.home, 'pm', 's.json'), 'repo_paths': {'a': 'p'}}
        asked, listed = [], []

        def git(root, *args, **kw):
            asked.append((root, args))
            return b'the/pm/root\n'

        def repo_list(names, pm_root, paths=None):
            listed.append((names, pm_root, paths))
            return [(n, None) for n in names]

        with mock.patch.object(serve.hubbacklog, 'git', git), \
                mock.patch.object(serve.pmstart, 'known_repos', lambda c: ['a', 'b']), \
                mock.patch.object(serve.backlog, 'repo_list', repo_list):
            pairs = serve._repo_pairs(cfg)
            self.assertEqual(asked, [])
            self.assertEqual(pairs(), [('a', None), ('b', None)])
            self.assertEqual(pairs(), [('a', None), ('b', None)])
        self.assertEqual(asked, [(os.path.join(self.home, 'pm'),
                                  ('rev-parse', '--show-toplevel'))])
        self.assertEqual(listed, [(['a', 'b'], 'the/pm/root', {'a': 'p'})] * 2)

    def test_a_pm_data_object_that_cannot_be_used_is_a_config_error(self):
        self.config({'pm_data': 5})
        with self.assertRaises(ConfigError):
            serve.machine_sources(self.home)


class ClientCase(ServeCase):
    def test_ask_reads_and_writes_as_the_listener_requires(self):
        hub = self.build()
        hub.start()
        status, obj = serve.ask(hub.port, 'GET', HEALTH, self.owner)
        self.assertEqual((status, obj['ok']), (200, True))
        status, obj = serve.ask(hub.port, 'GET', HEALTH)
        self.assertEqual((status, obj['error']['code']), (403, 'forbidden'))
        status, obj = serve.ask(hub.port, 'POST', httpd.PAIR_ROUTE, self.owner)
        self.assertEqual(status, 200, obj)
        self.assertTrue(obj['path'].startswith('/pair?code='))
        status, obj = serve.ask(hub.port, 'POST', BASE + '/todos', self.owner, {'title': 'x'})
        self.assertEqual(status, 200, obj)
        self.assertEqual([d.get('via') for d in hub.store.docs('todos').values()],
                         [serve.VIA_CLI])
        self.assertEqual(self.errors(), [])

    def test_ask_tells_a_server_that_is_not_running_from_one_that_is_not_the_hub(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((httpd.LOOPBACK, 0))         # bound, never listening: nothing answers there
            port = s.getsockname()[1]
            with self.assertRaises(serve.NotRunning) as cm:
                serve.ask(port, 'GET', HEALTH, 'token', timeout=TIMEOUT)
            self.assertEqual(cm.exception.port, port)
            self.assertIsInstance(cm.exception, serve.ServeError)
        for answer in (b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nhi',
                       b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n[]', b''):
            with self.assertRaises(serve.ServeError) as cm:
                serve.ask(self.stranger(answer), 'GET', HEALTH, timeout=TIMEOUT)
            self.assertNotIsInstance(cm.exception, serve.NotRunning)
            self.assertNotIn('Traceback', str(cm.exception))

    def stranger(self, answer):
        """The port of a loopback server that is not the hub: it reads one request and answers
        these bytes."""
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(srv.close)
        srv.bind((httpd.LOOPBACK, 0))
        srv.listen(1)
        srv.settimeout(TIMEOUT)

        def serve_one():
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(TIMEOUT)
                try:
                    conn.recv(65536)
                    conn.sendall(answer)
                except OSError:
                    pass

        t = threading.Thread(target=serve_one, name='test-stranger', daemon=True)
        t.start()
        self.addCleanup(t.join, TIMEOUT)
        return srv.getsockname()[1]

    def test_status_lines(self):
        health = {'ok': True, 'revision': 7, 'store': 'the/store',
                  'git': {'running': True, 'branch': 'data', 'pending_commit': False,
                          'pending_push': True, 'diverged': False, 'failed': 'push'},
                  'backlog': [{'repo': 'alpha', 'fetched_at': '2026-01-10T11:00:00+00:00',
                               'error': None},
                              {'repo': 'beta', 'fetched_at': None, 'error': 'git timed out'}],
                  'backlog_at': '2026-01-10T12:00:00+00:00', 'backlog_error': None,
                  'backlog_timer': None, 'unavailable': {'health': 'KeyError'}}
        self.assertEqual(serve.status_lines(health, NOW), [
            'revision 7',
            'store the/store',
            'git: running, branch data, pending: a push, the last pass failed at push',
            'backlog alpha: fetched 60 min ago',
            'backlog beta: never fetched; git timed out',
            'unavailable: health (KeyError)'])
        self.assertEqual(serve.status_lines({'revision': 0, 'git': {'running': False,
                                                                     'off': serve.SYNC_OFF},
                                             'backlog': None}, NOW),
                         ['revision 0', 'git: ' + serve.SYNC_OFF,
                          'backlog: no source on this server'])
        self.assertEqual(serve.status_lines({'revision': 1, 'git': None, 'backlog': [],
                                             'backlog_at': None}, NOW),
                         ['revision 1', 'git: no answer', 'backlog: not read yet'])


if __name__ == '__main__':
    unittest.main()
