"""tools/hub.py: its arguments and exit codes. The machine config, the token files and the store
are all under a temp home; a server is on port 0 or is one this test built; no browser is
opened."""
import _thread
import contextlib
import datetime
import importlib.util
import io
import json
import os
import shutil
import signal
import socket
import sys
import tempfile
import threading
import unittest
from unittest import mock

from kitlib.hubsite import approvals, auth, httpd, schema, serve, todosteps, tunnel
from kitlib.hubsite import settings as hub_settings
from kitlib.hubsite.store import Store, StoreBusy
from tests.home import machine_config, pm_repo
from tests.hubsite import httpfix

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load():
    spec = importlib.util.spec_from_file_location('hub_cli_under_test',
                                                  os.path.join(ROOT, 'tools', 'hub.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


HUB = load()
# a ledger id, built so no file holds a ruling id (tests/scrub)
RULING = 'R-' + '0007'


class CliCase(unittest.TestCase):
    def setUp(self):
        self.addCleanup(lambda: self.assertEqual(httpfix.http_threads(), [],
                                                 'a listener thread outlived its test'))
        self.home = tempfile.mkdtemp(prefix='kit-hubcli-')
        self.addCleanup(shutil.rmtree, self.home, True)
        env = mock.patch.dict(os.environ, {'USERPROFILE': self.home, 'HOME': self.home})
        env.start()
        self.addCleanup(env.stop)
        # The fenced PM repository, with no kit-pm-data.json record: no pm_data, as a machine
        # config without the key had before pm_data was derived (a test's pm_data.path makes one).
        pm_repo(self, record=False)
        self.store_dir = os.path.join(self.home, 'scratch-store')

    @property
    def tokens(self):
        """The hub's credential folder for the machine config as it stands now (auth.folder)."""
        return auth.folder(self.home)

    def run_cli(self, *argv, **kw):
        """(exit code, stdout, stderr) of hub.py with these arguments."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = HUB.main(list(argv), **kw)
            except SystemExit as e:
                code = e.code
        self.assertNotIn('Traceback', err.getvalue())
        return code, out.getvalue(), err.getvalue()

    def config(self, obj):
        """The machine config `obj`, in the fenced PM repository's two files: their folder."""
        return machine_config(self.home, obj, case=self)

    def dead_port(self):
        """A loopback port nothing listens on: bound here, never listening, until the test ends."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(s.close)
        s.bind((httpd.LOOPBACK, 0))
        return s.getsockname()[1]

    def server(self, tokens=None, port=0):
        """A started hub on a free port (or `port`), with the temp home's token files unless
        given others. Only a hub given its port keeps a sessions file (serve.build)."""
        cfg = hub_settings.settings(None)._replace(port=port, store=self.store_dir)
        hub = serve.build(cfg, serve.Sources(tokens=tokens or self.tokens,
                                             static_dir=os.path.join(self.home, 'web')))
        self.addCleanup(hub.close)
        self.assertTrue(hub.start())
        return hub

    def locked(self):
        try:
            Store.open(self.store_dir).close()
        except StoreBusy:
            return True
        return False


class UsageCase(CliCase):
    def test_no_verb_and_an_unknown_verb_are_usage_errors(self):
        code, out, err = self.run_cli()
        self.assertEqual((code, out), (2, ''))
        self.assertIn('USAGE: give a verb: serve, status, setup, open, unpair, stop, restart, '
                      'questions, answer, todos, stories, to-pm, inbox, pages, approvals, '
                      'handoffs, skill-edit, pending, wait or validate\n', err)
        self.assertEqual(sorted(HUB.VERBS), sorted(HUB.VERB_NAMES))     # every verb it has
        self.assertEqual(self.run_cli('launch')[0], 2)
        self.assertEqual(self.run_cli('serve', '--bind', '0.0.0.0')[0], 2)
        self.assertEqual(self.run_cli('serve', '--host', '0.0.0.0')[0], 2)

    def test_a_port_that_is_no_port_is_a_usage_error(self):
        for argv in (('serve', '--port', 'x'), ('serve', '--port', '70000'),
                     ('serve', '--port', '-1'), ('status', '--port', '0'),
                     ('open', '--port', '65536'), ('status', '--port', ''),
                     ('unpair', '--port', '0'), ('unpair', '--all'),
                     ('stop', '--port', '0'), ('restart', '--port', 'x'),
                     ('stop', '--timeout', '-1'), ('restart', '--timeout', 'soon'),
                     ('stop', '--all')):
            code, out, err = self.run_cli(*argv)
            self.assertEqual((code, out), (2, ''), argv)
            self.assertIn('USAGE: ', err)

    def test_a_machine_config_that_cannot_be_used_is_exit_2(self):
        self.config({'hub': {'port': 'eighty'}})
        for argv in (('serve', '--port', '0'), ('status',), ('setup',), ('open',), ('unpair',),
                     ('stop',), ('restart',)):
            code, out, err = self.run_cli(*argv)
            self.assertEqual((code, out), (2, ''), argv)
            self.assertIn('machine config: ', err)
        self.assertFalse(os.path.exists(self.tokens), 'setup made tokens on a bad config')


class ServeCase(CliCase):
    def test_serve_starts_on_a_scratch_store_and_closes_cleanly(self):
        stop = threading.Event()
        stop.set()
        code, out, err = self.run_cli('serve', '--store', self.store_dir, '--port', '0', stop=stop)
        self.assertEqual((code, err), (0, ''))
        lines = out.splitlines()
        # the start line, and one closing line: the owner sees that the server ended on purpose
        self.assertEqual(len(lines), 2, out)
        self.assertEqual(lines[1], HUB.CLOSED)
        self.assertEqual(HUB.CLOSED, 'hub closed: the server ended cleanly and the store lock '
                                     'is released')
        start = 'hub serving %s on http://127.0.0.1:' % os.path.abspath(self.store_dir)
        self.assertTrue(lines[0].startswith(start), lines[0])
        self.assertTrue(lines[0].endswith('to pair a browser run: ' + HUB.OPEN), lines[0])
        self.assertIn(' open', HUB.OPEN)
        port = int(lines[0][len(start):].split()[0])
        self.assertNotEqual(port, 0)
        self.assertFalse(self.locked(), 'the store lock outlived the server')
        self.assertTrue(auth.read_token(self.tokens, auth.OWNER))
        self.assertTrue(auth.read_token(self.tokens, auth.AGENT))
        self.assertEqual(self.run_cli('status', '--port', str(port))[0], 3)

    def test_serve_with_no_store_folder_is_refused(self):
        code, out, err = self.run_cli('serve', '--port', '0')
        self.assertEqual((code, out), (1, ''))
        self.assertIn('REFUSED: no store folder', err)

    def test_a_second_serve_on_the_same_store_is_refused_not_left_waiting(self):
        held = Store.open(self.store_dir)
        self.addCleanup(held.close)
        code, out, err = self.run_cli('serve', '--store', self.store_dir, '--port', '0')
        self.assertEqual((code, out), (1, ''))
        self.assertIn('REFUSED: ', err)
        self.assertIn('another hub server has this store open', err)
        self.assertTrue(self.locked(), 'the refused start took the lock from its holder')

    def test_serve_on_a_taken_port_is_refused(self):
        hub = self.server()
        other = os.path.join(self.home, 'other-store')
        code, out, err = self.run_cli('serve', '--store', other, '--port', str(hub.port))
        self.assertEqual((code, out), (1, ''))
        self.assertIn('REFUSED: ', err)
        self.assertIn(str(hub.port), err)

    def test_serve_with_a_token_file_that_cannot_serve_is_refused(self):
        os.makedirs(self.tokens)
        bad = os.path.join(self.tokens, auth.TOKEN_FILES[auth.AGENT])
        with open(bad, 'w', encoding='utf-8') as f:
            f.write('short\n')
        code, out, err = self.run_cli('serve', '--store', self.store_dir, '--port', '0')
        self.assertEqual((code, out), (1, ''))
        self.assertIn('REFUSED: ', err)
        self.assertIn(bad, err)
        self.assertFalse(self.locked())

    def test_an_empty_store_value_is_a_usage_error_and_never_the_configured_store(self):
        live = os.path.join(self.home, 'live-store')
        self.config({'hub': {'store': live, 'push': True}})
        built = []
        stop = threading.Event()
        stop.set()
        with mock.patch.object(HUB.serve, 'build', lambda *a, **kw: built.append((a, kw))):
            for argv in (('--store', ''), ('--store', '   '), ('--store', '\t'), ('--store=',)):
                code, out, err = self.run_cli('serve', '--port', '0', *argv, stop=stop)
                self.assertEqual((code, out), (2, ''), argv)
                self.assertIn('USAGE: ', err)
                self.assertIn('--store', err)
        self.assertEqual(built, [], 'a server was built for an empty --store')
        self.assertFalse(os.path.exists(live), 'the configured store was opened')

    def test_a_store_run_has_git_sync_off_whatever_the_machine_config_says(self):
        live = os.path.join(self.home, 'live-store')
        self.config({'hub': {'store': live, 'push': True, 'commit_every_s': 1}})
        real, seen, syncs = HUB.serve.build, [], []

        def build(settings, sources, log=None):
            hub = real(settings, sources, log=log)
            seen.append((settings, sources, hub))
            return hub

        class Sync(object):             # stands where the real git sync is taken from
            def __init__(self, *args, **kw):
                syncs.append((args, kw))
                raise AssertionError('a --store run made a git sync')

        stop = threading.Event()
        stop.set()
        with mock.patch.object(HUB.serve, 'build', build), \
                mock.patch.object(HUB.serve.gitsync, 'GitSync', Sync):
            code, out, err = self.run_cli('serve', '--store', self.store_dir, '--port', '0',
                                          stop=stop)
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(len(seen), 1)
        settings, sources, hub = seen[0]
        # nothing commits and nothing pushes: there is no sync at all, and push is off
        self.assertEqual((settings.store, settings.push),
                         (os.path.abspath(self.store_dir), False))
        self.assertIsNone(sources.sync)
        self.assertIsNone(hub.sync)
        self.assertEqual(syncs, [])
        self.assertEqual(hub.views.health()['git'], {'running': False, 'off': serve.SYNC_OFF})
        self.assertFalse(os.path.exists(live), 'the configured store was opened')
        self.assertFalse(os.path.exists(os.path.join(self.store_dir, '.git')))

    def test_an_unclean_close_is_exit_1_with_the_step_named_on_the_line_above(self):
        real = HUB.serve.build

        def build(settings, sources, log=None):
            hub = real(settings, sources, log=log)
            lis = hub.listeners[0]
            join = lis.join
            lis.join = lambda timeout=None: join(timeout) and False     # ended, but says not
            return hub

        stop = threading.Event()
        stop.set()
        with mock.patch.object(HUB.serve, 'build', build):
            code, out, err = self.run_cli('serve', '--store', self.store_dir, '--port', '0',
                                          stop=stop)
        self.assertEqual(code, 1)
        self.assertEqual(err.splitlines(), [
            'ERROR: closing: the handler threads did not end within %g s' % serve.CLOSE_TIMEOUT_S,
            'the hub server did not close cleanly: see the lines above'])
        self.assertFalse(self.locked())
        # no closing line: that one is for a close that was clean
        self.assertEqual(len(out.splitlines()), 1, out)
        self.assertNotIn('hub closed', out)

    def synced(self, sync):
        """The machine config of a server whose store is the configured one, so git sync is on;
        `sync` stands where the real git sync is taken from. -> a patch to serve under. Push
        notifications are off without a word (PushCase covers them)."""
        self.store_dir = os.path.join(self.home, 'live-store')
        self.config({'hub': {'store': self.store_dir}})
        quiet = mock.patch.object(HUB, '_push_delivery', lambda: (None, None))
        quiet.start()
        self.addCleanup(quiet.stop)
        return mock.patch.object(HUB.serve.gitsync, 'GitSync', lambda *args, **kw: sync)

    def test_a_clean_close_of_a_synced_server_says_the_wait_for_git_then_that_it_closed(self):
        seen = []

        class Sync(object):
            def start(self):
                return True

            def stop(self, timeout=None):       # what the console held when the wait began
                seen.append((sys.stdout.getvalue(), sys.stderr.getvalue()))
                return True

            def sync_once(self):
                return self.status()

            def status(self):
                return {'running': True, 'failed': '', 'detail': ''}

        stop = threading.Event()
        stop.set()
        with self.synced(Sync()):
            code, out, err = self.run_cli('serve', '--port', '0', stop=stop)
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertEqual(len(lines), 2, out)
        self.assertTrue(lines[0].startswith('hub serving %s on ' % self.store_dir), lines[0])
        self.assertEqual(lines[1], HUB.CLOSED)
        self.assertEqual(err.splitlines(), ['NOTE: ' + serve.LAST_PASS])
        # the wait for git was on the console before it began, and the closing line after it
        self.assertEqual(seen, [(lines[0] + '\n', 'NOTE: %s\n' % serve.LAST_PASS)])
        self.assertIn('finishing a sync pass, waiting for git', err)
        self.assertFalse(self.locked(), 'the store lock outlived the server')

    def test_a_second_ctrl_c_ends_the_wait_for_git_with_what_to_check_and_exit_1(self):
        if threading.current_thread() is not threading.main_thread():
            self.skipTest('a Ctrl+C reaches the main thread only')
        was = signal.signal(signal.SIGINT, signal.default_int_handler)
        if was is not None:
            self.addCleanup(signal.signal, signal.SIGINT, was)
        release = threading.Event()

        class Sync(object):             # its last pass hangs in git; the owner presses again
            def start(self):
                return True

            def stop(self, timeout=None):
                return True

            def sync_once(self):
                _thread.interrupt_main()
                release.wait(httpfix.TIMEOUT)
                return self.status()

            def status(self):
                return {'running': True, 'failed': '', 'detail': ''}

        def ended():
            waits = [t for t in threading.enumerate() if t.name == serve.GIT_WAIT_THREAD]
            for t in waits:
                t.join(httpfix.TIMEOUT)
            self.assertEqual([t for t in waits if t.is_alive()], [],
                             'a wait for git outlived its test')

        self.addCleanup(ended)
        self.addCleanup(release.set)
        stop = threading.Event()
        stop.set()
        with self.synced(Sync()), mock.patch.object(serve, 'POLL_S', 0.01):
            try:
                code, out, err = self.run_cli('serve', '--port', '0', stop=stop)
            except KeyboardInterrupt:
                self.fail('the second Ctrl+C left hub.py as a KeyboardInterrupt')
        self.assertEqual(code, 1)
        self.assertEqual(err.splitlines(), [
            'NOTE: ' + serve.LAST_PASS,
            'ERROR: ' + serve.CUT_SHORT,
            'the hub server did not close cleanly: see the lines above'])
        self.assertIn('a git command may have been left half-way', err)
        self.assertIn('index.lock', err)
        self.assertEqual(len(out.splitlines()), 1, out)
        self.assertNotIn('hub closed', out)
        self.assertFalse(self.locked(), 'the store lock outlived the server')

    def ctrl_c(self):
        """Make a Ctrl+C a KeyboardInterrupt in this, the main thread, whatever the runner set
        for it."""
        if threading.current_thread() is not threading.main_thread():
            self.skipTest('a Ctrl+C reaches the main thread only')
        was = signal.signal(signal.SIGINT, signal.default_int_handler)
        if was is not None:
            self.addCleanup(signal.signal, signal.SIGINT, was)

    def test_the_first_ctrl_c_is_a_clean_close_and_exit_0(self):
        self.ctrl_c()
        real, looks, stop = HUB.serve.build, [], threading.Event()

        def build(settings, sources, log=None):
            hub = real(settings, sources, log=log)

            def watch():                # each turn of the serving wait: the owner presses once
                looks.append(True)
                if len(looks) == 1:
                    _thread.interrupt_main()
                else:
                    stop.set()          # the Ctrl+C did not end the wait: the test fails below

            hub.watch = watch
            return hub

        with mock.patch.object(HUB.serve, 'build', build), \
                mock.patch.object(serve, 'POLL_S', 0.01):
            try:
                code, out, err = self.run_cli('serve', '--store', self.store_dir, '--port', '0',
                                              stop=stop)
            except KeyboardInterrupt:
                self.fail('the first Ctrl+C left hub.py as a KeyboardInterrupt')
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(looks, [True], 'the server went on serving after the Ctrl+C')
        lines = out.splitlines()
        self.assertEqual(len(lines), 2, out)
        self.assertEqual(lines[1], HUB.CLOSED)
        self.assertFalse(self.locked(), 'the store lock outlived the server')

    def cut_in(self, step):
        """A patch to serve under: the built server's close is left by a Ctrl+C in `step` of its
        listener ('drain': the requests in flight; 'join': the handler threads, after the store
        closed)."""
        real = HUB.serve.build

        def build(settings, sources, log=None):
            hub = real(settings, sources, log=log)
            lis = hub.listeners[0]
            was = getattr(lis, step)

            def cut(timeout=None):
                was(timeout)
                raise KeyboardInterrupt()

            setattr(lis, step, cut)
            return hub

        return mock.patch.object(HUB.serve, 'build', build)

    def test_a_ctrl_c_that_leaves_the_close_itself_says_so_and_is_exit_1(self):
        stop = threading.Event()
        stop.set()
        # no git sync to wait for: the second Ctrl+C ends the close, and the store was released
        with self.cut_in('drain'):
            code, out, err = self.run_cli('serve', '--store', self.store_dir, '--port', '0',
                                          stop=stop)
        self.assertEqual(code, 1)
        self.assertEqual(err.splitlines(), [HUB.CLOSE_CUT])
        self.assertTrue(HUB.CLOSE_CUT.startswith('interrupted during the close: the hub server '
                                                 'did not close cleanly'))
        self.assertIn('The store lock is released', HUB.CLOSE_CUT)
        self.assertNotIn('before the server closed', err)
        self.assertEqual(len(out.splitlines()), 1, out)
        self.assertNotIn('hub closed', out)
        self.assertFalse(self.locked(), 'the store lock outlived the server')

    def test_a_ctrl_c_in_another_verb_is_exit_1(self):
        def pressed(*args, **kw):
            raise KeyboardInterrupt()

        with mock.patch.object(HUB.settings, 'load', pressed):
            try:
                code, out, err = self.run_cli('status')
            except KeyboardInterrupt:
                self.fail('the Ctrl+C left hub.py as a KeyboardInterrupt')
        # no server was closing: the line does not say that one did not close
        self.assertEqual((code, out, err), (1, '', HUB.INTERRUPTED + '\n'))
        self.assertEqual(HUB.INTERRUPTED, 'interrupted')

    def test_a_closed_console_at_the_closing_line_is_still_a_clean_close(self):
        for gone in (OSError(9, 'Bad file descriptor'),
                     ValueError('I/O operation on closed file')):
            with self.subTest(gone=type(gone).__name__):
                class Shut(io.StringIO):        # the console closed while the server served
                    def write(self, text):
                        if HUB.CLOSED in text:
                            raise gone
                        return io.StringIO.write(self, text)

                stop = threading.Event()
                stop.set()
                out, err = Shut(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = HUB.main(['serve', '--store', self.store_dir, '--port', '0'],
                                    stop=stop)
                self.assertEqual((code, err.getvalue()), (0, ''))
                self.assertEqual(len(out.getvalue().splitlines()), 1, out.getvalue())
                self.assertFalse(self.locked(), 'the store lock outlived the server')

    def test_a_refusal_and_a_config_error_are_printed_without_the_user_or_password_of_a_url(self):
        remote = 'https://someone:s3cret@git.example.test/team/data.git'
        bare = 'https://git.example.test/team/data.git'

        def raising(exc):
            def raise_it(*args, **kw):
                raise exc
            return raise_it

        for at, name, exc, code, line in (
                (HUB.serve, 'build', serve.ServeError('the remote %s cannot be used' % remote),
                 1, 'REFUSED: the remote %s cannot be used' % bare),
                (HUB.serve, 'build', auth.TokenFileError("a token file beside '%s'" % remote),
                 1, "REFUSED: a token file beside '%s'" % bare),
                (HUB.settings, 'load', HUB.ConfigError('hub.remote: %s is no name' % remote),
                 2, 'machine config: hub.remote: %s is no name' % bare)):
            with self.subTest(line=line), mock.patch.object(at, name, raising(exc)):
                got, out, err = self.run_cli('serve', '--store', self.store_dir, '--port', '0')
                self.assertEqual((got, out, err), (code, '', line + '\n'))
        # the answer of a server that is not as expected: its message is another process's text
        answer = (500, {'error': {'code': 'internal', 'message': "cannot reach '%s'" % remote}})
        with mock.patch.object(HUB.serve, 'ask', lambda *args, **kw: answer):
            got, out, err = self.run_cli('status', '--port', '8790')
        self.assertEqual((got, out, err), (
            1, '', "REFUSED: the server on port 8790 answered 500 internal: cannot reach '%s'\n"
            % bare))

    def test_the_log_prints_errors_and_notes_and_no_request_line(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            HUB._log('access', 'GET /api/v1/health 200')
            HUB._log('note', 'closing: the last pass')
            HUB._log('error', 'closing: it failed')
        self.assertEqual(err.getvalue(), 'NOTE: closing: the last pass\n'
                                         'ERROR: closing: it failed\n')

    def test_no_printed_line_holds_the_user_or_password_of_a_url(self):
        remote = 'https://someone:s3cret@git.example.test/team/data.git'
        trace = ('GET /api/v1/health: unexpected error\nTraceback (most recent call last):\n'
                 '  File "x.py", line 1, in route\nRuntimeError: cannot reach \'%s\'' % remote)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            HUB._log('error', trace)            # a listener's line: httpd does not scrub its own
            HUB._log('note', 'the remote is %s' % remote)
        text = err.getvalue()
        self.assertEqual(text, 'ERROR: %s\nNOTE: the remote is %s\n' % (
            trace.replace('someone:s3cret@', ''), 'https://git.example.test/team/data.git'))
        for word in ('someone', 's3cret'):
            self.assertNotIn(word, text)

    def test_a_start_note_is_printed_without_the_user_or_password_of_a_url(self):
        real = HUB.serve.build

        def build(settings, sources, log=None):
            hub = real(settings, sources, log=log)
            hub.notes.append('git sync is off: https://someone:s3cret@git.example.test/x refused')
            return hub

        stop = threading.Event()
        stop.set()
        with mock.patch.object(HUB.serve, 'build', build):
            code, out, err = self.run_cli('serve', '--store', self.store_dir, '--port', '0',
                                          stop=stop)
        self.assertEqual(code, 0)
        self.assertEqual(err, 'NOTE: git sync is off: https://git.example.test/x refused\n')

    def test_the_help_says_what_store_turns_off_and_what_setup_does_not_write(self):
        def helped(*argv):
            with mock.patch.dict(os.environ, {'NO_COLOR': '1', 'COLUMNS': '200'}):
                code, out, err = self.run_cli(*argv)
            self.assertEqual((code, err), (0, ''), argv)
            return ' '.join(out.split())

        text = helped('serve', '--help')
        self.assertIn('git sync is off for the run', text)
        self.assertIn('nothing in it is committed or pushed', text)
        self.assertIn('the machine config is not written', helped('--help'))
        doc = ' '.join(HUB.__doc__.split())
        self.assertIn('git sync is off for that run', doc)
        self.assertIn('An empty PATH is a usage error', doc)
        self.assertIn('It writes nothing else: never the machine config', doc)


class PushCase(CliCase):
    """serve hands the server the host's push delivery and the launcher's state folder
    (docs/action-hub.md "Push notifications"); a host stands in, so nothing is ever sent."""

    def host(self, adapter):
        fake = mock.Mock()
        fake.adapter_module.return_value = mock.Mock(ADAPTER=adapter)
        return mock.patch.dict(sys.modules, {'_host': fake})

    def test_without_a_launcher_state_folder_push_is_off_with_one_note(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(HUB._push_delivery(), (None, None))
        self.assertTrue(err.getvalue().startswith('NOTE: push notifications off: LaunchError: '),
                        err.getvalue())
        self.assertIn('launcher.state_dir is not set', err.getvalue())

    def test_a_host_without_a_delivery_is_off_with_one_note(self):
        state = os.path.join(self.home, 'launcher-state')
        os.makedirs(state)
        self.config({'launcher': {'state_dir': state}})
        with self.host(object()), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(HUB._push_delivery(), (None, None))
        self.assertEqual(err.getvalue(), 'NOTE: push notifications off: this host has no delivery\n')

    def test_serve_passes_the_delivery_and_the_state_folder_and_a_scratch_store_none(self):
        state = os.path.join(self.home, 'launcher-state')
        os.makedirs(state)
        self.store_dir = os.path.join(self.home, 'live-store')
        self.config({'launcher': {'state_dir': state}, 'hub': {'store': self.store_dir}})
        delivery, made = mock.Mock(), []

        class Adapter(object):
            def push(self, launch):
                made.append(launch.state_dir())
                return delivery

        real, seen = HUB.serve.build, []

        def build(settings, sources, log=None):     # what serve gave, then served without either
            seen.append((sources.push, sources.push_dir))
            sources.push = sources.sync = None
            return real(settings, sources, log=log)

        stop = threading.Event()
        stop.set()
        with self.host(Adapter()), mock.patch.object(HUB.serve, 'build', build):
            code, out, err = self.run_cli('serve', '--port', '0', stop=stop)
            self.assertEqual(code, 0, err)
            code, out, err = self.run_cli('serve', '--store', os.path.join(self.home, 'scratch'),
                                          '--port', '0', stop=stop)
            self.assertEqual(code, 0, err)
        self.assertEqual(made, [state])
        self.assertEqual(seen, [(delivery, state), (None, None)])


class SetupCase(CliCase):
    def test_setup_makes_the_token_files_and_writes_no_config(self):
        code, out, err = self.run_cli('setup')
        self.assertEqual((code, err), (0, ''))
        owner, agent = (auth.read_token(self.tokens, r) for r in (auth.OWNER, auth.AGENT))
        self.assertTrue(owner and agent and owner != agent)
        for role in (auth.OWNER, auth.AGENT):
            self.assertIn('%s token: %s' % (role, os.path.join(self.tokens,
                                                              auth.TOKEN_FILES[role])), out)
        self.assertNotIn(owner, out)
        self.assertNotIn(agent, out)
        self.assertIn('NOTE: no store folder is set', out)
        self.assertIn('serve', out)
        # the machine config folder holds the credential folder only: no config file is written
        self.assertEqual(os.listdir(os.path.dirname(self.tokens)), ['hub'])
        # a second run keeps the tokens
        self.assertEqual(self.run_cli('setup')[0], 0)
        self.assertEqual(auth.read_token(self.tokens, auth.OWNER), owner)

    def test_setup_speaks_of_no_old_hub(self):
        # the PM start checklist shows the hub site with or without the `hub` key: no switch to name
        for _ in range(2):          # the first run makes the files, the second changes nothing
            code, out, err = self.run_cli('setup')
            self.assertEqual((code, err), (0, ''))
            for old in ('old hub', 'Action Hub', 'artifact', 'switches'):
                self.assertNotIn(old, out)

    def test_setup_names_the_configured_store(self):
        self.config({'hub': {'store': self.store_dir}})
        code, out, err = self.run_cli('setup')
        self.assertEqual((code, err), (0, ''))
        self.assertIn('store: %s' % self.store_dir, out)
        self.assertNotIn('NOTE', out)
        self.assertFalse(os.path.exists(self.store_dir), 'setup opened the store')

    def test_setup_refuses_a_token_file_that_cannot_serve(self):
        os.makedirs(self.tokens)
        bad = os.path.join(self.tokens, auth.TOKEN_FILES[auth.OWNER])
        with open(bad, 'w', encoding='utf-8'):
            pass
        code, out, err = self.run_cli('setup')
        self.assertEqual(code, 1)
        self.assertIn('REFUSED: ', err)
        self.assertIn(bad, err)


class SetupCheckCase(CliCase):
    """`setup --check`: read-only. It names the Access state, probes the Access keys when the
    settings can serve, and lists each plugin copy that would refuse the `hub` key."""
    NEW = "SCHEMA = {'hub': 1, 'plugin': 1}\n"
    OLD = "SCHEMA = {'plugin': 1}\n"

    def setUp(self):
        CliCase.setUp(self)
        self.root = os.path.join(self.home, 'exports')

    def export(self, name, text, current=False):
        folder = os.path.join(self.root, name, 'kitlib', 'config')
        os.makedirs(folder)
        with open(os.path.join(folder, 'user.py'), 'w', encoding='utf-8') as f:
            f.write(text)
        if current:
            os.makedirs(os.path.join(self.root, '.claude-plugin'), exist_ok=True)
            with open(os.path.join(self.root, '.claude-plugin', 'marketplace.json'), 'w',
                      encoding='utf-8') as f:
                json.dump({'name': 'pm-kit', 'plugins': [{'name': 'pm-kit',
                                                          'source': './' + name}]}, f)

    def check(self, hub=None):
        obj = {'plugin': {'export_dir': self.root}}
        if hub is not None:
            obj['hub'] = hub
        folder = self.config(obj)

        def files():
            got = {}
            for n in os.listdir(folder):
                with open(os.path.join(folder, n), 'rb') as f:
                    got[n] = f.read()
            return got
        before = files()
        code, out, err = self.run_cli('setup', '--check')
        self.assertEqual(files(), before, 'setup --check wrote the machine config')
        self.assertFalse(os.path.exists(self.tokens), 'setup --check made the token files')
        return code, out, err

    def test_every_copy_in_use_knows_hub_so_it_is_ready_and_nothing_is_written(self):
        self.export('aaaaaaaaaaaa', self.NEW, current=True)
        self.export('bbbbbbbbbbbb', self.OLD)
        code, out, err = self.check()
        self.assertEqual((code, err), (0, ''))
        self.assertIn('access: not set (hub.access)', out)
        self.assertNotIn('access keys:', out)
        self.assertIn('aaaaaaaaaaaa (current export): knows every key', out)
        self.assertIn('bbbbbbbbbbbb (not in use): would refuse hub', out)
        self.assertIn('READY: ', out)
        self.assertNotIn('NOT READY', out)

    def test_a_copy_in_use_that_would_refuse_hub_is_not_ready(self):
        self.export('aaaaaaaaaaaa', self.OLD, current=True)
        code, out, err = self.check()
        self.assertEqual((code, err), (1, ''))
        self.assertIn('aaaaaaaaaaaa (current export): would refuse hub', out)
        self.assertIn('NOT READY: ', out)

    def test_access_settings_that_can_serve_are_probed_once(self):
        self.export('aaaaaaaaaaaa', self.NEW, current=True)
        hub = {'access': {'team': 'example-team', 'aud': 'aud-tag',
                          'emails': ['owner@example.invalid']}}
        calls = []

        def probe(team):
            calls.append(team)
            return True, 'access keys: 2 fetched from example-team.cloudflareaccess.com'
        with mock.patch.object(HUB.access, 'probe', probe):
            code, out, err = self.check(hub)
        self.assertEqual((code, err, calls), (0, '', ['example-team']))
        self.assertIn('access: set', out)
        self.assertIn('access keys: 2 fetched from example-team.cloudflareaccess.com', out)
        with mock.patch.object(HUB.access, 'probe',
                               lambda team: (False, 'access keys: not fetched from x: keys-fetch')):
            code, out, err = self.check(hub)
        self.assertEqual(code, 1)
        self.assertIn('access keys: not fetched from x: keys-fetch', out)
        self.assertIn('NOT READY: ', out)

    def test_access_settings_that_cannot_serve_are_named_and_never_probed(self):
        self.export('aaaaaaaaaaaa', self.NEW, current=True)
        hub = {'access': {'team': 'example-team', 'aud': 'aud-tag', 'emails': []}}
        with mock.patch.object(HUB.access, 'probe', side_effect=AssertionError('probed')):
            code, out, err = self.check(hub)
        self.assertEqual(code, 1)
        self.assertIn('access: cannot serve: %s' % tunnel.NO_EMAILS, out)
        self.assertNotIn('access keys:', out)

    def test_kit_copies_that_cannot_be_looked_at_are_not_ready(self):
        from kitlib import pluginsource
        with mock.patch.object(pluginsource, 'load',
                               side_effect=pluginsource.RefreshError('no export root set')):
            code, out, err = self.check()
        self.assertEqual((code, err), (1, ''))
        self.assertIn('kit copies: cannot look: no export root set', out)
        self.assertNotIn('kit copies, checked against', out)
        self.assertIn('NOT READY: the kit copies could not be looked at', out)

    def test_an_account_install_whose_folder_is_gone_says_to_refresh_that_account(self):
        from kitlib import pluginsource
        self.export('aaaaaaaaaaaa', self.NEW, current=True)
        gone = os.path.join(self.home, 'acct', 'plugins', 'cache', 'pm-kit', 'pm-kit', '0.1')
        acct = os.path.join(self.home, 'acct')
        with mock.patch.object(pluginsource, 'installs', lambda: [(gone, acct)]):
            code, out, err = self.check()
        self.assertEqual((code, err), (1, ''))
        self.assertIn('%s (installed in %s): cannot tell: the folder is missing: refresh that '
                      'account (' % (gone, acct), out)
        self.assertIn('plugin_refresh', out)
        self.assertIn('NOT READY: ', out)

    def test_check_takes_no_other_option(self):
        for extra in (('--autostart',), ('--autostart', '--whatif'), ('--whatif',)):
            code, out, err = self.run_cli('setup', '--check', *extra)
            self.assertEqual(code, 2, extra)
            self.assertIn('--check', err)
        self.assertFalse(os.path.exists(self.tokens))


class ClientCase(CliCase):
    def test_status_and_open_with_no_server_are_exit_3_with_the_start_command(self):
        self.assertEqual(self.run_cli('setup')[0], 0)
        port = self.dead_port()
        for argv in (('status', '--port', str(port)), ('open', '--print', '--port', str(port))):
            code, out, err = self.run_cli(*argv)
            self.assertEqual((code, out), (3, ''), argv)
            self.assertIn('no hub server is listening on port %d' % port, err)
            self.assertIn(HUB.START, err)
        self.assertTrue(HUB.START.endswith(' serve'))

    def test_status_prints_the_servers_health(self):
        hub = self.server()
        code, out, err = self.run_cli('status', '--port', str(hub.port))
        self.assertEqual((code, err), (0, ''))
        lines = out.splitlines()
        self.assertEqual(lines[0], 'hub server on http://127.0.0.1:%d' % hub.port)
        self.assertEqual(lines[1], 'revision %d' % hub.store.revision)
        self.assertIn('store %s' % os.path.abspath(self.store_dir), lines)
        self.assertIn('git: ' + serve.SYNC_OFF, lines)

    def test_status_says_in_one_line_why_the_tunnel_listener_is_off(self):
        hub = self.server()             # no hub.access in the settings: the tunnel is off
        self.assertEqual(len(hub.listeners), 1)
        code, out, err = self.run_cli('status', '--port', str(hub.port))
        self.assertEqual((code, err), (0, ''))
        said = [line for line in out.splitlines() if line.startswith('tunnel')]
        self.assertEqual(said, ['tunnel: off: ' + tunnel.UNSET])

    def test_status_with_tokens_that_are_not_the_servers_is_refused(self):
        hub = self.server(tokens=os.path.join(self.home, 'other-tokens'))
        self.assertEqual(self.run_cli('setup')[0], 0)
        code, out, err = self.run_cli('status', '--port', str(hub.port))
        self.assertEqual((code, out), (1, ''))
        self.assertIn('REFUSED: the server on port %d answered 403 forbidden' % hub.port, err)

    def test_open_gives_a_pairing_link_that_works_once(self):
        hub = self.server()
        code, out, err = self.run_cli('open', '--print', '--port', str(hub.port))
        self.assertEqual((code, err), (0, ''))
        link = out.strip()
        base = 'http://127.0.0.1:%d' % hub.port
        self.assertTrue(link.startswith(base + '/pair?code='), link)
        self.assertEqual(httpfix.get(hub.port, link[len(base):]).status, 303)
        self.assertNotEqual(httpfix.get(hub.port, link[len(base):]).status, 303)
        opened = []
        with mock.patch.object(HUB.webbrowser, 'open', lambda url: opened.append(url) or True):
            code, out, err = self.run_cli('open', '--port', str(hub.port))
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(len(opened), 1)
        self.assertTrue(opened[0].startswith(base + '/pair?code='))
        self.assertNotIn('code=', out)
        with mock.patch.object(HUB.webbrowser, 'open', lambda url: False):
            code, out, err = self.run_cli('open', '--port', str(hub.port))
        self.assertEqual(code, 1)
        self.assertIn('REFUSED: no browser could be opened', err)

    def test_open_needs_the_owners_token(self):
        hub = self.server(tokens=os.path.join(self.home, 'other-tokens'))
        code, out, err = self.run_cli('open', '--print', '--port', str(hub.port))
        self.assertEqual((code, out), (1, ''))
        self.assertIn('REFUSED: no owner token', err)
        self.assertEqual(self.run_cli('setup')[0], 0)
        code, out, err = self.run_cli('open', '--print', '--port', str(hub.port))
        self.assertEqual((code, out), (1, ''))
        self.assertIn('answered 403', err)

    def test_unpair_ends_every_browser_the_running_server_paired(self):
        self.assertEqual(self.run_cli('setup')[0], 0)
        probe = self.server()               # port 0 keeps no sessions file: find a port, ask it
        port = probe.port
        self.assertIs(probe.close(), True)
        hub = self.server(port=port)
        base = 'http://127.0.0.1:%d' % hub.port
        cookies = []
        for _ in range(2):
            link = self.run_cli('open', '--print', '--port', str(hub.port))[1].strip()
            set_cookie = httpfix.get(hub.port, link[len(base):]).header('Set-Cookie')[0]
            cookies.append(('Cookie', set_cookie.split(';')[0]))
        for cookie in cookies:
            self.assertEqual(httpfix.get(hub.port, HUB.HEALTH, headers=[cookie]).status, 200)
        code, out, err = self.run_cli('unpair', '--port', str(hub.port))
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(out, 'unpaired every browser of the hub server on port %d\n' % hub.port)
        for cookie in cookies:
            self.assertEqual(httpfix.get(hub.port, HUB.HEALTH, headers=[cookie]).status, 403)
        with open(os.path.join(self.tokens, auth.sessions_file(hub.port)), encoding='ascii') as f:
            self.assertEqual(json.load(f)['sessions'], [])

    def test_unpair_with_no_server_empties_the_sessions_file_itself(self):
        self.assertEqual(self.run_cli('setup')[0], 0)
        port = self.dead_port()
        creds = auth.Credentials(self.tokens, port=port, persist=True)
        value = creds.pair(creds.new_pairing_code())
        cookie = {auth.cookie_name(port): value}
        reread = auth.Credentials(self.tokens, port=port, persist=True)
        self.assertEqual(reread.role_for({}, cookie), auth.OWNER)
        code, out, err = self.run_cli('unpair', '--port', str(port))
        self.assertEqual((code, err), (0, ''))
        self.assertIn('no hub server is listening on port %d' % port, out)
        self.assertIn(auth.sessions_file(port), out)
        reread = auth.Credentials(self.tokens, port=port, persist=True)
        self.assertEqual((reread.role_for({}, cookie), reread.notes), (None, []))

    def unpair_racing_a_start(self, second):
        """unpair with no server at its first ask, and `second` the answer to the next one:
        a server that bound the port and read the sessions file before the file was emptied."""
        self.assertEqual(self.run_cli('setup')[0], 0)
        port = self.dead_port()
        asked = []

        def ask(p, method, path, token=None, **kw):
            asked.append((p, method, path))
            if len(asked) == 1:
                raise serve.NotRunning(p)
            if isinstance(second, Exception):
                raise second
            return second

        with mock.patch.object(HUB.serve, 'ask', ask):
            got = self.run_cli('unpair', '--port', str(port))
        self.assertEqual(asked, [(port, 'POST', httpd.UNPAIR_ROUTE)] * 2)
        return port, got

    def test_unpair_with_no_server_asks_again_after_the_file_and_unpairs_one_that_started(self):
        port, (code, out, err) = self.unpair_racing_a_start((200, {'ok': True}))
        self.assertEqual((code, err), (0, ''))
        self.assertIn(auth.sessions_file(port), out)
        self.assertIn('unpaired every browser of the hub server on port %d' % port, out)

    def test_unpair_with_a_server_that_started_and_refuses_is_refused(self):
        port, (code, out, err) = self.unpair_racing_a_start((403, {'error': 'forbidden'}))
        self.assertEqual(code, 1)
        self.assertIn('REFUSED: the server on port %d answered 403' % port, err)

    def test_unpair_with_still_no_server_at_the_second_ask_is_the_file_alone(self):
        port, (code, out, err) = self.unpair_racing_a_start(serve.NotRunning(self.dead_port()))
        self.assertEqual((code, err), (0, ''))
        self.assertIn(auth.sessions_file(port), out)
        self.assertNotIn('of the hub server', out)

    def test_unpair_with_no_server_and_a_file_it_cannot_write_is_refused(self):
        self.assertEqual(self.run_cli('setup')[0], 0)
        port = self.dead_port()
        os.makedirs(os.path.join(self.tokens, auth.sessions_file(port)))   # a folder in its place
        code, out, err = self.run_cli('unpair', '--port', str(port))
        self.assertEqual((code, out), (1, ''))
        self.assertIn('REFUSED: ', err)
        self.assertIn('could not be written', err)

    def test_unpair_needs_the_owners_token(self):
        port = self.dead_port()
        code, out, err = self.run_cli('unpair', '--port', str(port))
        self.assertEqual((code, out), (1, ''))
        self.assertIn('REFUSED: no owner token', err)
        self.assertFalse(os.path.exists(os.path.join(self.tokens, auth.sessions_file(port))))
        hub = self.server(tokens=os.path.join(self.home, 'other-tokens'))
        self.assertEqual(self.run_cli('setup')[0], 0)
        code, out, err = self.run_cli('unpair', '--port', str(hub.port))
        self.assertEqual((code, out), (1, ''))
        self.assertIn('REFUSED: the server on port %d answered 403 forbidden' % hub.port, err)


class StopRestartCase(CliCase):
    """hub.py stop and hub.py restart (kitlib.hubsite.control). No test here reads or starts
    the real scheduled task: its state and its start are replaced in every restart."""

    def running(self):
        """A hub that serve.run serves on a thread, as `hub.py serve` does: the hub and the
        thread. Its port is a free one."""
        cfg = hub_settings.settings(None)._replace(port=0, store=self.store_dir)
        hub = serve.build(cfg, serve.Sources(tokens=self.tokens,
                                             static_dir=os.path.join(self.home, 'web')))
        up, stop = threading.Event(), threading.Event()
        runner = threading.Thread(target=serve.run, args=(hub, lambda h: up.set(), stop),
                                  name='test-run', daemon=True)
        runner.start()
        self.addCleanup(runner.join, 30)
        self.addCleanup(stop.set)
        self.assertTrue(up.wait(30), 'the server never started')
        return hub, runner

    def fake_task(self, state):
        """The task's state is `state`; a start of it is noted in the list returned. hub.port
        is a port nothing listens on, so a restart that went on to wait for a new server would
        never ask the machine's own."""
        self.config({'hub': {'port': self.dead_port()}})
        started = []
        states = mock.patch.object(HUB.control.autostart, 'task_state', lambda: state)
        starts = mock.patch.object(HUB.control.autostart, 'start_task',
                                   lambda: started.append(True))
        states.start()
        starts.start()
        self.addCleanup(states.stop)
        self.addCleanup(starts.stop)
        return started

    def test_stop_ends_the_running_server_and_waits_for_its_process(self):
        self.assertEqual(self.run_cli('setup')[0], 0)
        hub, runner = self.running()
        asked = []

        def alive(pid, ctime):
            asked.append((pid, ctime))
            return runner.is_alive()

        with mock.patch.object(HUB.control.process, 'alive', alive):
            code, out, err = self.run_cli('stop', '--port', str(hub.port))
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(out, 'STOPPED: the hub server on port %d (pid %d) has closed and its '
                              'process has ended\n' % (hub.port, os.getpid()))
        runner.join(30)
        self.assertFalse(runner.is_alive())
        self.assertFalse(self.locked())
        self.assertTrue(asked, 'the process was never asked about')
        self.assertEqual(asked[0][0], os.getpid())

    def test_stop_with_nothing_listening_is_nothing_to_stop(self):
        self.assertEqual(self.run_cli('setup')[0], 0)
        port = self.dead_port()
        code, out, err = self.run_cli('stop', '--port', str(port))
        self.assertEqual((code, err), (0, ''))
        self.assertTrue(out.startswith('nothing to stop: nothing answers on port %d' % port), out)
        # a server stopped a moment ago has freed its port, but may still hold the store
        self.assertIn('may still be closing', out)
        self.assertNotIn('no hub server is listening', out)

    def test_stop_and_restart_need_the_owners_token(self):
        port = self.dead_port()
        started = self.fake_task('Ready')
        for verb in ('stop', 'restart'):
            code, out, err = self.run_cli(verb, '--port', str(port))
            self.assertEqual((code, out), (1, ''), verb)
            self.assertIn('REFUSED: no owner token', err)
        self.assertEqual(started, [])

    def test_a_stop_the_server_refuses_is_refused_and_it_serves_on(self):
        hub = self.server(tokens=os.path.join(self.home, 'other-tokens'))
        self.assertEqual(self.run_cli('setup')[0], 0)
        code, out, err = self.run_cli('stop', '--port', str(hub.port))
        self.assertEqual((code, out), (1, ''))
        self.assertIn('REFUSED: the server on port %d answered 403 forbidden' % hub.port, err)
        self.assertFalse(hub.stopping.is_set())

    def test_stop_passes_the_port_the_token_and_the_timeout(self):
        self.assertEqual(self.run_cli('setup')[0], 0)
        self.config({'hub': {'port': 8799}})
        calls = []

        def stop(port, token, timeout, **kw):
            calls.append((port, token, timeout))
            return HUB.control.Stopped(False, None, False)

        with mock.patch.object(HUB.control, 'stop', stop):
            self.assertEqual(self.run_cli('stop')[0], 0)
            self.assertEqual(self.run_cli('stop', '--port', '8801', '--timeout', '7.5')[0], 0)
        owner = auth.read_token(self.tokens, auth.OWNER)
        self.assertEqual(calls, [(8799, owner, HUB.control.DEFAULT_TIMEOUT_S),
                                 (8801, owner, 7.5)])
        self.assertEqual(HUB.control.DEFAULT_TIMEOUT_S, 300)

    def test_restart_with_no_task_starts_nothing_and_prints_the_start_command(self):
        self.assertEqual(self.run_cli('setup')[0], 0)
        port = self.dead_port()
        started = self.fake_task(None)
        code, out, err = self.run_cli('restart', '--port', str(port))
        self.assertEqual((code, err), (3, ''))
        self.assertIn('nothing to stop: nothing answers on port %d' % port, out)
        self.assertIn(HUB.hub_autostart.TASK_NAME, out)
        self.assertTrue(out.endswith(HUB.task_advice(HUB.control.NO_TASK) + '\n'), out)
        self.assertIn('setup --autostart', out)
        self.assertIn('Start-ScheduledTask', out)
        self.assertNotIn(HUB.START, out)
        self.assertEqual(started, [])

    def test_restart_of_a_server_started_by_hand_stops_it_and_starts_nothing(self):
        self.assertEqual(self.run_cli('setup')[0], 0)
        hub, runner = self.running()
        started = self.fake_task('Ready')
        with mock.patch.object(HUB.control.process, 'alive',
                               lambda pid, ctime: runner.is_alive()):
            code, out, err = self.run_cli('restart', '--port', str(hub.port))
        self.assertEqual((code, err), (3, ''))
        self.assertIn('STOPPED: the hub server on port %d' % hub.port, out)
        self.assertIn('started by hand', out)
        self.assertTrue(out.endswith(HUB.task_advice(HUB.control.HAND_STARTED) + '\n'), out)
        self.assertIn('Start-ScheduledTask', out)
        self.assertNotIn(HUB.START, out)
        self.assertEqual(started, [])
        runner.join(30)
        self.assertFalse(runner.is_alive())

    def test_restart_with_a_disabled_task_starts_nothing(self):
        self.assertEqual(self.run_cli('setup')[0], 0)
        started = self.fake_task('Disabled')
        code, out, err = self.run_cli('restart', '--port', str(self.dead_port()))
        self.assertEqual((code, err), (3, ''))
        self.assertIn('disabled', out)
        self.assertTrue(out.endswith(HUB.task_advice(HUB.control.DISABLED) + '\n'), out)
        self.assertIn('Enable-ScheduledTask', out)
        self.assertNotIn(HUB.START, out)
        self.assertEqual(started, [])

    def test_restart_says_where_the_task_s_server_answers(self):
        self.assertEqual(self.run_cli('setup')[0], 0)
        self.config({'hub': {'port': 8799}})
        calls = []

        def restart(port, token, timeout, new_port=None, **kw):
            calls.append((port, timeout, new_port))
            return HUB.control.Restarted(HUB.control.Stopped(True, 77, False),
                                         HUB.control.RESTARTED, new_port)

        with mock.patch.object(HUB.control, 'restart', restart):
            code, out, err = self.run_cli('restart', '--port', '8801', '--timeout', '9')
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(calls, [(8801, 9.0, 8799)])
        self.assertIn('STOPPED: the hub server on port 8801 (pid 77)', out)
        self.assertIn('RESTARTED: the hub server answers on port 8799', out)

    def test_a_timeout_is_exit_4_and_says_nothing_was_killed(self):
        self.assertEqual(self.run_cli('setup')[0], 0)
        port = self.dead_port()
        for verb, phase in (('stop', HUB.control.CLOSING_PHASE),
                            ('restart', HUB.control.ANSWER_PHASE)):
            with self.subTest(verb=verb):
                def fail(*args, **kw):
                    raise HUB.control.TimedOut(phase, 'port %d did not close within 1 s; '
                                                      'nothing was killed' % port)

                with mock.patch.object(HUB.control, verb, fail):
                    code, out, err = self.run_cli(verb, '--port', str(port))
                self.assertEqual((code, out), (4, ''))
                self.assertIn('TIMED OUT: port %d did not close within 1 s; nothing was killed'
                              % port, err)
                # a server the task started has no console: its start-up lines go nowhere
                self.assertEqual('hub.py serve' in err and 'console' in err, verb == 'restart',
                                 err)

    def test_the_console_advice_follows_a_timeout_waiting_for_the_new_server_alone(self):
        # a server that never answered after the start may have said why at start-up, in a
        # console it does not have; a stop or a task that has not ended says nothing there
        self.assertEqual(self.run_cli('setup')[0], 0)
        port = self.dead_port()
        for phase in (HUB.control.CLOSING_PHASE, HUB.control.PROCESS_PHASE,
                      HUB.control.TASK_PHASE, HUB.control.ANSWER_PHASE):
            with self.subTest(phase=phase):
                def fail(*args, **kw):
                    raise HUB.control.TimedOut(phase, 'ran out; nothing was killed')

                with mock.patch.object(HUB.control, 'restart', fail):
                    code, out, err = self.run_cli('restart', '--port', str(port))
                self.assertEqual((code, out), (4, ''))
                self.assertIn('TIMED OUT: ran out; nothing was killed', err)
                self.assertEqual(HUB.NO_CONSOLE in err, phase == HUB.control.ANSWER_PHASE, err)

    def test_a_task_that_cannot_be_started_after_the_stop_says_the_server_stopped(self):
        self.assertEqual(self.run_cli('setup')[0], 0)
        port = self.dead_port()
        for reason in ("starting the task 'x': powershell exit 1: denied",
                       "reading the state of the task 'x': powershell timed out after 60 s"):
            with self.subTest(reason=reason):
                def fail(*args, **kw):
                    raise HUB.control.StartFailed(HUB.control.Stopped(True, 77, False),
                                                  HUB.hub_autostart.AutostartError(reason))

                with mock.patch.object(HUB.control, 'restart', fail):
                    code, out, err = self.run_cli('restart', '--port', str(port))
                self.assertEqual(code, 1)
                self.assertEqual(out, 'STOPPED: the hub server on port %d (pid 77) has closed and '
                                      'its process has ended\n' % port)
                self.assertNotIn('REFUSED', err)
                self.assertIn('the scheduled task could not be started: %s' % reason, err)
                self.assertTrue(err.endswith(HUB.task_advice(None) + '\n'), err)
                self.assertIn('Start-ScheduledTask', err)
                self.assertNotIn(HUB.START, err)


class ClosedConsoleCase(CliCase):
    """`serve` beside a console that was closed while it served: every line of it is lost, and
    the server ends as it would have."""

    def shut(self, gone, texts):
        class Shut(io.StringIO):
            def write(self, text):
                if any(t in text for t in texts):
                    raise gone
                return io.StringIO.write(self, text)
        return Shut()

    def serve(self, gone, texts, under):
        stop = threading.Event()
        stop.set()
        out, err = self.shut(gone, texts), self.shut(gone, texts)
        with under, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = HUB.main(['serve', '--store', self.store_dir, '--port', '0'], stop=stop)
            except (OSError, ValueError) as e:
                self.fail('the closed console left hub.py as %r' % e)
        self.assertFalse(self.locked(), 'the store lock outlived the server')
        return code, out.getvalue(), err.getvalue()

    GONE = (OSError(9, 'Bad file descriptor'),)     # ValueError takes the same except in hub.py

    def test_a_start_line_that_cannot_be_written_does_not_stop_the_server(self):
        served = []
        real = HUB.serve.run

        def run(hub, started, stop):
            def said(h):
                started(h)
                served.append(get_health(h))
            return real(hub, said, stop)

        def get_health(h):
            token = auth.read_token(self.tokens, auth.AGENT)
            return httpfix.get(h.port, HUB.HEALTH, token).status

        for gone in self.GONE:
            with self.subTest(gone=type(gone).__name__):
                del served[:]
                code, out, err = self.serve(gone, ['hub serving ', HUB.CLOSED],
                                            mock.patch.object(HUB.serve, 'run', run))
                self.assertEqual((code, out, err), (0, '', ''))
                self.assertEqual(served, [200], 'the server did not serve after its start line')


class VerbCase(CliCase):
    """The verbs that ask the server, against one this test started on port 0."""

    def setUp(self):
        CliCase.setUp(self)
        self.hub = self.server()
        self.port = str(self.hub.port)

    def ok(self, *argv):
        """The stdout lines of a verb that must end with exit 0 and say nothing on stderr."""
        code, out, err = self.run_cli(*(argv + ('--port', self.port)))
        self.assertEqual((code, err), (0, ''), (argv, out))
        lines = out.splitlines()
        if '--json' in argv:
            return lines
        self.warned = [line for line in lines if line.startswith('WARN ')]
        return [line for line in lines if not line.startswith('WARN ')]

    def refused(self, *argv):
        code, out, err = self.run_cli(*(argv + ('--port', self.port)))
        self.assertEqual((code, out), (1, ''), (argv, err))
        return err

    def direct(self, path):
        r = httpfix.get(self.hub.port, HUB.api.BASE + path,
                        auth.read_token(self.tokens, auth.AGENT))
        self.assertEqual(r.status, 200, r.body)
        return r.json()

    def file(self, name, text):
        path = os.path.join(self.home, name)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)
        return path

    def ask(self, title='which way'):
        return self.ok('questions', 'add', '--title', title, '--repo', 'kit', '--class', 'go',
                       '--option', 'left::go left', '--option', 'right :: go right')


class ApprovalsCase(VerbCase):
    """`approvals` lists and shows the store's requests with their status read now."""

    def setUp(self):
        VerbCase.setUp(self)
        now = datetime.datetime.now(datetime.timezone.utc)
        old = now - datetime.timedelta(seconds=approvals.APPROVED_TTL_S + 60)
        files = [{'path': 'AGENTS.md', 'base_blob': 'b' * 40, 'content': 'new\n'},
                 {'path': 'gone.md', 'base_blob': 'c' * 40, 'content': None}]
        live = approvals.new_doc('https://github.com/o/r', 'live one', 'a' * 40, files, now,
                                 reason='why not')
        stale = dict(approvals.new_doc('https://github.com/o/other', 'stale one', 'a' * 40,
                                       files, old),
                     status='approved', decision='approve', decided_at=old.isoformat())
        self.assertEqual(self.hub.store.add('approvals', live).id, 'A-001')
        self.assertEqual(self.hub.store.add('approvals', stale).id, 'A-002')
        self.blob = approvals.blob_id('new\n')

    def test_the_list_applies_the_expiry_and_filters(self):
        self.assertEqual(self.ok('approvals'), ['A-001 pending [github.com/o/r] live one',
                                                'A-002 expired [github.com/o/other] stale one'])
        self.assertEqual(self.ok('approvals', '--status', 'expired'),
                         ['A-002 expired [github.com/o/other] stale one'])
        self.assertEqual(self.ok('approvals', '--status', 'approved'), [])
        self.assertEqual(self.ok('approvals', '--repo', 'GitHub.com/o/r'),
                         ['A-001 pending [github.com/o/r] live one'])
        items = json.loads('\n'.join(self.ok('approvals', '--json')))['items']
        self.assertEqual([(d['id'], d['status']) for d in items],
                         [('A-001', 'pending'), ('A-002', 'expired')])

    def test_show_prints_the_request_and_its_files(self):
        self.assertEqual(self.ok('approvals', 'show', 'A-001'), [
            'A-001 pending [github.com/o/r] live one', 'head ' + 'a' * 40, 'reason why not',
            'AGENTS.md %s -> %s' % ('b' * 40, self.blob), 'gone.md %s -> deleted' % ('c' * 40)])
        self.assertIn('no approval A-009', self.refused('approvals', 'show', 'A-009'))

    def test_a_document_that_is_not_an_object_is_malformed(self):
        real = self.hub.store.snapshot

        def snapshot():
            revision, docs = real()
            docs['approvals']['A-003'] = ['not', 'an', 'object']
            return revision, docs

        with mock.patch.object(self.hub.store, 'snapshot', snapshot):
            self.assertEqual(self.ok('approvals')[2].rstrip(), 'A-003 malformed [-]')
            self.assertEqual(self.ok('approvals', 'show', 'A-003')[0].rstrip(),
                             'A-003 malformed [-]')


class PendingCase(VerbCase):
    def test_pending_prints_the_items_get_pending_answers(self):
        self.assertEqual(self.ok('pending'), [])
        self.assertEqual(self.ask(), ['ASKED Q-1000'])
        self.assertEqual(self.ok('answer', 'Q-1000', '--choice', '1'), ['ANSWERED Q-1000 right'])
        self.assertEqual(self.ok('to-pm', 'add', '--text', 'look at this', '--repo', 'kit'),
                         ['QUEUED P-001'])
        self.assertEqual(self.ok('to-pm', 'send'), ['SENT P-001'])
        want = self.direct('/pending')
        self.assertEqual(len(want['items']), 2, want)
        self.assertEqual(json.loads('\n'.join(self.ok('pending', '--json'))), want)
        self.assertEqual(self.ok('pending'), [
            '%s %s [%s] to=%s from=%s %s%s' % (i['kind'], i['id'], i['repo'] or '-', i['route'],
                                               i['from'], 'class=%s ' % i['class']
                                               if 'class' in i else '', i['title'])
            for i in want['items']])
        self.assertIn('Q-1000 [kit] to=pm from=pm class=go which way', self.ok('pending')[0])

    def filed_pair(self):
        """Q-1000 answered and filed into its record (as the store holds it), Q-1001 only answered."""
        self.ask()
        self.ask('other way')
        self.ok('answer', 'Q-1000', '--choice', '1')
        self.ok('answer', 'Q-1001', '--choice', '1')
        self.hub.store.update('questions', 'Q-1000', {'status': 'filed', 'filed_at': '2026-10-24T00:00:00Z'})

    def test_pending_marks_a_filed_question(self):
        self.filed_pair()
        lines = {l.split()[1]: l for l in self.ok('pending')}
        self.assertTrue(lines['Q-1000'].endswith(' which way (filed)'), lines)
        self.assertTrue(lines['Q-1001'].endswith(' other way'), lines)

    def test_pending_for_pm_lists_classed_answers_apart_for_its_audit(self):
        self.ask()
        self.ok('questions', 'add', '--title', 'which go', '--repo', 'kit', '--class', 'go',
                '--option', 'a::b')
        self.hub.store.update('questions', 'Q-1000', {'class': None})   # one asked before classes
        for qid in ('Q-1000', 'Q-1001'):
            self.ok('answer', qid, '--choice', '0')
        self.assertEqual(self.ok('pending', '--for', 'pm'), [
            'question Q-1000 [kit] to=pm from=pm which way',
            'AUDIT classed answers, no record to file into:',
            'question Q-1001 [kit] to=pm from=pm class=go which go'])
        self.assertEqual(self.ok('pending'), [
            'question Q-1000 [kit] to=pm from=pm which way',
            'question Q-1001 [kit] to=pm from=pm class=go which go'])

    def test_wait_pending_marks_a_filed_question(self):
        self.filed_pair()
        lines = {l.split()[3]: l for l in self.ok('wait', '--pending', '--timeout', '5')}
        self.assertTrue(lines['Q-1000'].endswith(' which way (filed)'), lines)
        self.assertTrue(lines['Q-1001'].endswith(' other way'), lines)


class WriteCase(VerbCase):
    def test_a_question_is_asked_answered_and_logged(self):
        context = self.file('context.md', 'the context\nof it\n')
        self.assertEqual(self.ok(
            'questions', 'add', '--title', 'which way', '--repo', 'kit', '--class', 'go',
            '--context-file', context,
            '--option', 'left::go left', '--option', 'right::go right', '--recommended', '0',
            '--refs', 'B-1', 'B-2', '--urgent', '--from', 'pm'), ['ASKED Q-1000'])
        doc = self.direct('/questions')['items'][0]
        self.assertEqual((doc['context'], doc['recommended'], doc['refs'], doc['urgent'],
                          doc['from']), ('the context\nof it\n', 0, ['B-1', 'B-2'], True, 'pm'))
        self.assertEqual(doc['options'], [{'label': 'left', 'description': 'go left'},
                                          {'label': 'right', 'description': 'go right'}])
        self.assertEqual(self.ok('questions'), ['Q-1000 v1 open [kit] which way'])
        self.assertEqual(self.ok('questions', '--status', 'answered'), [])
        self.assertEqual(self.ok('questions', '--repo', 'elsewhere'), [])
        # the first option is place 0: it is a choice, not "no choice"
        self.assertEqual(self.ok('answer', 'Q-1000', '--choice', '0', '--note', 'as planned'),
                         ['ANSWERED Q-1000 left'])
        self.assertEqual(self.ok('questions', '--status', 'answered'),
                         ['Q-1000 v2 answered [kit] which way'])
        self.assertEqual(self.ok('questions', 'log', 'Q-1000', '--if-version', '2'),
                         ['LOGGED Q-1000'])
        doc = self.direct('/questions?status=all')['items'][0]
        self.assertEqual((doc['status'], doc.get('ruling'), doc['answer']['note']),
                         ('logged', None, 'as planned'))
        self.assertEqual(json.loads(self.ok('questions', '--status', 'all', '--json')[0]),
                         self.direct('/questions?status=all'))

    def test_questions_log_takes_no_ruling_id_and_a_logged_question_is_not_logged_again(self):
        self.ask()
        self.ok('answer', 'Q-1000', '--choice', '0')
        for extra in (RULING, '--replace'):
            (code, out, err) = self.run_cli('questions', 'log', 'Q-1000', extra, '--if-version', '2')
            self.assertEqual(code, 2, (extra, out, err))
        self.assertEqual(self.ok('questions', 'log', 'Q-1000', '--if-version', '2'), ['LOGGED Q-1000'])
        self.assertIn('REFUSED', self.refused('questions', 'log', 'Q-1000', '--if-version', '3'))
        self.assertNotIn('ruling', self.direct('/questions?status=all')['items'][0])

    def test_questions_show_prints_one_question_with_its_answer_and_note(self):
        self.ask()
        self.ask('another')
        out = self.ok('questions', 'show', 'Q-1000')
        self.assertEqual(out[0], 'Q-1000 v1 open [kit] which way')
        self.assertIn('option 1: right - go right', out)
        self.assertFalse([l for l in out if l.startswith('answer')])
        self.ok('answer', 'Q-1000', '--choice', '0', '--note', 'as planned')
        out = self.ok('questions', 'show', 'Q-1000')
        self.assertEqual((out[0], out[-2:]), ('Q-1000 v2 answered [kit] which way',
                                              ['answer: left', 'note: as planned']))
        doc = json.loads(self.ok('questions', 'show', 'Q-1000', '--json')[0])
        self.assertEqual(doc['answer']['note'], 'as planned')
        self.assertIn('REFUSED: Q-9 is not a question', self.refused('questions', 'show', 'Q-9'))

    def test_a_question_the_hub_marked_done_says_so_in_its_line_and_its_show(self):
        self.ask()
        self.ask('another')
        self.ask('a third')
        self.hub.store.update('questions', 'Q-1000', {
            'status': 'done', 'done_by': 'hub', 'done_why': 'filed into kit:EX-1',
            'done_at': '2026-10-24T00:00:00Z'})
        self.hub.store.update('questions', 'Q-1001', {
            'status': 'done', 'done_why': 'a one-off\n  go', 'done_at': '2026-10-24T00:00:00Z'})
        self.assertEqual(self.ok('questions', '--status', 'all'), [
            'Q-1000 v2 done [kit] which way (done by the hub: filed into kit:EX-1)',
            'Q-1001 v2 done [kit] another (done: a one-off go)',
            'Q-1002 v1 open [kit] a third'])
        self.assertEqual(self.ok('questions', 'show', 'Q-1000')[0],
                         'Q-1000 v2 done [kit] which way (done by the hub: filed into kit:EX-1)')

    def test_questions_show_marks_the_recommended_option_and_prints_context_and_other(self):
        self.ok('questions', 'add', '--title', 'which road', '--class', 'go', '--context-file',
                self.file('context.md', 'why it matters'), '--option', 'up::go up',
                '--option', 'down::go down', '--recommended', '1')
        out = self.ok('questions', 'show', 'Q-1000')
        self.assertIn('option 1: down (recommended) - go down', out)
        self.assertIn('option 0: up - go up', out)
        self.assertIn('context: why it matters', out)
        self.ok('answer', 'Q-1000', '--choice', 'other', '--note', 'neither')
        self.assertEqual(self.ok('questions', 'show', 'Q-1000')[-2:], ['answer: Other', 'note: neither'])

    def test_other_needs_a_note_and_a_stale_version_is_refused(self):
        self.ask()
        self.assertIn('REFUSED invalid: ', self.refused('answer', 'Q-1000', '--choice', 'other'))
        self.assertIn('REFUSED invalid: ', self.refused('answer', 'Q-1000', '--choice', '7'))
        self.assertIn('REFUSED conflict: ', self.refused('answer', 'Q-1000', '--choice', '0',
                                                         '--if-version', '99'))
        self.assertEqual(self.ok('answer', 'Q-1000', '--choice', 'other', '--note', 'neither'),
                         ['ANSWERED Q-1000 Other'])
        self.assertIn('REFUSED conflict: ', self.refused('questions', 'log', 'Q-1000',
                                                         '--if-version', '0'))
        self.assertIn('REFUSED not_found: ', self.refused('questions', 'log', 'Q-009',
                                                          '--if-version', '1'))
        self.assertIn('REFUSED not_found: ', self.refused('answer', 'Q-009', '--choice', '0'))

    def test_a_log_for_an_answer_that_changed_since_is_refused_and_nothing_is_logged(self):
        """The version is the one the caller read: an answer changed after that read is never
        logged as if it were the earlier one."""
        self.ask()
        self.assertEqual(self.ok('answer', 'Q-1000', '--choice', '0'), ['ANSWERED Q-1000 left'])
        read = self.direct('/questions?status=answered')['items'][0]['version']
        self.assertEqual(self.ok('questions', '--status', 'answered'),
                         ['Q-1000 v%d answered [kit] which way' % read])
        self.assertEqual(self.ok('answer', 'Q-1000', '--choice', '1'), ['ANSWERED Q-1000 right'])
        err = self.refused('questions', 'log', 'Q-1000', '--if-version', str(read))
        self.assertTrue(err.startswith('REFUSED conflict: '), err)
        doc = self.direct('/questions?status=all')['items'][0]
        self.assertEqual((doc['status'], doc.get('ruling'), doc['answer']['label']),
                         ('answered', None, 'right'))
        # the version of the answer as it is now logs it
        self.assertEqual(self.ok('questions', 'log', 'Q-1000', '--if-version',
                                 str(doc['version'])), ['LOGGED Q-1000'])

    def test_questions_since_lists_the_ones_written_at_or_after_it(self):
        # the PM-start digest's command (docs/pm-start.md "Filed answers")
        self.ask()
        written = self.direct('/questions')['items'][0]['updated_at']
        self.assertEqual(self.ok('questions', '--since', written),
                         ['Q-1000 v1 open [kit] which way'])
        self.assertEqual(self.ok('questions', '--since', '2999-01-01T00:00:00+00:00'), [])
        self.assertIn('REFUSED invalid: ', self.refused('questions', '--since', 'yesterday'))

    def test_a_story_set_on_a_version_it_no_longer_has_is_refused_and_not_set(self):
        story = self.file('story.md', 'As the owner I want it.\n')
        self.ok('stories', 'add', '--repo', 'kit', '--title', 'a verb', '--story-file', story)
        read = self.direct('/stories')['items'][0]['version']
        self.assertEqual(self.ok('stories'), ['S-001 v%d submitted [kit] a verb' % read])
        self.ok('stories', 'set', 'S-001', '--status', 'review', '--if-version', str(read))
        err = self.refused('stories', 'set', 'S-001', '--status', 'declined', '--if-version',
                           str(read))
        self.assertTrue(err.startswith('REFUSED conflict: '), err)
        self.assertEqual(self.direct('/stories?status=all')['items'][0]['status'], 'review')

    def test_the_questions_and_stories_lines_and_json_show_each_documents_version(self):
        self.ask()
        self.ask('and then')
        self.ok('answer', 'Q-1001', '--choice', '0')
        story = self.file('story.md', 'As the owner I want it.\n')
        self.ok('stories', 'add', '--repo', 'kit', '--title', 'a verb', '--story-file', story)
        for verb, path in (('questions', '/questions?status=all'), ('stories', '/stories?status=all')):
            docs = self.direct(path)['items']
            self.assertTrue(docs and all(isinstance(d['version'], int) for d in docs), docs)
            self.assertEqual(self.ok(verb, '--status', 'all'),
                             ['%s v%d %s [kit] %s' % (d['id'], d['version'], d['status'], d['title'])
                              for d in docs])
            got = json.loads(self.ok(verb, '--status', 'all', '--json')[0])
            self.assertEqual([(d['id'], d['version']) for d in got['items']],
                             [(d['id'], d['version']) for d in docs])
        versions = [d['version'] for d in self.direct('/questions?status=all')['items']]
        self.assertNotEqual(versions[0], versions[1], 'the two lines must not show one figure')

    def test_a_refusal_with_json_prints_the_servers_answer_too(self):
        code, out, err = self.run_cli('answer', 'Q-009', '--choice', '0', '--json', '--port',
                                      self.port)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)['error']['code'], 'not_found')
        self.assertTrue(err.startswith('REFUSED not_found: '), err)

    def test_todos_are_added_done_reopened_and_acked(self):
        self.assertEqual(self.ok('todos', 'add', '--title', 'rotate the key', '--detail', 'soon',
                                 '--command', 'tool one', '--command', 'tool two', '--repo',
                                 'kit', '--from', 'coordinator'), ['ADDED T-001'])
        self.assertEqual(self.ok('todos', 'add', '--title', 'my own', '--owner'), ['ADDED T-002'])
        docs = self.direct('/todos')['items']
        self.assertEqual([(d['from'], d.get('commands')) for d in docs],
                         [('coordinator', ['tool one', 'tool two']), ('you', None)])
        self.assertEqual(self.ok('todos'), ['T-001 open [kit] rotate the key',
                                            'T-002 open [-] my own'])
        self.assertEqual(self.ok('todos', 'done', 'T-001'), ['DONE T-001'])
        self.assertEqual(self.ok('todos', '--status', 'open'), ['T-002 open [-] my own'])
        self.assertEqual(self.ok('todos', 'ack', 'T-001'), ['ACKED T-001'])
        self.assertEqual(self.ok('todos', 'done', 'T-001', '--undo'), ['REOPENED T-001'])
        self.assertEqual(len(self.ok('todos', '--status', 'open')), 2)
        code, out, err = self.run_cli('todos', 'add', '--title', 't', '--owner', '--from', 'x')
        self.assertEqual((code, out), (2, ''))

    def test_todos_add_steps_reads_a_json_file_and_the_server_renders_it(self):
        steps = [{'open': 'C:\\w\\a.txt', 'line': 3}, {'run': 'python t.py'}]
        good = self.file('steps.json', json.dumps(steps))
        self.assertEqual(self.ok('todos', 'add', '--title', 't', '--detail', 'Why.',
                                 '--command', 'first', '--steps', good), ['ADDED T-001'])
        d = self.direct('/todos')['items'][0]
        self.assertEqual(d['commands'], ['first'] + todosteps.render(steps))
        self.assertEqual(d['detail'],
                         'Why.\n\nopen C:\\w\\a.txt at line 3 in Notepad++\nrun python t.py')
        self.assertNotIn('steps', d)
        bad = self.file('bad.json', json.dumps([{'open': 'rel.txt'}]))
        err = self.refused('todos', 'add', '--title', 't', '--steps', bad)
        self.assertTrue(err.startswith('REFUSED invalid: steps[0]'), err)
        err = self.refused('todos', 'add', '--title', 't', '--steps', self.file('x.json', '[1'))
        self.assertIn('cannot read the steps file', err)
        self.assertEqual(len(self.direct('/todos')['items']), 1)

    def test_a_question_is_edited_and_withdrawn_by_its_asker(self):
        self.ok('questions', 'add', '--title', 'which way', '--from', 'pm', '--urgent', '--class', 'go',
                '--option', 'left::go left')
        context = self.file('context.md', 'new context\n')
        self.assertEqual(self.ok('questions', 'edit', 'Q-1000', '--from', 'pm', '--if-version',
                                 '1', '--title', 'which road', '--context-file', context,
                                 '--option', 'up::go up', '--option', 'down::go down',
                                 '--recommended', '1', '--refs', 'B-3', '--no-urgent'),
                         ['EDITED Q-1000'])
        doc = self.direct('/questions')['items'][0]
        self.assertEqual((doc['title'], doc['context'], [o['label'] for o in doc['options']],
                          doc['recommended'], doc['refs'], doc['urgent'], doc['version']),
                         ('which road', 'new context\n', ['up', 'down'], 1, ['B-3'], False, 2))
        err = self.refused('questions', 'edit', 'Q-1000', '--from', 'someone', '--if-version',
                           '2', '--title', 'x')
        self.assertTrue(err.startswith('REFUSED forbidden: '), err)
        err = self.refused('questions', 'withdraw', 'Q-1000', '--from', 'pm', '--if-version', '1')
        self.assertTrue(err.startswith('REFUSED conflict: '), err)
        self.assertEqual(self.ok('questions', 'withdraw', 'Q-1000', '--from', 'pm',
                                 '--if-version', '2'), ['WITHDRAWN Q-1000'])
        self.assertEqual(self.direct('/questions?status=all')['items'], [])

    def test_a_todo_is_edited_and_withdrawn_by_its_creator(self):
        self.ok('todos', 'add', '--title', 'rotate', '--command', 'tool one', '--from', 'coord')
        self.assertEqual(self.ok('todos', 'edit', 'T-001', '--from', 'coord', '--if-version', '1',
                                 '--title', 'rotate the key', '--detail', 'soon', '--command',
                                 'tool two', '--repo', 'kit', '--refs', 'B-1', '--urgent'),
                         ['EDITED T-001'])
        doc = self.direct('/todos')['items'][0]
        self.assertEqual((doc['title'], doc['detail'], doc['commands'], doc['repo'], doc['refs'],
                          doc['urgent']),
                         ('rotate the key', 'soon', ['tool two'], 'kit', ['B-1'], True))
        err = self.refused('todos', 'withdraw', 'T-001', '--from', 'pm', '--if-version', '2')
        self.assertTrue(err.startswith('REFUSED forbidden: '), err)
        self.assertEqual(self.ok('todos', 'withdraw', 'T-001', '--from', 'coord',
                                 '--if-version', '2'), ['WITHDRAWN T-001'])
        self.assertEqual(self.direct('/todos?status=all')['items'], [])

    def test_a_record_the_hub_cannot_check_is_refused_and_nothing_is_asked(self):
        err = self.refused('questions', 'add', '--title', 't', '--record', 'kit:EX-1')
        self.assertTrue(err.startswith('REFUSED unavailable: record kit:EX-1 could not be '
                                       'checked'), err)
        self.assertEqual(self.direct('/questions')['items'], [])

    def test_edit_and_withdraw_need_from_and_a_version(self):
        for argv in (('questions', 'edit', 'Q-1000', '--if-version', '1', '--title', 't'),
                     ('questions', 'edit', 'Q-1000', '--from', 'pm', '--title', 't'),
                     ('questions', 'withdraw', 'Q-1000', '--from', 'pm'),
                     ('questions', 'withdraw', 'Q-1000', '--if-version', '1'),
                     ('todos', 'edit', 'T-001', '--from', 'pm', '--title', 't'),
                     ('todos', 'withdraw', 'T-001', '--if-version', '1')):
            code, out, err = self.run_cli(*(argv + ('--port', self.port)))
            self.assertEqual((code, out), (2, ''), argv)

    def test_stories_are_submitted_set_and_withdrawn(self):
        story = self.file('story.md', 'As the owner I want it.\n')
        self.assertEqual(self.ok('stories', 'add', '--repo', 'kit', '--title', 'a verb',
                                 '--story-file', story), ['SUBMITTED S-001'])
        self.assertEqual(self.ok('stories'), ['S-001 v1 submitted [kit] a verb'])
        self.assertEqual(self.ok('stories', 'set', 'S-001', '--status', 'review', '--reviewer',
                                 'coordinator', '--review-note', 'reading', '--if-version', '1'),
                         ['SET S-001 review'])
        self.assertEqual(self.ok('stories', 'set', 'S-001', '--status', 'added', '--backlog-id',
                                 'B-12', '--if-version', '2'), ['SET S-001 added'])
        doc = self.direct('/stories?status=all')['items'][0]
        self.assertEqual((doc['status'], doc['backlog_id'], doc['story']),
                         ('added', 'B-12', 'As the owner I want it.\n'))
        self.ok('stories', 'add', '--repo', 'kit', '--title', 'another', '--story-file', story)
        self.assertEqual(self.ok('stories', 'withdraw', 'S-002'), ['WITHDRAWN S-002'])
        self.assertEqual(self.ok('stories', '--status', 'withdrawn'),
                         ['S-002 v2 withdrawn [kit] another'])
        self.assertIn('REFUSED conflict: ', self.refused('stories', 'set', 'S-001', '--status',
                                                         'review', '--if-version', '0'))
        code, out, err = self.run_cli('stories', 'set', 'S-001', '--status', 'withdrawn',
                                      '--if-version', '3')
        self.assertEqual((code, out), (2, ''))
        self.assertIn('--status', err)          # the status is what is wrong, not a missing flag
        self.assertIn('REFUSED: cannot read the story file', self.refused(
            'stories', 'add', '--repo', 'kit', '--title', 't', '--story-file',
            os.path.join(self.home, 'absent.md')))

    def test_messages_to_the_pm_are_queued_sent_and_replied_to(self):
        self.assertEqual(self.ok('to-pm', 'send'), ['SENT nothing'])
        self.assertEqual(self.ok('to-pm', 'add', '--text', 'first', '--repo', 'kit'),
                         ['QUEUED P-001'])
        self.assertEqual(self.ok('to-pm', 'add', '--text', 'second\nline', '--urgent'),
                         ['QUEUED P-002'])
        self.assertEqual(self.ok('to-pm'), ['P-001 queued [kit] first', 'P-002 queued [-] second line'])
        self.assertEqual(self.ok('to-pm', 'send'), ['SENT P-001 P-002'])
        (code, out, err) = self.run_cli('to-pm', 'reply', 'P-001', 'done as asked', RULING)
        self.assertEqual(code, 2, 'a ruling id is no argument of reply: %r' % ((out, err),))
        self.assertEqual(self.ok('to-pm', 'reply', 'P-001', 'done as asked'),
                         ['REPLIED P-001'])
        self.assertEqual(self.ok('to-pm', 'reply', 'P-002', 'seen'), ['REPLIED P-002'])
        docs = self.direct('/to_pm?status=all')['items']
        self.assertEqual([(d['reply'], d.get('ruling')) for d in docs],
                         [('done as asked', None), ('seen', None)])

    def test_the_inbox_is_posted_to_listed_and_read(self):
        self.assertEqual(self.ok('inbox'), [])
        self.assertEqual(self.ok('inbox', 'add', '--text', 'the answer', '--title', 'about it',
                                 '--repo', 'kit', '--ref', 'P-001', '--asked', 'what'),
                         ['POSTED M-001'])
        self.assertEqual(self.ok('inbox'), ['M-001 unread [kit] about it'])
        self.assertEqual(self.ok('inbox', 'read', 'M-001'), ['READ M-001'])
        self.assertEqual(self.ok('inbox'), ['M-001 read [kit] about it'])
        self.assertEqual(self.ok('inbox', 'read', 'M-001', '--undo'), ['UNREAD M-001'])
        got = json.loads(self.ok('inbox', '--json')[0])
        self.assertEqual([(m['id'], m['read'], m['text']) for m in got['items']],
                         [('M-001', False, 'the answer')])

    def test_a_hand_off_is_recorded_and_each_sessions_newest_listed(self):
        state = os.path.join(self.home, 'state-pm.md')
        self.assertEqual(self.ok('handoffs'), [])
        self.assertEqual(self.ok('handoffs', 'add', '--kind', 'handoff', '--state', state,
                                 '--from', 'pm'), ['RECORDED C-001'])
        self.assertEqual(self.ok('handoffs', 'add', '--kind', 'stop', '--state', state,
                                 '--from', 'pm'), ['RECORDED C-002'])
        got = self.ok('handoffs')
        self.assertEqual(len(got), 1, got)
        self.assertTrue(got[0].startswith('C-002 stop from=pm at='), got)
        self.assertTrue(got[0].endswith(' ' + state), got)
        self.assertEqual(self.ok('handoffs', '--from', 'kit-9'), [])
        code, out, err = self.run_cli('handoffs', 'add', '--kind', 'stop', '--state', state,
                                      '--from', 'kit-9', '--port', self.port)
        self.assertEqual((code, out), (1, ''))
        self.assertIn('not a session the hub knows', err)
        code, out, err = self.run_cli('handoffs', 'add', '--kind', 'pause', '--state', state,
                                      '--from', 'pm')
        self.assertEqual((code, out), (2, ''))

    def test_a_page_is_published_listed_and_shown(self):
        html = '<h1>Plan</h1>\n<p>café &amp; more</p>\n'
        page = self.file('plan.html', html)
        self.assertEqual(self.ok('pages'), [])
        self.assertEqual(self.ok('pages', 'publish', 'plan', '--title', 'The plan', '--html-file',
                                 page, '--repo', 'kit', '--summary', 'what we do',
                                 '--refs', 'B-1', '--from', 'coord'), ['PUBLISHED plan'])
        self.assertEqual(self.ok('pages'), ['plan v1 - [kit] The plan'])
        card = self.direct('/pages')['items'][0]
        self.assertEqual((card['summary'], card['refs'], card['bytes']),
                         ('what we do', ['B-1'], len(html.encode('utf-8'))))
        out = os.path.join(self.home, 'out.html')
        self.assertEqual(self.ok('pages', 'show', 'plan', '--out', out), ['WROTE %s' % out])
        with open(out, encoding='utf-8', newline='') as f:
            self.assertEqual(f.read(), html)
        self.assertEqual(json.loads(self.ok('pages', 'show', 'plan', '--json')[0])['html'], html)
        # over a published page: its version, or a conflict
        err = self.refused('pages', 'publish', 'plan', '--title', 'again', '--html-file', page)
        self.assertTrue(err.startswith('REFUSED conflict: '), err)
        self.assertEqual(self.ok('pages', 'publish', 'plan', '--title', 'again', '--html-file',
                                 page, '--if-version', '1'), ['PUBLISHED plan'])
        err = self.refused('pages', 'show', 'absent')
        self.assertTrue(err.startswith('REFUSED not_found: '), err)

    def test_a_page_the_check_refuses_is_never_sent(self):
        bad = self.file('bad.html', '<p>x</p><script>alert(1)</script>')
        err = self.refused('pages', 'publish', 'plan', '--title', 't', '--html-file', bad)
        self.assertTrue(err.startswith('REFUSED: the page check refuses '), err)
        self.assertIn('<script>', err)
        mermaid = self.file('m.html', '<div class="mermaid">graph TD; A-->B</div>')
        self.assertIn('render it to svg first', self.refused(
            'pages', 'publish', 'plan', '--title', 't', '--html-file', mermaid))
        self.assertIn('cannot read the html file', self.refused(
            'pages', 'publish', 'plan', '--title', 't', '--html-file',
            os.path.join(self.home, 'absent.html')))
        self.assertEqual(self.direct('/pages')['items'], [])

    def test_pages_has_no_delete(self):
        code, out, err = self.run_cli('pages', 'delete', 'plan', '--port', self.port)
        self.assertEqual((code, out), (2, ''))

    def test_a_listed_title_is_one_line_with_no_user_or_password_of_a_url(self):
        self.ok('todos', 'add', '--owner', '--title',
                'clone https://someone:s3cret@git.example.test/x\n  then build')
        self.assertEqual(self.ok('todos'),
                         ['T-001 open [-] clone https://git.example.test/x then build'])
        self.assertNotIn('s3cret', self.ok('todos', '--json')[0])

    def test_a_write_says_each_warning_of_the_document_it_wrote(self):
        self.assertEqual(self.ask(), ['ASKED Q-1000'])
        self.assertEqual(len(self.warned), 1, self.warned)
        self.assertTrue(self.warned[0].startswith('WARN questions/Q-1000 repo: '), self.warned)
        self.assertTrue(self.warned[0].endswith('[repo-alias]'), self.warned)


