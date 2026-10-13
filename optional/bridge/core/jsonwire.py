"""Bounded JSON-line framing for the bridge's private pipes, workers and registry records.

Objects are untrusted data, not authenticated peers or permitted RPC calls.
There is no transport, routing, token lookup or runtime-specific interpretation.
"""
import json
import math

MAX_FRAME_BYTES = 128 * 1024
MAX_CHUNK_BYTES = 8 * MAX_FRAME_BYTES
MAX_FRAMES_PER_FEED = 128
MAX_DEPTH = 64
MAX_NODES = 20000


class WireError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__('JSON-line frame refused: %s' % code)


def _limit(value):
    if type(value) is not int or not 1 <= value <= MAX_FRAME_BYTES:
        raise WireError('invalid-frame-limit')
    return value


def _check_structure(root, limit):
    if type(root) is not dict:
        raise WireError('object-required')
    stack = [(root, 0)]
    nodes, budget = 0, 0
    while stack:
        value, depth = stack.pop()
        nodes += 1
        budget += 1
        if depth > MAX_DEPTH:
            raise WireError('object-depth')
        if nodes > MAX_NODES:
            raise WireError('node-limit')
        kind = type(value)
        if kind is dict:
            if nodes + len(stack) + len(value) > MAX_NODES:
                raise WireError('node-limit')
            for key, item in value.items():
                if type(key) is not str:
                    raise WireError('non-string-key')
                if len(key) > limit:
                    raise WireError('frame-size')
                try:
                    budget += len(key.encode('utf-8'))
                except UnicodeError:
                    raise WireError('invalid-unicode') from None
                if budget > limit:
                    raise WireError('frame-size')
                stack.append((item, depth + 1))
        elif kind in (list, tuple):
            if nodes + len(stack) + len(value) > MAX_NODES:
                raise WireError('node-limit')
            stack.extend((item, depth + 1) for item in value)
        elif kind is str:
            if len(value) > limit:
                raise WireError('frame-size')
            try:
                budget += len(value.encode('utf-8'))
            except UnicodeError:
                raise WireError('invalid-unicode') from None
        elif kind is float:
            if not math.isfinite(value):
                raise WireError('non-finite-number')
        elif kind is int:
            if value.bit_length() > limit * 4:
                raise WireError('frame-size')
        elif kind not in (bool, type(None)):
            raise WireError('value-type')
        if budget > limit:
            raise WireError('frame-size')


def encode_frame(value, max_frame_bytes=MAX_FRAME_BYTES):
    """Serialize one plain object followed by LF; never performs I/O."""
    limit = _limit(max_frame_bytes)
    _check_structure(value, limit)
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False,
                             separators=(',', ':')).encode('utf-8')
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise WireError('invalid-object') from None
    if len(encoded) > limit:
        raise WireError('frame-size')
    return encoded + b'\n'


def _pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise WireError('duplicate-key')
        out[key] = value
    return out


def _constant(_value):
    raise WireError('non-finite-number')


def _float(value):
    number = float(value)
    if not math.isfinite(number):
        raise WireError('non-finite-number')
    return number


def _decode(line, limit):
    if line.endswith(b'\r'):
        line = line[:-1]
    if not line:
        raise WireError('empty-frame')
    try:
        value = json.loads(line.decode('utf-8'), object_pairs_hook=_pairs,
                           parse_constant=_constant, parse_float=_float)
    except WireError:
        raise
    except (UnicodeError, ValueError, RecursionError):
        raise WireError('invalid-json') from None
    _check_structure(value, limit)
    return value


class JsonLineDecoder:
    """A bounded stream decoder; malformed input permanently ends this instance.

    feed() returns only after its entire chunk passes. Earlier returned frames
    cannot be rolled back; a framing error is not proof of native non-delivery.
    """
    def __init__(self, max_frame_bytes=MAX_FRAME_BYTES):
        self.limit = _limit(max_frame_bytes)
        self._buffer = bytearray()
        self._failed = False
        self._finished = False

    @property
    def pending_bytes(self):
        return len(self._buffer)

    def _require_open(self):
        if self._failed:
            raise WireError('decoder-failed')
        if self._finished:
            raise WireError('decoder-finished')

    def feed(self, chunk):
        self._require_open()
        try:
            if type(chunk) is not bytes:
                raise WireError('input-type')
            if len(chunk) > MAX_CHUNK_BYTES:
                raise WireError('chunk-size')
            frames, offset = [], 0
            while offset < len(chunk):
                newline = chunk.find(b'\n', offset)
                end = len(chunk) if newline < 0 else newline
                if len(self._buffer) + end - offset > self.limit:
                    raise WireError('frame-size')
                self._buffer.extend(chunk[offset:end])
                if newline < 0:
                    break
                if len(frames) >= MAX_FRAMES_PER_FEED:
                    raise WireError('frame-count')
                frames.append(_decode(bytes(self._buffer), self.limit))
                self._buffer.clear()
                offset = newline + 1
            return tuple(frames)
        except WireError:
            self._failed = True
            self._buffer.clear()
            raise

    def finish(self):
        self._require_open()
        self._finished = True
        if self._buffer:
            self._buffer.clear()
            self._failed = True
            raise WireError('truncated-frame')
