"""Review-only per-user Windows startup artifacts; no task registration."""
from pathlib import Path
import os
import json
import subprocess
import sys
import tempfile
import unittest

import xml.etree.ElementTree as ET

import optional.bridge.win.startup as startup
from optional.bridge.win.runtime import parse_config
from kitlib.proc.pipe import process_sids
from kitlib.proc.job import Job


class StartupTests(unittest.TestCase):
    def test_task_uses_current_interactive_user_and_hidden_explicit_python_launcher(self):
        with tempfile.TemporaryDirectory(prefix='bridge-startup-') as folder:
            root=Path(folder)
            kit=Path(__file__).resolve().parents[2]
            config=parse_config({'protocol':1,'kit_root':str(kit),'python':sys.executable,
                'state_root':str(root/'state'),'host_id':'startup-fixture',
                'native_hosts':{'codex':sys.executable},'profiles':{'codex':{'profile_root':str(root)}}})
            files=startup.render(config,root/'prepared')
            xml=ET.fromstring(files['startup-task.xml'])
            ns={'t':'http://schemas.microsoft.com/windows/2004/02/mit/task'}
            self.assertEqual(xml.findtext('t:Principals/t:Principal/t:UserId',namespaces=ns),process_sids(os.getpid())[0])
            self.assertEqual(xml.findtext('t:Principals/t:Principal/t:LogonType',namespaces=ns),'InteractiveToken')
            self.assertEqual(xml.findtext('t:Principals/t:Principal/t:RunLevel',namespaces=ns),'LeastPrivilege')
            self.assertIn('-WindowStyle Hidden',xml.findtext('t:Actions/t:Exec/t:Arguments',namespaces=ns))
            script=files['start-bridge.ps1'].decode('utf-8')
            self.assertIn('-WindowStyle Hidden',script)
            self.assertIn('-Wait -PassThru',script)
            self.assertNotIn('ExecutionPolicy',script)
            self.assertFalse((root/'prepared').exists())

    def test_actual_hidden_launcher_preserves_path_arguments_and_child_exit_code(self):
        with tempfile.TemporaryDirectory(prefix='bridge-startup-') as folder:
            root=Path(folder)/"owned space $ data's café 東京";root.mkdir()
            kit=Path(__file__).resolve().parents[2]
            config=parse_config({'protocol':1,'kit_root':str(kit),'python':sys.executable,
                'state_root':str(root/'state'),'host_id':'startup-fixture',
                'native_hosts':{'codex':sys.executable},'profiles':{'codex':{'profile_root':str(root)}}})
            files=startup.render(config,root)
            script=root/'start-bridge.ps1';script.write_bytes(files['start-bridge.ps1'])
            dummy=root/'fixture kit';(dummy/'optional/bridge/tools').mkdir(parents=True)
            result=root/'argv.json'
            (dummy/'optional/bridge/tools/bridge_service.py').write_text(
                'import json,sys; from pathlib import Path; Path('+repr(str(result))+').write_text(json.dumps({'
                '"argv":sys.argv,"isolated":bool(sys.flags.isolated),"bytecode_disabled":sys.dont_write_bytecode})); sys.exit(23)',encoding='utf-8')
            runtime=root/'runtime.json'
            runtime.write_text(json.dumps({'kit_root':str(dummy),'python':sys.executable},ensure_ascii=False),encoding='utf-8')
            powershell=Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
            with Job(kill_on_close=True) as job:
                process=job.start([str(powershell),'-NoProfile','-NonInteractive','-WindowStyle','Hidden',
                    '-File',str(script),'-Config',str(runtime)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                    creationflags=subprocess.CREATE_NO_WINDOW,close_fds=True)
                output,error=process.communicate(timeout=10)
                self.assertEqual(process.returncode,23,error.decode(errors='replace'))
            captured=json.loads(result.read_text())
            self.assertEqual(captured['argv'][1:],['--config',str(runtime)])
            self.assertIs(captured['isolated'],True)
            self.assertIs(captured['bytecode_disabled'],True)

    def test_native_scheduler_validates_xml_without_registering_a_task(self):
        with tempfile.TemporaryDirectory(prefix='bridge-task-validation-') as folder:
            root=Path(folder);kit=Path(__file__).resolve().parents[2]
            config=parse_config({'protocol':1,'kit_root':str(kit),'python':sys.executable,
                'state_root':str(root/'state'),'host_id':'validation-fixture',
                'native_hosts':{'codex':sys.executable},'profiles':{'codex':{'profile_root':str(root)}}})
            xml=root/'startup-task.xml';xml.write_bytes(startup.render(config,root)['startup-task.xml'])
            script=root/'validate.ps1'
            script.write_text("param([string]$Xml)\n$ErrorActionPreference='Stop'\n"
                "$service=New-Object -ComObject 'Schedule.Service'\n$service.Connect()\n"
                "$folder=$service.GetFolder('\\')\n"
                "$folder.RegisterTask('AgentBridge-Validation-Only',[System.IO.File]::ReadAllText($Xml),1,$null,$null,3,$null) | Out-Null\n")
            powershell=Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
            with Job(kill_on_close=True) as job:
                process=job.start([str(powershell),'-NoProfile','-NonInteractive','-WindowStyle','Hidden',
                    '-File',str(script),'-Xml',str(xml)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                    creationflags=subprocess.CREATE_NO_WINDOW,close_fds=True)
                output,error=process.communicate(timeout=10)
                self.assertEqual(process.returncode,0,error.decode(errors='replace'))