class SkillEditCase(VerbCase):
    """`skill-edit`: an agent shows the owner's text, commits it and says done or fail; the blob
    it sends is read from its own checkout, and the hub reads the branch again itself."""
    OLD = '---\nname: builder\n---\nold\n'
    NEW = '---\nname: builder\n---\nnew\n'

    def setUp(self):
        VerbCase.setUp(self)
        from kitlib.hubsite import skilledits
        from tests.gitfixture import TempRepo
        self.repo = TempRepo()
        self.addCleanup(self.repo.cleanup)
        src = 'content/skills/builder/SKILL.md'
        self.repo.write(src, self.OLD)
        self.repo.commit('old', src)
        self.repo.git('checkout', '-q', '-b', 'kit/edit')
        self.repo.write(src, self.NEW)
        self.repo.commit('new', src)
        self.repo.git('checkout', '-q', 'main')
        self.blob = lambda t: skilledits.git_blob(t.encode('utf-8'))
        self.hub.api.kit_blob = lambda branch, path: self.repo.git(
            'rev-parse', '--verify', '--quiet', '%s:%s' % (branch, path), check=False) or None
        self.assertEqual(self.hub.store.add('skill_edits', {
            'status': 'submitted', 'skill': 'builder', 'text': self.NEW,
            'base_blob': self.blob(self.OLD)}).id, 'E-001')

    def item(self):
        return self.direct('/skill_edits/E-001')['item']

    def test_show_lists_and_writes_the_owners_text_byte_for_byte(self):
        self.assertEqual(self.ok('skill-edit'), ['E-001 v1 submitted skill builder'])
        self.assertEqual(self.ok('skill-edit', 'show', 'E-001'),
                         ['E-001 v1 submitted skill builder base ' + self.blob(self.OLD)])
        out = os.path.join(self.home, 'edit.md')
        self.assertEqual(self.ok('skill-edit', 'show', 'E-001', '--out', out), ['WROTE ' + out])
        with open(out, 'rb') as f:
            self.assertEqual(f.read(), self.NEW.encode('utf-8'))

    def test_done_sends_the_branchs_blob_and_the_hub_takes_it(self):
        self.assertEqual(self.ok('skill-edit', 'done', 'E-001', '--branch', 'kit/edit',
                                 '--checkout', self.repo.root, '--if-version', '1'),
                         ['COMMITTED E-001'])
        d = self.item()
        self.assertEqual((d['status'], d['branch'], d['blob']),
                         ('committed', 'kit/edit', self.blob(self.NEW)))

    def test_done_on_a_branch_without_the_text_is_refused_and_nothing_changes(self):
        err = self.refused('skill-edit', 'done', 'E-001', '--branch', 'main', '--checkout',
                           self.repo.root, '--if-version', '1')
        self.assertIn('blob', err)
        self.assertEqual(self.item()['status'], 'submitted')

    def test_fail_at_a_main_that_moved_on_supersedes_the_edit(self):
        src = 'content/skills/builder/SKILL.md'
        self.repo.write(src, self.OLD + 'later\n')
        self.repo.commit('later', src)
        self.assertEqual(self.ok('skill-edit', 'fail', 'E-001', '--why', 'the skill changed on main',
                                 '--at', 'main', '--checkout', self.repo.root, '--if-version', '1'),
                         ['SUPERSEDED E-001'])
        d = self.item()
        self.assertEqual((d['status'], d['source_blob']),
                         ('superseded', self.blob(self.OLD + 'later\n')))


