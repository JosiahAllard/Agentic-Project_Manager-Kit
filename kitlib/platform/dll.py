"""Windows DLLs bound at first use, never at import (docs/linux-support-plan.md section 2.1, rule 3).

  windll(name, **kw)          ctypes.WinDLL(name, **kw) where ctypes has it (Windows); elsewhere
                              PlatformUnsupported naming the DLL and the platform
  LazyDLL(name, prototypes, loader=windll, **kw)
                              stands for that DLL: the DLL is loaded when the first function is
                              asked for, and each function gets its (restype, argtypes) from
                              `prototypes` when it is first asked for, then is kept

So every kit module imports on any platform, and a call off Windows fails with a clear error. On
Windows a missing DLL or export fails at first use instead of at import; the AttributeError ctypes
raises names the function, and it is raised again at every later ask, since nothing is kept for a
function that failed. Attributes starting with `_` are never looked up in the DLL, so copy, mock
and pickle probes get a plain AttributeError.
"""
import ctypes

from kitlib import platform


# ctypes has WinDLL on Windows alone. windll() asks it, not kitlib.platform.NAME, which a test
# patches to stand for another platform: a DLL first loaded under that patch still loads.
_WINDLL = getattr(ctypes, 'WinDLL', None)


def windll(name, **kw):
    if _WINDLL is None:
        raise platform.PlatformUnsupported('the Windows DLL %s cannot load on this platform (%s)'
                                           % (name, platform.NAME))
    return _WINDLL(name, **kw)


class LazyDLL(object):
    def __init__(self, name, prototypes=None, loader=None, **kw):
        self.__dict__.update(_name=name, _prototypes=dict(prototypes or {}),
                             _loader=loader or windll, _kw=kw, _dll=None)

    def _load(self):
        if self._dll is None:
            self.__dict__['_dll'] = self._loader(self._name, **self._kw)
        return self._dll

    def __getattr__(self, name):
        if name.startswith('_'):
            raise AttributeError(name)
        function = getattr(self._load(), name)
        prototype = self._prototypes.get(name)
        if prototype is not None:
            function.restype, function.argtypes = prototype
        self.__dict__[name] = function
        return function

    def __repr__(self):
        return '<LazyDLL %s%s>' % (self._name, '' if self._dll is None else ' (loaded)')
