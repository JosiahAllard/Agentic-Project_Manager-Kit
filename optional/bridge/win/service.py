"""One controller serves verified MCP frontends through its private Windows pipe."""
from optional.bridge.core.messages import DeliveryError
from kitlib.proc.errors import ProcError
from kitlib.proc.process import (PROCESS_QUERY_LIMITED_INFORMATION,SYNCHRONIZE,WAIT_TIMEOUT,
    close,handle_ctime,open_process)
from kitlib.proc.win32 import K32
from .rpc import PipeServer,encode


def running(pid,creation):
    """True only while process (pid, creation) has not exited. An exited process stays openable,
    with its creation time, while any handle to it is open anywhere on the machine, so a matching
    creation time alone does not mean it still runs: its exit is read from the wait state."""
    handle=open_process(pid,PROCESS_QUERY_LIMITED_INFORMATION|SYNCHRONIZE)
    if not handle: return False
    try: return handle_ctime(handle)==creation and K32.WaitForSingleObject(handle,0)==WAIT_TIMEOUT
    finally: close(handle)


class BrokerService:
    def __init__(self,controller,inspector, *, source_factory=None,hook_sink=None,view_handler=None):
        self.controller,self.inspector=controller,inspector
        self.source_factory=source_factory
        if hook_sink is not None and not callable(hook_sink): raise DeliveryError('invalid-hook-sink')
        self.hook_sink=hook_sink
        if view_handler is not None and not callable(view_handler): raise DeliveryError('invalid-view-handler')
        self.view_handler=view_handler
        self.frontends,self.sources={},{}
        self.server=PipeServer(controller.leader,handler=self.handle)
        self.closed=False

    def handle(self,peer,frame):
        if (self.closed or type(frame) is not dict
                or type(frame['protocol']) is not int or frame['protocol']!=1):
            raise DeliveryError('invalid-private-request')
        if set(frame)=={'protocol','view'}:
            if frame['view']!='connections' or self.view_handler is None:
                raise DeliveryError('owner-view-unavailable')
            self.inspector.verify_helper(peer)
            # This only opens a window. It cannot approve or select any route.
            self.view_handler()
            return {'protocol':1,'response':{'requested':True}}
        if set(frame)=={'protocol','hook'}:
            if self.hook_sink is None: raise DeliveryError('hook-producer-unavailable')
            self.inspector.verify_helper(peer)
            # The sink independently verifies exact producer/native parent and
            # provenance. No ticket or source identity is returned to the caller.
            self.hook_sink(peer,encode(frame['hook'])[:-1])
            return {'protocol':1,'response':None}
        if set(frame) not in ({'protocol','request'},{'protocol','request','channel'}):
            raise DeliveryError('invalid-private-request')
        # This field is the frontend's private publication, not model arguments.
        native=self.inspector.inspect(peer,channel=frame.get('channel'))
        for stale,(previous,server) in tuple(self.frontends.items()):
            if not running(previous.pid,previous.creation):
                self.controller.detach_frontend(server)
                del self.frontends[stale]
                self.sources.pop(stale,None)
        # A helper's request can be read after its frontend exited; it must not register it again.
        if not running(native.pid,native.creation): raise DeliveryError('frontend-exited')
        key=(native.pid,native.creation)
        current=self.frontends.get(key)
        if current is None:
            if len(self.frontends)>=128: raise DeliveryError('frontend-capacity')
            source=self.source_factory(native) if self.source_factory is not None else None
            attest=getattr(source,'attest',None)
            if callable(attest):
                # Any SenderAttestor (port.py); one with prepare() also leases hook metadata.
                prepare=getattr(source,'prepare',None)
                if prepare is not None and not callable(prepare):
                    # A lease that cannot run must not let wire _meta reach attest() unreplaced.
                    raise DeliveryError('invalid-native-source')
                if prepare is not None: self.sources[key]=source
                observe=attest
            else: observe=source
            current=(native,self.controller.frontend(observe))
            self.frontends[key]=current
        elif current[0]!=native: raise DeliveryError('frontend-host-changed')
        message=frame['request']
        source=self.sources.get(key)
        if (source is not None and source.prepare is not None and type(message) is dict
                and message.get('method')=='tools/call' and 'id' in message):
            metadata,consume=source.prepare(message)
            if not callable(consume): raise DeliveryError('invalid-native-request-lease')
            try:
                message={**message,'params':{**message.get('params',{}),'_meta':metadata}}
                return {'protocol':1,'response':current[1].handle(message)}
            finally: consume()
        return {'protocol':1,'response':current[1].handle(message)}

    def run(self,stop):
        while not self.closed and not stop.is_set():
            try: self.server.once(timeout=.1)
            except ProcError as error:
                if error.code!='private-pipe-accept-timeout': raise
            except DeliveryError:
                if not self.controller.leader.owns(): raise

    def close(self):
        self.closed=True
        for native,server in self.frontends.values():
            self.controller.detach_frontend(server)
        self.frontends.clear()
        self.sources.clear()
