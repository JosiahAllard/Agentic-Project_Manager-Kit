"""Observe existing Codex chats without acquiring their native writer.

The caller metadata must arrive from a verified host-owned frontend, outside
ordinary tool arguments. This reader verifies its match to native persisted
identity/active-turn/permission facts; it does not authenticate arbitrary callers
that can invoke Python or manufacture metadata. Current login and frontend IPC
verification are separate deployment requirements. No native state is modified.
"""
import ctypes
from ctypes import wintypes as w
from dataclasses import dataclass, field
import hashlib
import json
import ntpath
import os
from pathlib import Path
import re
import stat

from optional.bridge.core.contracts import SessionIdentity, WorkspaceIdentity
from optional.bridge.core.jsonwire import JsonLineDecoder
from optional.bridge.core.pairing import ChatBinding
from kitlib.proc.pipe import K, bind, close, open_process, process_image, process_sids
from kitlib.proc.process import handle_ctime

MAX_ENTRIES, MAX_DIRECTORIES = 100000, 4096
MAX_ROLLOUT_BYTES = 256*1024*1024
MAX_AUTHORITY_BYTES = 128*1024
RECORD_TRIES = 3
UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z')
HEADER = re.compile(rb'\A\{\s*"timestamp"\s*:\s*"[0-9TtZz:.+ -]{1,64}"\s*,\s*"type"\s*:\s*"([a-z_]+)"\s*,\s*"payload"\s*:\s*')
EVENT = re.compile(rb'\A\{\s*"type"\s*:\s*"([a-z_]+)"')
LIFECYCLE = frozenset(('task_started','task_complete','task_completed','task_aborted',
    'turn_started','turn_complete','turn_completed','turn_aborted'))
STARTED = frozenset(('task_started','turn_started'))
PROMPTING = frozenset(('on-request','on-failure','untrusted'))
get_pid = bind(K,'GetProcessId',[w.HANDLE],w.DWORD)
wait = bind(K,'WaitForSingleObject',[w.HANDLE,w.DWORD],w.DWORD)


class ContextError(ValueError):
    def __init__(self, code):
        self.code=code
        super().__init__('native Codex context unavailable: '+code)


def _sha(value):
    data=json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode('utf-8')
    return hashlib.sha256(data).hexdigest()


def _regular(path, *, directory=False):
    info=path.lstat()
    if (path.is_symlink() or getattr(info,'st_file_attributes',0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))):
        raise ContextError('nonregular-native-evidence')
    return info


def _version(info):
    # Windows path-stat ctime may still be creation while handle-stat ctime is
    # metadata change time. Birthtime is the comparable creation field; older
    # Python uses ctime for creation on both APIs. Keep handle change-time checks
    # separately so this compatibility difference cannot hide writes.
    return (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,
        getattr(info,'st_birthtime_ns',info.st_ctime_ns))


def _identity(value):
    if (type(value) is not str or not 0 < len(value) <= 512 or value.strip() != value
            or any(ord(character)<32 or ord(character)==127 for character in value)):
        raise ContextError('native-account-identity-missing')
    return value


def _directory(value):
    if type(value) is not str or not Path(value).is_absolute():
        raise ContextError('native-workspace-missing')
    path=Path(value).resolve(strict=True)
    info=_regular(path,directory=True)
    return str(path),(info.st_dev,info.st_ino)


def _root_write(path):
    if type(path) is not str or not ntpath.isabs(path):
        return True  # Unknown interpretation cannot be classified as restricted.
    normalized=ntpath.normpath(path)
    drive,tail=ntpath.splitdrive(normalized)
    return not tail.strip('\\/') or not drive


