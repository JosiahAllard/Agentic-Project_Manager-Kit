"""Real owned return registration lifetime under compiled service assembly."""
import json
import os
import sys
import unittest
from unittest.mock import patch

import tests.bridge.test_claude_runtime as fixture
from optional.bridge.hosts.claude.assembly import build_delivery
import optional.bridge.hosts.claude.assembly as assembly
from optional.bridge.core.messages import DeliveryError
from optional.bridge.win.runtime import Config,Runtime


class ClaudeAssemblyTests(unittest.TestCase):
    setUp=fixture.ClaudeRuntimeTests.setUp
    tearDown=fixture.ClaudeRuntimeTests.tearDown
    write=fixture.ClaudeRuntimeTests.write

    def runtime(self):
        config=Config(self.state,self.kit,sys.executable,'owned-host',{'claude':sys.executable},{'claude':{}})
        return Runtime(self.controller,config,{'claude':lambda controller,**settings:self.sources},
            delivery_factories={'claude':build_delivery})

    def test_owned_endpoint_uses_original_registry_and_broker_workspace_then_cleans(self):
        # Fixture impersonates a native process with this test PID. Free only
        # that owned fixture record; production broker/native PIDs are distinct.
        record=self.registry/('%d.json'%os.getpid())
        record.unlink()
        before=set(self.registry.iterdir())
        runtime=self.runtime()
        try:
            adapter=self.controller.core.adapters['claude-peer']
            self.assertEqual(adapter.endpoint.registry,self.registry)
            self.assertEqual(json.loads(record.read_text())['cwd'],str(self.controller.root))
            self.assertTrue(adapter.endpoint._thread.is_alive())
            self.assertTrue(adapter.describe().capabilities)
        finally: runtime.close()
        self.assertFalse(adapter.endpoint._thread.is_alive())
        self.assertEqual(adapter.describe().capabilities,())
        self.assertEqual(set(self.registry.iterdir()),before)

    def test_failure_after_endpoint_publication_cleans_owned_registration(self):
        record=self.registry/('%d.json'%os.getpid())
        record.unlink()
        before=set(self.registry.iterdir())
        def failed(endpoint,resolver):
            self.assertTrue(record.exists())
            raise DeliveryError('assembly-fixture-failure')
        with patch.object(self.sources,'delivery_adapter',side_effect=failed):
            with self.assertRaises(DeliveryError): self.runtime()
        self.assertEqual(set(self.registry.iterdir()),before)
        self.assertTrue(self.controller.leader.owns())

    def test_existing_registration_is_preserved_and_never_adopted(self):
        record=self.registry/('%d.json'%os.getpid())
        previous=record.read_bytes()
        with self.assertRaises(DeliveryError): self.runtime()
        self.assertEqual(record.read_bytes(),previous)

    def test_compiled_source_uses_host_image_set_and_rejects_profile_override(self):
        from pathlib import Path
        alternate=Path(os.environ['WINDIR'])/'System32/cmd.exe'
        images=(alternate,Path(sys.executable))
        settings={'registry':self.registry,'host_id':'owned-host','desktop_profile':self.profile,
            'terminal_account':self.terminal_account,'terminal_projects':self.projects,
            'mcp_server_name':'kit','plugin_name':'owned-plugin'}
        sources=assembly.build_sources(self.controller,images,**settings)
        try:
            self.assertEqual(sources.directory.executables,tuple(path.resolve() for path in images))
            self.assertEqual(sources.marker,self.controller.root/'service.json')
            self.assertEqual(sources.refresh()[0].identity.session_id,self.task)
        finally: sources.close()
        for key,value in (('native_executable',sys.executable),('marker',self.root/'other.json')):
            with self.subTest(key=key):
                with self.assertRaises(DeliveryError):
                    assembly.build_sources(self.controller,images,**settings,**{key:value})
        del settings['plugin_name']
        with self.assertRaises(DeliveryError): assembly.build_sources(self.controller,images,**settings)
