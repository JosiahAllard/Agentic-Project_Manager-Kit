"""Private native I/O helper logic; the supervising process bounds its lifetime."""
import ctypes
from ctypes import wintypes as w

from optional.bridge.core.jsonwire import JsonLineDecoder
from kitlib.proc.pipe import connect, write
from .transport import MAX_PACKET_BYTES, PipeTarget


def pipe_write(input_stream):
    """Read one private request and return a content-free transfer result.

    This function may block in WriteFile. It only runs inside the controller's
    kill-on-close job; it must never run on the broker thread itself.
    """
    pipe = None
    try:
        try:
            header = input_stream.readline(2049)
            decoder = JsonLineDecoder(max_frame_bytes=2048)
            frames = decoder.feed(header)
            decoder.finish()
            if len(frames) != 1 or set(frames[0]) != {'pipe', 'pid', 'creation', 'length'}:
                raise ValueError('invalid private request')
            metadata = frames[0]
            target = PipeTarget(metadata['pipe'], metadata['pid'], metadata['creation'])
            size = metadata['length']
            if type(size) is not int or not 0 < size <= MAX_PACKET_BYTES:
                raise ValueError('invalid packet size')
            data = bytearray()
            while len(data) < size:
                chunk = input_stream.read(size-len(data))
                if not chunk:
                    raise ValueError('truncated input')
                data.extend(chunk)
            # connect checks the kernel pipe-server PID, creation time and SID,
            # not the supplied address or an advertised owner label alone.
            pipe = connect(target.name, target.pid, target.creation)
        except Exception:
            return {'state': 'not-written'}
        count = w.DWORD()
        payload = bytes(data)
        if (not write(pipe.handle, payload, len(payload), ctypes.byref(count), None)
                or count.value != len(payload)):
            return {'state': 'uncertain'}
        return {'state': 'written', 'bytes_written': count.value}
    except Exception:
        return {'state': 'uncertain'}
    finally:
        if pipe is not None:
            pipe.close()