class ForeignCase(CliCase):
    def test_tokens_that_are_not_the_servers_are_refused_by_the_server(self):
        other = self.server(tokens=os.path.join(self.home, 'other-tokens'))
        self.assertEqual(self.run_cli('setup')[0], 0)
        code, out, err = self.run_cli('pending', '--port', str(other.port))
        self.assertEqual((code, out), (1, ''))
        self.assertTrue(err.startswith('REFUSED forbidden: '), err)


class WaitVerbCase(VerbCase):
    def test_a_wait_with_nothing_is_exit_4_and_prints_nothing(self):
        code, out, err = self.run_cli('wait', '--timeout', '0', '--port', self.port)
        self.assertEqual((code, out, err), (4, '', ''))
        code, out, err = self.run_cli('wait', '--timeout', '0', '--pending', '--json', '--port',
                                      self.port)
        self.assertEqual((code, err), (4, ''))
        self.assertIs(json.loads(out)['timed_out'], True)

    def test_a_wait_prints_each_change_and_each_pending_item(self):
        was = self.hub.store.revision
        self.ask()
        now = self.hub.store.revision
        self.assertEqual(self.ok('wait', '--since', str(was), '--timeout', '5'),
                         ['HUB %d questions Q-1000 %s open' % (
                             now, self.direct('/wait?since=%d&timeout=0' % was)['changes'][0]['op'])])
        self.ok('answer', 'Q-1000', '--choice', '0')
        items = self.direct('/pending')['items']
        self.assertEqual([i['id'] for i in items], ['Q-1000'])
        self.assertEqual(self.ok('wait', '--pending', '--timeout', '5'),
                         ['HUB %d %s Q-1000 to=pm from=pm which way' % (self.hub.store.revision,
                                                                       items[0]['kind'])])

    def test_a_wait_argument_that_is_no_number_is_a_usage_error(self):
        for argv in (('wait', '--timeout', 'soon'), ('wait', '--timeout', '-1'),
                     ('wait', '--since', '-1'), ('wait', '--since', 'x'),
                     ('wait', '--timeout', 'nan'), ('wait', '--timeout', 'inf')):
            code, out, err = self.run_cli(*argv)
            self.assertEqual((code, out), (2, ''), argv)
            self.assertIn('USAGE: ', err)


