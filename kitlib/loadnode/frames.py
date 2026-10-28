"""The frames a load node streams to the main machine (`follow`, `fetch`): binary-safe, one header
line and then exactly that many bytes.

    L <n>\\n<n bytes>     job log bytes, in order from the offset asked for
    R <n>\\n<json>        the job's result: it has ended (follow's last frame before E)
    M <n>\\n<json>        the artifact manifest (fetch's first frame)
    F <n>\\n<n bytes>     artifact bytes, the manifest's files one after another
    K 0\\n               keep-alive: nothing new yet
    X <n>\\n<json>        the node refused: {"error": why}
    E 0\\n               the end: everything was sent

A frame cut short (the connection dropped) is never handed on: the Decoder keeps the bytes of a
frame until all of it is there, so a log offset counts only bytes a whole L frame carried. Nothing
here prints or touches the network.
"""
LOG, RESULT, MANIFEST, FILE, KEEP, ERROR, END = 'L', 'R', 'M', 'F', 'K', 'X', 'E'
KINDS = (LOG, RESULT, MANIFEST, FILE, KEEP, ERROR, END)
MAX_FRAME = 1024 * 1024                 # the most bytes one frame may carry
_MAX_HEADER = 16                        # 'L 1048576\n' and room to spare


class FrameError(Exception):
    """The stream is not frames: a bad header or a frame larger than MAX_FRAME."""


def encode(kind, payload=b''):
    """One frame: header and payload as bytes."""
    if kind not in KINDS:
        raise ValueError('unknown frame kind %r' % (kind,))
    if len(payload) > MAX_FRAME:
        raise ValueError('a frame carries at most %d bytes' % MAX_FRAME)
    return ('%s %d\n' % (kind, len(payload))).encode('ascii') + bytes(payload)


class Decoder:
    """Feed it the stream's bytes as they come; it gives back each whole frame once."""

    def __init__(self, max_frame=MAX_FRAME):
        self.max_frame = max_frame
        self._buf = bytearray()

    @property
    def pending(self):
        """Bytes held for a frame not yet whole."""
        return len(self._buf)

    def feed(self, data):
        """[(kind, payload bytes)] of the frames `data` completes. FrameError on a bad header."""
        self._buf += data
        out = []
        while True:
            nl = self._buf.find(b'\n', 0, _MAX_HEADER + 1)
            if nl < 0:
                if len(self._buf) > _MAX_HEADER:
                    raise FrameError('no frame header in the first %d bytes' % _MAX_HEADER)
                return out
            head = bytes(self._buf[:nl])
            parts = head.split(b' ')
            if (len(parts) != 2 or parts[0].decode('ascii', 'replace') not in KINDS
                    or not parts[1].isdigit() or len(parts[1]) > 8):
                raise FrameError('a bad frame header')
            n = int(parts[1])
            if n > self.max_frame:
                raise FrameError('a frame of %d bytes is over the %d limit' % (n, self.max_frame))
            if len(self._buf) < nl + 1 + n:
                return out
            out.append((parts[0].decode('ascii'), bytes(self._buf[nl + 1:nl + 1 + n])))
            del self._buf[:nl + 1 + n]
