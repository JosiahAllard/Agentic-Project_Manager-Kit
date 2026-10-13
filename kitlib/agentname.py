"""Agent names with and without the kit plugin's prefix (docs/guard.md "Agent names").

A host names an agent a plugin bundles by its hosts.json profile agent_name_format (Claude Code:
`<plugin>:<name>`); the kit's plugin is kitlib.lockmcp.PLUGIN_NAME, so a kit agent can reach the
guard, the launcher and the usage reports both as `lander` and as `PREFIX + 'lander'`. PREFIX is
the default host's format up to {name}, read once at import. Two rules, because stripping more is the
permissive direction for one and the restrictive direction for the other:

  bare(name)       an agent type (a hook payload's agent_type, a launcher --agent): exactly one
                   leading PREFIX, case-sensitive, is removed. Any other form (another plugin's
                   prefix, the prefix twice, another case, the prefix alone) comes back unchanged,
                   so it names no known role and is held like any other subagent.
  bare_mark(mark)  a session mark (PM_KIT_ROLE): the prefix is removed case-insensitively, as
                   often as it is repeated, before the mark is compared or checked against the
                   reserved marks. A mark that would be left empty comes back unchanged: an empty
                   mark reads as an unmarked session, which is the owner's.
  qualified(name)  PREFIX + bare(name).
"""
from kitlib.host import registry
from kitlib.lockmcp import PLUGIN_NAME


def prefix(host=None, plugin=PLUGIN_NAME, path=None):
    """The qualified-name prefix of `host` (None: the default host): its agent_name_format up to
    {name}, filled in with `plugin`."""
    name = registry.default(path) if host is None else host
    fmt = registry.host(name, path).profile.agent_name_format
    return fmt.split('{name}', 1)[0].format(plugin=plugin)


PREFIX = prefix()


def bare(name):
    """`name` without one leading PREFIX (exact case); anything else unchanged."""
    if isinstance(name, str) and name.startswith(PREFIX):
        return name[len(PREFIX):]
    return name


def bare_mark(mark):
    """The session mark `mark` without leading PREFIXes (case ignored, surrounding spaces
    dropped); `mark` itself when nothing would be left, or when it is not a string."""
    if not isinstance(mark, str):
        return mark
    out = mark.strip()
    while out[:len(PREFIX)].lower() == PREFIX.lower():
        out = out[len(PREFIX):].strip()
    return out or mark


def qualified(name):
    """The plugin-qualified form of the agent `name`."""
    return PREFIX + bare(name)
