"""Explicit native executable sets; process basenames never identify a host."""
from pathlib import Path

from kitlib.ident import identifier
from optional.bridge.core.messages import DeliveryError
from .evidence import regular


def path_refusal(role,error):
    """The refusal for a configured path that fails its check, naming the setting (`role`, as
    'codex.native-image.0' or 'codex.desktop_executable') and never the path: missing when it does
    not exist, invalid when it is a link, a reparse point or the wrong kind."""
    word='missing' if isinstance(error,FileNotFoundError) else 'invalid'
    code=('runtime-path-%s-%s'%(word,role)).replace('.','-').replace('_','-').lower()
    if len(code)>64 or any(character not in 'abcdefghijklmnopqrstuvwxyz0123456789-' for character in code):
        code='runtime-path-'+word
    return DeliveryError(code)


def native_hosts(values,*,named=False):
    """The checked, resolved native images per host kind. `named` (the runtime configuration check)
    refuses a missing or wrong-kind image by its setting (path_refusal); otherwise the check's own
    error propagates (the frontend inspector the supervisor assembles)."""
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
        for index,image in enumerate(images):
            if not isinstance(image,(str,Path)) or not Path(image).is_absolute():
                raise DeliveryError('invalid-native-image')
            try: regular(image)
            except Exception as error:
                if not named: raise
                raise path_refusal('%s.native-image.%d'%(kind,index),error) from None
            path=Path(image).resolve(strict=True)
            if path in seen: raise DeliveryError('ambiguous-native-image')
            seen.add(path)
            paths.append(path)
        result[kind]=tuple(paths)
    return result
