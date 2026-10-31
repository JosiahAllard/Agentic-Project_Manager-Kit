"""The launcher's machine settings: the `launcher` object of the machine config
(docs/launcher.md "Settings"). Nothing here names a machine path, an account folder or a repo:
each comes from this object, and a missing one is an error that says which key to set.

  state_dir           absolute folder holding sessions.json and coordinators.json (the launched
                      sessions and the PM's record of them) and the launcher's logs
  repos               {name: absolute folder}: what a sessions.json entry's "repo" and
                      `launch.py start --repo` resolve to
  accounts            {"<n>": [config folder, ...]}: a Claude account's config folders, first
                      choice first; a folder is absolute or starts with ~ (the home folder).
                      Unset: one account, "1", in the default folder ~/.claude
  orgs                {"<n>": config folder}: organization sign-ins (`--org n`)
  coordinator_agent   the agent file a coordinator session runs as, bare or with the kit plugin's
                      prefix (kitlib.agentname); unset: coordinator
  pm_resume_beside_desktop
                      true: after-reboot resumes the PM session even while the desktop app or an
                      unnamed Claude session runs (reboot.resume_pm); unset or false: it skips
  terminal            Linux: the terminal command a window opens in, as a list of words, with
                      `/bin/bash -c <script>` appended (kitlib.launcher.linuxwin); unset: a
                      detached tmux session
  usage_crossover     how far the hub's usage crossover job goes (kitlib.hubjobs.usage_crossover):
                      notify, normal or full; unset: notify, which stops and starts nothing
"""
import os
import re

from kitlib import agentname
from kitlib.config import user as kuser
from kitlib.config.types import ConfigError

KEY = 'launcher'
DEFAULT_FOLDER = '.claude'          # Claude Code's own config folder under the home folder
DEFAULT_ACCOUNTS = {'1': ['~/' + DEFAULT_FOLDER]}
DEFAULT_COORDINATOR_AGENT = 'coordinator'
_NUM = re.compile(r'^[1-9][0-9]?$')
# an agent name, bare or with the kit plugin's prefix (the same pattern as window.AGENT)
_AGENT = re.compile(r'^(?:%s)?[A-Za-z0-9_-]+$' % re.escape(agentname.PREFIX))
KNOWN = ('state_dir', 'repos', 'accounts', 'orgs', 'coordinator_agent', 'pm_resume_beside_desktop',
         'terminal', 'usage_crossover')
NOTIFY, NORMAL, FULL = 'notify', 'normal', 'full'   # usage_crossover's values
CROSSOVER_MODES = (NOTIFY, NORMAL, FULL)


class LaunchError(Exception):
    """A refusal or failure, said in one message. `lines` are what was said before it."""

    def __init__(self, msg, lines=None):
        Exception.__init__(self, msg)
        self.lines = list(lines or [])


def _folder(v, where, errs):
    if not isinstance(v, str) or not v.strip():
        errs.append('%s must be a folder path' % where)
    elif not (os.path.isabs(v) or v == '~' or v.startswith(('~/', '~\\'))):
        errs.append('%s must be absolute or start with ~, not %r' % (where, v))


