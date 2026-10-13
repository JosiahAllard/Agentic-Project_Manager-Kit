"""Concrete configured broker startup with private compiled provider factories."""
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from optional.bridge.core.messages import DeliveryError
from optional.bridge.core.contracts import AdapterDescription
from optional.bridge.win.runtime import load_config,Runtime,run_service
import optional.bridge.win.runtime as bootstrap
from optional.bridge.win.lifecycle import Supervisor
from kitlib.proc.job import Job
from tests.bridge.test_pairing import chat


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='bridge-bootstrap-')
        self.root=Path(self.temp.name)
        self.kit=Path(__file__).resolve().parents[2]
        self.document={'protocol':1,'kit_root':str(self.kit),'python':sys.executable,
            'state_root':str(self.root/'state'),'host_id':'test-host',
            'native_hosts':{'codex':sys.executable},'profiles':{'codex':{'profile_root':str(self.root)}}}
        self.config=self.root/'runtime.json'

    def tearDown(self): self.temp.cleanup()

    def load(self):
        self.config.write_text(json.dumps(self.document),encoding='utf-8')
        return load_config(self.config)

    def test_configuration_cannot_import_arbitrary_code_or_supply_agent_authority(self):
        for key in ('module','source_factory','owner_approved','sender'):
            with self.subTest(key=key):
                self.document[key]='untrusted'
                with self.assertRaises(DeliveryError): self.load()
                del self.document[key]

    def test_preparation_and_runtime_share_strict_profile_validation(self):
        self.assertEqual(bootstrap.parse_config(self.document),self.load())
        for key,value in (('owner_approved',True),('module','untrusted'),
                ('profile_root','relative'),('desktop_executable',str(self.root))):
            with self.subTest(key=key):
                profile={**self.document['profiles']['codex'],key:value}
                candidate={**self.document,'profiles':{'codex':profile}}
                with self.assertRaises(DeliveryError): bootstrap.parse_config(candidate)

    def test_native_kind_cannot_use_drive_relative_external_profile_schema(self):
        outside=self.root/'schemas';outside.mkdir()
        (outside/'bridge-profile.json').write_text(json.dumps({'required':['profile_root'],
            'optional':[],'directories':['profile_root'],'names':[]}))
        kind=self.root.drive+'schemas'
        candidate={**self.document,'native_hosts':{kind:sys.executable},
            'profiles':{kind:{'profile_root':str(self.root)}}}
        previous=os.getcwd()
        try:
            os.chdir(self.root)  # Windows drive-relative lookup now names our owned fixture.
            with self.assertRaises(DeliveryError): bootstrap.parse_config(candidate)
        finally: os.chdir(previous)

    def test_claude_profile_requires_explicit_existing_paths_and_native_tool_name(self):
        account=self.root/'account.json';account.write_text('{}')
        self.document['native_hosts']={'claude':sys.executable}
        profile={'registry':str(self.root),'desktop_profile':str(self.root),
            'terminal_account':str(account),'terminal_projects':str(self.root),'mcp_server_name':'kit-bridge','plugin_name':'owned-plugin'}
        self.document['profiles']={'claude':profile}
        self.assertEqual(bootstrap.parse_config(self.document),self.load())
        for key,value in (('registry','relative'),('terminal_account',str(self.root)),
                ('mcp_server_name','bad__name'),('marker',str(self.root/'service.json'))):
            with self.subTest(key=key):
                candidate={**self.document,'profiles':{'claude':{**profile,key:value}}}
                with self.assertRaises(DeliveryError): bootstrap.parse_config(candidate)

    def test_provider_profiles_must_match_known_native_host_kinds(self):
        self.document['profiles']={'other':{}}
        with self.assertRaises(DeliveryError): self.load()

    def test_configuration_interpreter_must_match_actual_supervisor_interpreter(self):
        self.document['python']=str(self.kit/'optional/bridge/tools/bridge_service.py')
        with self.assertRaisesRegex(DeliveryError,'runtime-python-mismatch'): self.load()

    def test_native_kind_accepts_explicit_distinct_desktop_and_terminal_images(self):
        alternate=Path(os.environ['WINDIR'])/'System32/cmd.exe'
        self.document['native_hosts']['codex']=[str(alternate),sys.executable]
        config=self.load()
        self.assertEqual(config.native_hosts['codex'],(alternate.resolve(),Path(sys.executable).resolve()))

    def test_ambiguous_or_duplicate_native_images_fail_during_configuration(self):
        self.document['native_hosts']['codex']=[sys.executable,sys.executable]
        with self.assertRaises(DeliveryError): self.load()
        self.document['native_hosts']['codex']=sys.executable
        self.document['native_hosts']['claude']=sys.executable
        self.document['profiles']['claude']={}
        with self.assertRaises(DeliveryError): self.load()

    def test_malformed_native_image_sets_do_not_become_registered_hosts(self):
        for images in ([],[sys.executable]*9,[[sys.executable]],[True],[str(self.root)],['relative.exe']):
            with self.subTest(images=images):
                self.document['native_hosts']['codex']=images
                with self.assertRaises(DeliveryError): self.load()

    def test_factories_receive_real_controller_and_run_before_marker_publication(self):
        config=self.load()
        constructed=[]
        class Provider:
            def __init__(provider,controller,host_id,profile_root):
                self.assertFalse((config.state_root/'service.json').exists())
                self.assertTrue(controller.leader.owns())
                constructed.append((host_id,profile_root))
            def __call__(provider,native): raise DeliveryError('source-not-observed')
            def close(provider): constructed.append('closed')
        with Supervisor(config.state_root,config.kit_root,config.python,config.native_hosts,
                runtime_builder=lambda controller:Runtime(controller,config,{'codex':Provider})) as running:
            self.assertEqual(constructed,[('test-host',str(self.root))])
            self.assertTrue((config.state_root/'service.json').exists())
            self.assertIs(running.service.source_factory.__self__,running.runtime)
        self.assertEqual(constructed[-1],'closed')

    def test_failed_provider_build_releases_singleton_and_publishes_nothing(self):
        config=self.load()
        def failed(*arguments,**keywords): raise DeliveryError('native-profile-unavailable')
        with self.assertRaises(DeliveryError):
            Supervisor(config.state_root,config.kit_root,config.python,config.native_hosts,
                runtime_builder=lambda controller:Runtime(controller,config,{'codex':failed}))
        self.assertFalse((config.state_root/'service.json').exists())
        with Supervisor(config.state_root,config.kit_root,config.python,config.native_hosts) as running:
            self.assertTrue(running.leader.owns())

    def test_compiled_delivery_builder_installs_before_publication_and_closes_first(self):
        config=self.load()
        observed=[]
        class Provider:
            def __init__(provider,controller,**settings): provider.controller=controller
            def close(provider): observed.append('provider-closed')
        class Adapter:
            def describe(adapter): return AdapterDescription('codex-desktop',(),'not captured')
            def close(adapter): observed.append('adapter-closed')
        adapter=Adapter()
        def build(provider,runtime):
            self.assertIs(runtime.controller,provider.controller)
            self.assertIs(runtime.providers['codex'],provider)
            self.assertTrue(provider.controller.leader.owns())
            self.assertFalse((config.state_root/'service.json').exists())
            observed.append('built')
            return adapter
        with Supervisor(config.state_root,config.kit_root,config.python,config.native_hosts,
                runtime_builder=lambda controller:Runtime(controller,config,{'codex':Provider},
                    delivery_factories={'codex':build})) as running:
            self.assertIs(running.controller.core.adapters['codex-desktop'],adapter)
            self.assertEqual(adapter.describe().capabilities,())
        self.assertEqual(observed,['built','adapter-closed','provider-closed'])

    def test_failed_delivery_build_cleans_providers_and_releases_unpublished_service(self):
        config=self.load()
        observed=[]
        class Provider:
            def __init__(provider,controller,**settings): pass
            def close(provider): observed.append('provider-closed')
        def failed(provider,runtime): raise DeliveryError('native-delivery-unavailable')
        with self.assertRaises(DeliveryError):
            Supervisor(config.state_root,config.kit_root,config.python,config.native_hosts,
                runtime_builder=lambda controller:Runtime(controller,config,{'codex':Provider},
                    delivery_factories={'codex':failed}))
        self.assertEqual(observed,['provider-closed'])
        self.assertFalse((config.state_root/'service.json').exists())
        with Supervisor(config.state_root,config.kit_root,config.python,config.native_hosts) as running:
            self.assertTrue(running.leader.owns())

    def test_allocated_adapter_registration_and_cleanup_failure_release_service(self):
        config=self.load()
        observed=[]
        class Provider:
            def __init__(provider,controller,**settings): pass
            def close(provider): observed.append('provider-closed')
        class Adapter:
            def describe(adapter): raise DeliveryError('native-descriptor-unavailable')
            def close(adapter):
                observed.append('adapter-close-attempted')
                raise RuntimeError('cleanup fixture failure')
        with self.assertRaisesRegex(DeliveryError,'native-provider-cleanup-unconfirmed'):
            Supervisor(config.state_root,config.kit_root,config.python,config.native_hosts,
                runtime_builder=lambda controller:Runtime(controller,config,{'codex':Provider},
                    delivery_factories={'codex':lambda provider,runtime:Adapter()}))
        self.assertEqual(observed,['adapter-close-attempted','provider-closed'])
        self.assertFalse((config.state_root/'service.json').exists())
        with Supervisor(config.state_root,config.kit_root,config.python,config.native_hosts) as running:
            self.assertTrue(running.leader.owns())

    def test_composite_mode_requires_one_verified_supplier_and_live_runtime(self):
        config=self.load()
        class Provider:
            def __init__(provider,controller,**settings): pass
            def sender_mode(provider,binding): return 'prompting'
            def close(provider): pass
        with Supervisor(config.state_root,config.kit_root,config.python,config.native_hosts,
                runtime_builder=lambda controller:Runtime(controller,config,{'codex':Provider})) as running:
            runtime=running.runtime
            binding=chat('mode-source')
            running.controller.enroll(binding,lambda current:current==binding)
            self.assertEqual(runtime.sender_mode(binding),'prompting')
            runtime.providers['duplicate']=Provider(running.controller)
            with self.assertRaisesRegex(DeliveryError,'native-sender-mode-unverified'):
                runtime.sender_mode(binding)
            runtime.providers.pop('duplicate')
            runtime.providers['codex'].sender_mode=lambda binding:True
            with self.assertRaises(DeliveryError): runtime.sender_mode(binding)
            runtime.providers['codex'].sender_mode=lambda binding:'bypass'
            runtime.close()
            with self.assertRaises(DeliveryError): runtime.sender_mode(binding)

    def test_mode_callback_closing_runtime_cannot_return_a_stale_class(self):
        config=self.load()
        class Provider:
            def __init__(provider,controller,**settings): pass
            def close(provider): pass
        with Supervisor(config.state_root,config.kit_root,config.python,config.native_hosts,
                runtime_builder=lambda controller:Runtime(controller,config,{'codex':Provider})) as running:
            binding=chat('mode-source')
            running.controller.enroll(binding,lambda current:current==binding)
            def mode(binding):
                running.runtime.close()
                return 'prompting'
            running.runtime.providers['codex'].sender_mode=mode
            with self.assertRaises(DeliveryError): running.runtime.sender_mode(binding)

    def test_configured_runner_serves_real_stdio_frontend_and_cleans_up(self):
        config=self.load()
        stop=threading.Event()
        observed=[]
        marker=config.state_root/'service.json'
        class Provider:
            def __init__(provider,controller,**settings): pass
            def __call__(provider,native): return lambda metadata:None
            def close(provider): observed.append('provider-closed')
        def probe():
            job=Job(kill_on_close=True)
            process=None
            try:
                import time
                deadline=time.monotonic()+5
                while not marker.exists() and time.monotonic()<deadline: stop.wait(.01)
                process=job.start([sys.executable,'-I','-B',str(self.kit/'optional/bridge/mcp_frontend.py'),str(marker)],
                    stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,close_fds=True)
                process.stdin.write(b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}\n')
                process.stdin.flush()
                observed.append(json.loads(process.stdout.readline(1024*1024)))
            finally:
                job.close()
                if process is not None:
                    process.wait(timeout=3)
                    process.stdin.close()
                    process.stdout.close()
                stop.set()
        worker=threading.Thread(target=probe)
        worker.start()
        try: run_service(config,{'codex':Provider},stop=stop)
        finally:
            stop.set()
            worker.join(timeout=8)
        self.assertFalse(worker.is_alive())
        self.assertIn('result',observed[0])
        self.assertEqual(observed[-1],'provider-closed')
        self.assertFalse(marker.exists())
