"""Claude CLI account facts and native, process-bound PreToolUse attribution.

Only the private controller calls observe_hook with kernel-authenticated process
metadata. JSON cannot choose a producer, sender, task root or account. Configured
hook_argv is an exact exec-form command, never a model argument. Hooks observe
native permission_mode; they do not authorize routes or approve native tools.

The controller must preserve the native permission pipeline and verify its owned
RPC helper before this port. This protects normal plugin calls, not a deliberate
same-user rewrite of the application or hook installation on the full computer.
"""
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import threading
import time

from optional.bridge.hosts.claude.records import read_record
from optional.bridge.hosts.claude.sessions import ClaudeDirectory, ClaudeSession
from kitlib.ident import identifier
from optional.bridge.core.callers import CallProofs
from optional.bridge.core.contracts import SessionIdentity, WorkspaceIdentity
from optional.bridge.core.messages import MessageIntent, SenderAuthority, text_bytes
from optional.bridge.core.mcp import TOOLS, _args
from optional.bridge.core.pairing import ChatBinding
from kitlib.proc.pipe import process_image, process_sids
from optional.bridge.win.evidence import document, regular, stable_bytes
from optional.bridge.win.launch import PYTHON_FLAGS
from kitlib.proc.process import ancestors_of, command_line, proc_ctime

UUID=re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z',re.I)
MODES=frozenset(('default','acceptEdits','auto','dontAsk','bypassPermissions'))
MAX_HOOK_BYTES=256*1024
MAX_PENDING=256
MAX_SEEN=8192
HOOK_TTL=5


class TerminalError(ValueError):
    def __init__(self,code):
        self.code=code
        super().__init__('native Claude terminal unavailable: '+code)


def _sha(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,
        separators=(',',':'),allow_nan=False).encode('utf-8')).hexdigest()


def _workspace(value):
    if type(value) is not str or not Path(value).is_absolute():
        raise TerminalError('native-workspace-unverified')
    # Check the supplied directory before resolving so a direct junction is not
    # accepted as ordinary evidence. The controller owns configured ancestors.
    info=regular(value,directory=True)
    path=Path(value).resolve(strict=True)
    current=regular(path,directory=True)
    if (info.st_dev,info.st_ino)!=(current.st_dev,current.st_ino):
        raise TerminalError('native-workspace-changed')
    return (info.st_dev,info.st_ino)


class ClaudeTerminal:
    """Controller-selected account/profile pair, including alternate profiles.

    Native oauthAccount is profile metadata, not a credential/authentication
    probe. The controller selects the account file and its associated projects
    root together; the authenticated hook must confirm that exact root. No
    default profile, login state, or process environment is inferred.
    """
    def __init__(self,account_file,*,transcript_root):
        raw=Path(account_file)
        if not raw.is_absolute(): raise TerminalError('invalid-account-path')
        regular(raw)
        regular(raw.parent,directory=True)
        self.path=raw.resolve(strict=True)
        self.parent_identity=_workspace(str(self.path.parent))
        native_root=Path(transcript_root)
        if not native_root.is_absolute(): raise TerminalError('invalid-transcript-root')
        self.transcript_identity=_workspace(str(native_root))
        self.transcript_root=native_root.resolve(strict=True)

    def transcript(self,value,session):
        """Native path provenance, not asynchronously lagging transcript contents."""
        try:
            if _workspace(str(self.transcript_root))!=self.transcript_identity:
                raise TerminalError('native-transcript-root-changed')
            if (not (type(value) is str or isinstance(value,Path)) or not Path(value).is_absolute()
                    or type(session) is not str or UUID.fullmatch(session) is None):
                raise TerminalError('native-transcript-unverified')
            raw=Path(value)
            info=regular(raw)
            path=raw.resolve(strict=True)
            parts=path.relative_to(self.transcript_root).parts
            if len(parts)!=2 or path.name.lower()!=session.lower()+'.jsonl':
                raise TerminalError('native-transcript-profile-or-session-mismatch')
            regular(path.parent,directory=True)
            current=regular(path)
            if (info.st_dev,info.st_ino)!=(current.st_dev,current.st_ino):
                raise TerminalError('native-transcript-changed')
            return path,(info.st_dev,info.st_ino)
        except TerminalError: raise
        except Exception: raise TerminalError('native-transcript-unverified') from None

    def observe_account(self):
        try:
            if _workspace(str(self.path.parent))!=self.parent_identity:
                raise TerminalError('native-account-parent-changed')
            raw=stable_bytes(self.path,4*1024*1024)
            config=document(raw)
            account=config.get('oauthAccount')
            if type(account) is not dict: raise TerminalError('native-account-unverified')
            values=[]
            for key in ('accountUuid','organizationUuid'):
                value=account.get(key)
                if type(value) is not str or UUID.fullmatch(value) is None:
                    raise TerminalError('native-account-unverified')
                values.append(value.lower())
            # No email, token, display name, or role enters the public reference.
            reference=_sha(['claude-terminal',*values])
            if stable_bytes(self.path,4*1024*1024)!=raw:
                raise TerminalError('native-account-changed')
            return reference
        except TerminalError: raise
        except Exception: raise TerminalError('native-account-evidence-unavailable') from None

    def account_probe(self,record,registry):
        if type(record) is not dict or record.get('entrypoint')!='cli':
            raise TerminalError('not-native-terminal')
        return self.observe_account()


