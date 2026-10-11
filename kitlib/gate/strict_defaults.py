"""The kit's default strict landing paths (owner-authored; in OWNER_PATHS).

A branch that changes a path matching one of these globs lands only with a
strong reviewer who is not its author and a --full --fresh merged gate, and
never in a batch. land.py reads this file from main, never from the branch.
A repo adds paths with .kit.json landing.strict_paths; it cannot remove these.
"""

STRICT_DEFAULTS = (
    'kitlib/guard/**',
    'kitlib/lock/**',
    'kitlib/gate/**',
    'kitlib/proc/**',
    'kitlib/lockmcp/**',
    'claude/hooks/**',
    'claude/agents/**',
)