class DirectQuestionCase(VerbCase):
    """questions add --from <session key>, pending --for and wait --pending --for, with the
    session keys read from the temp home's launcher sessions.json."""

    def setUp(self):
        VerbCase.setUp(self)
        state = os.path.join(self.home, 'launcher-state')
        os.makedirs(state)
        self.config({'launcher': {'state_dir': state}})
        with open(os.path.join(state, 'sessions.json'), 'w', encoding='utf-8') as f:
            json.dump({'_note': 'x', 'kit': {'repo': 'kit'}, 'other': {'repo': 'other'}}, f)

    def asks(self, sender, title):
        return self.ok('questions', 'add', '--title', title, '--repo', 'kit', '--from', sender,
                       '--class', 'go',
                       '--option', 'a::b', '--option', 'c::d')[0].split()[1]

    def test_a_question_from_a_launcher_key_comes_back_to_it_alone(self):
        mine, theirs, pms = self.asks('kit', 'mine'), self.asks('other', 'theirs'), \
            self.ask('pms')[0].split()[1]
        for qid in (mine, theirs, pms):
            self.ok('answer', qid, '--choice', '0')
        got = json.loads('\n'.join(self.ok('pending', '--json', '--for', 'kit')))
        self.assertEqual([(i['id'], i['from']) for i in got['items']], [(mine, 'kit')])
        # the PM's list holds every answered question, each to be logged, whoever asked it
        self.assertEqual(sorted(line.split()[1] for line in self.ok('pending', '--for', 'pm')
                                if not line.startswith('AUDIT ')),      # the classed ones
                         sorted([mine, theirs, pms]))
        self.assertEqual(sorted(line.split()[1] for line in self.ok('pending')),
                         sorted([mine, theirs, pms]))
        self.assertEqual(self.ok('wait', '--pending', '--for', 'other', '--timeout', '5'),
                         ['HUB %d %s %s to=other from=other theirs' % (
                             self.hub.store.revision, got['items'][0]['kind'], theirs)])

    def test_pending_and_wait_lines_say_who_handles_each_item_and_whose_it_is(self):
        mine, pms = self.asks('kit', 'mine'), self.ask('pms')[0].split()[1]
        for qid in (mine, pms):
            self.ok('answer', qid, '--choice', '0')
        kind = self.direct('/pending')['items'][0]['kind']
        # kit has never waited: the PM handles its answer too
        self.assertEqual(sorted(self.ok('pending')),
                         sorted(['%s %s [kit] to=pm from=kit class=go mine' % (kind, mine),
                                 '%s %s [kit] to=pm from=pm class=go pms' % (kind, pms)]))
        rev = self.hub.store.revision
        self.assertEqual(self.ok('wait', '--pending', '--for', 'kit', '--timeout', '5'),
                         ['HUB %d %s %s to=kit from=kit mine' % (rev, kind, mine)])
        # handed over to kit: kit handles it now, and the PM still logs it
        self.assertEqual(sorted(self.ok('wait', '--pending', '--for', 'pm', '--timeout', '5')),
                         sorted(['HUB %d %s %s to=kit from=kit mine' % (rev, kind, mine),
                                 'HUB %d %s %s to=pm from=pm pms' % (rev, kind, pms)]))

    def test_a_wait_for_a_key_with_nothing_for_it_times_out(self):
        self.ok('answer', self.asks('other', 'theirs'), '--choice', '0')
        code, out, err = self.run_cli('wait', '--pending', '--for', 'kit', '--timeout', '0',
                                      '--port', self.port)
        self.assertEqual((code, out, err), (4, '', ''))

    def test_a_question_from_an_unknown_key_is_refused(self):
        err = self.refused('questions', 'add', '--title', 't', '--from', 'coordinator', '--class', 'go',
                           '--option', 'a::b', '--option', 'c::d')
        self.assertTrue(err.startswith('REFUSED invalid: '), err)
        self.assertIn('known: kit, other, pm', err)
        self.assertNotIn('sessions.json', err)
        self.assertEqual(self.direct('/questions')['items'], [])

    def test_for_without_pending_is_a_usage_error(self):
        code, out, err = self.run_cli('wait', '--for', 'kit', '--timeout', '0',
                                      '--port', self.port)
        self.assertEqual((code, out), (2, ''))
        self.assertIn('USAGE: --for picks among the pending items', err)

    def test_except_leaves_out_an_acted_on_answer_until_another_comes(self):
        old, new = self.asks('kit', 'old'), self.asks('kit', 'new')
        self.ok('answer', old, '--choice', '0')
        code, out, err = self.run_cli('wait', '--pending', '--for', 'kit', '--except', old,
                                      '--timeout', '0', '--port', self.port)
        self.assertEqual((code, out, err), (4, '', ''))
        self.ok('answer', new, '--choice', '0')
        got = self.ok('wait', '--pending', '--for', 'kit', '--except', old, 'Q-0', '--timeout', '5')
        self.assertEqual([line.split()[3] for line in got], [new])

    def test_except_without_pending_is_a_usage_error(self):
        code, out, err = self.run_cli('wait', '--except', 'Q-1', '--timeout', '0',
                                      '--port', self.port)
        self.assertEqual((code, out), (2, ''))
        self.assertIn('USAGE: --except leaves out pending items', err)


