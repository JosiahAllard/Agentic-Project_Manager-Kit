"""Native Claude desktop account/task/permission and process-bound caller facts.

Configured roots come from the controller, never tool arguments. A desktop record
is not substituted for terminal identity. No model-supplied role or name grants a
connection or chooses the caller. The frontend process identity must be obtained
from its owned stdio/private IPC connection before constructing ClaudeCalls.
"""
from dataclasses import dataclass,field
import hashlib
import json
import os
from pathlib import Path
import re

from optional.bridge.hosts.claude.records import read_record
from optional.bridge.hosts.claude.sessions import ClaudeDirectory,ClaudeSession
from kitlib.ident import identifier
from optional.bridge.core.contracts import SessionIdentity,WorkspaceIdentity
from optional.bridge.core.messages import SenderAuthority
from optional.bridge.core.pairing import ChatBinding
from kitlib.proc.pipe import process_sids
from optional.bridge.win.evidence import document,regular,stable_bytes
from kitlib.proc.process import ancestors_of,proc_ctime

UUID=re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z',re.I)
TASK=re.compile(r'local_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z',re.I)
MODES={'default':'prompting','acceptEdits':'prompting','auto':'prompting','dontAsk':'prompting',
    'bypassPermissions':'bypass'}
MAX_ENTRIES=8192
MAX_TOTAL_BYTES=64*1024*1024
POLICY_FIELDS=('permissionMode','chromePermissionMode','sessionPermissionUpdates',
    'alwaysAllowedReasons','toolSurfaceSnapshot','remoteMcpServersConfig')


class ClaudeContextError(ValueError):
    def __init__(self,code):
        self.code=code
        super().__init__('native Claude context unavailable: '+code)