def check(obj):
    """The errors in a `launcher` object, as messages ([] when it is good)."""
    errs = []
    if obj is None:
        return errs
    if not isinstance(obj, dict):
        return ['launcher must be an object']
    for k in obj:
        if k not in KNOWN:
            errs.append('launcher.%s is not a launcher key (%s)' % (k, ', '.join(KNOWN)))
    sd = obj.get('state_dir')
    if sd is not None and (not isinstance(sd, str) or not os.path.isabs(sd)):
        errs.append('launcher.state_dir must be an absolute path, not %r' % (sd,))
    repos = obj.get('repos')
    if repos is not None:
        if not isinstance(repos, dict):
            errs.append('launcher.repos must be an object of name: absolute folder')
        else:
            for n, p in repos.items():
                if not isinstance(p, str) or not os.path.isabs(p):
                    errs.append('launcher.repos.%s must be an absolute path, not %r' % (n, p))
    accts = obj.get('accounts')
    if accts is not None:
        if not isinstance(accts, dict) or not accts:
            errs.append('launcher.accounts must be a non-empty object of "<n>": [folder, ...]')
        else:
            for n, dirs in accts.items():
                if not _NUM.match(str(n)):
                    errs.append('launcher.accounts key %r must be a number 1..99' % (n,))
                if not isinstance(dirs, list) or not dirs:
                    errs.append('launcher.accounts.%s must be a non-empty list of folders' % n)
                    continue
                for i, d in enumerate(dirs):
                    _folder(d, 'launcher.accounts.%s[%d]' % (n, i), errs)
    orgs = obj.get('orgs')
    if orgs is not None:
        if not isinstance(orgs, dict):
            errs.append('launcher.orgs must be an object of "<n>": folder')
        else:
            for n, d in orgs.items():
                if not _NUM.match(str(n)):
                    errs.append('launcher.orgs key %r must be a number 1..99' % (n,))
                _folder(d, 'launcher.orgs.%s' % n, errs)
    ag = obj.get('coordinator_agent')
    if ag is not None and (not isinstance(ag, str) or not _AGENT.match(ag)):
        errs.append('launcher.coordinator_agent must be an agent name, not %r' % (ag,))
    pr = obj.get('pm_resume_beside_desktop')
    if pr is not None and not isinstance(pr, bool):
        errs.append('launcher.pm_resume_beside_desktop must be true or false, not %r' % (pr,))
    term = obj.get('terminal')
    if term is not None and (not isinstance(term, list) or not term
                             or not all(isinstance(w, str) and w.strip() for w in term)):
        errs.append('launcher.terminal must be a non-empty list of words (a command), not %r' % (term,))
    uc = obj.get('usage_crossover')
    if uc is not None and uc not in CROSSOVER_MODES:
        errs.append('launcher.usage_crossover must be one of %s, not %r'
                    % (', '.join(CROSSOVER_MODES), uc))
    return errs


class Settings(object):
    """The checked `launcher` object, with its defaults. `home` is the folder ~ stands for."""

    def __init__(self, obj=None, home=None):
        errs = check(obj)
        if errs:
            raise ConfigError('; '.join(errs))
        obj = obj or {}
        self.home = home or kuser.home_dir()
        self._state_dir = obj.get('state_dir')
        self.repos = dict(obj.get('repos') or {})
        self.accounts = {str(k): list(v) for k, v in (obj.get('accounts') or DEFAULT_ACCOUNTS).items()}
        self.orgs = {str(k): v for k, v in (obj.get('orgs') or {}).items()}
        self.coordinator_agent = obj.get('coordinator_agent') or DEFAULT_COORDINATOR_AGENT
        self.pm_resume_beside_desktop = obj.get('pm_resume_beside_desktop') is True
        self.terminal = list(obj['terminal']) if obj.get('terminal') else None
        self.usage_crossover = obj.get('usage_crossover') or NOTIFY

    def expand(self, folder):
        """A configured folder made absolute: ~ is the home folder."""
        if folder == '~':
            return self.home
        if folder.startswith(('~/', '~\\')):
            return os.path.normpath(os.path.join(self.home, folder[2:]))
        return os.path.normpath(folder)

    def default_folder(self):
        return os.path.normpath(os.path.join(self.home, DEFAULT_FOLDER))

    def is_default(self, folder):
        """True for Claude Code's own folder (~/.claude): CLAUDE_CONFIG_DIR is then left unset,
        since setting it, even to that folder, moves where Claude keeps its .claude.json."""
        return os.path.normcase(os.path.normpath(folder)) == os.path.normcase(self.default_folder())

    def account_numbers(self):
        return sorted(self.accounts, key=int)

    def account_dirs(self, n):
        n = str(n)
        if n not in self.accounts:
            raise LaunchError('account %s is not in launcher.accounts (accounts: %s)'
                              % (n, ', '.join(self.account_numbers())))
        return [self.expand(d) for d in self.accounts[n]]

    def org_dir(self, n):
        n = str(n)
        if n not in self.orgs:
            raise LaunchError('org %s is not in launcher.orgs (orgs: %s)'
                              % (n, ', '.join(sorted(self.orgs, key=int)) or 'none'))
        return self.expand(self.orgs[n])

    def state_dir(self):
        if not self._state_dir:
            raise LaunchError('launcher.state_dir is not set in %s: the folder holding sessions.json '
                              'and coordinators.json (docs/launcher.md "Settings")' % kuser.key_file(self.home, KEY))
        return self._state_dir

    def repo_path(self, name):
        if name in self.repos:
            return self.repos[name]
        raise LaunchError('repo %r is not in launcher.repos of %s (known: %s); give --repo-path instead'
                          % (name, kuser.key_file(self.home, KEY), ', '.join(sorted(self.repos)) or 'none'))


def load(home=None):
    """The machine's launcher Settings (a ConfigError for a bad config)."""
    return Settings(kuser.user_config(home).get(KEY), home)
