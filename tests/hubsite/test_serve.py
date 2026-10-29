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
from kitlib.hubsite import access, auth, backlog, client, gitsync, httpd, selfrestart, serve
from kitlib.hubsite import tunnel
from kitlib.hubsite import settings as hub_settings
from kitlib.hubsite.api import BASE
from kitlib.hubsite.store import Store, StoreBusy
from kitlib.proc import process
from kitlib.config import user
from tests.home import PM_REPO_FENCE_ENV, machine_config, pm_repo
from tests.hubsite import access_keys as ak
from tests.hubsite import httpfix
from tests.hubsite import test_gitsync as gitcase       # its Case: a data work tree on the fake git
from tests.hubsite.test_store import question
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
NOT_TASK = 'a test server is not the scheduled task\'s'   # what selfrestart.eligible says here
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
        self.stops = []         # the `stop` each read was given

    def clock(self):
        return NOW

    def get(self, fresh=False, stop=None):
        self.reads.append(fresh)
        self.stops.append(stop)
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


def second_ctrl_c(test):
    """Make a Ctrl+C a KeyboardInterrupt in this, the main thread, whatever the runner set for
    it; and after `test`, end every wait for git it left on a thread. The event the stand-ins
    of a hung git wait on: set after the test."""
    if threading.current_thread() is not threading.main_thread():
        test.skipTest('a Ctrl+C reaches the main thread only')
    was = signal.signal(signal.SIGINT, signal.default_int_handler)
    if was is not None:
        test.addCleanup(signal.signal, signal.SIGINT, was)
    release = threading.Event()

    def ended():
        for t in git_waits():
            t.join(TIMEOUT)
        test.assertEqual(git_waits(), [], 'a wait for git outlived its test')

    test.addCleanup(ended)
    test.addCleanup(release.set)
    return release


