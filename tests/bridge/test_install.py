"""Staging-only bridge installation artifacts and owned Windows rollback."""
import copy
import hashlib
import json
import msvcrt
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests.bridge.registered import codex_registered
from optional.bridge.hosts.claude.runtime import hook_descriptor
from optional.bridge.core.mcp import TOOLS
from optional.bridge.core.messages import DeliveryError
from optional.bridge.win import install
from optional.bridge.win.runtime import load_config
from optional.bridge.win.lifecycle import _verify_root
from kitlib.proc.pipe import INVALID,create_file


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='bridge-prepare-')
        # Codex is served in process through a test hosts.json (tests/bridge/registered.py).
        registered=codex_registered();registered.__enter__()
        self.addCleanup(registered.__exit__,None,None,None)
        self.root=Path(self.temp.name).resolve()
        self.kit=Path(__file__).resolve().parents[2]
        self.profile=self.root/'profiles'
        self.profile.mkdir()
        for name in ('codex','registry','desktop','projects'):
            (self.profile/name).mkdir()
        self.account=self.profile/'account.json'
        self.secret=b'{"oauthAccount":{"token":"owned_account_secret_never_copy"}}'
        self.account.write_bytes(self.secret)
        self.state=self.root/'remembered-state'
        self.output=self.root/'prepared'
        self.doc={'protocol':1,'kit_root':str(self.kit),'python':sys.executable,
            'state_root':str(self.state),'host_id':'installation-fixture',
            'native_hosts':{'codex':sys.executable,'claude':[
                str(Path(os.environ['WINDIR'])/'System32/cmd.exe'),
                str(Path(sys.executable).with_name('pythonw.exe'))]},
            'profiles':{'codex':{'profile_root':str(self.profile/'codex')},
                'claude':{'registry':str(self.profile/'registry'),
                    'desktop_profile':str(self.profile/'desktop'),
                    'terminal_account':str(self.account),
                    'terminal_projects':str(self.profile/'projects'),
                    'mcp_server_name':'fixture_bridge-9','plugin_name':'owned-plugin'}}}

    def tearDown(self): self.temp.cleanup()

    def prepare(self): return install.prepare(self.doc,self.output)

    def test_cli_arguments_exit_codes_and_inactive_preparation(self):
        # A subprocess reads the kit's own hosts.json, which may not register Codex yet (plan
        # step 9), so the CLI case uses Claude alone; test_codex_cli_* covers the gap.
        for table in ('native_hosts','profiles'): del self.doc[table]['codex']
        config=self.root/'input.json';config.write_text(json.dumps(self.doc))
        argv=[sys.executable,'-I',str(self.kit/'optional/bridge/tools/bridge_prepare.py'),
            '--config',str(config),'--output',str(self.output)]
        first=subprocess.run(argv,capture_output=True,text=True,timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(first.returncode,0,first.stderr)
        self.assertEqual(self.read('manifest.json')['scope'],'prepared,notactivated')
        before=(self.output/'runtime.json').read_bytes()
        again=subprocess.run(argv,capture_output=True,text=True,timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(again.returncode,1)
        self.assertEqual((self.output/'runtime.json').read_bytes(),before)
        self.assertFalse(self.state.exists())

    def test_codex_cli_preparation_waits_for_codex_in_hosts_json(self):
        from kitlib.host import registry
        config=self.root/'input.json';config.write_text(json.dumps(self.doc))
        argv=[sys.executable,'-I',str(self.kit/'optional/bridge/tools/bridge_prepare.py'),
            '--config',str(config),'--output',str(self.output)]
        with open(registry.kit_path(),encoding='utf-8') as f: real=json.load(f)
        done=subprocess.run(argv,capture_output=True,text=True,timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW)
        # The kit's hosts.json decides: refused until step 9 registers Codex, then prepared.
        self.assertEqual(done.returncode,0 if 'codex' in real else 1,done.stderr)

    def read(self,name): return json.loads((self.output/name).read_bytes())

    def shared_bytes(self,path,data=None):
        # Python CRT readers omit FILE_SHARE_DELETE. Our fixture must cooperate
        # with the materializer's pinned DELETE handle to exercise real mutation.
        handle=create_file(str(path),0x80000000 if data is None else 0x40000000,
            7,None,3 if data is None else 2,0x80,None)
        self.assertNotEqual(handle,INVALID)
        fd=msvcrt.open_osfhandle(int(handle),(os.O_RDONLY if data is None else os.O_WRONLY)|os.O_BINARY)
        with os.fdopen(fd,'rb' if data is None else 'wb',buffering=0) as stream:
            if data is None: return stream.read()
            self.assertEqual(stream.write(data),len(data))

    def test_bundle_roundtrips_canonical_runtime_without_creating_live_state(self):
        before=copy.deepcopy(self.doc)
        result=self.prepare()
        config=load_config(self.output/'runtime.json')
        self.assertEqual(config.kit_root,self.kit.resolve())
        self.assertEqual(config.python,Path(sys.executable).resolve())
        self.assertEqual(config.native_hosts['claude'],tuple(Path(p).resolve() for p in self.doc['native_hosts']['claude']))
        self.assertEqual(config.profiles,self.doc['profiles'])
        self.assertEqual(self.doc,before)
        self.assertEqual(result.root,self.output)
        self.assertEqual(result.manifest.path,self.output/'manifest.json')
        self.assertFalse(self.state.exists())
        self.assertEqual(self.account.read_bytes(),self.secret)
        self.assertEqual(list((self.profile/'registry').iterdir()),[])
        _verify_root(self.output)
        for artifact in result.files:
            self.assertEqual(artifact.sha256,hashlib.sha256(artifact.path.read_bytes()).hexdigest())
            self.assertNotIn(b'owned_account_secret_never_copy',artifact.path.read_bytes())

    def test_mcp_fragments_bind_absolute_isolated_python_and_exact_server_names(self):
        result=self.prepare()
        packet=self.read('mcp.json')
        self.assertEqual(set(packet),{'protocol','servers'})
        expected={'command':str(Path(sys.executable).resolve()),
            'args':['-I','-B',str(self.kit/'optional/bridge/mcp_frontend.py'),str(self.state/'service.json')]}
        self.assertEqual(packet['servers']['codex'],{'name':'agent-bridge','configuration':expected})
        self.assertEqual(packet['servers']['claude'],{'name':'fixture_bridge-9','configuration':expected,
            'plugin_name':'owned-plugin','scoped_name':'plugin:owned-plugin:fixture_bridge-9'})
        dependencies={item.role:item.path for item in result.dependencies}
        self.assertEqual(dependencies['mcp-frontend'],self.kit/'optional/bridge/mcp_frontend.py')
        self.assertEqual(dependencies['service-entry'],self.kit/'optional/bridge/tools/bridge_service.py')
        self.assertEqual(dependencies['python'],Path(sys.executable).resolve())
        bindings={item.role:item.path for item in result.bindings}
        self.assertEqual(bindings['service-marker'],self.state/'service.json')
        self.assertEqual(bindings['claude.terminal_account'],self.account)

    def test_claude_hook_uses_verified_direct_exec_and_only_exact_native_server_tools(self):
        self.prepare()
        hooks=self.read('claude-hooks.json')
        self.assertEqual(set(hooks),{'hooks'})
        groups=hooks['hooks']['PreToolUse']
        self.assertEqual(len(groups),1)
        self.assertEqual(groups[0]['hooks'],[hook_descriptor(self.state/'service.json')])
        matcher=re.compile(groups[0]['matcher'])
        for tool in TOOLS:
            self.assertIsNotNone(matcher.fullmatch('mcp__plugin_owned-plugin_fixture_bridge-9__'+tool))
        for tool in ('mcp__other__bridge_send','mcp__fixture_bridge-9__arbitrary',
                'mcp__fixture_bridge-9_extra__bridge_send','Bash'):
            self.assertIsNone(matcher.fullmatch(tool))

    def test_manifest_hashes_exact_staged_destinations_and_claims_no_activation(self):
        result=self.prepare()
        manifest=self.read('manifest.json')
        self.assertEqual(manifest['scope'],'prepared,notactivated')
        self.assertEqual(manifest['role'],'installation-materializer')
        self.assertEqual(manifest['source'],{'kit_root':str(self.kit),'module':'optional.bridge.win.install'})
        self.assertEqual(manifest['active_capabilities'],[])
        files={item['destination']:item for item in manifest['files']}
        self.assertEqual(set(files),{str(self.output/name) for name in
            ('runtime.json','mcp.json','claude-hooks.json','start-bridge.ps1','startup-task.xml')})
        self.assertIs(manifest['startup']['registered'],False)
        for path,item in files.items():
            self.assertEqual(item['sha256'],hashlib.sha256(Path(path).read_bytes()).hexdigest())
        self.assertEqual(result.manifest.sha256,hashlib.sha256((self.output/'manifest.json').read_bytes()).hexdigest())
        self.assertEqual({item['role']:item['path'] for item in manifest['bindings']},
            {item.role:str(item.path) for item in result.bindings})

    def test_legacy_single_image_and_codex_only_bundle_need_no_claude_hook(self):
        del self.doc['native_hosts']['claude']
        del self.doc['profiles']['claude']
        self.prepare()
        self.assertFalse((self.output/'claude-hooks.json').exists())
        self.assertEqual(set(self.read('mcp.json')['servers']),{'codex'})
        self.assertEqual(self.read('runtime.json')['native_hosts']['codex'],[str(Path(sys.executable).resolve())])

    def test_remembered_routes_and_original_native_profiles_are_untouched(self):
        self.state.mkdir()
        route=self.state/'connections.sqlite'
        route.write_bytes(b'owned_remembered_route_bytes')
        native=self.profile/'desktop/settings.json'
        native.write_bytes(b'{"permissions":{"mode":"existing-owned-value"}}')
        before={p:(p.read_bytes(),p.stat().st_mtime_ns) for p in (route,native,self.account)}
        self.prepare()
        self.assertFalse((self.state/'service.json').exists())
        self.assertEqual(before,{p:(p.read_bytes(),p.stat().st_mtime_ns) for p in before})

    def test_existing_destination_is_never_adopted_even_when_empty(self):
        self.output.mkdir()
        with self.assertRaises(DeliveryError): self.prepare()
        self.assertEqual(list(self.output.iterdir()),[])
        foreign=self.output/'runtime.json'
        foreign.write_bytes(b'foreign prepared configuration')
        with self.assertRaises(DeliveryError): self.prepare()
        self.assertEqual(foreign.read_bytes(),b'foreign prepared configuration')

    def test_relative_output_and_overlap_with_native_state_or_kit_refuse_before_writes(self):
        candidates=(Path('relative'),self.state,self.state/'stage',self.root,
            self.profile/'registry/stage',self.profile/'projects/stage',
            self.profile/'desktop/stage',self.kit/'bridge-prepared-fixture')
        for path in candidates:
            with self.subTest(path=path),self.assertRaises(DeliveryError):
                install.prepare(self.doc,path)
        self.assertFalse(self.state.exists())
        self.assertFalse((self.profile/'registry/stage').exists())
        self.assertFalse((self.kit/'bridge-prepared-fixture').exists())

    def test_missing_parent_refuses_without_creating_ancestor_directories(self):
        missing=self.root/'new-parent'
        with self.assertRaises(DeliveryError): install.prepare(self.doc,missing/'bundle')
        self.assertFalse(missing.exists())

    def test_authority_fields_and_provider_overrides_refuse_before_staging(self):
        original=copy.deepcopy(self.doc)
        for key in ('module','source_factory','owner_approved','sender','grants','role'):
            for location in ('top','claude','codex'):
                with self.subTest(key=key,location=location):
                    self.doc=copy.deepcopy(original)
                    target=self.doc if location=='top' else self.doc['profiles'][location]
                    target[key]='untrusted'
                    with self.assertRaises(DeliveryError): self.prepare()
                    self.assertFalse(self.output.exists())
        for key in ('native_executable','marker'):
            self.doc=copy.deepcopy(original)
            self.doc['profiles']['claude'][key]=str(self.state)
            with self.assertRaises(DeliveryError): self.prepare()
            self.assertFalse(self.output.exists())

    def test_relative_native_profile_paths_and_wrong_path_types_refuse_before_staging(self):
        original=copy.deepcopy(self.doc)
        cases=[('codex','profile_root','relative'),('claude','registry','relative'),
            ('claude','desktop_profile','relative'),('claude','terminal_projects','relative'),
            ('claude','terminal_account','relative'),('claude','terminal_account',str(self.profile)),
            ('claude','desktop_profile',str(self.account))]
        for kind,key,value in cases:
            with self.subTest(key=key,value=value):
                self.doc=copy.deepcopy(original)
                self.doc['profiles'][kind][key]=value
                with self.assertRaises(DeliveryError): self.prepare()
                self.assertFalse(self.output.exists())
        self.doc=copy.deepcopy(original)
        self.doc['state_root']='relative'
        with self.assertRaises(DeliveryError): self.prepare()
        self.assertFalse(self.output.exists())

    def test_invalid_or_oversized_document_never_creates_output(self):
        for value in (None,[],{'protocol':1},dict(self.doc,host_id='x'*70000)):
            with self.subTest(value=type(value)),self.assertRaises(DeliveryError):
                install.prepare(value,self.output)
            self.assertFalse(self.output.exists())

    def test_failure_cleans_only_files_and_directory_created_by_this_prepare(self):
        write=install._write_file
        def fail(path,data):
            if path.name=='mcp.json': raise OSError('owned fixture write failure')
            return write(path,data)
        with mock.patch.object(install,'_write_file',side_effect=fail):
            with self.assertRaises(DeliveryError): self.prepare()
        self.assertFalse(self.output.exists())
        self.assertEqual(self.account.read_bytes(),self.secret)

    def test_failure_preserves_foreign_same_path_replacement_not_just_matching_bytes(self):
        write=install._write_file
        def replace_then_fail(path,data):
            if path.name=='mcp.json':
                runtime=self.output/'runtime.json'
                packet=self.shared_bytes(runtime)
                runtime.rename(self.output/'moved-owned-runtime.json')
                runtime.write_bytes(packet)
                raise OSError('owned replacement fixture failure')
            return write(path,data)
        with mock.patch.object(install,'_write_file',side_effect=replace_then_fail):
            with self.assertRaises(DeliveryError): self.prepare()
        self.assertTrue((self.output/'runtime.json').exists())
        self.assertEqual(set(p.name for p in self.output.iterdir()),{'runtime.json'})
        self.assertFalse(self.state.exists())

    def test_failure_preserves_in_place_changed_file_and_unowned_extra_file(self):
        write=install._write_file
        def modify_then_fail(path,data):
            if path.name=='manifest.json':
                self.shared_bytes(self.output/'runtime.json',b'foreign changed runtime')
                (self.output/'unowned.txt').write_bytes(b'foreign extra file')
                raise OSError('owned mutation fixture failure')
            return write(path,data)
        with mock.patch.object(install,'_write_file',side_effect=modify_then_fail):
            with self.assertRaises(DeliveryError): self.prepare()
        self.assertEqual((self.output/'runtime.json').read_bytes(),b'foreign changed runtime')
        self.assertEqual((self.output/'unowned.txt').read_bytes(),b'foreign extra file')
        self.assertEqual(set(p.name for p in self.output.iterdir()),{'runtime.json','unowned.txt'})

    def test_create_new_file_race_never_overwrites_unowned_destination(self):
        write=install._write_file
        def collide(path,data):
            if path.name=='mcp.json': path.write_bytes(b'foreign collision')
            return write(path,data)
        with mock.patch.object(install,'_write_file',side_effect=collide):
            with self.assertRaises(DeliveryError): self.prepare()
        self.assertEqual((self.output/'mcp.json').read_bytes(),b'foreign collision')
        self.assertEqual(set(p.name for p in self.output.iterdir()),{'mcp.json'})

    def test_failure_preserves_replacement_empty_directory(self):
        write=install._write_file
        moved=self.root/'moved-owned-output'
        def replace_then_fail(path,data):
            if path.name=='runtime.json':
                self.output.rename(moved)
                self.output.mkdir()
                raise OSError('owned directory replacement fixture failure')
            return write(path,data)
        with mock.patch.object(install,'_write_file',side_effect=replace_then_fail):
            with self.assertRaises(DeliveryError): self.prepare()
        self.assertTrue(self.output.is_dir())
        self.assertEqual(list(self.output.iterdir()),[])
        self.assertFalse(moved.exists())

    def test_fsync_failure_cleans_completed_owned_payload_and_private_directory(self):
        with mock.patch.object(install.os,'fsync',side_effect=OSError('owned flush fixture failure')):
            with self.assertRaises(DeliveryError): self.prepare()
        self.assertFalse(self.output.exists())

    def test_atomic_directory_does_not_reopen_output_path(self):
        opening=install.create_file
        def refuse_path_reopen(path,access,share,security,disposition,flags,template):
            if Path(path)==self.output and flags&0x02000000:
                raise AssertionError('atomic creation must retain its returned handle')
            return opening(path,access,share,security,disposition,flags,template)
        with mock.patch.object(install,'create_file',side_effect=refuse_path_reopen):
            result=self.prepare()
        self.assertEqual(result.root,self.output)

    def test_parent_directory_open_failure_creates_no_output(self):
        opening=install.create_file
        def fail_parent(path,access,share,security,disposition,flags,template):
            if Path(path)==self.output.parent and flags&0x02000000: return INVALID
            return opening(path,access,share,security,disposition,flags,template)
        with mock.patch.object(install,'create_file',side_effect=fail_parent):
            with self.assertRaises(DeliveryError): self.prepare()
        self.assertFalse(self.output.exists())

    def test_directory_fd_conversion_failure_safely_removes_atomic_owned_object(self):
        with mock.patch.object(install.msvcrt,'open_osfhandle',side_effect=OSError('owned descriptor conversion failure')):
            with self.assertRaises(DeliveryError): self.prepare()
        self.assertFalse(self.output.exists())

    def test_replacement_before_first_directory_stat_never_adopts_or_deletes_foreign_object(self):
        reading=install.regular
        moved=self.root/'created-before-first-stat'
        replaced=[]
        def replace_before_stat(path,*,directory=False):
            if Path(path)==self.output and directory and not replaced:
                self.output.rename(moved)
                self.output.mkdir()
                replaced.append(True)
            return reading(path,directory=directory)
        with mock.patch.object(install,'regular',side_effect=replace_before_stat):
            with self.assertRaises(DeliveryError): self.prepare()
        self.assertEqual(replaced,[True])
        self.assertTrue(self.output.is_dir())
        self.assertEqual(list(self.output.iterdir()),[])
        self.assertFalse(moved.exists())

    def test_atomic_create_requires_retained_parent_leaf_and_file_created_evidence(self):
        creating=install.nt_create_file
        observed=[]
        def inspect_create(handle,access,attributes,io,allocation,file_attributes,share,disposition,options,ea,ea_length):
            attrs=attributes._obj
            observed.append((attrs.RootDirectory,attrs.ObjectName.contents.Buffer,disposition,options))
            status=creating(handle,access,attributes,io,allocation,file_attributes,share,disposition,options,ea,ea_length)
            self.assertEqual(status,0)
            self.assertEqual(io._obj.Information,2)
            return status
        with mock.patch.object(install,'nt_create_file',side_effect=inspect_create): self.prepare()
        self.assertEqual(len(observed),1)
        parent,leaf,disposition,options=observed[0]
        self.assertTrue(parent)
        self.assertEqual(leaf,self.output.name)
        self.assertEqual(disposition,2)
        self.assertEqual(options&1,1)

    def test_atomic_create_collision_never_opens_or_adopts_existing_directory(self):
        creating=install.nt_create_file
        def collide(*args):
            self.output.mkdir()
            (self.output/'foreign.txt').write_bytes(b'foreign collision directory')
            return creating(*args)
        with mock.patch.object(install,'nt_create_file',side_effect=collide):
            with self.assertRaises(DeliveryError): self.prepare()
        self.assertEqual((self.output/'foreign.txt').read_bytes(),b'foreign collision directory')
        self.assertEqual(set(p.name for p in self.output.iterdir()),{'foreign.txt'})

    def test_unavailable_created_object_identity_explicitly_reports_unconfirmed_cleanup(self):
        identity=install._handle_identity
        calls=[]
        def fail_created_identity(handle):
            calls.append(handle)
            if len(calls)==2: raise OSError('owned identity query failure')
            return identity(handle)
        with mock.patch.object(install,'_handle_identity',side_effect=fail_created_identity):
            with self.assertRaisesRegex(DeliveryError,'cleanup-unconfirmed'): self.prepare()
        self.assertTrue(self.output.is_dir())
        self.assertEqual(list(self.output.iterdir()),[])