class RecordArgumentCase(CliCase):
    """questions add and edit --record REPO:ID and edit --no-record: what each sends the hub,
    and the forms that are a usage error before anything is sent."""

    def setUp(self):
        CliCase.setUp(self)
        self.assertEqual(self.run_cli('setup')[0], 0)
        self.sent = []

    def ask(self, port, method, path, token=None, body=None, *more, **kw):
        self.sent.append(body)
        return 200, {'ok': True, 'id': 'Q-1000', 'version': 2, 'revision': 3, 'warnings': []}

    def run_asking(self, *argv):
        with mock.patch.object(serve, 'ask', self.ask):
            return self.run_cli(*(argv + ('--port', '8791')))

    def test_record_is_sent_as_repo_and_id(self):
        for argv, line in ((('questions', 'add', '--title', 't', '--record', 'pm:KIT-A:B'),
                            'ASKED Q-1000'),
                           (('questions', 'edit', 'Q-1000', '--from', 'pm', '--if-version', '1',
                             '--record', ' kit : EX-1 '), 'EDITED Q-1000')):
            with self.subTest(argv=argv):
                code, out, err = self.run_asking(*argv)
                self.assertEqual((code, out.splitlines(), err), (0, [line], ''))
        self.assertEqual([b['record'] for b in self.sent],
                         [{'repo': 'pm', 'id': 'KIT-A:B'}, {'repo': 'kit', 'id': 'EX-1'}])

    def test_no_record_sends_null_and_no_flag_sends_nothing(self):
        code, out, err = self.run_asking('questions', 'edit', 'Q-1000', '--from', 'pm',
                                         '--if-version', '1', '--no-record')
        self.assertEqual((code, err), (0, ''))
        self.assertIn('record', self.sent[-1])
        self.assertIsNone(self.sent[-1]['record'])
        for argv in (('questions', 'add', '--title', 't'),
                     ('questions', 'edit', 'Q-1000', '--from', 'pm', '--if-version', '1',
                      '--title', 'x')):
            self.assertEqual(self.run_asking(*argv)[0], 0)
            self.assertNotIn('record', self.sent[-1])

    def test_a_record_that_is_not_repo_colon_id_or_both_forms_is_a_usage_error(self):
        for argv in (('questions', 'add', '--title', 't', '--record', 'EX-1'),
                     ('questions', 'add', '--title', 't', '--record', ':EX-1'),
                     ('questions', 'add', '--title', 't', '--record', 'kit:'),
                     ('questions', 'add', '--title', 't', '--record', ' : '),
                     ('questions', 'add', '--title', 't', '--no-record'),
                     ('questions', 'edit', 'Q-1000', '--from', 'pm', '--if-version', '1',
                      '--record', 'kit:EX-1', '--no-record')):
            with self.subTest(argv=argv):
                code, out, err = self.run_asking(*argv)
                self.assertEqual((code, out), (2, ''))
                self.assertIn('USAGE: ', err)
        self.assertEqual(self.sent, [])

    def test_questions_bind_needs_a_mode_and_prints_what_the_hub_answers(self):
        code, out, err = self.run_asking('questions', 'bind')
        self.assertEqual((code, out), (2, ''))
        self.assertIn('USAGE: ', err)
        self.assertEqual(self.run_asking('questions', 'bind', '--dry-run', '--apply')[0], 2)
        self.assertEqual(self.sent, [])
        answer = {'ok': True, 'pairs': [{'question': 'Q-1000', 'bound': True,
                                         'record': {'repo': 'kit', 'id': 'EX-1'}},
                                        {'question': 'Q-1005', 'bound': True,
                                         'record': {'repo': 'kit', 'id': 'EX-5'},
                                         'filing': {'filed': True}},
                                        {'question': 'Q-1006', 'bound': True,
                                         'record': {'repo': 'kit', 'id': 'EX-6'},
                                         'filing': {'filed': False, 'code': 'store',
                                                    'message': 'busy'}},
                                        {'question': 'Q-1007', 'bound': True,
                                         'record': {'repo': 'kit', 'id': 'EX-7'},
                                         'filing': {'filed': False, 'moved': True}},
                                        {'question': 'Q-1008', 'bound': True,
                                         'record': {'repo': 'kit', 'id': 'EX-8'},
                                         'filing': {'filed': False, 'unanswered': True}}],
                  'failed': [{'question': 'Q-1001', 'code': 'not_found', 'message': 'gone'}],
                  'misses': [{'record': {'repo': 'kit', 'id': 'EX-3'}, 'why': 'cites no question'},
                             {'record': {'repo': 'down', 'id': None},
                              'why': 'the repository could not be read'}],
                  'multi': [{'record': {'repo': 'kit', 'id': 'EX-4'},
                             'questions': ['Q-1002', 'Q-1003']}],
                  'skipped': [{'question': 'Q-1004', 'why': 'bound to another record'}]}
        self.ask = lambda port, method, path, token=None, body=None, *m, **kw: (
            self.sent.append((path, body)) or (200, answer))
        code, out, err = self.run_asking('questions', 'bind', '--apply')
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(out.splitlines(), [
            'BOUND Q-1000 kit:EX-1', 'BOUND Q-1005 kit:EX-5', 'FILED Q-1005',
            'BOUND Q-1006 kit:EX-6', 'NOT FILED Q-1006: busy (store)',
            'BOUND Q-1007 kit:EX-7', 'NOT FILED Q-1007: moved (filed by the answer handler)',
            'BOUND Q-1008 kit:EX-8', 'NOT FILED Q-1008: no longer answered',
            'NOT BOUND Q-1001: gone (not_found)',
            'MISS kit:EX-3: cites no question', 'MISS down: the repository could not be read',
            'MULTI kit:EX-4 cites Q-1002 Q-1003',
            'SKIP Q-1004: bound to another record'])
        self.assertTrue(self.sent[-1][0].endswith('/questions/bind'))
        self.assertEqual(self.sent[-1][1], {'from': 'pm', 'apply': True})


