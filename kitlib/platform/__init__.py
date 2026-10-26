"""kitlib.platform: the one place that knows the operating system (docs/linux-support-plan.md
section 2.1, "The seam").

  NAME                  'windows', 'linux' or 'unsupported', from sys.platform when kitlib
                        loads. Code reads it as kitlib.platform.NAME at call time, so a test can
                        patch it.
  PlatformUnsupported   an OSError: the running platform cannot do what was asked. The kit
                        raises it for a primitive with no backend (any platform but Windows and
                        Linux, darwin included), for a Linux primitive not built yet, and for a
                        Windows DLL asked for off Windows (kitlib.platform.dll).
  backend()             the running platform's backend module (kitlib.platform.windows or
                        kitlib.platform.linux); PlatformUnsupported on any other platform.
  kitlib.platform.<n>   a primitive the contract names (kitlib.platform.api.NAMES), from
                        backend().

This module imports no other kitlib module, so the Windows bindings (kitlib.proc.win32, which
asks kitlib.platform.dll for its DLLs) can import it without a cycle. The backend is imported only
when a primitive is first asked for.
"""
import importlib
import sys


def _pick(platform):
    if platform == 'win32':
        return 'windows'
    if platform.startswith('linux'):
        return 'linux'
    return 'unsupported'


NAME = _pick(sys.platform)


class PlatformUnsupported(OSError):
    """The running platform cannot do what was asked: the message names it and the thing asked."""


def backend():
    """The backend module of the running platform (NAME read now)."""
    if NAME not in ('windows', 'linux'):
        raise PlatformUnsupported('the kit has no backend for this platform (%s)' % sys.platform)
    return importlib.import_module('kitlib.platform.' + NAME)


def __getattr__(name):
    api = importlib.import_module('kitlib.platform.api')   # never `from . import`: it asks here
    if name in api.NAMES:
        return getattr(backend(), name)
    raise AttributeError('module %r has no attribute %r' % (__name__, name))
