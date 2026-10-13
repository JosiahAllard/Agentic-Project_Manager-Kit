"""Request the human chooser; this port cannot change a chat connection."""
import os
from pathlib import Path

from optional.bridge.core.messages import DeliveryError
from .frontend import service_target
from .rpc import request


def open_connections(marker=None):
    path=Path(os.environ['LOCALAPPDATA'])/'pm-kit/agent-bridge/service.json' if marker is None else Path(marker)
    if not path.is_absolute(): raise DeliveryError('absolute-service-marker-required')
    reply=request(service_target(path),{'protocol':1,'view':'connections'})
    if (type(reply) is not dict or set(reply)!={'protocol','response'}
            or type(reply['protocol']) is not int or reply['protocol']!=1
            or type(reply['response']) is not dict or set(reply['response'])!={'requested'}
            or reply['response']['requested'] is not True):
        raise DeliveryError('owner-view-unavailable')
