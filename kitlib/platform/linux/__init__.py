"""The Linux backend of the seam: every primitive the contract names (kitlib.platform.api). An area
whose slice of docs/linux-support-plan.md section 8 has been built takes its primitives from that
area's module (BUILT); every other primitive is a stub that raises PlatformUnsupported naming
itself and the slice that builds it. Never a primitive that silently does less (section 2.1,
rule 5)."""
from kitlib.platform import PlatformUnsupported, api
from kitlib.platform.linux import hardware

BUILT = {'hardware': hardware}      # area -> the module that provides its primitives (slice 6)


def _stub(name, area, slice_number):
    def primitive(*args, **kwargs):
        raise PlatformUnsupported('%s is not built on Linux yet (the %s area, slice %d of '
                                  'docs/linux-support-plan.md section 8)' % (name, area, slice_number))
    primitive.__name__ = primitive.__qualname__ = name
    return primitive


for _area, (_names, _slice) in api.AREAS.items():
    for _name in _names:
        globals()[_name] = (getattr(BUILT[_area], _name) if _area in BUILT
                            else _stub(_name, _area, _slice))

__all__ = list(api.NAMES)
