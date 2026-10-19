"""Private owned request/reply helper; parent bounds synchronous Windows I/O."""
import ctypes
from ctypes import wintypes as w
import time

from optional.bridge.core.jsonwire import JsonLineDecoder,encode_frame
from kitlib.proc.errors import ProcError
from kitlib.proc.pipe import K,bind,connect,write
from .rpc import MAX_BYTES,encode,read_frame
from .transport import PipeTarget

wait_pipe=bind(K,'WaitNamedPipeW',[w.LPCWSTR,w.DWORD],w.BOOL)
PIPE_BUSY=231


def _connect(target,deadline):
    """Open the service's one pipe instance, waiting while another client holds it.

    The service serves one client at a time on a one-instance pipe, so a second
    client (a hook beside a frontend call, or two sessions) meets ERROR_PIPE_BUSY
    until the first is served. Nothing has been written then, so waiting is safe;
    every other open or identity failure is still refused at once.
    """
    while True:
        try: return connect(target.name,target.pid,target.creation)
        except ProcError as error:
            # connect() raises straight after CreateFileW, so this is its error.
            if error.code!='pipe-connect-failed' or ctypes.get_last_error()!=PIPE_BUSY: raise
        remaining=deadline-time.monotonic()
        if remaining<=0: raise ProcError('pipe-busy-timeout')
        # Returns once an instance listens again (or on timeout); either way retry the open.
        wait_pipe(target.name,max(1,min(1000,int(remaining*1000))))


def exchange(input_stream,output_stream):
    pipe=None
    started=False
    try:
        decoder=JsonLineDecoder(max_frame_bytes=2048)
        frames=decoder.feed(input_stream.readline(2049))
        decoder.finish()
        if len(frames)!=1 or set(frames[0])!={'pipe','pid','creation','length'}:
            raise ValueError('bad private request')
        meta=frames[0]
        target=PipeTarget(meta['pipe'],meta['pid'],meta['creation'])
        size=meta['length']
        if type(size) is not int or not 0<size<=MAX_BYTES: raise ValueError('bad packet size')
        data=bytearray()
        while len(data)<size:
            part=input_stream.read(size-len(data))
            if not part: raise ValueError('truncated input')
            data.extend(part)
        # The owning request() bounds the whole exchange; this bound only backs it up.
        pipe=_connect(target,time.monotonic()+10)
        started=True
        count=w.DWORD()
        payload=bytes(data)
        if not write(pipe.handle,payload,len(payload),ctypes.byref(count),None) or count.value!=len(payload):
            raise ValueError('partial write')
        reply=encode(read_frame(pipe.handle,time.monotonic()+10,blocking=True))
        output_stream.write(encode_frame({'state':'reply','length':len(reply)},max_frame_bytes=2048))
        output_stream.write(reply)
    except Exception:
        output_stream.write(encode_frame({'state':'uncertain' if started else 'not-written'},max_frame_bytes=2048))
    finally:
        if pipe is not None: pipe.close()
        output_stream.flush()
