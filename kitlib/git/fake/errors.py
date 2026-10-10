"""How the fake git stops.

Unsupported derives from BaseException on purpose: kit code catches Exception around git calls
(a failed call is an expected answer), and a fake that met something it cannot do must never look
like an answer. unittest records it as an error. Kit code that catches BaseException on purpose
turns it into its own failure (kitlib/gate/land.py: NOT LANDED or STUCK; kitlib/git/batch.py's
reader: a dead git), and land.py's best-effort steps after a verified push drop it.
"""


class Unsupported(BaseException):
    """The fake cannot do this. Never caught inside the fake; never turned into real git."""

    def __init__(self, what):
        super().__init__('unsupported in fake git: %s' % what)


class GitExit(Exception):
    """git ended with `rc`, having written `err` (text, already formatted) to stderr."""

    def __init__(self, rc, err=''):
        super().__init__(err)
        self.rc = rc
        self.err = err


def die(msg):
    """git's die(): `fatal: msg`, exit 128."""
    return GitExit(128, 'fatal: %s\n' % msg)


def usage(msg):
    """A usage error: exit 129."""
    return GitExit(129, 'error: %s\n' % msg)


def error(msg, rc=1):
    return GitExit(rc, 'error: %s\n' % msg)
