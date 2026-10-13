"""Private owned request/reply helper; parent bounds synchronous Windows I/O."""
import ctypes
from ctypes import wintypes as w
import time

from optional.bridge.core.jsonwire import JsonLineDecoder,encode_frame
from kitlib.proc.pipe import connect,write
from .rpc import MAX_BYTES,encode,read_frame
from .transport import PipeTarget


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
        pipe=connect(target.name,target.pid,target.creation)
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
