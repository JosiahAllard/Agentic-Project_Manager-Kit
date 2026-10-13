"""The tools edge: tools/_host.py imports the adapter module hosts.json names, and the merged
--host flag (with --provider as its one-release alias) on the tools that take it."""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from kitlib.host import registry
from tests.host.test_registry import kit_hosts, second

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def tool(name):
    spec = importlib.util.spec_from_file_location('host_edge_%s' % name,
                                                  os.path.join(ROOT, 'tools', name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AdapterImport(unittest.TestCase):
    def setUp(self):
        folder = tempfile.mkdtemp(prefix='hosts-')
        self.addCleanup(shutil.rmtree, folder, True)
        self.path = os.path.join(folder, 'hosts.json')
        data = kit_hosts()
        data['claude']['adapter'] = 'json.decoder'       # any importable module stands in
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump(data, f)
        self.edge = tool('_host')

    def test_the_named_module_is_imported_at_the_edge(self):
        self.assertIs(self.edge.adapter_module('claude', {}, self.path), sys.modules['json.decoder'])
        self.assertIs(self.edge.adapter_module(None, {}, self.path), sys.modules['json.decoder'])

    def test_an_unregistered_host_is_refused_before_any_import(self):
        with mock.patch('importlib.import_module') as imp, \
                self.assertRaises(registry.RegistryError):
            self.edge.adapter_module('nobody', {}, self.path)
        imp.assert_not_called()

    def test_the_edge_is_not_a_command(self):
        from kitlib import stubs
        self.assertNotIn('_host', stubs.commands(ROOT))


class HostFlag(unittest.TestCase):
    def test_provider_is_an_alias_of_host(self):
        commit = tool('commit').parser()
        for flag in ('--host', '--provider'):
            with self.subTest(flag=flag):
                a = commit.parse_args(['-m', 's', flag, 'claude', 'a.py'])
                self.assertEqual(a.host, 'claude')
        self.assertIsNone(commit.parse_args(['-m', 's', 'a.py']).host)
        sync = tool('sync_base').build_parser()
        self.assertEqual(sync.parse_args(['abc', '--provider', 'claude']).host, 'claude')
        self.assertEqual(sync.parse_args(['abc', '--host', 'claude']).host, 'claude')
        self.assertEqual(tool('new_worktree').parser().parse_args(['a/b', '--host', 'x']).host, 'x')

    def test_an_unregistered_host_is_a_usage_error_before_any_work(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as stop, \
                mock.patch.dict(os.environ, {registry.ENV: ''}):
            tool('commit').main(['-m', 's', '--host', 'nobody', 'a.py'])
        self.assertEqual(stop.exception.code, 2)
        self.assertIn('unknown host', err.getvalue())

    def test_a_bad_pm_kit_host_is_a_usage_error(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as stop, \
                mock.patch.dict(os.environ, {registry.ENV: 'nobody'}):
            tool('sync_base').main(['abc', '--model', 'Opus'])
        self.assertEqual(stop.exception.code, 2)
        self.assertIn('unknown host', err.getvalue())


    def test_sync_base_names_the_chosen_hosts_model_variable(self):
        """A missing --model names the --host's own fallback variable, not the default host's."""
        folder = tempfile.mkdtemp(prefix='hosts-')
        self.addCleanup(shutil.rmtree, folder, True)
        path = os.path.join(folder, 'hosts.json')
        data = second(kit_hosts())
        data['other']['profile']['attribution']['model_env'] = 'OTHER_AGENT_MODEL'
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as stop, \
                mock.patch.object(registry, 'PATH', path), \
                mock.patch.dict(os.environ, {registry.ENV: '', 'OTHER_AGENT_MODEL': ''}):
            tool('sync_base').main(['abc', '--host', 'other'])
        self.assertEqual(stop.exception.code, 2)
        self.assertIn('OTHER_AGENT_MODEL', err.getvalue())
        self.assertNotIn('CLAUDE_AGENT_MODEL', err.getvalue())

if __name__ == '__main__':
    unittest.main()