def _sha(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def _workspace(value):
    if type(value) is not str or not Path(value).is_absolute():
        raise ClaudeContextError('native-workspace-unverified')
    path=Path(value).resolve(strict=True)
    info=regular(path,directory=True)
    return (info.st_dev,info.st_ino)


@dataclass(frozen=True)
class ClaudeContext:
    task_id:str
    cli_session_id:str
    workspace_identity:tuple
    account_ref:str=field(repr=False)
    mode:str
    permission_sha256:str
    title:str|None=field(default=None,compare=False)

    def binding(self,snapshot,host_id):
        if (type(snapshot) is not ClaudeSession or snapshot.kind!='desktop'
                or snapshot.native_session_id!=self.cli_session_id or snapshot.account_ref!=self.account_ref
                or snapshot.workspace_identity!=self.workspace_identity):
            raise ClaudeContextError('native-binding-mismatch')
        workspace=_sha(self.workspace_identity)
        incarnation=_sha([snapshot.pid,snapshot.creation,self.permission_sha256])
        return ChatBinding(SessionIdentity(self.task_id,self.cli_session_id,'claude-peer',incarnation,
            WorkspaceIdentity(identifier(host_id),workspace,workspace)),self.account_ref,
            int(self.permission_sha256[:13],16))


class ClaudeDesktop:
    def __init__(self,profile_root):
        raw=Path(profile_root)
        if not raw.is_absolute(): raise ClaudeContextError('invalid-native-profile')
        info=regular(raw,directory=True)
        self.root=raw.resolve(strict=True)
        self.identity=(info.st_dev,info.st_ino)

    def _account(self):
        info=regular(self.root,directory=True)
        if (info.st_dev,info.st_ino)!=self.identity:
            raise ClaudeContextError('native-profile-changed')
        data=stable_bytes(self.root/'config.json',1024*1024)
        config=document(data)
        account=config.get('lastKnownAccountUuid')
        if (config.get('windowSizeWasSignedIn') is not True or type(account) is not str
                or UUID.fullmatch(account) is None):
            raise ClaudeContextError('native-account-unverified')
        account=account.lower()
        reference=hashlib.sha256(('claude-desktop\0'+account).encode()).hexdigest()
        return account,reference,data

    def _files(self,account):
        root=self.root/'claude-code-sessions'
        regular(root,directory=True)
        scanned,files,queue=0,[],[(root,0)]
        while queue:
            directory,depth=queue.pop()
            regular(directory,directory=True)
            for path in directory.iterdir():
                scanned+=1
                if scanned>MAX_ENTRIES: raise ClaudeContextError('native-task-scan-limit')
                if depth==0 and path.name.lower()!=account: continue
                if depth<2 and UUID.fullmatch(path.name):
                    regular(path,directory=True)
                    queue.append((path,depth+1))
                elif depth==2 and path.suffix=='.json' and TASK.fullmatch(path.stem):
                    regular(path)
                    files.append(path)
        return tuple(sorted(files))

    def observe(self,record):
        try:
            if type(record) is not dict or record.get('entrypoint')!='claude-desktop':
                raise ClaudeContextError('not-native-desktop')
            session=record.get('sessionId')
            if type(session) is not str or UUID.fullmatch(session) is None:
                raise ClaudeContextError('invalid-native-cli-session')
            account,reference,config=self._account()
            files=self._files(account)
            selected,aliases=[],[]
            total,versions=0,{}
            for path in files:
                data=stable_bytes(path,4*1024*1024)
                total+=len(data)
                if total>MAX_TOTAL_BYTES: raise ClaudeContextError('native-task-data-limit')
                versions[path]=hashlib.sha256(data).digest()
                task=document(data)
                if task.get('cliSessionId')==session: selected.append((path,task))
                if (type(task.get('bridgeSessionIds')) is list
                        and record.get('bridgeSessionId') in task['bridgeSessionIds']):
                    aliases.append(path)
            if len(selected)!=1: raise ClaudeContextError('native-task-missing-or-ambiguous')
            path,task=selected[0]
            task_id=task.get('sessionId')
            if (type(task_id) is not str or TASK.fullmatch(task_id) is None or task_id!=path.stem
                    or task.get('isArchived') is not False):
                raise ClaudeContextError('native-task-identity-mismatch')
            bridge=record.get('bridgeSessionId')
            if bridge is not None:
                identifier(bridge)  # Native aliases include 32-hex IDs, not only UUIDs.
                if aliases!=[path]: raise ClaudeContextError('native-bridge-alias-mismatch')
            if record.get('hostSessionId',task_id)!=task_id:
                raise ClaudeContextError('native-host-task-mismatch')
            workspace=_workspace(record.get('cwd'))
            if _workspace(task.get('cwd'))!=workspace:
                raise ClaudeContextError('native-workspace-mismatch')
            mode=MODES.get(task.get('permissionMode'))
            if mode is None: raise ClaudeContextError('native-permission-mode-unverified')
            if self._files(account)!=files or self._account()!=(account,reference,config):
                raise ClaudeContextError('native-evidence-changed')
            for file,digest in versions.items():
                if hashlib.sha256(stable_bytes(file,4*1024*1024)).digest()!=digest:
                    raise ClaudeContextError('native-evidence-changed')
            title=task.get('title')
            if type(title) is not str or len(title)>1024: title=None
            return ClaudeContext(task_id,session,workspace,reference,mode,
                _sha({key:task.get(key) for key in POLICY_FIELDS}),title)
        except ClaudeContextError:
            raise
        except Exception:
            raise ClaudeContextError('native-evidence-unavailable') from None

    def account_probe(self,record,registry):
        return self.observe(record).account_ref


class ClaudeCalls:
    def __init__(self,directory,desktop,host_id,client_pid,client_creation):
        if type(directory) is not ClaudeDirectory or type(desktop) is not ClaudeDesktop:
            raise ClaudeContextError('invalid-native-components')
        if (type(client_pid) is not int or not 0<client_pid<=0xffffffff
                or type(client_creation) is not int or client_creation<=0):
            raise ClaudeContextError('invalid-native-client')
        self.directory,self.desktop,self.host_id=directory,desktop,identifier(host_id)
        self.pid,self.creation=client_pid,client_creation

    def _require_client(self):
        if proc_ctime(self.pid)!=self.creation or process_sids(self.pid)[0]!=self.directory.user:
            raise ClaudeContextError('native-client-changed')

    def observe(self,host_metadata):
        self._require_client()
        ancestry=[(self.pid,self.creation),*ancestors_of(self.pid,self.creation,limit=8)]
        snapshots={(item.pid,item.creation):item for item in self.directory.discover()}
        for pid,creation in ancestry:
            path=self.directory.registry/('%d.json'%pid)
            if not path.is_file(): continue
            record=read_record(path)
            if record.get('entrypoint') not in ('cli','claude-desktop'): continue
            # Do not skip an unavailable closest native caller and accidentally
            # borrow the identity of a different outer chat in its ancestry.
            snapshot=snapshots.get((pid,creation))
            if snapshot is None or snapshot.kind!='desktop':
                raise ClaudeContextError('closest-native-caller-unverified')
            self.directory.require_live(snapshot)
            facts=self.desktop.observe(record)
            self._require_client()
            return SenderAuthority(facts.binding(snapshot,self.host_id),facts.task_id)
        raise ClaudeContextError('native-caller-not-found')
