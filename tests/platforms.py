"""Platform test markers: a test class or method that runs on one platform only, with its reason
(docs/test-tools.md "Platform markers and the ratchet").

    @windows_only('a directory junction is a Windows feature')
    class JunctionTest(unittest.TestCase): ...

    @posix_only('a file mode bit')
    def test_mode(self): ...

Each marker records its reason in REASONS, keyed 'module.qualname' of what it marks, and applies
unittest.skipUnless with the reason as the skip text, so the class or method carries
`__unittest_skip__` as a hand-written skipUnless does (kit_tests --list counts it the same) and
tools/ci_ratchet.py counts the markers by reason. The platform is read when the marker is applied,
from WINDOWS and POSIX below. A marker with no reason is refused.

This module imports no kitlib: on Linux the kitlib import chain does not load yet (docs/
linux-support-plan.md section 8, slice 2), and a test module must be able to name its markers."""
import os
import unittest

WINDOWS = os.name == 'nt'
POSIX = os.name == 'posix'

REASONS = {}        # 'module.qualname' -> (platform, reason)


def windows_only(reason):
    """Class or method decorator: run on Windows only; `reason` says why."""
    return _marker('windows', 'WINDOWS', reason)


def posix_only(reason):
    """Class or method decorator: run on a POSIX platform (Linux, macOS) only; `reason` says why."""
    return _marker('posix', 'POSIX', reason)


def _marker(platform, flag, reason):
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError('a %s_only marker needs its reason, a non-empty string: %r'
                         % (platform, reason))

    def mark(obj):
        REASONS['%s.%s' % (obj.__module__, obj.__qualname__)] = (platform, reason)
        return unittest.skipUnless(globals()[flag], reason)(obj)
    return mark
