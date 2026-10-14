"""Render per-user startup review artifacts without registering or starting tasks."""
import hashlib
import os
from pathlib import Path
import xml.etree.ElementTree as ET

from optional.bridge.core.messages import DeliveryError
from kitlib.proc.pipe import process_sids
from .runtime import Config
from optional.bridge.paths import SERVICE_REL
from .evidence import regular

NS='http://schemas.microsoft.com/windows/2004/02/mit/task'


def _quoted_argument(path):
    value=str(path)
    if '"' in value or any(character in value for character in '\r\n\x00'):
        raise DeliveryError('invalid-startup-path')
    return '"'+value+'"'


def render(config,prepared_directory):
    if type(config) is not Config: raise DeliveryError('invalid-startup-config')
    output=Path(prepared_directory)
    if not output.is_absolute(): raise DeliveryError('absolute-startup-directory-required')
    powershell=Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
    regular(powershell)
    script=output/'start-bridge.ps1'
    runtime=output/'runtime.json'
    # Paths are passed as data to Start-Process, never interpolated into shell
    # source or command expressions. No execution-policy or permission override.
    launcher='''param([Parameter(Mandatory=$true)][string]$Config)
$ErrorActionPreference = 'Stop'
$resolved = (Resolve-Path -LiteralPath $Config).Path
$settings = Get-Content -LiteralPath $resolved -Raw -Encoding UTF8 | ConvertFrom-Json
$entry = Join-Path $settings.kit_root '%s'
$arguments = @('-I', '-B', ('"' + $entry + '"'), '--config', ('"' + $resolved + '"'))
$process = Start-Process -FilePath $settings.python -ArgumentList $arguments -WindowStyle Hidden -Wait -PassThru
exit $process.ExitCode
'''%SERVICE_REL
    root=ET.Element('Task',{'xmlns':NS,'version':'1.2'})
    def child(parent,name,value=None,attributes=None):
        node=ET.SubElement(parent,name,attributes or {})
        if value is not None: node.text=value
        return node
    sid=process_sids(os.getpid())[0]
    info=child(root,'RegistrationInfo')
    child(info,'Description','Per-user local agent bridge. Register only after reviewing installation and native acceptance.')
    triggers=child(root,'Triggers')
    trigger=child(triggers,'LogonTrigger')
    child(trigger,'Enabled','true')
    child(trigger,'UserId',sid)
    principals=child(root,'Principals')
    principal=child(principals,'Principal',attributes={'id':'Owner'})
    child(principal,'UserId',sid)
    child(principal,'LogonType','InteractiveToken')
    child(principal,'RunLevel','LeastPrivilege')
    settings=child(root,'Settings')
    for name,value in (('MultipleInstancesPolicy','IgnoreNew'),('DisallowStartIfOnBatteries','false'),
            ('StopIfGoingOnBatteries','false'),('StartWhenAvailable','true'),('ExecutionTimeLimit','PT0S')):
        child(settings,name,value)
    restart=child(settings,'RestartOnFailure')
    child(restart,'Interval','PT1M')
    child(restart,'Count','3')
    actions=child(root,'Actions',attributes={'Context':'Owner'})
    action=child(actions,'Exec')
    child(action,'Command',str(powershell))
    child(action,'Arguments','-NoProfile -NonInteractive -WindowStyle Hidden -File '+
        _quoted_argument(script)+' -Config '+_quoted_argument(runtime))
    child(action,'WorkingDirectory',str(config.kit_root))
    return {'start-bridge.ps1':launcher.encode('utf-8'),
        # Task Scheduler consumes a Unicode BSTR through its native COM API.
        # UTF-16 with a BOM also roundtrips correctly through file readers.
        'startup-task.xml':ET.tostring(root,encoding='utf-16',xml_declaration=True)}


def task_name(config):
    value=process_sids(os.getpid())[0]+'\0'+str(config.state_root).casefold()
    return 'AgentBridge-'+hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]
