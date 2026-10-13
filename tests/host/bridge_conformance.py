"""The bridge-port part of the host conformance suite (docs/multi-host-plan.md section 2.6), for a
host whose bridge half exists (optional/bridge/hosts/<name>/). Not a test module: a host's
conformance class takes it through `mixin(HOST)`, which is BridgeConformance when the half exists
and an empty class otherwise, so a host without a half runs none of it:

    class ClaudeConformance(conformance.HostConformance, bridge_conformance.mixin('claude'),
                            unittest.TestCase):

What a subclass may supply:
  bridge_delivery() -> (adapter, message, receive) or None
                  a DeliveryAdapter wired to a fake recipient pipe, an OutboundMessage addressed
                  to it, and receive(), which starts the fake recipient and returns done(); done()
                  waits for it and returns the frames it read. None: the test skips, saying so.

Every check reads the half's own answers; none names a host.
"""
import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def mixin(host):
    from optional.bridge import registry
    return BridgeConformance if registry.has_half(host) else NoBridge


class NoBridge(object):
    """The host has no bridge half: nothing to check."""


class BridgeConformance(object):

    def bridge_host(self):
        from optional.bridge import port, registry
        module = importlib.import_module(registry.PACKAGE + '.' + registry.module_name(self.HOST))
        return port.check_host(getattr(module, 'HOST', None), self.HOST)

    def sample_profile(self, folder):
        """A profile meeting the half's bridge-profile.json: a folder for each directory setting,
        a file for each other path, a plain name for each name setting."""
        from optional.bridge import registry
        schema = json.loads(registry.profile_path(self.HOST).read_text(encoding='utf-8'))
        required, _optional, directories, names = registry.profile_schema(schema)
        profile = {}
        for name in sorted(required):
            path = Path(folder) / name
            if name in names:
                profile[name] = 'sample-' + name.replace('_', '-')
            elif name in directories:
                path.mkdir()
                profile[name] = str(path)
            else:
                path.write_text('{}', encoding='utf-8')
                profile[name] = str(path)
        return profile

    def test_bridge_half_exports_its_host(self):
        host = self.bridge_host()
        self.assertEqual(host.name, self.HOST)
        self.assertTrue(callable(host.provider))

    def test_bridge_profile_schema_is_well_formed(self):
        from optional.bridge import registry
        schema = registry.read_profile(self.HOST)
        required, _optional, _directories, _names = registry.profile_schema(schema)
        self.assertTrue(required, 'a half takes at least one setting')
        registry.channel_names(schema)                 # channel_env, if any, is well formed

    def test_bridge_install_fragment(self):
        from optional.bridge.port import InstallFragment
        host = self.bridge_host()
        self.assertIsNotNone(host.install, 'installing the bridge registers each half (README "Install")')
        with tempfile.TemporaryDirectory(prefix='bridge-conformance-') as folder:
            marker = Path(folder) / 'state' / 'service.json'
            fragment = host.install(self.sample_profile(folder), python=Path(sys.executable),
                                    marker=marker, kit_root=ROOT)
        self.assertIs(type(fragment), InstallFragment)
        self.assertIsInstance(fragment.server.get('name'), str)
        self.assertNotIn('configuration', fragment.server, 'the installer writes the command line')
        for role, path in fragment.dependencies:
            self.assertTrue(Path(path).is_file(), '%s: %s' % (role, path))
        for name, role, document in fragment.files:
            self.assertTrue(name.startswith(self.HOST + '-') and name.endswith('.json'), name)
            self.assertNotIn('/', name)
            json.dumps(document, allow_nan=False)
            # A file the half registers names the hook it runs, by the same marker.
            if 'hooks' in document:
                for groups in document['hooks'].values():
                    for group in groups:
                        for hook in group['hooks']:
                            self.assertEqual(hook['args'][-1], str(marker))
                            self.assertTrue(Path(hook['args'][-2]).is_file(), hook)

    @unittest.skipUnless(os.name == 'nt', 'the bridge is Windows only (named pipes)')
    def test_bridge_delivery_describes_and_sends_on_a_fake_pipe(self):
        from optional.bridge.core.contracts import AdapterDescription, Capability, CONTRACT_VERSION
        from optional.bridge.core.messages import BeforeWriteError, DeliveryError, NativeResult
        wired = self.bridge_delivery()
        if wired is None:
            self.skipTest('%s records no fake recipient for its delivery adapter' % self.HOST)
        adapter, message, receive = wired
        description = adapter.describe()
        self.assertIs(type(description), AdapterDescription)
        self.assertEqual(description.adapter_id, message.recipient.identity.adapter_id)
        self.assertEqual(description.contract_version, CONTRACT_VERSION)
        self.assertIn(Capability.SEND, description.capabilities)
        done = receive()
        try:
            result = adapter.send(message)
        except BeforeWriteError as error:
            self.fail('nothing reached the fake recipient: %s' % error.code)
        except DeliveryError:
            result = None             # written, outcome unknown: the core never resends it
        frames = done()
        self.assertTrue(frames, 'the fake recipient read nothing')
        if result is not None:
            self.assertIs(type(result), NativeResult)
            self.assertEqual(result.operation_id, message.operation_id)

    def bridge_delivery(self):
        return None
