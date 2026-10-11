"""kitlib.hubsite.serve: the server wired from its parts. Every hub here serves a temp store on
port 0 from stand-in sources: no machine config, no real repository, no git, no network past the
loopback address. Every hub a test builds is closed before the test ends, and no listener thread
may outlive its test."""
import copy
import datetime
import json
import os
import shutil
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
    still locked and whether every door was closed when the last pass ran."""

    def __init__(self, case, fail=None):
        self.case = case
        self.fail = fail
        self.last = None

    def start(self):
        self.case.calls.append('sync.start')
        return True

    def stop(self, timeout=None):
        self.case.calls.append('sync.stop')
        if self.fail == 'stop':
            raise RuntimeError(SECRET)
        return True

    def sync_once(self):
        self.case.calls.append('sync.sync_once')
        self.last = {'locked': self.case.locked(),
                     'doors': [lis.closing for lis in self.case.hub.listeners],
                     'writes': self.case.hub.store.revision}
        return self.status()

    def status(self):
        return {'running': True, 'branch': 'data', 'remote': 'origin', 'failed': '',
                'detail': SECRET, 'pending_commit': False, 'pending_push': True,
                'diverged': False}


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
