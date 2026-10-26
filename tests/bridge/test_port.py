"""The bridge port and registry (optional/bridge/port.py, registry.py), and the layout rules of the
optional install: the service names no host, the host shells never mention the bridge, and the
Phase 0 probe and scaffold modules stay deleted (optional/bridge/README.md)."""
import ast
import json
import os
from pathlib import Path
import unittest

from optional.bridge import port, registry
from optional.bridge.core.contracts import AdapterDescription, Capability
from optional.bridge.core.messages import BeforeWriteError, DeliveryError
from tests.bridge.registered import codex_unregistered
from tests.host.test_leaks import occurrences

ROOT = Path(__file__).resolve().parents[2]
BRIDGE = ROOT / 'optional' / 'bridge'

# Deleted by plan step 10 (docs/multi-host-plan.md section 2.4); git history keeps them.
DELETED_MODULES = (
    'kitlib.bridgeprobe', 'kitlib.bridge', 'kitlib.bridgewin', 'kitlib.bridgeui',
    'claude.bridge_phase0', 'claude.bridge_assembly', 'claude.bridge_runtime',
    'codex.bridge_runtime', 'codex.pmkit_codex.bridge_client',
    *('optional.bridge.core.' + m for m in ('bootstrap', 'journal', 'pipeline', 'restart', 'delegation',
                                             'schema_probe', 'ports', 'disabled', 'status', 'errors')),
)
DELETED_FILES = (
    'kitlib/bridgeprobe', 'kitlib/bridge', 'kitlib/bridgewin', 'kitlib/bridgeui',
    'claude/bridge_phase0.py', 'claude/bridge-profile.json', 'codex/bridge-profile.json',
    'tools/bridge.py', 'tools/bridge_live_probe.py', 'tools/bridge_schema_probe.py',
    'tools/bridge_service.py', 'tools/bridge_prepare.py', 'mcp/bridge_frontend.py',
    'tests/bridgeprobe',
)


def python_files():
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in ('__pycache__', '.git', '.claude')]
        for name in files:
            if name.endswith(('.py', '.pyw')):
                yield Path(base) / name