def _mode(context, metadata):
    for name in ('auto_review_enabled','node_repl_auto_review_required'):
        if type(metadata.get(name)) is not bool:
            raise ContextError('native-review-evidence-missing')
    policy=context.get('approval_policy')
    valid_policy=type(policy) is str and policy in PROMPTING|{'never'}
    if type(policy) is dict and set(policy)=={'granular'}:
        categories=policy['granular']
        valid_policy=(type(categories) is dict
            and {'sandbox_approval','rules','mcp_elicitations'} <= set(categories)
            and set(categories) <= {'sandbox_approval','rules','mcp_elicitations','request_permissions','skill_approval'}
            and all(type(value) is bool for value in categories.values()))
    if not valid_policy:
        raise ContextError('unsupported-native-approval-policy')
    profile,sandbox=context.get('permission_profile'),context.get('sandbox_policy')
    if type(profile) is not dict or type(sandbox) is not dict:
        raise ContextError('native-permission-profile-missing')
    if profile=={'type':'disabled'} and sandbox=={'type':'danger-full-access'}:
        if context.get('approvals_reviewer') != 'user':
            raise ContextError('unsupported-native-reviewer')
        if policy=='never': return 'bypass'
        if type(policy) is str and policy in PROMPTING: return 'prompting'
        raise ContextError('unsupported-full-access-policy')
    if (set(profile) != {'type','file_system','network'} or profile['type']!='managed'
            or profile['network'] not in ('restricted','enabled')
            or context.get('approvals_reviewer') not in ('user','auto_review')
            or sandbox.get('type') not in ('read-only','workspace-write')):
        raise ContextError('unsupported-managed-permissions')
    files=profile['file_system']
    if (type(files) is not dict or files.get('type')!='restricted'
            or set(files)-{'type','entries','glob_scan_max_depth'} or type(files.get('entries')) is not list
            or len(files['entries'])>512 or 'glob_scan_max_depth' in files
                and (type(files['glob_scan_max_depth']) is not int or files['glob_scan_max_depth']<=0)):
        raise ContextError('unsupported-managed-filesystem')
    for entry in files['entries']:
        if (type(entry) is not dict or set(entry)-{'path','access','missing_path_behavior'}
                or entry.get('access') not in ('read','write','deny','none')
                or 'missing_path_behavior' in entry and entry['missing_path_behavior']!='skip'):
            raise ContextError('unsupported-managed-entry')
        target=entry.get('path')
        if type(target) is not dict:
            raise ContextError('unsupported-managed-path')
        kind=target.get('type')
        if kind=='path' and set(target)=={'type','path'} and type(target['path']) is str:
            broad=_root_write(target['path'])
        elif (kind=='special' and set(target)=={'type','value'} and type(target['value']) is dict
                and set(target['value'])=={'kind'} and type(target['value']['kind']) is str):
            broad=target['value']['kind'] not in ('project_roots','current_working_directory','tmpdir','slash_tmp')
        elif kind=='glob_pattern' and set(target)=={'type','pattern'} and type(target['pattern']) is str:
            broad=True
        else:
            raise ContextError('unsupported-managed-path')
        if entry['access']=='write' and (broad or sandbox['type']=='read-only'):
            raise ContextError('unrestricted-managed-write')
    return 'prompting'  # Review automation and never do not remove a real sandbox.


@dataclass(frozen=True)
class CodexContext:
    thread_id: str
    turn_id: str
    kind: str
    mode: str
    workspace: str = field(repr=False)
    workspace_identity: tuple
    account_ref: str = field(repr=False)
    permission_sha256: str
    host_pid: int
    host_creation: int

    def binding(self, host_id):
        workspace=_sha(self.workspace_identity)
        incarnation=_sha([self.host_pid,self.host_creation,self.permission_sha256])
        return ChatBinding(SessionIdentity(self.thread_id,self.thread_id,'codex-'+self.kind,
            incarnation,WorkspaceIdentity(host_id,workspace,workspace)),self.account_ref,
            int(self.permission_sha256[:13],16))


