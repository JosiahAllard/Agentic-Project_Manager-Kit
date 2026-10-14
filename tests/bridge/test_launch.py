"""Actual isolated helper imports must leave a fresh code directory unchanged."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from optional.bridge.win.transport import run_worker


class LaunchTests(unittest.TestCase):
    def test_actual_owned_worker_disables_bytecode_even_when_isolated_python_ignores_environment(self):
        with tempfile.TemporaryDirectory(prefix='bridge-launch-') as folder:
            root=Path(folder)
            tools=root/'optional/bridge/tools';tools.mkdir(parents=True)
            module=root/'optional/bridge/win/transport.py'
            module.parent.mkdir(parents=True);module.touch()
            (tools/'owned_import.py').write_text('VALUE=23\n',encoding='utf-8')
            (tools/'bridge_rpc_worker.py').write_text(
                'import json,sys; from pathlib import Path; '
                'sys.path.insert(0,str(Path(__file__).resolve().parent)); import owned_import; '
                'sys.stdin.buffer.read(); '
                'print(json.dumps({"isolated":bool(sys.flags.isolated),'
                '"bytecode_disabled":sys.dont_write_bytecode,"value":owned_import.VALUE}))',encoding='utf-8')
            with patch('optional.bridge.win.transport.__file__',str(module)),patch.dict(os.environ,{'PYTHONDONTWRITEBYTECODE':'1'}):
                result=json.loads(run_worker('bridge_rpc_worker.py',b'{}',limit=1024,timeout=5))
            self.assertEqual(result,{'isolated':True,'bytecode_disabled':True,'value':23})
            self.assertFalse((tools/'__pycache__').exists())
