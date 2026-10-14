"""Documented plugin namespace and owned caller correlation; no native inference."""
import json
from pathlib import Path
import re
import subprocess
import sys
import unittest
import uuid

from optional.bridge.hosts.claude.provenance import PluginMcp
from optional.bridge.hosts.claude.runtime import ClaudeSources
from optional.bridge.hosts.claude.hook import entry_command
from optional.bridge.core.mcp import TOOLS
from optional.bridge.core.messages import DeliveryError
from optional.bridge.win.runtime import Config
from optional.bridge.win.install import _render
import tests.bridge.test_claude_runtime as runtime_fixture


class PluginNamespaceTests(unittest.TestCase):
    def test_registration_scope_tool_names_and_hook_matcher_are_distinct(self):
        names=PluginMcp('bridge-native-probe','agent-bridge')
        self.assertEqual(names.plugin_name,'bridge-native-probe')
        self.assertEqual(names.server_name,'agent-bridge')
        self.assertEqual(names.registered_name,'plugin:bridge-native-probe:agent-bridge')
        self.assertEqual(names.tool_prefix,'mcp__plugin_bridge-native-probe_agent-bridge__')
        self.assertEqual(set(names.tools),{names.tool_prefix+tool for tool in TOOLS})
        self.assertTrue(all(value=={'name':names.registered_name,'source':'plugin'} for value in names.tools.values()))
        matcher=re.compile(names.matcher)
        for name in names.tools:self.assertIsNotNone(matcher.fullmatch(name))
        for name in ('mcp__agent-bridge__bridge_connections',
                'mcp__plugin_other_agent-bridge__bridge_connections',
                names.tool_prefix+'arbitrary',names.tool_prefix+'bridge_connections_extra'):
            self.assertIsNone(matcher.fullmatch(name))

    def test_normalized_namespace_collision_never_collapses_raw_provenance(self):
        left=PluginMcp('scope_kit','bridge')
        right=PluginMcp('scope','kit_bridge')
        self.assertEqual(left.tool_prefix,right.tool_prefix)
        self.assertNotEqual(left.registered_name,right.registered_name)
        self.assertNotEqual(left.tools,right.tools)

    def test_explicit_component_names_refuse_precombined_scope_or_malformed_values(self):
        for plugin,server in ((None,'bridge'),('kit',False),('','bridge'),('kit',''),
                ('plugin:kit:bridge','bridge'),('kit','plugin:kit:bridge'),
                ('kit','a__b'),('a__b','bridge'),('kit','relative/name'),
                ('kit\n','bridge'),('kit','bridge name'),('k'*129,'bridge')):
            with self.subTest(plugin=plugin,server=server),self.assertRaises(DeliveryError):
                PluginMcp(plugin,server)

    def test_metadata_is_frozen_and_returned_tool_map_cannot_change_expected_scope(self):
        names=PluginMcp('pm-kit','agent-bridge')
        with self.assertRaises(AttributeError):names.plugin_name='other'
        external=names.tools
        external[names.tool_prefix+'bridge_connections']['source']='sdk'
        self.assertEqual(names.tools[names.tool_prefix+'bridge_connections']['source'],'plugin')

    def test_installer_keeps_bare_registration_key_and_emits_shared_scoped_matcher(self):
        kit=Path(__file__).resolve().parents[2]
        root=kit.parent/'owned-render-only-path'
        config=Config(root,kit,Path(sys.executable).resolve(),'owned-render-host',
            {'claude':(Path(sys.executable).resolve(),)},
            {'claude':{'plugin_name':'bridge-native-probe','mcp_server_name':'agent-bridge'}})
        payloads,dependencies=_render(config,kit.parent/'owned-prepared-path')
        packets={name:json.loads(packet) for name,role,packet in payloads if name.endswith('.json')}
        names=PluginMcp('bridge-native-probe','agent-bridge')
        server=packets['mcp.json']['servers']['claude']
        self.assertEqual(server['name'],'agent-bridge')
        self.assertEqual(server['plugin_name'],names.plugin_name)
        self.assertEqual(server['scoped_name'],names.registered_name)
        self.assertEqual(server['configuration']['args'][-1],str(root/'service.json'))
        self.assertEqual(packets['claude-hooks.json']['hooks']['PreToolUse'][0]['matcher'],names.matcher)
        self.assertIn(kit/'optional/bridge/hosts/claude/provenance.py',[item.path for item in dependencies])

    def test_installer_claude_profile_cannot_prepare_a_bare_production_namespace(self):
        kit=Path(__file__).resolve().parents[2]
        config=Config(kit.parent/'render-only',kit,Path(sys.executable).resolve(),'render-host',
            {'claude':(Path(sys.executable).resolve(),)}, {'claude':{'mcp_server_name':'agent-bridge'}})
        with self.assertRaises(DeliveryError):_render(config,kit.parent/'owned-prepared-path')


class ScopedOwnedRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.fixture=runtime_fixture.ClaudeRuntimeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        f=self.fixture
        f.sources.close()
        self.names=PluginMcp('scope_kit','bridge')
        f.sources=ClaudeSources(f.controller,f.registry,sys.executable,'owned-host',f.profile,
            f.terminal_account,f.projects,f.marker,'bridge',plugin_name='scope_kit')
        f.cli_mode()
        f.start_service()

    def hook(self,*,name=None,source='plugin',tool=None):
        f=self.fixture
        event={'session_id':f.cli,'prompt_id':str(uuid.uuid4()),'cwd':str(f.root),
            'transcript_path':str(f.transcript),'permission_mode':'auto','hook_event_name':'PreToolUse',
            'tool_name':self.names.tool_prefix+'bridge_connections' if tool is None else tool,
            'tool_input':{},'tool_use_id':'toolu_'+uuid.uuid4().hex,
            'mcp_server':{'name':self.names.registered_name if name is None else name,'source':source}}
        child=f.job.start(entry_command(f.marker),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW,close_fds=True)
        f.children.append(child)
        output,error=child.communicate(json.dumps(event).encode(),timeout=8)
        self.assertEqual((child.returncode,output,error),(0,b'',b''))

    def call(self):
        return self.fixture.call({'jsonrpc':'2.0','id':2,'method':'tools/call','params':{
            'name':'bridge_connections','arguments':{},'_meta':{'mcp_server':{
                'name':self.names.registered_name,'source':'plugin'},'sender':'untrusted'}}})

    def test_owned_scoped_hook_enrolls_and_native_frontend_consumes_one_exact_request(self):
        f=self.fixture
        self.assertEqual(f.sources.tools,self.names.tools)
        self.hook()
        self.assertEqual(len(f.controller.pairs.registered()),1)
        result=self.call()
        self.assertFalse(result['result']['isError'],result)
        self.assertIn('error',self.call())
        self.assertEqual(f.controller.pairs.owner_routes(),())
        self.assertFalse(f.controller.core.adapters)

    def test_bare_provenance_never_falls_back_to_scoped_tool_identity(self):
        self.hook(name='bridge')
        self.assertEqual(self.fixture.controller.pairs.registered(),())
        self.assertIn('error',self.call())

    def test_matching_scoped_name_from_nonplugin_source_is_denied(self):
        self.hook(source='sdk')
        self.assertEqual(self.fixture.controller.pairs.registered(),())
        self.assertIn('error',self.call())

    def test_identical_normalized_prefix_with_other_raw_plugin_scope_is_denied(self):
        other=PluginMcp('scope','kit_bridge')
        self.assertEqual(other.tool_prefix,self.names.tool_prefix)
        self.hook(name=other.registered_name)
        self.assertEqual(self.fixture.controller.pairs.registered(),())
        self.assertIn('error',self.call())

    def test_bare_or_other_plugin_tool_name_cannot_borrow_scoped_provenance(self):
        self.hook(tool='mcp__bridge__bridge_connections')
        self.hook(tool='mcp__plugin_other_bridge__bridge_connections')
        self.assertEqual(self.fixture.controller.pairs.registered(),())
        self.assertIn('error',self.call())