def imports(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.lineno, node.module
            for alias in node.names:
                yield node.lineno, node.module + '.' + alias.name


class ProbeCodeStaysDeleted(unittest.TestCase):
    def test_no_deleted_file_exists(self):
        self.assertEqual([rel for rel in DELETED_FILES if (ROOT / rel).exists()], [])

    def test_nothing_imports_a_deleted_module(self):
        found = []
        for path in python_files():
            tree = ast.parse(path.read_text(encoding='utf-8'), str(path))
            for line, name in imports(tree):
                if any(name == gone or name.startswith(gone + '.') for gone in DELETED_MODULES):
                    found.append('%s:%d %s' % (path.relative_to(ROOT).as_posix(), line, name))
        self.assertEqual(found, [])


class Layout(unittest.TestCase):
    def test_no_host_name_in_bridge_code_outside_the_halves(self):
        found = []
        for path in BRIDGE.rglob('*.py'):
            rel = path.relative_to(ROOT).as_posix()
            if rel.startswith('optional/bridge/hosts/') and rel != 'optional/bridge/hosts/__init__.py':
                continue
            found.extend('%s:%d %r' % (rel, line, text)
                         for line, text in occurrences(path.read_text(encoding='utf-8')))
        self.assertEqual(found, [], 'the service loops over the installed halves (registry.py)')

    def test_no_host_shell_mentions_the_bridge(self):
        # The shells (hosts.json "shell") and Claude's root manifest: what a host loads. Adapter code
        # may use the word for other things (Codex's workflow names a 'bridge' dimension).
        shells = [ROOT / '.claude-plugin'] + [ROOT / h.shell for h in registry.host_registry.load().values()]
        found = []
        for shell in shells:
            for path in shell.rglob('*'):
                if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
                    if 'bridge' in path.read_text(encoding='utf-8', errors='replace').lower():
                        found.append(path.relative_to(ROOT).as_posix())
        self.assertEqual(found, [], 'installing the bridge registers it (optional/bridge/README.md "Install")')

    def test_every_half_has_its_profile_schema(self):
        for module in halves_on_disk():
            schema = registry.read_profile(module)
            self.assertTrue({'required', 'optional', 'directories', 'names'} <= set(schema), module)
            self.assertTrue(set(schema) <= {'required', 'optional', 'directories', 'names', 'channel_env'}, module)


def halves_on_disk():
    root = BRIDGE / 'hosts'
    return sorted(p.name for p in root.iterdir() if (p / '__init__.py').is_file())


class Registry(unittest.TestCase):
    def test_served_hosts_are_registered_hosts_with_a_half(self):
        names = registry.names()
        self.assertTrue(set(names) <= set(registry.host_registry.names()))
        self.assertEqual(set(names), {n for n in registry.host_registry.names() if registry.has_half(n)})
        self.assertEqual(set(registry.installed()), set(names))

    def test_every_half_on_disk_exports_a_valid_host_of_its_name(self):
        import importlib
        for module in halves_on_disk():
            host = importlib.import_module(registry.PACKAGE + '.' + module).HOST
            self.assertIs(port.check_host(host, module), host)
            self.assertIsNotNone(host.install, module)

    def test_an_unregistered_host_is_not_served(self):
        with self.assertRaisesRegex(DeliveryError, 'native-provider-unavailable'):
            registry.half('no-such-host')

    def test_codex_is_served_with_its_channel_variable(self):
        """hosts.json registers Codex (plan step 9), so its half is served for real."""
        self.assertIn('codex', registry.host_registry.names())
        self.assertIn('codex', registry.names())
        self.assertEqual(registry.half('codex').name, 'codex')
        self.assertIn('codex', registry.installed())
        self.assertIn('CODEX_APP_TOOLS_PIPE_PATH', registry.channel_env())

    def test_without_its_hosts_json_entry_codex_is_refused(self):
        with codex_unregistered():
            self.assertNotIn('codex', registry.names())
            with self.assertRaisesRegex(DeliveryError, 'native-provider-unavailable'):
                registry.half('codex')
            self.assertNotIn('codex', registry.installed())
            self.assertNotIn('CODEX_APP_TOOLS_PIPE_PATH', registry.channel_env())

    def test_a_host_name_maps_to_a_module_name(self):
        self.assertEqual(registry.module_name('my-host'), 'my_host')

    def test_check_host_refuses_a_malformed_half(self):
        good = port.BridgeHost('x', lambda *a, **k: None)
        self.assertIs(port.check_host(good, 'x'), good)
        for bad in (None, good, port.BridgeHost('x', None), port.BridgeHost('x', len, delivery=1),
                    port.BridgeHost('x', len, install='no')):
            name = 'y' if bad is good else 'x'
            with self.subTest(bad=bad), self.assertRaisesRegex(DeliveryError, 'invalid-bridge-host'):
                port.check_host(bad, name)

    def test_channel_env_names_are_distinct_variable_names(self):
        names = registry.channel_env()
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(all(registry.ENV_NAME.fullmatch(n) for n in names))

    def test_a_schema_with_a_bad_channel_variable_is_refused(self):
        base = {'required': ['a'], 'optional': [], 'directories': [], 'names': ['a']}
        self.assertEqual(registry.channel_names(base), ())
        self.assertEqual(registry.channel_names({**base, 'channel_env': ['APP_PIPE']}), ('APP_PIPE',))
        for bad in (['1X'], ['A B'], [''], 'APP_PIPE', ['A', 'A']):
            with self.subTest(bad=bad), self.assertRaisesRegex(DeliveryError, 'invalid-native-profile-schema'):
                registry.channel_names({**base, 'channel_env': bad})
        with self.assertRaisesRegex(DeliveryError, 'invalid-native-profile-schema'):
            registry.profile_schema({**base, 'other': []})


class StrayHalves(unittest.TestCase):
    """The MCP frontend reads only registered halves, as data: a stray or broken folder under
    hosts/ is never imported and cannot take channel nomination down."""

    def kit(self, folder, registered):
        kit = Path(folder)
        data = json.loads(Path(registry.host_registry.PATH).read_text(encoding='utf-8'))
        self.assertTrue(set(registered) <= set(data))       # hosts the kit's hosts.json registers
        (kit / 'hosts.json').write_text(json.dumps(data), encoding='utf-8')
        return kit, str(kit / 'hosts.json')

    def half(self, kit, module, schema, init='HOST = None\n'):
        folder = kit / 'optional' / 'bridge' / 'hosts' / module
        folder.mkdir(parents=True)
        (folder / '__init__.py').write_text(init, encoding='utf-8')
        (folder / 'bridge-profile.json').write_text(schema, encoding='utf-8')

    def test_a_stray_folder_is_neither_imported_nor_read(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            kit, hosts = self.kit(folder, ['codex'])
            good = {'required': ['a'], 'optional': [], 'directories': [], 'names': ['a'],
                    'channel_env': ['GOOD_PIPE']}
            self.half(kit, 'codex', json.dumps(good))
            marker = kit / 'imported'
            self.half(kit, 'stray', '{broken', init='open(%r, "w").close()\nraise SystemExit(9)\n' % str(marker))
            self.assertEqual(registry.channel_env(hosts, kit), ('GOOD_PIPE',))
            self.assertNotIn('stray', registry.names(hosts, kit))
            self.assertFalse(marker.exists())

    def test_a_registered_half_with_a_broken_profile_nominates_nothing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            kit, hosts = self.kit(folder, ['codex'])
            self.half(kit, 'codex', '{broken')
            first = next(iter(json.loads(Path(hosts).read_text(encoding='utf-8'))))
            good = {'required': ['a'], 'optional': [], 'directories': [], 'names': ['a'],
                    'channel_env': ['OTHER_PIPE']}
            self.half(kit, registry.module_name(first), json.dumps(good))
            self.assertEqual(registry.channel_env(hosts, kit), ('OTHER_PIPE',))

    def test_channel_variables_differing_only_by_case_are_one(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            kit, hosts = self.kit(folder, ['codex'])
            first = next(iter(json.loads(Path(hosts).read_text(encoding='utf-8'))))
            schema = {'required': ['a'], 'optional': [], 'directories': [], 'names': ['a']}
            self.half(kit, registry.module_name(first), json.dumps({**schema, 'channel_env': ['APP_PIPE']}))
            self.half(kit, 'codex', json.dumps({**schema, 'channel_env': ['app_pipe', 'OTHER']}))
            self.assertEqual(registry.channel_env(hosts, kit), ('APP_PIPE', 'OTHER'))

    def test_reading_channel_variables_imports_no_half(self):
        import subprocess
        import sys
        code = ('import sys; sys.path.insert(0, %r); from optional.bridge import registry; '
                'registry.channel_env(); '
                'print([m for m in sys.modules if m.startswith("optional.bridge.hosts.")])' % str(ROOT))
        out = subprocess.run([sys.executable, '-I', '-c', code], capture_output=True, text=True, timeout=60,
                             **({'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), '[]')


class Adapter:
    def __init__(self, adapter_id='fake', capabilities=(Capability.SEND,)):
        self.description = AdapterDescription(adapter_id, capabilities)
        self.sent = []

    def describe(self):
        return self.description

    def send(self, message):
        self.sent.append(message)
        return 'sent'

    def reconcile(self, message, native_id):
        return 'reconciled'


class Channel:
    closed = 0

    def close(self):
        self.closed += 1


class Port(unittest.TestCase):
    def test_native_source_attests_through_its_observer(self):
        source = port.NativeSource(lambda proof: ('attested', proof))
        self.assertIsInstance(source, port.SenderAttestor)
        self.assertEqual(source.attest(b'proof'), ('attested', b'proof'))
        with self.assertRaisesRegex(DeliveryError, 'invalid-native-source'):
            port.NativeSource(None)
        with self.assertRaisesRegex(DeliveryError, 'invalid-native-source'):
            port.NativeSource(len, prepare=1)

    def test_owned_delivery_closes_its_channel_once_and_then_refuses(self):
        adapter, channel = Adapter('peer'), Channel()
        owned = port.OwnedDelivery(adapter, channel, adapter_id='peer', unavailable='peer-closed')
        self.assertIsInstance(owned, port.DeliveryAdapter)
        self.assertIsInstance(channel, port.ReturnChannel)
        self.assertTrue(port.sends(owned))
        self.assertEqual(owned.send('m'), 'sent')
        self.assertEqual(owned.reconcile('m', 'n'), 'reconciled')
        owned.close()
        owned.close()
        self.assertEqual(channel.closed, 1)
        self.assertEqual(owned.describe(), AdapterDescription('peer', (), 'Owned return endpoint closed.'))
        self.assertFalse(port.sends(owned))
        with self.assertRaisesRegex(BeforeWriteError, 'peer-closed'):
            owned.send('m')
        with self.assertRaisesRegex(DeliveryError, 'peer-closed'):
            owned.reconcile('m', 'n')

    def test_sends_needs_send_capability_and_this_contract(self):
        self.assertTrue(port.sends(Adapter()))
        self.assertFalse(port.sends(Adapter(capabilities=(Capability.READ_STATUS,))))
        old = Adapter()
        old.description = AdapterDescription('fake', (Capability.SEND,), None, 0)
        self.assertFalse(port.sends(old))

        class Broken:
            def describe(self):
                raise RuntimeError('down')
        self.assertFalse(port.sends(Broken()))


class Assembly(unittest.TestCase):
    """win/runtime.py: providers and delivery builders come from the halves, keyed by kind."""

    def config(self, kinds):
        from optional.bridge.win.runtime import Config
        return Config(ROOT, ROOT, ROOT, 'host-id', {k: ('image-' + k,) for k in kinds}, {k: {'s': k} for k in kinds})

    def test_factories_wrap_each_halfs_provider_with_its_images(self):
        from optional.bridge.win.runtime import factories
        calls = []
        hosts = {k: port.BridgeHost(k, lambda controller, images, **settings: calls.append((controller, images, settings)),
                                    delivery=(lambda p, r: None) if k == 'a' else None)
                 for k in ('a', 'b')}
        providers, deliveries = factories(self.config(['a', 'b']), hosts)
        self.assertEqual(set(providers), {'a', 'b'})
        self.assertEqual(set(deliveries), {'a'})
        providers['b']('controller', host_id='h', s='b')
        self.assertEqual(calls, [('controller', ('image-b',), {'host_id': 'h', 's': 'b'})])

    def test_a_profile_without_a_half_refuses_the_start(self):
        from optional.bridge.win.runtime import factories
        with self.assertRaisesRegex(DeliveryError, 'native-provider-unavailable'):
            factories(self.config(['a', 'missing']), {'a': port.BridgeHost('a', len)})

    def test_a_codex_profile_is_served_and_refused_without_its_registration(self):
        from optional.bridge.win.runtime import factories
        config = self.config(['codex'])
        providers, deliveries = factories(config)
        self.assertEqual(set(providers), {'codex'})
        self.assertEqual(set(deliveries), {'codex'})
        with codex_unregistered(), self.assertRaisesRegex(DeliveryError, 'native-provider-unavailable'):
            factories(config)

    def test_a_hook_frame_goes_to_the_one_provider_with_a_sink(self):
        from optional.bridge.win.runtime import Runtime
        seen = []

        class WithSink:
            def __init__(self, controller, **settings):
                pass

            def hook_sink(self, peer, payload):
                seen.append((peer, payload))

        class Without:
            def __init__(self, controller, **settings):
                pass
        runtime = Runtime(object(), self.config(['a', 'b']), {'a': Without, 'b': WithSink})
        runtime.hook_sink('peer', b'{}')
        self.assertEqual(seen, [('peer', b'{}')])
        both = Runtime(object(), self.config(['a', 'b']), {'a': WithSink, 'b': WithSink})
        with self.assertRaisesRegex(DeliveryError, 'hook-producer-unavailable'):
            both.hook_sink('peer', b'{}')
        none = Runtime(object(), self.config(['a']), {'a': Without})
        with self.assertRaisesRegex(DeliveryError, 'hook-producer-unavailable'):
            none.hook_sink('peer', b'{}')



class ServiceAttests(unittest.TestCase):
    """win/service.py calls attest() on any SenderAttestor a provider returns (port.py)."""

    def handle(self, source):
        from kitlib.proc.process import own_ctime
        from optional.bridge.win.service import BrokerService
        observed = []

        class Native:
            # A running process, as the real inspector returns: the broker refuses an exited one.
            pid, creation = os.getpid(), own_ctime()
            mark = None

        class Server:
            def handle(self, message):
                return {'handled': message}

        class Controller:
            def frontend(self, observe, *, mark=None):
                observed.append(observe)
                return Server()

        class Inspector:
            def inspect(self, peer, channel=None, mark=None):
                return Native()
        service = BrokerService.__new__(BrokerService)
        service.closed, service.controller, service.inspector = False, Controller(), Inspector()
        service.source_factory, service.frontends, service.sources = (lambda native: source), {}, {}
        reply = service.handle('peer', {'protocol': 1, 'request': {'method': 'ping'}})
        self.assertEqual(reply, {'protocol': 1, 'response': {'handled': {'method': 'ping'}}})
        return observed[0], service

    def test_any_object_with_attest_is_called_through_attest(self):
        class Attestor:
            def attest(self, proof):
                return ('attested', proof)
        observe, service = self.handle(Attestor())
        self.assertEqual(observe('p'), ('attested', 'p'))
        self.assertEqual(service.sources, {})

    def test_a_native_source_with_prepare_is_kept_for_its_lease(self):
        source = port.NativeSource(lambda proof: proof, prepare=lambda request: ({}, lambda: None))
        observe, service = self.handle(source)
        self.assertEqual(observe('p'), 'p')
        self.assertEqual(list(service.sources.values()), [source])

    def test_a_plain_callable_is_attest_itself(self):
        def attest(proof):
            return proof
        observe, _ = self.handle(attest)
        self.assertIs(observe, attest)


    def test_a_prepare_lease_that_is_not_callable_refuses_the_source(self):
        class Attestor:
            prepare = 'not callable'

            def attest(self, proof):
                return proof
        with self.assertRaisesRegex(DeliveryError, 'invalid-native-source'):
            self.handle(Attestor())


class UnregisteredProfiles(unittest.TestCase):
    """A profile for a host hosts.json does not register is refused before its half is read."""

    def test_an_unregistered_kind_is_refused_without_reading_its_profile(self):
        from unittest import mock
        from optional.bridge import paths
        from optional.bridge.win import runtime
        read = []
        real = registry.profile_path

        def recorded(name, kit=None):
            read.append(name)
            return real(name, kit)
        with mock.patch.object(registry, 'profile_path', side_effect=recorded):
            with self.assertRaisesRegex(DeliveryError, 'native-provider-unavailable'):
                runtime._profiles({'unregistered-host': {}}, paths.KIT)
            with codex_unregistered(), self.assertRaisesRegex(DeliveryError, 'native-provider-unavailable'):
                runtime._profiles({'codex': {'profile_root': str(ROOT)}}, paths.KIT)
        self.assertEqual(read, [])

    def test_install_renders_codexs_server_entry_from_the_real_registry(self):
        import sys
        import tempfile
        from unittest import mock
        from optional.bridge import paths
        from optional.bridge.win import install, runtime
        with tempfile.TemporaryDirectory() as folder:
            doc = {'protocol': 1, 'kit_root': str(paths.KIT), 'python': str(Path(sys.executable).resolve()),
                   'state_root': str(Path(folder) / 'state'), 'host_id': 'served-fixture',
                   'native_hosts': {'codex': sys.executable}, 'profiles': {'codex': {'profile_root': folder}}}
            images = {'codex': (Path(sys.executable).resolve(),)}
            # The image check and the startup task are Win32 (links, SystemRoot, the user's SID);
            # the configuration check and the MCP entry are not.
            with mock.patch('optional.bridge.win.runtime.native_hosts', return_value=images):
                config = runtime.parse_config(doc)
            with mock.patch.object(install, 'render_startup', return_value={}):
                payloads, _ = install._render(config, Path(folder) / 'prepared')
        servers = json.loads(dict((name, data) for name, _, data in payloads)['mcp.json'])['servers']
        self.assertEqual(servers['codex']['name'], 'agent-bridge')

    def test_install_prepare_refuses_codex_with_the_provider_code_when_unregistered(self):
        import sys
        import tempfile
        from optional.bridge import paths
        from optional.bridge.win import install
        with tempfile.TemporaryDirectory() as folder:
            doc = {'protocol': 1, 'kit_root': str(paths.KIT), 'python': str(Path(sys.executable).resolve()),
                   'state_root': str(Path(folder) / 'state'), 'host_id': 'gap-fixture',
                   'native_hosts': {'codex': sys.executable}, 'profiles': {'codex': {'profile_root': folder}}}
            from unittest import mock
            images = {'codex': (Path(sys.executable).resolve(),)}
            # The image check is Win32 evidence (it refuses a link such as a POSIX python3);
            # the refusal under test comes after it, from the host registry.
            with mock.patch('optional.bridge.win.runtime.native_hosts', return_value=images), \
                    codex_unregistered(), self.assertRaisesRegex(DeliveryError, 'native-provider-unavailable'):
                install.prepare(doc, Path(folder) / 'prepared')
            self.assertFalse((Path(folder) / 'prepared').exists())

    def test_channel_variable_names_differ_by_case_only_once(self):
        base = {'required': ['a'], 'optional': [], 'directories': [], 'names': ['a']}
        with self.assertRaisesRegex(DeliveryError, 'invalid-native-profile-schema'):
            registry.channel_names({**base, 'channel_env': ['APP_PIPE', 'app_pipe']})


if __name__ == '__main__':
    unittest.main()
