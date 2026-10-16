"""Trusted Claude provider assembly for the existing shared Windows controller.

Desktop candidates use real original-task/account/permission records. CLI account
metadata is not permission evidence: a terminal binding becomes available only
through an authenticated PreToolUse hook for its verified, already-open frontend.
No owner connection writes or peer creation occur in provider construction. A
configured delivery adapter uses a separately bootstrap-owned return endpoint;
native bytes alone supply no acceptance or wake evidence. Native permissions
remain in the native app.

Map locks protect short snapshots only. Native observers and all controller or
pairing callbacks run after those locks are released, avoiding lock inversion.
"""
from dataclasses import dataclass
from pathlib import Path
import sys
import threading

from optional.bridge.hosts.claude.context import ClaudeCalls,ClaudeDesktop,MODES
from optional.bridge.hosts.claude.records import read_record
from optional.bridge.hosts.claude.peer import ClaudePeerAdapter,ClaudePeerEndpoint
from optional.bridge.hosts.claude.provenance import PluginMcp
from optional.bridge.hosts.claude.sessions import ClaudeDirectory,ClaudeSession
from optional.bridge.hosts.claude.terminal import ClaudeTerminal,TerminalCalls
from optional.bridge.hosts.claude.hook import entry_command
from kitlib.ident import identifier
from optional.bridge.core.messages import DeliveryError
from optional.bridge.core.pairing import ChatBinding
from optional.bridge.core.mcp import TOOLS
from optional.bridge.win.controller import Controller
from optional.bridge.win.launch import PYTHON_FLAGS
from optional.bridge.win.evidence import document
from optional.bridge.win.frontend import NativeFrontend,arguments,running
from optional.bridge.win.rpc import PipeClient
from optional.bridge.port import NativeSource
from optional.bridge.paths import RPC_WORKER_REL,kit_file
from kitlib.proc.pipe import process_image,process_sids
from kitlib.proc.process import ancestors_of,proc_ctime


def hook_descriptor(marker):
    """Actual native exec form; argv/parent relationship verified on Claude 2.1.296."""
    argv=entry_command(marker)
    return {'type':'command','command':argv[0],'args':argv[1:]}


@dataclass(frozen=True)
class _Frontend:
    native:NativeFrontend
    calls:object
    source:NativeSource


@dataclass(frozen=True)
class _TerminalEvidence:
    binding:ChatBinding
    snapshot:ClaudeSession
    calls:TerminalCalls
    native:NativeFrontend
    mode:str


