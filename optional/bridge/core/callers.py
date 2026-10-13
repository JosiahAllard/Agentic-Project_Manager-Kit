"""Private, bounded one-use caller proofs from a trusted native frontend port."""
import secrets
import threading
import time

from .messages import DeliveryError, MessageIntent, SenderAuthority


class CallProofs:
    def __init__(self, *, observe=None, clock=time.monotonic):
        if observe is not None and not callable(observe):
            raise DeliveryError('invalid-caller-observer')
        self._observe,self._clock=observe,clock
        self._pending,self._lock={},threading.RLock()

    def observe(self, metadata):
        try:
            authority=self._observe(metadata) if self._observe is not None else None
        except Exception:
            authority=None
        if type(authority) is not SenderAuthority:
            raise DeliveryError('caller-context-unverified')
        return authority

    def issue(self, intent, metadata):
        if type(intent) is not MessageIntent:
            raise DeliveryError('invalid-caller-intent')
        authority=self.observe(metadata)
        with self._lock:
            now=self._clock()
            self._pending={key:value for key,value in self._pending.items() if value[2]>now}
            if len(self._pending)>=256:
                raise DeliveryError('caller-proof-capacity')
            proof=secrets.token_bytes(32)
            self._pending[proof]=(intent,authority,now+5)
            return proof

    def authorize(self, intent, proof):
        if type(proof) is not bytes: return None
        with self._lock:
            record=self._pending.pop(proof,None)
            if record is None or record[0]!=intent or record[2]<=self._clock(): return None
            return record[1]
