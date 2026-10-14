"""Bounded JSON-lines RPC for host adapters, with reentrant server requests.

Owns no process or policy. A host provides streams, event/request callbacks and lifetime.
No command, process, approval or messaging operation is exposed by this transport itself.
"""
import json
import copy
from collections import OrderedDict
import queue
import threading
import time


class Client:
    def __init__(self, incoming, outgoing, timeout=30, on_event=None, on_request=None):
        if not 0 < timeout <= 3600:
            raise ValueError('invalid RPC timeout')
        self.incoming, self.outgoing = incoming, outgoing
        self.timeout, self.on_event, self.on_request = timeout, on_event, on_request
        self.messages, self.responses, self.pending = queue.Queue(maxsize=256), {}, set()
        self.terminal = None
        self.serial, self.lock = 0, threading.RLock()
        self.active_server, self.deferred, self.server_results = None, [], OrderedDict()
        self.server_seen = set()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        try:
            while True:
                line = self.incoming.readline(8 * 1024 * 1024 + 1)
                if not line:
                    break
                if len(line) > 8 * 1024 * 1024:
                    raise RuntimeError('RPC response exceeds the size limit')
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise RuntimeError('invalid RPC response')
                self.messages.put_nowait(value)
        except Exception as error:
            self.terminal = RuntimeError('RPC read failed: %s' % error)
        finally:
            try:
                self.incoming.close()
            except Exception:
                pass
            if self.terminal is None:
                self.terminal = EOFError('local app-server proxy closed')

    def send(self, message):
        self.outgoing.write(json.dumps(message) + '\n')
        self.outgoing.flush()

    def _handle(self, message):
        if 'method' in message:
            if 'id' in message:
                key = message['id']
                if not isinstance(key, (str, int)) or isinstance(key, bool):
                    raise RuntimeError('invalid server RPC id')
                signature = copy.deepcopy((message['method'], message.get('params') or {}))
                if key in self.server_results:
                    previous, response = self.server_results[key]
                    if previous != signature:
                        raise RuntimeError('server reused an RPC id for a different request')
                    self.send(response)
                    return
                if self.active_server is not None:
                    if len(self.deferred) >= 64:
                        raise RuntimeError('too many deferred server requests')
                    self.deferred.append(message)
                    return
                if key in self.server_seen:
                    raise RuntimeError('server request outcome expired; refusing to reexecute')
                if len(self.server_seen) >= 4096:
                    raise RuntimeError('server request limit reached')
                self.server_seen.add(key)
                self.active_server = key
                try:
                    if self.on_request is None:
                        raise ValueError('unsupported server request')
                    result = self.on_request(message['method'], message.get('params') or {})
                    response = {'id': key, 'result': result}
                except Exception as error:
                    response = {'id': key, 'error': {'code': -32601, 'message': str(error)}}
                finally:
                    self.active_server = None
                self.server_results[key] = (signature, response)
                if len(self.server_results) > 64:
                    self.server_results.popitem(last=False)
                self.send(response)
                while self.deferred:
                    self._handle(self.deferred.pop(0))
            elif self.on_event is not None:
                self.on_event(message['method'], message.get('params') or {})
        elif message.get('id') in self.pending:
            self.responses[message['id']] = message

    def _next(self, timeout):
        deadline = time.monotonic() + timeout
        while True:
            try:
                message = self.messages.get_nowait()
            except queue.Empty:
                if self.terminal is not None:
                    raise self.terminal
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError('local app-server RPC timed out') from None
                try:
                    message = self.messages.get(timeout=min(left, .1))
                except queue.Empty:
                    continue
            self._handle(message)
            return

    def request(self, method, params=None, timeout=None):
        with self.lock:
            self.serial += 1
            ident = self.serial
            self.pending.add(ident)
            deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
            try:
                self.send({'id': ident, 'method': method, 'params': params or {}})
                while ident not in self.responses:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise TimeoutError('local app-server RPC timed out')
                    self._next(left)
                result = self.responses.pop(ident)
                if 'error' in result:
                    raise RuntimeError('app-server request refused: %s' %
                                       result['error'].get('message', 'error'))
                return result.get('result', {})
            finally:
                self.pending.discard(ident)
                self.responses.pop(ident, None)

    def pump(self, timeout=1):
        """Handle one event/request while no synchronous request is pending."""
        with self.lock:
            self._next(timeout)