class ClaudeSources:
    def __init__(self,controller,registry,native_executable,host_id,desktop_profile,
            terminal_account,terminal_projects,marker,mcp_server_name,*,plugin_name=None):
        if type(controller) is not Controller:raise DeliveryError('invalid-claude-controller')
        self.controller,self.host_id=controller,identifier(host_id)
        self.desktop=ClaudeDesktop(desktop_profile)
        self.terminal=ClaudeTerminal(terminal_account,transcript_root=terminal_projects)
        self.directory=ClaudeDirectory(registry,native_executable,account_probe=self._account)
        self.marker=Path(marker)
        if not self.marker.is_absolute():
            self.directory.close()
            raise DeliveryError('absolute-claude-service-marker-required')
        if (type(mcp_server_name) is not str or not mcp_server_name
                or any(char not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for char in mcp_server_name)
                or '__' in mcp_server_name or len(mcp_server_name)>128):
            self.directory.close()
            raise DeliveryError('invalid-claude-mcp-server')
        self.hook_argv=entry_command(self.marker)
        # Installed service profiles require explicit plugin_name. None retains
        # only the pre-install prototype fixture port; production never falls
        # back from scoped to bare names or accepts another provenance source.
        self.namespace=None
        if plugin_name is not None:
            try:self.namespace=PluginMcp(plugin_name,mcp_server_name)
            except Exception:
                self.directory.close()
                raise
        self.tools=(self.namespace.tools if self.namespace is not None else
            {'mcp__'+mcp_server_name+'__'+tool:{'name':mcp_server_name,'source':'plugin'} for tool in TOOLS})
        self._lock=threading.RLock()
        self._closed=False
        self._frontends,self._desktop,self._terminal={}, {}, {}

    def _open(self):
        with self._lock:closed=self._closed
        if closed:raise DeliveryError('claude-provider-closed')
        # Shared ownership is checked after releasing the runtime map lock.
        self.controller._require()

    def _account(self,record,registry):
        entry=record.get('entrypoint') if type(record) is dict else None
        if entry=='claude-desktop':return self.desktop.account_probe(record,registry)
        if entry=='cli':return self.terminal.account_probe(record,registry)
        raise DeliveryError('claude-native-entrypoint-unverified')

    def _native(self,native):
        self._open()
        if (type(native) is not NativeFrontend or native.kind!='claude'
                or native.host_executable not in self.directory.executables
                or proc_ctime(native.pid)!=native.creation
                or process_sids(native.pid)[0]!=self.directory.user
                or proc_ctime(native.host_pid)!=native.host_creation
                or process_image(native.host_pid)!=native.host_executable
                or process_sids(native.host_pid)[0]!=self.directory.user
                or (native.host_pid,native.host_creation) not in ancestors_of(native.pid,native.creation,limit=8)):
            raise DeliveryError('claude-native-frontend-unverified')
        sessions={(item.pid,item.creation):item for item in self.directory.discover()}
        snapshot=sessions.get((native.host_pid,native.host_creation))
        if snapshot is None or snapshot.executable!=native.host_executable:
            raise DeliveryError('claude-native-session-unverified')
        self.directory.require_live(snapshot)
        return snapshot

    def _desktop_live(self,binding):
        try:
            self._open()
            with self._lock:current=self._desktop.get(binding.identity.session_id)
            if current is None or current[0]!=binding:return False
            snapshot=current[1]
            self.directory.require_live(snapshot)
            record=read_record(self.directory.registry/('%d.json'%snapshot.pid))
            facts=self.desktop.observe(record)
            return facts.binding(snapshot,self.host_id)==binding
        except Exception:return False

    def _desktop_facts(self,snapshot):
        self._open()
        self.directory.require_live(snapshot)
        record=read_record(self.directory.registry/('%d.json'%snapshot.pid))
        facts=self.desktop.observe(record)
        return facts,facts.binding(snapshot,self.host_id)

    def _enroll_desktop(self,snapshot,facts,binding):
        with self._lock:
            if self._closed:raise DeliveryError('claude-provider-closed')
            self._desktop[binding.identity.session_id]=(binding,snapshot)
        # Never hold the runtime lock across controller -> pairing -> verifier.
        description={'title':facts.title} if facts.title is not None else {}
        self.controller.enroll(binding,self._desktop_live,description)
        return binding

    def refresh(self):
        """Discover original desktop candidates; CLI mode is never guessed at startup."""
        self._open()
        found=[]
        for snapshot in self.directory.discover():
            if snapshot.kind!='desktop':continue
            try:
                facts,binding=self._desktop_facts(snapshot)
                found.append(self._enroll_desktop(snapshot,facts,binding))
            except Exception:
                # Unavailable native evidence cannot become a candidate.
                continue
        live={binding.identity.session_id for binding in found}
        with self._lock:
            stale=tuple(session for session in self._desktop if session not in live)
            for session in stale:del self._desktop[session]
        # Withdraw only ephemeral observations, never stored owner connections.
        for session in stale:self.controller.pairs.withdraw(session)
        return tuple(found)

    def _desktop_observe(self,native,calls,metadata):
        snapshot=self._native(native)
        authority=calls.observe(metadata)  # Native observer ignores model _meta.
        facts,binding=self._desktop_facts(snapshot)
        if authority.sender!=binding:raise DeliveryError('claude-native-source-changed')
        self._enroll_desktop(snapshot,facts,binding)
        return authority

    def _cli_live(self,calls,binding):
        try:
            self._open()
            return calls.require_live(binding)
        except Exception:return False

    def _cli_observe(self,native,calls,metadata):
        self._native(native)
        authority=calls.observe(metadata)  # Only private TerminalCall is accepted.
        self.controller.enroll(authority.sender,lambda binding:self._cli_live(calls,binding))
        return authority

    def _cli_prepare(self,native,calls,request):
        self._native(native)
        metadata,consume=calls.prepare_request(request)
        try:self._cli_observe(native,calls,metadata)
        except Exception:
            consume()
            raise
        return metadata,consume

    def __call__(self,native):
        snapshot=self._native(native)
        key=(native.pid,native.creation)
        with self._lock:current=self._frontends.get(key)
        if current is not None:
            if (current.native!=native or (type(current.calls) is TerminalCalls)!=(snapshot.kind=='terminal')):
                raise DeliveryError('claude-native-source-changed')
            return current.source
        if snapshot.kind=='desktop':
            calls=ClaudeCalls(self.directory,self.desktop,self.host_id,native.pid,native.creation)
            source=NativeSource(lambda metadata:self._desktop_observe(native,calls,metadata))
            source.observe(None)  # Already-open desktop candidates need no first tool.
        elif snapshot.kind=='terminal':
            calls=TerminalCalls(self.directory,self.terminal,self.host_id,native.pid,native.creation,
                hook_argv=self.hook_argv,bridge_tools=self.tools)
            source=NativeSource(lambda metadata:self._cli_observe(native,calls,metadata),
                lambda request:self._cli_prepare(native,calls,request))
        else:raise DeliveryError('claude-native-entrypoint-unverified')
        entry=_Frontend(native,calls,source)
        with self._lock:
            if self._closed:raise DeliveryError('claude-provider-closed')
            # Drop exited frontend caches without touching controller state here.
            for old,previous in tuple(self._frontends.items()):
                if not running(previous.native.pid,previous.native.creation):del self._frontends[old]
            previous=self._frontends.get(key)
            if previous is not None:return previous.source
            if len(self._frontends)>=128:raise DeliveryError('claude-native-frontend-capacity')
            self._frontends[key]=entry
        return source

    def hook_sink(self,peer,payload):
        """Authenticated producer port; route by kernel ancestry, never hook JSON IDs."""
        self._open()
        if type(peer) is not PipeClient or proc_ctime(peer.pid)!=peer.creation:
            raise DeliveryError('claude-hook-producer-unverified')
        python=Path(self.hook_argv[0])
        argv=arguments(peer.pid,peer.creation)
        helper=kit_file(RPC_WORKER_REL)
        if (process_image(peer.pid)!=python or process_sids(peer.pid)[0]!=self.directory.user
                or len(argv)!=4 or argv[1:3]!=PYTHON_FLAGS or Path(argv[0]).resolve(strict=True)!=python
                or Path(argv[3]).resolve(strict=True)!=helper):
            raise DeliveryError('claude-hook-producer-unverified')
        lineage=ancestors_of(peer.pid,peer.creation,limit=3)
        with self._lock:entries=tuple(self._frontends.values())
        candidates=[entry for entry in entries if type(entry.calls) is TerminalCalls
            and (entry.native.host_pid,entry.native.host_creation) in lineage
            and running(entry.native.pid,entry.native.creation)]
        if len(candidates)!=1:raise DeliveryError('claude-hook-frontend-missing-or-ambiguous')
        entry=candidates[0]
        self._native(entry.native)
        ticket=entry.calls.observe_hook(peer,payload)
        event=document(payload)
        metadata=entry.calls.metadata(event.get('tool_name'),event.get('tool_input'),ticket=ticket)
        # Mode is retained only after the actual hook/session/intent proof passed.
        # It is paired with the complete binding, not an ID/title or default.
        authority=entry.calls.observe(metadata)
        mode=MODES.get(event.get('permission_mode'))
        if mode not in ('prompting','bypass'):raise DeliveryError('claude-native-mode-unverified')
        snapshot=self._native(entry.native)
        evidence=_TerminalEvidence(authority.sender,snapshot,entry.calls,entry.native,mode)
        with self._lock:
            if self._closed:raise DeliveryError('claude-provider-closed')
            self._terminal[authority.sender.identity.session_id]=evidence
        # TerminalCalls releases its own lock before controller enrollment.
        return self._cli_observe(entry.native,entry.calls,metadata).sender

    def resolve(self,binding):
        """Exact current native session; no successor by logical ID/name is substituted."""
        self._open()
        if type(binding) is not ChatBinding:raise DeliveryError('claude-native-binding-unverified')
        with self._lock:
            desktop=self._desktop.get(binding.identity.session_id)
            terminal=self._terminal.get(binding.identity.session_id)
        if desktop is not None and desktop[0]==binding:
            if not self._desktop_live(binding):raise DeliveryError('claude-native-binding-changed')
            snapshot=desktop[1]
            _,current=self._desktop_facts(snapshot)
            if current!=binding:raise DeliveryError('claude-native-binding-changed')
            self.directory.require_live(snapshot)
            return snapshot
        if terminal is not None and terminal.binding==binding:
            snapshot=self._native(terminal.native)
            if (snapshot!=terminal.snapshot or not self._cli_live(terminal.calls,binding)):
                raise DeliveryError('claude-native-binding-changed')
            self.directory.require_live(snapshot)
            return snapshot
        raise DeliveryError('claude-native-binding-unverified')

    def sender_mode(self,binding):
        """Verified native class: fresh desktop facts or exact retained hook evidence.

        CLI evidence covers its latest authenticated PreToolUse mode; native tool
        rules remain native. Missing/stale evidence has no prompting default.
        """
        snapshot=self.resolve(binding)
        if snapshot.kind=='desktop':
            facts,current=self._desktop_facts(snapshot)
            if current!=binding or facts.mode not in ('prompting','bypass'):
                raise DeliveryError('claude-native-mode-unverified')
            return facts.mode
        with self._lock:evidence=self._terminal.get(binding.identity.session_id)
        if (evidence is None or evidence.binding!=binding or evidence.snapshot!=snapshot
                or evidence.mode not in ('prompting','bypass') or not self._cli_live(evidence.calls,binding)):
            raise DeliveryError('claude-native-mode-unverified')
        return evidence.mode

    def delivery_adapter(self,endpoint,sender_mode_resolver):
        """Wire an already-owned return endpoint; never create/publish one here.

        The composite bootstrap resolver receives the complete sender binding.
        It must prove that runtime's current native mode. The existing adapter
        retains native framing/correlation: bytes alone are an unknown outcome.
        """
        self._open()
        if (type(endpoint) is not ClaudePeerEndpoint or endpoint.directory is not self.directory
                or not callable(sender_mode_resolver)):
            raise DeliveryError('invalid-claude-delivery-components')
        def mode(message):
            self.resolve(message.recipient)
            # All native observer/map locks are released before the other runtime.
            value=sender_mode_resolver(message.sender)
            if type(value) is not str or value not in ('prompting','bypass'):
                raise DeliveryError('claude-native-sender-mode-unverified')
            self.resolve(message.recipient)  # Callback/profile drift is not a lease.
            # Independently reattest both complete native bindings through the
            # shared live ports after the composite mode callback. This read is
            # reentrant under the core selected guard; it opens no transaction.
            self.controller.pairs.require_selected(message.sender,message.recipient)
            return value
        return ClaudePeerAdapter(self.directory,endpoint,resolve=self.resolve,sender_mode=mode)

    def close(self):
        with self._lock:
            if self._closed:return
            self._closed=True
            self._frontends.clear()
            self._desktop.clear()
            self._terminal.clear()
        self.directory.close()