class ArgumentCase(CliCase):
    VERBS =(('questions',), ('questions', 'add', '--title', 't'),
             ('questions', 'show', 'Q-001'), ('questions', 'log', 'Q-001', '--if-version', '1'),
             ('answer', 'Q-001', '--choice', '0'),
             ('todos',), ('todos', 'add', '--title', 't'), ('todos', 'done', 'T-001'),
             ('todos', 'ack', 'T-001'), ('stories',),
             ('stories', 'set', 'S-001', '--status', 'review', '--if-version', '1'),
             ('stories', 'withdraw', 'S-001'), ('to-pm',), ('to-pm', 'add', '--text', 't'),
             ('to-pm', 'send'), ('to-pm', 'reply', 'P-001', 'text'), ('inbox',),
             ('inbox', 'add', '--text', 't'), ('inbox', 'read', 'M-001'), ('pages',),
             ('pages', 'show', 'plan'), ('approvals',),
             ('approvals', 'show', 'A-001'), ('handoffs',),
             ('handoffs', 'add', '--kind', 'stop', '--state', 'C:/s.md', '--from', 'pm'),
             ('skill-edit',), ('skill-edit', 'show', 'E-001'),
             ('skill-edit', 'fail', 'E-001', '--why', 'w', '--if-version', '1'),
             ('pending',), ('wait', '--timeout', '0'))

    def test_every_verb_that_asks_is_exit_3_with_the_start_command_when_no_server_runs(self):
        self.assertEqual(self.run_cli('setup')[0], 0)
        def down(port, *args, **kw):            # a real refused connect is slow here (test_client.DownCase)
            raise serve.NotRunning(port)

        with mock.patch.object(serve, 'ask', down):
            for argv in self.VERBS:
                for more in ((), ('--json',)):
                    code, out, err = self.run_cli(*(argv + more + ('--port', '8791')))
                    self.assertEqual((code, out), (3, ''), argv)
                    self.assertIn('port 8791', err)
                    self.assertIn(HUB.START, err)
        # each verb of the tool is in that list, but for those that ask no running server
        self.assertEqual(sorted({v[0] for v in self.VERBS}),
                         sorted(set(HUB.VERB_NAMES)
                                - {'serve', 'status', 'setup', 'open', 'validate',
                                   'unpair',    # unpair with no server: ClientCase
                                   'stop', 'restart'}))     # with no server: StopRestartCase

    def test_the_port_is_the_configured_one_unless_given(self):
        self.assertEqual(self.run_cli('setup')[0], 0)
        self.config({'hub': {'port': 8793}})
        asked = []

        def down(port, *args, **kw):
            asked.append(port)
            raise serve.NotRunning(port)

        with mock.patch.object(serve, 'ask', down):
            self.assertEqual(self.run_cli('pending')[0], 3)
            self.assertEqual(self.run_cli('questions', '--port', '8794')[0], 3)
            self.assertEqual(self.run_cli('questions', '--port', '8794', 'log', 'Q-001',
                                          '--if-version', '1')[0], 3)
            self.assertEqual(self.run_cli('todos', 'ack', 'T-001', '--port', '8795')[0], 3)
        self.assertEqual(asked, [8793, 8794, 8794, 8795])

    def sent(self, *argv):
        """The (method, path, body) of each call a verb made to a server that says yes."""
        seen = []

        def ask(port, method, path, token=None, body=None, timeout=None):
            seen.append((method, path[len(HUB.api.BASE):], body))
            return 200, {'ok': True, 'id': 'X-1'}

        with mock.patch.object(serve, 'ask', ask):
            got = self.run_cli(*(argv + ('--port', '8791')))
        return got, seen

    def test_a_repo_given_before_add_is_sent_as_one_given_after_it(self):
        self.assertEqual(self.run_cli('setup')[0], 0)
        for verb, path, rest in (('questions', '/questions', ('--title', 't')),
                                 ('todos', '/todos', ('--title', 't')),
                                 ('to-pm', '/to_pm', ('--text', 't'))):
            for argv in ((verb, '--repo', 'kit', 'add') + rest, (verb, 'add', '--repo', 'kit') + rest,
                         (verb, 'add') + rest + ('--repo', 'kit'),
                         (verb, '--repo', 'other', 'add', '--repo', 'kit') + rest):
                with self.subTest(argv=argv):
                    (code, out, err), seen = self.sent(*argv)
                    self.assertEqual((code, err), (0, ''), out)
                    self.assertEqual([(m, p, b.get('repo')) for m, p, b in seen],
                                     [('POST', path, 'kit')])
            with self.subTest(verb=verb, repo='none'):
                (code, out, err), seen = self.sent(verb, 'add', *rest)
                self.assertEqual((code, err), (0, ''), out)
                self.assertEqual([(m, p, 'repo' in b) for m, p, b in seen], [('POST', path, False)])

    def test_questions_log_and_stories_set_need_the_version_and_ask_nothing_without_it(self):
        self.assertEqual(self.run_cli('setup')[0], 0)
        for argv in (('questions', 'log', 'Q-001'),
                     ('stories', 'set', 'S-001', '--status', 'review'),
                     ('questions', 'log', 'Q-001', '--json')):
            with self.subTest(argv=argv):
                (code, out, err), seen = self.sent(*argv)
                self.assertEqual((code, out), (2, ''), err)
                self.assertIn('USAGE: ', err)
                self.assertIn('--if-version', err.split('USAGE: ')[1])
                self.assertEqual(seen, [], 'a verb with no version asked the server')
        # with it, the one call is the write, with the version that was given: nothing is read first
        (code, out, err), seen = self.sent('questions', 'log', 'Q-001', '--if-version', '4')
        self.assertEqual((code, err), (0, ''), out)
        self.assertEqual(seen, [('POST', '/questions/Q-001/log', {'if_version': 4})])
        for argv, path, body in (
                (('questions', 'add', '--title', 't', '--class', 'cross-repo'), '/questions',
                 {'title': 't', 'class': 'cross-repo'}),
                (('questions', 'edit', 'Q-001', '--from', 'pm', '--class', 'rule',
                  '--no-record', '--if-version', '2'), '/questions/Q-001/edit',
                 {'from': 'pm', 'class': 'rule', 'record': None, 'if_version': 2})):
            with self.subTest(argv=argv):
                (code, out, err), seen = self.sent(*argv)
                self.assertEqual((code, err), (0, ''), out)
                self.assertEqual(seen, [('POST', path, body)])
        self.assertEqual(self.run_cli('questions', 'add', '--title', 't', '--class', 'other',
                                      '--port', '8791')[0], 2)
        for verb, more, body in (('log', (), {}),
                                 ('done', ('--why', 'w'), {'why': 'w'})):
            with self.subTest(verb=verb):
                (code, out, err), seen = self.sent('questions', verb, 'Q-001', *more,
                                                   '--unfiled', 'the record is gone',
                                                   '--if-version', '4')
                self.assertEqual((code, err), (0, ''), out)
                self.assertEqual(seen, [('POST', '/questions/Q-001/' + verb,
                                         dict(body, if_version=4,
                                              unfiled='the record is gone'))])
        (code, out, err), seen = self.sent('stories', 'set', 'S-001', '--status', 'review',
                                           '--if-version', '0')
        self.assertEqual((code, err), (0, ''), out)
        self.assertEqual(seen, [('POST', '/stories/S-001', {'status': 'review', 'if_version': 0})])

    def test_the_help_says_that_a_place_counts_from_0(self):
        for argv, flag in ((('questions', 'add', '--help'), '--recommended'),
                           (('answer', '--help'), '--choice')):
            code, out, err = self.run_cli(*argv)
            self.assertEqual((code, err), (0, ''), argv)
            said = ' '.join(out.split())
            about = said[said.rindex(flag + ' '):]          # the flag's own line, past the usage
            self.assertIn('(counts from 0)', about[:about.index(' --', 2)], (argv, about))

    def test_an_argument_that_is_missing_or_wrong_is_a_usage_error(self):
        for argv in (('answer', 'Q-001'), ('answer', '--choice', '0'),
                     ('answer', 'Q-001', '--choice', 'first'), ('answer', 'Q-001', '--choice', '-1'),
                     ('questions', 'add'), ('questions', 'add', '--title', 't', '--option', 'bare'),
                     ('questions', 'add', '--title', 't', '--option', '::no label'),
                     ('questions', 'add', '--title', 't', '--recommended', 'x'),
                     ('questions', 'log', 'Q-001'), ('questions', 'close', 'Q-001'),
                     ('questions', '--status', 'nonsense'), ('todos', 'add'), ('todos', 'done'),
                     ('todos', 'ack'), ('stories', 'add', '--title', 't', '--story-file', 'f'),
                     ('stories', 'add', '--repo', 'r', '--title', 't'), ('stories', 'set', 'S-001'),
                     ('to-pm', 'add'), ('to-pm', 'reply', 'P-001'), ('inbox', 'add'),
                     ('inbox', 'read'), ('pending', 'now'), ('pending', '--port', '0'),
                     ('answer', 'Q-001', '--choice', '0', '--if-version', 'x'),
                     ('validate', '--store', ''), ('validate', '--port', '8790')):
            code, out, err = self.run_cli(*argv)
            self.assertEqual((code, out), (2, ''), argv)
            self.assertIn('USAGE: ', err)

    def test_a_verb_with_no_token_file_is_refused_before_it_asks(self):
        asked = []
        with mock.patch.object(serve, 'ask', lambda *a, **kw: asked.append(a)):
            code, out, err = self.run_cli('pending', '--port', '8791')
            self.assertEqual((code, out), (1, ''))
            self.assertIn('REFUSED: no agent or owner token in %s: run hub.py setup'
                          % self.tokens, err)
            os.makedirs(self.tokens)
            auth.ensure_tokens(self.tokens)
            os.remove(os.path.join(self.tokens, auth.TOKEN_FILES[auth.OWNER]))
            code, out, err = self.run_cli('answer', 'Q-001', '--choice', '0', '--port', '8791')
            self.assertEqual((code, out), (1, ''))
            self.assertIn('REFUSED: no owner token', err)
        self.assertEqual(asked, [])

    def test_the_owner_verbs_carry_the_owners_token_and_the_others_the_agents(self):
        self.assertEqual(self.run_cli('setup')[0], 0)
        owner, agent = (auth.read_token(self.tokens, r) for r in (auth.OWNER, auth.AGENT))
        seen = []
        self.page = os.path.join(self.home, 'page.html')
        with open(self.page, 'w', encoding='utf-8') as f:
            f.write('<h1>plan</h1>')

        def ask(port, method, path, token=None, body=None, timeout=None):
            seen.append((method, path, {owner: 'owner', agent: 'agent'}[token]))
            return 200, {'ok': True, 'id': 'X-1', 'items': [{'id': 'Q-001', 'version': 3},
                                                             {'id': 'S-001', 'version': 4}]}

        base = HUB.api.BASE
        with mock.patch.object(serve, 'ask', ask):
            for argv, want in (
                    (('answer', 'Q-001', '--choice', '0'), ('POST', '/questions/Q-001/answer', 'owner')),
                    (('todos', 'done', 'T-001'), ('POST', '/todos/T-001/done', 'owner')),
                    (('todos', 'add', '--title', 't', '--owner'), ('POST', '/todos', 'owner')),
                    (('todos', 'add', '--title', 't'), ('POST', '/todos', 'agent')),
                    (('todos', 'ack', 'T-001'), ('POST', '/todos/T-001/ack', 'agent')),
                    (('stories', 'add', '--repo', 'r', '--title', 't', '--story-file', __file__),
                     ('POST', '/stories', 'owner')),
                    (('stories', 'withdraw', 'S-001'), ('POST', '/stories/S-001/withdraw', 'owner')),
                    (('stories', 'set', 'S-001', '--status', 'review', '--if-version', '4'),
                     ('POST', '/stories/S-001', 'agent')),
                    (('to-pm', 'add', '--text', 't'), ('POST', '/to_pm', 'owner')),
                    (('to-pm', 'send'), ('POST', '/to_pm/send', 'owner')),
                    (('to-pm', 'reply', 'P-001', 'text'), ('POST', '/to_pm/P-001/reply', 'agent')),
                    (('inbox', 'add', '--text', 't'), ('POST', '/inbox', 'agent')),
                    (('inbox', 'read', 'M-001'), ('POST', '/inbox/M-001/read', 'owner')),
                    (('handoffs', 'add', '--kind', 'stop', '--state', 'C:/s.md', '--from',
                      'pm'), ('POST', '/handoffs', 'agent')),
                    (('handoffs', '--from', 'pm'), ('GET', '/handoffs?from=pm', 'agent')),
                    (('questions', 'add', '--title', 't'), ('POST', '/questions', 'agent')),
                    (('questions', 'log', 'Q-001', '--if-version', '3'),
                     ('POST', '/questions/Q-001/log', 'agent')),
                    (('questions',), ('GET', '/questions', 'agent')),
                    (('inbox',), ('GET', '/snapshot', 'agent')),
                    (('pages', 'publish', 'plan', '--title', 't', '--html-file', self.page),
                     ('POST', '/pages', 'agent')),
                    (('pages',), ('GET', '/pages', 'agent')),
                    (('pages', 'show', 'plan'), ('GET', '/pages/plan', 'agent')),
                    (('pending',), ('GET', '/pending', 'agent')),
                    (('wait', '--timeout', '0'), ('GET', '/wait?timeout=0', 'agent'))):
                del seen[:]
                code, out, err = self.run_cli(*(argv + ('--port', '8791')))
                self.assertEqual((code, err), (0, ''), argv)
                self.assertEqual(seen[-1], (want[0], base + want[1], want[2]), argv)

    def test_an_id_cannot_leave_its_place_in_the_path(self):
        self.assertEqual(self.run_cli('setup')[0], 0)
        seen = []

        def ask(port, method, path, token=None, body=None, timeout=None):
            seen.append(path)
            return 200, {'ok': True}

        with mock.patch.object(serve, 'ask', ask):
            self.assertEqual(self.run_cli('todos', 'ack', '../stories/S-001?x=1#y', '--port',
                                          '8791')[0], 0)
        self.assertEqual(seen, [HUB.api.BASE + '/todos/..%2Fstories%2FS-001%3Fx%3D1%23y/ack'])


