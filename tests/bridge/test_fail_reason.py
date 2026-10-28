"""The bridge entry points name why they failed, as an identifier only."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from optional.bridge.core.messages import DeliveryError

KIT=Path(__file__).resolve().parents[2]


def load(name):
    spec=importlib.util.spec_from_file_location('fr_'+name,KIT/'optional/bridge/tools'/(name+'.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def run(module,argv):
    err=io.StringIO()
    with contextlib.redirect_stderr(err),contextlib.redirect_stdout(io.StringIO()):
        code=module.main(argv)
    return code,err.getvalue()


class FailReasonTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='bridge-reason-secret-')
        self.addCleanup(self.temp.cleanup)
        self.config=Path(self.temp.name)/'missing-config.json'
        self.prepare=load('bridge_prepare');self.service=load('bridge_service')

    def check(self,module,sentence,argv,reason):
        code,err=run(module,argv)
        self.assertEqual(code,1)
        self.assertTrue(err.startswith(sentence),err)
        self.assertIn(reason,err)
        self.assertNotIn(self.temp.name,err)
        self.assertNotIn('missing-config',err)
        return err

    def prepare_argv(self): return ['--config',str(self.config),'--output',str(Path(self.temp.name)/'out')]
    def service_argv(self): return ['--config',str(self.config)]

    def test_prepare_missing_config_names_the_reason(self):
        self.check(self.prepare,'Bridge installation preparation failed',self.prepare_argv(),'input file not found')

    def test_service_missing_config_prints_the_load_code(self):
        self.check(self.service,'Bridge service could not start',self.service_argv(),'invalid-runtime-config')

    def test_prepare_error_code_is_printed_not_message(self):
        error=DeliveryError('runtime-kit-root-mismatch')
        with mock.patch.object(self.prepare,'prepare_file',side_effect=error):
            err=self.check(self.prepare,'Bridge installation preparation failed',self.prepare_argv(),'runtime-kit-root-mismatch')

    def test_service_error_code_is_printed_not_message(self):
        error=DeliveryError('runtime-kit-root-mismatch')
        with mock.patch.object(self.service,'load_config',side_effect=error):
            err=self.check(self.service,'Bridge service could not start',self.service_argv(),'runtime-kit-root-mismatch')

    def test_service_names_a_missing_configured_path_by_its_setting_only(self):
        gone=Path(self.temp.name)/'gone-app'/'ChatGPT.exe'
        self.config.write_text(json.dumps({'protocol':1,'kit_root':str(KIT),'python':sys.executable,
            'state_root':str(Path(self.temp.name)/'state'),'host_id':'test-host',
            'native_hosts':{'codex':sys.executable},
            'profiles':{'codex':{'profile_root':self.temp.name,'desktop_executable':str(gone)}}}),
            encoding='utf-8')
        err=self.check(self.service,'Bridge service could not start',self.service_argv(),
            'runtime-path-missing-codex-desktop-executable')
        self.assertNotIn('gone-app',err)

    def test_codeless_error_prints_type_name_only(self):
        with mock.patch.object(self.prepare,'prepare_file',side_effect=KeyError('hunter2')):
            err=self.check(self.prepare,'Bridge installation preparation failed',self.prepare_argv(),'KeyError')
        self.assertNotIn('hunter2',err)


if __name__=='__main__': unittest.main()
