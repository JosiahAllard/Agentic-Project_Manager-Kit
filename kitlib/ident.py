"""Bounded identifier, digest and integer checks for opaque ids carried between processes.

Promoted from the bridge's Phase 0 model (docs/multi-host-plan.md section 2.4), where every bridge
record used them. Each check returns its value unchanged or raises IdentError with a content-free
code, so a refused value is never echoed back.
"""
import re

ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z')
HEX = re.compile(r'[0-9a-f]{64}\Z')
MAX_INTEGER = (1 << 53) - 1


class IdentError(ValueError):
    """A refused id, digest or integer. `code` names the check, never the value."""

    def __init__(self, code):
        self.code = code
        super().__init__('value refused: ' + code)


def identifier(value):
    """An opaque id: 1-128 characters, letters and digits first, then also _ . : -"""
    if type(value) is not str or not ID.fullmatch(value):
        raise IdentError('invalid-identifier')
    return value


def digest(value):
    """A lowercase hex SHA-256."""
    if type(value) is not str or not HEX.fullmatch(value):
        raise IdentError('invalid-digest')
    return value


def integer(value, maximum=MAX_INTEGER):
    """A positive int (bool refused) no larger than `maximum`."""
    if type(value) is not int or not 1 <= value <= maximum:
        raise IdentError('invalid-integer')
    return value
