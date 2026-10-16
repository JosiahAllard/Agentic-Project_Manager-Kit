"""`git status` for the landing and the gate, tried again through one Windows race: another git in
the same repository rewrites .git/config by rename, and a git reading it at that moment dies with
`fatal: unable to access '<path>config': Permission denied`."""
import re
import time

from kitlib.git import refs

TRIES = 5           # tries in all
PAUSE = 0.2         # seconds between tries
# git's access_or_die() on the repository's config (or a worktree's config.worktree); the quoted
# path is git's, with backslashes or forward slashes
CONFIG_RACE = re.compile(r"unable to access '(?:[^']*[\\/])?config(?:\.worktree)?': "
                         r"Permission denied")


def config_race(why):
    """True when the failure text `why` is the config race and nothing else."""
    return bool(why) and CONFIG_RACE.search(why) is not None


def status(cwd, untracked='all'):
    """kitlib.git.refs.status(cwd, untracked): `([(xy, path)], None)` or `(None, why)`. A failure
    that is the config race is tried again, up to TRIES in all, PAUSE seconds apart; the last
    try's answer stands. Any other failure is returned at once, unchanged."""
    for i in range(TRIES):
        entries, why = refs.status(cwd, untracked=untracked)
        if entries is not None or i == TRIES - 1 or not config_race(why):
            return entries, why
        time.sleep(PAUSE)
