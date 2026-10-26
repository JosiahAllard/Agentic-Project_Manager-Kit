"""The bridge entry points name why they failed, as an identifier only."""
import contextlib
import importlib.util
import io
from pathlib import Path
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

    def test_codeless_error_prints_type_name_only(self):
        with mock.patch.object(self.prepare,'prepare_file',side_effect=KeyError('hunter2')):
            err=self.check(self.prepare,'Bridge installation preparation failed',self.prepare_argv(),'KeyError')
        self.assertNotIn('hunter2',err)


if __name__=='__main__': unittest.main()
