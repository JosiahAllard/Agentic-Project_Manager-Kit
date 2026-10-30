"""The kit header: what every session and subagent is told at its start (docs/content.md "Kit
header"). Names no host; each host's adapter renders it into its own hook output (on Claude Code,
SessionStart and SubagentStart additionalContext).

  text(kit)   the header for the kit folder `kit` (absolute; the root kitboot bound), with the
              Dev Drive warning (NO_DEV_DRIVE) when the machine config sets no dev_drive

Skills and role files name the kit's folder `<kit>` (docs/content.md "Skills"), and a role or skill
body is not substituted on every host, so the header says once, at the start, what `<kit>` is. A
repository holds no copies of the kit's tools (decision 21 in docs/multi-host-plan.md section 7):
every call is `python <kit>/tools/<x>.py -C <checkout>`.
"""

# Said at every start while the machine config sets no dev_drive, or does not read: the guard's
# outer bound on agent writes (docs/guard.md "Write jail", "Outer bound") is off then.
NO_DEV_DRIVE = ('Warning: the machine config sets no dev_drive (the Dev Drive\'s root folder), or cannot be '
                'read, so the guard does not hold agent writes to the Dev Drive. The owner sets dev_drive '
                '(docs/kit-json.md "Machine config keys").')

_READ = object()


def dev_drive_note(user=_READ):
    """NO_DEV_DRIVE when `user` (a machine config as kitlib.config.user.user_config returns it;
    default: this machine's, read here) sets no dev_drive or does not read; else None. A process
    under the kit's home fence (kitlib.config.user.fenced: the kit's tests and their children)
    reads no machine config here and says nothing."""
    from kitlib.config import user as kuser
    if user is _READ:
        if kuser.fenced():
            return None
        try:
            user = kuser.user_config()
        except Exception:  # noqa: BLE001 - a config that does not read leaves the bound off: say so
            user = None
    return None if isinstance(user, dict) and kuser.dev_drive(user) else NO_DEV_DRIVE


def text(kit, user=_READ):
    k = kit.replace('\\', '/').rstrip('/')
    head = ('PM kit header. The kit folder is %s. In the kit\'s skills and role files, `<kit>` means '
            'that folder: `python <kit>/tools/machine_lock.py status` is '
            '`python "%s/tools/machine_lock.py" status`. The kit\'s tools run from that folder, never '
            'from a copy in the repository: a tool that acts on a checkout is named with -C, as in '
            '`python <kit>/tools/preflight.py -C <worktree>`, and runs the same from any directory. '
            'Every other name in angle brackets (`<worktree>`, `<repo>`, `<slug>`, `<branch>` and the '
            'like) is a placeholder you fill in from your brief, never a folder to look for.'
            % (k, k))
    note = dev_drive_note(user)
    return head + ('\n' + note if note else '')
