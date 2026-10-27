"""Which host may send to which: the explicit host-pair rule of the bridge.

The served peers and the refused (sender host, recipient host) pairs are data
(optional/bridge/peers.json, read by registry.host_pairs()); nothing here names
a host. A binding belongs to the peer its adapter id names: the id is the
peer's name or starts with the name and '-', and the longest such name wins.
An adapter id no listed peer names is outside the rule, so the service refuses
to install a half's adapter whose id does not name that half's peer
(Controller.adapter(peer=)). Every pair of listed
peers is allowed unless it is listed as refused, so a newly listed peer can
reach and be reached by every other one without a hand-written pair list.
"""
import re

from .messages import DeliveryError
from .pairing import ChatBinding

PEER_NAME = re.compile(r'[a-z][a-z0-9-]{0,31}\Z')
MAX_PEERS = 32
REFUSED = 'host-pair-refused'


class HostPairs:
    def __init__(self, peers, refused=()):
        try:
            peers, refused = tuple(peers), tuple(tuple(pair) for pair in refused)
        except TypeError:
            raise DeliveryError('invalid-host-pairs') from None
        # Types first: the duplicate checks below hash every name and pair.
        if (not 0 < len(peers) <= MAX_PEERS
                or any(type(name) is not str or not PEER_NAME.fullmatch(name) for name in peers)
                or len(set(peers)) != len(peers)
                or any(len(pair) != 2 or any(type(name) is not str or name not in peers for name in pair)
                       for pair in refused)
                or len(set(refused)) != len(refused)):
            raise DeliveryError('invalid-host-pairs')
        self.peers, self.refused = peers, frozenset(refused)

    def peer_of(self, adapter_id):
        """The listed peer `adapter_id` names, or None."""
        if type(adapter_id) is not str:
            return None
        found = [name for name in self.peers if adapter_id == name or adapter_id.startswith(name + '-')]
        return max(found, key=len) if found else None

    def host_of(self, binding):
        """The listed peer `binding`'s adapter id names, or None."""
        if type(binding) is not ChatBinding:
            raise DeliveryError('invalid-host-pair-binding')
        return self.peer_of(binding.identity.adapter_id)

    def allowed(self, sender, recipient):
        """False only when both ends are listed peers and their pair is refused."""
        pair = (self.host_of(sender), self.host_of(recipient))
        return None in pair or pair not in self.refused

    def check(self, sender, recipient):
        if not self.allowed(sender, recipient):
            raise DeliveryError(REFUSED)
