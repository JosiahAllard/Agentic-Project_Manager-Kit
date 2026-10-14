"""JSON-line test helpers over kitlib.proc.pipe handles: a client's send and a listener's drain.

They were methods of the probe's Pipe class (kitlib/bridgeprobe/winpipe.py, deleted); the promoted
kitlib.proc.pipe keeps only the handle work, so the tests that fake a peer carry them here. Not a
test module. Windows only."""
import ctypes
import time

from kitlib.proc.errors import ProcError
from optional.bridge.core.jsonwire import JsonLineDecoder, encode_frame

MAX_SEND_BYTES = 4096
MAX_FRAMES = 4


def send(pipe, frames):
    """Write `frames` as JSON lines through a connected pipe (kitlib.proc.pipe.connect)."""
    from ctypes import wintypes as w
    from kitlib.proc.pipe import write
    data = b''.join(encode_frame(frame, max_frame_bytes=2048) for frame in frames)
    if len(data) > MAX_SEND_BYTES:
        raise ProcError('pipe-send-limit')
    count = w.DWORD()
    if not write(pipe.handle, data, len(data), ctypes.byref(count), None) or count.value != len(data):
        raise ProcError('pipe-write-failed')


def frames_until_close(listener, timeout):
    """(frames, True) once the client closes; ProcError on a timeout or more than MAX_FRAMES."""
    from ctypes import wintypes as w
    from kitlib.proc.pipe import peek, read
    end = time.monotonic() + timeout
    decoder, frames = JsonLineDecoder(), []
    while time.monotonic() < end:
        available = w.DWORD()
        if not peek(listener.handle, None, 0, None, ctypes.byref(available), None):
            if ctypes.get_last_error() in (109, 232):
                decoder.finish()
                return frames, True
            raise ProcError('pipe-peek-failed')
        if available.value:
            buffer = ctypes.create_string_buffer(min(4096, available.value))
            count = w.DWORD()
            if not read(listener.handle, buffer, len(buffer), ctypes.byref(count), None):
                raise ProcError('pipe-read-failed')
            frames.extend(decoder.feed(buffer.raw[:count.value]))
            if len(frames) > MAX_FRAMES:
                raise ProcError('pipe-frame-limit')
        else:
            time.sleep(.02)
    raise ProcError('pipe-receipt-timeout')