@dataclass(frozen=True)
class TerminalCall:
    """Private controller metadata; construction alone is no attestation."""
    ticket:bytes=field(repr=False)
    tool_name:str
    input_sha256:str


@dataclass(frozen=True)
class _HookContext:
    snapshot:ClaudeSession
    authority:SenderAuthority
    prompt_id:str
    tool_use_id:str
    tool_name:str
    input_sha256:str
    arguments:dict=field(repr=False)
    transcript_path:Path=field(repr=False)
    transcript_identity:tuple
    expires:float


class TerminalCalls:
    def __init__(self,directory,terminal,host_id,client_pid,client_creation,*,
            hook_argv,bridge_tools,clock=time.monotonic):
        if type(directory) is not ClaudeDirectory or type(terminal) is not ClaudeTerminal:
            raise TerminalError('invalid-native-components')
        if (type(client_pid) is not int or not 0<client_pid<=0xffffffff
                or type(client_creation) is not int or client_creation<=0 or not callable(clock)):
            raise TerminalError('invalid-native-client')
        if (type(hook_argv) not in (list,tuple) or len(hook_argv)<2
                or any(type(item) is not str or not item for item in hook_argv)
                or not Path(hook_argv[0]).is_absolute()):
            raise TerminalError('exact-hook-command-required')
        self.hook_image=Path(hook_argv[0]).resolve(strict=True)
        self.hook_command=subprocess.list2cmdline(hook_argv)
        self.hook_script=None
        script_index=3 if tuple(hook_argv[1:3])==PYTHON_FLAGS else 2 if hook_argv[1]=='-I' else 1
        if hook_argv[1]!='-c':
            if len(hook_argv)<=script_index or not Path(hook_argv[script_index]).is_absolute():
                raise TerminalError('exact-hook-command-required')
            self.hook_script=Path(hook_argv[script_index])
            self.hook_script_digest=hashlib.sha256(stable_bytes(self.hook_script,MAX_HOOK_BYTES)).digest()
        if type(bridge_tools) is not dict or not 0<len(bridge_tools)<=len(TOOLS):
            raise TerminalError('verified-tool-provenance-required')
        self.tools={}
        for name,source in bridge_tools.items():
            if (type(name) is not str or not name.startswith('mcp__') or name.split('__')[-1] not in TOOLS
                    or type(source) is not dict or set(source)!={'name','source'}
                    or type(source['name']) is not str or not source['name']
                    or source['source'] not in ('plugin','sdk','user','project','local','managed')):
                raise TerminalError('verified-tool-provenance-required')
            self.tools[name]=dict(source)
        self.directory,self.terminal,self.host_id=directory,terminal,identifier(host_id)
        self.pid,self.creation=client_pid,client_creation
        self.image=process_image(client_pid)
        self.clock=clock
        self._pending,self._seen,self._bindings,self._leased={},set(),{},set()
        self._native_transcripts={}
        self._lock=threading.RLock()
        self.proofs=CallProofs(observe=self.observe,clock=clock)
        self._require_client()

    def _require_process(self,pid,creation,expected_image=None):
        if (type(pid) is not int or not 0<pid<=0xffffffff or type(creation) is not int or creation<=0
                or proc_ctime(pid)!=creation or process_sids(pid)[0]!=self.directory.user
                or expected_image is not None and process_image(pid)!=expected_image
                or proc_ctime(pid)!=creation):
            raise TerminalError('native-process-changed')

    def _require_client(self):
        self._require_process(self.pid,self.creation,self.image)

    def _closest(self,pid,creation):
        self._require_process(pid,creation)
        lineage=[(pid,creation),*ancestors_of(pid,creation,limit=8)]
        for native_pid,native_creation in lineage:
            path=self.directory.registry/('%d.json'%native_pid)
            if not path.is_file(): continue
            record=read_record(path)
            # A registry-bearing inner caller must not inherit an outer chat.
            if record.get('entrypoint') not in ('cli','claude-desktop'):
                raise TerminalError('closest-native-caller-unverified')
            snapshots={(item.pid,item.creation):item for item in self.directory.discover()}
            snapshot=snapshots.get((native_pid,native_creation))
            if snapshot is None or snapshot.kind!='terminal':
                raise TerminalError('closest-native-caller-unverified')
            if snapshot.executable not in self.directory.executables:
                raise TerminalError('closest-native-caller-unverified')
            self._require_process(native_pid,native_creation,snapshot.executable)
            self.directory.require_live(snapshot)
            return snapshot
        raise TerminalError('native-caller-not-found')

    def _producer(self,authenticated_process,frontend):
        # Only an internal object carrying kernel-supplied identity is accepted;
        # ordinary model JSON dictionaries never reach the process verifier.
        pid=getattr(authenticated_process,'pid',None)
        creation=getattr(authenticated_process,'creation',None)
        self._require_process(pid,creation)
        lineage=[(pid,creation),*ancestors_of(pid,creation,limit=3)]
        producer=None
        # The shared IPC helper may be one child below the actual hook producer.
        for index,(candidate,ctime) in enumerate(lineage[:2]):
            if (process_image(candidate)==self.hook_image
                    and command_line(candidate,ctime)==self.hook_command):
                if index+1>=len(lineage) or lineage[index+1]!=(frontend.pid,frontend.creation):
                    raise TerminalError('hook-native-parent-mismatch')
                self._require_process(candidate,ctime,self.hook_image)
                producer=(candidate,ctime)
                break
        if producer is None: raise TerminalError('hook-producer-unverified')
        if self.hook_script is not None and hashlib.sha256(
                stable_bytes(self.hook_script,MAX_HOOK_BYTES)).digest()!=self.hook_script_digest:
            raise TerminalError('hook-producer-script-changed')
        if self._closest(pid,creation)!=frontend:
            raise TerminalError('hook-native-caller-mismatch')
        return pid,creation

    def _binding(self,snapshot,mode):
        workspace=_sha(snapshot.workspace_identity)
        permission=_sha({'permission_mode':mode})
        incarnation=_sha([snapshot.pid,snapshot.creation,permission])
        identity=SessionIdentity(snapshot.native_session_id,snapshot.native_session_id,'claude-peer',
            incarnation,WorkspaceIdentity(self.host_id,workspace,workspace))
        return ChatBinding(identity,snapshot.account_ref,int(permission[:13],16))

    def observe_hook(self,authenticated_process,envelope):
        """Private producer sink. Returns an opaque ticket, never an owner grant."""
        try:
            if type(envelope) is not bytes or not 0<len(envelope)<=MAX_HOOK_BYTES:
                raise TerminalError('invalid-hook-envelope')
            event=document(envelope)
            with self._lock:
                self._require_client()
                frontend=self._closest(self.pid,self.creation)
                producer=self._producer(authenticated_process,frontend)
                if event.get('hook_event_name')!='PreToolUse':
                    raise TerminalError('native-pretooluse-required')
                if event.get('session_id')!=frontend.native_session_id:
                    raise TerminalError('hook-native-session-mismatch')
                if _workspace(event.get('cwd'))!=frontend.workspace_identity:
                    raise TerminalError('hook-native-workspace-mismatch')
                transcript_path,transcript_identity=self.terminal.transcript(
                    event.get('transcript_path'),frontend.native_session_id)
                mode=event.get('permission_mode')
                if type(mode) is not str or mode not in MODES:
                    raise TerminalError('native-permission-mode-unverified')
                prompt=event.get('prompt_id')
                use=event.get('tool_use_id')
                if (type(prompt) is not str or UUID.fullmatch(prompt) is None
                        or type(use) is not str or not 1<=len(use)<=128 or not use.isascii()
                        or re.fullmatch('[A-Za-z0-9_-]+',use) is None):
                    raise TerminalError('native-intent-correlation-unverified')
                tool=event.get('tool_name')
                if type(tool) is not str or tool not in self.tools or event.get('mcp_server')!=self.tools[tool]:
                    raise TerminalError('native-tool-provenance-unverified')
                arguments=_args(tool.split('__')[-1],event.get('tool_input'))
                if 'text' in arguments: text_bytes(arguments['text'])
                now=self.clock()
                self._pending={key:value for key,value in self._pending.items() if value.expires>now}
                replay=(frontend.pid,frontend.creation,prompt.lower(),use)
                # Retain consumed/expired use IDs for the native process lifetime.
                # Capacity fails closed rather than evicting replay protection.
                if replay in self._seen: raise TerminalError('hook-replay')
                if len(self._seen)>=MAX_SEEN or len(self._pending)>=MAX_PENDING:
                    raise TerminalError('hook-context-capacity')
                account=self.terminal.observe_account()
                if account!=frontend.account_ref: raise TerminalError('native-account-changed')
                binding=self._binding(frontend,mode)
                self.directory.require_live(frontend)
                self._require_client()
                self._require_process(*producer)
                if self.terminal.observe_account()!=account:
                    raise TerminalError('native-account-changed')
                ticket=secrets.token_bytes(32)
                self._pending[ticket]=_HookContext(frontend,SenderAuthority(binding,frontend.native_session_id),
                    prompt.lower(),use,tool,_sha(arguments),dict(arguments),
                    transcript_path,transcript_identity,now+HOOK_TTL)
                self._seen.add(replay)
                self._bindings[(frontend.pid,frontend.creation)]=binding
                self._native_transcripts[(frontend.pid,frontend.creation)]=(transcript_path,transcript_identity)
                return ticket
        except TerminalError: raise
        except Exception: raise TerminalError('native-hook-evidence-unavailable') from None

    def metadata(self,tool_name,arguments,*,ticket=None):
        """Private RPC request correlation, outside ordinary MCP arguments/_meta."""
        try:
            with self._lock:
                if type(tool_name) is not str or type(arguments) is not dict:
                    raise TerminalError('invalid-native-call')
                checksum=_sha(arguments)
                if ticket is None:
                    found=[key for key,value in self._pending.items() if value.tool_name==tool_name
                        and value.input_sha256==checksum and value.expires>self.clock()]
                    if len(found)!=1: raise TerminalError('hook-context-required')
                    ticket=found[0]
                context=self._pending.get(ticket) if type(ticket) is bytes else None
                if context is None or context.tool_name!=tool_name or context.input_sha256!=checksum:
                    raise TerminalError('hook-call-mismatch')
                metadata=TerminalCall(ticket,tool_name,checksum)
                self.observe(metadata)
                return metadata
        except TerminalError: raise
        except Exception: raise TerminalError('native-call-evidence-unavailable') from None

    def prepare_request(self,native_request):
        """Private MCP tools/call seam: (metadata, consume_once).

        The controller calls this only after its kernel inspector verifies the
        actual frontend, then injects metadata outside model params._meta. Read
        observers may consult metadata repeatedly during that one request. The
        controller invokes consume in finally, including native/core refusals.
        """
        try:
            with self._lock:
                if (type(native_request) is not dict or native_request.get('jsonrpc')!='2.0'
                        or native_request.get('method')!='tools/call'
                        or 'id' not in native_request or type(native_request['id']) not in (str,int)
                        or set(native_request)-{'jsonrpc','method','id','params'}):
                    raise TerminalError('invalid-native-request')
                params=native_request.get('params')
                if type(params) is not dict or set(params)-{'name','arguments','_meta'}:
                    raise TerminalError('invalid-native-request')
                name=params.get('name')
                tools=[tool for tool in self.tools if tool.split('__')[-1]==name]
                if len(tools)!=1: raise TerminalError('native-tool-provenance-unverified')
                arguments=_args(name,params.get('arguments',{}))
                checksum=_sha(arguments)
                found=[ticket for ticket,context in self._pending.items() if ticket not in self._leased
                    and context.tool_name==tools[0] and context.input_sha256==checksum
                    and context.expires>self.clock()]
                if len(found)!=1: raise TerminalError('hook-context-required')
                ticket=found[0]
                metadata=self.metadata(tools[0],arguments,ticket=ticket)
                self._leased.add(ticket)
                consumed=False
                def consume():
                    nonlocal consumed
                    with self._lock:
                        if consumed: return
                        consumed=True
                        self._pending.pop(ticket,None)
                        self._leased.discard(ticket)
                return metadata,consume
        except TerminalError: raise
        except Exception: raise TerminalError('native-request-evidence-unavailable') from None

    def observe(self,metadata):
        """Non-consuming observer for CallProofs; use issue() once at request admission."""
        try:
            with self._lock:
                if type(metadata) is not TerminalCall: raise TerminalError('hook-context-required')
                context=self._pending.get(metadata.ticket)
                if (context is None or context.expires<=self.clock() or context.tool_name!=metadata.tool_name
                        or context.input_sha256!=metadata.input_sha256):
                    raise TerminalError('hook-context-expired-or-mismatched')
                self._require_client()
                snapshot=self._closest(self.pid,self.creation)
                if (snapshot!=context.snapshot or self.terminal.observe_account()!=snapshot.account_ref
                        or self._bindings.get((snapshot.pid,snapshot.creation))!=context.authority.sender):
                    raise TerminalError('native-context-changed')
                if self.terminal.transcript(context.transcript_path,snapshot.native_session_id)[1]!=context.transcript_identity:
                    raise TerminalError('native-transcript-changed')
                self.directory.require_live(snapshot)
                return context.authority
        except TerminalError: raise
        except Exception: raise TerminalError('native-call-evidence-unavailable') from None

    def require_live(self,binding):
        """Controller pairing port: native liveness/account and latest observed mode.

        Hook evidence is not a complete native permission rule snapshot. Native
        permissions must still run; changed modes invalidate calls at every hook.
        """
        try:
            with self._lock:
                if type(binding) is not ChatBinding: return False
                snapshot=self._closest(self.pid,self.creation)
                self._require_client()
                transcript=self._native_transcripts.get((snapshot.pid,snapshot.creation))
                return (transcript is not None
                    and self.terminal.transcript(transcript[0],snapshot.native_session_id)[1]==transcript[1]
                    and self._bindings.get((snapshot.pid,snapshot.creation))==binding
                    and snapshot.account_ref==binding.account_ref
                    and self.terminal.observe_account()==binding.account_ref)
        except Exception: return False

    def issue(self,intent,metadata):
        """Consume native hook correlation and delegate proof custody to shared core."""
        with self._lock:
            self.observe(metadata)
            context=self._pending[metadata.ticket]
            try:
                if type(intent) is not MessageIntent: raise TerminalError('invalid-native-intent')
                arguments=context.arguments
                tool=context.tool_name.split('__')[-1]
                if arguments.get('operation_id')!=intent.operation_id:
                    raise TerminalError('hook-intent-mismatch')
                if tool=='bridge_send' and (arguments['recipient']!=intent.recipient.reference
                        or hashlib.sha256(text_bytes(arguments['text'])).hexdigest()!=intent.body_sha256
                        or arguments.get('expires_at')!=intent.expires_at):
                    raise TerminalError('hook-intent-mismatch')
                # Reads/replies are correlated to the observed operation. The
                # shared broker checks that operation's sender/recipient/task.
                return self.proofs.issue(intent,metadata)
            finally:
                self._pending.pop(metadata.ticket,None)
