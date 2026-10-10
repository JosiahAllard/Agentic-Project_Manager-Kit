"""The ledger writer's two refusals; tools/ledger.py maps them to exit codes."""


class Usage(Exception):
    """The request is malformed (an empty field, an unknown repo or provenance): exit 2."""


class Refused(Exception):
    """The request is well formed but cannot be carried out (an invalid ledger, an entry that
    would break it, git refused): exit 1."""