class ValidateCase(CliCase):
    def test_validate_reads_the_store_beside_a_running_server_and_writes_nothing(self):
        hub = self.server()
        self.assertEqual(self.run_cli('todos', 'add', '--owner', '--title', 't', '--port',
                                      str(hub.port))[0], 0)

        def tree():
            return sorted((os.path.join(d, n), os.path.getmtime(os.path.join(d, n)))
                          for d, _, names in os.walk(self.store_dir) for n in names)

        was = tree()
        self.assertTrue(self.locked(), 'the server does not hold its store')
        code, out, err = self.run_cli('validate', '--store', self.store_dir)
        self.assertEqual((code, out, err), (0, 'VALIDATE clean: no finding\n', ''))
        self.assertEqual(tree(), was)
        self.config({'hub': {'store': self.store_dir}})     # the configured store, by default
        self.assertEqual(self.run_cli('validate'), (0, 'VALIDATE clean: no finding\n', ''))

    def test_a_store_with_an_error_finding_is_exit_1_with_each_finding(self):
        hub = self.server()
        self.assertEqual(self.run_cli('todos', 'add', '--owner', '--title', 't', '--port',
                                      str(hub.port))[0], 0)
        hub.close()
        path = os.path.join(self.store_dir, 'todos', 'T-001.json')
        with open(path, encoding='utf-8') as f:
            doc = json.load(f)
        doc['status'] = 'someday'
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(doc, f)
        code, out, err = self.run_cli('validate', '--store', self.store_dir)
        self.assertEqual((code, err), (1, ''))
        lines = out.splitlines()
        self.assertTrue(all(line.startswith('VALIDATE ') for line in lines), out)
        self.assertTrue(any(line.startswith('VALIDATE ERROR todos/T-001 status: ')
                            for line in lines), out)

    def test_a_text_recommended_that_names_an_option_is_exit_0_with_its_warning(self):
        hub = self.server()
        self.assertEqual(self.run_cli('questions', 'add', '--title', 'which way', '--class', 'go', '--option',
                                      'left::go left', '--option', 'right::go right',
                                      '--recommended', '0', '--port', str(hub.port))[0], 0)
        hub.close()
        path = os.path.join(self.store_dir, 'questions', 'Q-1000.json')
        with open(path, encoding='utf-8') as f:
            doc = json.load(f)
        doc['recommended'] = 'right'            # a legacy file: the option's label, not its place
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(doc, f)
        code, out, err = self.run_cli('validate', '--store', self.store_dir)
        self.assertEqual((code, err), (0, ''), out)
        lines = out.splitlines()
        self.assertIn("VALIDATE WARN questions/Q-1000 recommended: recommended is the text "
                      "'right' in its file; the store reads it as the number 1 and its next "
                      "write stores the number [%s]" % schema.CODE, lines)
        self.assertFalse(any(line.startswith('VALIDATE ERROR') for line in lines), out)

    def test_validate_of_no_store_is_refused(self):
        code, out, err = self.run_cli('validate')
        self.assertEqual((code, out), (1, ''))
        self.assertIn('REFUSED: no store folder is set', err)
        code, out, err = self.run_cli('validate', '--store', os.path.join(self.home, 'absent'))
        self.assertEqual((code, out), (1, ''))
        self.assertIn('REFUSED: the hub store ', err)
        self.assertFalse(os.path.exists(os.path.join(self.home, 'absent')))


if __name__ == '__main__':
    unittest.main()
