"""tools/hub.py: its arguments and exit codes. The machine config, the token files and the store
are all under a temp home; a server is on port 0 or is one this test built; no browser is
opened."""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import socket
import tempfile
import threading
import unittest
from unittest import mock

from kitlib.hubsite import auth, httpd, serve
from kitlib.hubsite import settings as hub_settings
from kitlib.hubsite.store import Store, StoreBusy
from tests.hubsite import httpfix

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load():
    spec = importlib.util.spec_from_file_location('hub_cli_under_test',
                                                  os.path.join(ROOT, 'tools', 'hub.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


HUB = load()


class CliCase(unittest.TestCase):
    def setUp(self):
        self.addCleanup(lambda: self.assertEqual(httpfix.http_threads(), [],
                                                 'a listener thread outlived its test'))
        self.home = tempfile.mkdtemp(prefix='kit-hubcli-')
        self.addCleanup(shutil.rmtree, self.home, True)
        env = mock.patch.dict(os.environ, {'USERPROFILE': self.home, 'HOME': self.home})
        env.start()
        self.addCleanup(env.stop)
        self.tokens = os.path.join(self.home, '.pm-kit', 'hub')
        self.store_dir = os.path.join(self.home, 'scratch-store')

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
        folder = os.path.join(self.home, '.pm-kit')
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, 'config.json'), 'w', encoding='utf-8') as f:
            json.dump(obj, f)

    def dead_port(self):
        """A loopback port nothing listens on: bound here, never listening, until the test ends."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(s.close)
        s.bind((httpd.LOOPBACK, 0))
        return s.getsockname()[1]

    def server(self, tokens=None):
        """A started hub on a free port, with the temp home's token files unless given others."""
        cfg = hub_settings.settings(None)._replace(port=0, store=self.store_dir)
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
        self.assertIn('USAGE: give a verb: serve, status, setup or open', err)
        self.assertEqual(self.run_cli('launch')[0], 2)
        self.assertEqual(self.run_cli('serve', '--bind', '0.0.0.0')[0], 2)
        self.assertEqual(self.run_cli('serve', '--host', '0.0.0.0')[0], 2)

    def test_a_port_that_is_no_port_is_a_usage_error(self):
        for argv in (('serve', '--port', 'x'), ('serve', '--port', '70000'),
                     ('serve', '--port', '-1'), ('status', '--port', '0'),
                     ('open', '--port', '65536'), ('status', '--port', '')):
            code, out, err = self.run_cli(*argv)
            self.assertEqual((code, out), (2, ''), argv)
            self.assertIn('USAGE: ', err)

    def test_a_machine_config_that_cannot_be_used_is_exit_2(self):
        self.config({'hub': {'port': 'eighty'}})
        for argv in (('serve', '--port', '0'), ('status',), ('setup',), ('open',)):
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
        self.assertEqual(len(lines), 1, out)
        start = 'hub serving %s on http://127.0.0.1:' % os.path.abspath(self.store_dir)
        self.assertTrue(lines[0].startswith(start), lines[0])
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
        self.assertEqual(os.listdir(os.path.join(self.home, '.pm-kit')), ['hub'])
        # a second run keeps the tokens
        self.assertEqual(self.run_cli('setup')[0], 0)
        self.assertEqual(auth.read_token(self.tokens, auth.OWNER), owner)

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


if __name__ == '__main__':
    unittest.main()