def cut_close(test, hub, timeout=TIMEOUT):
    """What hub.close() answers when a second Ctrl+C arrives during it. A KeyboardInterrupt that
    leaves the close would end the whole test run: it fails `test` instead."""
    try:
        with mock.patch.object(serve, 'POLL_S', 0.01):
            return hub.close(timeout)
    except KeyboardInterrupt:
        test.fail('the second Ctrl+C left the close as a KeyboardInterrupt')


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
        # a hub that sees new code asks whether it may restart itself: here it may not (no
        # PowerShell), and the asking runs in the test's thread
        for name, value in (('eligible', lambda port, store: NOT_TASK),
                            ('in_thread', selfrestart.run_now)):
            patcher = mock.patch.object(selfrestart, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

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
    def test_the_api_reads_the_machine_lock_through_the_door_it_is_given(self):
        from tests.hubsite import test_api_roles as roles
        door = roles.LockDoor()
        self.assertIs(self.build(lock=door).api.lock, door)
        self.hub.close()
        self.assertIsNone(self.build().api.lock)
        self.assertIsNone(serve.Sources().lock)

    def test_the_api_reads_the_approval_key_and_finds_a_checkout_by_its_remote(self):
        from kitlib.hubsite import approvals
        lists = [[('alpha', 'X:\\alpha'), ('beta', 'X:\\beta')]]

        class Repos(FakeBacklog):
            def repos(self):
                if isinstance(lists[0], Exception):
                    raise lists[0]
                return lists[0]

        ids = {'X:\\alpha': 'github.com/team/alpha', 'X:\\beta': 'github.com/team/beta'}
        patcher = mock.patch.object(approvals, 'remote_id', lambda root: (
            ids.get(root), None if root in ids else 'no remote'))
        patcher.start()
        self.addCleanup(patcher.stop)
        hub = self.build(backlog=lambda refresh_s: Repos(self.calls))
        key = hub.api.approval_key()
        self.assertIsNotNone(key)
        self.assertEqual(key, auth.read_approval_key(self.tokens))
        self.assertNotIn(key, (self.owner, self.agent))
        self.assertEqual(hub.api.checkout('github.com/team/beta'), 'X:\\beta')
        self.assertIsNone(hub.api.checkout('github.com/team/gamma'))
        lists[0] = KeyError('no list')                  # a list that cannot be read: none
        self.assertIsNone(hub.api.checkout('github.com/team/beta'))
        hub.close()
        # no backlog at all: no checkout
        self.assertIsNone(self.build().api.checkout('github.com/team/beta'))

    def test_the_api_names_an_approvals_repo_by_the_launchers_session_keys(self):
        """build() hands _repo_name the launcher's session keys: of two names on one checkout
        the coordinator's key wins over the folder name that sorts first, and the root's remote
        is asked of git once, not on every read."""
        from kitlib.hubsite import approvals

        class Repos(FakeBacklog):
            def repos(self):
                return [('Folder-Kit', 'X:\\kit'), ('kit', 'X:\\kit')]
        asked = []
        for p in (mock.patch.object(approvals, 'remote_id', lambda root: asked.append(root) or (
                      'github.com/team/kit', None)),
                  mock.patch.object(serve._api, 'launcher_keys', lambda: {'kit'})):
            p.start()
            self.addCleanup(p.stop)
        hub = self.build(backlog=lambda refresh_s: Repos(self.calls))
        for _ in range(3):
            self.assertEqual(hub.api.repo_name('github.com/team/kit'), 'kit')
        self.assertEqual(asked, ['X:\\kit'])
        hub.close()

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

    def test_the_sources_configured_repo_names_reach_the_built_store(self):
        names = lambda: ['Gamerepo']        # noqa: E731
        hub = self.build(known_repos=names)
        self.assertIs(hub.store.known_repos, names)
        self.assertEqual(hub.store.add('questions', question(repo='gamerepo')).warnings, [])
        r = hub.store.add('questions', question(repo='ghost'))
        self.assertEqual([f.code for f in r.warnings], ['repo-alias'])

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

    def test_a_server_on_port_0_keeps_no_sessions_file(self):
        hub = self.build()
        self.assertIsNotNone(hub.creds.pair(hub.creds.new_pairing_code()))
        self.assertIsNone(hub.creds.path)
        self.assertEqual([n for n in os.listdir(self.tokens) if n.startswith('browser-')], [])

    def test_a_paired_browser_stays_paired_when_the_server_is_built_again(self):
        probe = self.build()                # port 0 keeps no sessions: find a port, then ask it
        port = probe.port
        self.assertIs(probe.close(), True)
        first = self.build(self.settings._replace(port=port))
        # paired through the credentials: no connection is made to the first port, so the
        # second build can bind it again at once
        value = first.creds.pair(first.creds.new_pairing_code())
        self.assertTrue(os.path.isfile(os.path.join(self.tokens, auth.sessions_file(port))))
        self.assertIs(first.close(), True)
        hub = self.build(self.settings._replace(port=port))
        self.assertEqual(hub.port, port)
        self.assertEqual(hub.notes, [])
        hub.start()
        cookie = ('Cookie', '%s=%s' % (auth.cookie_name(port), value))
        r = get(port, HEALTH, headers=[cookie])
        self.assertEqual(r.status, 200, r.body)
        self.assertIn('store', r.json())
        # the owner's token unpairs it, here and in the file the next build reads
        self.assertEqual(post(port, httpd.UNPAIR_ROUTE, {}, self.owner).status, 200)
        self.assertEqual(get(port, HEALTH, headers=[cookie]).status, 403)
        self.assertEqual(auth.Credentials(self.tokens, port=port, persist=True)
                         .role_for({}, {auth.cookie_name(port): value}), None)
        self.assertEqual(self.errors(), [])

    def test_a_sessions_file_that_cannot_be_read_is_a_start_up_note(self):
        first = self.build()
        port = first.port
        self.assertIs(first.close(), True)
        os.makedirs(self.tokens, exist_ok=True)
        with open(os.path.join(self.tokens, auth.sessions_file(port)), 'w') as f:
            f.write('not json')
        hub = self.build(self.settings._replace(port=port))
        self.assertEqual(len(hub.notes), 1, hub.notes)
        self.assertIn(auth.sessions_file(port), hub.notes[0])

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

    def test_the_approval_key_is_made_beside_the_tokens_at_start(self):
        self.build()
        key = auth.read_approval_key(self.tokens)
        self.assertIsNotNone(key)
        self.assertNotIn(key, (auth.read_token(self.tokens, auth.OWNER),
                               auth.read_token(self.tokens, auth.AGENT)))

    def test_an_approval_key_that_cannot_serve_is_refused_and_the_store_is_released(self):
        auth.ensure_tokens(self.tokens)
        bad = os.path.join(self.tokens, auth.APPROVAL_FILE)
        with open(bad, 'w', encoding='ascii') as f:
            f.write(auth.read_token(self.tokens, auth.OWNER))   # the owner's token is no key
        with self.assertRaises(serve.ServeError) as cm:
            serve.build(self.settings, serve.Sources(tokens=self.tokens), log=self.log)
        self.assertIn(bad, str(cm.exception))
        self.assertFalse(self.locked())
        self.assertEqual(httpfix.http_threads(), [])

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
        # a stop that did not answer in its time is asked again with no limit, so the last pass
        # is never called beside a pass in flight; that one raised too: each raise is a line,
        # the pass may still run, and no last pass is called beside it
        self.assertEqual(self.calls, ['sync.start', 'sync.stop', 'sync.stop'])
        self.assertEqual(self.sync.timeouts, [serve.CLOSE_TIMEOUT_S, None])
        self.assertFalse(self.locked())
        self.assertEqual(len(self.errors()), 3, self.errors())
        self.assertIn('the git sync failed: RuntimeError', self.errors()[0])
        self.assertIn('the wait for the git sync pass in flight failed: RuntimeError',
                      self.errors()[1])
        self.assertEqual(self.errors()[2], serve.LEFT_RUNNING)
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


class StopCase(ServeCase):
    """The stop route of a built server (httpd.STOP_ROUTE): the owner's token ends run() as
    Ctrl+C does, and nothing else does."""

    def running(self, settings=None, **src):
        """A built hub that run() serves on a thread: the hub, the thread, and the list run()'s
        answer goes to."""
        hub = self.build(settings, **src)
        up, stop, out = threading.Event(), threading.Event(), []
        runner = threading.Thread(target=lambda: out.append(serve.run(hub, lambda h: up.set(),
                                                                      stop)),
                                  name='test-run', daemon=True)
        runner.start()
        self.addCleanup(runner.join, TIMEOUT)
        self.addCleanup(stop.set)
        self.assertTrue(up.wait(TIMEOUT), 'the server never started')
        return hub, runner, out

    def test_the_owners_stop_ends_run_with_a_clean_close(self):
        hub, runner, out = self.running(sync=self.sync_factory(),
                                        backlog=lambda s: FakeBacklog(self.calls))
        self.assertFalse(hub.stopping.is_set())
        r = post(hub.port, httpd.STOP_ROUTE, {}, self.owner)
        self.assertEqual(r.status, 200, r.body)
        self.assertEqual(r.json(), {'ok': True, 'stopping': True, 'pid': os.getpid(),
                                    'ctime': process.own_ctime()})
        runner.join(TIMEOUT)
        self.assertFalse(runner.is_alive(), 'run() did not end after the stop')
        self.assertEqual(out, [True])
        # the same close as Ctrl+C: the last sync pass after the listeners, then the store
        self.assertEqual(self.calls, ['sync.start', 'backlog.start', 'backlog.stop', 'sync.stop',
                                      'sync.sync_once'])
        self.assertEqual(httpfix.http_threads(), [])
        self.assertFalse(self.locked())
        with self.assertRaises(serve.NotRunning):
            serve.ask(hub.port, 'GET', HEALTH, self.owner)
        self.assertIn(('note', serve.STOP_ASKED), self.lines)
        self.assertEqual(self.errors(), [])

    def test_no_one_but_the_owners_token_stops_the_server(self):
        hub, runner, out = self.running()
        path = post(hub.port, httpd.PAIR_ROUTE, {}, self.owner).json()['path']
        session = ('Cookie', get(hub.port, path).header('Set-Cookie')[0].split(';')[0])
        self.assertIn('store', get(hub.port, HEALTH, headers=[session]).json())
        for what, token, headers in (('no credential', None, []), ('the agent', self.agent, []),
                                     ('a browser session', None, [session]),
                                     ('the agent beside a session', self.agent, [session])):
            with self.subTest(what=what):
                r = post(hub.port, httpd.STOP_ROUTE, {}, token, headers=headers)
                self.assertEqual((r.status, r.code()), (403, 'forbidden'), r.body)
        # writes the listener refuses before any route: no Origin, not JSON, another host
        r = send(hub.port, 'POST', httpd.STOP_ROUTE,
                 [bearer(self.owner), ('Content-Type', httpd.JSON)], b'{}')
        self.assertEqual(r.status, 403)
        r = send(hub.port, 'POST', httpd.STOP_ROUTE,
                 [bearer(self.owner), ('Origin', 'http://127.0.0.1:%d' % hub.port)], b'{}')
        self.assertEqual(r.status, 415)
        evil = 'evil.example:%d' % hub.port
        r = send(hub.port, 'POST', httpd.STOP_ROUTE,
                 [bearer(self.owner), ('Content-Type', httpd.JSON), ('Origin', 'http://' + evil)],
                 b'{}', host=evil)
        self.assertEqual(r.status, 403)
        self.assertFalse(hub.stopping.is_set())
        self.assertTrue(runner.is_alive())
        self.assertEqual(out, [])
        self.assertEqual(get(hub.port, HEALTH, self.owner).status, 200)

    def test_a_paired_browser_stays_paired_across_a_stop(self):
        probe = self.build()                # port 0 keeps no sessions: find a port, then ask it
        port = probe.port
        self.assertIs(probe.close(), True)
        hub, runner, out = self.running(self.settings._replace(port=port))
        value = hub.creds.pair(hub.creds.new_pairing_code())
        self.assertEqual(post(port, httpd.STOP_ROUTE, {}, self.owner).status, 200)
        runner.join(TIMEOUT)
        self.assertEqual(out, [True])
        reread = auth.Credentials(self.tokens, port=port, persist=True)
        self.assertEqual(reread.role_for({}, {auth.cookie_name(port): value}), auth.OWNER)


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
            'repo': 'Alpha', 'fetched_at': '2026-01-10T11:00:00+00:00',
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

    def test_the_timers_read_for_health_is_given_its_stop_and_may_be_cut_short(self):
        class Timed(FakeBacklog):
            cut = False                 # True: a read answers None, as one that was told to stop

            def wait(self, stop, seconds):
                return stop.is_set()

            def get(self, fresh=False, stop=None):
                got = FakeBacklog.get(self, fresh, stop)
                return None if self.cut else got

        fake = Timed(self.calls)
        hub = self.build(backlog=lambda refresh_s: fake)
        stop = threading.Event()
        hub.backlog.wait(stop, 5)
        # the read after a round is told when the timer stops (backlog.Views.get reads no
        # further repository then), so the close does not wait for every repository's read
        self.assertEqual(len(fake.stops), 1)
        self.assertIs(fake.stops[0], stop)
        was = hub.views.health()
        self.assertEqual(was['backlog_at'], ANSWER['at'])
        # a read that was cut short: health keeps what it had and the timer's thread goes on
        fake.cut = True
        self.assertIs(hub.backlog.wait(stop, 5), False)
        self.assertEqual(fake.reads, [False, False])
        self.assertEqual(hub.views.health(), was)
        # a client's read is given no stop: nothing ends it half way
        fake.cut = False
        hub.start()
        self.assertEqual(get(hub.port, BASE + '/backlog', self.agent).status, 200)
        self.assertIsNone(fake.stops[-1])

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
        for name in ('backlog', 'completed', 'repos', 'agents', 'skills'):
            r = get(hub.port, '%s/%s' % (BASE, name), self.owner)
            self.assertEqual((r.status, r.code()), (503, 'unavailable'), name)
            self.assertEqual(r.json()['error']['field'], 'views')
        self.assertEqual(get(hub.port, BASE + '/notices', self.owner).json()['items'], [])
        usage = get(hub.port, BASE + '/usage', self.owner).json()['items']
        self.assertEqual((usage['available'], usage['reason']), (False, serve.NO_USAGE_SOURCE))
        r = get(hub.port, BASE + '/snapshot', self.owner)
        self.assertEqual(r.status, 200)
        self.assertEqual(sorted(r.json()['unavailable']), ['agents', 'backlog', 'completed',
                                                           'repos', 'skills'])
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
        hub = self.build(usage=usage, repos=repos, agents=missing, skills=os.path.join(self.root, 'no-skills-here'),
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
        r = get(hub.port, BASE + '/skills', self.owner)
        self.assertEqual((r.json()['items']['available'], r.json()['items']['reason']),
                         (False, serve.SKILLS_FAILED))
        self.assertNotIn('no-skills-here', r.body.decode('utf-8'))
        r = get(hub.port, BASE + '/repos', self.owner)
        self.assertEqual(r.json()['items'], [{'name': 'alpha', 'aliases': []}])
        self.assertEqual(seen, [[]])
        self.assertEqual(get(hub.port, BASE + '/notices', self.owner).json()['items'], [])


class RecordBacklog(FakeBacklog):
    """FakeBacklog with backlog.Views's two record reads; `raises` is what they raise instead."""
    raises = None

    def record(self, repo, rid):
        self.calls.append(('record', repo, rid))
        if self.raises is not None:
            raise self.raises
        return {'repo': repo, 'id': rid, 'where': 'open'}

    def record_template(self, repo):
        self.calls.append(('record_template', repo))
        if self.raises is not None:
            raise self.raises
        return {'repo': repo, 'fields': []}

    def _named(self, repo):
        if repo.lower() != 'alpha':
            raise backlog.RecordError('not_found', backlog.NO_REPO, 'repo')
        return 'Alpha', 'C:\\alpha'

    def reread(self, name, root):
        self.calls.append(('reread', name, root))


class RecordViewsCase(ServeCase):
    def site(self, raises=None):
        source = RecordBacklog([])
        source.raises = raises
        return serve.SiteViews(None, serve.Sources(), source), source

    def test_the_record_views_are_bound_only_with_a_backlog(self):
        bare = serve.SiteViews(None, serve.Sources())
        self.assertFalse(hasattr(bare, 'record'))
        self.assertFalse(hasattr(bare, 'record_template'))
        views, source = self.site()
        self.assertEqual(views.record('kit', 'EX-1'), {'repo': 'kit', 'id': 'EX-1',
                                                        'where': 'open'})
        self.assertEqual(views.record_template('kit'), {'repo': 'kit', 'fields': []})
        self.assertEqual(source.calls, [('record', 'kit', 'EX-1'), ('record_template', 'kit')])
        self.assertEqual(source.reads, [], 'a record read is not a backlog read')

    def test_what_the_record_read_raises_is_a_hub_error_with_its_fixed_phrase(self):
        for raised, want in (
                (backlog.RecordError('not_found', backlog.NO_RECORD, 'id'),
                 ('not_found', backlog.NO_RECORD, 'id')),
                (backlog.RecordError('not_found', backlog.NO_REPO, 'repo'),
                 ('not_found', backlog.NO_REPO, 'repo')),
                (backlog.RecordError('invalid', backlog.BAD_ID, 'id'),
                 ('invalid', backlog.BAD_ID, 'id')),
                (backlog.ReadError(backlog.NO_CHECKOUT), ('unavailable', backlog.NO_CHECKOUT,
                                                          'views')),
                (backlog.ReadError('C:\\secret\\folder'), ('unavailable', backlog.READ_FAILED,
                                                          'views'))):
            views, _ = self.site(raised)
            for call in (lambda: views.record('kit', 'EX-1'),
                         lambda: views.record_template('kit')):
                with self.subTest(raised=raised):
                    with self.assertRaises(serve.HubError) as got:
                        call()
                    e = got.exception
                    self.assertEqual((e.code, e.message, e.field), want)

    def test_the_routes_answer_over_http(self):
        source = RecordBacklog(self.calls)
        hub = self.build(backlog=lambda refresh_s: source)
        hub.start()
        r = get(hub.port, BASE + '/records/Alpha/A-1', self.agent)
        self.assertEqual(r.status, 200, r.body)
        self.assertEqual((r.json()['repo'], r.json()['id']), ('Alpha', 'A-1'))
        r = get(hub.port, BASE + '/records/Alpha/new', self.owner)
        self.assertEqual((r.status, r.json()['fields']), (200, []))
        source.raises = backlog.RecordError('not_found', backlog.NO_RECORD, 'id')
        r = get(hub.port, BASE + '/records/Alpha/A-404', self.agent)
        self.assertEqual((r.status, r.json()['error']['message']), (404, backlog.NO_RECORD))
        r = get(hub.port, BASE + '/records/Alpha/%2e%2e', self.agent)
        self.assertEqual((r.status, r.json()['error']['code']), (400, 'invalid'))
        self.assertNotIn('%2e', r.body.decode('utf-8'))
        self.assertEqual([c for c in self.calls if isinstance(c, tuple)],
                         [('record', 'Alpha', 'A-1'), ('record_template', 'Alpha'),
                          ('record', 'Alpha', 'A-404')])

    def test_the_record_writes_are_bound_only_with_a_backlog(self):
        bare = serve.SiteViews(None, serve.Sources())
        for name in ('record_add', 'record_edit', 'record_delete'):
            self.assertFalse(hasattr(bare, name))
            self.assertTrue(callable(getattr(self.site()[0], name)))

    def test_a_write_goes_to_the_configured_root_and_answers_the_record_read_after(self):
        views, source = self.site()
        seen = source.calls

        def fake(name):
            return lambda *a: seen.append((name,) + a) or {'lines': [name]}
        with mock.patch.multiple(serve.recordwrite, add=fake('add'), edit=fake('edit'),
                                 delete=fake('delete')):
            self.assertEqual(views.record_add('alpha', 'A-2', 's', ['x'], None, None),
                             {'lines': ['add'], 'record': {'repo': 'Alpha', 'id': 'A-2',
                                                           'where': 'open'}})
            self.assertEqual(views.record_edit('ALPHA', 'A-2', 'c:v', [('effort', ['s'])], [],
                                               None)['record']['id'], 'A-2')
            self.assertEqual(views.record_delete('Alpha', 'A-2', 'w'), {'lines': ['delete']})
        # each write is followed by a read of that repository, so the snapshot shows it
        reread = ('reread', 'Alpha', 'C:\\alpha')
        self.assertEqual(seen, [('add', 'C:\\alpha', 'A-2', 's', ['x'], None, None), reread,
                                ('record', 'Alpha', 'A-2'),
                                ('edit', 'C:\\alpha', 'A-2', 'c:v', [('effort', ['s'])], [],
                                 None), reread, ('record', 'Alpha', 'A-2'),
                                ('delete', 'C:\\alpha', 'A-2', 'w'), reread])

    def test_a_bad_id_or_a_repository_not_configured_is_refused_before_any_write(self):
        views, _ = self.site()
        with mock.patch.object(serve.recordwrite, 'add') as add:
            for repo, rid, want in (('Alpha', 'A..B', ('invalid', backlog.BAD_ID, 'id')),
                                    ('beta', 'A-2', ('not_found', backlog.NO_REPO, 'repo'))):
                with self.subTest(repo=repo, rid=rid):
                    with self.assertRaises(serve.HubError) as got:
                        views.record_add(repo, rid, 's', ['x'], None, None)
                    e = got.exception
                    self.assertEqual((e.code, e.message, e.field), want)
            add.assert_not_called()


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

    def test_scrub_takes_too_much_rather_than_leave_a_password_that_holds_quotes_and_a_comma(self):
        # a password may hold `','`, so that text does not end the user part of a URL ...
        self.assertEqual(serve.scrub("https://someone:s3','cret@git.example.test/x"),
                         'https://git.example.test/x')
        # ... and two quoted words with no space between lose what stands before the `@`: the
        # known price (serve._USERINFO), text that held no password
        self.assertEqual(serve.scrub("'https://h','x@y'"), "'https://y'")

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

    def test_watch_says_once_that_the_hub_code_changed_since_this_server_started(self):
        stamp = [(('serve.py', 1, 10),)]
        with mock.patch.object(serve, 'code_stamp', lambda: stamp[0]),                 mock.patch.object(serve, 'CODE_LOOK_S', 0):
            hub = self.build()
            hub.watch()
            self.assertEqual(self.lines, [])
            stamp[0] = (('serve.py', 2, 12),)
            hub.watch()
            hub.watch()
            self.assertEqual(self.lines, self.warned())
            self.assertIn('restart', serve.STALE_CODE)
            stamp[0] = (('serve.py', 3, 14),)
            hub.watch()
            self.assertEqual(len(self.lines), 3, 'said again for the same stale server')
        hub.close()

    def warned(self):
        """The lines of a hub that is not its task's when it first sees new code."""
        return [('note', selfrestart.CHANGED), ('note', selfrestart.NOT_HERE % NOT_TASK),
                ('error', serve.STALE_CODE)]

    def test_a_task_run_server_restarts_itself_on_new_code_and_shows_no_warning(self):
        stamp = [(('serve.py', 1, 10),)]
        started = []
        with mock.patch.object(serve, 'code_stamp', lambda: stamp[0]), \
                mock.patch.object(serve, 'CODE_LOOK_S', 0), \
                mock.patch.object(selfrestart, 'SETTLE_S', 0), \
                mock.patch.object(selfrestart, 'eligible', lambda port, store: None), \
                mock.patch.object(selfrestart, 'check', lambda: (True, None)), \
                mock.patch.object(selfrestart, 'launch',
                                  lambda argv: started.append(argv) or (4242, None)):
            hub = self.build()
            hub.start()
            stamp[0] = (('serve.py', 2, 12),)
            for _ in range(3):          # asked, checked, restarted: one turn each
                hub.watch()
            log = os.path.join(self.tokens, selfrestart.LOG_NAME)
            self.assertEqual(started, [selfrestart.restarter_argv(log, hub.port)])
            self.assertEqual(self.lines[0], ('note', selfrestart.CHANGED))
            self.assertEqual(self.lines[-1], ('note', selfrestart.LAUNCHED % (
                selfrestart.digest((('serve.py', 1, 10),)), selfrestart.digest(stamp[0]), 4242,
                hub.port, log)))
            self.assertEqual(self.errors(), [])
            self.assertEqual(get(hub.port, HEALTH, self.owner).json()['code'],
                             {'stale': False, 'restart': None})
            self.assertTrue(os.path.isfile(log))
        hub.close()

    def test_a_hub_knows_its_token_folder_for_the_restart_log(self):
        self.assertEqual(self.build().tokens, self.tokens)

    def test_the_hub_code_is_its_package_its_jobs_and_the_settings_packages(self):
        from kitlib import hubjobs, pmdata
        folders = serve.code_folders()
        for module in (serve, hubjobs, pmdata, user):
            self.assertIn(os.path.dirname(os.path.abspath(module.__file__)), folders)
        self.assertEqual(os.path.basename(folders[-1]), 'config')
        self.assertEqual(len(set(folders)), 4)

    def test_quiet_waits_for_every_request_in_flight_and_every_running_job(self):
        class Lis(object):
            busy = False

            def drain(self, timeout=None):
                assert timeout == 0, 'never waits'
                return not self.busy

        class Jobs(object):
            blocks = {'a': {'running': False}}

            def status(self):
                if isinstance(self.blocks, Exception):
                    raise self.blocks
                return self.blocks

        hub = self.build()
        real = hub.listeners
        hub.listeners = [Lis(), Lis()]
        self.assertTrue(hub._quiet())
        hub.listeners[1].busy = True
        self.assertFalse(hub._quiet(), 'a request in flight')
        hub.listeners[1].busy = False
        hub.jobs = Jobs()
        self.assertTrue(hub._quiet())
        hub.jobs.blocks = {'a': {'running': False}, 'b': {'running': True}}
        self.assertFalse(hub._quiet(), 'a job running')
        hub.jobs.blocks = RuntimeError('gone')
        self.assertFalse(hub._quiet(), 'jobs that cannot say are not known to be idle')
        hub.jobs, hub.listeners = None, real
        hub.close()

    def test_quiet_is_false_while_a_request_is_being_answered(self):
        hub = self.build()
        hub.start()
        lis = hub.listeners[0]
        self.assertTrue(hub._quiet())
        with lis._cond:
            lis._active += 1            # a request in its handler
        try:
            self.assertFalse(hub._quiet())
        finally:
            with lis._cond:
                lis._active -= 1
        self.assertTrue(hub._quiet())
        hub.close()

    def test_a_self_restart_look_that_raises_shows_the_warning_and_the_server_goes_on(self):
        class Broken(object):
            stale = False

            def look(self, stamp):
                raise RuntimeError('broken look')

        stamp = [(('serve.py', 1, 10),)]
        with mock.patch.object(serve, 'code_stamp', lambda: stamp[0]), \
                mock.patch.object(serve, 'CODE_LOOK_S', 0):
            hub = self.build()
            hub.self_restart = Broken()
            hub.watch()
            self.assertEqual(self.lines, [
                ('error', 'the self-restart look failed: RuntimeError: broken look'),
                ('error', serve.STALE_CODE)])
            hub.watch()
            self.assertEqual(len(self.lines), 3, 'the look is tried again, STALE_CODE not')
        hub.close()

    def test_health_and_snapshot_say_when_the_hub_code_on_disk_is_newer(self):
        stamp = [(('serve.py', 1, 10),)]
        with mock.patch.object(serve, 'code_stamp', lambda: stamp[0]),                 mock.patch.object(serve, 'CODE_LOOK_S', 0):
            hub = self.build()
            hub.start()
            for path in (HEALTH, BASE + '/snapshot'):
                self.assertEqual(get(hub.port, path, self.owner).json()['code'],
                                 {'stale': False, 'restart': None}, path)
            stamp[0] = (('serve.py', 2, 12),)
            hub.watch()
            for path in (HEALTH, BASE + '/snapshot'):
                code = get(hub.port, path, self.owner).json()['code']
                self.assertIs(code['stale'], True, path)
                self.assertIn('hub.py" restart', code['restart'])
                self.assertIn('hub.py" stop, then python', code['restart'])   # the fallback for a hub with no autostart task
                self.assertIn('hub.py" serve', code['restart'])
        hub.close()

    def test_a_code_status_that_raises_or_is_none_gives_no_code_and_health_still_answers(self):
        hub = self.build()
        hub.start()
        self.assertEqual(hub.api.code_seen(), {'stale': False, 'restart': None})
        hub.api.code_status = lambda: {}['gone']
        self.assertIsNone(hub.api.code_seen())
        self.assertIsNone(get(hub.port, HEALTH, self.owner).json()['code'])
        hub.api.code_status = None
        self.assertIsNone(hub.api.code_seen())
        hub.api.code_status = lambda: 'old'
        self.assertIsNone(hub.api.code_seen())
        hub.close()

    def test_watch_takes_the_code_stamp_at_most_once_in_the_look_window(self):
        looks = []

        def stamp():
            looks.append(1)
            return (('serve.py', len(looks), 1),)

        with mock.patch.object(serve, 'code_stamp', stamp):
            hub = self.build()
            hub.watch()
            hub.watch()
            self.assertEqual(len(looks), 1, 'a look inside the window')
            hub._looked -= serve.CODE_LOOK_S
            hub.watch()
            self.assertEqual(len(looks), 2)
            self.assertEqual(self.lines, self.warned())
        hub.close()

    def test_watch_survives_a_code_look_that_fails(self):
        with mock.patch.object(serve, 'code_stamp', lambda: {}['gone']),                 mock.patch.object(serve, 'CODE_LOOK_S', 0):
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
                # a step that ran out of time stops none after it: the requests had ended, so
                # the last pass ran, and the store was released. A sync whose thread had not
                # ended is waited for with no limit before that pass (a second stop); this one
                # does not end then either, so no last pass is called beside its pass.
                late = step == 'sync'
                self.assertEqual(self.errors(), [line] + [serve.LEFT_RUNNING] * late)
                self.assertEqual(self.calls, ['sync.start', 'backlog.start', 'backlog.stop']
                                 + ['sync.stop'] * (2 if late else 1)
                                 + ['sync.sync_once'] * (not late))
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
        return second_ctrl_c(self)

    def cut(self, hub, timeout=TIMEOUT):
        return cut_close(self, hub, timeout)

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

    def test_a_ctrl_c_in_a_close_with_no_sync_leaves_it_with_the_store_released(self):
        hub = self.interrupted()
        with self.assertRaises(KeyboardInterrupt):
            hub.close(TIMEOUT)
        self.assertEqual(self.lines, [])
        self.assertFalse(self.locked(), 'the store was not released')
        self.assertEqual(httpfix.http_threads(), [])
        self.assertIs(hub.close(), False)

    def test_a_ctrl_c_in_the_last_two_steps_leaves_the_close_with_a_sync_too(self):
        hub = self.build(sync=self.sync_factory())
        hub.start()
        lis = hub.listeners[0]
        join = lis.join

        def cut(timeout=None):
            join(timeout)
            raise KeyboardInterrupt()

        lis.join = cut
        with self.assertRaises(KeyboardInterrupt):
            hub.close(TIMEOUT)
        # every wait for git had ended: no line says that a git command may be left half-way
        self.assertEqual(self.lines, [('note', serve.LAST_PASS)])
        self.assertEqual(self.calls, ['sync.start', 'sync.stop', 'sync.sync_once'])
        self.assertFalse(self.locked(), 'the store was not released')
        self.assertEqual(httpfix.http_threads(), [])
        self.assertIs(hub.close(), False)

    def test_a_store_closed_beside_a_pass_that_may_still_run_is_said(self):
        for ends in (False, True, RuntimeError(SECRET)):
            with self.subTest(ends=ends):
                del self.calls[:], self.lines[:]
                self.store_dir = tempfile.mkdtemp(prefix='store-', dir=self.root)
                self.settings = self.settings._replace(store=self.store_dir)
                hub = self.in_flight(Late(self, ends))
                self.assertIs(hub.close(0.05), False)
                # a wait that raised is a line of its own, before the one that says what follows
                raised = ['closing: the wait for the git sync pass in flight failed: '
                          'RuntimeError: ' + SECRET] if isinstance(ends, Exception) else []
                self.assertEqual(self.errors(), [
                    'closing: the requests in flight did not end within 0.05 s',
                    serve.LAST_PASS_SKIPPED,
                    'closing: the git sync did not end within 0.05 s'] + raised + [
                    serve.LEFT_RUNNING] * (ends is not True) + [
                    'closing: the handler threads did not end within 0.05 s'])
                if ends is True:        # the wait with no limit ended: said first, then waited for
                    self.assertIn(('note', serve.IN_FLIGHT), self.lines)
                    self.assertIn(('note', serve.STILL_WAITING), self.lines)
                self.assertEqual(self.sync.timeouts, [0.05, None])
                self.assertFalse(self.locked(), 'the store was not released')

    def test_a_sync_that_cannot_be_told_to_begin_no_pass_does_not_keep_the_store(self):
        release = self.ctrl_c()

        class Deaf(Stuck):              # its stop with no wait, the one a cut close makes, raises
            def stop(self, timeout=None):
                if timeout != 0:
                    return Stuck.stop(self, timeout)
                self.case.calls.append('sync.stop')
                self.timeouts.append(timeout)
                raise RuntimeError(SECRET)

        hub = self.build(sync=self.given(Deaf(self, 'sync_once', release)))
        hub.start()
        self.assertIs(self.cut(hub), False)
        self.assertEqual(self.lines, [('note', serve.LAST_PASS), ('error', serve.CUT_SHORT)])
        self.assertFalse(self.locked(), 'the store was not released')

    def test_a_pass_in_flight_is_waited_for_before_the_last_pass_is_called(self):
        hub = self.build(sync=self.given(Late(self)))
        hub.start()
        self.assertIs(hub.close(TIMEOUT), False)
        # no further line: LAST_PASS said that a pass in flight ends first
        self.assertEqual(self.lines, [
            ('note', serve.LAST_PASS),
            ('error', 'closing: the git sync did not end within %g s' % TIMEOUT)])
        # the wait for the pass in flight is a stop with no limit, which begins no pass; the last
        # pass is called after it, with the store still locked
        self.assertEqual(self.calls, ['sync.start', 'sync.stop', 'sync.stop', 'sync.sync_once'])
        self.assertEqual(self.sync.timeouts, [TIMEOUT, None])
        self.assertEqual(self.sync.waited, {'locked': True, 'lines': self.lines})
        self.assertIs(self.sync.last['locked'], True)
        self.assertEqual(self.sync.on, [serve.GIT_WAIT_THREAD] * 3)
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
        self.assertEqual(self.errors(), [])     # health carries it: no line for it

    def test_a_round_whose_read_fails_is_one_error_line_not_silence(self):
        class Old(FakeBacklog):         # a backlog source whose get takes no `stop`
            def get(self, fresh=False):
                return FakeBacklog.get(self, fresh)

            def wait(self, stop, seconds):
                return True

        source = Old(self.calls)
        hub = self.build(backlog=lambda refresh_s: source)
        stop = threading.Event()
        self.assertIs(source.wait(stop, 1), True)      # the pause after a round: serve's wrap
        self.assertEqual(len(self.errors()), 1, self.lines)
        line = self.errors()[0]
        self.assertTrue(line.startswith(serve.ROUND_FAILED % ('TypeError', '')), line)
        self.assertIn("unexpected keyword argument 'stop'", line)
        self.assertIn('health does not follow the timer', line)
        self.assertEqual(hub.views.health()['backlog'], [])    # what it said before: nothing
        # the same failure in the next round is not said again; one that is new is
        source.wait(stop, 1)
        self.assertEqual(self.errors(), [line])
        source.get = lambda fresh=False, stop=None: {}['repos']
        source.wait(stop, 1)
        self.assertEqual(self.errors(), [line, serve.ROUND_FAILED % ('KeyError', "'repos'")])
        # a read that works again, then the first failure: said again
        source.get = lambda fresh=False, stop=None: copy.deepcopy(ANSWER)
        source.wait(stop, 1)
        self.assertEqual(len(hub.views.health()['backlog']), len(ANSWER['repos']))
        del source.get
        source.wait(stop, 1)
        self.assertEqual(self.errors()[2:], [line])

    def test_a_round_failure_is_logged_without_the_user_or_password_of_a_url(self):
        class Broken(FakeBacklog):
            def get(self, fresh=False, stop=None):
                raise RuntimeError(WHY)

            def wait(self, stop, seconds):
                return True

        source = Broken(self.calls)
        self.build(backlog=lambda refresh_s: source)
        source.wait(threading.Event(), 1)
        self.assertEqual(len(self.errors()), 1)
        for word in CREDENTIALS:
            self.assertNotIn(word, self.errors()[0])
        self.assertIn('git.example.test', self.errors()[0])
        # a log that raises does not reach the timer's thread
        views = serve.SiteViews(None, serve.Sources(tokens=self.tokens), Broken(self.calls),
                                log=lambda level, text: {}['gone'])
        self.assertIsNone(views.after_round(threading.Event()))


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

    def test_a_close_cut_short_beside_a_pass_in_flight_begins_no_pass_after_it(self):
        second_ctrl_c(self)
        self.addCleanup(lambda: self.assertEqual(httpfix.http_threads(), [],
                                                 'a listener thread outlived its test'))
        entered, gate = threading.Event(), threading.Event()
        passes, stops, lines, sent = [], [], [], []

        def git(args, cwd, **kw):           # the thread's pass at start hangs in its first git
            if threading.current_thread().name == 'hub-gitsync' and not entered.is_set():
                entered.set()
                gate.wait(TIMEOUT)
            return self.runner(args, cwd, **kw)

        def ctrl_c():                       # the owner's second one, once
            if not sent:
                sent.append(True)
                _thread.interrupt_main()

        def make(store_dir, every, remote=None, branch=None, push=True):
            sync = gitsync.GitSync(store_dir, every, remote=remote, branch=branch, push=push,
                                   git=git, env=self.env)
            once, stop = sync.sync_once, sync.stop

            def recorded():
                if threading.current_thread().name == serve.GIT_WAIT_THREAD:
                    ctrl_c()                # a last pass called beside the one in flight
                out = once()
                passes.append(threading.current_thread().name)
                return out

            def stopping(timeout=None):
                stops.append(timeout)
                if timeout is None:
                    ctrl_c()                # the wait for the pass in flight
                return stop(timeout)

            sync.sync_once, sync.stop = recorded, stopping
            return sync

        def sync_threads():
            return [t for t in threading.enumerate()
                    if t.name in ('hub-gitsync', serve.GIT_WAIT_THREAD)]

        def ended():
            for t in sync_threads():
                t.join(TIMEOUT)
            self.assertEqual(sync_threads(), [], 'a git thread outlived its test')

        tokens = os.path.join(gitcase.tmpdir(self), 'tokens')
        cfg = hub_settings.settings(None)._replace(port=0, store=self.hub, push=True,
                                                   commit_every_s=3600)
        server = serve.build(cfg, serve.Sources(
            tokens=tokens, static_dir=os.path.join(gitcase.tmpdir(self), 'web'), sync=make,
            pm_data={'path': self.wt, 'branch': 'pmdata', 'remote': 'origin'}),
            log=lambda level, text: lines.append((level, text)))
        self.addCleanup(ended)
        self.addCleanup(server.close)
        self.addCleanup(gate.set)
        self.assertIs(server.start(), True)
        self.assertTrue(entered.wait(TIMEOUT), 'the pass at start never began')
        self.assertIs(cut_close(self, server, 0.05), False)
        self.assertEqual(sent, [True], 'no wait of the close was left by the Ctrl+C')
        self.assertIn(('error', serve.CUT_SHORT), lines)
        Store.open(self.hub).close()        # the lock is free, beside the pass still in flight
        self.assertEqual(passes, [])
        # the pass in flight ends after the store closed. GitSync's stop began none, and no call
        # of sync_once was left behind that pass to begin one.
        gate.set()
        ended()
        self.assertEqual(passes, ['hub-gitsync'])
        self.assertEqual(stops[0], 0.05)
        self.assertEqual(stops[-1], 0)
        self.assertEqual(server.sync.status()['running'], False)


class MachineCase(unittest.TestCase):
    """machine_sources reads the machine config under the home it is given, here a temp one."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix='kit-hubhome-')
        self.addCleanup(shutil.rmtree, self.home, True)
        env = mock.patch.dict(os.environ, {'USERPROFILE': self.home, 'HOME': self.home})
        env.start()
        self.addCleanup(env.stop)

    def config(self, obj):
        """The machine config `obj`, in the fenced PM repository's two files: their folder."""
        return machine_config(self.home, obj, case=self)

    def test_a_machine_with_no_pointer_has_no_sources(self):
        # no token folder without the PM-repo pointer: hub.py serve refuses, never a home folder
        with mock.patch.dict(os.environ):
            os.environ.pop(PM_REPO_FENCE_ENV, None)
            user.forget()
            self.addCleanup(user.forget)
            with self.assertRaises(ConfigError) as got:
                serve.machine_sources(self.home)
        self.assertIn('--pm-repo', str(got.exception))
        self.assertEqual(os.listdir(self.home), [], 'reading the sources wrote something')

    def test_a_machine_with_no_config_serves_on_defaults(self):
        pm = pm_repo(self, record=False)
        src = serve.machine_sources(self.home)
        self.assertEqual(os.path.normcase(src.tokens),
                         os.path.normcase(os.path.join(pm, user.DIR_NAME, auth.FOLDER_NAME)))
        self.assertEqual((src.pm_data, src.notices, src.repos, src.usage, src.backlog),
                         (None, None, None, None, None))
        self.assertIs(src.sync, gitsync.GitSync)
        self.assertEqual(src.static_dir, serve.STATIC_DIR)
        self.assertEqual(os.path.basename(serve.STATIC_DIR), 'web')
        self.assertEqual(os.path.split(src.agents)[1], 'agents')
        self.assertEqual(src.skills, os.path.join(serve.pmstart.KIT_ROOT, 'content', 'skills'))
        self.assertIsNone(serve.machine_sources(self.home, sync=False).sync)
        # the machine lock's door, made now and opened on its first read (none here)
        from kitlib.hubsite import lockdoor
        self.assertIsInstance(src.lock, lockdoor.LockDoor)
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
                mock.patch.object(serve.pmstart, 'known_repos', lambda c, **kw: ['a', 'b']), \
                mock.patch.object(serve.backlog, 'repo_list', repo_list):
            pairs = serve._repo_pairs(cfg)
            self.assertEqual(asked, [])
            self.assertEqual(pairs(), [('a', None), ('b', None)])
            self.assertEqual(pairs(), [('a', None), ('b', None)])
        self.assertEqual(asked, [(os.path.join(self.home, 'pm'),
                                  ('rev-parse', '--show-toplevel'))])
        self.assertEqual(listed, [(['a', 'b'], 'the/pm/root', {'a': 'p'})] * 2)

    def test_an_unreadable_sessions_file_gives_no_repo_names_and_a_write_still_lands(self):
        self.config({'pm_start': {'sessions_file': os.path.join(self.home, 'missing', 's.json')}})
        src = serve.machine_sources(self.home)
        self.assertEqual(src.known_repos(), ())
        root = os.path.join(self.home, 'store')
        store = Store.open(root, known_repos=src.known_repos)
        self.addCleanup(store.close)
        self.assertEqual(store.put('repos', 'alpha', {'name': 'alpha'}).warnings, [])

    def sessions(self, text, **pm_start):
        """A sessions file holding `text` under the temp home, and the config naming it."""
        path = os.path.join(self.home, 'pm', 's.json')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)
        pm_start['sessions_file'] = path
        self.config({'pm_start': pm_start})
        return path

    def store(self, src):
        store = Store.open(os.path.join(self.home, 'store'), known_repos=src.known_repos)
        self.addCleanup(store.close)
        return store

    def test_the_configured_repo_names_reach_the_sources_and_git_is_asked_once(self):
        path = self.sessions(json.dumps({'g': {'repo': 'Gamerepo'}, 'x': 5}), repos=['extra'])
        asked = []

        def toplevel(folder):
            asked.append(folder)
            return 'pmrepo' if folder == os.path.dirname(path) else 'kitrepo'

        with mock.patch.object(serve.pmstart, '_toplevel', toplevel):
            src = serve.machine_sources(self.home)
            self.assertEqual(asked, [], 'reading the sources ran git')
            names = ['extra', 'Gamerepo', 'kitrepo', 'pmrepo']
            want = names + ['g']                    # the session's key is an alias
            self.assertEqual(src.known_repos(), want)
            store = self.store(src)
            for _ in range(3):
                self.assertEqual(store.add('questions', question(repo='gamerepo')).warnings, [])
            self.assertEqual(store.add('questions', question(repo='PMREPO')).warnings, [])
            r = store.add('questions', question(repo='ghost'))
            self.assertEqual([f.code for f in r.warnings], ['repo-alias'])
            self.assertEqual(src.known_repos(), want)
            for _ in range(3):                      # the repos view, on every snapshot
                self.assertEqual([r['name'] for r in src.repos([])], names)
        self.assertEqual(len(asked), 2, asked)      # the two folders, once each, for every call
        self.assertEqual(asked[0], os.path.dirname(path))

    def test_a_session_key_is_an_alias_of_its_repo_so_its_tag_has_no_warning(self):
        self.sessions(json.dumps({'kit': {'repo': 'kitrepo'}, 'pm-kit': {'repo': 'kitrepo'}}))
        with mock.patch.object(serve.pmstart, '_toplevel', lambda d: 'kitrepo'):
            src = serve.machine_sources(self.home)
            store = self.store(src)
            for tag in ('kit', 'PM-KIT'):
                self.assertEqual(store.add('questions', question(repo=tag)).warnings, [])
            r = store.add('questions', question(repo='ghost'))
            self.assertEqual([f.code for f in r.warnings], ['repo-alias'])
            menu = src.repos([])
            self.assertEqual(menu, [{'name': 'kitrepo', 'aliases': ['kit', 'pm-kit']}])

    def test_a_missing_sessions_file_leaves_the_session_keys_out(self):
        self.config({'pm_start': {'sessions_file': os.path.join(self.home, 'missing', 's.json')}})
        src = serve.machine_sources(self.home)
        self.assertEqual(src.known_repos(), ())
        self.assertEqual(serve._session_aliases({'sessions_file': os.path.join(self.home, 'x')}), {})
        r = self.store(src).add('questions', question(repo='kit'))      # the write still lands
        self.assertEqual([f.code for f in r.warnings], ['repo-alias'])   # today's behaviour

    def test_a_sessions_file_whose_json_is_not_an_object_gives_no_repo_names(self):
        for text in ('[]', 'null', '5', '"s"'):
            self.sessions(text)
            src = serve.machine_sources(self.home)
            self.assertEqual(src.known_repos(), (), text)
        store = self.store(src)
        r = store.add('questions', question(repo='gamerepo'))      # the write still lands
        self.assertEqual(store.get('questions', r.id)['repo'], 'gamerepo')

    def test_bad_json_or_a_bad_repos_value_gives_no_repo_names(self):
        for text, repos in (('not json', None), ('{}', 'gamerepo'), ('{}', [''])):
            kw = {} if repos is None else {'repos': repos}
            self.sessions(text, **kw)
            src = serve.machine_sources(self.home)
            self.assertEqual(src.known_repos(), (), (text, repos))
        store = self.store(src)
        r = store.add('questions', question(repo='gamerepo'))      # the write still lands
        self.assertEqual([f.code for f in r.warnings], ['repo-alias'])
        self.assertEqual(store.get('questions', r.id)['repo'], 'gamerepo')

    def test_a_sessions_file_that_is_not_a_path_gives_no_repo_names(self):
        self.config({'pm_start': {'sessions_file': ['not', 'a', 'path']}})
        self.assertEqual(serve.machine_sources(self.home).known_repos(), ())

    def test_with_no_git_the_names_are_what_the_sessions_file_says(self):
        self.sessions(json.dumps({'g': {'repo': 'Gamerepo'}}), repos=['extra'])
        ran = []

        def no_git(*a, **kw):
            ran.append(a[0])
            raise FileNotFoundError('git')

        with mock.patch('kitlib.git.cmd.subprocess_run', no_git):
            src = serve.machine_sources(self.home)
            self.assertEqual(src.known_repos(), ['extra', 'Gamerepo', 'g'])
            self.assertEqual(self.store(src).add('questions', question(repo='gamerepo')).warnings,
                             [])
        self.assertEqual(len(ran), 2, ran)          # git was tried, for both folders

    def test_a_found_top_level_is_kept_and_a_miss_is_asked_again_after_a_while(self):
        now, answers, asked = [0.0], {'pm': 'pmrepo', 'gone': None}, []

        def toplevel(folder):
            asked.append(folder)
            return answers[folder]
        with mock.patch.object(serve.pmstart, '_toplevel', toplevel):
            tops = serve._toplevels(clock=lambda: now[0])
            for _ in range(3):
                self.assertEqual((tops('pm'), tops('gone')), ('pmrepo', None))
            self.assertEqual(asked, ['pm', 'gone'])
            now[0] = serve.TOPLEVEL_MISS_S
            answers['gone'] = 'found-later'
            self.assertEqual((tops('pm'), tops('gone')), ('pmrepo', 'found-later'))
            now[0] = 10 * serve.TOPLEVEL_MISS_S
            self.assertEqual((tops('pm'), tops('gone')), ('pmrepo', 'found-later'))
        self.assertEqual(asked, ['pm', 'gone', 'gone'])

    def test_a_pm_data_key_in_the_machine_config_is_never_the_sources_pm_data(self):
        # pm_data is derived from the PM-repo pointer (kitlib.pmdata.machine_settings), so a key
        # planted in the file cannot change it; the file itself is refused by hub_settings.load,
        # which `hub.py serve` reads before machine_sources.
        pm = os.path.dirname(self.config({'pm_data': {'path': os.path.join(self.home, 'pm')}}))
        self.config('{"pm_data": 5}')
        self.assertEqual(serve.machine_sources(self.home).pm_data,
                         {'path': pm, 'branch': 'main', 'remote': 'origin'})
        with self.assertRaises(ConfigError) as cm:
            hub_settings.load(home=self.home)
        self.assertIn('pm_data is derived', str(cm.exception))


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

    def test_ask_names_the_via_it_is_given_and_a_client_passes_its_own(self):
        hub = self.build()
        hub.start()
        status, obj = serve.ask(hub.port, 'POST', BASE + '/todos', self.owner, {'title': 'a'},
                                via=serve.VIA_MCP)
        self.assertEqual(status, 200, obj)
        c = client.Client(hub.port, tokens=self.tokens, via=serve.VIA_MCP)
        c.post('/inbox', client.AGENT, {'text': 'b'})
        self.assertEqual([d.get('via') for d in hub.store.docs('todos').values()],
                         [serve.VIA_MCP])
        self.assertEqual([d.get('via') for d in hub.store.docs('inbox').values()],
                         [serve.VIA_MCP])
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

    def test_status_lines_say_the_code_and_the_jobs_before_the_tunnel(self):
        from kitlib.hubsite import codestale
        base = {'revision': 2, 'git': None, 'backlog': None, 'tunnel': 'on'}
        health = dict(base, code=codestale.state(False), jobs=None)
        self.assertEqual(serve.status_lines(health, NOW)[3:],
                         ['code: current', 'jobs: none', 'tunnel: on'])
        health['code'] = codestale.state(True)
        self.assertEqual(serve.status_lines(health, NOW)[3], 'code: stale: ' + codestale.text())
        long_note = 'n' * 100
        health['jobs'] = {
            'zeta': {'runs': 4, 'failures': 2, 'running': True, 'note': 'old note',
                     'error': 'RefreshFailed: exited 1\nat https://user:pw@example.invalid/x'},
            'alpha': {'runs': 1, 'failures': 0, 'running': False, 'note': long_note,
                      'error': None},
            'beta': {'runs': 0, 'failures': 0, 'running': False, 'note': None, 'error': None}}
        self.assertEqual(serve.status_lines(health, NOW)[4], 'jobs: ' + '; '.join([
            'alpha runs 1, failures 0: ' + 'n' * (serve.JOB_NOTE_MAX - 3) + '...',
            'beta runs 0, failures 0',
            'zeta runs 4, failures 2, running: RefreshFailed: exited 1 at '
            'https://example.invalid/x']))
        # an older server's answer, without the fields: neither line
        self.assertEqual(serve.status_lines(base, NOW),
                         ['revision 2', 'git: no answer', 'backlog: no source on this server',
                          'tunnel: on'])


class _Keys(object):
    """A key source holding the test key: get(kid), and of itself what a test tells it to say
    (`ready`, `error`), as tunnel.Gate.keys asks."""

    def __init__(self, ready=True, error=''):
        self._keys = {ak.KEY_A.kid: ak.jwk(ak.KEY_A)}
        self.said = ready
        self.error = error

    def get(self, kid):
        return self._keys.get(kid)

    def ready(self):
        return self.said


class TunnelCase(ServeCase):
    """The tunnel listener in the server's life: built beside the local one, started with it and
    closed with it. What that listener lets in is tests.hubsite.test_tunnel's."""

    def setUp(self):
        ServeCase.setUp(self)
        self.keyed = []         # the team names a key source was made for
        self.tunnelled = self.settings._replace(
            tunnel_port=0, access=hub_settings.Access(ak.TEAM, ak.AUD, (ak.EMAIL,)))

    def keys(self, team):
        # a source that says it has its keys: one with no ready() (a plain dict) is never `on`
        self.keyed.append(team)
        return _Keys()

    def build(self, settings=None, **src):
        src.setdefault('access_keys', self.keys)
        src.setdefault('access_now', lambda: ak.NOW)
        return ServeCase.build(self, settings or self.tunnelled, **src)

    def through(self, port, path):
        """GET `path` on the tunnel port `port` as the tunnel program hands it on: the public
        Host and a valid Access token."""
        return send(port, 'GET', path, [(tunnel.HEADER, ak.token(ak.KEY_A))],
                    host='hub.example.invalid')

    def taken_port(self):
        """A loopback port another program listens on."""
        other = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(other.close)
        other.bind((httpd.LOOPBACK, 0))
        other.listen(1)
        return other.getsockname()[1]

    def test_a_server_with_no_access_settings_binds_no_second_port_and_says_nothing(self):
        # the tunnel port is another program's: a server that tried to bind it would say so
        hub = self.build(self.settings._replace(tunnel_port=self.taken_port()))
        self.assertEqual((len(hub.listeners), hub.notes), (1, []))
        self.assertEqual(hub.tunnel, 'off: ' + tunnel.UNSET)
        self.assertEqual(self.keyed, [])
        hub.start()
        self.assertEqual(httpfix.http_threads(), ['hub-http-%d' % hub.port])
        r = get(hub.port, HEALTH, self.owner)
        self.assertEqual((r.status, r.json()['tunnel']), (200, 'off: ' + tunnel.UNSET))
        self.assertIs(hub.close(), True)
        self.assertEqual(self.lines, [('access', '127.0.0.1 GET %s 200 owner' % HEALTH)])

    def test_a_taken_tunnel_port_is_a_note_and_the_local_listener_still_serves(self):
        port = self.taken_port()
        hub = self.build(self.tunnelled._replace(tunnel_port=port))
        self.assertEqual(len(hub.listeners), 1)
        self.assertEqual(len(hub.notes), 1, hub.notes)
        self.assertTrue(hub.notes[0].startswith(
            'the tunnel listener is off: cannot listen on 127.0.0.1:%d: ' % port), hub.notes[0])
        self.assertEqual(hub.tunnel, 'off: ' + serve.TUNNEL_BIND)
        hub.start()
        # health says it in fixed words, to the agent as to the owner
        for token in (self.owner, self.agent):
            r = get(hub.port, HEALTH, token)
            self.assertEqual((r.status, r.json()['tunnel']),
                             (200, 'off: ' + serve.TUNNEL_BIND))
        self.assertEqual(post(hub.port, BASE + '/todos', {'title': 't'}, self.owner).status, 200)
        self.assertIs(hub.close(), True)
        self.assertFalse(self.locked())
        self.assertEqual(self.errors(), [])

    def test_both_doors_are_closed_before_the_last_pass_and_no_socket_or_thread_is_left(self):
        hub = self.build(sync=self.sync_factory())
        self.assertEqual(len(hub.listeners), 2)
        self.assertEqual(self.keyed, [ak.TEAM])
        self.assertEqual(httpfix.http_threads(), [])        # built: nothing is started
        self.assertIs(hub.start(), True)
        local, tunnelled = hub.listeners
        self.assertEqual(httpfix.http_threads(),
                         sorted('hub-http-%d' % lis.port for lis in hub.listeners))
        self.assertEqual(self.through(tunnelled.port, HEALTH).status, 200)
        self.assertEqual(post(local.port, BASE + '/todos', {'title': 't'}, self.owner).status,
                         200)
        written = hub.store.revision
        self.assertIs(hub.close(), True)
        # the last pass ran with both doors closed and the store still this server's, whole
        self.assertEqual(self.sync.last,
                         {'locked': True, 'doors': [True, True], 'writes': written})
        for lis in hub.listeners:
            self.assertEqual(lis._server.socket.fileno(), -1, 'a listening socket was left open')
        self.assertEqual(httpfix.http_threads(), [])
        self.assertFalse(self.locked())
        self.assertEqual(self.errors(), [])
        # a second close does nothing more
        self.assertIs(hub.close(), True)
        self.assertEqual(self.calls, ['sync.start', 'sync.stop', 'sync.sync_once'])

    def test_a_close_ends_a_long_wait_on_the_tunnel_port_and_leaves_no_thread(self):
        hub = self.build()
        hub.start()
        tunnelled = hub.listeners[1]
        got = []
        path = '%s/wait?since=%d&timeout=%d' % (BASE, hub.store.revision, TIMEOUT)
        client = threading.Thread(
            target=lambda: got.append(self.through(tunnelled.port, path)),
            name='test-waiter', daemon=True)
        client.start()
        self.addCleanup(client.join, TIMEOUT)
        self.assertTrue(tunnelled.await_waits(1, TIMEOUT), 'the wait never arrived')
        self.assertIs(hub.close(TIMEOUT), True)
        client.join(TIMEOUT)
        self.assertEqual(len(got), 1, 'the wait was never answered')
        self.assertIn(got[0].status, (200, 503))
        self.assertEqual(httpfix.http_threads(), [])
        self.assertFalse(self.locked())
        self.assertEqual(self.errors(), [])

    def test_a_start_up_that_fails_after_the_tunnel_port_was_bound_releases_it(self):
        made = []
        real = tunnel.start

        def start(*args, **kw):
            made.append(real(*args, **kw))
            return made[-1]

        with mock.patch.object(tunnel, 'start', start), \
                mock.patch.object(serve, 'Hub', side_effect=RuntimeError('no hub')):
            with self.assertRaises(RuntimeError):
                serve.build(self.tunnelled,
                            serve.Sources(tokens=self.tokens, access_keys=self.keys),
                            log=self.log)
        self.assertEqual(len(made), 1)
        self.assertEqual(made[0]._server.socket.fileno(), -1, 'the tunnel port was left bound')
        self.assertFalse(self.locked())
        self.assertEqual(httpfix.http_threads(), [])

    def test_the_tunnel_is_told_a_failing_sync_in_one_word_and_the_pc_its_reason(self):
        # was test_the_tunnels_owner_is_given_the_reason_of_a_failing_sync_without_credentials:
        # health through the tunnel is fixed words now, so git's reason stays at the PC
        hub = self.build(sync=self.sync_factory())
        self.sync.state = {'failed': 'push', 'detail': WHY}
        hub.start()
        local, tunnelled = hub.listeners
        r = self.through(tunnelled.port, HEALTH)
        self.assertEqual(r.status, 200, r.body)
        self.assertEqual(r.json(), {'ok': True, 'version': 'v1', 'store': 'ok', 'git': 'failed',
                                    'tunnel': 'on'})
        text = r.body.decode('utf-8')
        for word in CREDENTIALS + ('git.example.test', 'Could not resolve', 'push',
                                   os.path.basename(self.root)):
            self.assertNotIn(word, text)
        # the owner at the local listener is given the reason as before, without credentials
        r = get(local.port, HEALTH, self.owner)
        git = r.json()['git']
        self.assertEqual((git['failed'], git['detail']), ('push', serve.scrub(WHY)))
        self.assertEqual(r.json()['store'], os.path.abspath(self.store_dir))
        for word in CREDENTIALS:
            self.assertNotIn(word, r.body.decode('utf-8'))

    def test_a_close_refuses_the_request_held_on_a_key_fetch_and_stays_clean(self):
        entered, release = threading.Event(), threading.Event()

        def fetch(team, timeout):
            entered.set()
            release.wait(TIMEOUT)               # a key source that hangs past every deadline
            raise OSError('the network is down')

        hub = self.build(sync=self.sync_factory(),
                         access_keys=lambda team: access.KeyCache(team, fetch=fetch))
        hub.start()
        local, tunnelled = hub.listeners
        self.assertEqual(post(local.port, BASE + '/todos', {'title': 't'}, self.owner).status,
                         200)
        written = hub.store.revision
        got = []
        client = threading.Thread(target=lambda: got.append(self.through(tunnelled.port, HEALTH)),
                                  name='test-held', daemon=True)
        self.addCleanup(client.join, TIMEOUT)
        self.addCleanup(release.set)
        client.start()
        self.assertTrue(entered.wait(TIMEOUT), 'the token asked for no key')
        self.assertEqual(got, [])
        # well inside the time a held request would wait for its keys (access.FETCH_DEADLINE_S)
        self.assertLess(5, access.FETCH_DEADLINE_S)
        self.assertIs(hub.close(5), True)
        self.assertFalse(release.is_set())      # the fetch is still out: nobody waited for it
        # the last pass ran, with both doors closed and the store still this server's
        self.assertEqual(self.calls, ['sync.start', 'sync.stop', 'sync.sync_once'])
        self.assertEqual(self.sync.last,
                         {'locked': True, 'doors': [True, True], 'writes': written})
        # the held request was answered, with the one refusal
        client.join(TIMEOUT)
        self.assertEqual([r.status for r in got], [403])
        self.assertEqual(got[0].json(), {'ok': False, 'error': {
            'code': 'forbidden', 'field': 'access', 'message': tunnel.REFUSED}})
        self.assertIn(('note', tunnel.REFUSAL_LINE % tunnel.CLOSING), self.lines)
        self.assertEqual(self.errors(), [])
        self.assertEqual(httpfix.http_threads(), [])
        self.assertFalse(self.locked())
        for lis in hub.listeners:
            self.assertEqual(lis._server.socket.fileno(), -1, 'a listening socket was left open')
        # the one thread a stop cannot end is the fetch itself: it ends when its fetcher does
        release.set()
        for thread in threading.enumerate():
            if thread.name == 'hub-access-keys':
                thread.join(TIMEOUT)
                self.assertFalse(thread.is_alive(), 'the key fetch thread never ended')
        # was assertEqual(hub.tunnel, serve.TUNNEL_BOUND): a closed hub says it is closed
        self.assertEqual(hub.tunnel, 'closed')

    def test_the_strong_pairing_route_is_served_on_the_tunnel_port_alone(self):
        hub = self.build()
        hub.start()
        local, tunnelled = hub.listeners
        r = send(tunnelled.port, 'POST', httpd.STRONG_PAIR_ROUTE,
                 [(tunnel.HEADER, ak.token(ak.KEY_A)), ('Content-Type', httpd.JSON),
                  ('Origin', 'https://hub.example.invalid')], b'', host='hub.example.invalid')
        self.assertEqual(r.status, 200, r.body)
        self.assertTrue(r.json()['path'].startswith(httpd.PAIR_PATH + '?code='), r.body)
        self.assertEqual(post(local.port, httpd.STRONG_PAIR_ROUTE, None, self.owner).status, 404)

    def test_a_closed_hub_says_closed_of_its_tunnel_and_a_running_one_never_does(self):
        hub = self.build()
        self.assertEqual(hub.tunnel, 'on')          # built, not started: the gate has its keys
        hub.start()
        local, tunnelled = hub.listeners
        self.assertEqual(hub.tunnel, 'on')
        self.assertEqual(get(local.port, HEALTH, self.owner).json()['tunnel'], 'on')
        self.assertEqual(self.through(tunnelled.port, HEALTH).json()['tunnel'], 'on')
        self.assertIs(hub.close(), True)
        self.assertEqual(hub.tunnel, 'closed')
        self.assertEqual(serve.TUNNEL_CLOSED, 'closed')
        self.assertIs(hub.close(), True)            # a second close: the same word
        self.assertEqual(hub.tunnel, 'closed')
        # a source with no keys yet: `bound` while it runs, `closed` after
        bound = self.build(access_keys=lambda team: _Keys(ready=False, error='keys-fetch'))
        self.assertEqual(bound.tunnel, 'bound, keys not fetched (keys-fetch)')
        bound.start()
        self.assertEqual(bound.tunnel, 'bound, keys not fetched (keys-fetch)')
        self.assertIs(bound.close(), True)
        self.assertEqual(bound.tunnel, 'closed')

    def test_a_closed_hub_with_no_tunnel_listener_still_says_why_it_had_none(self):
        hub = self.build(self.settings)             # no access settings: no second listener
        said = 'off: ' + tunnel.UNSET
        self.assertEqual(hub.tunnel, said)
        hub.start()
        self.assertIs(hub.close(), True)
        self.assertEqual(hub.tunnel, said)
        port = self.taken_port()
        hub = self.build(self.tunnelled._replace(tunnel_port=port))
        self.assertIs(hub.close(), True)
        self.assertEqual(hub.tunnel, 'off: ' + serve.TUNNEL_BIND)

    def test_status_lines_print_what_a_running_servers_health_says_of_the_tunnel(self):
        # the health answer of a real hub, not a dict written here: a wrong health text for a
        # state makes its line wrong
        def lines(hub, token):
            r = get(hub.port, HEALTH, token)
            self.assertEqual(r.status, 200, r.body)
            return serve.status_lines(r.json(), NOW)

        taken = self.taken_port()
        for src, settings, line in (
                ({}, None, 'tunnel: on'),
                ({'access_keys': lambda team: _Keys(ready=False)}, None,
                 'tunnel: bound, keys not fetched'),
                ({'access_keys': lambda team: _Keys(ready=False, error='keys-fetch')}, None,
                 'tunnel: bound, keys not fetched (keys-fetch)'),
                # a reason that is not one of access.REASONS is never printed
                ({'access_keys': lambda team: _Keys(ready=False, error='a made-up reason')},
                 None, 'tunnel: bound, keys not fetched'),
                # no ready() at all: never `on`
                ({'access_keys': lambda team: {ak.KEY_A.kid: ak.jwk(ak.KEY_A)}}, None,
                 'tunnel: bound, keys not fetched'),
                ({}, self.settings, 'tunnel: off: ' + tunnel.UNSET),
                ({}, self.tunnelled._replace(tunnel_port=taken),
                 'tunnel: off: ' + serve.TUNNEL_BIND)):
            with self.subTest(line=line):
                hub = self.build(settings, **src)
                hub.start()
                for token in (self.owner, self.agent):
                    said = lines(hub, token)
                    self.assertEqual([s for s in said if s.startswith('tunnel')], [line], said)
                    # after the backlog line, the last line of a server with nothing missing
                    self.assertEqual(said[-1], line, said)
                    # a real server's `code` and `jobs` (none built here) read as these lines
                    self.assertEqual(said[-3:-1], ['code: current', 'jobs: none'], said)
                self.assertIs(hub.close(), True)
        self.assertEqual(self.errors(), [])

    def test_status_lines_say_the_tunnel_after_the_backlog_and_before_what_is_unavailable(self):
        health = {'revision': 3, 'git': {'running': False, 'off': serve.SYNC_OFF},
                  'backlog': None, 'backlog_error': 'the list could not be read',
                  'tunnel': 'on', 'unavailable': {'usage': 'KeyError'}}
        self.assertEqual(serve.status_lines(health, NOW), [
            'revision 3', 'git: ' + serve.SYNC_OFF, 'backlog: no source on this server',
            'backlog: the list could not be read', 'tunnel: on',
            'unavailable: usage (KeyError)'])
        health['tunnel'] = 'off: ' + tunnel.NO_AUD
        self.assertEqual(serve.status_lines(health, NOW)[4], 'tunnel: off: ' + tunnel.NO_AUD)
        # bound, and the Access keys not fetched: the server's words as they are
        why = serve.TUNNEL_BOUND_WHY % 'keys-fetch'
        self.assertEqual((serve.TUNNEL_ON, serve.TUNNEL_BOUND, why),
                         ('on', 'bound, keys not fetched', 'bound, keys not fetched (keys-fetch)'))
        for said in (serve.TUNNEL_BOUND, why):
            health['tunnel'] = said
            self.assertEqual(serve.status_lines(health, NOW)[4], 'tunnel: ' + said)
        # what is printed never holds the user part of a URL, whatever a server answered
        health['tunnel'] = 'off: https://someone:s3cret@hub.example.invalid'
        self.assertEqual(serve.status_lines(health, NOW)[4],
                         'tunnel: off: https://hub.example.invalid')
        # a server that says nothing of a tunnel (an earlier one), or nothing usable: no line
        for value in (None, '', 7, ['on'], {'on': True}):
            health['tunnel'] = value
            self.assertEqual(len(serve.status_lines(health, NOW)), 5, value)
        del health['tunnel']
        self.assertEqual(len(serve.status_lines(health, NOW)), 5)


if __name__ == '__main__':
    unittest.main()
