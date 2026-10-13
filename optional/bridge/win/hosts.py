"""Explicit native executable sets; process basenames never identify a host."""
from pathlib import Path

from kitlib.ident import identifier
from optional.bridge.core.messages import DeliveryError
from .evidence import regular


def native_hosts(values):
    if type(values) is not dict or not 0<len(values)<=16:
        raise DeliveryError('invalid-native-hosts')
    result,seen={},set()
    for kind,value in values.items():
        kind=identifier(kind)
        if any(character not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for character in kind):
            raise DeliveryError('invalid-native-host-kind')
        images=value if type(value) in (list,tuple) else (value,)
        if not 0<len(images)<=8: raise DeliveryError('invalid-native-images')
        paths=[]
        for image in images:
            if not isinstance(image,(str,Path)) or not Path(image).is_absolute():
                raise DeliveryError('invalid-native-image')
            regular(image)
            path=Path(image).resolve(strict=True)
            if path in seen: raise DeliveryError('ambiguous-native-image')
            seen.add(path)
            paths.append(path)
        result[kind]=tuple(paths)
    return result
