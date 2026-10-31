"""The kit's default strict landing paths (owner-authored; in OWNER_PATHS).

A branch that changes a path matching one of these globs lands only with a
strong reviewer who is not its author and a --full --fresh merged gate, and
never in a batch. land.py reads this file from main, never from the branch.
Every host's hook entries and rendered agents are strict (hosts/<name>/shell/),
and so is the content they are rendered from (content/roles, content/skills). boot/ (the pre-import
check every entry runs) and each host's adapter (its hook payloads become the guard's Actions there)
are strict as the guard is.
"""

STRICT_DEFAULTS = (
    'kitlib/guard/**',
    'kitlib/lock/**',
    'kitlib/gate/**',
    'kitlib/proc/**',
    'kitlib/lockmcp/**',
    'kitlib/hubmcp/**',
    'kitlib/mcpwire/**',
    'kitlib/commit/**',
    'kitlib/records/**',
    'tools/land.py',
    'tools/gate.py',
    'boot/**',
    'hosts/*/adapter/**',
    'hosts/*/shell/hooks/**', 'hosts/*/shell/hooks-plugin/**',
    'hosts/*/shell/agents/**',
    'content/roles/**',
    'content/skills/**',
)
