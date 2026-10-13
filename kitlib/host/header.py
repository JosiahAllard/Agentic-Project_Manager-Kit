"""The kit header: what every session and subagent is told at its start (docs/content.md "Kit
header"). Names no host; each host's adapter renders it into its own hook output (on Claude Code,
SessionStart and SubagentStart additionalContext).

  text(kit)   the header for the kit folder `kit` (absolute; the root kitboot bound)

Skills and role files name the kit's folder `<kit>` (docs/content.md "Skills"), and a role or skill
body is not substituted on every host, so the header says once, at the start, what `<kit>` is. A
repository holds no copies of the kit's tools (decision 21 in docs/multi-host-plan.md section 7):
every call is `python <kit>/tools/<x>.py -C <checkout>`.
"""


def text(kit):
    k = kit.replace('\\', '/').rstrip('/')
    return ('PM kit header. The kit folder is %s. In the kit\'s skills and role files, `<kit>` means '
            'that folder: `python <kit>/tools/machine_lock.py status` is '
            '`python "%s/tools/machine_lock.py" status`. The kit\'s tools run from that folder, never '
            'from a copy in the repository: a tool that acts on a checkout is named with -C, as in '
            '`python <kit>/tools/preflight.py -C <worktree>`, and runs the same from any directory. '
            'Every other name in angle brackets (`<worktree>`, `<repo>`, `<slug>`, `<branch>` and the '
            'like) is a placeholder you fill in from your brief, never a folder to look for.'
            % (k, k))
