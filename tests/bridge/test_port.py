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
        shells = [ROOT / '.claude-plugin'] + [ROOT / 'hosts' / name for name in registry.host_registry.names()]
        found = []
        for shell in shells:
            for path in shell.rglob('*'):
                if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
                    if 'bridge' in path.read_text(encoding='utf-8', errors='replace').lower():
                        found.append(path.relative_to(ROOT).as_posix())
        self.assertEqual(found, [], 'installing the bridge registers it (optional/bridge/README.md "Install")')

    def test_every_half_has_its_profile_schema(self):
        for module in registry.present():
            schema = json.loads(registry.profile_schema(module).read_text(encoding='utf-8'))
            self.assertEqual(set(schema), {'required', 'optional', 'directories', 'names'}, module)


class Registry(unittest.TestCase):
    def test_served_hosts_are_registered_hosts_with_a_half(self):
        names = registry.names()
        self.assertTrue(set(names) <= set(registry.host_registry.names()))
        self.assertEqual(set(names), {n for n in registry.host_registry.names() if registry.has_half(n)})
        self.assertEqual(set(registry.installed()), set(names))

    def test_every_half_on_disk_exports_a_valid_host_of_its_name(self):
        import importlib
        for module in registry.present():
            host = importlib.import_module(registry.PACKAGE + '.' + module).HOST
            self.assertIs(port.check_host(host, module), host)
            self.assertIsNotNone(host.install, module)

    def test_an_unregistered_host_is_not_served(self):
        with self.assertRaisesRegex(DeliveryError, 'native-provider-unavailable'):
            registry.half('no-such-host')

    def test_a_host_name_maps_to_a_module_name(self):
        self.assertEqual(registry.module_name('my-host'), 'my_host')

    def test_check_host_refuses_a_malformed_half(self):
        good = port.BridgeHost('x', lambda *a, **k: None)
        self.assertIs(port.check_host(good, 'x'), good)
        for bad in (None, good, port.BridgeHost('x', None), port.BridgeHost('x', len, delivery=1),
                    port.BridgeHost('x', len, channel_env=['A']), port.BridgeHost('x', len, channel_env=('',))):
            name = 'y' if bad is good else 'x'
            with self.subTest(bad=bad), self.assertRaisesRegex(DeliveryError, 'invalid-bridge-host'):
                port.check_host(bad, name)

    def test_channel_env_collects_every_half(self):
        names = registry.channel_env()
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(all(type(n) is str and n for n in names))


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


if __name__ == '__main__':
    unittest.main()