class CodexObserver:
    """Controller-configured native profile and retained writer process identity.

    Creator account facts come from the exact native session header; this is not
    a provider signature or proof that the current app remains signed into it.
    A production frontend must also match its current native account observer.
    """
    def __init__(self, profile_root, host_pid, host_creation, host_executable):
        self._handle=None
        try:
            raw=Path(profile_root)
            if not raw.is_absolute(): raise ContextError('invalid-native-profile')
            _regular(raw,directory=True)
            self.root=raw.resolve(strict=True)
            info=_regular(self.root,directory=True)
            self._root_identity=(info.st_dev,info.st_ino)
            self.executable=Path(host_executable).resolve(strict=True)
            if (type(host_pid) is not int or not 0<host_pid<=0xffffffff
                    or type(host_creation) is not int or host_creation<=0 or not self.executable.is_file()):
                raise ContextError('invalid-native-host')
            self.pid,self.creation=host_pid,host_creation
            self.user=process_sids(os.getpid())[0]
            self._handle=open_process(0x00101000,False,self.pid)
            if not self._handle: raise ContextError('native-host-unavailable')
            self.require_host()
        except Exception:
            self.close()
            raise ContextError('native-host-unverified') from None

    def require_host(self):
        if (not self._handle or get_pid(self._handle)!=self.pid or handle_ctime(self._handle)!=self.creation
                or wait(self._handle,0)!=258 or process_sids(self.pid)[0]!=self.user
                or os.path.normcase(str(process_image(self.pid)))!=os.path.normcase(str(self.executable))):
            raise ContextError('native-host-changed')
        info=_regular(self.root,directory=True)
        if (info.st_dev,info.st_ino)!=self._root_identity:
            raise ContextError('native-profile-changed')

    def _find(self, thread):
        root=self.root/'sessions'
        _regular(root,directory=True)
        queue=[(root,0)]
        scanned=directories=0
        matches=[]
        while queue:
            directory,depth=queue.pop()
            directories+=1
            if directories>MAX_DIRECTORIES: raise ContextError('native-directory-limit')
            _regular(directory,directory=True)
            for path in directory.iterdir():
                scanned+=1
                if scanned>MAX_ENTRIES: raise ContextError('native-entry-limit')
                if depth<3 and re.fullmatch(r'\d{4}' if depth==0 else r'\d{2}',path.name):
                    _regular(path,directory=True)
                    queue.append((path,depth+1))
                elif depth==3 and path.name.startswith('rollout-') and path.name.endswith('-'+thread+'.jsonl'):
                    _regular(path)
                    matches.append(path)
        if len(matches)!=1: raise ContextError('native-rollout-missing-or-ambiguous')
        return matches[0]

    def _records(self, path, *, contexts=None):
        """The rollout's authority records, read whole while the file stands still.

        A running chat appends to its rollout, so a read that overlaps an append is
        retried, at most RECORD_TRIES times in all; a file that changes during every
        try is refused as before. Failing one read must not drop a live chat.
        contexts, when given, collects (turn_id, sha) of every turn_context in order.
        """
        for attempt in range(RECORD_TRIES):
            if contexts is not None:
                contexts.clear()
            try:
                return self._read_records(path,contexts)
            except ContextError as error:
                if error.code!='native-rollout-changed' or attempt==RECORD_TRIES-1:
                    raise

    def _read_records(self, path, contexts):
        before=_regular(path)
        if not 0<before.st_size<=MAX_ROLLOUT_BYTES: raise ContextError('native-rollout-size-limit')
        session=context=lifecycle=None
        with path.open('rb') as stream:
            opened=os.fstat(stream.fileno())
            if _version(opened)!=_version(before):
                raise ContextError('native-rollout-changed')
            consumed=0
            while True:
                line=stream.readline(MAX_AUTHORITY_BYTES+1)
                if not line: break
                consumed+=len(line)
                if consumed>MAX_ROLLOUT_BYTES: raise ContextError('native-rollout-size-limit')
                header=HEADER.match(line)
                kind=header[1].decode('ascii') if header else None
                interesting=kind in (None,'session_meta','turn_context','event_msg')
                if len(line)>MAX_AUTHORITY_BYTES:
                    event=EVENT.match(line[header.end():]) if header and kind=='event_msg' else None
                    if kind is None or kind in ('session_meta','turn_context') or kind=='event_msg' and (
                            event is None or event[1].decode('ascii') in LIFECYCLE):
                        raise ContextError('native-authority-record-limit')
                    interesting=False
                complete=line.endswith(b'\n')
                while not complete:
                    chunk=stream.readline(MAX_AUTHORITY_BYTES+1)
                    if not chunk: raise ContextError('native-rollout-incomplete')
                    consumed+=len(chunk)
                    if consumed>MAX_ROLLOUT_BYTES: raise ContextError('native-rollout-size-limit')
                    complete=chunk.endswith(b'\n')
                if not interesting: continue
                decoder=JsonLineDecoder()
                frames=decoder.feed(line)
                decoder.finish()
                if len(frames)!=1 or type(frames[0].get('payload')) is not dict:
                    raise ContextError('invalid-native-authority-record')
                record=frames[0]
                kind,payload=record.get('type'),record['payload']
                if kind=='session_meta':
                    if session is not None: raise ContextError('repeated-native-session')
                    session=payload
                elif kind=='turn_context':
                    context=payload
                    if contexts is not None: contexts.append((payload.get('turn_id'),_sha(payload)))
                elif kind=='event_msg' and payload.get('type') in LIFECYCLE: lifecycle=payload
            after=os.fstat(stream.fileno())
            if (_version(after)!=_version(before) or after.st_ctime_ns!=opened.st_ctime_ns
                    or _version(_regular(path))!=_version(before)):
                raise ContextError('native-rollout-changed')
        return session,context,lifecycle

    def _observe(self, host_metadata, *, active, record_hashes=None):
        try:
            self.require_host()
            metadata=host_metadata.get('x-codex-turn-metadata') if type(host_metadata) is dict else None
            if type(metadata) is not dict: raise ContextError('native-caller-metadata-missing')
            thread,turn=metadata.get('thread_id'),metadata.get('turn_id')
            if any(type(value) is not str or UUID.fullmatch(value) is None for value in (thread,turn)):
                raise ContextError('invalid-native-caller-identity')
            session,context,lifecycle=self._records(self._find(thread))
            if (type(session) is not dict or session.get('id')!=thread or session.get('thread_source')!='user'
                    or metadata.get('thread_source')!='user'):
                raise ContextError('native-session-identity-mismatch')
            origin,source=session.get('originator'),session.get('source')
            if origin in ('Codex Desktop','codex_work_desktop') and source=='vscode': kind='desktop'
            elif origin=='codex_cli_rs' and source=='cli': kind='terminal'
            else: raise ContextError('unsupported-native-caller-source')
            if (type(context) is not dict or type(lifecycle) is not dict or context.get('turn_id')!=turn
                    or lifecycle.get('turn_id')!=turn or lifecycle.get('type') not in (STARTED if active else LIFECYCLE)):
                raise ContextError('native-caller-turn-inactive')
            if record_hashes is not None and (_sha(session),_sha(context))!=record_hashes:
                raise ContextError('captured-native-context-changed')
            workspace,identity=_directory(context.get('cwd'))
            if _directory(session.get('cwd'))[1]!=identity:
                raise ContextError('native-workspace-changed')
            mode=_mode(context,metadata)
            policy=_sha([context['permission_profile'],context['sandbox_policy'],context['approval_policy'],
                context['approvals_reviewer'],metadata['auto_review_enabled'],metadata['node_repl_auto_review_required']])
            account=_sha(['codex',_identity(session.get('creator_account_id')),_identity(session.get('creator_user_id'))])
            self.require_host()
            return CodexContext(thread,turn,kind,mode,workspace,identity,account,policy,self.pid,self.creation)
        except ContextError:
            raise
        except Exception:
            raise ContextError('native-evidence-unavailable') from None

    def observe(self, host_metadata):
        """Active caller only; metadata provenance belongs to the native frontend."""
        return self._observe(host_metadata,active=True)

    def observe_existing(self,captured,host_metadata, *, session_sha256,context_sha256):
        """Idle-safe check of a context previously captured from active native metadata.

        Captured flags and record hashes come only from the trusted runtime,
        never tool arguments. A new turn or changed context needs fresh native
        metadata; completed/aborted lifecycle alone does not end a live chat.
        This method does not authorize a caller or acquire a native writer.
        """
        if (type(captured) is not CodexContext or any(type(value) is not str
                or not re.fullmatch('[0-9a-f]{64}',value) for value in (session_sha256,context_sha256))):
            raise ContextError('captured-native-context-unverified')
        facts=self._observe(host_metadata,active=False,record_hashes=(session_sha256,context_sha256))
        if facts!=captured:
            raise ContextError('captured-native-context-changed')
        return facts

    def rollout_identity(self, thread):
        """The (device, file id) of a chat's one rollout file."""
        info=_regular(self._find(thread))
        return info.st_dev,info.st_ino

    def observe_successor(self, captured, *, session_sha256, context_sha256, rollout):
        """After a delivery wrote to this chat: the same chat, its turn possibly moved on.

        A delivered message starts a new turn in the recipient, so the captured turn
        need not be the last one. The rollout must be the same file (rollout, from
        rollout_identity), its session record unchanged, and the captured turn's
        exact turn_context still present; newer turns after it are accepted. This
        is identity evidence after a write only: it authorizes no write.
        """
        try:
            self.require_host()
            if (type(captured) is not CodexContext or type(rollout) is not tuple
                    or any(type(value) is not str or not re.fullmatch('[0-9a-f]{64}',value)
                        for value in (session_sha256,context_sha256))):
                raise ContextError('captured-native-context-unverified')
            path=self._find(captured.thread_id)
            info=_regular(path)
            if (info.st_dev,info.st_ino)!=rollout:
                raise ContextError('native-rollout-replaced')
            contexts=[]
            session,_context,_lifecycle=self._records(path,contexts=contexts)
            if type(session) is not dict or session.get('id')!=captured.thread_id or _sha(session)!=session_sha256:
                raise ContextError('native-session-identity-mismatch')
            if (captured.turn_id,context_sha256) not in contexts:
                raise ContextError('captured-native-context-changed')
            info=_regular(path)
            if (info.st_dev,info.st_ino)!=rollout:
                raise ContextError('native-rollout-replaced')
            self.require_host()
            return True
        except ContextError:
            raise
        except Exception:
            raise ContextError('native-evidence-unavailable') from None

    def close(self):
        if self._handle:
            close(self._handle)
            self._handle=None

    def __enter__(self): return self

    def __exit__(self,*error): self.close()
